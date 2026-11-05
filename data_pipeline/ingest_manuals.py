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
from openpyxl.styles import Font, PatternFill, Alignment

MIN_CHUNK_CHARS = 50
MAX_EXCEL_CHUNKS_PER_SHEET = 15
SCHEMA_VERSION = "v3.2"

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
DB_DOCS_DIR = os.path.join(BASE_DIR, "db_docs")
MD_SPECS_DIR = os.path.join(DB_DOCS_DIR, "markdown_specs")
EXCEL_REPORT_PATH = os.path.join(DB_DOCS_DIR, "VectorDB_통합관리대장.xlsx")

STANDARD_CATEGORIES = [
    "대외/발주처 BIM 지침",
    "사내 표준/세팅기준",
    "스펙북/자재기준",
    "소프트웨어 가이드북",
    "BIM 검토보고서",
    "BIM 검토리스트",
    "수량산출서",
    "BIM 견적서",
    "BIM 기술제안서/BEP",
    "기타기술문서"
]


def classify_doc_category(file_name, ext, folder_path=""):
    """상위 폴더 경로(folder_path)와 파일명(file_name)을 동시에 분석하여 9대 표준 카테고리 및 향후 신규 폴더 분류를 자동 지정합니다."""
    combined_target = "{0} {1}".format(folder_path, file_name).lower()
    folder_clean = folder_path.strip()

    if any(k in combined_target for k in ['01_', '대외', '발주처', '조달청', '국토부', 'lh', '도로공사', '지침서', '납품기준']):
        return "대외/발주처 BIM 지침"
    elif any(k in combined_target for k in ['02_', '사내표준', '세팅기준', '표준양식', '템플릿', 'template', '매개변수', 'parameter']):
        return "사내 표준/세팅기준"
    elif any(k in combined_target for k in ['03_', '스펙', 'spec', '시방서', '재료', '자재', '마감']):
        return "스펙북/자재기준"
    elif any(k in combined_target for k in ['04_', '뷰어', '가이드북', '매뉴얼', 'manual', 'guide', '다룩스', 'dalux', '트림블', 'trimble', 'acc', 'navisworks']):
        return "소프트웨어 가이드북"
    elif any(k in combined_target for k in ['05_', '검토보고', '보고서', 'report']):
        return "BIM 검토보고서"
    elif any(k in combined_target for k in ['06_', '검토리스트', '체크리스트', 'checklist', '검토목록']):
        return "BIM 검토리스트"
    elif any(k in combined_target for k in ['08_', '견적', '내역서', '일위대가', '단가', '원가', 'cost', 'estimate']):
        return "BIM 견적서"
    elif any(k in combined_target for k in ['07_', '수량', '물량', '산출', '비교표', 'boq', 'qto']):
        return "수량산출서"
    elif any(k in combined_target for k in ['09_', '제안서', '기술제안', '수행계획', 'bep', '입찰', 'proposal']):
        return "BIM 기술제안서/BEP"

    if folder_clean and folder_clean not in {"루트폴더", "매뉴얼루트", "BIM파일루트"}:
        top_sub = folder_clean.split("/")[0].strip()
        cleaned_sub = re.sub(r'^\d+[_-\s]*', '', top_sub).strip()
        if cleaned_sub:
            return cleaned_sub

    return "수량산출서" if ext == 'xlsx' else "기타기술문서"


def get_domain_group(doc_category):
    """9대 카테고리를 4대 상위 도메인으로 그룹화합니다."""
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
    """조각(Chunk)의 텍스트 내용을 분석하여 프로젝트 세팅 및 실무 활용 주제를 자동 태깅합니다."""
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
    if any(k in t_lower for k in ['수행계획', 'bep', '조직도', '일정', '목표', '제안', '전략', '프로세스']):
        topics.append("기술제안/BEP수행전략")

    return ", ".join(topics) if topics else "일반내용/개요"


