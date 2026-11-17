import os
import io
import re
import sys
import json
import time
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
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)


def clean_env_val(key, default=""):
    val = os.getenv(key, default)
    if not val:
        return default
    return val.strip().strip('"').strip("'").strip()


def read_raw_server_path_from_env(key="SERVER_STORAGE_PATH", default=""):
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
    except Exception:
        return False


GOOGLE_API_KEY = clean_env_val("GOOGLE_API_KEY")
CRED_PATH = clean_env_val("GOOGLE_APPLICATION_CREDENTIALS")
if CRED_PATH and not os.path.isabs(CRED_PATH):
    CRED_PATH = os.path.join(BASE_DIR, CRED_PATH)

SHEET_ID = clean_env_val("SPREADSHEET_ID")
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def resolve_storage_paths():
    raw_srv = read_raw_server_path_from_env("SERVER_STORAGE_PATH")
    srv_ok = check_server_accessible(raw_srv) if raw_srv else False
    if raw_srv and srv_ok:
        srv_root = raw_srv
        idx_dir = os.path.join(srv_root, "00_시스템인덱스")
        proj_out_dir = os.path.join(srv_root, "01_프로젝트_산출물")
        archive_dir = os.path.join(srv_root, "99_bf_archive")
        md_dir = os.path.join(idx_dir, "doc_markdown")
        c_path = read_raw_server_path_from_env("CHROMA_DB_PATH") or os.path.join(idx_dir, "chroma_db")
        cat_json = os.path.join(idx_dir, "showcase_catalog.json")
        sld_json = os.path.join(idx_dir, "showcase_slides_index.json")
        excel_ledger = os.path.join(idx_dir, "BF_파일및벡터DB_관리대장.xlsx")
        manifest_json = os.path.join(idx_dir, "sync_manifest_cache.json")
        os.makedirs(idx_dir, exist_ok=True)
        os.makedirs(md_dir, exist_ok=True)
        return srv_root, idx_dir, proj_out_dir, archive_dir, md_dir, c_path, cat_json, sld_json, excel_ledger, manifest_json, True
    else:
        temp_root = os.path.join(tempfile.gettempdir(), "BIM_AI_Offline_Cache")
        os.makedirs(temp_root, exist_ok=True)
        srv_root = raw_srv or temp_root
        proj_out_dir = os.path.join(temp_root, "01_프로젝트_산출물")
        archive_dir = os.path.join(temp_root, "99_bf_archive")
        md_dir = os.path.join(temp_root, "doc_markdown")
        c_path = os.path.join(temp_root, "chroma_db")
        cat_json = os.path.join(temp_root, "showcase_catalog.json")
        sld_json = os.path.join(temp_root, "showcase_slides_index.json")
        excel_ledger = os.path.join(temp_root, "BF_파일및벡터DB_관리대장.xlsx")
        manifest_json = os.path.join(temp_root, "sync_manifest_cache.json")
        os.makedirs(md_dir, exist_ok=True)
        return srv_root, temp_root, proj_out_dir, archive_dir, md_dir, c_path, cat_json, sld_json, excel_ledger, manifest_json, False


(
    SERVER_STORAGE_PATH,
    SYSTEM_INDEX_DIR,
    PROJECT_OUTPUT_DIR,
    ARCHIVE_ROOT_DIR,
    DOC_MARKDOWN_DIR,
    CHROMA_PATH,
    CATALOG_JSON_PATH,
    SLIDES_JSON_PATH,
    EXCEL_LEDGER_PATH,
    MANIFEST_CACHE_PATH,
    IS_SERVER_ACCESSIBLE
) = resolve_storage_paths()

ADMIN_LEDGER_KEYWORDS = ["파일관리대장", "관리대장", "bf_파일및벡터db_관리대장", "vectordb_현황"]

# [보완 1] 윈도우 탐색기 썸네일 캐시(Thumbs.db) 및 시스템 임시 파일 원천 제외 목록
EXCLUDED_SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store", "folder.jpg", "albumartsmall.jpg"}
EXCLUDED_SYSTEM_EXTS = {".db", ".ini", ".tmp", ".temp", ".lnk", ".log", ".bak", ".pyc", ".json", ".md"}

MANUAL_GUIDELINE_FOLDERS = {
    "01_대외bim지침서",
    "02_bf_표준및세팅",
    "03_뷰어_협업가이드",
    "09_자재마감및스펙북"
}

MANUAL_OUTPUT_FOLDERS = {
    "04_bim검토보고서",
    "05_bim검토리스트",
    "06_bim수량산출서",
    "07_bim견적서",
    "08_bim기술제안및공정시뮬레이션"
}

TAXONOMY_USAGE = {
    "데이터센터": ["데이터센터", "idc", "서버실", "전산동", "부평 데이터센터", "구로 데이터센터", "원창동데이터센터", "코로케이션", "항온항습", "ups"],
    "공동주택": ["공동주택", "아파트", "lh", "주거", "오피스텔", "세대", "단위세대", "지하주차장", "분양", "주택재개발", "정비사업"],
    "업무시설": ["업무시설", "사옥", "오피스", "키움", "파이낸스", "무교", "다동", "빌딩", "타워", "본사", "근린생활"],
    "공장": ["공장", "플랜트", "생산동", "제조", "클린룸", "반도체", "배터리", "라인", "산업시설", "바이오", "r&pd", "정수장"],
    "창고시설": ["창고", "물류", "물류센터", "냉동창고", "상온창고", "비축창고", "랙구간", "도크", "하역장", "스태커", "어시장"]
}

