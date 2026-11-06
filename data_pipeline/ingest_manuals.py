import os
import io
import re
import time
import json
import hashlib
import tempfile
import datetime
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import chromadb
import gspread

from pypdf import PdfReader
import docx
from pptx import Presentation
import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill, Alignment

MIN_CHUNK_CHARS = 50
OPTIMAL_CHUNK_SIZE = 1500
OPTIMAL_CHUNK_OVERLAP = 200
EXCEL_ROWS_PER_CHUNK = 25
EMBED_BATCH_SIZE = 15
SCHEMA_VERSION = "v3.6"

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
DB_DOCS_DIR = os.path.join(BASE_DIR, "db_docs")
MD_SPECS_DIR = os.path.join(DB_DOCS_DIR, "markdown_specs")
EXCEL_REPORT_PATH = os.path.join(DB_DOCS_DIR, "VectorDB_통합관리대장.xlsx")
LOCAL_CACHE_PATH = os.path.join(DB_DOCS_DIR, "local_text_cache.json")


class DailyQuotaExhaustedError(Exception):
    """구글 API 일일 무료 할당량(1,000건/일) 소진 시 로컬 텍스트 캐시 모드로 전환하기 위한 예외"""
    pass


_RATE_WINDOW = []


def match_category_keywords(target_str):
    t = target_str.lower()
    if any(k in t for k in ['뷰어', '가이드북', '매뉴얼', 'manual', 'guide', '다룩스', 'dalux', '트림블', 'trimble', 'acc', 'navisworks']):
        return "소프트웨어 가이드북"
    elif any(k in t for k in ['검토리스트', '체크리스트', 'checklist', '검토목록']):
        return "BIM 검토리스트"
    elif any(k in t for k in ['검토보고', '보고서', 'report']):
        return "BIM 검토보고서"
    elif any(k in t for k in ['견적', '내역서', '일위대가', '단가', '원가', 'cost', 'estimate']):
        return "BIM 견적서"
    elif any(k in t for k in ['수량', '물량', '산출', '비교표', 'boq', 'qto']):
        return "수량산출서"
    elif any(k in t for k in ['제안', '기술제안', '수행계획', 'bep', '입찰', 'proposal', '공정시뮬레이션', '공정표', 'schedule', '실적']):
        return "BIM 기술제안서/BEP"
    elif any(k in t for k in ['스펙', 'spec', '시방서', '재료', '자재', '마감']):
        return "스펙북/자재기준"
    elif any(k in t for k in ['bf표준', '사내표준', '세팅', '표준양식', '템플릿', 'template', '매개변수', 'parameter', '사전 검토', '코디네이션']):
        return "사내 표준/세팅기준"
    elif any(k in t for k in ['대외', '발주처', '조달청', '국토부', 'lh', '도로공사', '지침', '납품기준', '실무요령', '표준공사코드', '공간분류코드']):
        return "대외/발주처 BIM 지침"
    return None


def classify_doc_category(file_name, ext, folder_path=""):
    folder_clean = folder_path.strip()
    if folder_clean and folder_clean not in {"루트폴더", "매뉴얼루트", "BIM파일루트"}:
        folder_matched = match_category_keywords(folder_clean)
        if folder_matched:
            return folder_matched

    file_matched = match_category_keywords(file_name)
    if file_matched:
        return file_matched

    if folder_clean and folder_clean not in {"루트폴더", "매뉴얼루트", "BIM파일루트"}:
        top_sub = folder_clean.split("/")[0].strip()
        cleaned_sub = re.sub(r'^\d+[_-\s]*', '', top_sub).strip()
        if cleaned_sub:
            return cleaned_sub

    return "수량산출서" if ext == 'xlsx' else "기타기술문서"


def get_domain_group(doc_category):
    if doc_category in {"대외/발주처 BIM 지침", "사내 표준/세팅기준", "스펙북/자재기준", "소프트웨어 가이드북"}:
        return "1_표준및기준"
    elif doc_category in {"BIM 검토보고서", "BIM 검토리스트"}:
        return "2_품질및검수"
    elif doc_category in {"수량산출서", "BIM 견적서"}:
        return "3_원가및적산"
    elif doc_category in {"BIM 기술제안서/BEP"}:
        return "4_입찰및수행"
    return "5_기타확장분류"


