import os
import io
import math
import re
import time
import hashlib
import tempfile
from collections import deque
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

# 최소 유효 글자 수 기준 (표지, 간지, 단순 목차 등 노이즈 조각 제거를 통한 경량화)
MIN_CHUNK_CHARS = 50
# 엑셀 시트당 최대 허용 청크 수 (수천 행짜리 산출서로 인한 DB 비대화 및 속도 저하 방지)
MAX_EXCEL_CHUNKS_PER_SHEET = 15
MAX_EMBEDDING_TOKENS_PER_MINUTE = 20000
EMBEDDING_RATE_WINDOW_SECONDS = 60

def classify_doc_category(file_name, ext):
    """파일명과 확장자 규칙을 기반으로 사내 표준 문서 분류(doc_category)를 자동 지정합니다."""
    name_lower = file_name.lower()
    if any(k in name_lower for k in ['양식', '템플릿', 'template', '표준']):
        return "표준양식/템플릿"
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


def clean_and_filter_text(text):
    """불필요한 연속 공백과 특수문자를 정리하고 유효 길이를 검사합니다."""
    if not text:
        return ""
    cleaned = re.sub(r'[ \t]+', ' ', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def count_embedding_tokens(embeddings, texts):
    response = embeddings.client.models.count_tokens(
        model=embeddings.model,
        contents=texts
    )
    if response.total_tokens is None:
        raise RuntimeError("Gemini 토큰 수 응답에 total_tokens 값이 없습니다.")
    return response.total_tokens


def split_text_by_token_budget(text, embeddings, max_tokens):
    if count_embedding_tokens(embeddings, [text]) <= max_tokens:
        return [text]

    splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=0)
    parts = splitter.split_text(text)
    if len(parts) <= 1:
        midpoint = len(text) // 2
        if midpoint == 0:
            raise ValueError("토큰 한도보다 큰 텍스트를 더 이상 분할할 수 없습니다.")
        parts = [text[:midpoint], text[midpoint:]]

    return [
        part
        for candidate in parts
        for part in split_text_by_token_budget(candidate, embeddings, max_tokens)
    ]


class EmbeddingTokenRateLimiter:
    def __init__(self):
        self.usage = deque()
        self.total_tokens = 0

    def wait_for_capacity(self, token_count):
        if token_count > MAX_EMBEDDING_TOKENS_PER_MINUTE:
            raise ValueError(
                f"임베딩 요청 토큰 수({token_count})가 안전 한도 "
                f"({MAX_EMBEDDING_TOKENS_PER_MINUTE})를 초과했습니다."
            )

        while True:
            now = time.monotonic()
            while self.usage and now - self.usage[0][0] >= EMBEDDING_RATE_WINDOW_SECONDS:
                _, expired_tokens = self.usage.popleft()
                self.total_tokens -= expired_tokens

            if self.total_tokens + token_count <= MAX_EMBEDDING_TOKENS_PER_MINUTE:
                return

            wait_sec = max(
                1,
                math.ceil(
                    EMBEDDING_RATE_WINDOW_SECONDS - (now - self.usage[0][0])
                )
            )
            print(f"   -> [임베딩 토큰 속도 제한] {wait_sec}초 대기...")
            time.sleep(wait_sec)

    def record(self, token_count):
        self.usage.append((time.monotonic(), token_count))
        self.total_tokens += token_count


