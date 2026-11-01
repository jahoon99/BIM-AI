import os
import io
import re
import time
import hashlib
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

MIN_CHUNK_CHARS = 50
MAX_EXCEL_CHUNKS_PER_SHEET = 15

def classify_doc_category(file_name, ext):
    """파일명과 확장자 규칙을 기반으로 사내 표준 문서 분류(doc_category)를 자동 지정합니다."""
    name_lower = file_name.lower()
    if any(k in name_lower for k in ['양식', '템플릿', 'template', '표준', '매개변수', 'parameter', '조달청']):
        return "표준양식/세팅기준"
    elif any(k in name_lower for k in ['스펙', 'spec', '시방서', '재료', '마감']):
        return "스펙북/자재기준"
    elif any(k in name_lower for k in ['검토보고', '보고서', 'report']):
        return "검토보고서"
    elif any(k in name_lower for k in ['검토리스트', '체크리스트', 'checklist', '검토목록']):
        return "검토리스트"
    elif any(k in name_lower for k in ['수량', '물량', '산출', '비교표', '내역', 'boq', 'qto']):
        return "수량산출서"
    elif any(k in name_lower for k in ['가이드', '매뉴얼', 'manual', 'guide', '지침']):
        return "매뉴얼/가이드북"
    else:
        return "검토리스트/수량산출서" if ext == 'xlsx' else "기타기술문서"


def detect_setup_topic(text):
    """조각(Chunk)의 텍스트 내용을 분석하여 프로젝트 세팅 및 실무 활용 주제를 자동 태깅합니다."""
    t_lower = text.lower()
    topics = []
    if any(k in t_lower for k in ['매개변수', '파라미터', 'parameter', '속성', '유형 특성', '인스턴스', 'guid']):
        topics.append("매개변수/속성세팅")
    if any(k in t_lower for k in ['뷰', 'view', '필터', 'filter', '가시성', '그래픽', '레벨', 'level', '그리드', 'grid', '시트', '탐색기']):
        topics.append("뷰/그리드/레벨세팅")
    if any(k in t_lower for k in ['재료', '재질', 'material', 'rgb', '색상', '스펙', '마감재', '열관류율']):
        topics.append("재료/스펙세팅")
    if any(k in t_lower for k in ['nwc', 'nwd', 'ifc', 'dwg', '내보내기', 'export', '업로드', '플러그인', '링크']):
        topics.append("파일연동/내보내기세팅")
    if any(k in t_lower for k in ['간섭', 'clash', '오류', '누락', '검토', '간격', '중복']):
        topics.append("모델검토/QA기준")
    if any(k in t_lower for k in ['산출수식', '물량', '수량', '단열', '콘크리트', '합계']):
        topics.append("수량산출/적산기준")
        
    return ", ".join(topics) if topics else "일반내용/개요"