TAXONOMY_METHOD = {
    "탑다운": ["탑다운", "top-down", "top down", "역타", "역타공법", "prd", "rcd", "기둥박기", "흙막이 가시설"],
    "순타": ["순타", "순타공법", "오픈컷", "open cut", "순방향"],
    "PC": ["pc", "프리캐스트", "precast", "pc보", "pc기둥", "더블티", "할로우코어", "중공슬래브", "양중"],
    "모듈": ["모듈", "모듈러", "modular", "osc", "탈현장", "유닛", "공장제작", "포드"]
}

TAXONOMY_DISCIPLINE = {
    "RC": ["rc", "철근콘크리트", "콘크리트", "철근", "거푸집", "골조", "슬래브", "벽체", "기초", "옹벽", "헌치", "버림콘"],
    "철골": ["철골", "steel", "h형강", "철골보", "철골기둥", "데크플레이트", "접합부", "트러스", "브레이싱", "앙카"],
    "단열공사": ["단열", "단열재", "압출법", "비드법", "그라스울", "미네랄울", "열관류율", "결로", "외단열", "내단열", "수장"],
    "방수공사": ["방수", "우레탄", "시트방수", "도막방수", "액체방수", "지붕방수", "외벽방수", "방근", "코킹", "실란트"],
    "MEP(기계/전기/설비)": ["mep", "설비", "기계", "전기", "배관", "덕트", "공조", "소방", "케이블트레이", "위생"],
    "토목/가시설": ["토목", "가시설", "흙막이", "띠장", "버팀보", "어스앙카", "cip", "슬러리월", "부대토목", "굴착"]
}

TAXONOMY_TASK = {
    "설계검토": ["설계검토", "검토보고", "검토리스트", "체크리스트", "간섭", "clash", "도서상이", "오류", "누락", "시공성", "결과보고"],
    "수량": ["수량", "물량", "산출", "산출서", "비교표", "견적", "견적서", "내역", "일위대가", "qto", "boq", "할증"],
    "공정시뮬레이션": ["공정", "시뮬레이션", "기술제안", "4d", "타임라이너", "timeliner", "공기", "공정표", "애니메이션", "영상"],
    "표준세팅/가이드": ["템플릿", "패밀리", "매개변수", "파라미터", "지침", "가이드", "스펙북", "자재마감", "수행계획", "bep"]
}

MEDIA_EXT_MAP = {
    "PDF 장표/문서": {".pdf", ".pptx", ".docx"},
    "이미지/다이어그램": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"},
    "엑셀 산출서/체크리스트": {".xlsx", ".xls", ".csv"},
    "영상/시뮬레이션": {".mp4", ".webm", ".mov", ".avi"},
    "BIM 원본(.rvt/.rfa/.rte)": {".rvt", ".rfa", ".rte", ".nwc", ".nwd", ".dwg", ".txt"}
}

# [보완 2] Manual_Folder, BIM_File_Folder 등이 프로젝트명으로 잘못 추출되지 않도록 완벽 차단
STOP_FOLDER_TOKENS = {
    "00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "99",
    "bf", "archive", "bim", "file", "folder", "manual",
    "manual_folder", "bim_file_folder", "99_bf_archive", "bf_ai_assistant", "99 bf_ai_assistant",
    "비아이엠팩토리", "bimfactory", "01_bimfactory", "bf-db", "project", "data",
    "대외bim지침서", "bf_표준및세팅", "표준및세팅", "뷰어_협업가이드", "협업가이드",
    "bim검토보고서", "bim검토리스트", "검토리스트", "bim수량산출서", "bim견적서",
    "bim기술제안및공정시뮬레이션", "기술제안및공정시뮬레이션", "자재마감및스펙북",
    "시스템인덱스", "프로젝트", "산출물", "설계검토", "보고서", "수량산출서", "내역비교표",
    "공정시뮬레이션", "시각자료", "루트", "결과보고서", "실시설계", "준공", "납품",
    "비교표", "산출서", "견적서", "내역서", "지침서", "체크리스트", "사전검토", "sm", "skep"
}

GUIDELINE_EXCLUDE_KEYWORDS = [
    "지침", "가이드", "guide", "매뉴얼", "manual", "요령", "수행계획", "bep",
    "스펙북", "기준서", "표준공종", "사전 검토 사항", "사전검토사항", "체크리스트 양식"
]

INHOUSE_OUTPUT_KEYWORDS = [
    "검토보고서", "결과보고서", "검토리스트", "준공", "납품", "수량", "산출서", "비교표",
    "견적", "내역", "시뮬레이션", "기술제안", "간섭", "데이터센터", "파이낸스", "키움",
    "무교다동", "바이오사이언스", "어시장"
]

INVALID_LEGACY_PROJECT_NAMES = {
    "Manual_Folder 프로젝트",
    "BIM_File_Folder 프로젝트",
    "99_bf_archive 프로젝트",
    "bf_archive 프로젝트"
}


def is_inhouse_output_file(rel_folder, file_name, body_text=""):
    f_low = file_name.lower()
    folder_low = rel_folder.lower()
    ext_low = os.path.splitext(f_low)[1]

    if ext_low in {".rte", ".txt", ".rfa"}:
        return False
    if any(ak in f_low for ak in ADMIN_LEDGER_KEYWORDS):
        return False

    for g_folder in MANUAL_GUIDELINE_FOLDERS:
        if g_folder in folder_low:
            return False

    for o_folder in MANUAL_OUTPUT_FOLDERS:
        if o_folder in folder_low:
            if any(k in f_low for k in ["적용지침", "가이드북", "사전 검토 사항", "사전검토사항", "양식"]):
                return False
            return True

    if any(gk in f_low for gk in GUIDELINE_EXCLUDE_KEYWORDS):
        if not any(ok in f_low for ok in ["검토보고서", "결과보고서", "준공", "수량", "비교표", "견적", "기술제안"]):
            return False

    head_low = (body_text or "")[:600].lower()
    if any(gk in head_low for gk in ["적용지침", "지침서", "가이드북", "업무수행계획서"]):
        if not any(ok in f_low for ok in ["검토보고서", "결과보고서"]):
            return False

    return True


