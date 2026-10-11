import os
import re
import io
import json
from dotenv import load_dotenv
import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from langchain_google_genai import ChatGoogleGenerativeAI

def extract_revit_version_from_binary(drive_service, file_id, ext):
    """Revit 파일(.rfa, .rvt, .rte)의 바이너리에서 실제 저장된 Revit 버전을 추출합니다."""
    if ext not in {'rfa', 'rvt', 'rte'}:
        return ""
        
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        # 대형 .rvt 파일을 대비해 청크 크기를 5MB로 설정하고 최대 3번(15MB)까지만 읽음
        downloader = MediaIoBaseDownload(fh, request, chunksize=5 * 1024 * 1024)
        done = False
        chunk_count = 0
        while not done and chunk_count < 3:
            status, done = downloader.next_chunk()
            chunk_count += 1
            
        raw_bytes = fh.getvalue()
        
        # 1차 탐색: UTF-16LE 인코딩된 BasicFileInfo 영역에서 버전 탐색
        text_utf16 = raw_bytes.decode('utf-16le', errors='ignore')
        
        # 패턴 A: "Format: 2024" 또는 "Revit Build: Autodesk Revit 2025"
        match = re.search(r'(?:Format\s*:\s*|Autodesk\s+Revit\s+|Revit\s+Architecture\s+|Revit\s+Structure\s+|Revit\s+MEP\s+)(20[1-3]\d)', text_utf16)
        if match:
            return f"Revit {match.group(1)}"
            
        # 패턴 B: Build 문자열 주변의 4자리 연도 탐색
        match_build = re.search(r'(20[1-3]\d).*?Build', text_utf16, re.IGNORECASE)
        if match_build:
            return f"Revit {match_build.group(1)}"

        # 2차 탐색: UTF-16 정렬(짝수/홀수 바이트 경계)이 어긋난 경우를 대비해 바이트 직접 정규식 탐색
        # 'F\x00o\x00r\x00m\x00a\x00t\x00:\x00 \x002\x000\x002\x00X\x00'
        byte_match = re.search(b'F\x00o\x00r\x00m\x00a\x00t\x00:\x00\s*\x00(2\x000\x00[1-3]\x00\d\x00)', raw_bytes)
        if byte_match:
            ver_str = byte_match.group(1).decode('utf-16le', errors='ignore')
            return f"Revit {ver_str}"
            
    except Exception as e:
        print(f"   -> [참고] 바이너리 버전 추출 생략 (사유: {e})")
        
    return ""


def generate_metadata_with_ai(file_names, api_key):
    """신규 파일명 리스트를 받아 AI가 분류, 버전(파일명 기준), 상세 설명을 일괄 생성합니다."""
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
1. "category": 파일의 성격에 따라 [건축/패밀리, 구조/패밀리, 토목/패밀리, MEP/패밀리, 가시설/패밀리, 템플릿, 프로젝트모델, 물량산출, 기타] 중 가장 적합한 분류를 적으세요.
2. "software_version": 파일명에 버전 표기(예: R24, R26, 2025 등)가 있다면 'Revit 2026' 형태로 적고, 알 수 없다면 빈 문자열("")로 두세요.
3. "description": 파일의 용도를 명확히 설명하고, 사용자가 검색할 만한 **한글 발음, 영문 약어 풀이, 실무 동의어**를 괄호 안에 풍부하게 적으세요.
   - 예시: "BF-T-Bar.rfa" -> "구조 T-Bar 보 패밀리 파일 (티바, T형보, 철골보, Beam)"
   - 예시: "BF_Bracket(SPS).rfa" -> "토목 SPS 공법용 브라켓 패밀리 파일 (브래킷, 영구존치 가시설, 버팀보 연결재)"

