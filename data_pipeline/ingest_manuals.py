import os
import io
import tempfile
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import chromadb

from pypdf import PdfReader
import docx
from pptx import Presentation
import openpyxl

def extract_documents_from_file(temp_path, ext, base_meta):
    docs = []
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)

    if ext == 'pdf':
        reader = PdfReader(temp_path)
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                meta = base_meta.copy()
                meta["location"] = f"{i + 1}페이지"
                for chunk in text_splitter.split_text(text):
                    docs.append(Document(page_content=chunk, metadata=meta))

    elif ext == 'docx':
        doc = docx.Document(temp_path)
        full_text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        if full_text:
            meta = base_meta.copy()
            meta["location"] = "본문"
            for chunk in text_splitter.split_text(full_text):
                docs.append(Document(page_content=chunk, metadata=meta))

    elif ext == 'pptx':
        prs = Presentation(temp_path)
        for i, slide in enumerate(prs.slides):
            slide_texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        if paragraph.text.strip():
                            slide_texts.append(paragraph.text.strip())
            full_slide_text = "\n".join(slide_texts)
            if full_slide_text:
                meta = base_meta.copy()
                meta["location"] = f"슬라이드 {i + 1}장"
                for chunk in text_splitter.split_text(full_slide_text):
                    docs.append(Document(page_content=chunk, metadata=meta))

    elif ext == 'xlsx':
        wb = openpyxl.load_workbook(temp_path, data_only=True)
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) < 2:
                continue
            headers = [str(h).strip() if h is not None else f"열{idx+1}" for idx, h in enumerate(rows[0])]
            for r_idx, row in enumerate(rows[1:], start=2):
                row_items = []
                for h, val in zip(headers, row):
                    if val is not None and str(val).strip() != "":
                        row_items.append(f"{h}: {str(val).strip()}")
                if row_items:
                    row_text = f"[{sheet} 시트] " + " | ".join(row_items)
                    meta = base_meta.copy()
                    meta["location"] = f"{sheet} 시트 {r_idx}행"
                    docs.append(Document(page_content=row_text, metadata=meta))

    return docs


def ingest_manuals_to_chroma():
    print("=== [Step 1-B: 매뉴얼 및 검토보고서 Vector DB 완전 동기화(추가/수정/삭제) 시작] ===\n")

    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    manual_folder_id = os.getenv("MANUAL_FOLDER_ID")
    chroma_path = os.getenv("CHROMA_DB_PATH")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")

    # 1. 로컬 DB 연결 및 기존 파일 목록 확보
    chroma_client = chromadb.PersistentClient(path=chroma_path)
    collection = chroma_client.get_or_create_collection(name="bim_manuals")

    existing_data = collection.get(include=["metadatas"])
    existing_files = {}
    for meta in existing_data.get("metadatas", []):
        if meta and "file_id" in meta:
            existing_files[meta["file_id"]] = meta.get("modified_time", "")

    print(f"1. 현재 Vector DB에 등록된 문서 파일 수: {len(existing_files)}개")

    # 2. 구글 드라이브 스캔
    creds = Credentials.from_service_account_file(
        key_path, scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    drive_service = build('drive', 'v3', credentials=creds)

    query = f"'{manual_folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(
        q=query,
        pageSize=1000,
        fields="files(id, name, fileExtension, webViewLink, modifiedTime)"
    ).execute()

    drive_files = results.get('files', [])
    print(f"2. 구글 드라이브 매뉴얼 폴더 내 전체 파일 수: {len(drive_files)}개\n")

    # [핵심 로직] 삭제된 파일(Orphan Data) 감지 및 Vector DB에서 제거
    drive_file_ids = {file.get('id') for file in drive_files}
    deleted_ids = set(existing_files.keys()) - drive_file_ids
    deleted_count = 0

    if deleted_ids:
        print("3. [삭제 감지] 구글 드라이브에서 제거된 파일 데이터를 Vector DB에서 지웁니다.")
        for del_id in deleted_ids:
            collection.delete(where={"file_id": del_id})
            deleted_count += 1
            print(f" - Vector DB 삭제 완료 (ID: {del_id})")
        print()

    # 3. 추가 및 수정 로직
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=gemini_api_key
    )

    supported_extensions = {'pdf', 'docx', 'pptx', 'xlsx'}
    new_count = 0
    updated_count = 0
    total_chunks_added = 0

    for file in drive_files:
        file_id = file.get('id')
        file_name = file.get('name', '')
        drive_mod_time = file.get('modifiedTime', '')

        ext = file.get('fileExtension', '').lower()
        if not ext and '.' in file_name:
            ext = file_name.rsplit('.', 1)[-1].lower()

        if ext not in supported_extensions:
            continue

        is_update = False
        if file_id in existing_files:
            if existing_files[file_id] == drive_mod_time:
                continue
            else:
                print(f" - [변경 감지] '{file_name}' 파일 수정됨. 기존 데이터 삭제 후 재학습...")
                collection.delete(where={"file_id": file_id})
                is_update = True
        else:
            print(f" - [신규 처리] '{file_name}' 다운로드 및 텍스트 추출 중...")

        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()

        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as temp_file:
            temp_file.write(fh.getvalue())
            temp_path = temp_file.name

        try:
            base_meta = {
                "file_id": file_id,
                "file_name": file_name,
                "drive_link": file.get('webViewLink', ''),
                "modified_time": drive_mod_time
            }
            docs = extract_documents_from_file(temp_path, ext, base_meta)

            if docs:
                texts = [doc.page_content for doc in docs]
                metadatas = [doc.metadata for doc in docs]
                ids = [f"{file_id}_chunk_{i}" for i in range(len(docs))]

                vectors = embeddings.embed_documents(texts)
                collection.add(
                    ids=ids,
                    embeddings=vectors,
                    documents=texts,
                    metadatas=metadatas
                )
                if is_update:
                    updated_count += 1
                else:
                    new_count += 1
                total_chunks_added += len(docs)
                print(f"   -> 완료: {len(docs)}개 조각(Chunk) 적재")

        except Exception as e:
            print(f"   -> [오류 발생] '{file_name}' 처리 중 에러: {e}")

        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    print(f"\n4. 작업 요약: 신규 {new_count}개 / 수정 {updated_count}개 / 삭제 {deleted_count}개")
    print("=== [Vector DB 완전 동기화 종료] ===")

if __name__ == "__main__":
    ingest_manuals_to_chroma()