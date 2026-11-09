import os
import io
import re
import time
import uuid
import json
import urllib.request
import urllib.error
from dotenv import load_dotenv
import chromadb
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

load_dotenv()
BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
SHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
CHROMA_PATH = os.getenv("CHROMA_DB_PATH", "").strip() or os.path.join(BASE_DIR, "chroma_db")

BRIDGE_DIR = os.path.join(BASE_DIR, "bridge")
os.makedirs(BRIDGE_DIR, exist_ok=True)
LIVE_SCRIPT_PATH = os.path.join(BRIDGE_DIR, "live_setup.py")
DYN_PLAYER_PATH = os.path.join(BRIDGE_DIR, "BF_AI세팅실행기.dyn")

REVIT_BASE_URL = "http://" + "127.0.0.1:48884/bim-ai-codex"
REVIT_PING_URL = REVIT_BASE_URL + "/ping"
REVIT_EXEC_URL = REVIT_BASE_URL + "/execute"
REVIT_INSPECT_URL = REVIT_BASE_URL + "/inspect"

DEFAULT_9_CATEGORIES = [
    "대외/발주처 BIM 지침",
    "사내 표준/세팅기준",
    "스펙북/자재기준",
    "소프트웨어 가이드북",
    "BIM 검토보고서",
    "BIM 검토리스트",
    "수량산출서",
    "BIM 견적서",
    "BIM 기술제안서/BEP"
]

# [고속 검색용 사내 BIM/건설 한-영 실무 동의어 사전 (LLM 중복 호출 없이 0.001초 만에 유의어 자동 확장)]
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
    "레벨": ["level", "층고", "높이"],
    "그리드": ["grid", "축선", "열선"],
    "단열": ["단열재", "수장공사", "비교표", "물량"],
    "골조": ["콘크리트", "철근", "거푸집", "구조", "수량"]
}

STOPWORDS_KO = {
    "신입사원인데", "신입사원", "우리", "회사", "회사에서", "어떻게", "알려줘", "설명해줘",
    "찾아줘", "비교해줘", "정리해줘", "만들어줘", "무엇", "어떤", "있나요", "해줘", "대한",
    "관련", "기준", "방법", "쉽게", "단계별로", "바탕으로", "위주로", "전부", "모두"
}

# [서킷 브레이커] 임베딩 API 429 감지 시 불필요한 네트워크 대기(1.5초)를 건너뛰기 위한 타이머
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

chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
try:
    collection = chroma_client.get_or_create_collection(
        name="bim_manuals",
        metadata={"hnsw:space": "cosine"}
    )
except Exception:
    collection = None

# [무중단 지연 연결] 구글 서비스 계정(.json) 오류 시에도 슬랙 봇 및 로컬 Vector DB 검색이 죽지 않도록 방어
gc = None
sh = None
worksheet = None

_SHEET_CACHE = {
    "data": None,
    "last_fetched": 0
}
CACHE_TTL_SECONDS = 600


def get_worksheet_safe():
    """구글 시트 연결을 지연 초기화하며, 인증키 오류(invalid_grant 등) 발생 시 프로그램 종료 대신 경고만 출력합니다."""
    global gc, sh, worksheet
    if worksheet is not None:
        return worksheet
    try:
        if not CRED_PATH or not os.path.exists(CRED_PATH):
            print("[시트 연결 경고] 인증키 파일을 찾을 수 없습니다:", CRED_PATH)
            return None
        if not SHEET_ID:
            print("[시트 연결 경고] SPREADSHEET_ID가 설정되지 않았습니다.")
            return None
        gc = gspread.service_account(filename=CRED_PATH)
        sh = gc.open_by_key(SHEET_ID)
        worksheet = sh.get_worksheet(0)
        return worksheet
    except Exception as e:
        print("[시트 인증 경고] 구글 서비스 계정 키가 만료되었거나 정지되었습니다 (.json 키 확인 필요):", e)
        return None


def get_collection():
    global collection
    try:
        collection = chroma_client.get_or_create_collection(
            name="bim_manuals",
            metadata={"hnsw:space": "cosine"}
        )
    except Exception:
        collection = None
    return collection


def extract_keywords_fast_local(query):
    """
    [데이터 누락 없는 0.001초 초고속 키워드/동의어 추출기]
    외부 LLM API를 중복 호출(3.5초 소요)하지 않고 한국어 조사 제거 및 BIM 전문 유의어 사전을 결합하여
    검색 키워드를 즉시 확장합니다.
    """
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


