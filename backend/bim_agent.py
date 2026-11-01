import os
import re
import time
import uuid
import json
from dotenv import load_dotenv
import chromadb
import gspread
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

# 1. 환경변수 및 로컬 브릿지 경로 설정
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SHEET_ID = os.getenv("SPREADSHEET_ID")
CHROMA_PATH = os.getenv("CHROMA_DB_PATH")

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
BRIDGE_DIR = os.path.join(BASE_DIR, "bridge")
os.makedirs(BRIDGE_DIR, exist_ok=True)
LIVE_SCRIPT_PATH = os.path.join(BRIDGE_DIR, "live_setup.py")
DYN_PLAYER_PATH = os.path.join(BRIDGE_DIR, "BF_AI세팅실행기.dyn")

# 2. 글로벌 인스턴스 초기화
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
    collection = chroma_client.get_collection(name="bim_manuals")
except Exception:
    collection = None

gc = gspread.service_account(filename=CRED_PATH)
sh = gc.open_by_key(SHEET_ID)
worksheet = sh.get_worksheet(0)

# 3. 구글 시트 데이터 인메모리 캐시(In-Memory Cache) 설정
_SHEET_CACHE = {
    "data": None,
    "last_fetched": 0
}
CACHE_TTL_SECONDS = 600

def get_cached_sheet_records(force_refresh=False):
    """구글 시트 API 호출을 줄이기 위해 10분 주기로 메모리에 데이터를 캐싱합니다."""
    current_time = time.time()
    if force_refresh or _SHEET_CACHE["data"] is None or (current_time - _SHEET_CACHE["last_fetched"] > CACHE_TTL_SECONDS):
        _SHEET_CACHE["data"] = worksheet.get_all_records()
        _SHEET_CACHE["last_fetched"] = current_time
    return _SHEET_CACHE["data"]


def get_indexed_manual_list():
    """현재 ChromaDB에 학습되어 있는 전체 매뉴얼/보고서의 파일명 목록을 반환합니다."""
    global collection
    if not collection:
        try:
            collection = chroma_client.get_collection(name="bim_manuals")
        except Exception:
            return []
    try:
        data = collection.get(include=["metadatas"])
        names = {m.get("file_name") for m in data.get("metadatas", []) if m and m.get("file_name")}
        return sorted(list(names))
    except Exception:
        return []


