import os
import re
import time
import uuid
import json
import urllib.request
import urllib.error
from dotenv import load_dotenv
import chromadb
import gspread
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

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

REVIT_BASE_URL = "http://" + "127.0.0.1:48884/bim-ai-codex"
REVIT_PING_URL = REVIT_BASE_URL + "/ping"
REVIT_EXEC_URL = REVIT_BASE_URL + "/execute"
REVIT_INSPECT_URL = REVIT_BASE_URL + "/inspect"

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

_SHEET_CACHE = {
    "data": None,
    "last_fetched": 0
}
CACHE_TTL_SECONDS = 600

def get_cached_sheet_records(force_refresh=False):
    current_time = time.time()
    if force_refresh or _SHEET_CACHE["data"] is None or (current_time - _SHEET_CACHE["last_fetched"] > CACHE_TTL_SECONDS):
        _SHEET_CACHE["data"] = worksheet.get_all_records()
        _SHEET_CACHE["last_fetched"] = current_time
    return _SHEET_CACHE["data"]

def get_indexed_manual_list():
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
                            "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 세팅주제: {4} | 거리: {5:.3f}] ---\n내용: {6}\n원본 링크: {7}".format(
                                chunk_counter, fname, loc, cat, stopic, dist, doc, link
                            )
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
                "--- [조각 #{0} | 문서: {1} ({2}) | 분류: {3} | 세팅주제: {4} | 거리: {5:.3f}] ---\n내용: {6}\n원본 링크: {7}".format(
                    chunk_counter, fname, loc, cat, stopic, dist, doc, link
                )
            )
            chunk_counter += 1
            if len(context_list) >= top_k:
                break

    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."

def extract_keywords_with_llm(query):
    extraction_prompt = "다음 사용자의 질문에서 BIM 파일 검색에 필요한 '핵심 대상 명사'만 추출하세요.\n'패밀리', '파일', '레빗', '찾아줘', '필요해' 같은 일반 명사나 서술어, 조사는 모두 제거하세요.\n특히 추출된 단어의 한글 발음, 영문 표기, 건설/BIM 실무 동의어가 있다면 반드시 함께 나열하세요.\n결과는 다른 설명 없이 오직 쉼표(,)로 구분된 단어로만 출력하세요.\n\n사용자 질문: {0}".format(query)
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
    all_records = get_cached_sheet_records()
    if not all_records:
        return "구글 시트에 등록된 BIM 파일이 없습니다."

    if len(all_records) <= 100:
        result_list = ["[전체 BIM 파일 목록 직접 스캔 모드 (총 {0}개 / 메모리 캐시 적용됨)]".format(len(all_records))]
        for f in all_records:
            file_info = "- 파일명: {0} | 분류: {1} | 버전: {2}\n  설명: {3}\n  다운로드: {4}".format(
                f.get('file_name'), f.get('category'), f.get('software_version'), f.get('description'), f.get('drive_link')
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
        return "관련된 BIM 파일을 찾을 수 없습니다. (AI 자동 확장 검색어: {0})".format(', '.join(clean_keywords))
        
    scored_files.sort(key=lambda x: x[0], reverse=True)
    result_list = ["[AI 자동 확장 검색어: {0} / 메모리 캐시 적용됨]".format(', '.join(clean_keywords))]
    for score, f in scored_files[:10]:
        file_info = "- 파일명: {0} | 분류: {1} | 버전: {2} | 점수: {3}점\n  설명: {4}\n  다운로드: {5}".format(
            f.get('file_name'), f.get('category'), f.get('software_version'), score, f.get('description'), f.get('drive_link')
        )
        result_list.append(file_info)
    return "\n".join(result_list)

def get_ai_response(user_query, chat_history=None, return_context=False, target_manuals=None, target_category=None):
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

    manual_context = search_manuals(search_query, top_k=8, target_manuals=target_manuals, target_category=target_category)
    file_context = search_bim_files(search_query)
    
    prompt = "당신은 사내 BIM 및 건설 데이터 관리 전문 AI 어시스턴트입니다.\n아래의 [이전 대화 기록]을 참고하고, 제공된 [매뉴얼/보고서/세팅기준 지식]과 [BIM 파일 목록]만을 바탕으로 정확하게 답변하세요.\n\n[핵심 사실 및 제한 규칙]\n1. 두 저장소 모두에서 관련 정보를 전혀 찾을 수 없을 때만 '제공된 문서에서 찾을 수 없습니다.'라고 답변하세요.\n2. 제공된 데이터에 없는 내용을 추측하여 지어내지 마세요.\n\n[이전 대화 기록]\n{0}\n\n현재 사용자 질문: {1}\n\n[매뉴얼/보고서/세팅기준 지식]\n{2}\n\n[BIM 파일 목록]\n{3}".format(
        history_text, user_query, manual_context, file_context
    )
    
    response = llm.invoke(prompt)
    content = response.content
    final_answer = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)

    if return_context:
        return final_answer, manual_context, file_context
    return final_answer