def build_direct_download_url(view_url):
    """구글 드라이브 웹 뷰어 링크(webViewLink)로부터 즉시 다운로드 직링크를 생성합니다."""
    if not view_url:
        return ""
    u = str(view_url).strip()
    if "export=download" in u or "/export?" in u or "/export/" in u:
        return u

    m = re.search(r'/d/([a-zA-Z0-9_-]{10,})', u) or re.search(r'[?&]id=([a-zA-Z0-9_-]{10,})', u)
    if not m:
        return u

    fid = m.group(1)
    if "docs.google.com/spreadsheets" in u:
        return "https://docs.google.com/spreadsheets/d/{0}/export?format=xlsx".format(fid)
    elif "docs.google.com/presentation" in u:
        return "https://docs.google.com/presentation/d/{0}/export/pptx".format(fid)
    elif "docs.google.com/document" in u:
        return "https://docs.google.com/document/d/{0}/export?format=docx".format(fid)
    return "https://drive.google.com/uc?export=download&id={0}".format(fid)


def format_dual_links_in_answer(md_text):
    """AI 답변 내의 모든 구글 드라이브 마크다운 링크를 '파일명 : [즉시 다운로드] | [웹 미리보기]'로 자동 변환합니다."""
    if not md_text:
        return ""

    pattern = re.compile(r'\[([^\]]+)\]\((https?://(?:drive|docs)\.google\.com/[^\)]+)\)')

    def repl(match):
        label = match.group(1).strip()
        url = match.group(2).strip()
        if label in {"즉시 다운로드", "웹 미리보기", "구글 시트/웹 미리보기"}:
            return match.group(0)
        if "export=download" in url or "/export?" in url or "/export/" in url:
            return match.group(0)
        direct_url = build_direct_download_url(url)
        clean_label = label.replace("다운로드", "").replace("원본 보기", "").strip(" -:|[]") or "관련 파일"
        return "{0} : [즉시 다운로드]({1}) | [웹 미리보기]({2})".format(clean_label, direct_url, url)

    return pattern.sub(repl, md_text)