def extract_project_name(rel_folder, file_name, body_text=""):
    """
    [정밀 프로젝트명 추출기]
    Manual_Folder, BIM_File_Folder 등 상위 시스템 폴더명을 철저히 배제하고,
    파일명과 본문에서 실제 수행 프로젝트명(구로 데이터센터, 원창동 데이터센터, 활성탄 비축창고 등)을 정확히 추출합니다.
    """
    combined_head = "{0} {1} {2}".format(rel_folder, file_name, (body_text or "")[:500])
    low_head = combined_head.lower()

    if "구로" in low_head and "데이터" in low_head:
        return "구로 데이터센터 프로젝트"
    if "부평" in low_head and "데이터" in low_head:
        return "부평 데이터센터 프로젝트"
    if "원창동" in low_head and "데이터" in low_head:
        return "원창동 데이터센터 프로젝트"
    if "수자원공사" in low_head or "활성탄" in low_head:
        return "한국수자원공사 활성탄 비축창고 프로젝트"
    if "구리" in low_head and "수택" in low_head:
        return "구리 수택지구 주택재개발 프로젝트"
    if any(k in low_head for k in ["키움", "파이낸스스퀘어", "kfsq"]):
        return "키움 파이낸스스퀘어(사옥) 프로젝트"
    if "무교" in low_head or "다동" in low_head:
        return "무교다동 업무시설 프로젝트"
    if "바이오사이언스" in low_head or "r&pd" in low_head:
        return "SK바이오사이언스 R&PD 프로젝트"
    if "공동어시장" in low_head:
        return "부산공동어시장 현대화 프로젝트"

    # 1순위: 파일명에서 의미 있는 프로젝트 토큰 추출 (폴더명보다 파일명이 훨씬 정확함)
    base = os.path.splitext(file_name)[0]
    base_clean = re.sub(r"^\[[^\]]+\]\s*", "", base)
    base_no_prefix = re.sub(r'^(20\d{2}|\d{6}|\d{4}|[aA]_|[sS]_|[cC]_|[mM]_)+[_\-\s]*', '', base_clean)
    tokens = [t.strip() for t in re.split(r'[_\-\s\[\]\(\)]', base_no_prefix) if len(t.strip()) >= 2]
    for tok in tokens:
        tok_low = tok.lower()
        if tok_low not in STOP_FOLDER_TOKENS and not re.match(r'^\d+$', tok_low):
            return "{0} 프로젝트".format(tok)

    # 2순위: 하위 폴더명에서 추출 (단, Manual_Folder, BIM_File_Folder 및 9분류 폴더는 완전 배제)
    parts = [p.strip() for p in re.split(r'[\\/]', rel_folder) if p.strip()]
    for p in reversed(parts):
        p_low = p.lower()
        if p_low in MANUAL_GUIDELINE_FOLDERS or p_low in MANUAL_OUTPUT_FOLDERS or p_low in STOP_FOLDER_TOKENS:
            continue
        clean_p = re.sub(r'^\d+[_\-\s]*', '', p).strip()
        if clean_p and clean_p.lower() not in STOP_FOLDER_TOKENS and len(clean_p) >= 2:
            if not clean_p.endswith("프로젝트"):
                return "{0} 프로젝트".format(clean_p)
            return clean_p

    return "사내 수행 프로젝트"


def analyze_multi_labels(text_content, rule_dict, fallback_list=None, rel_folder=""):
    t_low = "{0} {1}".format(rel_folder or "", text_content or "").lower()
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
    if not IS_SERVER_ACCESSIBLE:
        return
    sample_dirs = [
        "00_시스템인덱스/chroma_db",
        "00_시스템인덱스/doc_markdown",
        "01_프로젝트_산출물/01_설계검토_보고서",
        "01_프로젝트_산출물/02_수량산출서_내역비교표",
        "01_프로젝트_산출물/03_공정시뮬레이션_시각자료",
        "99_bf_archive/BIM_File_Folder",
        "99_bf_archive/Manual_Folder/01_대외BIM지침서",
        "99_bf_archive/Manual_Folder/02_BF_표준및세팅",
        "99_bf_archive/Manual_Folder/03_뷰어_협업가이드",
        "99_bf_archive/Manual_Folder/04_BIM검토보고서",
        "99_bf_archive/Manual_Folder/05_BIM검토리스트",
        "99_bf_archive/Manual_Folder/06_BIM수량산출서",
        "99_bf_archive/Manual_Folder/07_BIM견적서",
        "99_bf_archive/Manual_Folder/08_BIM기술제안및공정시뮬레이션",
        "99_bf_archive/Manual_Folder/09_자재마감및스펙북"
    ]
    for d in sample_dirs:
        try:
            os.makedirs(os.path.join(SERVER_STORAGE_PATH, d), exist_ok=True)
        except Exception:
            pass


