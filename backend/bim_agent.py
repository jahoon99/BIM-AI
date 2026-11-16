import os
import io
import re
import stat
import time
import uuid
import json
import shutil
import datetime
import tempfile
import urllib.request
import urllib.error
from dotenv import load_dotenv
import chromadb
import gspread
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)


def _clean_env(key, default=""):
    val = os.getenv(key, default)
    return val.strip().strip('"').strip("'").strip() if val else default


def _read_raw_server_path(key="SERVER_STORAGE_PATH", default=""):
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
        raw_val = _clean_env(key, default)
    if not raw_val:
        return default
    norm_val = raw_val.replace("/", "\\")
    if norm_val.startswith("\\") and not norm_val.startswith("\\\\"):
        norm_val = "\\" + norm_val
    elif re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\\", norm_val):
        norm_val = "\\\\" + norm_val
    return norm_val.rstrip("\\")


def _check_server_accessible(path_str):
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


GOOGLE_API_KEY = _clean_env("GOOGLE_API_KEY")
CRED_PATH = _clean_env("GOOGLE_APPLICATION_CREDENTIALS")
if CRED_PATH and not os.path.isabs(CRED_PATH):
    CRED_PATH = os.path.join(BASE_DIR, CRED_PATH)

SHEET_ID = _clean_env("SPREADSHEET_ID")
RAW_SERVER_PATH = _read_raw_server_path("SERVER_STORAGE_PATH")
RAW_CHROMA_PATH = _read_raw_server_path("CHROMA_DB_PATH")

os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

if RAW_SERVER_PATH and _check_server_accessible(RAW_SERVER_PATH):
    SERVER_STORAGE_PATH = RAW_SERVER_PATH
    SYSTEM_INDEX_DIR = os.path.join(SERVER_STORAGE_PATH, "00_시스템인덱스")
    ARCHIVE_MASTER_DIR = os.path.join(SERVER_STORAGE_PATH, "99_bf_archive")
    os.makedirs(SYSTEM_INDEX_DIR, exist_ok=True)
    MASTER_CHROMA_PATH = RAW_CHROMA_PATH or os.path.join(SYSTEM_INDEX_DIR, "chroma_db")
    CATALOG_JSON_PATH = os.path.join(SYSTEM_INDEX_DIR, "showcase_catalog.json")
    SLIDES_JSON_PATH = os.path.join(SYSTEM_INDEX_DIR, "showcase_slides_index.json")
    EXCEL_LEDGER_PATH = os.path.join(SYSTEM_INDEX_DIR, "BF_파일및벡터DB_관리대장.xlsx")
else:
    FALLBACK_TEMP_ROOT = os.path.join(tempfile.gettempdir(), "BIM_AI_Offline_Cache")
    SYSTEM_INDEX_DIR = os.path.join(FALLBACK_TEMP_ROOT, "00_시스템인덱스")
    ARCHIVE_MASTER_DIR = os.path.join(FALLBACK_TEMP_ROOT, "99_bf_archive")
    os.makedirs(SYSTEM_INDEX_DIR, exist_ok=True)
    SERVER_STORAGE_PATH = RAW_SERVER_PATH or FALLBACK_TEMP_ROOT
    MASTER_CHROMA_PATH = os.path.join(SYSTEM_INDEX_DIR, "chroma_db")
    CATALOG_JSON_PATH = os.path.join(SYSTEM_INDEX_DIR, "showcase_catalog.json")
    SLIDES_JSON_PATH = os.path.join(SYSTEM_INDEX_DIR, "showcase_slides_index.json")
    EXCEL_LEDGER_PATH = os.path.join(SYSTEM_INDEX_DIR, "BF_파일및벡터DB_관리대장.xlsx")

BRIDGE_DIR = os.path.join(BASE_DIR, "bridge")
os.makedirs(BRIDGE_DIR, exist_ok=True)
LIVE_SCRIPT_PATH = os.path.join(BRIDGE_DIR, "live_setup.py")
DYN_PLAYER_PATH = os.path.join(BRIDGE_DIR, "BF_AI세팅실행기.dyn")

REVIT_BASE_URL = "http://" + "127.0.0.1:48884/bim-ai-codex"
REVIT_PING_URL = REVIT_BASE_URL + "/ping"
REVIT_EXEC_URL = REVIT_BASE_URL + "/execute"
REVIT_INSPECT_URL = REVIT_BASE_URL + "/inspect"

DEFAULT_9_CATEGORIES = [
    "설계검토",
    "수량",
    "공정시뮬레이션",
    "표준세팅/가이드",
    "대외/발주처 BIM 지침",
    "사내 표준/세팅기준",
    "스펙북/자재기준",
    "BIM 검토보고서",
    "수량산출서"
]