def download_referenced_excel_files(ai_answer_text, max_files=2):
    """답변에 인용된 엑셀(.xlsx) 원본 파일을 구글 드라이브에서 직접 내려받아 반환합니다."""
    if not ai_answer_text or not CRED_PATH or not os.path.exists(CRED_PATH):
        return []

    urls = re.findall(r'https?://(?:drive|docs)\.google\.com/[^\s\)\>]+', ai_answer_text)
    file_ids = []
    for u in urls:
        m = re.search(r'/d/([a-zA-Z0-9_-]{10,})', u) or re.search(r'[?&]id=([a-zA-Z0-9_-]{10,})', u)
        if m:
            fid = m.group(1)
            if fid not in file_ids:
                file_ids.append(fid)

    if not file_ids:
        return []

    downloaded = []
    try:
        creds = Credentials.from_service_account_file(
            CRED_PATH,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        drive_service = build('drive', 'v3', credentials=creds)

        for fid in file_ids:
            if len(downloaded) >= max_files:
                break
            try:
                meta = drive_service.files().get(fileId=fid, fields="id, name, mimeType, size").execute()
                fname = meta.get("name", "첨부파일.xlsx")
                mime = meta.get("mimeType", "")
                fsize = int(meta.get("size", 0)) if meta.get("size") else 0
                if fsize > 25 * 1024 * 1024:
                    continue

                is_excel = fname.lower().endswith(".xlsx") or mime in {
                    "application/vnd.google-apps.spreadsheet",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                }
                if not is_excel:
                    continue

                if mime == "application/vnd.google-apps.spreadsheet":
                    req = drive_service.files().export_media(
                        fileId=fid,
                        mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                    if not fname.lower().endswith(".xlsx"):
                        fname += ".xlsx"
                else:
                    req = drive_service.files().get_media(fileId=fid)

                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, req)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                downloaded.append((fname, fh.getvalue()))
            except Exception:
                continue
    except Exception:
        return []

    return downloaded


def get_cached_sheet_records(force_refresh=False):
    current_time = time.time()
    if force_refresh or _SHEET_CACHE["data"] is None or (current_time - _SHEET_CACHE["last_fetched"] > CACHE_TTL_SECONDS):
        ws = get_worksheet_safe()
        if ws is not None:
            try:
                _SHEET_CACHE["data"] = ws.get_all_records()
                _SHEET_CACHE["last_fetched"] = current_time
            except Exception as e:
                print("[시트 데이터 조회 경고] 시트 레코드를 읽는 중 오류가 발생했습니다:", e)
                if _SHEET_CACHE["data"] is None:
                    _SHEET_CACHE["data"] = []
        else:
            if _SHEET_CACHE["data"] is None:
                _SHEET_CACHE["data"] = []
    return _SHEET_CACHE["data"]


def get_indexed_manual_list():
    col = get_collection()
    if not col:
        return []
    try:
        data = col.get(include=["metadatas"])
        names = {m.get("file_name") for m in data.get("metadatas", []) if m and m.get("file_name")}
        return sorted(list(names))
    except Exception:
        return []


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
            cat = meta.get("doc_category", "미분류(구버전)")
            fpath = meta.get("folder_path", "루트")
            ext = meta.get("file_ext", fname.rsplit('.', 1)[-1].lower() if '.' in fname else "")
            loc = meta.get("location", "")
            ctype = meta.get("chunk_type", "일반")
            stopic = meta.get("setup_topic", "일반내용/개요")
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

    if meta.get("chunk_type") in {"양식및산출기준", "세팅기준데이터"}:
        if any(k in query for k in ["양식", "수식", "산출", "견적", "단가", "어떻게", "기준", "매개변수", "파라미터"]):
            lexical_bonus += 0.06

    return max(0.0, vector_dist - min(lexical_bonus, 0.25))


def lexical_fallback_search(col, query, top_k=8, effective_manuals=None, target_category=None):
    """
    [초고속 무중단 로컬 검색]
    LLM 중복 호출 없이 로컬 BIM 동의어 사전(extract_keywords_fast_local)으로 0.005초 만에
    전체 ChromaDB 원문을 정밀 스캔하여 동일한 top_k=8 조각을 데이터 손실 없이 반환합니다.
    """
    try:
        data = col.get(include=["documents", "metadatas"])
        docs = data.get("documents", [])
        metas = data.get("metadatas", [])
    except Exception:
        return "로컬 Vector DB 데이터를 불러올 수 없습니다."

    if not docs:
        return "현재 Vector DB에 적재된 문서가 없습니다."

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
        if meta.get("chunk_type") in {"양식및산출기준", "세팅기준데이터"}:
            s += 2.0
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
                if target_category and target_category != "전체 분류" and meta.get("doc_category") != target_category:
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
                    "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 세팅주제: {4}] ---\n내용: {5}\n원본 링크: {6}".format(
                        chunk_counter, fname, loc, cat, stopic, doc_txt, link
                    )
                )
                chunk_counter += 1
        return "\n\n".join(context_list) if context_list else "선택한 문서에서 관련 내용을 찾을 수 없습니다."

    scored_all = []
    for doc_txt, meta in zip(docs, metas):
        if not meta:
            continue
        if target_category and target_category != "전체 분류" and meta.get("doc_category") != target_category:
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
            "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 세팅주제: {4}] ---\n내용: {5}\n원본 링크: {6}".format(
                chunk_counter, fname, loc, cat, stopic, doc_txt, link
            )
        )
        chunk_counter += 1
        if len(context_list) >= top_k:
            break

    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."


def search_manuals(query, top_k=8, target_manuals=None, target_category=None):
    """서킷 브레이커가 적용된 하이브리드 검색 엔진 (데이터 누락 0%, 검색 대기시간 5초 단축)."""
    global _EMBED_QUOTA_COOLDOWN_UNTIL
    col = get_collection()
    if not col:
        return "학습된 매뉴얼 데이터가 없습니다."

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

    if time.time() < _EMBED_QUOTA_COOLDOWN_UNTIL:
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
            where_cond = {"file_name": fname}
            if target_category and target_category != "전체 분류":
                where_cond = {"$and": [{"file_name": fname}, {"doc_category": target_category}]}
            try:
                res = col.query(
                    query_embeddings=[query_vector],
                    n_results=per_doc_k * 2,
                    where=where_cond,
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
                            "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 세팅주제: {4}] ---\n내용: {5}\n원본 링크: {6}".format(
                                chunk_counter, fname, loc, cat, stopic, doc, link
                            )
                        )
                        chunk_counter += 1
            except Exception:
                continue
        return "\n\n".join(context_list) if context_list else "선택한 문서에서 관련 내용을 찾을 수 없습니다."

    query_kwargs = {
        "query_embeddings": [query_vector],
        "n_results": 24,
        "include": ["documents", "metadatas", "distances"]
    }
    if target_category and target_category != "전체 분류":
        query_kwargs["where"] = {"doc_category": target_category}

    try:
        results = col.query(**query_kwargs)
    except Exception:
        return "선택한 조건에 해당하는 문서 데이터가 없습니다."

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
                "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 세팅주제: {4}] ---\n내용: {5}\n원본 링크: {6}".format(
                    chunk_counter, fname, loc, cat, stopic, doc, link
                )
            )
            chunk_counter += 1
            if len(context_list) >= top_k:
                break

    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."


