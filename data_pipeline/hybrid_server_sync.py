import os
import io
import re
import json
import time
import stat
import shutil
import hashlib
import datetime
import tempfile
from dotenv import load_dotenv
import chromadb
import gspread
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)


def clean_env_val(key, default=""):
    val = os.getenv(key, default)
    if not val:
        return default
    return val.strip().strip('"').strip("'").strip()


def read_raw_server_path_from_env(key="SERVER_STORAGE_PATH", default=""):
    """
    .env 파일에서 큰따옴표("...")나 작은따옴표('...')와 무관하게 원문 그대로의
    Windows UNC 네트워크 경로(\\\\172.17.100.2\\...)를 읽어옵니다.
    """
    raw_val = ""
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line_s = line.strip()
                    if not line_s or line_s.startswith("#"):
                        continue
                    m = re.match(r"^" + re.escape(key) + r"\s*=\s*(.*)$", line_s)
                    if m:
                        val_part = m.group(1).strip()
                        if (val_part.startswith('"') and val_part.endswith('"')) or (
                            val_part.startswith("'") and val_part.endswith("'")
                        ):
                            val_part = val_part[1:-1].strip()
                        raw_val = val_part
                        break
        except Exception:
            pass

    if not raw_val:
        raw_val = clean_env_val(key, default)
    if not raw_val:
        return default

    norm_val = raw_val.replace("/", "\\")
    if norm_val.startswith("\\") and not norm_val.startswith("\\\\"):
        norm_val = "\\" + norm_val
    elif re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\\", norm_val):
        norm_val = "\\\\" + norm_val

    return norm_val.rstrip("\\")


def check_server_accessible(path_str):
    if not path_str:
        return False
    try:
        if os.path.exists(path_str):
            return True
        parent_dir = os.path.dirname(path_str)
        if parent_dir and os.path.exists(parent_dir):
            os.makedirs(path_str, exist_ok=True)
            return True
        os.makedirs(path_str, exist_ok=True)
        return os.path.exists(path_str)
    except Exception as e:
        print("[사내 서버 경로 접근 점검 안내] 지정 경로 접근 불가 ({0}): {1}".format(path_str, e))
        return False


GOOGLE_API_KEY = clean_env_val("GOOGLE_API_KEY")
CRED_PATH = clean_env_val("GOOGLE_APPLICATION_CREDENTIALS")
if CRED_PATH and not os.path.isabs(CRED_PATH):
    CRED_PATH = os.path.join(BASE_DIR, CRED_PATH)

SHEET_ID = clean_env_val("SPREADSHEET_ID")
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def resolve_storage_paths():
    """
    [사내 서버 3대 폴더 일원화 아키텍처]
    - 00_시스템인덱스: chroma_db, doc_markdown(파일별 분석 .md), BF_파일및벡터DB_관리대장.xlsx, JSON 인덱스
    - 01_프로젝트_산출물: 01_설계검토_보고서 / 02_수량산출서_내역비교표 / 03_공정시뮬레이션_시각자료 (자동 분류 배포)
    - 99_bf_archive: BIM_File_Folder / Manual_Folder (마스터 원본 투입소)
    """
    raw_srv = read_raw_server_path_from_env("SERVER_STORAGE_PATH")
    srv_ok = check_server_accessible(raw_srv) if raw_srv else False
    if raw_srv and srv_ok:
        srv_root = raw_srv
        idx_dir = os.path.join(srv_root, "00_시스템인덱스")
        out_dir = os.path.join(srv_root, "01_프로젝트_산출물")
        arc_dir = os.path.join(srv_root, "99_bf_archive")
        md_dir = os.path.join(idx_dir, "doc_markdown")
        c_path = read_raw_server_path_from_env("CHROMA_DB_PATH") or os.path.join(idx_dir, "chroma_db")
        cat_json = os.path.join(idx_dir, "showcase_catalog.json")
        sld_json = os.path.join(idx_dir, "showcase_slides_index.json")
        excel_ledger = os.path.join(idx_dir, "BF_파일및벡터DB_관리대장.xlsx")
        for d in (idx_dir, out_dir, arc_dir, md_dir):
            os.makedirs(d, exist_ok=True)
        return srv_root, idx_dir, out_dir, arc_dir, md_dir, c_path, cat_json, sld_json, excel_ledger, True
    else:
        temp_root = os.path.join(tempfile.gettempdir(), "BIM_AI_Offline_Cache")
        idx_dir = os.path.join(temp_root, "00_시스템인덱스")
        out_dir = os.path.join(temp_root, "01_프로젝트_산출물")
        arc_dir = os.path.join(temp_root, "99_bf_archive")
        md_dir = os.path.join(idx_dir, "doc_markdown")
        c_path = os.path.join(idx_dir, "chroma_db")
        cat_json = os.path.join(idx_dir, "showcase_catalog.json")
        sld_json = os.path.join(idx_dir, "showcase_slides_index.json")
        excel_ledger = os.path.join(idx_dir, "BF_파일및벡터DB_관리대장.xlsx")
        for d in (idx_dir, out_dir, arc_dir, md_dir):
            os.makedirs(d, exist_ok=True)
        return raw_srv or temp_root, idx_dir, out_dir, arc_dir, md_dir, c_path, cat_json, sld_json, excel_ledger, False


