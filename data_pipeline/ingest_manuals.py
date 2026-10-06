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
    """파일 확장자별로 텍스트를 추출하여 LangChain Document 리스트로 반환합니다."""
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
    print("=== [Step 1-B: 매뉴얼 및 검토보고서 Vector DB 동기화(추가/수정) 시작] ===\n")

    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    manual_folder_id = os.getenv("MANUAL_FOLDER_ID")
    chroma_path = os.getenv("CHROMA_DB_PATH")
    gemini_api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not gemini_api_key or gemini_api_key.startswith("여기에_"):
        raise ValueError(
            "GOOGLE_API_KEY가 설정되지 않았습니다. "
            ".env에 Google AI Studio에서 발급한 Gemini API 키를 설정하세요."
        )

    # 1. 로컬 ChromaDB 연결 및 기존 적재 파일의 {file_id: modified_time} 매핑 사전 생성
    chroma_client = chromadb.PersistentClient(path=chroma_path)
    collection = chroma_client.get_or_create_collection(name="bim_manuals")

    existing_data = collection.get(include=["metadatas"])
    existing_files = {}  # {file_id: modified_time}
    for meta in existing_data.get("metadatas", []):
        if meta and "file_id" in meta:
            existing_files[meta["file_id"]] = meta.get("modified_time", "")

    print(f"1. 현재 Vector DB에 등록된 문서 파일 수: {len(existing_files)}개")

    # 2. 구글 드라이브 매뉴얼 폴더 스캔 (modifiedTime 필드 추가 조회)
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

    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=gemini_api_key
    )

    supported_extensions = {'pdf', 'docx', 'pptx', 'xlsx'}
    new_count = 0
    updated_count = 0
    total_chunks_added = 0

    # 3. 파일별 신규/수정 여부 판별 및 처리
    for file in drive_files:
        file_id = file.get('id')
        file_name = file.get('name', '')
        drive_mod_time = file.get('modifiedTime', '')

        ext = file.get('fileExtension', '').lower()
        if not ext and '.' in file_name:
            ext = file_name.rsplit('.', 1)[-1].lower()

        if ext not in supported_extensions:
            print(f" - [건너뜀] 지원하지 않는 파일 형식: {file_name}")
            continue

        # [핵심 로직] 기존 DB 데이터와 수정 일자 비교
        is_update = False
        if file_id in existing_files:
            if existing_files[file_id] == drive_mod_time:
                # 수정 일자가 같으면 변경 사항이 없으므로 건너뜀
                continue
            else:
                print(
                    f" - [변경 감지] '{file_name}' 파일이 수정되었습니다. "
                    "새 임베딩이 성공한 뒤 기존 데이터를 교체합니다..."
                )
                is_update = True
        else:
            print(f" - [신규 처리] '{file_name}' 다운로드 및 텍스트 추출 중...")

        # 구글 드라이브 파일 다운로드
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
            # 메타데이터에 modified_time을 함께 기록하여 다음 실행 시 비교 기준으로 사용
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

                collection.upsert(
                    ids=ids,
                    embeddings=vectors,
                    documents=texts,
                    metadatas=metadatas
                )
                if is_update:
                    previous_ids = collection.get(
                        where={"file_id": file_id},
                        include=["metadatas"]
                    )["ids"]
                    stale_ids = [chunk_id for chunk_id in previous_ids if chunk_id not in ids]
                    if stale_ids:
                        collection.delete(ids=stale_ids)
                if is_update:
                    updated_count += 1
                else:
                    new_count += 1
                total_chunks_added += len(docs)
                print(f"   -> 완료: {len(docs)}개의 지식 조각(Chunk)을 Vector DB에 반영했습니다.")
            else:
                print(f"   -> 알림: '{file_name}' 내부에 추출할 수 있는 텍스트가 없습니다.")

        except Exception as e:
            print(f"   -> [오류 발생] '{file_name}' 처리 중 에러: {e}")
            error_message = str(e).upper()
            if any(marker in error_message for marker in ("UNAUTHENTICATED", "401", "API_KEY_INVALID")):
                raise RuntimeError(
                    "Gemini 임베딩 인증에 실패했습니다. .env의 GOOGLE_API_KEY에 "
                    "Google AI Studio에서 발급한 Gemini API 키를 입력하세요. "
                    "GOOGLE_APPLICATION_CREDENTIALS의 서비스 계정 키는 "
                    "Google Drive 인증용이며 Gemini API 키를 대신할 수 없습니다."
                ) from e
            if any(marker in error_message for marker in ("RESOURCE_EXHAUSTED", "429", "QUOTA_EXCEEDED")):
                raise RuntimeError(
                    "Gemini API 요청 한도 또는 할당량을 초과했습니다. "
                    "오류 메시지에 표시된 재시도 시간까지 기다린 후 다시 실행하거나, "
                    "Google AI Studio에서 프로젝트의 사용량 및 결제 설정을 확인하세요."
                ) from e

        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    print(f"\n3. 작업 요약: 신규 적재 {new_count}개 / 수정 업데이트 {updated_count}개 (총 {total_chunks_added}개 벡터 청크 반영)")
    print("=== [Vector DB 동기화 종료] ===")

if __name__ == "__main__":
    ingest_manuals_to_chroma()