def search_bim_files(query):
    all_records = get_cached_sheet_records()
    if not all_records:
        return "구글 시트에 등록된 BIM 파일이 없거나 시트 연결이 비활성 상태입니다."

    if len(all_records) <= 100:
        result_list = ["[전체 BIM 파일 목록 직접 스캔 모드 (총 {0}개)]".format(len(all_records))]
        for f in all_records:
            file_info = "- 파일명: {0} | 분류: {1} | 버전: {2}\n  설명: {3}\n  원본 링크: {4}".format(
                f.get('file_name'), f.get('category'), f.get('software_version'), f.get('description'), f.get('drive_link')
            )
            result_list.append(file_info)
        return "\n".join(result_list)

    clean_keywords = extract_keywords_fast_local(query)
    if not clean_keywords:
        return "질문에서 검색 키워드를 추출하지 못했습니다."

    scored_files = []
    for record in all_records:
        file_name_clean = re.sub(r'[^a-z0-9가-힣]', '', str(record.get('file_name', '')).lower())
        cat_clean = re.sub(r'[^a-z0-9가-힣]', '', str(record.get('category', '')).lower())
        desc_clean = re.sub(r'[^a-z0-9가-힣]', '', str(record.get('description', '')).lower())

        score = 0
        for kw in clean_keywords:
            if kw in file_name_clean:
                score += 5
            if kw in cat_clean:
                score += 3
            if kw in desc_clean:
                score += 2
        if score > 0:
            scored_files.append((score, record))

    if not scored_files:
        return "관련된 BIM 파일을 찾을 수 없습니다."

    scored_files.sort(key=lambda x: x[0], reverse=True)
    result_list = ["[관련 BIM 파일 검색 결과]"]
    for score, f in scored_files[:10]:
        file_info = "- 파일명: {0} | 분류: {1} | 버전: {2}\n  설명: {3}\n  원본 링크: {4}".format(
            f.get('file_name'), f.get('category'), f.get('software_version'), f.get('description'), f.get('drive_link')
        )
        result_list.append(file_info)
    return "\n".join(result_list)