(
    SERVER_STORAGE_PATH,
    SYSTEM_INDEX_DIR,
    PROJECT_OUTPUT_DIR,
    ARCHIVE_MASTER_DIR,
    DOC_MARKDOWN_DIR,
    CHROMA_PATH,
    CATALOG_JSON_PATH,
    SLIDES_JSON_PATH,
    EXCEL_LEDGER_PATH,
    IS_SERVER_ACCESSIBLE
) = resolve_storage_paths()

EXCLUDED_SYSTEM_DIRS = {
    "00_시스템인덱스", "01_프로젝트_산출물", "chroma_db", "showcase_slides",
    "doc_markdown", "db_docs", "server_storage", ".git", "__pycache__"
}

ADMIN_LEDGER_KEYWORDS = ["파일관리대장", "관리대장", "bf_파일및벡터db_관리대장", "vectordb_현황"]

# 1. 용도 (Usage)
TAXONOMY_USAGE = {
    "데이터센터": ["데이터센터", "idc", "서버실", "전산동", "부평 데이터센터", "코로케이션", "항온항습", "ups"],
    "공동주택": ["공동주택", "아파트", "lh", "주거", "오피스텔", "세대", "단위세대", "지하주차장", "분양"],
    "업무시설": ["업무시설", "사옥", "오피스", "키움", "파이낸스", "무교", "다동", "빌딩", "타워", "본사", "근린생활"],
    "공장": ["공장", "플랜트", "생산동", "제조", "클린룸", "반도체", "배터리", "라인", "산업시설", "바이오", "r&pd"],
    "창고시설": ["창고", "물류", "물류센터", "냉동창고", "상온창고", "랙구간", "도크", "하역장", "스태커", "어시장"]
}

# 2. 공법 (Method)
TAXONOMY_METHOD = {
    "탑다운": ["탑다운", "top-down", "top down", "역타", "역타공법", "prd", "rcd", "기둥박기", "흙막이 가시설"],
    "순타": ["순타", "순타공법", "오픈컷", "open cut", "순방향"],
    "PC": ["pc", "프리캐스트", "precast", "pc보", "pc기둥", "더블티", "할로우코어", "중공슬래브", "양중"],
    "모듈": ["모듈", "모듈러", "modular", "osc", "탈현장", "유닛", "공장제작", "포드"]
}

# 3. 공종 (Discipline)
TAXONOMY_DISCIPLINE = {
    "RC": ["rc", "철근콘크리트", "콘크리트", "철근", "거푸집", "골조", "슬래브", "벽체", "기초", "옹벽", "헌치"],
    "철골": ["철골", "steel", "h형강", "철골보", "철골기둥", "데크플레이트", "접합부", "트러스", "브레이싱", "앙카"],
    "단열공사": ["단열", "단열재", "압출법", "비드법", "그라스울", "미네랄울", "열관류율", "결로", "외단열", "내단열", "수장"],
    "방수공사": ["방수", "우레탄", "시트방수", "도막방수", "액체방수", "지붕방수", "외벽방수", "방근", "코킹", "실란트"],
    "MEP(기계/전기/설비)": ["mep", "설비", "기계", "전기", "배관", "덕트", "공조", "소방", "케이블트레이", "위생"],
    "토목/가시설": ["토목", "가시설", "흙막이", "띠장", "버팀보", "어스앙카", "cip", "슬러리월", "부대토목", "굴착"]
}

# 4. 업무 (Task)
TAXONOMY_TASK = {
    "설계검토": ["설계검토", "검토보고", "간섭", "clash", "도서상이", "오류", "누락", "시공성", "결과보고"],
    "수량": ["수량", "물량", "산출", "산출서", "비교표", "견적", "내역", "일위대가", "qto", "boq", "할증"],
    "공정시뮬레이션": ["공정", "시뮬레이션", "4d", "타임라이너", "timeliner", "공기", "공정표", "애니메이션", "영상"],
    "표준세팅/가이드": ["템플릿", "패밀리", "매개변수", "파라미터", "지침", "가이드북", "스펙북", "수행계획", "bep"]
}

MEDIA_EXT_MAP = {
    "PDF 장표/문서": {".pdf", ".pptx", ".docx"},
    "이미지/다이어그램": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"},
    "엑셀 산출서/체크리스트": {".xlsx", ".xls", ".csv"},
    "영상/시뮬레이션": {".mp4", ".webm", ".mov", ".avi"},
    "BIM 원본(.rvt/.rfa/.rte)": {".rvt", ".rfa", ".rte", ".nwc", ".nwd", ".dwg", ".txt"}
}

STOP_FOLDER_TOKENS = {
    "00", "01", "02", "03", "04", "05", "99", "bf", "archive", "bim", "file", "folder",
    "manual", "시스템인덱스", "프로젝트", "산출물", "설계검토", "보고서", "수량산출서",
    "내역비교표", "공정시뮬레이션", "시각자료", "영업쇼케이스", "아웃풋", "데이터센터",
    "공동주택", "업무시설", "공장", "창고시설", "표준문서", "지침", "검토보고서", "견적서",
    "내역서", "bim원본", "템플릿", "패밀리", "루트", "결과보고서", "실시설계", "준공",
    "납품", "비교표", "산출서", "체크리스트", "사전검토", "sm"
}

GUIDELINE_EXCLUDE_KEYWORDS = [
    "지침", "가이드", "guide", "매뉴얼", "manual", "요령", "수행계획", "bep",
    "스펙북", "기준서", "표준공종", "사전 검토 사항", "사전검토사항", "체크리스트 양식"
]