def detect_setup_topic(text):
    t_lower = text.lower()
    topics = []
    if any(k in t_lower for k in ['매개변수', '파라미터', 'parameter', '속성', '유형 특성', '인스턴스', 'guid', '분류코드']):
        topics.append("매개변수/속성세팅")
    if any(k in t_lower for k in ['뷰', 'view', '필터', 'filter', '가시성', '그래픽', '레벨', 'level', '그리드', 'grid', '시트', '탐색기']):
        topics.append("뷰/그리드/레벨세팅")
    if any(k in t_lower for k in ['재료', '재질', 'material', 'rgb', '색상', '스펙', '마감재', '열관류율']):
        topics.append("재료/스펙세팅")
    if any(k in t_lower for k in ['nwc', 'nwd', 'ifc', 'dwg', '내보내기', 'export', '업로드', '플러그인', '링크']):
        topics.append("파일연동/내보내기세팅")
    if any(k in t_lower for k in ['간섭', 'clash', '오류', '누락', '검토', '간격', '중복']):
        topics.append("모델검토/QA기준")
    if any(k in t_lower for k in ['산출수식', '물량', '수량', '단열', '콘크리트', '합계', '단가', '금액', '재료비', '노무비', '견적']):
        topics.append("수량산출/견적기준")
    if any(k in t_lower for k in ['수행계획', 'bep', '조직도', '일정', '목표', '제안', '전략', '프로세스', '공정']):
        topics.append("기술제안/BEP수행전략")
    return ", ".join(topics) if topics else "일반내용/개요"


def sanitize_for_excel(val):
    if val is None:
        return ""
    if not isinstance(val, str):
        return val
    cleaned = val.replace('\x0b', '\n').replace('\x0c', '\n')
    cleaned = ILLEGAL_CHARACTERS_RE.sub(' ', cleaned)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', ' ', cleaned)
    if len(cleaned) > 32000:
        cleaned = cleaned[:32000] + "...(절삭됨)"
    if cleaned.startswith(('=', '+', '-', '@')):
        cleaned = "'" + cleaned
    return cleaned


def clean_and_filter_text(text):
    if not text:
        return ""
    cleaned = text.replace('\x0b', '\n').replace('\x0c', '\n')
    cleaned = ILLEGAL_CHARACTERS_RE.sub(' ', cleaned)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', ' ', cleaned)
    cleaned = re.sub(r'[\.·…]{5,}', ' ', cleaned)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def extract_shapes_text_recursively(shapes):
    extracted = []
    for shape in shapes:
        if hasattr(shape, "shapes"):
            extracted.extend(extract_shapes_text_recursively(shape.shapes))
        if getattr(shape, "has_text_frame", False):
            for paragraph in shape.text_frame.paragraphs:
                cleaned_p = clean_and_filter_text(paragraph.text)
                if cleaned_p:
                    extracted.append(cleaned_p)
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                row_txt = [clean_and_filter_text(cell.text).replace('\n', ' ') for cell in row.cells if clean_and_filter_text(cell.text)]
                if row_txt:
                    extracted.append(" | ".join(row_txt))
    return extracted