BIM_SYNONYM_DICT = {
    "다룩스": ["dalux", "뷰어", "가이드북", "모바일"],
    "dalux": ["다룩스", "뷰어", "가이드북"],
    "acc": ["autodesk", "construction", "cloud", "뷰어", "가이드북"],
    "트림블": ["trimble", "connect", "트림블커넥트", "뷰어"],
    "매개변수": ["파라미터", "parameter", "공유매개변수", "속성", "guid"],
    "파라미터": ["매개변수", "parameter", "공유매개변수", "속성"],
    "수량": ["물량", "산출", "산출서", "비교표", "qto", "boq", "단열", "콘크리트", "골조"],
    "물량": ["수량", "산출", "산출서", "비교표", "qto", "boq"],
    "견적": ["내역", "단가", "일위대가", "원가", "금액"],
    "검토": ["체크리스트", "검토리스트", "검토보고", "간섭", "clash", "오류", "누락", "상이"],
    "간섭": ["clash", "충돌", "중첩", "검토보고", "오류"],
    "템플릿": ["template", "rte", "세팅", "표준", "초기세팅"],
    "패밀리": ["family", "rfa", "라이브러리", "객체"],
    "조달청": ["발주처", "지침", "표준공사코드", "공간분류코드"],
    "lh": ["공동주택", "실무요령", "지침"],
    "탑다운": ["top-down", "역타", "역타공법", "가시설", "흙막이"],
    "순타": ["순타공법", "오픈컷"],
    "레벨": ["level", "층고", "높이"],
    "그리드": ["grid", "축선", "열선"],
    "단열": ["단열재", "수장공사", "비교표", "물량"],
    "골조": ["콘크리트", "철근", "거푸집", "구조", "수량", "rc"]
}

STOPWORDS_KO = {
    "신입사원인데", "신입사원", "우리", "회사", "회사에서", "어떻게", "알려줘", "설명해줘",
    "찾아줘", "비교해줘", "정리해줘", "만들어줘", "무엇", "어떤", "있나요", "해줘", "대한",
    "관련", "기준", "방법", "쉽게", "단계별로", "바탕으로", "위주로", "전부", "모두"
}

_EMBED_QUOTA_COOLDOWN_UNTIL = 0.0

llm = ChatGoogleGenerativeAI(
    model="gemini-3.8-flash",
    google_api_key=GOOGLE_API_KEY,
    temperature=0.1
)
embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001",
    google_api_key=GOOGLE_API_KEY
)