def clean_and_filter_text(text):
    """불필요한 연속 공백과 줄바꿈을 정리합니다."""
    if not text:
        return ""
    cleaned = re.sub(r'[ \t]+', ' ', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def extract_documents_from_file(temp_path, ext, base_meta):
    """문서 포맷별 텍스트 추출, 중복 제거 및 세팅 주제(setup_topic) 메타데이터를 부여합니다."""
    docs = []
    seen_hashes = set()
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200)

    def add_valid_chunk(content, location_str, chunk_type="일반본문"):
        cleaned = clean_and_filter_text(content)
        if len(cleaned) < MIN_CHUNK_CHARS:
            return
        content_hash = hashlib.md5(cleaned.encode('utf-8')).hexdigest()
        if content_hash in seen_hashes:
            return
        seen_hashes.add(content_hash)

        meta = base_meta.copy()
        meta["location"] = location_str
        meta["chunk_type"] = chunk_type
        meta["setup_topic"] = detect_setup_topic(cleaned)
        meta["char_length"] = len(cleaned)
        docs.append(Document(page_content=cleaned, metadata=meta))

    if ext == 'txt':
        with open(temp_path, 'rb') as f:
            raw_bytes = f.read()
        text = ""
        for enc in ['utf-16', 'utf-8-sig', 'cp949', 'utf-8']:
            try:
                text = raw_bytes.decode(enc)
                break
            except Exception:
                continue
        if text:
            if "*PARAM" in text and "*GROUP" in text:
                lines = text.splitlines()
                groups = {}
                params = []
                for line in lines:
                    parts = line.split('\t')
                    if parts[0] == 'GROUP' and len(parts) >= 3:
                        groups[parts[1]] = parts[2]
                    elif parts[0] == 'PARAM' and len(parts) >= 6:
                        p_guid = parts[1]
                        p_name = parts[2]
                        p_type = parts[3]
                        p_group = groups.get(parts[5], parts[5])
                        params.append(f"- 매개변수명: {p_name} | 자료형: {p_type} | 그룹: {p_group} | GUID: {p_guid}")
                if params:
                    param_summary = (
                        f"[Revit 사내 표준 공유 매개변수(Shared Parameters) 원본 정의 - 파일: {base_meta['file_name']}]\n"
                        f"총 등록된 매개변수 수: {len(params)}개\n" + "\n".join(params)
                    )
                    for idx, chunk in enumerate(text_splitter.split_text(param_summary), start=1):
                        add_valid_chunk(chunk, f"공유매개변수 목록 #{idx}", "세팅기준데이터")
            else:
                for idx, chunk in enumerate(text_splitter.split_text(text), start=1):
                    add_valid_chunk(chunk, f"텍스트 섹션 #{idx}", "세팅기준데이터")

    elif ext == 'pdf':
        reader = PdfReader(temp_path)
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                for chunk in text_splitter.split_text(text):
                    add_valid_chunk(chunk, f"{i + 1}페이지", "본문내용")

    elif ext == 'docx':
        doc = docx.Document(temp_path)
        full_text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        if full_text:
            for idx, chunk in enumerate(text_splitter.split_text(full_text), start=1):
                add_valid_chunk(chunk, f"본문 섹션 #{idx}", "본문내용")

    elif ext == 'pptx':
        prs = Presentation(temp_path)
        for i, slide in enumerate(prs.slides):
            slide_texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        if paragraph.text.strip():
                            slide_texts.append(paragraph.text.strip())
                if shape.has_table:
                    for row in shape.table.rows:
                        row_txt = [cell.text.strip().replace('\n', ' ') for cell in row.cells if cell.text.strip()]
                        if row_txt:
                            slide_texts.append(" | ".join(row_txt))
            full_slide_text = "\n".join(slide_texts)
            if full_slide_text:
                for chunk in text_splitter.split_text(full_slide_text):
                    add_valid_chunk(chunk, f"슬라이드 {i + 1}장", "보고서슬라이드")

    elif ext == 'xlsx':
        wb_val = openpyxl.load_workbook(temp_path, data_only=True)
        wb_formula = openpyxl.load_workbook(temp_path, data_only=False)

        for sheet in wb_val.sheetnames:
            ws_val = wb_val[sheet]
            ws_form = wb_formula[sheet]
            rows_val = list(ws_val.iter_rows(values_only=True))
            rows_form = list(ws_form.iter_rows(values_only=True))

            if len(rows_val) < 2:
                continue

            header_idx = 0
            max_non_empty = 0
            for idx in range(min(5, len(rows_val))):
                non_empty = sum(1 for c in rows_val[idx] if c is not None and str(c).strip() != "")
                if non_empty > max_non_empty:
                    max_non_empty = non_empty
                    header_idx = idx

            headers = [str(h).strip().replace('\n', ' ') if h is not None else f"열{i+1}" for i, h in enumerate(rows_val[header_idx])]
            valid_headers = [h for h in headers if not h.startswith("열")]

            formula_samples = []
            summary_rows = []
            for r_i in range(header_idx + 1, len(rows_form)):
                r_f = rows_form[r_i]
                r_v = rows_val[r_i]
                if len(formula_samples) < 5:
                    for h_name, f_cell, v_cell in zip(headers, r_f, r_v):
                        if isinstance(f_cell, str) and f_cell.startswith('='):
                            formula_samples.append(f"[{h_name}] 산출수식: {f_cell} (결과예시: {v_cell})")
                row_str_joined = " ".join([str(c) for c in r_v if c is not None])
                if any(k in row_str_joined for k in ['합계', '소계', '총계', '계', '비교', '차이']) and len(summary_rows) < 10:
                    items = [f"{h}: {str(v).strip()}" for h, v in zip(headers, r_v) if v is not None and str(v).strip() != ""]
                    summary_rows.append(f"({r_i+1}행 요약) " + " | ".join(items))

            structure_summary = (
                f"[엑셀 양식 및 작성 방법 요약 - 파일: {base_meta['file_name']} / 시트: {sheet}]\n"
                f"- 전체 데이터 행 수: 총 {len(rows_val) - header_idx - 1}행\n"
                f"- 표 컬럼(양식 항목) 구성: {', '.join(valid_headers)}\n"
                f"- 적용된 주요 산출 수식(Formula) 예시:\n  " + ("\n  ".join(formula_samples) if formula_samples else "별도 수식 없음(단순 값 기입 방식)") + "\n"
                f"- 시트 내 주요 합계/비교 요약 행:\n  " + ("\n  ".join(summary_rows) if summary_rows else "별도 합계 행 없음")
            )
            add_valid_chunk(structure_summary, f"{sheet} 시트 (양식/산출식 구조)", "양식및산출기준")

            batch_rows_text = []
            start_row_num = header_idx + 2
            sheet_chunk_count = 0

            for r_idx in range(header_idx + 1, len(rows_val)):
                row = rows_val[r_idx]
                row_items = [f"{h}: {str(val).strip()}" for h, val in zip(headers, row) if val is not None and str(val).strip() != ""]
                if row_items:
                    batch_rows_text.append(f"({r_idx+1}행) " + " | ".join(row_items))

                if len(batch_rows_text) >= 25 or r_idx == len(rows_val) - 1:
                    if batch_rows_text and sheet_chunk_count < MAX_EXCEL_CHUNKS_PER_SHEET:
                        combined_text = f"[{sheet} 시트 {start_row_num}~{r_idx+1}행 데이터]\n" + "\n".join(batch_rows_text)
                        add_valid_chunk(combined_text, f"{sheet} 시트 {start_row_num}~{r_idx+1}행", "세부데이터")
                        sheet_chunk_count += 1
                    batch_rows_text = []
                    start_row_num = r_idx + 2

    return docs


