import os
import re
import time
from dotenv import load_dotenv
import chromadb
import gspread
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

# 1. 환경변수 로드
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SHEET_ID = os.getenv("SPREADSHEET_ID")
CHROMA_PATH = os.getenv("CHROMA_DB_PATH")

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
CACHE_TTL_SECONDS = 600  # 10분(600초) 동안 메모리 데이터 유지

def get_cached_sheet_records(force_refresh=False):
    """구글 시트 API 호출을 줄이기 위해 10분 주기로 메모리에 데이터를 캐싱합니다."""
    current_time = time.time()
    if force_refresh or _SHEET_CACHE["data"] is None or (current_time - _SHEET_CACHE["last_fetched"] > CACHE_TTL_SECONDS):
        _SHEET_CACHE["data"] = worksheet.get_all_records()
        _SHEET_CACHE["last_fetched"] = current_time
    return _SHEET_CACHE["data"]


def get_indexed_manual_list():
    """현재 ChromaDB에 학습되어 있는 전체 매뉴얼/보고서의 파일명 목록을 반환합니다."""
    if not collection:
        return []
    try:
        data = collection.get(include=["metadatas"])
        names = {m.get("file_name") for m in data.get("metadatas", []) if m and m.get("file_name")}
        return sorted(list(names))
    except Exception:
        return []


def get_vector_db_status():
    """Vector DB 내부의 문서별 분류 현황 및 개별 청크(조각) 상세 데이터를 반환합니다."""
    if not collection:
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


def search_manuals(query, top_k=7, target_manual=None, target_category=None):
    """질문을 벡터화하여 검색하되, 문서 분류(검토보고서, 수량산출서 등)나 특정 문서 필터를 적용합니다."""
    if not collection:
        return "학습된 매뉴얼 데이터가 없습니다."
    
    query_vector = embeddings.embed_query(query)
    
    query_kwargs = {
        "query_embeddings": [query_vector],
        "n_results": top_k,
        "include": ["documents", "metadatas", "distances"]
    }
    
    # 메타데이터 필터 조건 구성
    where_conditions = []
    if target_manual and target_manual != "전체 문서 검색":
        where_conditions.append({"file_name": target_manual})
    if target_category and target_category != "전체 분류":
        where_conditions.append({"doc_category": target_category})
        
    if len(where_conditions) == 1:
        query_kwargs["where"] = where_conditions[0]
    elif len(where_conditions) > 1:
        query_kwargs["where"] = {"$and": where_conditions}
        
    try:
        results = collection.query(**query_kwargs)
    except Exception:
        # 필터에 해당하는 데이터가 없을 경우 빈 결과 처리
        return "선택한 필터 조건에 해당하는 문서 데이터가 없습니다."
    
    context_list = []
    if results and results['documents'] and results['documents'][0]:
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i]
            dist = results['distances'][0][i] if 'distances' in results and results['distances'] else 0.0
            
            file_name = meta.get("file_name", "알 수 없는 파일")
            cat = meta.get("doc_category", "미분류")
            ctype = meta.get("chunk_type", "본문")
            loc = meta.get("location", "")
            link = meta.get("drive_link", "")
            
            chunk_info = (
                f"--- [조각 #{i+1} | 분류: {cat}({ctype}) | 출처: {file_name} ({loc}) | 거리: {dist:.3f}] ---\n"
                f"내용: {doc}\n"
                f"원본 링크: {link}"
            )
            context_list.append(chunk_info)
            
    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."


def get_ai_response(user_query, chat_history=None, return_context=False, target_manual=None, target_category=None):
    """표준 양식 확인, 수량산출 작성 기준 파악 및 다중 문서 표 비교에 최적화된 응답을 생성합니다."""
    history_text = "이전 대화 없음"
    if chat_history:
        recent_msgs = chat_history[-6:]
        formatted_msgs = []
        for msg in recent_msgs:
            role_kr = "사용자" if msg["role"] == "user" else "AI"
            formatted_msgs.append(f"{role_kr}: {msg['content']}")
        history_text = "\n".join(formatted_msgs)

    search_query = user_query
    if chat_history and len(user_query) < 15:
        last_user_msgs = [m["content"] for m in chat_history if m["role"] == "user"]
        if last_user_msgs:
            search_query = f"{last_user_msgs[-1]} {user_query}"

    manual_context = search_manuals(
        search_query, 
        top_k=7, 
        target_manual=target_manual, 
        target_category=target_category
    )
    file_context = search_bim_files(search_query)
    
    prompt = f"""당신은 사내 BIM 및 건설 데이터 관리 전문 AI 어시스턴트입니다.
아래의 [이전 대화 기록]을 참고하여 문맥을 파악하고, 제공된 [매뉴얼/보고서/산출서 지식]과 [BIM 파일 목록]만을 바탕으로 정확하게 답변하세요.

[핵심 사실 및 제한 규칙]
1. 두 저장소 모두에서 질문과 관련된 정보를 전혀 찾을 수 없을 때만 '제공된 문서에서 찾을 수 없습니다.'라고 답변하세요.
2. 제공된 데이터에 없는 내용을 추측하거나 외부 지식을 섞어 지어내지 마세요.

[실무 아웃풋(양식/산출방법/검토보고서) 안내 규칙]
1. 사용자가 '수량산출서 양식', '어떻게 산출했는지(작성 기준)', '검토보고서 구성'을 질문한 경우:
   - 지식 조각 중 `[양식및산출기준]` 조각을 최우선으로 참조하여 **1) 표의 컬럼(항목) 구성 체계, 2) 적용된 엑셀 산출 수식(Formula) 및 계산 원리, 3) 주요 합계/비교 기준**을 마크다운 표로 명확히 정리하고 원본 다운로드 링크를 함께 제시하세요.
2. 검토사항 목록, 여러 개의 항목 정리, 두 가지 이상의 문서 비교, 또는 2개 이상의 BIM 파일이 검색된 경우에는 반드시 **마크다운 표(Markdown Table)**를 사용하여 한눈에 비교할 수 있도록 구조화하세요.
3. 링크는 사용자가 클릭할 수 있도록 `[원본 보기](링크주소)` 또는 `[다운로드](링크주소)` 형태의 마크다운 하이퍼링크로 표기하세요.

[이전 대화 기록]
{history_text}

현재 사용자 질문: {user_query}

[매뉴얼/보고서/산출서 지식 (상위 7개 조각)]
{manual_context}

[BIM 파일 목록]
{file_context}
"""
    
    response = llm.invoke(prompt)
    content = response.content
    
    if isinstance(content, list):
        text_parts = [part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content]
        final_answer = "\n".join(text_parts)
    else:
        final_answer = str(content)

    if return_context:
        return final_answer, manual_context, file_context
    return final_answer