INHOUSE_OUTPUT_KEYWORDS = [
    "검토보고서", "결과보고서", "준공", "납품", "수량", "산출서", "비교표",
    "견적", "내역", "시뮬레이션", "간섭", "데이터센터", "파이낸스", "키움",
    "무교다동", "바이오사이언스", "어시장"
]


def is_inhouse_output_file(rel_folder, file_name, body_text=""):
    """
    발주처 지침서·매뉴얼·가이드북을 철저히 배제하고,
    당사가 실제 수행한 프로젝트 산출물(보고서, 수량산출서, 시뮬레이션 영상, 이미지)만 선별합니다.
    """
    f_low = file_name.lower()
    folder_low = rel_folder.lower()
    ext_low = os.path.splitext(f_low)[1]

    if ext_low in {".rte", ".txt", ".rfa"}:
        return False

    if any(ak in f_low for ak in ADMIN_LEDGER_KEYWORDS):
        return False

    # 파일명에 지침서/가이드북/매뉴얼 키워드가 있으면 발주처·표준 기준 문서이므로 산출물 아카이브에서 제외
    if any(gk in f_low for gk in GUIDELINE_EXCLUDE_KEYWORDS):
        if not any(ok in f_low for ok in ["검토보고서", "결과보고서", "준공", "수량", "비교표"]):
            return False

    # Manual_Folder 내부 문서 중 사내 검토보고서/결과보고서/수량비교표가 아닌 일반 지침서는 제외
    if "manual_folder" in folder_low or "지침" in folder_low or "매뉴얼" in folder_low:
        if not any(ok in f_low for ok in ["검토보고서", "결과보고서", "준공", "수량", "비교표", "산출서"]):
            return False

    if any(ok in f_low for ok in INHOUSE_OUTPUT_KEYWORDS):
        return True
    if ext_low in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".avi", ".xlsx", ".xls"}:
        return True

    head_low = (body_text or "")[:600].lower()
    if any(gk in head_low for gk in ["적용지침", "지침서", "가이드북", "업무수행계획서"]):
        if not any(ok in f_low for ok in ["검토보고서", "결과보고서"]):
            return False

    return True


def extract_project_name(rel_folder, file_name, body_text=""):
    combined_head = "{0} {1} {2}".format(rel_folder, file_name, (body_text or "")[:500])
    low_head = combined_head.lower()

    if "부평" in low_head and "데이터" in low_head:
        return "부평 데이터센터 프로젝트"
    if any(k in low_head for k in ["키움", "파이낸스스퀘어", "kfsq"]):
        return "키움 파이낸스스퀘어(사옥) 프로젝트"
    if "무교" in low_head or "다동" in low_head:
        return "무교다동 업무시설 프로젝트"
    if "바이오사이언스" in low_head or "r&pd" in low_head:
        return "SK바이오사이언스 R&PD 프로젝트"
    if "공동어시장" in low_head:
        return "부산공동어시장 현대화 프로젝트"

    parts = [p.strip() for p in re.split(r'[\\/]', rel_folder) if p.strip()]
    for p in reversed(parts):
        clean_p = re.sub(r'^\d+[_\-\s]*', '', p).strip()
        if clean_p and clean_p.lower() not in STOP_FOLDER_TOKENS and len(clean_p) >= 2:
            if not clean_p.endswith("프로젝트"):
                return "{0} 프로젝트".format(clean_p)
            return clean_p

    base = os.path.splitext(file_name)[0]
    base_no_prefix = re.sub(r'^(\[.*?\]\s*|20\d{2}|\d{6}|\d{4}|[aA]_|[sS]_|[cC]_|[mM]_)+[_\-\s]*', '', base)
    tokens = [t.strip() for t in re.split(r'[_\-]', base_no_prefix) if len(t.strip()) >= 2]
    for tok in tokens:
        if tok.lower() not in STOP_FOLDER_TOKENS:
            return "{0} 프로젝트".format(tok)

    return "사내 수행 프로젝트"


def analyze_multi_labels(text_content, rule_dict, fallback_list=None):
    t_low = (text_content or "").lower()
    matched = []
    for label, kw_list in rule_dict.items():
        hit_count = sum(1 for k in kw_list if k in t_low)
        if hit_count > 0:
            matched.append((hit_count, label))
    matched.sort(key=lambda x: x[0], reverse=True)
    res = [m[1] for m in matched]
    if res:
        return res
    return list(fallback_list) if fallback_list else ["일반/공통"]


def detect_media_type(ext_low):
    for m_label, ext_set in MEDIA_EXT_MAP.items():
        if ext_low in ext_set:
            return m_label
    return "기타 파일"


def create_recommended_server_folders():
    """
    사내 서버 3대 폴더 구조(00_시스템인덱스, 01_프로젝트_산출물[방안2 3분류], 99_bf_archive)를 생성합니다.
    """
    if not IS_SERVER_ACCESSIBLE:
        return
    sample_dirs = [
        "00_시스템인덱스/chroma_db",
        "00_시스템인덱스/doc_markdown",
        "01_프로젝트_산출물/01_설계검토_보고서",
        "01_프로젝트_산출물/02_수량산출서_내역비교표",
        "01_프로젝트_산출물/03_공정시뮬레이션_시각자료",
        "99_bf_archive/BIM_File_Folder",
        "99_bf_archive/Manual_Folder"
    ]
    for d in sample_dirs:
        try:
            os.makedirs(os.path.join(SERVER_STORAGE_PATH, d), exist_ok=True)
        except Exception:
            pass