def distribute_to_project_output_folder(item, src_path):
    if not IS_SERVER_ACCESSIBLE or not item.get("is_inhouse_output"):
        return src_path

    fname = item.get("file_name", "")
    rel_low = item.get("rel_folder", "").lower()
    ext_low = os.path.splitext(fname)[1].lower()
    if ext_low in {".rvt", ".rfa", ".rte", ".nwd", ".nwc"}:
        return src_path

    if "06_bim수량산출서" in rel_low or "07_bim견적서" in rel_low:
        sub_cat = "02_수량산출서_내역비교표"
    elif "08_bim기술제안및공정시뮬레이션" in rel_low:
        sub_cat = "03_공정시뮬레이션_시각자료"
    elif "04_bim검토보고서" in rel_low or "05_bim검토리스트" in rel_low:
        sub_cat = "01_설계검토_보고서"
    elif ext_low in {".xlsx", ".xls", ".csv"}:
        sub_cat = "02_수량산출서_내역비교표"
    elif ext_low in {".mp4", ".webm", ".mov", ".avi", ".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        sub_cat = "03_공정시뮬레이션_시각자료"
    else:
        sub_cat = "01_설계검토_보고서"

    dest_dir = os.path.join(PROJECT_OUTPUT_DIR, sub_cat)
    os.makedirs(dest_dir, exist_ok=True)

    proj_short = item.get("project_name", "공통").replace(" 프로젝트", "").strip()
    usage_short = (item.get("usages") or ["일반"])[0]
    method_short = (item.get("methods") or ["일반"])[0]
    clean_fname = re.sub(r"^\[[^\]]+\]\s*", "", fname)
    tagged_fname = "[{0}_{1}_{2}] {3}".format(proj_short, usage_short, method_short, clean_fname)
    tagged_fname = re.sub(r'[\\/:*?"<>|]', '_', tagged_fname)

    # 만약 이전에 "[Manual_Folder_...]" 이름으로 잘못 복사된 파일이 있다면 자동 삭제
    for old_f in os.listdir(dest_dir):
        if old_f.endswith(clean_fname) and old_f.startswith("[Manual_Folder_") and old_f != tagged_fname:
            try:
                os.remove(os.path.join(dest_dir, old_f))
            except Exception:
                pass

    dest_path = os.path.join(dest_dir, tagged_fname)
    try:
        if not os.path.exists(dest_path) or os.path.getmtime(src_path) > os.path.getmtime(dest_path):
            shutil.copy2(src_path, dest_path)
        return dest_path
    except Exception:
        return src_path


def get_expected_md_path(fname):
    safe_stem = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(fname)[0])[:60]
    return os.path.join(DOC_MARKDOWN_DIR, "{0}_분석.md".format(safe_stem))


def save_per_file_markdown_summary(item, page_blocks, slide_records):
    try:
        os.makedirs(DOC_MARKDOWN_DIR, exist_ok=True)
        fname = item.get("file_name", "문서")
        md_path = get_expected_md_path(fname)

        md_lines = [
            "# 문서 분석 리포트: {0}".format(fname),
            "",
            "## 1. 자동 분류 메타데이터",
            "- **문서 구분(Role):** {0}".format(item.get("asset_role", "")),
            "- **보관 폴더 분류:** {0}".format(item.get("rel_folder", "")),
            "- **프로젝트명:** {0}".format(item.get("project_name", "")),
            "- **건물 용도:** {0}".format(item.get("usage", "")),
            "- **적용 공법:** {0}".format(item.get("method_type", "")),
            "- **관련 공종:** {0}".format(item.get("discipline", "")),
            "- **수행 업무:** {0}".format(item.get("task_type", "")),
            "- **파일 유형 / 크기:** {0} ({1} MB)".format(item.get("media_type", ""), item.get("size_mb", 0)),
            "- **원본 보관 경로:** `{0}`".format(item.get("server_path", "")),
            ""
        ]

        if slide_records:
            md_lines.append("## 2. 사내 산출물 장표(슬라이드) 목차 ({0}장)".format(len(slide_records)))
            for s in slide_records[:40]:
                md_lines.append("- **[{0}p] {1}** (용도:{2} | 공법:{3} | 공종:{4})".format(
                    s["page_num"], s["slide_title"],
                    "/".join(s.get("usages", [])),
                    "/".join(s.get("methods", [])),
                    "/".join(s.get("disciplines", []))
                ))
            md_lines.append("")

        if page_blocks:
            md_lines.append("## 3. 추출된 본문 핵심 텍스트")
            for loc_label, blk_txt in page_blocks[:25]:
                md_lines.append("### [{0}]".format(loc_label))
                md_lines.append(blk_txt[:1000])
                md_lines.append("")

        with open(md_path, "w", encoding="utf-8-sig") as mf:
            mf.write("\n".join(md_lines))
        return md_path
    except Exception:
        return ""


def infer_default_task_from_folder(rel_folder, is_output):
    rel_low = (rel_folder or "").lower()
    if "04_bim검토보고서" in rel_low or "05_bim검토리스트" in rel_low:
        return ["설계검토"]
    if "06_bim수량산출서" in rel_low or "07_bim견적서" in rel_low:
        return ["수량"]
    if "08_bim기술제안및공정시뮬레이션" in rel_low:
        return ["공정시뮬레이션"]
    if any(g in rel_low for g in MANUAL_GUIDELINE_FOLDERS):
        return ["표준세팅/가이드"]
    return ["설계검토" if is_output else "표준세팅/가이드"]