반드시 다른 설명이나 마크다운 기호 없이 아래의 순수 JSON 객체 형식으로만 출력하세요.
{{
  "파일명1.rfa": {{"category": "...", "software_version": "...", "description": "..."}}
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

        clean_json_str = re.sub(r'```json|```', '', raw_text).strip()
        return json.loads(clean_json_str)
    except Exception as e:
        print(f" -> [경고] AI 메타데이터 자동 생성 중 오류 발생: {e}")
        return {}


def sync_drive_to_sheets():
    print("=== [Step 1-A: BIM 파일 메타데이터 완전 동기화(추가/삭제/버전추출) 시작] ===\n")
    
    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    sheet_id = os.getenv("SPREADSHEET_ID")
    bim_folder_id = os.getenv("BIM_FILE_FOLDER_ID")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")

    # 1. 구글 시트 연결 및 기존 ID 목록(행 번호 포함) 읽기
    gc = gspread.service_account(filename=key_path)
    sh = gc.open_by_key(sheet_id)
    worksheet = sh.get_worksheet(0)

    col_a_values = worksheet.col_values(1)  # 1행은 헤더(file_id), 2행부터 실제 데이터
    existing_ids = set(col_a_values[1:])
    print(f"1. 현재 시트에 등록된 기존 파일 수: {len(existing_ids)}개")

    # 2. 구글 드라이브 스캔 (gspread 인증 객체 재사용)
    drive_service = build('drive', 'v3', credentials=gc.http_client.auth)

    query = f"'{bim_folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(
        q=query,
        pageSize=1000,
        fields="files(id, name, fileExtension, webViewLink, modifiedTime)"
    ).execute()
    
    drive_files = results.get('files', [])
    drive_file_ids = {f.get('id') for f in drive_files}
    print(f"2. 구글 드라이브 BIM 폴더에서 스캔된 파일 수: {len(drive_files)}개\n")

    # 3. [과제 1] 구글 드라이브에서 삭제된 파일(Orphan Row) 감지 및 시트 행 역순 삭제
    rows_to_delete = []
    for row_idx, sheet_file_id in enumerate(col_a_values[1:], start=2):
        if sheet_file_id and sheet_file_id not in drive_file_ids:
            rows_to_delete.append((row_idx, sheet_file_id))

    if rows_to_delete:
        print(f"3. [삭제 감지] 구글 드라이브에서 제거된 파일 {len(rows_to_delete)}개를 시트에서 삭제합니다.")
        # 인덱스 밀림 방지를 위해 반드시 아래쪽 행(큰 번호)부터 역순으로 삭제
        for row_idx, del_id in sorted(rows_to_delete, key=lambda x: x[0], reverse=True):
            worksheet.delete_rows(row_idx)
            print(f" - 시트 {row_idx}행 삭제 완료 (File ID: {del_id})")
        print()
    else:
        print("3. 삭제된 파일 없음 (시트 정합성 정상)\n")

    # 4. 신규 파일 추출 및 [과제 2] 실제 Revit 버전 판독 + AI 메타데이터 생성
    new_files = [f for f in drive_files if f.get('id') not in existing_ids]

    if not new_files:
        print("4. 추가할 신규 파일이 없습니다.")
        print(f"\n최종 요약: 신규 추가 0개 / 삭제 {len(rows_to_delete)}개")
        print("=== [동기화 작업 종료] ===")
        return

    new_file_names = [f.get('name', '') for f in new_files]
    print(f"4. 신규 파일 {len(new_files)}개 발견! AI 설명 생성 및 Revit 내부 버전 추출을 시작합니다...")
    
    ai_metadata_map = generate_metadata_with_ai(new_file_names, gemini_api_key)

    new_rows = []
    for file in new_files:
        file_id = file.get('id')
        file_name = file.get('name', '')
        
        extension = file.get('fileExtension', '').lower()
        if not extension and '.' in file_name:
            extension = file_name.rsplit('.', 1)[-1].lower()
            
        drive_link = file.get('webViewLink', '')
        modified_time = file.get('modifiedTime', '')
        updated_at = modified_time.split('T')[0] if 'T' in modified_time else modified_time

        meta = ai_metadata_map.get(file_name, {})
        category = meta.get("category", "")
        ai_sw_version = meta.get("software_version", "")
        description = meta.get("description", "")

        # [과제 2 작동] Revit 파일인 경우 바이너리에서 실제 버전을 추출하여 우선 적용
        binary_version = extract_revit_version_from_binary(drive_service, file_id, extension)
        final_version = binary_version if binary_version else ai_sw_version

        print(f" - [{file_name}] -> 버전: {final_version if final_version else '미상'} | 분류: {category}")

        row = [
            file_id,
            file_name,
            extension,
            category,
            final_version,
            description,
            drive_link,
            updated_at
        ]
        new_rows.append(row)

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')

    print(f"\n최종 요약: 신규 추가 {len(new_rows)}개 / 삭제 {len(rows_to_delete)}개")
    print("=== [동기화 작업 종료] ===")

if __name__ == "__main__":
    sync_drive_to_sheets()