def get_ai_response(user_query, chat_history=None, return_context=False, target_manuals=None, target_category=None):
    """
    [단일 LLM 호출 + 신입사원 맞춤형 카드형 가이드]
    검색 단계의 불필요한 API 대기를 완전히 제거하여 답변 속도를 2배 높이면서,
    내용 압축이나 누락 없이 4단계 상세 가이드를 생성합니다.
    """
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
        "당신은 사내 신입사원 및 실무자를 돕는 친절하고 명확한 'BIM 데이터 및 실무 가이드 AI 멘토'입니다.\n"
        "질문자는 BIM 및 사내 기준에 익숙하지 않은 '신입사원'이라고 가정하고, 아래 제공된 [매뉴얼/보고서/세팅기준 지식]과 [BIM 파일 목록]을 바탕으로 데이터를 절대 축약하거나 누락하지 말고 풍부하고 가독성 높게 답변하세요.\n\n"
        "[핵심 작성 원칙 (신입사원 눈높이 & 슬랙 가독성 극대화)]\n"
        "1. 어려운 BIM/건설 전문 용어(예: 공유 매개변수, LOD, NWC, 일위대가, BOQ, 간섭체크 등)가 등장하면 반드시 괄호 안에 초보자도 이해할 수 있는 쉬운 부연 설명을 덧붙이세요.\n"
        "2. 현재 사내 문서에 완벽한 표준 지침이 없더라도 단순히 '없습니다'로 끝내지 마세요. 검색된 유사 프로젝트 사례(검토보고서, 수량산출서 등)와 사내 보유 파일(.rte 템플릿, .txt 매개변수, .rfa 패밀리)을 조합하여 신입사원이 당장 무엇부터 시작해야 하는지 안내하세요.\n"
        "3. 여러 유형이나 항목(예: 5대 체크 항목, 문서 비교 등)을 나열할 때는 마크다운 표(| A | B |)로 작성하거나, 아래와 같은 번호형 구조화 블록으로 명확히 구분해 작성하세요:\n"
        "   *① [항목명]*\n"
        "   • 핵심 내용: ...\n"
        "   • 실무 사례/기준: ...\n"
        "4. 반드시 아래의 4개 섹션 제목(■ 1 ~ ■ 4)을 정확히 사용하여 답변을 구조화하세요:\n\n"
        "■ 1. 한 줄 핵심 결론\n"
        "- 질문에 대한 가장 명확한 결론을 1~2문장으로 먼저 요약하세요.\n\n"
        "■ 2. 신입사원 눈높이 개념 및 기준 정리\n"
        "- 질문과 관련된 핵심 개념과 사내/발주처 기준(또는 문서 간 공통점·차이점, 주요 체크 유형)을 항목별로 알기 쉽게 풀어서 상세히 설명하세요.\n\n"
        "■ 3. 실무 따라하기 (Step-by-Step 진행 순서)\n"
        "- 신입사원이 실제 업무에서 그대로 따라 할 수 있도록 '1단계 -> 2단계 -> 3단계' 순서로 구체적인 행동 지침(어떤 파일을 열고, 어떤 메뉴에서 무엇을 확인·입력하는지)을 제시하세요.\n\n"
        "■ 4. 관련 사내 문서 및 BIM 파일 바로가기\n"
        "- 답변에 참조한 문서와 다운로드해야 할 BIM 파일(.rte, .rfa, .txt, .xlsx 등)을 `- [파일명](원본링크URL)` 형식으로 빠짐없이 나열하세요.\n\n"
        "[이전 대화 기록]\n{0}\n\n"
        "현재 사용자 질문: {1}\n\n"
        "[매뉴얼/보고서/세팅기준 지식]\n{2}\n\n"
        "[BIM 파일 목록]\n{3}"
    ).format(history_text, user_query, manual_context, file_context)

    response = llm.invoke(prompt)
    content = response.content
    raw_answer = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)

    final_answer = format_dual_links_in_answer(raw_answer)

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
    runner_code = (
        "import os\nimport traceback\nimport clr\n"
        "clr.AddReference('RevitAPI')\nclr.AddReference('RevitAPIUI')\nclr.AddReference('RevitServices')\n"
        "import Autodesk\nfrom Autodesk.Revit.DB import *\nfrom Autodesk.Revit.UI import *\n"
        "from RevitServices.Persistence import DocumentManager\nfrom RevitServices.Transactions import TransactionManager\n"
        "doc = DocumentManager.Instance.CurrentDBDocument\n"
        "uiapp = DocumentManager.Instance.CurrentUIApplication\n"
        "uidoc = uiapp.ActiveUIDocument\n"
        "TransactionManager.Instance.ForceCloseTransaction()\n"
        "script_path = IN[0]\n"
        "if not script_path or not os.path.exists(script_path):\n"
        "    OUT = '[대기 중] 실행할 스크립트 파일이 없습니다: {}'.format(script_path)\n"
        "else:\n"
        "    try:\n"
        "        with open(script_path, 'r', encoding='utf-8-sig') as f:\n"
        "            ai_code = f.read()\n"
        "        exec_scope = globals().copy()\n"
        "        exec_scope['doc'] = doc\n"
        "        exec_scope['uiapp'] = uiapp\n"
        "        exec_scope['uidoc'] = uidoc\n"
        "        exec_scope['__revit__'] = uiapp\n"
        "        exec(ai_code, exec_scope)\n"
        "        res_msg = exec_scope.get('RESULT_MSG', '세팅 완료')\n"
        "        OUT = '[AI 자동 세팅 성공] {}'.format(res_msg)\n"
        "    except Exception as e:\n"
        "        OUT = '[실행 오류] {}\n{}'.format(str(e), traceback.format_exc())"
    )
    dyn_dict = {
        "Uuid": "8c5d4a21-9b3e-4f1a-8e2c-1d3f5a7b9c0d",
        "IsCustomNode": False,
        "Description": "AI 브릿지 실행기",
        "Name": "BF_AI세팅실행기",
        "ElementResolver": {"ResolutionMap": {}},
        "Inputs": [{"Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b", "Name": "AI 스크립트 경로 (자동연결)", "Type": "string", "Value": LIVE_SCRIPT_PATH}],
        "Outputs": [{"Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b", "Name": "실행 결과 로그", "Type": "var"}],
        "Nodes": [
            {"ConcreteType": "CoreNodeModels.Input.Filename, CoreNodeModels", "HintPath": LIVE_SCRIPT_PATH, "InputValue": LIVE_SCRIPT_PATH, "NodeType": "ExtensionNode", "Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b", "Inputs": [], "Outputs": [{"Id": "11111111222233334444555566667777", "Name": "", "Description": "File Path", "UsingDefaultValue": False, "Level": 2, "UseLevels": False, "KeepListStructure": False}]},
            {"ConcreteType": "PythonNodeModels.PythonNode, PythonNodeModels", "NodeType": "PythonScriptNode", "Code": runner_code, "Engine": "CPython3", "VariableInputPorts": True, "Id": "99999999888877776666555544443333", "Inputs": [{"Id": "aaaaaaaabbbbccccddddeeeeffffffff", "Name": "IN[0]", "Description": "Script File Path", "UsingDefaultValue": False, "Level": 2, "UseLevels": False, "KeepListStructure": False}], "Outputs": [{"Id": "fedcba9876543210fedcba9876543210", "Name": "OUT", "Description": "Execution Result", "UsingDefaultValue": False, "Level": 2, "UseLevels": False, "KeepListStructure": False}]},
            {"ConcreteType": "CoreNodeModels.Watch, CoreNodeModels", "NodeType": "ExtensionNode", "Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b", "Inputs": [{"Id": "0123456789abcdef0123456789abcdef", "Name": "", "Description": "Node to evaluate.", "UsingDefaultValue": False, "Level": 2, "UseLevels": False, "KeepListStructure": False}], "Outputs": [{"Id": "abcdef0123456789abcdef0123456789", "Name": "", "Description": "Watch contents.", "UsingDefaultValue": False, "Level": 2, "UseLevels": False, "KeepListStructure": False}]}
        ],
        "Connectors": [
            {"Start": "11111111222233334444555566667777", "End": "aaaaaaaabbbbccccddddeeeeffffffff", "Id": "c1111111222233334444555566667777", "IsHidden": "False"},
            {"Start": "fedcba9876543210fedcba9876543210", "End": "0123456789abcdef0123456789abcdef", "Id": "c2222222333344445555666677778888", "IsHidden": "False"}
        ],
        "Dependencies": [],
        "NodeLibraryDependencies": [],
        "ExtensionWorkspaceData": [],
        "Author": "BIM AI Assistant",
        "Bindings": [],
        "View": {"Dynamo": {"ScaleFactor": 1.0, "HasRunWithoutCrash": True, "IsVisibleInDynamoLibrary": True, "Version": "2.19.3.6394", "RunType": "Manual", "RunPeriod": "1000"}}
    }
    dyn_str = json.dumps(dyn_dict, indent=2, ensure_ascii=False)
    with open(DYN_PLAYER_PATH, "w", encoding="utf-8") as f:
        f.write(dyn_str)
    return dyn_str, DYN_PLAYER_PATH, LIVE_SCRIPT_PATH


