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


def search_manuals(query, top_k=7, target_manual=None):
    """질문을 벡터화하여 ChromaDB에서 검색하되, 특정 문서가 지정되면 해당 문서 내에서만 검색합니다."""
    if not collection:
        return "학습된 매뉴얼 데이터가 없습니다."
    
    query_vector = embeddings.embed_query(query)
    
    query_kwargs = {
        "query_embeddings": [query_vector],
        "n_results": top_k,
        "include": ["documents", "metadatas", "distances"]
    }
    # 사용자가 특정 문서를 지정한 경우 Metadata Where 필터 적용
    if target_manual and target_manual != "전체 문서 검색":
        query_kwargs["where"] = {"file_name": target_manual}
        
    results = collection.query(**query_kwargs)
    
    context_list = []
    if results and results['documents'] and results['documents'][0]:
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i]
            dist = results['distances'][0][i] if 'distances' in results and results['distances'] else 0.0
            
            file_name = meta.get("file_name", "알 수 없는 파일")
            loc = meta.get("location", "")
            link = meta.get("drive_link", "")
            
            chunk_info = (
                f"--- [문서 조각 #{i+1} | 출처: {file_name} ({loc}) | 벡터거리: {dist:.3f}] ---\n"
                f"내용: {doc}\n"
                f"원본 링크: {link}"
            )
            context_list.append(chunk_info)
            
    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."

def extract_keywords_with_llm(query):
    """사람이 만든 불용어 사전 대신, LLM이 직접 핵심 명사와 한/영 유의어를 추출합니다."""
    extraction_prompt = f"""다음 사용자의 질문에서 BIM 파일 검색에 필요한 '핵심 대상 명사'만 추출하세요.
'패밀리', '파일', '레빗', '찾아줘', '필요해' 같은 일반 명사나 서술어, 조사는 모두 제거하세요.
특히 추출된 단어의 한글 발음, 영문 표기, 건설/BIM 실무 동의어가 있다면 반드시 함께 나열하세요.
(예시 1: "티바 패밀리 찾아줘" -> 티바, tbar, t형보)
(예시 2: "부대토목 원형수로관 레빗 파일 있어?" -> 부대토목, 원형수로관, 수로관, 배수관)
(예시 3: "SPS 브라켓 모델링 자료" -> sps, 브라켓, bracket)

결과는 다른 설명 없이 오직 쉼표(,)로 구분된 단어로만 출력하세요.

사용자 질문: {query}"""

    try:
        response = llm.invoke(extraction_prompt)
        content = response.content
        if isinstance(content, list):
            raw_text = " ".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content])
        else:
            raw_text = str(content)
            
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

    # 시트에 등록된 전체 파일이 100개 이하라면 필터링 없이 전체 목록 전달 (누락률 0%)
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

    # 파일이 100개를 초과할 경우 LLM 쿼리 확장 검색 작동
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


def get_ai_response(user_query, chat_history=None, return_context=False, target_manual=None):
    """다중 문서 교차 비교 및 마크다운 표(Table) 구조화 출력이 강화된 응답 생성 함수입니다."""
    # 1. 이전 대화 기록 텍스트 구성 (최근 6개 메시지 = 3턴 참조)
    history_text = "이전 대화 없음"
    if chat_history:
        recent_msgs = chat_history[-6:]
        formatted_msgs = []
        for msg in recent_msgs:
            role_kr = "사용자" if msg["role"] == "user" else "AI"
            formatted_msgs.append(f"{role_kr}: {msg['content']}")
        history_text = "\n".join(formatted_msgs)

    # 2. 대명사/짧은 질문일 경우 직전 질문 키워드 결합
    search_query = user_query
    if chat_history and len(user_query) < 15:
        last_user_msgs = [m["content"] for m in chat_history if m["role"] == "user"]
        if last_user_msgs:
            search_query = f"{last_user_msgs[-1]} {user_query}"

    manual_context = search_manuals(search_query, top_k=7, target_manual=target_manual)
    file_context = search_bim_files(search_query)
    
    # 3. 다중 문서 합성 및 표(Table) 출력 특화 프롬프트
    prompt = f"""당신은 사내 BIM 및 건설 데이터 관리 전문 AI 어시스턴트입니다.
아래의 [이전 대화 기록]을 참고하여 문맥을 파악하고, 제공된 [매뉴얼/보고서 지식]과 [BIM 파일 목록]만을 바탕으로 정확하게 답변하세요.

[핵심 사실 및 제한 규칙]
1. [매뉴얼/보고서 지식]과 [BIM 파일 목록] 두 곳 모두에서 질문과 관련된 정보를 전혀 찾을 수 없을 때만 '제공된 문서에서 찾을 수 없습니다.'라고 답변하세요.
2. 한쪽 저장소에라도 관련 정보가 있다면 절대 거절하지 말고 해당 정보를 안내하세요.
3. 제공된 데이터에 없는 내용을 추측하거나 외부 지식을 섞어 지어내지 마세요.

[다중 문서 비교 및 표(Table) 출력 규칙]
1. 검토사항 목록, 여러 개의 항목 정리, 두 가지 이상의 문서/가이드북 비교, 또는 2개 이상의 BIM 파일이 검색된 경우에는 반드시 **마크다운 표(Markdown Table)**를 사용하여 한눈에 비교할 수 있도록 구조화하세요.
   - 매뉴얼/보고서 정리 표 권장 컬럼: | 구분/항목 | 상세 내용 및 검토사항 | 출처 (파일명 및 위치) |
   - BIM 파일 안내 표 권장 컬럼: | 파일명 | 분류 | 버전 | 상세 설명 | 다운로드 링크 |
2. 서로 다른 문서(예: PDF 보고서와 XLSX 엑셀 시트, 또는 서로 다른 가이드북)에서 관련된 내용이 함께 검색되었다면, 각 문서의 내용을 누락 없이 교차 비교하여 종합적으로 서술하세요.
3. 링크를 표기할 때는 사용자가 클릭할 수 있도록 `[원본 보기](링크주소)` 또는 `[다운로드](링크주소)` 형태의 마크다운 하이퍼링크 문법을 적용하세요.
4. 표 아래에는 핵심 요약 및 실무적 참고사항을 불릿 포인트(-)로 간결하게 덧붙이고, 인용된 매뉴얼들의 원본 링크를 하단에 정리해 주세요.

[이전 대화 기록]
{history_text}

현재 사용자 질문: {user_query}

[매뉴얼/보고서 지식 (상위 7개 조각)]
{manual_context}

[BIM 파일 목록]
{file_context}
"""
    
    response = llm.invoke(prompt)
    content = response.content
    
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])
            else:
                text_parts.append(str(part))
        final_answer = "\n".join(text_parts)
    else:
        final_answer = str(content)

    if return_context:
        return final_answer, manual_context, file_context
    return final_answer