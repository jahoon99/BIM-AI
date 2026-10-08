import os
import re
from dotenv import load_dotenv
import chromadb
import gspread
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

# 1. 환경변수 및 API 키 로드
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SHEET_ID = os.getenv("SPREADSHEET_ID")
CHROMA_PATH = os.getenv("CHROMA_DB_PATH")

# 2. 글로벌 인스턴스 초기화 (앱 실행 시 1회만 로드하여 속도 향상)
# LLM & Embedding 설정
llm = ChatGoogleGenerativeAI(
    model="gemini-3.8-flash", 
    google_api_key=GOOGLE_API_KEY, 
    temperature=0.1  # 창의성보다 정확성을 위해 낮게 설정
)
embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001", 
    google_api_key=GOOGLE_API_KEY
)

# ChromaDB 설정
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
try:
    collection = chroma_client.get_collection(name="bim_manuals")
except:
    collection = None

# 구글 시트 설정
gc = gspread.service_account(filename=CRED_PATH)
sh = gc.open_by_key(SHEET_ID)
worksheet = sh.get_worksheet(0)

def search_manuals(query, top_k=3):
    """질문을 벡터화하여 ChromaDB에서 관련 문서 조각을 찾습니다."""
    if not collection:
        return "학습된 매뉴얼 데이터가 없습니다."
    
    query_vector = embeddings.embed_query(query)
    results = collection.query(query_embeddings=[query_vector], n_results=top_k)
    
    context_list = []
    if results and results['documents'] and results['documents'][0]:
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i]
            file_name = meta.get("file_name", "알 수 없는 파일")
            loc = meta.get("location", "")
            link = meta.get("drive_link", "")
            
            chunk_info = f"[{file_name} ({loc})]\n내용: {doc}\n원본 링크: {link}"
            context_list.append(chunk_info)
            
    return "\n\n".join(context_list) if context_list else "관련 매뉴얼 내용을 찾을 수 없습니다."

def search_bim_files(query):
    """기호와 띄어쓰기를 완전히 무시하고 핵심 문자열로만 BIM 파일을 검색합니다."""
    # 1. 질문에서 키워드 분리 후 모든 특수기호 제거 (t-bar -> tbar)
    keywords = query.lower().split()
    clean_keywords = [re.sub(r'[^a-z0-9가-힣]', '', kw) for kw in keywords]
    clean_keywords = [kw for kw in clean_keywords if len(kw) > 1] # 1글자 제외
    
    all_records = worksheet.get_all_records()
    matched_files = []
    
    for record in all_records:
        # 2. 구글 시트의 데이터도 모두 합친 뒤 특수기호 및 공백 완전 제거
        raw_target = f"{record.get('file_name', '')} {record.get('category', '')} {record.get('description', '')}".lower()
        clean_target = re.sub(r'[^a-z0-9가-힣]', '', raw_target)
        
        # 3. 비교 (예: 'tbar'가 'tbarrfa' 안에 포함되는가?)
        for kw in clean_keywords:
            if kw in clean_target:
                matched_files.append(record)
                break
                
    if not matched_files:
        print("\n-> [디버깅: 파이썬 검색] 시트에서 매칭되는 파일을 찾지 못했습니다.")
        return "관련된 BIM/템플릿 파일을 찾을 수 없습니다."
        
    result_list = []
    for f in matched_files[:5]:
        file_info = f"- 파일명: {f['file_name']} (종류: {f['category']})\n  설명: {f['description']}\n  다운로드: {f['drive_link']}"
        result_list.append(file_info)
        
    final_result = "\n".join(result_list)
    print(f"\n-> [디버깅: 파이썬 검색 성공]\n{final_result}\n")
    return final_result

def get_ai_response(user_query):
    """두 저장소의 검색 결과를 종합하여 최종 답변을 생성합니다."""
    # 1. 데이터베이스 검색
    manual_context = search_manuals(user_query)
    file_context = search_bim_files(user_query)
    
    # 2. 프롬프트 엔지니어링
    prompt = f"""당신은 사내 BIM 및 건설 데이터 관리 AI 어시스턴트입니다.
사용자의 질문에 대해 아래 제공된 [매뉴얼/보고서 지식]과 [BIM 파일 목록]만을 바탕으로 답변하세요.
절대 본인의 기본 지식을 섞거나 추측하여 지어내지 마세요. 정보가 부족하면 '제공된 문서에서 찾을 수 없습니다'라고 답변하세요.

답변 규칙:
1. 매뉴얼/보고서의 내용을 인용할 때는 반드시 괄호로 파일명과 위치(예: 슬라이드 1장)를 명시하세요.
2. 매뉴얼 지식의 원본 드라이브 링크가 있다면 답변 끝에 첨부하세요.
3. 사용자가 찾고자 하는 파일(.rfa, .rvt 등)이 [BIM 파일 목록]에 있다면 다운로드 링크를 제공하세요.

사용자 질문: {user_query}

[매뉴얼/보고서 지식]
{manual_context}

[BIM 파일 목록]
{file_context}
"""
    
    # 3. 모델 호출 및 응답 반환
    response = llm.invoke(prompt)
    content = response.content
    
    # 응답이 리스트로 들어온 경우 텍스트 블록만 추출
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])
            else:
                text_parts.append(str(part))
        return "\n".join(text_parts)
        
    return str(content)

# --- 테스트용 터미널 실행 블록 ---
if __name__ == "__main__":
    print("=== [사내 AI 에이전트 터미널 테스트] ===")
    print("질문을 입력하세요. (종료하려면 'exit' 입력)")
    while True:
        user_input = input("\nQ: ")
        if user_input.lower() in ['exit', 'quit']:
            break
        
        print("\nAI 생각 중...")
        answer = get_ai_response(user_input)
        print("\n[AI 답변]\n" + answer)
        print("-" * 50)