def distribute_to_project_output_folder(item):
    """
    [방안 2 자동 분류 배포]
    99_bf_archive에서 판별된 사내 산출물(is_inhouse_output=True)을
    01_프로젝트_산출물 하위의 3대 형태별 폴더에 [프로젝트_용도_공법] 말머리를 붙여 자동 배포합니다.
    """
    if not IS_SERVER_ACCESSIBLE or not item.get("is_inhouse_output"):
        return item.get("server_path", "")

    src_path = item.get("server_path", "")
    if not src_path or not os.path.exists(src_path):
        return src_path

    fname = item.get("file_name", "")
    ext_low = os.path.splitext(fname)[1].lower()

    # 대용량 BIM 모델 원본(.rvt 등)은 99_bf_archive에만 유지하여 서버 용량 중복 방지
    if ext_low in {".rvt", ".rfa", ".rte", ".nwc", ".nwd", ".dwg"}:
        return src_path

    if ext_low in {".pdf", ".pptx", ".docx"}:
        sub_folder = "01_설계검토_보고서"
    elif ext_low in {".xlsx", ".xls", ".csv"}:
        sub_folder = "02_수량산출서_내역비교표"
    elif ext_low in {".mp4", ".webm", ".mov", ".avi", ".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        sub_folder = "03_공정시뮬레이션_시각자료"
    else:
        return src_path

    target_dir = os.path.join(PROJECT_OUTPUT_DIR, sub_folder)
    os.makedirs(target_dir, exist_ok=True)

    proj_short = item.get("project_name", "사내프로젝트").replace(" 프로젝트", "").strip()
    usage_first = (item.get("usages") or ["일반"])[0]
    method_first = (item.get("methods") or ["일반"])[0]

    clean_fname = re.sub(r'^\[.*?\]\s*', '', fname).strip()
    tagged_fname = "[{0}_{1}_{2}] {3}".format(proj_short, usage_first, method_first, clean_fname)
    tagged_fname = re.sub(r'[\\/:*?"<>|]', '_', tagged_fname)

    dst_path = os.path.join(target_dir, tagged_fname)
    try:
        if not os.path.exists(dst_path) or os.path.getmtime(src_path) > os.path.getmtime(dst_path):
            shutil.copy2(src_path, dst_path)
        return dst_path
    except Exception:
        return src_path


def save_file_analysis_markdown(item, page_blocks, slide_records):
    """
    [파일별 분석 내용 .md 자동 생성 및 관리]
    00_시스템인덱스/doc_markdown 폴더 안에 각 문서의 메타데이터, 장표 목차, 본문 분석 요약을
    마크다운(.md) 파일로 생성하여 관리합니다.
    """
    try:
        os.makedirs(DOC_MARKDOWN_DIR, exist_ok=True)
        fname = item.get("file_name", "document")
        safe_stem = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(fname)[0])[:60]
        md_path = os.path.join(DOC_MARKDOWN_DIR, "{0}_분석.md".format(safe_stem))

        md_lines = [
            "# 문서 분석 리포트: {0}".format(fname),
            "",
            "## 1. 기본 메타데이터 및 AI 자동 분류",
            "- **문서 역할:** {0}".format(item.get("asset_role", "")),
            "- **프로젝트명:** {0}".format(item.get("project_name", "")),
            "- **건물 용도:** {0}".format(item.get("usage", "")),
            "- **적용 공법:** {0}".format(item.get("method_type", "")),
            "- **관련 공종:** {0}".format(item.get("discipline", "")),
            "- **업무 구분:** {0}".format(item.get("task_type", "")),
            "- **파일 유형 / 크기:** {0} ({1} MB)".format(item.get("media_type", ""), item.get("size_mb", 0)),
            "- **사내 서버 원본 경로:** `{0}`".format(item.get("server_path", "")),
            ""
        ]

        if slide_records:
            md_lines.append("## 2. 장표(페이지)별 핵심 목차 및 태그")
            md_lines.append("| 페이지 | 장표 제목 | 용도 | 공법 | 공종 | 업무 |")
            md_lines.append("| :---: | :--- | :--- | :--- | :--- | :--- |")
            for s in slide_records[:50]:
                md_lines.append("| {0}p | {1} | {2} | {3} | {4} | {5} |".format(
                    s.get("page_num", 1),
                    str(s.get("slide_title", "")).replace("|", " "),
                    ", ".join(s.get("usages", [])),
                    ", ".join(s.get("methods", [])),
                    ", ".join(s.get("disciplines", [])),
                    ", ".join(s.get("tasks", []))
                ))
            md_lines.append("")

        md_lines.append("## 3. 본문 추출 텍스트 요약 (상위 15개 섹션)")
        for loc_label, blk_txt in page_blocks[:15]:
            preview_txt = (blk_txt or "").strip()[:600]
            if preview_txt:
                md_lines.append("### [{0}]".format(loc_label))
                md_lines.append(preview_txt)
                md_lines.append("")

        with open(md_path, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(md_lines))
        return md_path
    except Exception:
        return ""