def extract_text_and_slides_from_file(file_path, rel_folder=""):
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
            default_tasks = infer_default_task_from_folder(rel_folder, is_output)

            doc_ctx = "{0} {1} {2}".format(rel_folder, fname, first_pages_txt)
            doc_usages = analyze_multi_labels(doc_ctx, TAXONOMY_USAGE, ["일반건축"], rel_folder)
            doc_methods = analyze_multi_labels(doc_ctx, TAXONOMY_METHOD, ["일반공법"], rel_folder)
            doc_discs = analyze_multi_labels(doc_ctx, TAXONOMY_DISCIPLINE, ["RC"], rel_folder)
            doc_tasks = analyze_multi_labels(doc_ctx, TAXONOMY_TASK, default_tasks, rel_folder)

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
                        "usages": analyze_multi_labels(page_ctx, TAXONOMY_USAGE, doc_usages, rel_folder),
                        "methods": analyze_multi_labels(page_ctx, TAXONOMY_METHOD, doc_methods, rel_folder),
                        "disciplines": analyze_multi_labels(page_ctx, TAXONOMY_DISCIPLINE, doc_discs, rel_folder),
                        "tasks": analyze_multi_labels(page_ctx, TAXONOMY_TASK, doc_tasks, rel_folder)
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
            default_tasks = infer_default_task_from_folder(rel_folder, is_output)
            doc_ctx = "{0} {1} {2}".format(rel_folder, fname, head_txt)
            doc_usages = analyze_multi_labels(doc_ctx, TAXONOMY_USAGE, ["일반건축"], rel_folder)
            doc_methods = analyze_multi_labels(doc_ctx, TAXONOMY_METHOD, ["일반공법"], rel_folder)
            doc_discs = analyze_multi_labels(doc_ctx, TAXONOMY_DISCIPLINE, ["RC"], rel_folder)
            doc_tasks = analyze_multi_labels(doc_ctx, TAXONOMY_TASK, default_tasks, rel_folder)

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
                        "usages": analyze_multi_labels(page_ctx, TAXONOMY_USAGE, doc_usages, rel_folder),
                        "methods": analyze_multi_labels(page_ctx, TAXONOMY_METHOD, doc_methods, rel_folder),
                        "disciplines": analyze_multi_labels(page_ctx, TAXONOMY_DISCIPLINE, doc_discs, rel_folder),
                        "tasks": analyze_multi_labels(page_ctx, TAXONOMY_TASK, doc_tasks, rel_folder)
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

        elif ext in {".txt", ".csv"}:
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


def _build_local_fallback_vector(text_str, dim=3072):
    vec = [0.0] * dim
    tokens = re.findall(r'[a-zA-Z0-9가-힣]{2,}', (text_str or "").lower())
    if not tokens:
        vec[0] = 1.0
        return vec
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def ingest_and_summarize_chunks(new_catalog_pairs, cached_vdb_rows, active_file_names):
    """
    [완전 증분(Incremental) ChromaDB 적재 및 2번 시트 통계 통합기]
    - 변경되지 않은 기존 문서는 캐시된 청크 통계(cached_vdb_rows)를 그대로 사용하여 0.001초 만에 통과합니다.
    - 새로 추가·수정된 문서(new_catalog_pairs)만 청크 분할 및 배치 임베딩을 수행합니다.
    - 삭제된 파일이나 Thumbs.db 같은 시스템 파일이 기존 ChromaDB에 남아 있다면 자동으로 정리합니다.
    """
    splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)

    col = None
    existing_ids = set()
    try:
        from backend.bim_agent import get_collection
        col = get_collection()
        if col is not None:
            existing_data = col.get(include=["metadatas"])
            existing_ids = set(existing_data.get("ids", []))
            # 삭제된 파일이나 .md/Thumbs.db 유령 청크가 DB에 남아 있으면 자동 제거
            stale_ids = []
            for cid, meta in zip(existing_data.get("ids", []), existing_data.get("metadatas", [])):
                if meta:
                    m_fn = meta.get("file_name", "")
                    if m_fn and (m_fn not in active_file_names or m_fn.lower() in EXCLUDED_SYSTEM_FILES or m_fn.lower().endswith(".md")):
                        stale_ids.append(cid)
            if stale_ids:
                try:
                    col.delete(ids=stale_ids)
                    existing_ids -= set(stale_ids)
                except Exception:
                    pass
    except Exception as e:
        print("[ChromaDB 컬렉션 조회 안내]:", e)

    embeddings = None
    if GOOGLE_API_KEY and new_catalog_pairs:
        try:
            embeddings = GoogleGenerativeAIEmbeddings(
                model="models/gemini-embedding-001",
                google_api_key=GOOGLE_API_KEY
            )
        except Exception:
            embeddings = None

    quota_exhausted = False
    vdb_summary_rows = list(cached_vdb_rows)
    pending_ids = []
    pending_docs = []
    pending_metas = []
    new_computed_map = {}

    for item, page_blocks in new_catalog_pairs:
        if not page_blocks:
            continue

        fname = item.get("file_name", "")
        fpath = item.get("server_path", "")
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        proj_name = item.get("project_name", "공통")
        task_cat = item.get("task_type", "설계검토")
        rel_folder = item.get("rel_folder", "사내서버")
        file_hash = hashlib.md5(fname.encode("utf-8")).hexdigest()[:10]

        doc_chunk_cnt = 0
        doc_total_chars = 0

        for loc_label, blk_text in page_blocks:
            sub_chunks = splitter.split_text(blk_text)
            for sc in sub_chunks:
                sc_clean = sc.strip()
                if not sc_clean:
                    continue
                cid = "srv_{0}_{1}".format(file_hash, doc_chunk_cnt)
                doc_chunk_cnt += 1
                doc_total_chars += len(sc_clean)

                if col is not None:
                    meta = {
                        "file_name": fname,
                        "domain_group": proj_name,
                        "doc_category": task_cat,
                        "folder_path": rel_folder,
                        "file_ext": ext,
                        "location": loc_label,
                        "chunk_type": "사내서버아웃풋" if item.get("is_inhouse_output") else "발주처및표준지침",
                        "setup_topic": "{0}_{1}_{2}".format(
                            item.get("usage", ""), item.get("method_type", ""), item.get("discipline", "")
                        ),
                        "drive_link": fpath
                    }
                    # 기존 ID가 있더라도 프로젝트명이나 폴더가 바뀐 재분석 대상이면 upsert로 최신 메타데이터 갱신
                    pending_ids.append(cid)
                    pending_docs.append(sc_clean)
                    pending_metas.append(meta)

        if doc_chunk_cnt > 0:
            avg_chars = round(doc_total_chars / doc_chunk_cnt)
            row_val = [
                proj_name,
                task_cat,
                rel_folder,
                fname,
                ext,
                doc_chunk_cnt,
                doc_total_chars,
                avg_chars,
                fpath
            ]
            vdb_summary_rows.append(row_val)
            new_computed_map[fpath] = row_val

    if col is not None and pending_ids:
        batch_size = 50
        for i in range(0, len(pending_ids), batch_size):
            b_ids = pending_ids[i:i + batch_size]
            b_docs = pending_docs[i:i + batch_size]
            b_metas = pending_metas[i:i + batch_size]

            # 이미 벡터가 들어 있는 청크인지 확인하여 불필요한 API 재호출 방지
            need_embed = any(bid not in existing_ids for bid in b_ids)
            b_vecs = None

            if need_embed and embeddings is not None and not quota_exhausted:
                try:
                    b_vecs = embeddings.embed_documents(b_docs)
                except Exception as e:
                    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) or "PerDay" in str(e):
                        quota_exhausted = True
                        print("[임베딩 API 할당량 초과 감지] 로컬 고속 해시 벡터로 전환하여 누락 없이 적재합니다.")
                    b_vecs = None

            if b_vecs is None:
                b_vecs = [_build_local_fallback_vector(doc_txt, dim=3072) for doc_txt in b_docs]

            try:
                col.upsert(ids=b_ids, embeddings=b_vecs, documents=b_docs, metadatas=b_metas)
            except Exception as e_up:
                print("[ChromaDB 배치 적재 경고]:", e_up)

    vdb_summary_rows.sort(key=lambda x: (str(x[2]), str(x[3])))
    return vdb_summary_rows, new_computed_map