def _unlock_folder_permissions(folder_p):
    try:
        os.makedirs(folder_p, exist_ok=True)
        os.chmod(folder_p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except Exception:
        pass
    for root, dirs, files in os.walk(folder_p):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception:
                pass
        for fn in files:
            try:
                os.chmod(os.path.join(root, fn), stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass


def _copy_unlocked_files(src_dir, dst_dir):
    os.makedirs(dst_dir, exist_ok=True)
    if not os.path.exists(src_dir):
        return
    for root, dirs, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        target_root = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        os.makedirs(target_root, exist_ok=True)
        for fn in files:
            if fn.endswith(("-wal", "-shm", ".lock")):
                continue
            src_f = os.path.join(root, fn)
            dst_f = os.path.join(target_root, fn)
            try:
                if not os.path.exists(dst_f) or os.path.getmtime(src_f) > os.path.getmtime(dst_f):
                    shutil.copy2(src_f, dst_f)
                    os.chmod(dst_f, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass


def init_safe_chroma_client():
    target_path = MASTER_CHROMA_PATH
    try:
        _unlock_folder_permissions(target_path)
        client = chromadb.PersistentClient(path=target_path)
        col = client.get_or_create_collection(name="bim_manuals", metadata={"hnsw:space": "cosine"})
        return client, col, target_path
    except Exception as e1:
        print("[ChromaDB 네트워크 동시접속 캐시 가동] 사내 서버 DB 잠금 감지 ({0}) -> 시스템 임시 캐시로 연결합니다.".format(e1))

    temp_cache_path = os.path.join(tempfile.gettempdir(), "BIM_AI_Chroma_Runtime_Cache")
    try:
        _copy_unlocked_files(target_path, temp_cache_path)
        _unlock_folder_permissions(temp_cache_path)
        client = chromadb.PersistentClient(path=temp_cache_path)
        col = client.get_or_create_collection(name="bim_manuals", metadata={"hnsw:space": "cosine"})
        return client, col, temp_cache_path
    except Exception:
        try:
            client = chromadb.EphemeralClient()
            col = client.get_or_create_collection(name="bim_manuals", metadata={"hnsw:space": "cosine"})
            return client, col, temp_cache_path
        except Exception:
            return None, None, target_path


chroma_client, collection, CHROMA_PATH = init_safe_chroma_client()

gc = None
sh = None
worksheet = None

_SHEET_CACHE = {
    "data": None,
    "last_fetched": 0
}
CACHE_TTL_SECONDS = 600


def get_worksheet_safe():
    global gc, sh, worksheet
    if worksheet is not None:
        return worksheet
    try:
        if not CRED_PATH or not os.path.exists(CRED_PATH) or not SHEET_ID:
            return None
        gc = gspread.service_account(filename=CRED_PATH)
        sh = gc.open_by_key(SHEET_ID)
        worksheet = sh.get_worksheet(0)
        return worksheet
    except Exception as e:
        print("[시트 인증 경고] 사내 서버 카탈로그 모드로 작동합니다:", e)
        return None


def get_collection():
    global chroma_client, collection, CHROMA_PATH
    if collection is not None:
        return collection
    chroma_client, collection, CHROMA_PATH = init_safe_chroma_client()
    return collection


def extract_keywords_fast_local(query):
    raw_words = re.findall(r'[a-zA-Z0-9가-힣]{2,}', (query or "").lower())
    keywords = []
    for w in raw_words:
        if w in STOPWORDS_KO:
            continue
        stem = re.sub(r'(에서|으로|이나|부터|까지|이랑|하고|은|는|이|가|을|를|의|에|과|와|로|도)$', '', w)
        for candidate in (w, stem):
            if len(candidate) >= 2 and candidate not in STOPWORDS_KO and candidate not in keywords:
                keywords.append(candidate)
                for syn_key, syn_list in BIM_SYNONYM_DICT.items():
                    if syn_key in candidate or candidate in syn_key:
                        for syn in syn_list:
                            if syn not in keywords:
                                keywords.append(syn)
    return keywords if keywords else raw_words


def find_referenced_server_file_paths(ai_answer_text, manual_ctx="", file_ctx="", max_files=6):
    """
    [웹 & 슬랙 즉시 다운로드 지원]
    AI 답변 또는 참조 컨텍스트에 등장한 사내 서버 원본 파일(.pdf, .xlsx, .xls, .csv, .docx, .pptx, .txt 등)의
    실제 사내 서버 경로 리스트 [(파일명, 전체경로), ...]를 반환합니다.
    """
    search_root = ARCHIVE_MASTER_DIR if os.path.exists(ARCHIVE_MASTER_DIR) else SERVER_STORAGE_PATH
    if not os.path.exists(search_root):
        return []

    combined_text = "{0}\n{1}\n{2}".format(ai_answer_text or "", manual_ctx or "", file_ctx or "").lower()
    ans_only_low = (ai_answer_text or "").lower()
    allowed_exts = {".pdf", ".xlsx", ".xls", ".csv", ".docx", ".pptx", ".txt", ".rfa", ".rte"}

    matched_primary = []
    matched_secondary = []
    seen_names = set()

    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in {"00_시스템인덱스", "01_프로젝트_산출물", "chroma_db"} and not d.startswith(".")]
        for fname in files:
            if fname.startswith(("~$", ".")) or "관리대장" in fname:
                continue
            ext_low = os.path.splitext(fname)[1].lower()
            if ext_low not in allowed_exts:
                continue
            f_low = fname.lower()
            if f_low in seen_names:
                continue
            stem_low = os.path.splitext(f_low)[0]
            full_p = os.path.join(root, fname)

            if f_low in ans_only_low or (len(stem_low) >= 4 and stem_low in ans_only_low):
                matched_primary.append((fname, full_p))
                seen_names.add(f_low)
            elif f_low in combined_text or (len(stem_low) >= 4 and stem_low in combined_text):
                matched_secondary.append((fname, full_p))
                seen_names.add(f_low)

    results = (matched_primary + matched_secondary)[:max_files]
    return results


def download_referenced_excel_files(ai_answer_text, max_files=3):
    """
    [슬랙 봇 파일 즉시 첨부 지원 (하위 호환 함수명 유지)]
    엑셀(.xlsx, .csv)뿐만 아니라 AI 답변에서 참조된 PDF 지침서, 검토보고서, 워드/PPT 문서까지
    사내 서버에서 읽어 슬랙 첨부용 바이트 리스트 [(파일명, bytes), ...]로 반환합니다.
    """
    matched_paths = find_referenced_server_file_paths(ai_answer_text, max_files=max_files)
    downloaded = []
    for fname, full_p in matched_paths:
        try:
            if os.path.exists(full_p) and os.path.getsize(full_p) <= 25 * 1024 * 1024:
                with open(full_p, "rb") as f:
                    downloaded.append((fname, f.read()))
        except Exception:
            continue
    return downloaded


def log_ai_search_history(channel_name, user_query, ai_answer, ref_files_list):
    """
    [AI 지식 검색 내역 RDB 자동 누적]
    웹 대시보드 또는 슬랙에서 수행한 AI 지식 검색 내역을
    1) 구글 시트 3번째 탭(03_AI검색_이력로그)
    2) 사내 서버 통합관리대장 엑셀(BF_파일및벡터DB_관리대장.xlsx의 03_AI검색_이력로그 시트)
    양쪽에 실시간으로 누적 기록합니다.
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_ans = re.sub(r'\s+', ' ', (ai_answer or "")).strip()[:300]
    ref_names_str = ", ".join([r[0] for r in (ref_files_list or [])]) or "일반지식"
    log_row = [now_str, channel_name, (user_query or "")[:250], clean_ans, ref_names_str]

    # 1. 사내 서버 엑셀 통합관리대장 3번째 시트에 추가
    if os.path.exists(EXCEL_LEDGER_PATH):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(EXCEL_LEDGER_PATH)
            if "03_AI검색_이력로그" in wb.sheetnames:
                ws3 = wb["03_AI검색_이력로그"]
            else:
                ws3 = wb.create_sheet(title="03_AI검색_이력로그")
                ws3.append(["검색일시", "접속채널", "사용자_질문내용", "AI_핵심답변요약", "참조된_사내파일목록"])
            ws3.append(log_row)
            wb.save(EXCEL_LEDGER_PATH)
        except Exception:
            pass

    # 2. 구글 시트 3번째 탭(03_AI검색_이력로그)에 추가
    try:
        ws_first = get_worksheet_safe()
        if ws_first is not None and sh is not None:
            try:
                ws_log = sh.worksheet("03_AI검색_이력로그")
            except Exception:
                try:
                    ws_log = sh.get_worksheet(2)
                except Exception:
                    ws_log = sh.add_worksheet(title="03_AI검색_이력로그", rows="2000", cols="8")
                    ws_log.append_row(["검색일시", "접속채널", "사용자_질문내용", "AI_핵심답변요약", "참조된_사내파일목록"])
            if ws_log is not None:
                ws_log.append_row(log_row)
    except Exception:
        pass


def get_cached_sheet_records(force_refresh=False):
    current_time = time.time()
    if force_refresh or _SHEET_CACHE["data"] is None or (current_time - _SHEET_CACHE["last_fetched"] > CACHE_TTL_SECONDS):
        records = []
        if os.path.exists(CATALOG_JSON_PATH):
            try:
                with open(CATALOG_JSON_PATH, "r", encoding="utf-8") as f:
                    records = json.load(f)
            except Exception:
                records = []

        if not records:
            ws = get_worksheet_safe()
            if ws is not None:
                try:
                    records = ws.get_all_records()
                except Exception:
                    records = []

        _SHEET_CACHE["data"] = records
        _SHEET_CACHE["last_fetched"] = current_time
    return _SHEET_CACHE["data"]


def get_indexed_manual_list():
    col = get_collection()
    names = set()
    if col:
        try:
            data = col.get(include=["metadatas"])
            for m in data.get("metadatas", []):
                if m and m.get("file_name"):
                    names.add(m.get("file_name"))
        except Exception:
            pass
    if os.path.exists(CATALOG_JSON_PATH):
        try:
            with open(CATALOG_JSON_PATH, "r", encoding="utf-8") as f:
                for it in json.load(f):
                    if it.get("file_name"):
                        names.add(it.get("file_name"))
        except Exception:
            pass
    return sorted(list(names))


def get_indexed_categories():
    col = get_collection()
    db_cats = set()
    if col:
        try:
            data = col.get(include=["metadatas"])
            db_cats = {m.get("doc_category") for m in data.get("metadatas", []) if m and m.get("doc_category")}
        except Exception:
            pass
    merged = list(DEFAULT_9_CATEGORIES)
    for c in sorted(list(db_cats)):
        if c not in merged:
            merged.append(c)
    return merged


def get_vector_db_status():
    col = get_collection()
    if not col:
        return [], []
    try:
        data = col.get(include=["documents", "metadatas"])
        ids = data.get("ids", [])
        docs = data.get("documents", [])
        metas = data.get("metadatas", [])

        doc_summary_map = {}
        chunk_details = []

        for chunk_id, content, meta in zip(ids, docs, metas):
            if not meta:
                continue
            fname = meta.get("file_name", "미상")
            dgroup = meta.get("domain_group", "미분류")
            cat = meta.get("doc_category", "미분류")
            fpath = meta.get("folder_path", "사내서버")
            ext = meta.get("file_ext", fname.rsplit('.', 1)[-1].lower() if '.' in fname else "")
            loc = meta.get("location", "")
            ctype = meta.get("chunk_type", "일반")
            stopic = meta.get("setup_topic", "일반")
            clen = len(content) if content else 0
            link = meta.get("drive_link", "")

            if fname not in doc_summary_map:
                doc_summary_map[fname] = {
                    "domain_group": dgroup,
                    "doc_category": cat,
                    "folder_path": fpath,
                    "file_name": fname,
                    "file_ext": ext,
                    "chunk_count": 0,
                    "total_chars": 0,
                    "drive_link": link
                }
            doc_summary_map[fname]["chunk_count"] += 1
            doc_summary_map[fname]["total_chars"] += clen

            chunk_details.append({
                "chunk_id": chunk_id,
                "doc_category": cat,
                "folder_path": fpath,
                "file_name": fname,
                "location": loc,
                "chunk_type": ctype,
                "setup_topic": stopic,
                "char_length": clen,
                "preview": content[:120].replace("\n", " ") + "..." if content and len(content) > 120 else content,
                "full_content": content
            })

        summary_list = []
        for fname, info in sorted(doc_summary_map.items()):
            cnt = info["chunk_count"]
            info["avg_chars"] = round(info["total_chars"] / cnt) if cnt > 0 else 0
            summary_list.append(info)

        return summary_list, chunk_details
    except Exception:
        return [], []


def compute_hybrid_score(query, doc_text, meta, vector_dist):
    tokens = [t.lower() for t in re.findall(r'[a-zA-Z0-9가-힣]{2,}', query)]
    if not tokens:
        return vector_dist

    doc_lower = (doc_text or "").lower()
    fname_lower = str(meta.get("file_name", "")).lower()
    topic_lower = str(meta.get("setup_topic", "")).lower()

    lexical_bonus = 0.0
    for tok in tokens:
        if tok in doc_lower:
            lexical_bonus += 0.035
        if tok in fname_lower:
            lexical_bonus += 0.045
        if tok in topic_lower:
            lexical_bonus += 0.025

    if meta.get("chunk_type") in {"양식및산출기준", "세팅기준데이터", "사내서버아웃풋"}:
        lexical_bonus += 0.05

    return max(0.0, vector_dist - min(lexical_bonus, 0.25))


def lexical_fallback_search(col, query, top_k=8, effective_manuals=None, target_category=None):
    docs = []
    metas = []
    if col is not None:
        try:
            data = col.get(include=["documents", "metadatas"])
            docs = data.get("documents", [])
            metas = data.get("metadatas", [])
        except Exception:
            docs, metas = [], []

    if not docs and os.path.exists(SLIDES_JSON_PATH):
        try:
            with open(SLIDES_JSON_PATH, "r", encoding="utf-8") as f:
                slide_list = json.load(f)
            for s in slide_list:
                docs.append(s.get("slide_text", ""))
                metas.append({
                    "file_name": s.get("parent_file", ""),
                    "doc_category": ", ".join(s.get("tasks", [])),
                    "setup_topic": "{0}_{1}".format("/".join(s.get("usages", [])), "/".join(s.get("disciplines", []))),
                    "location": "p.{0}".format(s.get("page_num", 1)),
                    "drive_link": s.get("parent_path", "")
                })
        except Exception:
            pass

    if not docs:
        return "현재 사내 서버 Vector DB에 적재된 문서가 없습니다. 좌측 사이드바에서 'BF 파일 분석 & 동기화'를 1회 실행해 주세요."

    all_tokens = extract_keywords_fast_local(query)

    def score_single_chunk(doc_txt, meta):
        d_low = (doc_txt or "").lower()
        f_low = str(meta.get("file_name", "")).lower()
        t_low = str(meta.get("setup_topic", "")).lower()
        c_low = str(meta.get("doc_category", "")).lower()
        s = 0.0
        for tok in all_tokens:
            if len(tok) < 2:
                continue
            if tok in f_low:
                s += 6.0
            if tok in d_low:
                s += 4.0 + min(d_low.count(tok), 4) * 0.8
            if tok in t_low:
                s += 3.0
            if tok in c_low:
                s += 2.5
        return s

    context_list = []
    if effective_manuals and len(effective_manuals) >= 1:
        per_doc_k = max(3, top_k // len(effective_manuals))
        chunk_counter = 1
        for fname in effective_manuals:
            doc_candidates = []
            for doc_txt, meta in zip(docs, metas):
                if not meta or meta.get("file_name") != fname:
                    continue
                sc = score_single_chunk(doc_txt, meta)
                doc_candidates.append((sc, doc_txt, meta))
            doc_candidates.sort(key=lambda x: x[0], reverse=True)
            for sc, doc_txt, meta in doc_candidates[:per_doc_k]:
                cat = meta.get("doc_category", "미분류")
                stopic = meta.get("setup_topic", "일반")
                loc = meta.get("location", "")
                link = meta.get("drive_link", "")
                context_list.append(
                    "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 태그: {4}] ---\n내용: {5}\n사내서버 경로: {6}".format(
                        chunk_counter, fname, loc, cat, stopic, doc_txt, link
                    )
                )
                chunk_counter += 1
        return "\n\n".join(context_list) if context_list else "선택한 문서에서 관련 내용을 찾을 수 없습니다."

    scored_all = []
    for doc_txt, meta in zip(docs, metas):
        if not meta:
            continue
        sc = score_single_chunk(doc_txt, meta)
        scored_all.append((sc, doc_txt, meta))

    scored_all.sort(key=lambda x: x[0], reverse=True)
    doc_quota = {}
    chunk_counter = 1
    for sc, doc_txt, meta in scored_all:
        fname = meta.get("file_name", "알 수 없는 파일")
        if doc_quota.get(fname, 0) >= 3:
            continue
        doc_quota[fname] = doc_quota.get(fname, 0) + 1
        cat = meta.get("doc_category", "미분류")
        stopic = meta.get("setup_topic", "일반")
        loc = meta.get("location", "")
        link = meta.get("drive_link", "")
        context_list.append(
            "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 태그: {4}] ---\n내용: {5}\n사내서버 경로: {6}".format(
                chunk_counter, fname, loc, cat, stopic, doc_txt, link
            )
        )
        chunk_counter += 1
        if len(context_list) >= top_k:
            break

    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."


def search_manuals(query, top_k=8, target_manuals=None, target_category=None):
    global _EMBED_QUOTA_COOLDOWN_UNTIL
    col = get_collection()

    all_indexed_files = get_indexed_manual_list()
    auto_matched_files = []
    if not target_manuals:
        q_clean = re.sub(r'[^a-z0-9가-힣]', '', query.lower())
        for fname in all_indexed_files:
            fname_tokens = [re.sub(r'[^a-z0-9가-힣]', '', t.lower()) for t in fname.rsplit('.', 1)[0].split() if len(t) >= 2]
            for token in fname_tokens:
                if token not in {'비아이엠팩토리', '가이드북', '검토보고', '통합', 'skep'} and len(token) >= 2 and token in q_clean:
                    auto_matched_files.append(fname)
                    break

    effective_manuals = target_manuals if target_manuals else (auto_matched_files if len(auto_matched_files) >= 2 else None)

    if not col or time.time() < _EMBED_QUOTA_COOLDOWN_UNTIL:
        return lexical_fallback_search(col, query, top_k=top_k, effective_manuals=effective_manuals, target_category=target_category)

    try:
        query_vector = embeddings.embed_query(query)
    except Exception as e:
        err_msg = str(e)
        if "PerDay" in err_msg or "limit: 1000" in err_msg:
            _EMBED_QUOTA_COOLDOWN_UNTIL = time.time() + 3600.0
        elif "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            _EMBED_QUOTA_COOLDOWN_UNTIL = time.time() + 65.0
        return lexical_fallback_search(col, query, top_k=top_k, effective_manuals=effective_manuals, target_category=target_category)

    context_list = []
    if effective_manuals and len(effective_manuals) >= 1:
        per_doc_k = max(3, top_k // len(effective_manuals))
        chunk_counter = 1
        for fname in effective_manuals:
            try:
                res = col.query(
                    query_embeddings=[query_vector],
                    n_results=per_doc_k * 2,
                    where={"file_name": fname},
                    include=["documents", "metadatas", "distances"]
                )
                if res and res['documents'] and res['documents'][0]:
                    candidates = []
                    for i, doc in enumerate(res['documents'][0]):
                        meta = res['metadatas'][0][i]
                        dist = res['distances'][0][i] if 'distances' in res and res['distances'] else 0.0
                        h_score = compute_hybrid_score(query, doc, meta, dist)
                        candidates.append((h_score, dist, doc, meta))
                    candidates.sort(key=lambda x: x[0])

                    for h_score, dist, doc, meta in candidates[:per_doc_k]:
                        cat = meta.get("doc_category", "미분류")
                        stopic = meta.get("setup_topic", "일반")
                        loc = meta.get("location", "")
                        link = meta.get("drive_link", "")
                        context_list.append(
                            "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 태그: {4}] ---\n내용: {5}\n사내서버 경로: {6}".format(
                                chunk_counter, fname, loc, cat, stopic, doc, link
                            )
                        )
                        chunk_counter += 1
            except Exception:
                continue
        if context_list:
            return "\n\n".join(context_list)
        return lexical_fallback_search(col, query, top_k=top_k, effective_manuals=effective_manuals, target_category=target_category)

    try:
        results = col.query(
            query_embeddings=[query_vector],
            n_results=24,
            include=["documents", "metadatas", "distances"]
        )
    except Exception:
        return lexical_fallback_search(col, query, top_k=top_k, effective_manuals=effective_manuals, target_category=target_category)

    if results and results['documents'] and results['documents'][0]:
        scored_candidates = []
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i]
            dist = results['distances'][0][i] if 'distances' in results and results['distances'] else 0.0
            h_score = compute_hybrid_score(query, doc, meta, dist)
            scored_candidates.append((h_score, dist, doc, meta))

        scored_candidates.sort(key=lambda x: x[0])
        doc_quota = {}
        chunk_counter = 1
        for h_score, dist, doc, meta in scored_candidates:
            fname = meta.get("file_name", "알 수 없는 파일")
            if doc_quota.get(fname, 0) >= 3:
                continue
            doc_quota[fname] = doc_quota.get(fname, 0) + 1
            cat = meta.get("doc_category", "미분류")
            stopic = meta.get("setup_topic", "일반")
            loc = meta.get("location", "")
            link = meta.get("drive_link", "")

            context_list.append(
                "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 태그: {4}] ---\n내용: {5}\n사내서버 경로: {6}".format(
                    chunk_counter, fname, loc, cat, stopic, doc, link
                )
            )
            chunk_counter += 1
            if len(context_list) >= top_k:
                break

    if context_list:
        return "\n\n".join(context_list)
    return lexical_fallback_search(col, query, top_k=top_k, effective_manuals=effective_manuals, target_category=target_category)


def search_bim_files(query):
    all_records = get_cached_sheet_records()
    if not all_records:
        return "사내 서버 카탈로그에 등록된 산출물 파일이 없습니다."

    clean_keywords = extract_keywords_fast_local(query)
    scored_files = []
    for record in all_records:
        target_str = "{0} {1} {2} {3}".format(
            record.get("project_name", ""),
            record.get("file_name", ""),
            record.get("category", ""),
            record.get("description", "")
        ).lower()
        score = sum(3 for kw in clean_keywords if kw in target_str)
        if score > 0 or len(all_records) <= 30:
            scored_files.append((score, record))

    scored_files.sort(key=lambda x: x[0], reverse=True)
    result_list = ["[사내 서버 보유 산출물 파일 검색 결과]"]
    for score, f in scored_files[:12]:
        file_info = "- 프로젝트: {0} | 파일명: {1} | 분류: {2}\n  설명: {3}\n  사내서버 경로: {4}".format(
            f.get("project_name", "공통"),
            f.get("file_name"),
            f.get("category"),
            f.get("description"),
            f.get("server_path") or f.get("drive_link")
        )
        result_list.append(file_info)
    return "\n".join(result_list)


def get_ai_response(user_query, chat_history=None, return_context=False, target_manuals=None, target_category=None, channel="Web"):
    history_text = "이전 대화 없음"
    if chat_history:
        recent_msgs = chat_history[-6:]
        formatted_msgs = ["{0}: {1}".format('사용자' if m['role'] == 'user' else 'AI', m['content']) for m in recent_msgs]
        history_text = "\n".join(formatted_msgs)

    search_query = user_query
    if chat_history and len(user_query) < 15:
        last_user_msgs = [m["content"] for m in chat_history if m["role"] == "user"]
        if last_user_msgs:
            search_query = "{0} {1}".format(last_user_msgs[-1], user_query)

    manual_context = search_manuals(
        search_query,
        top_k=8,
        target_manuals=target_manuals,
        target_category=target_category
    )
    file_context = search_bim_files(search_query)

    prompt = (
        "당신은 비아이엠팩토리 사내 실무자 및 신입사원을 돕는 명확한 'BIM 데이터 및 실무 가이드 AI 어시스턴트'입니다.\n"
        "아래 제공된 [사내 문서/보고서 지식]과 [사내 서버 산출물 파일 목록]을 바탕으로 데이터를 축약하거나 누락하지 말고 신입사원도 바로 이해할 수 있게 명확하게 답변하세요.\n\n"
        "[핵심 작성 원칙]\n"
        "1. 발주처 지침서(조달청, LH 등)의 기준과 당사(비아이엠팩토리)가 실제 수행한 사내 프로젝트 산출물(검토보고서, 수량산출서 등)을 명확히 구분하여 설명하세요.\n"
        "2. 어려운 전문 용어(예: 공유 매개변수, LOD, NWC, 일위대가, BOQ, 간섭체크 등)가 등장하면 괄호 안에 쉬운 부연 설명을 덧붙이세요.\n"
        "3. 여러 항목이나 프로젝트 사례를 비교할 때는 마크다운 표(| 항목 | 내용 |)를 적극 활용하세요.\n"
        "4. 반드시 아래의 4개 섹션 제목(■ 1 ~ ■ 4)을 사용하여 답변을 구조화하세요:\n\n"
        "■ 1. 한 줄 핵심 결론\n"
        "■ 2. 상세 기준 및 프로젝트 수행 사례 정리\n"
        "■ 3. 실무 적용 진행 순서 (Step-by-Step)\n"
        "■ 4. 관련 사내 서버 문서 및 산출물 파일 안내 (정확한 파일명 및 서버 보관 폴더 위치 명시)\n\n"
        "[이전 대화 기록]\n{0}\n\n"
        "현재 사용자 질문: {1}\n\n"
        "[사내 문서/보고서 지식]\n{2}\n\n"
        "[사내 서버 산출물 파일 목록]\n{3}"
    ).format(history_text, user_query, manual_context, file_context)

    response = llm.invoke(prompt)
    content = response.content
    final_answer = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)

    # 참조된 사내 서버 파일 추출 및 구글시트/엑셀 검색 로그(03_AI검색_이력로그) 자동 기록
    ref_files = find_referenced_server_file_paths(final_answer, manual_context, file_context, max_files=6)
    log_ai_search_history(channel, user_query, final_answer, ref_files)

    if return_context:
        return final_answer, manual_context, file_context
    return final_answer


def generate_revit_shared_param_file(user_requirement, target_manuals=None):
    context = search_manuals("공유 매개변수 파라미터 속성 기준 {0}".format(user_requirement), top_k=8, target_manuals=target_manuals)
    prompt = (
        "당신은 Autodesk Revit 공유 매개변수(Shared Parameters) 설계 전문가입니다.\n"
        "아래 [참조 기준 문서]와 [사용자 요구사항]을 바탕으로 Revit 공유 매개변수 리스트를 JSON 형식으로 추출하세요.\n"
        "참조 기준 문서에 기존 GUID가 명시된 매개변수가 있다면 그 GUID를 그대로 유지하고, 새로 추가하는 매개변수는 'NEW'라고 표기하세요.\n"
        "Revit 허용 데이터 유형(datatype): TEXT, LENGTH, AREA, VOLUME, INTEGER, NUMBER, YESNO, MATERIAL, URL 중 선택.\n"
        "반드시 마크다운 코드블록 없이 순수 JSON 배열(키: group, name, datatype, guid, description) 형식으로만 응답하세요.\n\n"
        "[사용자 요구사항]: {0}\n\n[참조 기준 문서]:\n{1}"
    ).format(user_requirement, context)

    response = llm.invoke(prompt)
    content = response.content
    raw_text = " ".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
    clean_json = re.sub(r'```json|```', '', raw_text).strip()
    param_list = json.loads(clean_json)

    group_names = sorted(list({p.get("group", "기본그룹") for p in param_list}))
    group_map = {g_name: idx + 1 for idx, g_name in enumerate(group_names)}

    lines = [
        "# This is a Revit shared parameter file.",
        "# Do not edit manually.",
        "*META\tVERSION\tMINVERSION",
        "META\t2\t1",
        "*GROUP\tID\tNAME"
    ]
    for g_name, g_id in group_map.items():
        lines.append("GROUP\t{0}\t{1}".format(g_id, g_name))
    lines.append("*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE\tDESCRIPTION\tUSERMODIFIABLE")

    for p in param_list:
        raw_guid = p.get("guid", "NEW")
        if not re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', str(raw_guid)):
            p_guid = str(uuid.uuid4())
        else:
            p_guid = str(raw_guid)
        lines.append("PARAM\t{0}\t{1}\t{2}\t\t{3}\t1\t{4}\t1".format(
            p_guid,
            p.get("name", "신규매개변수").replace("\t", "_"),
            p.get("datatype", "TEXT").upper(),
            group_map.get(p.get("group", "기본그룹"), 1),
            p.get("description", "").replace("\t", " ")
        ))
    return "\r\n".join(lines) + "\r\n", param_list


def generate_dynamo_player_file():
    dyn_dict = {
        "Uuid": "8c5d4a21-9b3e-4f1a-8e2c-1d3f5a7b9c0d",
        "IsCustomNode": False,
        "Description": "AI 브릿지 실행기",
        "Name": "BF_AI세팅실행기",
        "ElementResolver": {"ResolutionMap": {}},
        "Inputs": [{"Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b", "Name": "AI 스크립트 경로 (자동연결)", "Type": "string", "Value": LIVE_SCRIPT_PATH}],
        "Outputs": [{"Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b", "Name": "실행 결과 로그", "Type": "var"}],
        "Nodes": [],
        "Connectors": [],
        "Dependencies": [],
        "NodeLibraryDependencies": [],
        "ExtensionWorkspaceData": [],
        "Author": "BIM AI Assistant",
        "Bindings": []
    }
    dyn_str = json.dumps(dyn_dict, indent=2, ensure_ascii=False)
    with open(DYN_PLAYER_PATH, "w", encoding="utf-8") as f:
        f.write(dyn_str)
    return dyn_str, DYN_PLAYER_PATH, LIVE_SCRIPT_PATH


def check_revit_connection():
    try:
        req = urllib.request.Request(REVIT_PING_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, "연결됨 (Revit {0})".format(data.get('revit_version', ''))
    except Exception:
        return False, "미연결"


def generate_revit_setup_script(user_requirement, target_manuals=None):
    context = search_manuals("템플릿 초기 세팅 레벨 그리드 재료 스펙 {0}".format(user_requirement), top_k=8, target_manuals=target_manuals)
    prompt = (
        "당신은 Autodesk Revit API 개발 전문가입니다.\n"
        "아래 [요구사항]과 [참조문서]를 바탕으로 열린 Revit 문서(doc)를 자동 세팅하는 파이썬 스크립트를 작성하세요.\n"
        "규칙: 1) f-string 절대 사용 금지(.format()만 사용), 2) Transaction(doc, 'AI Auto Setup') 필수, 3) mm 단위는 / 304.8 피트 변환, 4) 중복 이름 예외 방어, 5) 마지막 줄에 RESULT_MSG 문자열 변수 할당.\n"
        "상단에 세팅 요약표를 마크다운 표로 먼저 제시하고 아래에 ```python 코드블록을 작성하세요.\n\n"
        "[요구사항]: {0}\n\n[참조문서]:\n{1}"
    ).format(user_requirement, context)

    response = llm.invoke(prompt)
    content = response.content
    full_text = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
    match = re.search(r'```python\s*(.*?)\s*```', full_text, re.DOTALL)
    py_code = match.group(1) if match else full_text

    with open(LIVE_SCRIPT_PATH, "w", encoding="utf-8-sig") as f:
        f.write(py_code)
    generate_dynamo_player_file()
    return full_text, py_code, LIVE_SCRIPT_PATH