def extract_text_and_slides_from_file(file_path, rel_folder=""):
    """
    [물리적 낱장 PDF 생성 없는 초고속 장표 인덱싱]
    하드디스크에 수백 개의 낱장 PDF 파일을 미리 쪼개서 저장하지 않고,
    원본 파일 경로(parent_path)와 페이지 번호(page_num), 본문 태그만 JSON으로 인덱싱합니다.
    (실제 페이지 추출은 영업 자료 열람/다운로드 시점에 메모리에서 0.02초 만에 실시간 수행)
    """
    ext = os.path.splitext(file_path)[1].lower()
    fname = os.path.basename(file_path)
    page_blocks = []
    slide_records = []

    try:
        if ext == ".pdf":
            import pypdf
            reader = pypdf.PdfReader(file_path)
            total_pages = len(reader.pages)

            first_pages_txt = ""
            for p_i in range(min(3, total_pages)):
                first_pages_txt += "\n" + ((reader.pages[p_i].extract_text() or "").strip())

            is_output = is_inhouse_output_file(rel_folder, fname, first_pages_txt)
            proj_name = extract_project_name(rel_folder, fname, first_pages_txt)

            doc_ctx = "{0} {1} {2}".format(rel_folder, fname, first_pages_txt)
            doc_usages = analyze_multi_labels(doc_ctx, TAXONOMY_USAGE, ["일반건축"])
            doc_methods = analyze_multi_labels(doc_ctx, TAXONOMY_METHOD, ["일반공법"])
            doc_discs = analyze_multi_labels(doc_ctx, TAXONOMY_DISCIPLINE, ["RC"])
            doc_tasks = analyze_multi_labels(doc_ctx, TAXONOMY_TASK, ["설계검토"])

            for idx, page in enumerate(reader.pages):
                page_num = idx + 1
                txt = (page.extract_text() or "").strip()
                if txt:
                    page_blocks.append(("p.{0}".format(page_num), txt))

                if is_output and page_num <= 80 and len(txt) >= 10:
                    lines = [ln.strip() for ln in txt.splitlines() if len(ln.strip()) >= 2]
                    slide_title = lines[0][:60] if lines else "{0}p 장표".format(page_num)
                    page_ctx = "{0} {1}".format(fname, txt)

                    slide_records.append({
                        "project_name": proj_name,
                        "parent_file": fname,
                        "parent_path": file_path,
                        "page_num": page_num,
                        "total_pages": total_pages,
                        "slide_title": slide_title,
                        "slide_text": txt[:700],
                        "slide_pdf_path": file_path,
                        "is_inhouse_output": True,
                        "usages": analyze_multi_labels(page_ctx, TAXONOMY_USAGE, doc_usages),
                        "methods": analyze_multi_labels(page_ctx, TAXONOMY_METHOD, doc_methods),
                        "disciplines": analyze_multi_labels(page_ctx, TAXONOMY_DISCIPLINE, doc_discs),
                        "tasks": analyze_multi_labels(page_ctx, TAXONOMY_TASK, doc_tasks)
                    })

        elif ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(file_path)
            total_slides = len(prs.slides)
            all_slide_txts = []
            for idx, slide in enumerate(prs.slides):
                page_num = idx + 1
                s_txt = [shape.text_frame.text.strip() for shape in slide.shapes if shape.has_text_frame]
                joined = "\n".join([t for t in s_txt if t])
                if joined:
                    page_blocks.append(("Slide {0}".format(page_num), joined))
                    all_slide_txts.append((page_num, joined))

            head_txt = "\n".join([x[1] for x in all_slide_txts[:3]])
            is_output = is_inhouse_output_file(rel_folder, fname, head_txt)
            proj_name = extract_project_name(rel_folder, fname, head_txt)
            doc_ctx = "{0} {1} {2}".format(rel_folder, fname, head_txt)
            doc_usages = analyze_multi_labels(doc_ctx, TAXONOMY_USAGE, ["일반건축"])
            doc_methods = analyze_multi_labels(doc_ctx, TAXONOMY_METHOD, ["일반공법"])
            doc_discs = analyze_multi_labels(doc_ctx, TAXONOMY_DISCIPLINE, ["RC"])
            doc_tasks = analyze_multi_labels(doc_ctx, TAXONOMY_TASK, ["설계검토"])

            if is_output:
                for page_num, joined in all_slide_txts:
                    lines = [ln.strip() for ln in joined.splitlines() if len(ln.strip()) >= 2]
                    slide_title = lines[0][:60] if lines else "Slide {0}".format(page_num)
                    page_ctx = "{0} {1}".format(fname, joined)
                    slide_records.append({
                        "project_name": proj_name,
                        "parent_file": fname,
                        "parent_path": file_path,
                        "page_num": page_num,
                        "total_pages": total_slides,
                        "slide_title": slide_title,
                        "slide_text": joined[:700],
                        "slide_pdf_path": file_path,
                        "is_inhouse_output": True,
                        "usages": analyze_multi_labels(page_ctx, TAXONOMY_USAGE, doc_usages),
                        "methods": analyze_multi_labels(page_ctx, TAXONOMY_METHOD, doc_methods),
                        "disciplines": analyze_multi_labels(page_ctx, TAXONOMY_DISCIPLINE, doc_discs),
                        "tasks": analyze_multi_labels(page_ctx, TAXONOMY_TASK, doc_tasks)
                    })

        elif ext == ".docx":
            import docx
            doc = docx.Document(file_path)
            full_p = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            for t in doc.tables:
                for row in t.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        full_p.append(" | ".join(cells))
            if full_p:
                page_blocks.append(("본문", "\n".join(full_p)))

        elif ext in {".xlsx", ".xls"}:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, data_only=True)
            for sname in wb.sheetnames:
                ws = wb[sname]
                s_lines = []
                for r in list(ws.iter_rows(values_only=True))[:150]:
                    if any(c is not None for c in r):
                        s_lines.append(" | ".join(["" if c is None else str(c) for c in r]))
                if s_lines:
                    page_blocks.append(("시트:{0}".format(sname), "\n".join(s_lines)))

        elif ext in {".txt", ".csv", ".md"}:
            for enc in ("utf-8-sig", "utf-16", "cp949"):
                try:
                    with open(file_path, "r", encoding=enc) as f:
                        txt = f.read().strip()
                    if txt:
                        page_blocks.append(("전체", txt))
                    break
                except Exception:
                    continue
    except Exception as e:
        print("[본문 분석 경고] {0}: {1}".format(fname, e))

    full_body_text = "\n".join([b[1] for b in page_blocks])
    return page_blocks, full_body_text, slide_records