def generate_revit_shared_param_file(user_requirement, target_manuals=None):
    context = search_manuals("공유 매개변수 파라미터 속성 기준 {0}".format(user_requirement), top_k=8, target_manuals=target_manuals)
    prompt = "당신은 Autodesk Revit 공유 매개변수 설계 전문가입니다.\n[참조 기준 문서]와 [사용자 요구사항]을 바탕으로 Revit 공유 매개변수 리스트를 순수 JSON 배열 형식으로만 응답하세요.\n[사용자 요구사항]: {0}\n[참조 기준 문서]: {1}".format(user_requirement, context)
    response = llm.invoke(prompt)
    content = response.content
    raw_text = " ".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
    clean_json = re.sub(r'```json|```', '', raw_text).strip()
    param_list = json.loads(clean_json)
    
    group_names = sorted(list({p.get("group", "기본그룹") for p in param_list}))
    group_map = {g_name: idx + 1 for idx, g_name in enumerate(group_names)}
    
    lines = ["# This is a Revit shared parameter file.", "# Do not edit manually.", "*META\tVERSION\tMINVERSION", "META\t2\t1", "*GROUP\tID\tNAME"]
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
            p_guid, p.get("name", "신규").replace("\t", "_"), p.get("datatype", "TEXT").upper(), group_map.get(p.get("group", "기본"), 1), p.get("description", "").replace("\t", " ")
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
        "Inputs": [{"Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b", "Name": "AI 경로", "Type": "string", "Value": LIVE_SCRIPT_PATH}],
        "Outputs": [{"Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b", "Name": "로그", "Type": "var"}],
        "Nodes": [
            {"ConcreteType": "CoreNodeModels.Input.Filename, CoreNodeModels", "HintPath": LIVE_SCRIPT_PATH, "InputValue": LIVE_SCRIPT_PATH, "Id": "a1b2c3d4e5f647898a1b2c3d4e5f6a7b", "Outputs": [{"Id": "11111111222233334444555566667777"}]},
            {"ConcreteType": "PythonNodeModels.PythonNode, PythonNodeModels", "Code": runner_code, "Engine": "CPython3", "Id": "99999999888877776666555544443333", "Inputs": [{"Id": "aaaaaaaabbbbccccddddeeeeffffffff", "Name": "IN[0]"}]},
            {"ConcreteType": "CoreNodeModels.Watch, CoreNodeModels", "Id": "f6e5d4c3b2a147898a1b2c3d4e5f6a7b", "Inputs": [{"Id": "0123456789abcdef0123456789abcdef"}]}
        ],
        "Connectors": [
            {"Start": "11111111222233334444555566667777", "End": "aaaaaaaabbbbccccddddeeeeffffffff", "Id": "c1"},
            {"Start": "fedcba9876543210fedcba9876543210", "End": "0123456789abcdef0123456789abcdef", "Id": "c2"}
        ],
        "View": {"Dynamo": {"Version": "2.19.3.6394"}}
    }
    dyn_str = json.dumps(dyn_dict, indent=2, ensure_ascii=False)
    with open(DYN_PLAYER_PATH, "w", encoding="utf-8") as f:
        f.write(dyn_str)
    return dyn_str, DYN_PLAYER_PATH, LIVE_SCRIPT_PATH

def install_pyrevit_extension():
    startup_py = (
        "# -*- coding: utf-8 -*-\n"
        "from pyrevit import routes, HOST_APP\n"
        "import json, traceback, clr\n"
        "clr.AddReference('RevitAPI')\nclr.AddReference('RevitAPIUI')\n"
        "from Autodesk.Revit.DB import *\nfrom Autodesk.Revit.UI import *\n"
        "api = routes.API('bim-ai-codex')\n\n"
        "@api.route('/ping', methods=['GET'])\n"
        "def ping():\n"
        "    return routes.make_response(data={'status': 'online', 'revit_version': str(HOST_APP.version)})\n"
        "@api.route('/execute', methods=['POST'])\n"
        "def execute_ai_code(request, uiapp, doc, uidoc):\n"
        "    try:\n"
        "        payload = request.data if isinstance(request.data, dict) else json.loads(str(request.data))\n"
        "        ai_code = payload.get('code', '')\n"
        "        exec_scope = globals().copy()\n"
        "        exec_scope['doc'] = doc\n"
        "        exec_scope['uiapp'] = uiapp\n"
        "        exec_scope['uidoc'] = uidoc\n"
        "        exec_scope['RESULT_MSG'] = '세팅 완료'\n"
        "        exec(ai_code, exec_scope)\n"
        "        return routes.make_response(data={'status': 'success', 'output': str(exec_scope.get('RESULT_MSG', '성공'))})\n"
        "    except Exception as e:\n"
        "        return routes.make_response(data={'status': 'error', 'error_msg': str(e), 'traceback': traceback.format_exc()})\n"
        "@api.route('/inspect', methods=['GET'])\n"
        "def inspect_active_model(doc):\n"
        "    try:\n"
        "        if doc is None:\n"
        "            return routes.make_response(data={'status': 'error', 'error_msg': '문서 없음'})\n"
        "        p_info = doc.ProjectInformation\n"
        "        levels = FilteredElementCollector(doc).OfClass(Level).ToElements()\n"
        "        level_list = [{'name': str(l.Name), 'elevation_mm': round(l.Elevation * 304.8, 1)} for l in levels]\n"
        "        grids = FilteredElementCollector(doc).OfClass(Grid).ToElements()\n"
        "        grid_names = sorted([str(g.Name) for g in grids])\n"
        "        snapshot = {'project_name': str(p_info.Name) if p_info else '', 'levels': level_list, 'grids': grid_names}\n"
        "        return routes.make_response(data={'status': 'success', 'snapshot': snapshot})\n"
        "    except Exception as e:\n"
        "        return routes.make_response(data={'status': 'error', 'traceback': traceback.format_exc()})\n"
    )
    local_ext = os.path.join(BRIDGE_DIR, "BIM_AI_Codex.extension")
    os.makedirs(local_ext, exist_ok=True)
    with open(os.path.join(local_ext, "startup.py"), "w", encoding="utf-8") as f:
        f.write(startup_py)
    
    appdata = os.getenv("APPDATA")
    if appdata:
        py_ext = os.path.join(appdata, "pyRevit", "Extensions", "BIM_AI_Codex.extension")
        try:
            os.makedirs(py_ext, exist_ok=True)
            with open(os.path.join(py_ext, "startup.py"), "w", encoding="utf-8") as f:
                f.write(startup_py)
        except Exception:
            pass
    return [local_ext], startup_py