def embed_with_rate_limit(embeddings, texts, batch_size=100):
    """무한 루프 방지 및 API 효율 극대화를 위한 임베딩 함수입니다."""
    all_vectors = []
    total = len(texts)
    max_retries = 3
    
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        retry_count = 0
        
        while True:
            try:
                vectors = embeddings.embed_documents(batch)
                all_vectors.extend(vectors)
                print(f"     ... 임베딩 진행률: {min(i + batch_size, total)} / {total} 조각 완료")
                if i + batch_size < total:
                    time.sleep(2)
                break
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    retry_count += 1
                    if retry_count > max_retries:
                        print("\n[치명적 오류] 일일 무료 할당량이 고갈되었습니다. 작업을 강제 종료합니다.")
                        raise e
                    wait_sec = 45
                    print(f"   -> [API 할당량 대기] {wait_sec}초 대기 후 재시도... ({retry_count}/{max_retries}회)")
                    time.sleep(wait_sec)
                else:
                    raise e
    return all_vectors


def get_all_files_recursively(drive_service, folder_id):
    """폴더 및 하위 폴더의 모든 파일 목록을 재귀적으로 스캔합니다."""
    all_files = []
    folders_to_scan = [folder_id]
    while folders_to_scan:
        current_folder = folders_to_scan.pop(0)
        query = f"'{current_folder}' in parents and trashed = false"
        results = drive_service.files().list(
            q=query, pageSize=1000,
            fields="files(id, name, mimeType, fileExtension, webViewLink, modifiedTime)"
        ).execute()
        for item in results.get('files', []):
            if item.get('mimeType') == 'application/vnd.google-apps.folder':
                folders_to_scan.append(item.get('id'))
            else:
                all_files.append(item)
    return all_files