def ingest_server_docs_to_chroma(catalog_pair_list, max_new_chunks=180):
    if not GOOGLE_API_KEY:
        return 0
    try:
        from backend.bim_agent import get_collection
        col = get_collection()
        if col is None:
            return 0
        existing_data = col.get(include=[])
        existing_ids = set(existing_data.get("ids", []))
    except Exception:
        return 0

    splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)
    embeddings = None
    added_count = 0

    for item, page_blocks in catalog_pair_list:
        if added_count >= max_new_chunks or not page_blocks:
            continue
        fname = item.get("file_name", "")
        fpath = item.get("server_path", "")
        ext = os.path.splitext(fname)[1].lower()
        file_hash = hashlib.md5(fname.encode("utf-8")).hexdigest()[:10]

        chunk_counter = 0
        for loc_label, blk_text in page_blocks:
            sub_chunks = splitter.split_text(blk_text)
            for sc in sub_chunks:
                cid = "srv_{0}_{1}".format(file_hash, chunk_counter)
                chunk_counter += 1
                if cid in existing_ids:
                    continue
                if embeddings is None:
                    embeddings = GoogleGenerativeAIEmbeddings(
                        model="models/gemini-embedding-001",
                        google_api_key=GOOGLE_API_KEY
                    )
                try:
                    vec = embeddings.embed_query(sc)
                    meta = {
                        "file_name": fname,
                        "domain_group": item.get("project_name", "공통"),
                        "doc_category": item.get("task_type", "설계검토"),
                        "folder_path": item.get("rel_folder", "사내서버"),
                        "file_ext": ext.lstrip("."),
                        "location": loc_label,
                        "chunk_type": "사내서버아웃풋" if item.get("is_inhouse_output") else "발주처및표준지침",
                        "setup_topic": "{0}_{1}_{2}".format(
                            item.get("usage", ""), item.get("method_type", ""), item.get("discipline", "")
                        ),
                        "drive_link": fpath
                    }
                    col.upsert(ids=[cid], embeddings=[vec], documents=[sc], metadatas=[meta])
                    existing_ids.add(cid)
                    added_count += 1
                    if added_count >= max_new_chunks:
                        break
                except Exception as e:
                    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                        return added_count
                    break
    return added_count


def export_excel_management_ledger(all_catalog_items):
    """
    사내 서버 '00_시스템인덱스/BF_파일및벡터DB_관리대장.xlsx' 파일에
    1탭: 01_서버파일_분류대장
    2탭: 02_VectorDB_학습현황
    3탭: 03_AI검색_이력로그 (기존 로그 보존)
    3개 시트를 자동 생성·갱신합니다.
    """
    vdb_summary_rows = []
    try:
        from backend.bim_agent import get_vector_db_status
        summary_list, _ = get_vector_db_status()
        for info in summary_list:
            vdb_summary_rows.append([
                info.get("domain_group", "공통"),
                info.get("doc_category", "일반"),
                info.get("folder_path", "사내서버"),
                info.get("file_name", ""),
                info.get("file_ext", ""),
                info.get("chunk_count", 0),
                info.get("total_chars", 0),
                info.get("avg_chars", 0),
                info.get("drive_link", "")
            ])
    except Exception as e:
        print("[벡터 DB 현황 조회 경고]:", e)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        # 기존 03_AI검색_이력로그 시트 데이터 보존
        existing_logs = []
        if os.path.exists(EXCEL_LEDGER_PATH):
            try:
                old_wb = openpyxl.load_workbook(EXCEL_LEDGER_PATH, data_only=True)
                if "03_AI검색_이력로그" in old_wb.sheetnames:
                    old_ws3 = old_wb["03_AI검색_이력로그"]
                    for r in list(old_ws3.iter_rows(values_only=True))[1:]:
                        if any(r):
                            existing_logs.append(list(r))
            except Exception:
                pass

        wb = openpyxl.Workbook()

        # 시트 1: 01_서버파일_분류대장
        ws1 = wb.active
        ws1.title = "01_서버파일_분류대장"
        headers1 = [
            "구분(역할)", "프로젝트명", "파일명", "용도", "공법",
            "공종", "업무", "파일유형", "추출장표수", "크기(MB)",
            "수정일시", "내용요약", "사내서버_원본경로", "배포된_산출물경로", "분석_MD파일경로"
        ]
        ws1.append(headers1)
        for it in all_catalog_items:
            ws1.append([
                it.get("asset_role", ""),
                it.get("project_name", ""),
                it.get("file_name", ""),
                it.get("usage", ""),
                it.get("method_type", ""),
                it.get("discipline", ""),
                it.get("task_type", ""),
                it.get("media_type", ""),
                it.get("slide_count", 0),
                it.get("size_mb", 0.0),
                it.get("modified_at", ""),
                it.get("description", ""),
                it.get("server_path", ""),
                it.get("distributed_path", ""),
                it.get("md_report_path", "")
            ])

        # 시트 2: 02_VectorDB_학습현황
        ws2 = wb.create_sheet(title="02_VectorDB_학습현황")
        headers2 = [
            "프로젝트/도메인", "문서분류(업무)", "사내서버_폴더", "문서명(파일명)",
            "확장자", "적재된_청크(Chunk)수", "총_학습글자수", "청크당_평균글자수", "사내서버_원본경로"
        ]
        ws2.append(headers2)
        for v_row in vdb_summary_rows:
            ws2.append(v_row)

        # 시트 3: 03_AI검색_이력로그
        ws3 = wb.create_sheet(title="03_AI검색_이력로그")
        headers3 = ["검색일시", "접속채널", "사용자_질문내용", "AI_핵심답변요약", "참조된_사내파일목록"]
        ws3.append(headers3)
        for log_r in existing_logs:
            ws3.append(log_r)

        header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)
        for ws in (ws1, ws2, ws3):
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

        wb.save(EXCEL_LEDGER_PATH)
        print("[관리대장 엑셀 갱신 완료]:", EXCEL_LEDGER_PATH)
    except Exception as e:
        print("[관리대장 엑셀 저장 경고]:", e)

    return vdb_summary_rows