def check_revit_connection():
    try:
        req = urllib.request.Request(REVIT_PING_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, "연결됨 (Revit {0})".format(data.get('revit_version', ''))
    except Exception:
        return False, "미연결"

def send_code_to_revit_http(py_code):
    req = urllib.request.Request(REVIT_EXEC_URL, data=json.dumps({"code": py_code}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

def inspect_active_revit_model():
    req = urllib.request.Request(REVIT_INSPECT_URL, method="GET")
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

def run_live_model_qa(qa_focus, target_manuals=None):
    is_online, msg = check_revit_connection()
    if not is_online:
        return False, None, "Revit 연결 안 됨: {0}".format(msg), ""
    try:
        res = inspect_active_revit_model()
    except Exception as e:
        return False, None, "스캔 오류: {0}".format(e), ""
    if res.get("status") != "success":
        return False, None, "실패: {0}".format(res.get("error_msg")), ""
    
    snapshot = res.get("snapshot", {})
    context = search_manuals("세팅 기준 매개변수 {0}".format(qa_focus), top_k=5, target_manuals=target_manuals)
    prompt = "현재 Revit 모델 상태와 기준 문서를 대조하여 검수 보고서와 자동 보정 파이썬 코드를 작성하세요.\n[모델 상태]: {0}\n[기준 문서]: {1}".format(json.dumps(snapshot, ensure_ascii=False), context)
    
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
    context = search_manuals("템플릿 초기 세팅 레벨 그리드 {0}".format(user_requirement), top_k=5, target_manuals=target_manuals)
    prompt = "Revit API 파이썬 스크립트를 작성하세요. f-string 절대 금지, .format() 사용, Transaction 필수.\n[요구사항]: {0}\n[참조문서]: {1}".format(user_requirement, context)
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
    prompt = "Revit 에러를 수정하세요. f-string 금지, .format() 사용.\n[요구사항]: {0}\n[코드]: {1}\n[에러]: {2}".format(req, code, tb)
    resp = llm.invoke(prompt)
    content = resp.content
    raw = "\n".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]) if isinstance(content, list) else str(content)
    match = re.search(r'```python\s*(.*?)\s*```', raw, re.DOTALL)
    return match.group(1) if match else raw

def execute_in_revit_with_self_healing(user_requirement, target_manuals=None, max_attempts=3, custom_code=None):
    logs = []
    if custom_code:
        full_desc = "QA 자동 보정"
        code = custom_code
    else:
        full_desc, code, _ = generate_revit_setup_script(user_requirement, target_manuals=target_manuals)
    
    online, msg = check_revit_connection()
    if not online:
        return False, ["[연결 확인] Revit 미연결 ({0})".format(msg)], full_desc, code

    for attempt in range(1, max_attempts + 1):
        logs.append("[시도 {0}] 전송 중...".format(attempt))
        try:
            res = send_code_to_revit_http(code)
            if res.get("status") == "success":
                logs.append(" [성공] 완료: {0}".format(res.get("output")))
                return True, logs, full_desc, code
            else:
                tb = res.get("traceback", res.get("error_msg", ""))
                logs.append(" [에러] {0}".format(tb.strip().splitlines()[-1] if tb else "알 수 없음"))
                if attempt < max_attempts:
                    logs.append(" [디버깅] 자동 수정 중...")
                    code = fix_revit_code_with_llm(user_requirement, code, tb)
        except Exception as e:
            logs.append(" [통신 오류] {0}".format(e))
            break
    return False, logs, full_desc, code