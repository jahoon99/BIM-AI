import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

def sync_drive_to_sheets():
    print("=== [Step 1-A: BIM 파일 메타데이터 동기화 시작] ===\n")
    
    # 1. 환경변수 로드
    load_dotenv()
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    sheet_id = os.getenv("SPREADSHEET_ID")
    bim_folder_id = os.getenv("BIM_FILE_FOLDER_ID")

    # 2. 구글 스프레드시트 연결 및 첫 번째 시트 열기
    gc = gspread.service_account(filename=key_path)
    sh = gc.open_by_key(sheet_id)
    worksheet = sh.get_worksheet(0)  # 가장 첫 번째 탭 선택

    # 기존에 등록된 file_id 목록 가져오기 (A열 전체 조회 후 1행 헤더 제외)
    existing_ids = set(worksheet.col_values(1)[1:])
    print(f"1. 현재 시트에 등록된 기존 파일 수: {len(existing_ids)}개")

    # 3. 구글 드라이브 API 연결 및 BIM 폴더 내 파일 스캔
    creds = Credentials.from_service_account_file(
        key_path, scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    drive_service = build('drive', 'v3', credentials=creds)

    # 폴더 안의 파일(폴더 자체는 제외, 휴지통 제외) 정보 조회
    query = f"'{bim_folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(
        q=query,
        pageSize=1000,
        fields="files(id, name, fileExtension, webViewLink, modifiedTime)"
    ).execute()
    
    drive_files = results.get('files', [])
    print(f"2. 구글 드라이브 BIM 폴더에서 스캔된 파일 수: {len(drive_files)}개\n")

    # 4. 신규 파일 필터링 및 데이터 행(Row) 구성
    new_rows = []
    for file in drive_files:
        file_id = file.get('id')
        
        # 이미 시트에 있는 파일이면 건너뛰기 (중복 방지)
        if file_id in existing_ids:
            continue
            
        file_name = file.get('name', '')
        # fileExtension 필드가 비어있을 경우 파일명에서 직접 추출
        extension = file.get('fileExtension', '')
        if not extension and '.' in file_name:
            extension = file_name.rsplit('.', 1)[-1].lower()
            
        drive_link = file.get('webViewLink', '')
        # 날짜 포맷 정리 (예: 2026-10-04T08:30:00.000Z -> 2026-10-04)
        modified_time = file.get('modifiedTime', '')
        updated_at = modified_time.split('T')[0] if 'T' in modified_time else modified_time

        # 8개 표준 컬럼 순서에 맞춰 리스트 생성
        # [A:file_id, B:file_name, C:extension, D:category, E:software_version, F:description, G:drive_link, H:updated_at]
        row = [
            file_id,
            file_name,
            extension,
            "",  # D열: category (추후 입력 또는 자동화)
            "",  # E열: software_version (추후 입력 또는 자동화)
            "",  # F열: description (추후 수동 입력)
            drive_link,
            updated_at
        ]
        new_rows.append(row)

    # 5. 시트에 신규 데이터 일괄 추가
    if new_rows:
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        print(f"3. 동기화 완료: 총 {len(new_rows)}개의 신규 파일 정보가 시트에 추가되었습니다.")
    else:
        print("3. 동기화 완료: 새로 추가할 파일이 없습니다. (모두 최신 상태입니다)")

    print("\n=== [동기화 작업 종료] ===")

if __name__ == "__main__":
    sync_drive_to_sheets()