def _read_existing_json_list(json_path):
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def scan_hybrid_server_and_update(sync_sheets=True, ingest_new_docs=True):
    """
    [99_bf_archive 마스터 스캔 -> 01_프로젝트_산출물(3분류) 배포 & 00_시스템인덱스 종합 생성]
    """
    global SERVER_STORAGE_PATH, SYSTEM_INDEX_DIR, PROJECT_OUTPUT_DIR, ARCHIVE_MASTER_DIR, DOC_MARKDOWN_DIR, CHROMA_PATH, CATALOG_JSON_PATH, SLIDES_JSON_PATH, EXCEL_LEDGER_PATH, IS_SERVER_ACCESSIBLE
    (
        SERVER_STORAGE_PATH,
        SYSTEM_INDEX_DIR,
        PROJECT_OUTPUT_DIR,
        ARCHIVE_MASTER_DIR,
        DOC_MARKDOWN_DIR,
        CHROMA_PATH,
        CATALOG_JSON_PATH,
        SLIDES_JSON_PATH,
        EXCEL_LEDGER_PATH,
        IS_SERVER_ACCESSIBLE
    ) = resolve_storage_paths()

    if not IS_SERVER_ACCESSIBLE:
        print("[외부망/오프라인 모드 감지] 사내 서버({0})에 연결할 수 없어 기존 인덱스 데이터를 보호합니다.".format(SERVER_STORAGE_PATH))
        existing_cat = [it for it in _read_existing_json_list(CATALOG_JSON_PATH) if it.get("is_inhouse_output", True)]
        existing_slides = [
            s for s in _read_existing_json_list(SLIDES_JSON_PATH)
            if s.get("is_inhouse_output", True)
            and not any(gk in s.get("parent_file", "").lower() for gk in GUIDELINE_EXCLUDE_KEYWORDS)
        ]
        return existing_cat, existing_slides, False

    create_recommended_server_folders()

    # 99_bf_archive 폴더에 파일이 있으면 99_bf_archive만 스캔하여 01_프로젝트_산출물과의 중복 스캔을 원천 방지
    scan_target_root = ARCHIVE_MASTER_DIR if os.path.exists(ARCHIVE_MASTER_DIR) and os.listdir(ARCHIVE_MASTER_DIR) else SERVER_STORAGE_PATH
    print("[BF 파일 분석 & 동기화 시작] 마스터 스캔 경로:", scan_target_root)

    all_catalog_items = []
    showcase_output_items = []
    catalog_pairs = []
    all_slides = []

    for root, dirs, files in os.walk(scan_target_root):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SYSTEM_DIRS and not d.startswith(".")]

        for fname in files:
            if fname.startswith(("~$", ".")) or fname.endswith(".json"):
                continue
            if any(ak in fname.lower() for ak in ADMIN_LEDGER_KEYWORDS):
                continue

            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, scan_target_root)
            rel_folder = os.path.dirname(rel_path) or "루트"
            ext_low = os.path.splitext(fname)[1].lower()

            try:
                stat_info = os.stat(full_path)
                size_mb = round(stat_info.st_size / (1024 * 1024), 2)
                mtime_str = datetime.datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                size_mb = 0.0
                mtime_str = ""

            page_blocks, body_text, slide_records = extract_text_and_slides_from_file(full_path, rel_folder)
            is_output = is_inhouse_output_file(rel_folder, fname, body_text[:1000])

            if is_output and slide_records:
                all_slides.extend(slide_records)

            proj_name = extract_project_name(rel_folder, fname, body_text)
            analysis_corpus = "{0} {1} {2}".format(rel_folder, fname, body_text[:25000])
            usages = analyze_multi_labels(analysis_corpus, TAXONOMY_USAGE, ["일반건축"])
            methods = analyze_multi_labels(analysis_corpus, TAXONOMY_METHOD, ["일반공법"])
            disciplines = analyze_multi_labels(analysis_corpus, TAXONOMY_DISCIPLINE, ["RC"])
            tasks = analyze_multi_labels(analysis_corpus, TAXONOMY_TASK, ["설계검토" if is_output else "표준세팅/가이드"])
            media_type = detect_media_type(ext_low)

            content_summary = body_text[:180].replace("\n", " ").strip() if body_text else "시각/모델 원본 자산"

            item = {
                "project_name": proj_name,
                "file_name": fname,
                "is_inhouse_output": is_output,
                "asset_role": "사내아웃풋" if is_output else "지침/표준문서",
                "usages": usages,
                "methods": methods,
                "disciplines": disciplines,
                "tasks": tasks,
                "usage": ", ".join(usages),
                "method_type": ", ".join(methods),
                "discipline": ", ".join(disciplines),
                "task_type": ", ".join(tasks),
                "media_type": media_type,
                "slide_count": len(slide_records) if is_output else 0,
                "category": "[{0}] 용도:{1} | 공법:{2} | 공종:{3} | 업무:{4}".format(
                    proj_name, "/".join(usages), "/".join(methods), "/".join(disciplines), "/".join(tasks)
                ),
                "software_version": ext_low.lstrip(".").upper() or "FILE",
                "size_mb": size_mb,
                "modified_at": mtime_str,
                "server_path": full_path,
                "rel_folder": rel_folder,
                "content_preview": content_summary,
                "description": "[{0} | {1}] {2}".format(
                    "사내아웃풋" if is_output else "기준지침", proj_name, content_summary[:80]
                ),
                "drive_link": full_path
            }

            # 1) 방안 2 기준 01_프로젝트_산출물(3분류) 폴더로 사내 산출물 자동 정리·배포
            dist_path = distribute_to_project_output_folder(item)
            item["distributed_path"] = dist_path

            # 2) 00_시스템인덱스/doc_markdown 내 파일별 분석 리포트(.md) 자동 생성
            md_report_path = save_file_analysis_markdown(item, page_blocks, slide_records)
            item["md_report_path"] = md_report_path

            all_catalog_items.append(item)
            catalog_pairs.append((item, page_blocks))
            if is_output:
                showcase_output_items.append(item)

    with open(CATALOG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(showcase_output_items, f, indent=2, ensure_ascii=False)

    with open(SLIDES_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(all_slides, f, indent=2, ensure_ascii=False)

    if ingest_new_docs and catalog_pairs:
        ingest_server_docs_to_chroma(catalog_pairs)

    vdb_summary_rows = export_excel_management_ledger(all_catalog_items)

    sheet_updated = False
    if sync_sheets and CRED_PATH and os.path.exists(CRED_PATH) and SHEET_ID:
        try:
            gc = gspread.service_account(filename=CRED_PATH)
            sh = gc.open_by_key(SHEET_ID)

            ws1 = sh.get_worksheet(0)
            headers1 = [
                "asset_role", "project_name", "file_name", "usage", "method_type",
                "discipline", "task_type", "media_type", "slide_count", "size_mb",
                "modified_at", "description", "drive_link"
            ]
            rows1 = [headers1]
            for it in all_catalog_items:
                rows1.append([
                    it["asset_role"], it["project_name"], it["file_name"], it["usage"],
                    it["method_type"], it["discipline"], it["task_type"], it["media_type"],
                    it["slide_count"], it["size_mb"], it["modified_at"],
                    it["description"], it["drive_link"]
                ])
            if len(rows1) > 1:
                ws1.clear()
                ws1.update(range_name="A1", values=rows1)

            if vdb_summary_rows:
                try:
                    ws2 = sh.get_worksheet(1)
                except Exception:
                    ws2 = sh.add_worksheet(title="02_VectorDB_학습현황", rows="1000", cols="12")
                if ws2:
                    v_headers = [
                        "도메인/프로젝트", "문서분류", "사내서버_폴더", "문서명",
                        "확장자", "청크(Chunk)수", "총글자수", "평균글자수", "사내서버_경로"
                    ]
                    ws2.clear()
                    ws2.update(range_name="A1", values=[v_headers] + vdb_summary_rows)

            sheet_updated = True
        except Exception as e:
            print("[시트 갱신 안내] 로컬/서버 인덱스 생성은 완료되었으나 구글 시트 반영은 생략됨:", e)

    return showcase_output_items, all_slides, sheet_updated


def load_showcase_catalog():
    if os.path.exists(CATALOG_JSON_PATH):
        data = _read_existing_json_list(CATALOG_JSON_PATH)
        return [it for it in data if it.get("is_inhouse_output", True)]
    if not IS_SERVER_ACCESSIBLE:
        return []
    items, _, _ = scan_hybrid_server_and_update(sync_sheets=False, ingest_new_docs=False)
    return items


def load_showcase_slides():
    if os.path.exists(SLIDES_JSON_PATH):
        data = _read_existing_json_list(SLIDES_JSON_PATH)
        return [
            s for s in data
            if s.get("is_inhouse_output", True)
            and not any(gk in s.get("parent_file", "").lower() for gk in GUIDELINE_EXCLUDE_KEYWORDS)
        ]
    return []


if __name__ == "__main__":
    print("BF 파일 분석 & 동기화 실행 중...")
    items, slides, ok = scan_hybrid_server_and_update(sync_sheets=True, ingest_new_docs=True)
    print("[완료] 사내 서버 루트: {0}".format(SERVER_STORAGE_PATH))
    print("[완료] 시스템 인덱스: {0}".format(SYSTEM_INDEX_DIR))
    print("[완료] 사내 산출물 파일: {0}건 | 인덱싱된 장표: {1}장 | 시트 동기화: {2}".format(len(items), len(slides), ok))