def export_excel_management_ledger(all_catalog_items, vdb_summary_rows):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        existing_qa_rows = []
        if os.path.exists(EXCEL_LEDGER_PATH):
            try:
                old_wb = openpyxl.load_workbook(EXCEL_LEDGER_PATH, data_only=True)
                if "03_AI검색_이력로그" in old_wb.sheetnames:
                    old_ws3 = old_wb["03_AI검색_이력로그"]
                    for idx, r in enumerate(old_ws3.iter_rows(values_only=True)):
                        if idx > 0 and any(r):
                            existing_qa_rows.append(list(r))
            except Exception:
                pass

        wb = openpyxl.Workbook()

        ws1 = wb.active
        ws1.title = "01_서버파일_분류대장"
        headers1 = [
            "구분(역할)", "보관폴더(9분류)", "프로젝트명", "파일명", "용도", "공법",
            "공종", "업무", "파일유형", "추출장표수", "크기(MB)",
            "수정일시", "내용요약", "원본경로(99_bf_archive)", "산출물배포경로(01_프로젝트_산출물)"
        ]
        ws1.append(headers1)
        for it in all_catalog_items:
            ws1.append([
                it.get("asset_role", ""),
                it.get("rel_folder", ""),
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
                it.get("distributed_path", "")
            ])

        ws2 = wb.create_sheet(title="02_VectorDB_학습현황")
        headers2 = [
            "프로젝트/도메인", "문서분류(업무)", "사내서버_폴더", "문서명(파일명)",
            "확장자", "적재된_청크(Chunk)수", "총_학습글자수", "청크당_평균글자수", "사내서버_원본경로"
        ]
        ws2.append(headers2)
        for v_row in vdb_summary_rows:
            ws2.append(v_row)

        ws3 = wb.create_sheet(title="03_AI검색_이력로그")
        headers3 = ["검색일시", "접속채널", "사용자_질문", "AI_답변_핵심요약", "참조된_사내파일"]
        ws3.append(headers3)
        for q_row in existing_qa_rows:
            ws3.append(q_row)

        header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)
        for ws in (ws1, ws2, ws3):
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

        wb.save(EXCEL_LEDGER_PATH)
        print("[통합관리대장 엑셀 갱신 완료] 1시트({0}건) / 2시트 VectorDB({1}건): {2}".format(
            len(all_catalog_items), len(vdb_summary_rows), EXCEL_LEDGER_PATH
        ))
    except Exception as e:
        print("[관리대장 엑셀 저장 경고] 엑셀 파일이 열려 있다면 닫고 다시 실행하세요:", e)


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