def extract_documents_from_file(temp_path, ext, base_meta, embeddings=None):
    """표준화된 메타데이터 부여, 노이즈 조각 필터링, 엑셀 양식/수식 추출을 수행합니다."""
    docs = []
    seen_hashes = set()  # 문서 내 중복 텍스트(반복 머리말 등) 제거용 해시 세트
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200)

    def add_valid_chunk(content, location_str, chunk_type="일반본문"):
        cleaned = clean_and_filter_text(content)
        if len(cleaned) < MIN_CHUNK_CHARS:
            return  # 50자 미만의 표지/제목 조각은 과감히 버려 경량화

        chunks = (
            split_text_by_token_budget(
                cleaned, embeddings, MAX_EMBEDDING_TOKENS_PER_MINUTE
            )
            if ext == 'xlsx' and embeddings is not None
            else [cleaned]
        )
        for index, chunk in enumerate(chunks, start=1):
            content_hash = hashlib.md5(chunk.encode('utf-8')).hexdigest()
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            meta = base_meta.copy()
            meta["location"] = (
                f"{location_str} (부분 {index}/{len(chunks)})"
                if len(chunks) > 1
                else location_str
            )
            meta["chunk_type"] = chunk_type
            meta["char_length"] = len(chunk)
            docs.append(Document(page_content=chunk, metadata=meta))

    if ext == 'pdf':
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
                # PPT 내 표(Table) 텍스트도 누락 없이 추출
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
        # 1) 계산된 결과값 로드 (data_only=True) 및 2) 작성된 산출 수식 로드 (data_only=False)
        wb_val = openpyxl.load_workbook(temp_path, data_only=True)
        wb_formula = openpyxl.load_workbook(temp_path, data_only=False)

        for sheet in wb_val.sheetnames:
            ws_val = wb_val[sheet]
            ws_form = wb_formula[sheet]
            rows_val = list(ws_val.iter_rows(values_only=True))
            rows_form = list(ws_form.iter_rows(values_only=True))

            if len(rows_val) < 2:
                continue

            # 의미 있는 헤더 행 찾기 (첫 5행 중 텍스트가 가장 많은 행을 헤더로 인식)
            header_idx = 0
            max_non_empty = 0
            for idx in range(min(5, len(rows_val))):
                non_empty = sum(1 for c in rows_val[idx] if c is not None and str(c).strip() != "")
                if non_empty > max_non_empty:
                    max_non_empty = non_empty
                    header_idx = idx

            headers = [str(h).strip().replace('\n', ' ') if h is not None else f"열{i+1}" for i, h in enumerate(rows_val[header_idx])]
            valid_headers = [h for h in headers if not h.startswith("열")]

            # [핵심 고도화] 시트 양식 구조 및 수량산출 공식(Formula) 요약 청크 생성
            formula_samples = []
            summary_rows = []
            for r_i in range(header_idx + 1, len(rows_form)):
                r_f = rows_form[r_i]
                r_v = rows_val[r_i]
                # 수식('='로 시작)이 포함된 셀이 있다면 산출 방법 파악을 위해 샘플 수집 (최대 5개)
                if len(formula_samples) < 5:
                    for h_name, f_cell, v_cell in zip(headers, r_f, r_v):
                        if isinstance(f_cell, str) and f_cell.startswith('='):
                            formula_samples.append(f"[{h_name}] 산출수식: {f_cell} (결과예시: {v_cell})")
                # '합계', '소계', '총계', '비교'가 들어간 핵심 요약 행 별도 수집
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

            # 일반 데이터 행은 25행 단위로 묶되, 시트당 최대 15개 청크까지만 생성하여 DB 비대화 방지
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


def embed_with_rate_limit(embeddings, texts, batch_size=100, token_rate_limiter=None):
    """토큰 사용량을 제한하고 서버가 안내한 대기 시간으로 재시도합니다."""
    all_vectors = []
    total = len(texts)
    max_retries = 3
    token_rate_limiter = token_rate_limiter or EmbeddingTokenRateLimiter()

    def token_bounded_batches(batch):
        token_count = count_embedding_tokens(embeddings, batch)
        if token_count <= MAX_EMBEDDING_TOKENS_PER_MINUTE:
            yield batch, token_count
            return
        if len(batch) == 1:
            raise ValueError(
                f"단일 임베딩 조각이 안전 토큰 한도 "
                f"({MAX_EMBEDDING_TOKENS_PER_MINUTE})를 초과했습니다."
            )

        midpoint = len(batch) // 2
        yield from token_bounded_batches(batch[:midpoint])
        yield from token_bounded_batches(batch[midpoint:])
    
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        for bounded_batch, token_count in token_bounded_batches(batch):
            token_rate_limiter.wait_for_capacity(token_count)
            retry_count = 0

            while True:
                try:
                    vectors = embeddings.embed_documents(bounded_batch)
                    all_vectors.extend(vectors)
                    token_rate_limiter.record(token_count)
                    print(
                        f"     ... 임베딩 진행률: {len(all_vectors)} / "
                        f"{total} 조각 완료"
                    )
                    break
                except Exception as e:
                    err_msg = str(e)
                    if "429" not in err_msg and "RESOURCE_EXHAUSTED" not in err_msg:
                        raise

                    retry_count += 1
                    if retry_count > max_retries:
                        print(
                            "\n[임베딩 중단] API 할당량/속도 제한이 "
                            "재시도 후에도 지속됩니다."
                        )
                        raise

                    retry_match = re.search(
                        r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s",
                        err_msg,
                        re.IGNORECASE
                    )
                    if not retry_match:
                        retry_match = re.search(
                            r"retry in\s+(\d+(?:\.\d+)?)\s*(?:s|seconds)",
                            err_msg,
                            re.IGNORECASE
                        )
                    wait_sec = (
                        max(1, math.ceil(float(retry_match.group(1))))
                        if retry_match
                        else EMBEDDING_RATE_WINDOW_SECONDS
                    )
                    print(
                        f"   -> [API 할당량 대기] {wait_sec}초 대기 후 "
                        f"재시도... ({retry_count}/{max_retries}회)"
                    )
                    time.sleep(wait_sec)
    return all_vectors