def ingest_manuals_to_chroma(force_rebuild=False):
    print("=== [Step 1-B: 표준 분류 체계 및 세팅 기준 Vector DB 동기화 시작] ===\n")

    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    manual_folder_id = os.getenv("MANUAL_FOLDER_ID")
    bim_folder_id = os.getenv("BIM_FILE_FOLDER_ID")
    chroma_path = os.getenv("CHROMA_DB_PATH")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")

    chroma_client = chromadb.PersistentClient(path=chroma_path)
    if force_rebuild:
        print("[초기화] 기존 Vector DB 컬렉션을 삭제하고 새로운 표준 규격으로 재구축합니다...")
        try:
            chroma_client.delete_collection(name="bim_manuals")
        except Exception:
            pass

    collection = chroma_client.get_or_create_collection(name="bim_manuals")

    existing_data = collection.get(include=["metadatas"])
    existing_files = {}
    for meta in existing_data.get("metadatas", []):
        if meta and "file_id" in meta:
            if "doc_category" not in meta or "setup_topic" not in meta:
                existing_files[meta["file_id"]] = "NEED_UPGRADE"
            else:
                existing_files[meta["file_id"]] = meta.get("modified_time", "")

    print(f"1. 현재 Vector DB에 등록된 문서 파일 수: {len(existing_files)}개")

    drive_scope = "https://" + "www.googleapis.com/auth/drive.readonly"
    creds = Credentials.from_service_account_file(key_path, scopes=[drive_scope])
    drive_service = build('drive', 'v3', credentials=creds)

    manual_files = get_all_files_recursively(drive_service, manual_folder_id)
    bim_files = get_all_files_recursively(drive_service, bim_folder_id)
    unique_drive_files = {f.get('id'): f for f in manual_files + bim_files}
    drive_files = list(unique_drive_files.values())

    drive_file_ids = set(unique_drive_files.keys())
    deleted_ids = set(existing_files.keys()) - drive_file_ids
    deleted_count = 0

    if deleted_ids:
        for del_id in deleted_ids:
            collection.delete(where={"file_id": del_id})
            deleted_count += 1
        print(f"2. 삭제된 파일 정리: {deleted_count}개 문서 제거 완료\n")

    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=gemini_api_key
    )

    supported_extensions = {'pdf', 'docx', 'pptx', 'xlsx', 'txt'}
    new_count = 0
    updated_count = 0

    for file in drive_files:
        file_id = file.get('id')
        file_name = file.get('name', '')
        mime_type = file.get('mimeType', '')
        drive_mod_time = file.get('modifiedTime', '')

        ext = file.get('fileExtension', '').lower()
        if not ext and '.' in file_name:
            ext = file_name.rsplit('.', 1)[-1].lower()

        export_mime = None
        if mime_type == 'application/vnd.google-apps.spreadsheet':
            ext = 'xlsx'
            export_mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        elif mime_type == 'application/vnd.google-apps.presentation':
            ext = 'pptx'
            export_mime = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        elif mime_type == 'application/vnd.google-apps.document':
            ext = 'docx'
            export_mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

        if ext not in supported_extensions:
            continue

        doc_category = classify_doc_category(file_name, ext)

        is_update = False
        if file_id in existing_files:
            if existing_files[file_id] == drive_mod_time:
                print(f" - [유지] [{doc_category}] '{file_name}' (최신 표준 적용됨)")
                continue
            else:
                print(f" - [표준화 업그레이드/변경] [{doc_category}] '{file_name}' 재학습 중...")
                collection.delete(where={"file_id": file_id})
                is_update = True
        else:
            print(f" - [신규 처리] [{doc_category}] '{file_name}' 추출 중...")

        try:
            if export_mime:
                request = drive_service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                request = drive_service.files().get_media(fileId=file_id)
                
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as temp_file:
                temp_file.write(fh.getvalue())
                temp_path = temp_file.name

            base_meta = {
                "file_id": file_id,
                "file_name": file_name,
                "doc_category": doc_category,
                "file_ext": ext,
                "drive_link": file.get('webViewLink', ''),
                "modified_time": drive_mod_time
            }
            
            docs = extract_documents_from_file(temp_path, ext, base_meta)

            if docs:
                texts = [doc.page_content for doc in docs]
                metadatas = [doc.metadata for doc in docs]
                ids = [f"{file_id}_chunk_{i}" for i in range(len(docs))]

                print(f"   -> 경량화 후 유효 조각 수: {len(docs)}개 (임베딩 시작...)")
                vectors = embed_with_rate_limit(embeddings, texts, batch_size=100)
                
                collection.add(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)
                if is_update:
                    updated_count += 1
                else:
                    new_count += 1
                print(f"   -> 완료: '{file_name}' 적재 성공\n")
            else:
                print(f"   -> [경고] '{file_name}'에서 유효한 텍스트가 추출되지 않았습니다.\n")

        except Exception as e:
            print(f"   -> [오류 발생] '{file_name}' 처리 실패: {e}\n")

        finally:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)

    print(f"\n3. 작업 요약: 신규 {new_count}개 / 표준화갱신 {updated_count}개 / 삭제 {deleted_count}개")
    print("=== [Vector DB 동기화 종료] ===")

if __name__ == "__main__":
    ingest_manuals_to_chroma()