def get_vector_db_status():
    """Vector DB 내부의 문서별 분류 현황 및 세팅 주제(setup_topic) 상세 데이터를 반환합니다."""
    global collection
    if not collection:
        try:
            collection = chroma_client.get_collection(name="bim_manuals")
        except Exception:
            return [], []
    try:
        data = collection.get(include=["documents", "metadatas"])
        ids = data.get("ids", [])
        docs = data.get("documents", [])
        metas = data.get("metadatas", [])

        doc_summary_map = {}
        chunk_details = []

        for chunk_id, content, meta in zip(ids, docs, metas):
            if not meta:
                continue
            fname = meta.get("file_name", "미상")
            cat = meta.get("doc_category", "미분류(구버전)")
            ext = meta.get("file_ext", fname.rsplit('.', 1)[-1].lower() if '.' in fname else "")
            loc = meta.get("location", "")
            ctype = meta.get("chunk_type", "일반")
            stopic = meta.get("setup_topic", "일반내용/개요")
            clen = len(content) if content else 0
            link = meta.get("drive_link", "")

            if fname not in doc_summary_map:
                doc_summary_map[fname] = {
                    "file_name": fname,
                    "doc_category": cat,
                    "file_ext": ext,
                    "chunk_count": 0,
                    "total_chars": 0,
                    "drive_link": link
                }
            doc_summary_map[fname]["chunk_count"] += 1
            doc_summary_map[fname]["total_chars"] += clen

            chunk_details.append({
                "chunk_id": chunk_id,
                "file_name": fname,
                "doc_category": cat,
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


def search_manuals(query, top_k=8, target_manuals=None, target_category=None):
    """다중 문서 비교 시 특정 문서가 결과를 독점하지 않도록 문서별 균등 할당 검색을 수행합니다."""
    global collection
    if not collection:
        try:
            collection = chroma_client.get_collection(name="bim_manuals")
        except Exception:
            return "학습된 매뉴얼 데이터가 없습니다."
    
    query_vector = embeddings.embed_query(query)
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
    context_list = []

    if effective_manuals and len(effective_manuals) >= 1:
        per_doc_k = max(3, top_k // len(effective_manuals))
        chunk_counter = 1
        for fname in effective_manuals:
            where_cond = {"file_name": fname}
            if target_category and target_category != "전체 분류":
                where_cond = {"$and": [{"file_name": fname}, {"doc_category": target_category}]}
            try:
                res = collection.query(
                    query_embeddings=[query_vector],
                    n_results=per_doc_k,
                    where=where_cond,
                    include=["documents", "metadatas", "distances"]
                )
                if res and res['documents'] and res['documents'][0]:
                    for i, doc in enumerate(res['documents'][0]):
                        meta = res['metadatas'][0][i]
                        dist = res['distances'][0][i] if 'distances' in res and res['distances'] else 0.0
                        cat = meta.get("doc_category", "미분류")
                        stopic = meta.get("setup_topic", "일반")
                        loc = meta.get("location", "")
                        link = meta.get("drive_link", "")
                        context_list.append(
                            f"--- [조각 #{chunk_counter} | 문서: {fname} ({loc}) | 분류: {cat} | 세팅주제: {stopic} | 거리: {dist:.3f}] ---\n"
                            f"내용: {doc}\n원본 링크: {link}"
                        )
                        chunk_counter += 1
            except Exception:
                continue
        return "\n\n".join(context_list) if context_list else "선택한 문서에서 관련 내용을 찾을 수 없습니다."

    query_kwargs = {
        "query_embeddings": [query_vector],
        "n_results": 15,
        "include": ["documents", "metadatas", "distances"]
    }
    if target_category and target_category != "전체 분류":
        query_kwargs["where"] = {"doc_category": target_category}

    try:
        results = collection.query(**query_kwargs)
    except Exception:
        return "선택한 조건에 해당하는 문서 데이터가 없습니다."

    if results and results['documents'] and results['documents'][0]:
        doc_quota = {}
        chunk_counter = 1
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i]
            fname = meta.get("file_name", "알 수 없는 파일")
            if doc_quota.get(fname, 0) >= 3:
                continue
            doc_quota[fname] = doc_quota.get(fname, 0) + 1

            dist = results['distances'][0][i] if 'distances' in results and results['distances'] else 0.0
            cat = meta.get("doc_category", "미분류")
            stopic = meta.get("setup_topic", "일반")
            loc = meta.get("location", "")
            link = meta.get("drive_link", "")

            context_list.append(
                f"--- [조각 #{chunk_counter} | 문서: {fname} ({loc}) | 분류: {cat} | 세팅주제: {stopic} | 거리: {dist:.3f}] ---\n"
                f"내용: {doc}\n원본 링크: {link}"
            )
            chunk_counter += 1
            if len(context_list) >= top_k:
                break

    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."


def extract_keywords_with_llm(query):
    """질문에서 핵심 대상 명사와 한/영 유의어를 추출합니다."""
    extraction_prompt = f"""다음 사용자의 질문에서 BIM 파일 검색에 필요한 '핵심 대상 명사'만 추출하세요.
'패밀리', '파일', '레빗', '찾아줘', '필요해' 같은 일반 명사나 서술어, 조사는 모두 제거하세요.
특히 추출된 단어의 한글 발음, 영문 표기, 건설/BIM 실무 동의어가 있다면 반드시 함께 나열하세요.
결과는 다른 설명 없이 오직 쉼표(,)로 구분된 단어로만 출력하세요.

사용자 질문: {query}"""

    try:
        response = llm.invoke(extraction_prompt)
        content = response.content
        raw_text = " ".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
        keywords = []
        for kw in raw_text.split(','):
            clean_kw = re.sub(r'[^a-z0-9가-힣]', '', kw.lower())
            if clean_kw and clean_kw not in keywords:
                keywords.append(clean_kw)
        return keywords
    except Exception:
        return [re.sub(r'[^a-z0-9가-힣]', '', w.lower()) for w in query.split() if len(w) > 1]


def search_bim_files(query):
    """캐싱된 시트 데이터를 바탕으로 빠르게 BIM 파일을 검색합니다."""
    all_records = get_cached_sheet_records()
    if not all_records:
        return "구글 시트에 등록된 BIM 파일이 없습니다."

    if len(all_records) <= 100:
        result_list = [f"[전체 BIM 파일 목록 직접 스캔 모드 (총 {len(all_records)}개 / 메모리 캐시 적용됨)]"]
        for f in all_records:
            file_info = (
                f"- 파일명: {f.get('file_name')} | 분류: {f.get('category')} | 버전: {f.get('software_version')}\n"
                f"  설명: {f.get('description')}\n"
                f"  다운로드: {f.get('drive_link')}"
            )
            result_list.append(file_info)
        return "\n".join(result_list)

    clean_keywords = extract_keywords_with_llm(query)
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
        return f"관련된 BIM 파일을 찾을 수 없습니다. (AI 자동 생성 검색어: {', '.join(clean_keywords)})"
        
    scored_files.sort(key=lambda x: x[0], reverse=True)
    result_list = [f"[AI 자동 확장 검색어: {', '.join(clean_keywords)} / 메모리 캐시 적용됨]"]
    for score, f in scored_files[:10]:
        file_info = (
            f"- 파일명: {f.get('file_name')} | 분류: {f.get('category')} | 버전: {f.get('software_version')} | 점수: {score}점\n"
            f"  설명: {f.get('description')}\n"
            f"  다운로드: {f.get('drive_link')}"
        )
        result_list.append(file_info)
    return "\n".join(result_list)


def get_ai_response(user_query, chat_history=None, return_context=False, target_manuals=None, target_category=None):
    """다중 문서 공통점/차이점 비교 분석 및 프로젝트 초기 세팅 가이드 도출에 최적화된 응답 생성 함수입니다."""
    history_text = "이전 대화 없음"
    if chat_history:
        recent_msgs = chat_history[-6:]
        formatted_msgs = [f"{'사용자' if m['role'] == 'user' else 'AI'}: {m['content']}" for m in recent_msgs]
        history_text = "\n".join(formatted_msgs)

    search_query = user_query
    if chat_history and len(user_query) < 15:
        last_user_msgs = [m["content"] for m in chat_history if m["role"] == "user"]
        if last_user_msgs:
            search_query = f"{last_user_msgs[-1]} {user_query}"

    manual_context = search_manuals(
        search_query, 
        top_k=8, 
        target_manuals=target_manuals, 
        target_category=target_category
    )
    file_context = search_bim_files(search_query)
    
    prompt = f"""당신은 사내 BIM 및 건설 데이터 관리 전문 AI 어시스턴트입니다.
아래의 [이전 대화 기록]을 참고하고, 제공된 [매뉴얼/보고서/세팅기준 지식]과 [BIM 파일 목록]만을 바탕으로 정확하게 답변하세요.

[핵심 사실 및 제한 규칙]
1. 두 저장소 모두에서 관련 정보를 전혀 찾을 수 없을 때만 '제공된 문서에서 찾을 수 없습니다.'라고 답변하세요.
2. 제공된 데이터에 없는 내용을 추측하여 지어내지 마세요. 단, 문서에 명시된 사실들을 조합하여 실무 세팅 순서나 체크리스트로 재구성하는 것은 적극 수행하세요.

[다중 문서 비교 및 프로젝트 초기 세팅 도출 규칙]
1. **보고서 간 비교 / 가이드북 간 공통점·차이점 분석 요청 시:**
   - 비교 항목을 기준으로 **마크다운 비교표(| 비교 기준 | 문서 A | 문서 B | 비고/출처 |)**를 작성하세요.
   - 표 아래에 **[공통점]**과 **[핵심 차이점]**을 명확히 구분하여 요약하세요.
2. **프로젝트 초기 세팅(매개변수, 뷰/필터 세팅, 템플릿 활용 등) 요청 시:**
   - [매뉴얼/보고서/세팅기준 지식]과 [BIM 파일 목록]의 사내 표준 파일(템플릿 `.rte`, 공유 매개변수 `.txt`, 시트 도각 `.rfa` 등)을 연계하여 **[실무 프로젝트 초기 세팅 단계별 가이드]**를 제시하세요.
3. 모든 인용 출처와 파일은 클릭 가능한 `[원본 보기](링크)` 또는 `[다운로드](링크)` 하이퍼링크를 포함하세요.

[이전 대화 기록]
{history_text}

현재 사용자 질문: {user_query}

[매뉴얼/보고서/세팅기준 지식]
{manual_context}

[BIM 파일 목록]
{file_context}
"""
    
    response = llm.invoke(prompt)
    content = response.content
    final_answer = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)

    if return_context:
        return final_answer, manual_context, file_context
    return final_answer


def generate_revit_shared_param_file(user_requirement, target_manuals=None):
    """가이드라인 및 사내 공유 매개변수 지식을 바탕으로 Revit에서 즉시 로드 가능한 공유 매개변수(.txt) 파일을 생성합니다."""
    context = search_manuals(f"공유 매개변수 파라미터 속성 기준 {user_requirement}", top_k=8, target_manuals=target_manuals)
    
    prompt = f"""당신은 Autodesk Revit 공유 매개변수(Shared Parameters) 설계 전문가입니다.
아래 [참조 기준 문서]와 [사용자 요구사항]을 바탕으로 Revit 공유 매개변수 리스트를 JSON 형식으로 추출하세요.
만약 참조 기준 문서에 기존 GUID가 명시된 매개변수가 있다면 그 GUID를 그대로 유지하고, 새로 추가하는 매개변수는 "NEW"라고 표기하세요.

Revit 허용 데이터 유형(datatype): TEXT, LENGTH, AREA, VOLUME, INTEGER, NUMBER, YESNO, MATERIAL, URL 중 선택.

반드시 마크다운 코드블록 없이 아래 순수 JSON 배열 형식으로만 응답하세요:
[
  {{"group": "1_공통속성", "name": "조달청_분류코드", "datatype": "TEXT", "guid": "NEW", "description": "조달청 표준 BIM 분류체계 코드"}},
  {{"group": "2_구조속성", "name": "강도기준", "datatype": "TEXT", "guid": "NEW", "description": "콘크리트 및 철골 강도 기준"}}
]

[사용자 요구사항]
{user_requirement}

[참조 기준 문서]
{context}
"""
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
        lines.append(f"GROUP\t{g_id}\t{g_name}")
        
    lines.append("*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE\tDESCRIPTION\tUSERMODIFIABLE")
    
    for p in param_list:
        raw_guid = p.get("guid", "NEW")
        if not re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', str(raw_guid)):
            p_guid = str(uuid.uuid4())
        else:
            p_guid = str(raw_guid)
            
        p_name = p.get("name", "신규매개변수").replace("\t", "_")
        p_dtype = p.get("datatype", "TEXT").upper()
        p_group_id = group_map.get(p.get("group", "기본그룹"), 1)
        p_desc = p.get("description", "").replace("\t", " ")
        
        lines.append(f"PARAM\t{p_guid}\t{p_name}\t{p_dtype}\t\t{p_group_id}\t1\t{p_desc}\t1")
        
    final_txt_content = "\r\n".join(lines) + "\r\n"
    return final_txt_content, param_list


def generate_dynamo_player_file():
    """현재 PC의 브릿지 폴더 경로(LIVE_SCRIPT_PATH)가 자동 연결된 Dynamo 플레이어 파일(.dyn)을 생성하여 저장 및 반환합니다."""
    runner_code = (
        "import os\n"
        "import traceback\n"
        "import clr\n\n"
        "clr.AddReference('RevitAPI')\n"
        "clr.AddReference('RevitAPIUI')\n"
        "clr.AddReference('RevitServices')\n"
        "import Autodesk\n"
        "from Autodesk.Revit.DB import *\n"
        "from Autodesk.Revit.UI import *\n"
        "from RevitServices.Persistence import DocumentManager\n"
        "from RevitServices.Transactions import TransactionManager\n\n"
        "doc = DocumentManager.Instance.CurrentDBDocument\n"
        "uiapp = DocumentManager.Instance.CurrentUIApplication\n"
        "uidoc = uiapp.ActiveUIDocument\n\n"
        "# AI 코드 내부의 Transaction 충돌을 방지하기 위해 Dynamo 기본 트랜잭션 강제 종료\n"
        "TransactionManager.Instance.ForceCloseTransaction()\n\n"
        "script_path = IN[0]\n\n"
        "if not script_path or not os.path.exists(script_path):\n"
        "    OUT = f'[대기 중] 실행할 스크립트 파일이 없습니다: {script_path}'\n"
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
        "        OUT = f'[AI 자동 세팅 성공] 적용 파일: {os.path.basename(script_path)}'\n"
        "    except Exception as e:\n"
        "        OUT = f'[실행 오류] {str(e)}\\n{traceback.format_exc()}'"
    )

    dyn_dict = {
        "Uuid": "8c5d4a21-9b3e-4f1a-8e2c-1d3f5a7b9c0d",
        "IsCustomNode": False,
        "Description": "AI가 생성한 Revit 자동 세팅 스크립트(live_setup.py)를 실시간으로 읽어 실행하는 Codex 브릿지 실행기입니다.",
        "Name": "BF_AI세팅실행기",
        "ElementResolver": {"ResolutionMap": {}},
        "Inputs": [
            {
                "Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b",
                "Name": "AI 스크립트 경로 (자동연결)",
                "Type": "string",
                "Type2": "string",
                "Value": LIVE_SCRIPT_PATH,
                "Description": "AI 웹 서버가 실시간으로 세팅 코드를 전송하는 브릿지 파일 경로입니다.",
                "SelectedIndex": 0
            }
        ],
        "Outputs": [
            {
                "Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b",
                "Name": "실행 결과 로그",
                "Type": "var",
                "InitialValue": "",
                "Description": "AI 스크립트 실행 성공 여부 및 에러 로그를 표시합니다."
            }
        ],
        "Nodes": [
            {
                "ConcreteType": "CoreNodeModels.Input.Filename, CoreNodeModels",
                "HintPath": LIVE_SCRIPT_PATH,
                "InputValue": LIVE_SCRIPT_PATH,
                "NodeType": "ExtensionNode",
                "Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b",
                "Inputs": [],
                "Outputs": [
                    {
                        "Id": "11111111222233334444555566667777",
                        "Name": "",
                        "Description": "File Path",
                        "UsingDefaultValue": False,
                        "Level": 2,
                        "UseLevels": False,
                        "KeepListStructure": False
                    }
                ],
                "Replication": "Disabled",
                "Description": "실행할 AI 파이썬 스크립트(.py) 경로를 지정합니다."
            },
            {
                "ConcreteType": "PythonNodeModels.PythonNode, PythonNodeModels",
                "NodeType": "PythonScriptNode",
                "Code": runner_code,
                "Engine": "CPython3",
                "VariableInputPorts": True,
                "Id": "99999999888877776666555544443333",
                "Inputs": [
                    {
                        "Id": "aaaaaaaabbbbccccddddeeeeffffffff",
                        "Name": "IN[0]",
                        "Description": "Script File Path",
                        "UsingDefaultValue": False,
                        "Level": 2,
                        "UseLevels": False,
                        "KeepListStructure": False
                    }
                ],
                "Outputs": [
                    {
                        "Id": "fedcba9876543210fedcba9876543210",
                        "Name": "OUT",
                        "Description": "Execution Result",
                        "UsingDefaultValue": False,
                        "Level": 2,
                        "UseLevels": False,
                        "KeepListStructure": False
                    }
                ],
                "Replication": "Disabled",
                "Description": "AI가 생성한 파이썬 코드를 현재 열린 Revit 모델에 직접 실행합니다."
            },
            {
                "ConcreteType": "CoreNodeModels.Watch, CoreNodeModels",
                "NodeType": "ExtensionNode",
                "Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b",
                "Inputs": [
                    {
                        "Id": "0123456789abcdef0123456789abcdef",
                        "Name": "",
                        "Description": "Node to evaluate.",
                        "UsingDefaultValue": False,
                        "Level": 2,
                        "UseLevels": False,
                        "KeepListStructure": False
                    }
                ],
                "Outputs": [
                    {
                        "Id": "abcdef0123456789abcdef0123456789",
                        "Name": "",
                        "Description": "Watch contents.",
                        "UsingDefaultValue": False,
                        "Level": 2,
                        "UseLevels": False,
                        "KeepListStructure": False
                    }
                ],
                "Replication": "Disabled",
                "Description": "실행 결과 로그 출력"
            }
        ],
        "Connectors": [
            {
                "Start": "11111111222233334444555566667777",
                "End": "aaaaaaaabbbbccccddddeeeeffffffff",
                "Id": "c1111111222233334444555566667777",
                "IsHidden": "False"
            },
            {
                "Start": "fedcba9876543210fedcba9876543210",
                "End": "0123456789abcdef0123456789abcdef",
                "Id": "c2222222333344445555666677778888",
                "IsHidden": "False"
            }
        ],
        "Dependencies": [],
        "NodeLibraryDependencies": [],
        "Thumbnail": "",
        "GraphDocumentationURL": None,
        "ExtensionWorkspaceData": [],
        "Author": "BIM AI Assistant",
        "Linting": {
            "activeLinter": "None",
            "activeLinterId": "7b75fb44-43fd-4631-a878-29f4d5d8399a",
            "warningCount": 0,
            "errorCount": 0
        },
        "Bindings": [],
        "View": {
            "Dynamo": {
                "ScaleFactor": 1.0,
                "HasRunWithoutCrash": True,
                "IsVisibleInDynamoLibrary": True,
                "Version": "2.19.3.6394",
                "RunType": "Manual",
                "RunPeriod": "1000"
            },
            "Camera": {
                "Name": "_Background Preview",
                "EyeX": -17.0,
                "EyeY": 24.0,
                "EyeZ": 50.0,
                "LookX": 12.0,
                "LookY": -13.0,
                "LookZ": -58.0,
                "UpX": 0.0,
                "UpY": 1.0,
                "UpZ": 0.0
            },
            "ConnectorPins": [],
            "NodeViews": [
                {
                    "Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b",
                    "Name": "AI 스크립트 경로 (자동연결)",
                    "IsSetAsInput": True,
                    "IsSetAsOutput": False,
                    "Excluded": False,
                    "ShowGeometry": True,
                    "X": 100.0,
                    "Y": 200.0
                },
                {
                    "Id": "99999999888877776666555544443333",
                    "Name": "AI Codex Runner (CPython3)",
                    "IsSetAsInput": False,
                    "IsSetAsOutput": False,
                    "Excluded": False,
                    "ShowGeometry": True,
                    "X": 450.0,
                    "Y": 200.0
                },
                {
                    "Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b",
                    "Name": "실행 결과 로그",
                    "IsSetAsInput": False,
                    "IsSetAsOutput": True,
                    "Excluded": False,
                    "ShowGeometry": True,
                    "X": 780.0,
                    "Y": 200.0
                }
            ],
            "Annotations": [],
            "X": 0.0,
            "Y": 0.0,
            "Zoom": 1.0
        }
    }

    dyn_json_str = json.dumps(dyn_dict, indent=2, ensure_ascii=False)
    with open(DYN_PLAYER_PATH, "w", encoding="utf-8") as f:
        f.write(dyn_json_str)

    return dyn_json_str, DYN_PLAYER_PATH, LIVE_SCRIPT_PATH


def generate_revit_setup_script(user_requirement, target_manuals=None):
    """스펙북(재료) 및 프로젝트 초기 세팅(레벨, 그리드, 프로젝트 정보)을 기본 템플릿에 자동 적용하는 스크립트를 생성하고 로컬 브릿지에 즉시 배포합니다."""
    context = search_manuals(f"템플릿 초기 세팅 레벨 그리드 재료 스펙 뷰 탐색기 {user_requirement}", top_k=8, target_manuals=target_manuals)
    file_context = search_bim_files("템플릿 공유 매개변수 재료")
    
    prompt = f"""당신은 Autodesk Revit API (Revit 2024~2026 CPython3 호환) 개발 전문가입니다.
작업자가 열린 Revit 문서(`doc`)에서 실행하면 아래 [사용자 세팅 요구사항]과 [참조 기준 문서(스펙북/가이드라인)]의 값을 반영하여 모델을 자동으로 세팅해주는 파이썬 스크립트를 작성하세요.

[중요 코드 작성 규칙]
1. 실행 환경에는 이미 `doc`, `uiapp`, `uidoc`, `Autodesk`, `Transaction`, `Level`, `Grid`, `Material`, `Color`, `XYZ`, `Line`, `FilteredElementCollector`가 로드되어 있으나, 안전을 위해 필요한 클래스 임포트 구문을 포함하세요.
2. 모든 모델 변경 작업은 반드시 `t = Transaction(doc, "AI Codex Auto Setup")` → `t.Start()` → 작업 수행 → `t.Commit()` 블록 내부에서 수행하세요.
3. 단위 변환: Revit 내부 길이는 피트(Feet) 단위이므로, 밀리미터(mm) 값을 넣을 때는 반드시 `mm_val / 304.8` 변환 공식을 적용하세요.
4. 동일한 이름의 레벨, 그리드, 재료가 이미 문서에 존재하는지 먼저 확인하여 중복 이름 에러(`ArgumentException`)가 발생하지 않도록 예외 처리(`try...except`)를 적용하세요.

[출력 형식]
1. 상단에 **[생성된 스크립트의 자동 세팅 내역 요약표]**를 마크다운 표로 설명하세요.
2. 그 아래에 ```python 코드블록으로 실행 가능한 전체 파이썬 코드를 작성하세요.

[사용자 세팅 요구사항]
{user_requirement}

[참조 기준 문서 (스펙북/가이드라인)]
{context}

[사내 표준 BIM 파일 목록]
{file_context}
"""
    response = llm.invoke(prompt)
    content = response.content
    full_response = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
    
    code_match = re.search(r'```python\s*(.*?)\s*```', full_response, re.DOTALL)
    extracted_py_code = code_match.group(1) if code_match else full_response
    
    # [Codex 브릿지 연동] 생성된 파이썬 코드를 로컬 브릿지 경로(bridge/live_setup.py)에 즉시 자동 저장
    with open(LIVE_SCRIPT_PATH, "w", encoding="utf-8-sig") as f:
        f.write(extracted_py_code)
    
    # Dynamo 플레이어 파일(.dyn)도 최신 경로로 함께 갱신 보장
    generate_dynamo_player_file()
    
    return full_response, extracted_py_code, LIVE_SCRIPT_PATH