def get_all_files_recursively(drive_service, folder_id):
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


def ingest_manuals_to_chroma(force_rebuild=False, target_file_name=None):
    print("=== [Step 1-B: 표준 분류 체계 및 경량화 기반 Vector DB 동기화 시작] ===\n")

    if force_rebuild and target_file_name:
        raise ValueError("특정 파일 처리와 전체 Vector DB 재구축은 함께 실행할 수 없습니다.")

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
            # 표준 메타데이터(doc_category)가 없는 구버전 데이터는 재학습 유도
            if "doc_category" not in meta:
                existing_files[meta["file_id"]] = "NEED_UPGRADE"
            else:
                existing_files[meta["file_id"]] = meta.get("modified_time", "")

    print(f"1. 현재 Vector DB에 등록된 문서 파일 수: {len(existing_files)}개")

    drive_scope = "https://" + "www.googleapis.com/auth/drive.readonly"
    creds = Credentials.from_service_account_file(key_path, scopes=[drive_scope])
    drive_service = build('drive', 'v3', credentials=creds)

    manual_files = get_all_files_recursively(drive_service, manual_folder_id)
    bim_files = get_all_files_recursively(drive_service, bim_folder_id)
    listed_files = manual_files + bim_files
    if target_file_name:
        listed_files = [
            file for file in listed_files
            if file.get('name') == target_file_name
        ]
        if len(listed_files) != 1:
            raise FileNotFoundError(
                f"Google Drive에서 이름이 정확히 '{target_file_name}'인 파일을 "
                f"1개 찾을 것으로 예상했지만 {len(listed_files)}개 찾았습니다."
            )

    unique_drive_files = {f.get('id'): f for f in listed_files}
    drive_files = list(unique_drive_files.values())

    drive_file_ids = set(unique_drive_files.keys())
    deleted_ids = (
        set(existing_files.keys()) - drive_file_ids
        if target_file_name is None
        else set()
    )
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
    token_rate_limiter = EmbeddingTokenRateLimiter()

    supported_extensions = {'pdf', 'docx', 'pptx', 'xlsx'}
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
            if (
                existing_files[file_id] == drive_mod_time
                and target_file_name is None
            ):
                print(f" - [유지] [{doc_category}] '{file_name}' (최신 표준 적용됨)")
                continue
            else:
                print(f" - [표준화 업그레이드/변경] [{doc_category}] '{file_name}' 재학습 중...")
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
            
            docs = extract_documents_from_file(
                temp_path, ext, base_meta, embeddings=embeddings
            )

            if docs:
                texts = [doc.page_content for doc in docs]
                metadatas = [doc.metadata for doc in docs]
                ids = [f"{file_id}_chunk_{i}" for i in range(len(docs))]

                print(f"   -> 경량화 후 유효 조각 수: {len(docs)}개 (임베딩 시작...)")
                vectors = embed_with_rate_limit(
                    embeddings,
                    texts,
                    batch_size=100,
                    token_rate_limiter=token_rate_limiter
                )
                
                previous_ids = collection.get(
                    where={"file_id": file_id}, include=[]
                )["ids"]
                collection.upsert(
                    ids=ids,
                    embeddings=vectors,
                    documents=texts,
                    metadatas=metadatas
                )
                stale_ids = set(previous_ids) - set(ids)
                if stale_ids:
                    collection.delete(ids=list(stale_ids))
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