def extract_documents_from_file(temp_path, ext, base_meta):
    docs = []
    seen_hashes = set()
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=OPTIMAL_CHUNK_SIZE,
        chunk_overlap=OPTIMAL_CHUNK_OVERLAP
    )

    def add_valid_chunk(content, location_str, chunk_type="일반본문"):
        cleaned = clean_and_filter_text(content)
        if len(cleaned) < MIN_CHUNK_CHARS:
            return
        content_hash = hashlib.md5(cleaned.encode('utf-8')).hexdigest()
        if content_hash in seen_hashes:
            return
        seen_hashes.add(content_hash)

        topic_str = detect_setup_topic(cleaned)
        contextual_text = "[문서명: {0} | 분류: {1} | 폴더경로: {2} | 위치: {3} | 주제: {4}]\n{5}".format(
            base_meta["file_name"], base_meta["doc_category"], base_meta.get("folder_path", "루트"),
            location_str, topic_str, cleaned
        )
        meta = base_meta.copy()
        meta["location"] = location_str
        meta["chunk_type"] = chunk_type
        meta["setup_topic"] = topic_str
        meta["char_length"] = len(contextual_text)
        meta["schema_version"] = SCHEMA_VERSION
        docs.append(Document(page_content=contextual_text, metadata=meta))

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
                groups, params = {}, []
                for line in lines:
                    parts = line.split('\t')
                    if parts[0] == 'GROUP' and len(parts) >= 3:
                        groups[parts[1]] = parts[2]
                    elif parts[0] == 'PARAM' and len(parts) >= 6:
                        params.append("- 매개변수명: {0} | 자료형: {1} | 그룹: {2} | GUID: {3}".format(
                            parts[2], parts[3], groups.get(parts[5], parts[5]), parts[1]
                        ))
                if params:
                    param_summary = "[Revit 사내 표준 공유 매개변수 원본 정의 - 파일: {0}]\n총 {1}개\n".format(
                        base_meta['file_name'], len(params)
                    ) + "\n".join(params)
                    for idx, chunk in enumerate(text_splitter.split_text(param_summary), start=1):
                        add_valid_chunk(chunk, "공유매개변수 목록 #{0}".format(idx), "세팅기준데이터")
            else:
                for idx, chunk in enumerate(text_splitter.split_text(text), start=1):
                    add_valid_chunk(chunk, "텍스트 섹션 #{0}".format(idx), "세팅기준데이터")

    elif ext == 'pdf':
        reader = PdfReader(temp_path)
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                for chunk in text_splitter.split_text(text):
                    add_valid_chunk(chunk, "{0}페이지".format(i + 1), "본문내용")

    elif ext == 'docx':
        doc = docx.Document(temp_path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for t_idx, table in enumerate(doc.tables, start=1):
            t_rows = []
            for row in table.rows:
                r_txt = [clean_and_filter_text(cell.text).replace('\n', ' ') for cell in row.cells if cell.text.strip()]
                if r_txt:
                    t_rows.append(" | ".join(r_txt))
            if t_rows:
                paragraphs.append("[문서 내 표 #{0}]\n".format(t_idx) + "\n".join(t_rows))
        full_text = "\n".join(paragraphs)
        if full_text:
            for idx, chunk in enumerate(text_splitter.split_text(full_text), start=1):
                add_valid_chunk(chunk, "본문 섹션 #{0}".format(idx), "본문내용")

    elif ext == 'pptx':
        prs = Presentation(temp_path)
        for i, slide in enumerate(prs.slides):
            slide_texts = extract_shapes_text_recursively(slide.shapes)
            full_slide_text = "\n".join(slide_texts)
            if full_slide_text:
                for chunk in text_splitter.split_text(full_slide_text):
                    add_valid_chunk(chunk, "슬라이드 {0}장".format(i + 1), "보고서및제안서슬라이드")

    elif ext == 'xlsx':
        wb_val = openpyxl.load_workbook(temp_path, data_only=True)
        wb_formula = openpyxl.load_workbook(temp_path, data_only=False)
        for sheet in wb_val.sheetnames:
            rows_val = list(wb_val[sheet].iter_rows(values_only=True))
            rows_form = list(wb_formula[sheet].iter_rows(values_only=True))
            if len(rows_val) < 2:
                continue
            header_idx, max_non_empty = 0, 0
            for idx in range(min(10, len(rows_val))):
                non_empty = sum(1 for c in rows_val[idx] if c is not None and str(c).strip() != "")
                if non_empty > max_non_empty:
                    max_non_empty, header_idx = non_empty, idx
            headers = [clean_and_filter_text(str(h)).replace('\n', ' ') if h is not None else "열{0}".format(i + 1) for i, h in enumerate(rows_val[header_idx])]
            valid_headers = [h for h in headers if not h.startswith("열")]
            if any(k in " ".join(valid_headers) for k in ['단가', '금액', '재료비', '노무비', '경비', '일위대가']) and base_meta.get("doc_category") == "수량산출서":
                base_meta["doc_category"] = "BIM 견적서"
                base_meta["domain_group"] = "3_원가및적산"
            formula_samples, summary_rows = [], []
            for r_i in range(header_idx + 1, len(rows_form)):
                r_f, r_v = rows_form[r_i], rows_val[r_i]
                if len(formula_samples) < 8:
                    for h_name, f_cell, v_cell in zip(headers, r_f, r_v):
                        if isinstance(f_cell, str) and f_cell.startswith('='):
                            s_str = "[{0}] 산출수식: {1} (예시: {2})".format(h_name, f_cell, v_cell)
                            if s_str not in formula_samples:
                                formula_samples.append(s_str)
                if any(k in " ".join([str(c) for c in r_v if c is not None]) for k in ['합계', '소계', '총계', '계', '비교', '차이', '총괄', '순공사비']) and len(summary_rows) < 12:
                    items = ["{0}: {1}".format(h, str(v).strip()) for h, v in zip(headers, r_v) if v is not None and str(v).strip() != ""]
                    summary_rows.append("({0}행 요약) ".format(r_i + 1) + " | ".join(items))
            structure_summary = (
                "[엑셀 양식 및 산출/견적 요약 - 파일: {0} / 시트: {1}]\n- 분류: {2} (총 {3}행)\n- 컬럼: {4}\n- 수식:\n  {5}\n- 합계행:\n  {6}".format(
                    base_meta['file_name'], sheet, base_meta['doc_category'], len(rows_val) - header_idx - 1,
                    ', '.join(valid_headers), "\n  ".join(formula_samples) if formula_samples else "수식 없음",
                    "\n  ".join(summary_rows) if summary_rows else "합계 행 없음"
                )
            )
            add_valid_chunk(structure_summary, "{0} 시트 (양식/산출식 구조)".format(sheet), "양식및산출기준")

            batch_rows_text, start_row_num = [], header_idx + 2
            for r_idx in range(header_idx + 1, len(rows_val)):
                row_items = ["{0}: {1}".format(h, str(val).strip()) for h, val in zip(headers, rows_val[r_idx]) if val is not None and str(val).strip() != ""]
                if row_items:
                    batch_rows_text.append("({0}행) ".format(r_idx + 1) + " | ".join(row_items))
                if len(batch_rows_text) >= EXCEL_ROWS_PER_CHUNK or r_idx == len(rows_val) - 1:
                    if batch_rows_text:
                        add_valid_chunk(
                            "[{0} 시트 {1}~{2}행 데이터]\n".format(sheet, start_row_num, r_idx + 1) + "\n".join(batch_rows_text),
                            "{0} 시트 {1}~{2}행".format(sheet, start_row_num, r_idx + 1),
                            "세부데이터"
                        )
                    batch_rows_text, start_row_num = [], r_idx + 2

    total_cnt = len(docs)
    for idx, d in enumerate(docs):
        d.metadata["total_chunks"] = total_cnt
        d.metadata["chunk_index"] = idx

    return docs


def throttle_before_request(batch_chars, batch_count):
    global _RATE_WINDOW
    now = time.time()
    _RATE_WINDOW = [(ts, c, cnt) for ts, c, cnt in _RATE_WINDOW if now - ts < 62.0]

    used_chars = sum(c for _, c, _ in _RATE_WINDOW)
    used_reqs = sum(cnt for _, _, cnt in _RATE_WINDOW)

    if (used_chars + batch_chars > 22000 or used_reqs + batch_count > 75) and _RATE_WINDOW:
        oldest_ts = _RATE_WINDOW[0][0]
        sleep_sec = max(3.0, 62.5 - (now - oldest_ts))
        print("   -> [사전 속도 조절] 분당 토큰 한도(30,000 TPM) 보호를 위해 {0:.0f}초 대기 후 안전하게 전송합니다...".format(sleep_sec))
        time.sleep(sleep_sec)
        now = time.time()
        _RATE_WINDOW = [(ts, c, cnt) for ts, c, cnt in _RATE_WINDOW if now - ts < 62.0]

    _RATE_WINDOW.append((time.time(), batch_chars, batch_count))


def save_chunks_to_local_cache(file_id, docs, existing_chunk_ids):
    """일일 임베딩 할당량이 소진된 경우에도 드라이브의 모든 문서를 즉시 선택·검색할 수 있도록 로컬 텍스트 캐시에 저장합니다."""
    os.makedirs(DB_DOCS_DIR, exist_ok=True)
    cache_data = []
    if os.path.exists(LOCAL_CACHE_PATH):
        try:
            with open(LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except Exception:
            cache_data = []

    cache_map = {item["id"]: item for item in cache_data if isinstance(item, dict) and "id" in item}
    added = 0
    for i, doc in enumerate(docs):
        cid = "{0}_chunk_{1}".format(file_id, i)
        if cid not in existing_chunk_ids:
            cache_map[cid] = {
                "id": cid,
                "document": doc.page_content,
                "metadata": doc.metadata
            }
            added += 1

    with open(LOCAL_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(list(cache_map.values()), f, ensure_ascii=False, indent=2)
    return added


def remove_embedded_from_local_cache(embedded_ids):
    """임베딩이 완료된 조각은 로컬 대기 캐시에서 제거합니다."""
    if not os.path.exists(LOCAL_CACHE_PATH) or not embedded_ids:
        return
    try:
        with open(LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
        remained = [item for item in cache_data if item.get("id") not in embedded_ids]
        with open(LOCAL_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(remained, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def embed_and_save_incrementally(collection, embeddings, file_id, docs, existing_chunk_ids, quota_exhausted=False):
    total = len(docs)
    if quota_exhausted:
        added = save_chunks_to_local_cache(file_id, docs, existing_chunk_ids)
        print("   -> [무할당량 로컬 텍스트 인덱싱] 총 {0}개 조각을 로컬 검색 캐시에 즉시 등록했습니다 (슬랙/웹에서 바로 선택·검색 가능).".format(added))
        return False

    pending_items = []
    for i, doc in enumerate(docs):
        cid = "{0}_chunk_{1}".format(file_id, i)
        if cid not in existing_chunk_ids:
            pending_items.append((cid, doc))

    if not pending_items:
        print("   -> 모든 조각({0}개)이 이미 DB에 존재합니다.".format(total))
        return False

    already_done = total - len(pending_items)
    if already_done > 0:
        print("   -> [조각 이어받기] 전체 {0}개 중 기존 완료된 {1}개 조각 보존 -> 남은 {2}개 조각부터 이어서 임베딩합니다.".format(
            total, already_done, len(pending_items)
        ))

    max_retries = 5
    for idx in range(0, len(pending_items), EMBED_BATCH_SIZE):
        batch_slice = pending_items[idx : idx + EMBED_BATCH_SIZE]
        batch_ids = [item[0] for item in batch_slice]
        batch_texts = [item[1].page_content for item in batch_slice]
        batch_metas = [item[1].metadata for item in batch_slice]

        batch_chars = sum(len(t) for t in batch_texts)
        throttle_before_request(batch_chars, len(batch_texts))

        retry_count = 0
        while True:
            try:
                vectors = embeddings.embed_documents(batch_texts)
                collection.upsert(
                    ids=batch_ids,
                    embeddings=vectors,
                    documents=batch_texts,
                    metadatas=batch_metas
                )
                remove_embedded_from_local_cache(set(batch_ids))
                done_so_far = already_done + min(idx + EMBED_BATCH_SIZE, len(pending_items))
                print("     ... 실시간 DB 저장 진행률: {0} / {1} 조각 완료".format(done_so_far, total))
                if idx + EMBED_BATCH_SIZE < len(pending_items):
                    time.sleep(1.5)
                break
            except Exception as e:
                err_msg = str(e)
                if "PerDay" in err_msg or "per day" in err_msg.lower() or "limit: 1000" in err_msg:
                    saved_cnt = already_done + idx
                    already_embedded_now = existing_chunk_ids | {item[0] for item in pending_items[:idx]}
                    cached_cnt = save_chunks_to_local_cache(file_id, docs, already_embedded_now)
                    print("\n   -> [일일 무료 할당량(1,000건/일) 도달] {0}/{1} 조각은 Vector DB에, 남은 {2}개 조각은 로컬 검색 캐시에 저장했습니다.".format(
                        saved_cnt, total, cached_cnt
                    ))
                    return True

                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    retry_count += 1
                    if retry_count > max_retries:
                        raise e
                    delay_match = re.search(r'retry in (\d+(?:\.\d+)?)s', err_msg, re.IGNORECASE)
                    if not delay_match:
                        delay_match = re.search(r"'retryDelay':\s*'(\d+)s'", err_msg)
                    wait_sec = int(float(delay_match.group(1))) + 6 if delay_match else 62
                    print("   -> [분당 토큰 한도 대기] 데이터 손실 없이 진행하기 위해 {0}초 대기 후 이어서 전송합니다... ({1}/{2}회)".format(
                        wait_sec, retry_count, max_retries
                    ))
                    time.sleep(wait_sec)
                    _RATE_WINDOW.clear()
                else:
                    raise e

    return False


def get_all_files_recursively(drive_service, folder_id, root_label="루트폴더"):
    if not folder_id:
        return []
    all_files, folders_to_scan, visited_folders = [], [(folder_id, "")], set()
    while folders_to_scan:
        current_folder, current_path = folders_to_scan.pop(0)
        if current_folder in visited_folders:
            continue
        visited_folders.add(current_folder)
        page_token = None
        while True:
            results = drive_service.files().list(
                q="'{0}' in parents and trashed = false".format(current_folder),
                pageSize=1000, pageToken=page_token,
                fields="nextPageToken, files(id, name, mimeType, fileExtension, webViewLink, modifiedTime)"
            ).execute()
            for item in results.get('files', []):
                if item.get('mimeType') == 'application/vnd.google-apps.folder':
                    sub_name = item.get('name', '').strip()
                    folders_to_scan.append((item.get('id'), "{0}/{1}".format(current_path, sub_name) if current_path else sub_name))
                else:
                    item['folder_path'] = current_path if current_path else root_label
                    all_files.append(item)
            page_token = results.get('nextPageToken')
            if not page_token:
                break
    return all_files


def filter_duplicate_pdf_files(drive_files):
    editable_basenames = set()
    for f in drive_files:
        fname, mime = f.get('name', ''), f.get('mimeType', '')
        ext = f.get('fileExtension', '').lower() or (fname.rsplit('.', 1)[-1].lower() if '.' in fname else '')
        if mime in {'application/vnd.google-apps.spreadsheet', 'application/vnd.google-apps.presentation', 'application/vnd.google-apps.document'} or ext in {'pptx', 'docx', 'xlsx'}:
            editable_basenames.add(fname.rsplit('.', 1)[0].strip().lower())
    filtered, skipped_pdfs = [], []
    for f in drive_files:
        fname = f.get('name', '')
        ext = f.get('fileExtension', '').lower() or (fname.rsplit('.', 1)[-1].lower() if '.' in fname else '')
        if ext == 'pdf' and fname.rsplit('.', 1)[0].strip().lower() in editable_basenames:
            skipped_pdfs.append(fname)
            continue
        filtered.append(f)
    return filtered, skipped_pdfs


def export_vector_db_documentation(collection, key_path, sheet_id):
    """ChromaDB 적재 데이터와 로컬 텍스트 캐시 데이터를 통합하여 3중 문서화(Sheets + Excel + 1:1 MD)를 생성합니다."""
    print("\n=== [Step 1-C: Vector DB 자동 교정 및 3중 문서화(Sheets + Excel + 1:1 MD) 시작] ===")
    os.makedirs(DB_DOCS_DIR, exist_ok=True)
    os.makedirs(MD_SPECS_DIR, exist_ok=True)

    data = collection.get(include=["documents", "metadatas"])
    ids = list(data.get("ids", []))
    docs = list(data.get("documents", []))
    metas = list(data.get("metadatas", []))

    fix_ids, fix_metas = [], []
    for i, (cid, content, meta) in enumerate(zip(ids, docs, metas)):
        if not meta:
            continue
        fname = meta.get("file_name", "미상")
        ext = meta.get("file_ext", "")
        fpath = meta.get("folder_path", "루트")
        correct_cat = classify_doc_category(fname, ext, folder_path=fpath)
        correct_dom = get_domain_group(correct_cat)
        cleaned_txt = clean_and_filter_text(content) if content else ""
        cleaned_txt = re.sub(
            r'^\[문서명:\s*(.*?)\s*\|\s*분류:\s*(.*?)\s*\|\s*폴더경로:\s*(.*?)\s*\|',
            r'[문서명: \1 | 분류: {0} | 폴더경로: {1} |'.format(correct_cat, fpath),
            cleaned_txt
        )
        docs[i] = cleaned_txt

        if meta.get("doc_category") != correct_cat or meta.get("domain_group") != correct_dom:
            new_m = meta.copy()
            new_m["doc_category"] = correct_cat
            new_m["domain_group"] = correct_dom
            new_m["char_length"] = len(cleaned_txt)
            new_m["schema_version"] = SCHEMA_VERSION
            metas[i] = new_m
            fix_ids.append(cid)
            fix_metas.append(new_m)

    if fix_ids:
        collection.update(ids=fix_ids, metadatas=fix_metas)
        print(" -> [DB 메타데이터 교정 완료] 총 {0}개 조각의 카테고리 분류를 즉시 보정했습니다.".format(len(fix_ids)))

    # 로컬 텍스트 캐시(임베딩 대기 문서)도 문서화 및 검색 대장에 통합
    seen_id_set = set(ids)
    if os.path.exists(LOCAL_CACHE_PATH):
        try:
            with open(LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
                cached_list = json.load(f)
            for item in cached_list:
                cid = item.get("id")
                if cid and cid not in seen_id_set and item.get("metadata"):
                    seen_id_set.add(cid)
                    ids.append(cid)
                    docs.append(item.get("document", ""))
                    metas.append(item.get("metadata", {}))
        except Exception:
            pass

    doc_groups, all_chunks_rows = {}, []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    for cid, content, meta in zip(ids, docs, metas):
        if not meta:
            continue
        fname = meta.get("file_name", "미상")
        dgroup = meta.get("domain_group", "미분류")
        cat = meta.get("doc_category", "미분류")
        fpath = meta.get("folder_path", "루트")
        ext = meta.get("file_ext", "")
        loc = meta.get("location", "")
        ctype = meta.get("chunk_type", "일반")
        stopic = meta.get("setup_topic", "일반")
        clean_content = clean_and_filter_text(content)
        clen = len(clean_content)
        link = meta.get("drive_link", "")
        mtime = meta.get("modified_time", "")[:10]

        if fname not in doc_groups:
            doc_groups[fname] = {
                "domain_group": dgroup, "doc_category": cat, "folder_path": fpath,
                "file_name": fname, "file_ext": ext, "chunk_count": 0, "total_chars": 0,
                "topics_set": set(), "chunk_types_set": set(), "drive_link": link,
                "modified_time": mtime, "chunks": []
            }
        doc_groups[fname]["chunk_count"] += 1
        doc_groups[fname]["total_chars"] += clen
        for t_item in stopic.split(","):
            if t_item.strip():
                doc_groups[fname]["topics_set"].add(t_item.strip())
        doc_groups[fname]["chunk_types_set"].add(ctype)
        doc_groups[fname]["chunks"].append({
            "chunk_id": cid, "location": loc, "chunk_type": ctype,
            "setup_topic": stopic, "char_length": clen, "content": clean_content
        })
        all_chunks_rows.append([
            sanitize_for_excel(cid), sanitize_for_excel(dgroup), sanitize_for_excel(cat),
            sanitize_for_excel(fpath), sanitize_for_excel(fname), sanitize_for_excel(loc),
            sanitize_for_excel(ctype), sanitize_for_excel(stopic), clen, sanitize_for_excel(clean_content)
        ])

    sorted_docs = sorted(doc_groups.values(), key=lambda x: (x["domain_group"], x["doc_category"], x["file_name"]))

    for old_f in os.listdir(MD_SPECS_DIR):
        if old_f.endswith(".md"):
            try:
                os.remove(os.path.join(MD_SPECS_DIR, old_f))
            except Exception:
                pass

    for d_info in sorted_docs:
        safe_cat = re.sub(r'[\\/:*?"<>|\s]+', '_', d_info["doc_category"])
        safe_fname = re.sub(r'[\\/:*?"<>|]+', '_', d_info["file_name"])
        md_filepath = os.path.join(MD_SPECS_DIR, "[{0}]_{1}.md".format(safe_cat, safe_fname))
        avg_len = round(d_info["total_chars"] / d_info["chunk_count"]) if d_info["chunk_count"] > 0 else 0
        topics_joined = ", ".join(sorted(list(d_info["topics_set"])))
        md_lines = [
            "# Vector DB 1:1 데이터 명세서: {0}".format(d_info["file_name"]), "",
            "## 1. 문서 분류 및 메타데이터 요약",
            "- **상위 도메인:** `{0}`".format(d_info["domain_group"]),
            "- **9대 표준 분류:** `{0}`".format(d_info["doc_category"]),
            "- **폴더 경로:** `{0}`".format(d_info["folder_path"]),
            "- **총 조각 수:** `{0}개` (평균 `{1}자`)".format(d_info["chunk_count"], avg_len),
            "- **세팅 주제:** `{0}`".format(topics_joined),
            "- **원본 링크:** [구글 드라이브 바로가기]({0})".format(d_info["drive_link"]), "", "---",
            "## 2. 분할 조각(Chunk) 전체 원문 내역", ""
        ]
        for idx, ch in enumerate(d_info["chunks"], start=1):
            md_lines.extend([
                "### [조각 #{0}] 위치: {1} | 유형: {2} | 주제: {3} ({4}자)".format(idx, ch["location"], ch["chunk_type"], ch["setup_topic"], ch["char_length"]),
                "```text", ch["content"], "```", ""
            ])
        with open(md_filepath, "w", encoding="utf-8-sig") as mf:
            mf.write("\n".join(md_lines))

    print(" -> 1) 1:1 마크다운 명세서 생성 완료: 총 {0}개 파일".format(len(sorted_docs)))

    wb = openpyxl.Workbook()
    ws_summary = wb.active
    ws_summary.title = "1_문서별_통합관리대장"
    summary_headers = [
        "연번", "상위 도메인", "9대 표준 분류", "드라이브 폴더 경로", "파일명", "확장자",
        "총 조각(Chunk) 수", "총 글자 수", "조각당 평균 글자 수", "탐지된 세팅/활용 주제",
        "조각 구성 유형", "파일 수정일", "원본 드라이브 링크", "DB 동기화 일시"
    ]
    ws_summary.append(summary_headers)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for col_num in range(1, len(summary_headers) + 1):
        cell = ws_summary.cell(row=1, column=col_num)
        cell.fill, cell.font, cell.alignment = header_fill, header_font, Alignment(horizontal="center", vertical="center")

    sheet_rows_for_gsheet = [summary_headers]
    for idx, d_info in enumerate(sorted_docs, start=1):
        avg_len = round(d_info["total_chars"] / d_info["chunk_count"]) if d_info["chunk_count"] > 0 else 0
        row_vals = [
            idx, sanitize_for_excel(d_info["domain_group"]), sanitize_for_excel(d_info["doc_category"]),
            sanitize_for_excel(d_info["folder_path"]), sanitize_for_excel(d_info["file_name"]),
            sanitize_for_excel(d_info["file_ext"]), d_info["chunk_count"], d_info["total_chars"], avg_len,
            sanitize_for_excel(", ".join(sorted(list(d_info["topics_set"])))),
            sanitize_for_excel(", ".join(sorted(list(d_info["chunk_types_set"])))),
            sanitize_for_excel(d_info["modified_time"]), sanitize_for_excel(d_info["drive_link"]), now_str
        ]
        ws_summary.append(row_vals)
        sheet_rows_for_gsheet.append(row_vals)

    ws_chunks = wb.create_sheet(title="2_전체조각_상세명세")
    chunk_headers = ["조각 ID", "상위 도메인", "9대 표준 분류", "폴더 경로", "파일명", "추출 위치", "조각 유형", "세팅/활용 주제", "글자 수", "임베딩 원문"]
    ws_chunks.append(chunk_headers)
    for col_num in range(1, len(chunk_headers) + 1):
        cell = ws_chunks.cell(row=1, column=col_num)
        cell.fill, cell.font = header_fill, header_font
    for c_row in all_chunks_rows:
        ws_chunks.append(c_row)

    wb.save(EXCEL_REPORT_PATH)
    print(" -> 2) 통합 엑셀 리포트 생성 완료: '{0}'".format(EXCEL_REPORT_PATH))

    if key_path and sheet_id:
        try:
            gc = gspread.service_account(filename=key_path)
            sh = gc.open_by_key(sheet_id)
            try:
                ws_vdb = sh.worksheet("VectorDB_관리대장")
            except gspread.exceptions.WorksheetNotFound:
                ws_vdb = sh.add_worksheet(title="VectorDB_관리대장", rows=max(200, len(sorted_docs) + 50), cols=20)
            ws_vdb.clear()
            ws_vdb.update(range_name="A1", values=sheet_rows_for_gsheet)
            print(" -> 3) 구글 스프레드시트 [VectorDB_관리대장] 탭 자동 업데이트 완료")
        except Exception as e:
            print(" -> [경고] 구글 시트 업데이트 오류: {0}".format(e))
    print("=== [Vector DB 3중 문서화 완료] ===\n")


def ingest_manuals_to_chroma(force_rebuild=False):
    print("=== [Step 1-B: Vector DB v3.6 (무손실 정밀 청킹 + 할당량 초과 시 로컬 캐시 전수 인덱싱) 시작] ===\n")
    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    manual_folder_id = os.getenv("MANUAL_FOLDER_ID")
    bim_folder_id = os.getenv("BIM_FILE_FOLDER_ID")
    chroma_path = os.getenv("CHROMA_DB_PATH")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")
    sheet_id = os.getenv("SPREADSHEET_ID")

    chroma_client = chromadb.PersistentClient(path=chroma_path)
    if force_rebuild:
        print("[주의] 전체 재구축이 선택되어 기존 DB를 초기화합니다...")
        try:
            chroma_client.delete_collection(name="bim_manuals")
        except Exception:
            pass

    collection = chroma_client.get_or_create_collection(name="bim_manuals", metadata={"hnsw:space": "cosine"})
    existing_data = collection.get(include=["metadatas"])

    existing_files = {}
    for cid, meta in zip(existing_data.get("ids", []), existing_data.get("metadatas", [])):
        if meta and "file_id" in meta:
            fid = meta["file_id"]
            if fid not in existing_files:
                existing_files[fid] = {
                    "mod_time": meta.get("modified_time", ""),
                    "total_chunks": meta.get("total_chunks", 0),
                    "chunk_ids": set()
                }
            existing_files[fid]["chunk_ids"].add(cid)
            if meta.get("total_chunks", 0) > existing_files[fid]["total_chunks"]:
                existing_files[fid]["total_chunks"] = meta.get("total_chunks", 0)

    print("1. 현재 Vector DB에 적재된 문서 수: {0}개".format(len(existing_files)))

    creds = Credentials.from_service_account_file(key_path, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    drive_service = build('drive', 'v3', credentials=creds)

    manual_files = get_all_files_recursively(drive_service, manual_folder_id, root_label="매뉴얼루트")
    bim_files = get_all_files_recursively(drive_service, bim_folder_id, root_label="BIM파일루트")
    unique_drive_files = {f.get('id'): f for f in bim_files + manual_files}
    drive_files, _ = filter_duplicate_pdf_files(list(unique_drive_files.values()))

    valid_drive_ids = {f.get('id') for f in drive_files}
    for del_id in (set(existing_files.keys()) - valid_drive_ids):
        collection.delete(where={"file_id": del_id})

    embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001", google_api_key=gemini_api_key)
    supported_extensions = {'pdf', 'docx', 'pptx', 'xlsx', 'txt'}
    completed_count = 0
    quota_exhausted = False

    for file in drive_files:
        file_id, file_name = file.get('id'), file.get('name', '')
        mime_type, drive_mod_time = file.get('mimeType', ''), file.get('modifiedTime', '')
        folder_path = file.get('folder_path', '루트폴더')
        ext = file.get('fileExtension', '').lower() or (file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else '')

        export_mime = None
        if mime_type == 'application/vnd.google-apps.spreadsheet':
            ext, export_mime = 'xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        elif mime_type == 'application/vnd.google-apps.presentation':
            ext, export_mime = 'pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        elif mime_type == 'application/vnd.google-apps.document':
            ext, export_mime = 'docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

        if ext not in supported_extensions:
            continue

        doc_category = classify_doc_category(file_name, ext, folder_path=folder_path)
        domain_group = get_domain_group(doc_category)

        prev_state = existing_files.get(file_id)
        existing_cids = set()

        if prev_state:
            if prev_state["mod_time"] == drive_mod_time:
                expected_cnt = prev_state["total_chunks"]
                actual_cnt = len(prev_state["chunk_ids"])
                if expected_cnt == 0 or actual_cnt >= expected_cnt:
                    print(" - [100% 완료/보존] [{0} | {1}] '{2}' ({3}개 조각 완비 -> 스킵)".format(
                        domain_group, doc_category, file_name, actual_cnt
                    ))
                    continue
                else:
                    existing_cids = prev_state["chunk_ids"]
            else:
                collection.delete(where={"file_id": file_id})

        print(" - [정밀 적재/인덱싱] [{0} | {1}] '{2}' (경로: {3}) 추출 중...".format(domain_group, doc_category, file_name, folder_path))
        temp_path = None
        try:
            request = drive_service.files().export_media(fileId=file_id, mimeType=export_mime) if export_mime else drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".{0}".format(ext)) as temp_file:
                temp_file.write(fh.getvalue())
                temp_path = temp_file.name

            base_meta = {
                "file_id": file_id, "file_name": file_name, "domain_group": domain_group,
                "doc_category": doc_category, "folder_path": folder_path, "file_ext": ext,
                "drive_link": file.get('webViewLink', ''), "modified_time": drive_mod_time
            }
            docs = extract_documents_from_file(temp_path, ext, base_meta)
            if docs:
                hit_daily_limit = embed_and_save_incrementally(
                    collection, embeddings, file_id, docs, existing_cids, quota_exhausted=quota_exhausted
                )
                if hit_daily_limit:
                    quota_exhausted = True
                completed_count += 1
        except Exception as e:
            print("   -> [오류 발생] '{0}' 처리 실패: {1}\n".format(file_name, e))
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    export_vector_db_documentation(collection, key_path, sheet_id)
    print("=== [Vector DB 동기화 및 전수 인덱싱 종료 (이번 처리 파일: {0}개)] ===".format(completed_count))


if __name__ == "__main__":
    ingest_manuals_to_chroma()