def _load_manifest_cache():
    if os.path.exists(MANIFEST_CACHE_PATH):
        try:
            with open(MANIFEST_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_manifest_cache(cache_dict):
    try:
        with open(MANIFEST_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache_dict, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def scan_hybrid_server_and_update(sync_sheets=True, ingest_new_docs=True):
    """
    [완전 증분 스캔(Incremental Scan) 엔진 탑재 동기화 파이프라인]
    - sync_manifest_cache.json을 통해 이전 스캔 시점의 (수정일시 + 파일크기 + 보관폴더)를 대조합니다.
    - 변경이 없는 기존 파일은 무거운 PDF/PPTX/XLSX 본문 파싱을 건너뛰고 0.001초 만에 캐시를 재사용합니다.
    - 단, 새로 추가·수정된 파일이거나 이전에 'Manual_Folder 프로젝트'로 잘못 분류되었던 파일은 즉시 재분석하여 자동 교정합니다.
    """
    global SERVER_STORAGE_PATH, SYSTEM_INDEX_DIR, PROJECT_OUTPUT_DIR, ARCHIVE_ROOT_DIR, DOC_MARKDOWN_DIR, CHROMA_PATH, CATALOG_JSON_PATH, SLIDES_JSON_PATH, EXCEL_LEDGER_PATH, MANIFEST_CACHE_PATH, IS_SERVER_ACCESSIBLE
    (
        SERVER_STORAGE_PATH,
        SYSTEM_INDEX_DIR,
        PROJECT_OUTPUT_DIR,
        ARCHIVE_ROOT_DIR,
        DOC_MARKDOWN_DIR,
        CHROMA_PATH,
        CATALOG_JSON_PATH,
        SLIDES_JSON_PATH,
        EXCEL_LEDGER_PATH,
        MANIFEST_CACHE_PATH,
        IS_SERVER_ACCESSIBLE
    ) = resolve_storage_paths()

    if not IS_SERVER_ACCESSIBLE:
        print("[외부망/오프라인 모드 감지] 사내 서버에 연결할 수 없어 기존 인덱스 데이터를 보호합니다.")
        existing_cat = [it for it in _read_existing_json_list(CATALOG_JSON_PATH) if it.get("is_inhouse_output", True)]
        existing_slides = [
            s for s in _read_existing_json_list(SLIDES_JSON_PATH)
            if s.get("is_inhouse_output", True)
            and not any(gk in s.get("parent_file", "").lower() for gk in GUIDELINE_EXCLUDE_KEYWORDS)
        ]
        return existing_cat, existing_slides, False

    create_recommended_server_folders()

    scan_target_root = ARCHIVE_ROOT_DIR if (os.path.exists(ARCHIVE_ROOT_DIR) and any(os.scandir(ARCHIVE_ROOT_DIR))) else SERVER_STORAGE_PATH
    print("[BF 파일 분석 & 동기화 시작 (완전 증분 모드)] 마스터 스캔 경로:", scan_target_root)

    old_manifest = _load_manifest_cache()
    new_manifest = {}

    all_catalog_items = []
    showcase_output_items = []
    new_catalog_pairs = []
    cached_vdb_rows = []
    all_slides = []
    active_file_names = set()

    reused_cnt = 0
    analyzed_cnt = 0

    excluded_dirs = {"00_시스템인덱스", "01_프로젝트_산출물", "chroma_db", "showcase_slides", "doc_markdown", ".git", "__pycache__"}

    for root, dirs, files in os.walk(scan_target_root):
        dirs[:] = [d for d in dirs if d not in excluded_dirs and not d.startswith(".")]

        for fname in files:
            f_low = fname.lower()
            ext_low = os.path.splitext(f_low)[1]

            # [보완 1] Thumbs.db, desktop.ini, 임시 파일($~), 시스템 확장자(.db, .ini, .md, .json) 완벽 제외
            if fname.startswith(("~$", ".")):
                continue
            if f_low in EXCLUDED_SYSTEM_FILES or ext_low in EXCLUDED_SYSTEM_EXTS:
                continue
            if any(ak in f_low for ak in ADMIN_LEDGER_KEYWORDS):
                continue

            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, SERVER_STORAGE_PATH)
            rel_folder = os.path.dirname(rel_path) or "99_bf_archive"

            try:
                stat = os.stat(full_path)
                size_mb = round(stat.st_size / (1024 * 1024), 2)
                mtime_str = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                size_mb = 0.0
                mtime_str = ""

            active_file_names.add(fname)
            expected_md = get_expected_md_path(fname)

            # [완전 증분 스캔 판별]
            # 이전 매니페스트 캐시에 파일이 있고, 수정일시/크기/폴더가 일치하며,
            # 프로젝트명이 'Manual_Folder 프로젝트' 같은 구버전 오분류가 아니고, _분석.md 파일이 존재하면 본문 파싱 생략!
            cached_entry = old_manifest.get(full_path)
            can_reuse = False
            if cached_entry:
                c_item = cached_entry.get("item", {})
                c_proj = c_item.get("project_name", "")
                if (
                    cached_entry.get("mtime_str") == mtime_str
                    and cached_entry.get("size_mb") == size_mb
                    and cached_entry.get("rel_folder") == rel_folder
                    and c_proj not in INVALID_LEGACY_PROJECT_NAMES
                    and os.path.exists(expected_md)
                ):
                    can_reuse = True

            if can_reuse:
                reused_cnt += 1
                item = cached_entry["item"]
                slide_records = cached_entry.get("slide_records", [])
                vdb_row = cached_entry.get("vdb_row")

                dist_path = distribute_to_project_output_folder(item, full_path)
                item["distributed_path"] = dist_path if dist_path != full_path else ""

                if item.get("is_inhouse_output") and slide_records:
                    all_slides.extend(slide_records)
                if vdb_row:
                    cached_vdb_rows.append(vdb_row)

                all_catalog_items.append(item)
                if item.get("is_inhouse_output"):
                    showcase_output_items.append(item)

                new_manifest[full_path] = {
                    "mtime_str": mtime_str,
                    "size_mb": size_mb,
                    "rel_folder": rel_folder,
                    "item": item,
                    "slide_records": slide_records,
                    "vdb_row": vdb_row
                }
                continue

            # 신규 파일이거나 수정된 파일(또는 구버전 오분류 교정 대상)만 본문 정밀 분석 수행
            analyzed_cnt += 1
            page_blocks, body_text, slide_records = extract_text_and_slides_from_file(full_path, rel_folder)
            is_output = is_inhouse_output_file(rel_folder, fname, body_text[:1000])

            if is_output and slide_records:
                all_slides.extend(slide_records)

            proj_name = extract_project_name(rel_folder, fname, body_text)
            default_tasks = infer_default_task_from_folder(rel_folder, is_output)
            analysis_corpus = "{0} {1} {2}".format(rel_folder, fname, body_text[:25000])
            usages = analyze_multi_labels(analysis_corpus, TAXONOMY_USAGE, ["일반건축"], rel_folder)
            methods = analyze_multi_labels(analysis_corpus, TAXONOMY_METHOD, ["일반공법"], rel_folder)
            disciplines = analyze_multi_labels(analysis_corpus, TAXONOMY_DISCIPLINE, ["RC"], rel_folder)
            tasks = analyze_multi_labels(analysis_corpus, TAXONOMY_TASK, default_tasks, rel_folder)
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

            dist_path = distribute_to_project_output_folder(item, full_path)
            item["distributed_path"] = dist_path if dist_path != full_path else ""

            save_per_file_markdown_summary(item, page_blocks, slide_records)

            all_catalog_items.append(item)
            new_catalog_pairs.append((item, page_blocks))
            if is_output:
                showcase_output_items.append(item)

            new_manifest[full_path] = {
                "mtime_str": mtime_str,
                "size_mb": size_mb,
                "rel_folder": rel_folder,
                "item": item,
                "slide_records": slide_records,
                "vdb_row": None
            }

    # doc_markdown 폴더에서 삭제된 원본이나 Thumbs.db의 잔여 _분석.md 파일 자동 청소
    if os.path.exists(DOC_MARKDOWN_DIR):
        expected_md_files = {os.path.basename(get_expected_md_path(fn)) for fn in active_file_names}
        for md_fn in os.listdir(DOC_MARKDOWN_DIR):
            if md_fn.lower().endswith(".md") and md_fn not in expected_md_files:
                try:
                    os.remove(os.path.join(DOC_MARKDOWN_DIR, md_fn))
                except Exception:
                    pass

    with open(CATALOG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(showcase_output_items, f, indent=2, ensure_ascii=False)

    with open(SLIDES_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(all_slides, f, indent=2, ensure_ascii=False)

    # 신규/변경 문서만 청크 분할 및 ChromaDB 적재 후 기존 캐시 통계와 병합
    vdb_summary_rows, new_vdb_map = ingest_and_summarize_chunks(
        new_catalog_pairs, cached_vdb_rows, active_file_names
    )

    for fpath, v_row in new_vdb_map.items():
        if fpath in new_manifest:
            new_manifest[fpath]["vdb_row"] = v_row

    _save_manifest_cache(new_manifest)

    print("[증분 분석 요약] 기존 캐시 재사용(초고속 통과): {0}건 / 신규·변경 정밀 분석: {1}건".format(reused_cnt, analyzed_cnt))

    export_excel_management_ledger(all_catalog_items, vdb_summary_rows)

    sheet_updated = False
    if sync_sheets and CRED_PATH and os.path.exists(CRED_PATH) and SHEET_ID:
        try:
            gc = gspread.service_account(filename=CRED_PATH)
            sh = gc.open_by_key(SHEET_ID)

            ws = sh.get_worksheet(0)
            headers = [
                "asset_role", "rel_folder", "project_name", "file_name", "usage", "method_type",
                "discipline", "task_type", "media_type", "slide_count", "size_mb",
                "modified_at", "description", "drive_link"
            ]
            rows = [headers]
            for it in all_catalog_items:
                rows.append([
                    it["asset_role"], it["rel_folder"], it["project_name"], it["file_name"], it["usage"],
                    it["method_type"], it["discipline"], it["task_type"], it["media_type"],
                    it["slide_count"], it["size_mb"], it["modified_at"],
                    it["description"], it["drive_link"]
                ])
            if len(rows) > 1:
                ws.clear()
                ws.update(range_name="A1", values=rows)

            if vdb_summary_rows:
                try:
                    ws2 = sh.get_worksheet(1)
                except Exception:
                    ws2 = sh.add_worksheet(title="VectorDB_학습현황", rows="1000", cols="12")
                if ws2:
                    v_headers = [
                        "도메인/프로젝트", "문서분류", "사내서버_폴더", "문서명",
                        "확장자", "청크(Chunk)수", "총글자수", "평균글자수", "사내서버_경로"
                    ]
                    ws2.clear()
                    ws2.update(range_name="A1", values=[v_headers] + vdb_summary_rows)

            sheet_updated = True
        except Exception as e:
            print("[시트 갱신 안내] 로컬 인덱스 및 관리대장 엑셀 생성은 완료되었으나 시트 반영은 생략됨:", e)

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
    print("BF 파일 분석 & 동기화 실행 중 (완전 증분 스캔 + Thumbs.db 제외 + 프로젝트명 정밀 보정)...")
    items, slides, ok = scan_hybrid_server_and_update(sync_sheets=True, ingest_new_docs=True)
    print("[완료] 사내 산출물 파일: {0}개 | 실시간 추출 장표: {1}장 | 시트 반영: {2}".format(len(items), len(slides), ok))
