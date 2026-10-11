import os
import re
import json
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from langchain_google_genai import ChatGoogleGenerativeAI

def generate_metadata_with_ai(file_names, api_key):
    """신규 파일명 리스트를 받아 AI가 분류, 버전, 상세 설명(유의어 포함)을 일괄 생성합니다."""
    if not file_names:
        return {}

    llm = ChatGoogleGenerativeAI(
        model="gemini-3.8-flash",
        google_api_key=api_key,
        temperature=0.1
    )

    prompt = f"""당신은 BIM(Building Information Modeling) 및 건설 데이터 관리 전문가입니다.
아래 제공된 BIM/건설 파일명 목록을 분석하여 각 파일의 메타데이터를 추론하세요.

작성 규칙:
1. "category": 파일의 성격에 따라 [건축, 구조, 토목, MEP, 가시설, 템플릿, 물량산출, 기타] 중 가장 적합한 분류를 적으세요. (패밀리 파일이면 '구조/패밀리', '토목/패밀리' 형태로 기재)
2. "software_version": 파일명에 버전 표기(예: R24, R26, 2025 등)가 있다면 'Revit 2026' 형태로 적고, 알 수 없다면 빈 문자열("")로 두세요.
3. "description": 파일의 용도를 명확히 설명하고, 사용자가 검색할 만한 **한글 발음, 영문 약어 풀이, 실무 동의어**를 괄호 안에 풍부하게 적으세요.
   - 예시: 파일명이 "BF-T-Bar.rfa" 이면 -> "구조 T-Bar 보 패밀리 파일 (티바, T형보, 철골보, Beam)"
   - 예시: 파일명이 "BF_Bracket(SPS).rfa" 이면 -> "토목 SPS 공법용 브라켓 패밀리 파일 (브래킷, 영구존치 가시설, 버팀보 연결재)"

반드시 다른 부가 설명이나 마크다운 기호(```json 등) 없이, 아래의 순수 JSON 객체(Key는 정확한 원본 파일명) 형식으로만 출력하세요.
{{
  "파일명1.rfa": {{"category": "...", "software_version": "...", "description": "..."}},
  "파일명2.xlsx": {{"category": "...", "software_version": "...", "description": "..."}}
}}

분석할 파일명 목록:
{json.dumps(file_names, ensure_ascii=False)}"""

    try:
        response = llm.invoke(prompt)
        content = response.content
        if isinstance(content, list):
            raw_text = " ".join([part["text"] if isinstance(part, dict) and "text" in part else str(part) for part in content])
        else:
            raw_text = str(content)

        # 마크다운 코드블록 기호가 포함되어 있을 경우 제거 후 JSON 파싱
        clean_json_str = re.sub(r'```json|```', '', raw_text).strip()
        return json.loads(clean_json_str)
    except Exception as e:
        print(f" -> [경고] AI 메타데이터 자동 생성 중 오류 발생 (기본 빈칸으로 진행합니다): {e}")
        return {}


def sync_drive_to_sheets():
    print("=== [Step 1-A: BIM 파일 메타데이터 AI 자동 동기화 시작] ===\n")
    
    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    sheet_id = os.getenv("SPREADSHEET_ID")
    bim_folder_id = os.getenv("BIM_FILE_FOLDER_ID")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")

    # 1. gspread 연결 (내부적으로 Sheets 및 Drive API 권한을 모두 획득함)
    gc = gspread.service_account(filename=key_path)
    sh = gc.open_by_key(sheet_id)
    worksheet = sh.get_worksheet(0)

    existing_ids = set(worksheet.col_values(1)[1:])
    print(f"1. 현재 시트에 등록된 기존 파일 수: {len(existing_ids)}개")

    # 2. 별도의 URL 입력 없이 gspread가 획득한 인증 객체(gc.http_client.auth)를 그대로 Drive API에 연결
    drive_service = build('drive', 'v3', credentials=gc.http_client.auth)

    query = f"'{bim_folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(
        q=query,
        pageSize=1000,
        fields="files(id, name, fileExtension, webViewLink, modifiedTime)"
    ).execute()
    
    drive_files = results.get('files', [])
    print(f"2. 구글 드라이브 BIM 폴더에서 스캔된 파일 수: {len(drive_files)}개\n")

    # 신규 파일만 먼저 추려내기
    new_files = [f for f in drive_files if f.get('id') not in existing_ids]

    if not new_files:
        print("3. 동기화 완료: 새로 추가할 파일이 없습니다. (모두 최신 상태입니다)")
        print("\n=== [동기화 작업 종료] ===")
        return

    new_file_names = [f.get('name', '') for f in new_files]
    print(f"3. 신규 파일 {len(new_files)}개 발견! AI가 파일명을 분석하여 분류와 상세 설명을 자동 작성합니다...")
    
    # AI를 통해 메타데이터 일괄 생성
    ai_metadata_map = generate_metadata_with_ai(new_file_names, gemini_api_key)

    new_rows = []
    for file in new_files:
        file_id = file.get('id')
        file_name = file.get('name', '')
        
        extension = file.get('fileExtension', '')
        if not extension and '.' in file_name:
            extension = file_name.rsplit('.', 1)[-1].lower()
            
        drive_link = file.get('webViewLink', '')
        modified_time = file.get('modifiedTime', '')
        updated_at = modified_time.split('T')[0] if 'T' in modified_time else modified_time

        meta = ai_metadata_map.get(file_name, {})
        category = meta.get("category", "")
        sw_version = meta.get("software_version", "")
        description = meta.get("description", "")

        print(f" - [{file_name}] -> 분류: {category} | 설명: {description}")

        row = [
            file_id,
            file_name,
            extension,
            category,
            sw_version,
            description,
            drive_link,
            updated_at
        ]
        new_rows.append(row)

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        print(f"\n4. 동기화 완료: 총 {len(new_rows)}개의 신규 파일 및 AI 설명이 시트에 추가되었습니다.")

    print("\n=== [동기화 작업 종료] ===")

if __name__ == "__main__":
    sync_drive_to_sheets()