def clean_and_filter_text(text):
    """불필요한 연속 공백과 줄바꿈을 정리합니다."""
    if not text:
        return ""
    cleaned = re.sub(r'[ \t]+', ' ', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def extract_shapes_text_recursively(shapes):
    """PPTX 슬라이드 내 일반 도형, 표, 그리고 그룹화된 도형(GroupShape) 내부의 텍스트까지 재귀적으로 모두 추출합니다."""
    extracted = []
    for shape in shapes:
        if hasattr(shape, "shapes"):
            extracted.extend(extract_shapes_text_recursively(shape.shapes))
        if getattr(shape, "has_text_frame", False):
            for paragraph in shape.text_frame.paragraphs:
                if paragraph.text and paragraph.text.strip():
                    extracted.append(paragraph.text.strip())
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                row_txt = [cell.text.strip().replace('\n', ' ') for cell in row.cells if cell.text and cell.text.strip()]
                if row_txt:
                    extracted.append(" | ".join(row_txt))
    return extracted


def extract_documents_from_file(temp_path, ext, base_meta):
    """문서 포맷별 텍스트 추출, 문맥 헤더(Contextual Header) 주입 및 동적 엑셀 병합을 수행합니다."""
    docs = []
    seen_hashes = set()
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1800, chunk_overlap=200)

    def add_valid_chunk(content, location_str, chunk_type="일반본문"):
        cleaned = clean_and_filter_text(content)
        if len(cleaned) < MIN_CHUNK_CHARS:
            return
        content_hash = hashlib.md5(cleaned.encode('utf-8')).hexdigest()
        if content_hash in seen_hashes:
            return
        seen_hashes.add(content_hash)

        topic_str = detect_setup_topic(cleaned)
        contextual_text = (
            "[문서명: {0} | 분류: {1} | 폴더경로: {2} | 위치: {3} | 주제: {4}]\n{5}".format(
                base_meta["file_name"],
                base_meta["doc_category"],
                base_meta.get("folder_path", "루트"),
                location_str,
                topic_str,
                cleaned
            )
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
                        params.append("- 매개변수명: {0} | 자료형: {1} | 그룹: {2} | GUID: {3}".format(p_name, p_type, p_group, p_guid))
                if params:
                    param_summary = (
                        "[Revit 사내 표준 공유 매개변수(Shared Parameters) 원본 정의 - 파일: {0}]\n"
                        "총 등록된 매개변수 수: {1}개\n".format(base_meta['file_name'], len(params)) + "\n".join(params)
                    )
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
                r_txt = [cell.text.strip().replace('\n', ' ') for cell in row.cells if cell.text.strip()]
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
            ws_val = wb_val[sheet]
            ws_form = wb_formula[sheet]
            rows_val = list(ws_val.iter_rows(values_only=True))
            rows_form = list(ws_form.iter_rows(values_only=True))

            if len(rows_val) < 2:
                continue

            header_idx = 0
            max_non_empty = 0
            for idx in range(min(10, len(rows_val))):
                non_empty = sum(1 for c in rows_val[idx] if c is not None and str(c).strip() != "")
                if non_empty > max_non_empty:
                    max_non_empty = non_empty
                    header_idx = idx

            headers = [str(h).strip().replace('\n', ' ') if h is not None else "열{0}".format(i + 1) for i, h in enumerate(rows_val[header_idx])]
            valid_headers = [h for h in headers if not h.startswith("열")]

            header_joined = " ".join(valid_headers)
            if any(k in header_joined for k in ['단가', '금액', '재료비', '노무비', '경비', '일위대가']) and base_meta.get("doc_category") == "수량산출서":
                base_meta["doc_category"] = "BIM 견적서"
                base_meta["domain_group"] = "3_원가및적산"

            formula_samples = []
            summary_rows = []
            for r_i in range(header_idx + 1, len(rows_form)):
                r_f = rows_form[r_i]
                r_v = rows_val[r_i]
                if len(formula_samples) < 8:
                    for h_name, f_cell, v_cell in zip(headers, r_f, r_v):
                        if isinstance(f_cell, str) and f_cell.startswith('='):
                            sample_str = "[{0}] 산출수식: {1} (계산값 예시: {2})".format(h_name, f_cell, v_cell)
                            if sample_str not in formula_samples:
                                formula_samples.append(sample_str)
                row_str_joined = " ".join([str(c) for c in r_v if c is not None])
                if any(k in row_str_joined for k in ['합계', '소계', '총계', '계', '비교', '차이', '총괄', '순공사비']) and len(summary_rows) < 12:
                    items = ["{0}: {1}".format(h, str(v).strip()) for h, v in zip(headers, r_v) if v is not None and str(v).strip() != ""]
                    summary_rows.append("({0}행 요약) ".format(r_i + 1) + " | ".join(items))

            structure_summary = (
                "[엑셀 양식 및 산출/견적 방법 요약 - 파일: {0} / 시트: {1}]\n"
                "- 문서 분류: {2}\n"
                "- 전체 데이터 행 수: 총 {3}행\n"
                "- 표 컬럼(양식 항목) 구성: {4}\n"
                "- 적용된 주요 산출/견적 수식(Formula) 예시:\n  {5}\n"
                "- 시트 내 주요 합계/비교 요약 행:\n  {6}".format(
                    base_meta['file_name'],
                    sheet,
                    base_meta['doc_category'],
                    len(rows_val) - header_idx - 1,
                    ', '.join(valid_headers),
                    "\n  ".join(formula_samples) if formula_samples else "별도 수식 없음(단순 값 기입 방식)",
                    "\n  ".join(summary_rows) if summary_rows else "별도 합계 행 없음"
                )
            )
            add_valid_chunk(structure_summary, "{0} 시트 (양식/산출식 구조)".format(sheet), "양식및산출기준")

            total_data_rows = max(1, len(rows_val) - (header_idx + 1))
            rows_per_chunk = max(25, (total_data_rows // MAX_EXCEL_CHUNKS_PER_SHEET) + 1)

            batch_rows_text = []
            start_row_num = header_idx + 2

            for r_idx in range(header_idx + 1, len(rows_val)):
                row = rows_val[r_idx]
                row_items = ["{0}: {1}".format(h, str(val).strip()) for h, val in zip(headers, row) if val is not None and str(val).strip() != ""]
                if row_items:
                    batch_rows_text.append("({0}행) ".format(r_idx + 1) + " | ".join(row_items))

                if len(batch_rows_text) >= rows_per_chunk or r_idx == len(rows_val) - 1:
                    if batch_rows_text:
                        combined_text = "[{0} 시트 {1}~{2}행 데이터]\n".format(sheet, start_row_num, r_idx + 1) + "\n".join(batch_rows_text)
                        add_valid_chunk(combined_text, "{0} 시트 {1}~{2}행".format(sheet, start_row_num, r_idx + 1), "세부데이터")
                    batch_rows_text = []
                    start_row_num = r_idx + 2

    return docs


def embed_with_rate_limit(embeddings, texts, batch_size=80):
    """무한 루프 방지 및 API 할당량 제어를 위한 배치 임베딩 함수입니다."""
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
                print("     ... 임베딩 진행률: {0} / {1} 조각 완료".format(min(i + batch_size, total), total))
                if i + batch_size < total:
                    time.sleep(2)
                break
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    retry_count += 1
                    if retry_count > max_retries:
                        print("\n[치명적 오류] 일일 무료 할당량이 고갈되었습니다. 작업을 안전하게 종료합니다.")
                        raise e
                    wait_sec = 45
                    print("   -> [API 할당량 대기] {0}초 대기 후 재시도... ({1}/{2}회)".format(wait_sec, retry_count, max_retries))
                    time.sleep(wait_sec)
                else:
                    raise e
    return all_vectors


def get_all_files_recursively(drive_service, folder_id, root_label="루트폴더"):
    """지정된 드라이브 폴더와 모든 하위 폴더를 재귀 스캔하며 각 파일의 상위 폴더 경로(folder_path)를 함께 기록합니다."""
    if not folder_id:
        return []
    all_files = []
    folders_to_scan = [(folder_id, "")]
    visited_folders = set()

    while folders_to_scan:
        current_folder, current_path = folders_to_scan.pop(0)
        if current_folder in visited_folders:
            continue
        visited_folders.add(current_folder)

        page_token = None
        while True:
            query = "'{0}' in parents and trashed = false".format(current_folder)
            results = drive_service.files().list(
                q=query,
                pageSize=1000,
                pageToken=page_token,
                fields="nextPageToken, files(id, name, mimeType, fileExtension, webViewLink, modifiedTime)"
            ).execute()
            for item in results.get('files', []):
                if item.get('mimeType') == 'application/vnd.google-apps.folder':
                    sub_name = item.get('name', '').strip()
                    next_path = "{0}/{1}".format(current_path, sub_name) if current_path else sub_name
                    folders_to_scan.append((item.get('id'), next_path))
                else:
                    item['folder_path'] = current_path if current_path else root_label
                    all_files.append(item)
            page_token = results.get('nextPageToken')
            if not page_token:
                break
    return all_files


def filter_duplicate_pdf_files(drive_files):
    """동일한 파일명의 원본(.pptx, .docx, .xlsx)과 변환본(.pdf)이 공존할 경우 원본만 남기고 .pdf를 자동 제외합니다."""
    editable_basenames = set()
    for f in drive_files:
        fname = f.get('name', '')
        mime = f.get('mimeType', '')
        ext = f.get('fileExtension', '').lower()
        if not ext and '.' in fname:
            ext = fname.rsplit('.', 1)[-1].lower()
        if mime in {'application/vnd.google-apps.spreadsheet', 'application/vnd.google-apps.presentation', 'application/vnd.google-apps.document'}:
            base = fname.rsplit('.', 1)[0].strip().lower()
            editable_basenames.add(base)
        elif ext in {'pptx', 'docx', 'xlsx'}:
            base = fname.rsplit('.', 1)[0].strip().lower()
            editable_basenames.add(base)

    filtered = []
    skipped_pdfs = []
    for f in drive_files:
        fname = f.get('name', '')
        ext = f.get('fileExtension', '').lower()
        if not ext and '.' in fname:
            ext = fname.rsplit('.', 1)[-1].lower()
        base = fname.rsplit('.', 1)[0].strip().lower()
        if ext == 'pdf' and base in editable_basenames:
            skipped_pdfs.append(fname)
            continue
        filtered.append(f)

    return filtered, skipped_pdfs


def export_vector_db_documentation(collection, key_path, sheet_id):
    """
    Vector DB(ChromaDB)에 적재된 전체 데이터를 추출하여 3중 문서화 체계를 자동 구축합니다:
    1) Google Sheets 통합 대장 (2번째 시트 탭 'VectorDB_관리대장' 자동 생성 및 일괄 기록)
    2) 로컬 통합 엑셀 마스터 리포트 ('db_docs/VectorDB_통합관리대장.xlsx' - 요약 시트 + 전체 청크 상세 시트)
    3) 파일별 1:1 마크다운 벡터 명세서 ('db_docs/markdown_specs/[분류]_파일명.md')
    """
    print("\n=== [Step 1-C: Vector DB 3중 문서화(Google Sheets + 통합 Excel + 1:1 Markdown) 자동 생성 시작] ===")
    os.makedirs(DB_DOCS_DIR, exist_ok=True)
    os.makedirs(MD_SPECS_DIR, exist_ok=True)

    data = collection.get(include=["documents", "metadatas"])
    ids = data.get("ids", [])
    docs = data.get("documents", [])
    metas = data.get("metadatas", [])

    doc_groups = {}
    all_chunks_rows = []
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
        clen = len(content) if content else 0
        link = meta.get("drive_link", "")
        mtime = meta.get("modified_time", "")[:10]

        if fname not in doc_groups:
            doc_groups[fname] = {
                "domain_group": dgroup,
                "doc_category": cat,
                "folder_path": fpath,
                "file_name": fname,
                "file_ext": ext,
                "chunk_count": 0,
                "total_chars": 0,
                "topics_set": set(),
                "chunk_types_set": set(),
                "drive_link": link,
                "modified_time": mtime,
                "chunks": []
            }

        doc_groups[fname]["chunk_count"] += 1
        doc_groups[fname]["total_chars"] += clen
        for t_item in stopic.split(","):
            if t_item.strip():
                doc_groups[fname]["topics_set"].add(t_item.strip())
        doc_groups[fname]["chunk_types_set"].add(ctype)
        doc_groups[fname]["chunks"].append({
            "chunk_id": cid,
            "location": loc,
            "chunk_type": ctype,
            "setup_topic": stopic,
            "char_length": clen,
            "content": content
        })

        all_chunks_rows.append([
            cid, dgroup, cat, fpath, fname, loc, ctype, stopic, clen, content
        ])

    # 정렬: 도메인 -> 표준분류 -> 파일명 순
    sorted_docs = sorted(
        doc_groups.values(),
        key=lambda x: (x["domain_group"], x["doc_category"], x["file_name"])
    )

    # ---------------------------------------------------------
    # 1. 파일별 1:1 마크다운(.md) 상세 명세서 생성 (기존 구버전 파일 정리 후 재생성)
    # ---------------------------------------------------------
    for old_f in os.listdir(MD_SPECS_DIR):
        if old_f.endswith(".md"):
            try:
                os.remove(os.path.join(MD_SPECS_DIR, old_f))
            except Exception:
                pass

    for d_info in sorted_docs:
        safe_cat = re.sub(r'[\\/:*?"<>|\s]+', '_', d_info["doc_category"])
        safe_fname = re.sub(r'[\\/:*?"<>|]+', '_', d_info["file_name"])
        md_filename = "[{0}]_{1}.md".format(safe_cat, safe_fname)
        md_filepath = os.path.join(MD_SPECS_DIR, md_filename)

        avg_len = round(d_info["total_chars"] / d_info["chunk_count"]) if d_info["chunk_count"] > 0 else 0
        topics_joined = ", ".join(sorted(list(d_info["topics_set"])))

        md_lines = [
            "# Vector DB 1:1 데이터 명세서: {0}".format(d_info["file_name"]),
            "",
            "## 1. 문서 분류 및 메타데이터 요약",
            "- **상위 도메인:** `{0}`".format(d_info["domain_group"]),
            "- **9대 표준 분류:** `{0}`".format(d_info["doc_category"]),
            "- **구글 드라이브 폴더 경로:** `{0}`".format(d_info["folder_path"]),
            "- **파일 형식:** `{0}`".format(d_info["file_ext"]),
            "- **총 분할 조각(Chunk) 수:** `{0}개` (총 `{1}자`, 조각당 평균 `{2}자`)".format(d_info["chunk_count"], d_info["total_chars"], avg_len),
            "- **탐지된 실무 세팅/활용 주제:** `{0}`".format(topics_joined),
            "- **원본 파일 열기:** [구글 드라이브 바로가기]({0})".format(d_info["drive_link"]),
            "- **명세서 갱신 일시:** `{0}`".format(now_str),
            "",
            "---",
            "## 2. 분할 조각(Chunk) 전체 원문 및 문맥 헤더 내역",
            ""
        ]

        for idx, ch in enumerate(d_info["chunks"], start=1):
            md_lines.append("### [조각 #{0}] 위치: {1} | 유형: {2} | 주제: {3} ({4}자)".format(
                idx, ch["location"], ch["chunk_type"], ch["setup_topic"], ch["char_length"]
            ))
            md_lines.append("```text")
            md_lines.append(ch["content"])
            md_lines.append("```")
            md_lines.append("")

        with open(md_filepath, "w", encoding="utf-8-sig") as mf:
            mf.write("\n".join(md_lines))

    print(" -> 1) 1:1 마크다운 명세서 생성 완료: '{0}' 폴더 내 총 {1}개 파일".format(MD_SPECS_DIR, len(sorted_docs)))

    # ---------------------------------------------------------
    # 2. 로컬 통합 엑셀 마스터 대장 (.xlsx) 생성 (시트1: 통합관리대장 / 시트2: 전체청크명세)
    # ---------------------------------------------------------
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
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    sheet_rows_for_gsheet = [summary_headers]

    for idx, d_info in enumerate(sorted_docs, start=1):
        avg_len = round(d_info["total_chars"] / d_info["chunk_count"]) if d_info["chunk_count"] > 0 else 0
        topics_str = ", ".join(sorted(list(d_info["topics_set"])))
        ctypes_str = ", ".join(sorted(list(d_info["chunk_types_set"])))
        row_vals = [
            idx,
            d_info["domain_group"],
            d_info["doc_category"],
            d_info["folder_path"],
            d_info["file_name"],
            d_info["file_ext"],
            d_info["chunk_count"],
            d_info["total_chars"],
            avg_len,
            topics_str,
            ctypes_str,
            d_info["modified_time"],
            d_info["drive_link"],
            now_str
        ]
        ws_summary.append(row_vals)
        sheet_rows_for_gsheet.append(row_vals)

    # 시트 2: 전체 조각(Chunk) 상세 명세
    ws_chunks = wb.create_sheet(title="2_전체조각_상세명세")
    chunk_headers = [
        "조각 ID", "상위 도메인", "9대 표준 분류", "폴더 경로", "파일명",
        "추출 위치", "조각 유형", "세팅/활용 주제", "글자 수", "임베딩 원문 (문맥 헤더 포함)"
    ]
    ws_chunks.append(chunk_headers)
    for col_num in range(1, len(chunk_headers) + 1):
        cell = ws_chunks.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font

    for c_row in all_chunks_rows:
        ws_chunks.append(c_row)

    wb.save(EXCEL_REPORT_PATH)
    print(" -> 2) 통합 엑셀 리포트 생성 완료: '{0}' (문서 {1}개 / 조각 {2}개)".format(EXCEL_REPORT_PATH, len(sorted_docs), len(all_chunks_rows)))

    # ---------------------------------------------------------
    # 3. Google Sheets ('VectorDB_관리대장' 탭) 실시간 클라우드 동기화
    # ---------------------------------------------------------
    if key_path and sheet_id:
        try:
            gc = gspread.service_account(filename=key_path)
            sh = gc.open_by_key(sheet_id)
            tab_title = "VectorDB_관리대장"
            try:
                ws_vdb = sh.worksheet(tab_title)
            except gspread.exceptions.WorksheetNotFound:
                ws_vdb = sh.add_worksheet(title=tab_title, rows=max(200, len(sorted_docs) + 50), cols=20)

            ws_vdb.clear()
            ws_vdb.update(range_name="A1", values=sheet_rows_for_gsheet)
            print(" -> 3) 구글 스프레드시트 [{0}] 탭 자동 업데이트 완료 (총 {1}개 문서 현황 등재)".format(tab_title, len(sorted_docs)))
        except Exception as e:
            print(" -> [경고] 구글 시트 'VectorDB_관리대장' 탭 업데이트 중 오류 발생: {0}".format(e))

    print("=== [Vector DB 3중 문서화 완료] ===\n")


def ingest_manuals_to_chroma(force_rebuild=False):
    print("=== [Step 1-B: Vector DB v3.2 (9대 표준 분류 + 3중 자동 문서화) 동기화 시작] ===\n")

    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    manual_folder_id = os.getenv("MANUAL_FOLDER_ID")
    bim_folder_id = os.getenv("BIM_FILE_FOLDER_ID")
    chroma_path = os.getenv("CHROMA_DB_PATH")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")
    sheet_id = os.getenv("SPREADSHEET_ID")

    chroma_client = chromadb.PersistentClient(path=chroma_path)
    if force_rebuild:
        print("[초기화] 기존 Vector DB 컬렉션을 삭제하고 v3.2 표준 규격으로 재구축합니다...")
        try:
            chroma_client.delete_collection(name="bim_manuals")
        except Exception:
            pass

    collection = chroma_client.get_or_create_collection(
        name="bim_manuals",
        metadata={"hnsw:space": "cosine"}
    )

    existing_data = collection.get(include=["metadatas"])
    existing_files = {}
    for meta in existing_data.get("metadatas", []):
        if meta and "file_id" in meta:
            fid = meta["file_id"]
            if meta.get("schema_version") != SCHEMA_VERSION:
                existing_files[fid] = {"mod_time": "NEED_UPGRADE", "category": "", "folder": ""}
            else:
                existing_files[fid] = {
                    "mod_time": meta.get("modified_time", ""),
                    "category": meta.get("doc_category", ""),
                    "folder": meta.get("folder_path", "")
                }

    print("1. 현재 Vector DB에 등록된 문서 파일 수: {0}개".format(len(existing_files)))

    drive_scope = "https://" + "www.googleapis.com/auth/drive.readonly"
    creds = Credentials.from_service_account_file(key_path, scopes=[drive_scope])
    drive_service = build('drive', 'v3', credentials=creds)

    manual_files = get_all_files_recursively(drive_service, manual_folder_id, root_label="매뉴얼루트")
    bim_files = get_all_files_recursively(drive_service, bim_folder_id, root_label="BIM파일루트")
    unique_drive_files = {f.get('id'): f for f in bim_files + manual_files}
    all_scanned_files = list(unique_drive_files.values())

    drive_files, skipped_pdfs = filter_duplicate_pdf_files(all_scanned_files)
    if skipped_pdfs:
        print("2. [중복 자동 차단] 원본(.pptx/.docx)과 중복되는 PDF {0}건 자동 제외: {1}".format(len(skipped_pdfs), ", ".join(skipped_pdfs)))

    valid_drive_ids = {f.get('id') for f in drive_files}
    deleted_ids = set(existing_files.keys()) - valid_drive_ids
    deleted_count = 0

    if deleted_ids:
        for del_id in deleted_ids:
            collection.delete(where={"file_id": del_id})
            deleted_count += 1
        print("3. [삭제/중복 정리] 드라이브에서 삭제되었거나 중복 제외된 문서 {0}건 Vector DB에서 제거 완료\n".format(deleted_count))

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
        folder_path = file.get('folder_path', '루트폴더')

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

        doc_category = classify_doc_category(file_name, ext, folder_path=folder_path)
        domain_group = get_domain_group(doc_category)

        is_update = False
        if file_id in existing_files:
            prev_info = existing_files[file_id]
            if prev_info["mod_time"] == drive_mod_time and prev_info["category"] == doc_category and prev_info["folder"] == folder_path:
                print(" - [유지] [{0} | {1}] '{2}' (최신 상태)".format(domain_group, doc_category, file_name))
                continue
            else:
                print(" - [폴더이동/변경 감지] [{0} | {1}] '{2}' (경로: {3}) 재학습 중...".format(domain_group, doc_category, file_name, folder_path))
                is_update = True
        else:
            print(" - [신규 파일 감지] [{0} | {1}] '{2}' (경로: {3}) 추출 중...".format(domain_group, doc_category, file_name, folder_path))

        temp_path = None
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

            with tempfile.NamedTemporaryFile(delete=False, suffix=".{0}".format(ext)) as temp_file:
                temp_file.write(fh.getvalue())
                temp_path = temp_file.name

            base_meta = {
                "file_id": file_id,
                "file_name": file_name,
                "domain_group": domain_group,
                "doc_category": doc_category,
                "folder_path": folder_path,
                "file_ext": ext,
                "drive_link": file.get('webViewLink', ''),
                "modified_time": drive_mod_time
            }

            docs = extract_documents_from_file(temp_path, ext, base_meta)

            if docs:
                texts = [doc.page_content for doc in docs]
                metadatas = [doc.metadata for doc in docs]
                ids = ["{0}_chunk_{1}".format(file_id, i) for i in range(len(docs))]

                print("   -> 문맥 헤더 주입 및 정제 완료: 총 {0}개 조각 (임베딩 시작...)".format(len(docs)))
                vectors = embed_with_rate_limit(embeddings, texts, batch_size=80)

                if is_update:
                    collection.delete(where={"file_id": file_id})
                collection.add(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)

                if is_update:
                    updated_count += 1
                else:
                    new_count += 1
                print("   -> 완료: '{0}' Vector DB 적재 성공\n".format(file_name))
            else:
                print("   -> [경고] '{0}'에서 유효한 텍스트가 추출되지 않았습니다.\n".format(file_name))

        except Exception as e:
            print("   -> [오류 발생] '{0}' 처리 실패 (기존 DB 데이터 보존됨): {1}\n".format(file_name, e))

        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    print("\n4. 동기화 요약: 신규 {0}개 / 갱신(폴더이동 포함) {1}개 / 삭제·중복제거 {2}개".format(new_count, updated_count, deleted_count))

    # 동기화 직후 3중 문서화(Google Sheets + 통합 Excel + 1:1 Markdown) 자동 실행
    export_vector_db_documentation(collection, key_path, sheet_id)
    print("=== [Vector DB v3.2 완전 동기화 및 문서화 종료] ===")


if __name__ == "__main__":
    ingest_manuals_to_chroma()