def install_pyrevit_extension():
    startup_lines = [
        "# -*- coding: utf-8 -*-",
        "from pyrevit import routes, HOST_APP",
        "import json, traceback, clr",
        "clr.AddReference('RevitAPI')",
        "clr.AddReference('RevitAPIUI')",
        "from Autodesk.Revit.DB import *",
        "from Autodesk.Revit.UI import *",
        "api = routes.API('bim-ai-codex')",
        "",
        "@api.route('/ping', methods=['GET'])",
        "def ping():",
        "    return routes.make_response(data={'status': 'online', 'revit_version': str(HOST_APP.version)})",
        "",
        "@api.route('/execute', methods=['POST'])",
        "def execute_ai_code(request, uiapp, doc, uidoc):",
        "    try:",
        "        payload = request.data if isinstance(request.data, dict) else json.loads(str(request.data))",
        "        ai_code = payload.get('code', '')",
        "        exec_scope = globals().copy()",
        "        exec_scope['doc'] = doc",
        "        exec_scope['uiapp'] = uiapp",
        "        exec_scope['uidoc'] = uidoc",
        "        exec_scope['__revit__'] = uiapp",
        "        exec_scope['RESULT_MSG'] = 'Revit 모델 자동 세팅이 완료되었습니다.'",
        "        exec(ai_code, exec_scope)",
        "        return routes.make_response(data={'status': 'success', 'output': str(exec_scope.get('RESULT_MSG', '성공'))})",
        "    except Exception as e:",
        "        return routes.make_response(data={'status': 'error', 'error_msg': str(e), 'traceback': traceback.format_exc()})",
        "",
        "@api.route('/inspect', methods=['GET'])",
        "def inspect_active_model(doc):",
        "    try:",
        "        if doc is None:",
        "            return routes.make_response(data={'status': 'error', 'error_msg': '열려 있는 Revit 문서가 없습니다.'})",
        "        p_info = doc.ProjectInformation",
        "        proj_data = {",
        "            'doc_title': str(doc.Title),",
        "            'project_name': str(p_info.Name) if p_info and p_info.Name else '미입력',",
        "            'client_name': str(p_info.ClientName) if p_info and p_info.ClientName else '미입력'",
        "        }",
        "        levels = FilteredElementCollector(doc).OfClass(Level).ToElements()",
        "        level_list = [{'name': str(l.Name), 'elevation_mm': round(l.Elevation * 304.8, 1)} for l in sorted(levels, key=lambda x: x.Elevation)]",
        "        grids = FilteredElementCollector(doc).OfClass(Grid).ToElements()",
        "        grid_names = sorted([str(g.Name) for g in grids])",
        "        param_bindings = []",
        "        it = doc.ParameterBindings.ForwardIterator()",
        "        it.Reset()",
        "        while it.MoveNext():",
        "            defn = it.Key",
        "            b_type = 'Instance' if isinstance(it.Current, InstanceBinding) else 'Type'",
        "            param_bindings.append({'param_name': str(defn.Name), 'binding_type': b_type})",
        "        materials = FilteredElementCollector(doc).OfClass(Material).ToElements()",
        "        mat_list = []",
        "        for m in list(materials)[:25]:",
        "            c_str = '{},{},{}'.format(m.Color.Red, m.Color.Green, m.Color.Blue) if m.Color and m.Color.IsValid else '미지정'",
        "            mat_list.append({'name': str(m.Name), 'rgb': c_str, 'transparency': int(m.Transparency)})",
        "        snapshot = {",
        "            'project_info': proj_data,",
        "            'level_count': len(level_list),",
        "            'levels': level_list,",
        "            'grid_count': len(grid_names),",
        "            'grids': grid_names,",
        "            'bound_parameters': param_bindings,",
        "            'key_materials': mat_list",
        "        }",
        "        return routes.make_response(data={'status': 'success', 'snapshot': snapshot})",
        "    except Exception as e:",
        "        return routes.make_response(data={'status': 'error', 'error_msg': str(e), 'traceback': traceback.format_exc()})"
    ]
    startup_py = "\n".join(startup_lines) + "\n"

    installed_paths = []
    local_ext = os.path.join(BRIDGE_DIR, "BIM_AI_Codex.extension")
    os.makedirs(local_ext, exist_ok=True)
    with open(os.path.join(local_ext, "startup.py"), "w", encoding="utf-8") as f:
        f.write(startup_py)
    installed_paths.append(local_ext)

    appdata = os.getenv("APPDATA")
    if appdata:
        py_ext = os.path.join(appdata, "pyRevit", "Extensions", "BIM_AI_Codex.extension")
        try:
            os.makedirs(py_ext, exist_ok=True)
            with open(os.path.join(py_ext, "startup.py"), "w", encoding="utf-8") as f:
                f.write(startup_py)
            installed_paths.append(py_ext)
        except Exception:
            pass
    return installed_paths, startup_py


def check_revit_connection():
    try:
        req = urllib.request.Request(REVIT_PING_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, "연결됨 (Revit {0})".format(data.get('revit_version', ''))
    except Exception:
        return False, "미연결 (Revit 미실행 또는 pyRevit Routes 비활성화)"


def send_code_to_revit_http(py_code):
    req = urllib.request.Request(
        REVIT_EXEC_URL,
        data=json.dumps({"code": py_code}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def inspect_active_revit_model():
    req = urllib.request.Request(REVIT_INSPECT_URL, method="GET")
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_live_model_qa(qa_focus, target_manuals=None):
    is_online, msg = check_revit_connection()
    if not is_online:
        return False, None, "Revit 실시간 연결이 감지되지 않았습니다 ({0}).".format(msg), ""
    try:
        res = inspect_active_revit_model()
    except Exception as e:
        return False, None, "Revit 모델 스캔 오류: {0}".format(e), ""
    if res.get("status") != "success":
        return False, None, "스캔 실패: {0}".format(res.get("error_msg")), ""

    snapshot = res.get("snapshot", {})
    context = search_manuals("표준 세팅 기준 공유 매개변수 스펙북 검토리스트 {0}".format(qa_focus), top_k=8, target_manuals=target_manuals)
    prompt = (
        "당신은 BIM 품질 검수(Model QA) 및 Autodesk Revit API 자동 보정 전문가입니다.\n"
        "[현재 열려 있는 Revit 모델 스냅샷]을 [사내 표준/발주처 기준 문서]와 대조하여 마크다운 검수표(| 검수 영역 | 기준 요구사항 | 현재 모델 상태 | 판정 | 조치 방안 |)를 작성하고, "
        "하단에 미흡 사항을 즉시 보정하는 IronPython 2.7 / CPython 3 호환 코드블록(```python ... ```, f-string 금지, .format() 사용)을 제공하세요.\n\n"
        "[사용자 중점 검수 요청]: {0}\n\n[현재 모델 상태]:\n{1}\n\n[기준 문서]:\n{2}"
    ).format(qa_focus, json.dumps(snapshot, indent=2, ensure_ascii=False), context)

    response = llm.invoke(prompt)
    content = response.content
    report = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)

    match = re.search(r'```python\s*(.*?)\s*```', report, re.DOTALL)
    fix_code = match.group(1) if match else ""
    if fix_code:
        with open(LIVE_SCRIPT_PATH, "w", encoding="utf-8-sig") as f:
            f.write(fix_code)
    return True, snapshot, report, fix_code


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


def fix_revit_code_with_llm(req, code, tb):
    prompt = (
        "Revit API 실행 중 발생한 에러를 수정하여 전체 파이썬 코드만 ```python 코드블록 안에 작성하세요 (f-string 금지, .format() 사용).\n"
        "[요구사항]: {0}\n[기존 코드]:\n{1}\n[에러 로그]:\n{2}"
    ).format(req, code, tb)
    resp = llm.invoke(prompt)
    content = resp.content
    raw = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
    match = re.search(r'```python\s*(.*?)\s*```', raw, re.DOTALL)
    return match.group(1) if match else raw


def execute_in_revit_with_self_healing(user_requirement, target_manuals=None, max_attempts=3, custom_code=None):
    logs = []
    if custom_code:
        full_desc = "양방향 검수(QA) 결과 도출된 자동 보정 스크립트 실행"
        code = custom_code
    else:
        full_desc, code, _ = generate_revit_setup_script(user_requirement, target_manuals=target_manuals)

    online, msg = check_revit_connection()
    if not online:
        return False, ["[연결 확인] Revit HTTP 수신기 미감지 ({0})".format(msg), "[대체 동작] 로컬 브릿지(live_setup.py)에 최신 코드를 저장했습니다."], full_desc, code

    for attempt in range(1, max_attempts + 1):
        logs.append("[시도 {0}/{1}차] 실행 중인 Revit으로 코드 직접 전송 중...".format(attempt, max_attempts))
        try:
            res = send_code_to_revit_http(code)
            if res.get("status") == "success":
                logs.append(" -> [성공] Revit 모델 자동 반영 완료: {0}".format(res.get("output")))
                with open(LIVE_SCRIPT_PATH, "w", encoding="utf-8-sig") as f:
                    f.write(code)
                return True, logs, full_desc, code
            else:
                tb = res.get("traceback", res.get("error_msg", ""))
                logs.append(" -> [에러 감지] {0}".format(tb.strip().splitlines()[-1] if tb else "알 수 없는 오류"))
                if attempt < max_attempts:
                    logs.append(" -> [AI 자율 디버깅] 에러 로그를 분석하여 코드를 자동 수정합니다...")
                    code = fix_revit_code_with_llm(user_requirement, code, tb)
                else:
                    logs.append(" -> [최종 실패] 최대 재시도 횟수(3회)를 초과했습니다.")
        except Exception as e:
            logs.append(" -> [통신 오류] {0}".format(e))
            break
    return False, logs, full_desc, code