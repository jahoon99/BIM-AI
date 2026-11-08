import os
import io
import re
import time
import datetime
import threading
import traceback
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from backend.bim_agent import (
    get_ai_response,
    get_cached_sheet_records,
    get_indexed_manual_list,
    get_indexed_categories,
    generate_revit_shared_param_file,
    generate_revit_setup_script
)
from data_pipeline.sync_bim_metadata import sync_drive_to_sheets
from data_pipeline.ingest_manuals import ingest_manuals_to_chroma

load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()
GOOGLE_KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
SHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()

SYNC_INTERVAL_SECONDS = 21600
SLACK_MAX_TEXT_LEN = 3000
QA_LOG_TAB_TITLE = "질문응답_로그"

_MODAL_META_CACHE = {
    "manuals": [],
    "categories": [],
    "last_updated": 0
}

_USER_NAME_CACHE = {}
_CHANNEL_NAME_CACHE = {}

if not SLACK_BOT_TOKEN.startswith("xoxb-") or not SLACK_APP_TOKEN.startswith("xapp-"):
    raise ValueError(
        "[설정 오류] .env 파일의 SLACK_BOT_TOKEN(xoxb-로 시작)과 "
        "SLACK_APP_TOKEN(xapp-로 시작)이 정확히 입력되었는지 확인하세요."
    )

app = App(token=SLACK_BOT_TOKEN)


def build_direct_download_url(view_url):
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


def ensure_dual_links_in_text(md_text):
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
    if not ai_answer_text or not GOOGLE_KEY_PATH:
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
            GOOGLE_KEY_PATH,
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


def resolve_user_display(client, user_id, fallback_name=None):
    if not user_id:
        return "미상"
    uid_str = str(user_id)

    if uid_str not in _USER_NAME_CACHE:
        try:
            res = client.users_info(user=uid_str)
            if res and res.get("user"):
                u_obj = res["user"]
                prof = u_obj.get("profile", {})
                real_name = (
                    prof.get("real_name_normalized")
                    or prof.get("real_name")
                    or prof.get("display_name_normalized")
                    or prof.get("display_name")
                    or u_obj.get("real_name")
                    or u_obj.get("name")
                )
                if real_name:
                    _USER_NAME_CACHE[uid_str] = real_name
        except Exception as e:
            if "missing_scope" in str(e):
                print(" -> [권한 안내] 사용자 실명 조회를 위해 App Manifest에 'users:read' 추가 후 Reinstall이 필요합니다.")
            if fallback_name:
                _USER_NAME_CACHE[uid_str] = fallback_name

    final_name = _USER_NAME_CACHE.get(uid_str) or fallback_name
    if final_name and final_name != uid_str:
        return "{0} ({1})".format(final_name, uid_str)
    return uid_str


def resolve_channel_display(client, channel_id, fallback_name=None, user_display=None):
    if not channel_id:
        return "미상"
    cid_str = str(channel_id)

    if cid_str.startswith("D"):
        if user_display and "(" in user_display:
            only_name = user_display.split("(")[0].strip()
            return "1:1 개인DM - {0} ({1})".format(only_name, cid_str)
        return "1:1 개인DM ({0})".format(cid_str)

    if fallback_name and fallback_name not in {"directmessage", "privategroup"}:
        _CHANNEL_NAME_CACHE[cid_str] = fallback_name

    if cid_str not in _CHANNEL_NAME_CACHE:
        try:
            res = client.conversations_info(channel=cid_str)
            if res and res.get("channel"):
                ch_name = res["channel"].get("name")
                if ch_name:
                    _CHANNEL_NAME_CACHE[cid_str] = ch_name
        except Exception as e:
            if "missing_scope" in str(e):
                print(" -> [권한 안내] 채널명 조회를 위해 App Manifest에 'channels:read', 'groups:read' 추가 후 Reinstall이 필요합니다.")

    final_ch_name = _CHANNEL_NAME_CACHE.get(cid_str) or fallback_name
    if final_ch_name and final_ch_name != cid_str:
        clean_ch = final_ch_name.lstrip("#")
        return "#{0} ({1})".format(clean_ch, cid_str)
    return "채널 ({0})".format(cid_str)


def extract_uploaded_file_url(upload_res):
    if not upload_res:
        return "", ""
    try:
        file_obj = upload_res.get("file")
        if not file_obj and upload_res.get("files"):
            file_obj = upload_res.get("files")[0]
        if isinstance(file_obj, dict):
            dl_url = file_obj.get("url_private_download") or file_obj.get("url_private") or ""
            perma_url = file_obj.get("permalink") or dl_url
            return dl_url, perma_url
    except Exception:
        pass
    return "", ""


def parse_ai_answer_single_line_and_note(raw_answer):
    if not raw_answer:
        return "내용 없음", "내용 없음", "참조 문서 없음"

    cited_docs = []
    for line in raw_answer.splitlines():
        if "[즉시 다운로드]" in line:
            doc_part = line.split(":")[0].strip(" -•*[]")
            if doc_part and doc_part not in cited_docs:
                cited_docs.append(doc_part)

    if not cited_docs:
        cited_links = re.findall(r'\[([^\]]+)\]\(https?://[^\)]+\)', raw_answer)
        for item in cited_links:
            clean_item = item.replace("즉시 다운로드", "").replace("웹 미리보기", "").replace("원본 보기", "").replace("다운로드", "").strip(" -|[]•:")
            if clean_item and clean_item not in cited_docs:
                cited_docs.append(clean_item)

    text_no_links = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\1', raw_answer)
    clean_text = re.sub(r'[\*#`]+', '', text_no_links)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

    sec1_match = re.search(r'1\.\s*한\s*줄\s*핵심\s*결론\s*\n+(.*?)(?=\n+■|\n+2\.|\Z)', clean_text, re.DOTALL)
    if sec1_match and sec1_match.group(1).strip():
        raw_core = sec1_match.group(1).strip().lstrip("-• ")
    else:
        lines = [line.strip().lstrip("-• ") for line in clean_text.splitlines() if line.strip() and not line.strip().startswith("■")]
        raw_core = lines[0] if lines else clean_text[:100]

    single_line_core = re.sub(r'\s+', ' ', raw_core).strip()
    if len(single_line_core) > 85:
        single_line_core = single_line_core[:82] + "..."

    refs_single_line = ", ".join(cited_docs) if cited_docs else "전체 문서 및 시트 통합 참조"
    note_full_text = "[마우스오버 답변 전문]\n\n" + clean_text[:3500]

    return single_line_core, note_full_text, refs_single_line[:300]


def ensure_qa_log_worksheet():
    if not GOOGLE_KEY_PATH or not SHEET_ID:
        return None
    try:
        gc = gspread.service_account(filename=GOOGLE_KEY_PATH)
        sh = gc.open_by_key(SHEET_ID)
        headers = [
            "일시",
            "실행 채널 (채널명/ID)",
            "요청자 (이름/ID)",
            "작업 유형",
            "적용 분류 필터",
            "지정 참조 문서",
            "질문 원문 (한줄표시)",
            "AI 한줄 결론 (마우스 올리면 전문 팝업)",
            "참조된 사내 문서/파일",
            "답변 전문(.md) 파일 링크"
        ]
        try:
            ws_log = sh.worksheet(QA_LOG_TAB_TITLE)
            first_row = ws_log.row_values(1)
            if first_row != headers:
                ws_log.update(range_name="A1:J1", values=[headers])
        except gspread.exceptions.WorksheetNotFound:
            ws_log = sh.add_worksheet(title=QA_LOG_TAB_TITLE, rows=1000, cols=12)
            ws_log.update(range_name="A1:J1", values=[headers])
            try:
                ws_log.format("A2:J1000", {"wrapStrategy": "CLIP", "verticalAlignment": "MIDDLE"})
            except Exception:
                pass
        return ws_log
    except Exception as e:
        print(" -> [경고] 구글 시트 '{0}' 탭 초기화 중 오류: {1}".format(QA_LOG_TAB_TITLE, e))
        return None


def append_qa_log_to_sheet(client, channel_id, user_id, task_label, category_str, manuals_str, query_text, answer_text, md_download_url="", md_permalink="", fallback_user_name=None, fallback_channel_name=None):
    try:
        ws_log = ensure_qa_log_worksheet()
        if not ws_log:
            return
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_disp = resolve_user_display(client, user_id, fallback_name=fallback_user_name)
        channel_disp = resolve_channel_display(client, channel_id, fallback_name=fallback_channel_name, user_display=user_disp)

        single_core, note_full_text, refs_single = parse_ai_answer_single_line_and_note(answer_text)
        single_query = re.sub(r'\s+', ' ', (query_text or "").strip())

        def safe_cell(val):
            s = re.sub(r'\s+', ' ', str(val or "").strip())
            if s.startswith(("=", "+", "-", "@")):
                return "'" + s
            return s

        target_url = md_download_url or md_permalink
        if target_url:
            safe_url = target_url.replace('"', '%22')
            md_link_cell = '=HYPERLINK("{0}", "[MD 답변전문 다운로드]")'.format(safe_url)
        else:
            md_link_cell = "첨부 없음"

        row_data = [
            now_str,
            safe_cell(channel_disp),
            safe_cell(user_disp),
            safe_cell(task_label),
            safe_cell(category_str),
            safe_cell(manuals_str),
            safe_cell(single_query),
            safe_cell(single_core),
            safe_cell(refs_single),
            md_link_cell
        ]

        append_res = ws_log.append_row(row_data, value_input_option="USER_ENTERED")

        try:
            updated_range = append_res.get("updates", {}).get("updatedRange", "")
            row_match = re.search(r'(\d+)$', updated_range)
            if row_match:
                r_num = int(row_match.group(1))
                ws_log.insert_note("H{0}".format(r_num), note_full_text)
                ws_log.format("A{0}:J{0}".format(r_num), {"wrapStrategy": "CLIP", "verticalAlignment": "MIDDLE"})
        except Exception:
            pass

        print(" -> [로그 기록 완료] 요청자: '{0}' | 채널: '{1}' (1줄 요약 + 마우스오버 메모 + MD 링크 저장)".format(
            user_disp, channel_disp
        ))
    except Exception as e:
        print(" -> [경고] 질문응답 로그 기록 실패: {0}".format(e))


def refresh_modal_meta_cache():
    try:
        _MODAL_META_CACHE["manuals"] = get_indexed_manual_list()
        _MODAL_META_CACHE["categories"] = get_indexed_categories()
        _MODAL_META_CACHE["last_updated"] = time.time()
    except Exception:
        pass


def get_fast_modal_meta():
    if not _MODAL_META_CACHE["categories"] or (time.time() - _MODAL_META_CACHE["last_updated"] > 300):
        refresh_modal_meta_cache()
    return _MODAL_META_CACHE["manuals"], _MODAL_META_CACHE["categories"]


def run_startup_self_diagnosis():
    try:
        refresh_modal_meta_cache()
        ws_log = ensure_qa_log_worksheet()
        log_status = "정상 연결됨 ('{0}' 1줄 고정 + 마우스오버 메모 + MD링크 모드)".format(QA_LOG_TAB_TITLE) if ws_log else "미연결"
        auth_res = app.client.auth_test()
        team_name = auth_res.get("team", "알 수 없음")
        bot_user = auth_res.get("user", "알 수 없음")
        manuals = _MODAL_META_CACHE["manuals"]
        print("-------------------------------------------------------------------------")
        print(" [자가진단 성공] 연결된 슬랙 워크스페이스: {0} (봇 계정: @{1})".format(team_name, bot_user))
        print(" [지원 명령어] 채팅창에 /bf 또는 /bim 입력 시 즉시 팝업창 호출 가능")
        print(" [가독성 엔진] 마크다운 표 -> 슬랙 인용구(>) 카드형 리스트 자동 변환 활성")
        print(" [DB 준비 상태] 현재 Vector DB 검색 가능 문서 수: 총 {0}개".format(len(manuals)))
        print(" [로그 시트 상태] 구글 시트 3번째 탭: {0}".format(log_status))
        print("-------------------------------------------------------------------------")
    except Exception as e:
        print("[자가진단 실패] 슬랙 토큰 인증에 실패했습니다. .env 파일의 토큰 값을 확인하세요: {0}".format(e))


def background_auto_sync_loop():
    """
    [무개입 자동 이어받기 스케줄러]
    서버 실행 60초 후(슬랙 초기 연결 및 팝업 캐싱 완료 직후) 미동기화 파일을 자동으로 추려 1차 이어받기를 수행하고,
    이후 6시간 주기로 계속 자동 점검합니다.
    """
    # 부팅 직후 60초 동안은 슬랙 명령어 응답 속도를 100% 보장하기 위해 대기
    time.sleep(60)
    while True:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("\n[자동 동기화 스케줄러 | {0}] 미동기화 파일 및 남은 조각 자동 이어받기 점검 시작...".format(now_str))
        try:
            sync_drive_to_sheets()
            get_cached_sheet_records(force_refresh=True)
        except Exception as e:
            print(" -> [경고] BIM 파일 시트 동기화 오류: {0}".format(e))
            traceback.print_exc()

        try:
            # force_rebuild=False 이므로 이미 완료된 문서는 0.1초 만에 건너뛰고 미비된 문서만 자동 적재함
            ingest_manuals_to_chroma(force_rebuild=False)
            refresh_modal_meta_cache()
        except Exception as e:
            print(" -> [경고] 매뉴얼 Vector DB 동기화 오류: {0}".format(e))
            traceback.print_exc()

        next_time = (datetime.datetime.now() + datetime.timedelta(seconds=SYNC_INTERVAL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        print("[자동 동기화 완료] 오늘치 할당량 내 적재 완료. 다음 자동 이어받기 시각: {0}\n".format(next_time))
        time.sleep(SYNC_INTERVAL_SECONDS)


def render_table_as_slack_cards(table_lines):
    """
    [핵심 가독성 엔진]
    슬랙 코드블록(```) 안에서 별표(*도서 상이*)가 노출되고 줄바꿈(|)이 깨지는 마크다운 표를 분석하여,
    슬랙 네이티브 볼드체와 좌측 인용구 바(>)가 적용된 '구조화 카드형 리스트'로 자동 변환합니다.
    """
    rows = []
    for line in table_lines:
        stripped = line.strip().strip('|')
        cells = [c.strip() for c in stripped.split('|')]
        if all(re.match(r'^[:\-\s]+$', c) for c in cells if c):
            continue
        rows.append(cells)

    if len(rows) < 2:
        return "\n".join(table_lines)

    headers = [re.sub(r'\*+', '', h).strip() for h in rows[0]]
    card_blocks = []
    circled_nums = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫"]

    for idx, r in enumerate(rows[1:]):
        if not any(r):
            continue
        num_icon = circled_nums[idx] if idx < len(circled_nums) else "{0}.".format(idx + 1)
        first_val = re.sub(r'\*+', '', r[0]).strip() if len(r) > 0 else "항목 {0}".format(idx + 1)
        first_header = headers[0] if len(headers) > 0 else "구분"

        card_lines = ["*{0} [{1}] {2}*".format(num_icon, first_header, first_val)]
        for col_i in range(1, len(r)):
            col_val = re.sub(r'\*+', '', r[col_i]).strip()
            if not col_val:
                continue
            col_hdr = headers[col_i] if col_i < len(headers) else "항목 {0}".format(col_i + 1)
            card_lines.append("> • *{0}:* {1}".format(col_hdr, col_val))
        card_blocks.append("\n".join(card_lines))

    return "\n\n".join(card_blocks)


def convert_markdown_to_slack(md_text):
    """마크다운 텍스트와 표를 슬랙 최적화 레이아웃(카드형 블록 및 볼드 헤더)으로 변환합니다."""
    if not md_text:
        return ""
    converted = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<\2|\1>', md_text)
    converted = re.sub(r'\*\*([^\*]+)\*\*', r'*\1*', converted)
    converted = re.sub(r'^#{1,4}\s*(.+)$', r'*\1*', converted, flags=re.MULTILINE)
    converted = re.sub(r'^■\s*(.+)$', r'\n*■ \1*', converted, flags=re.MULTILINE)
    converted = re.sub(r'^\s*-\s+', r'• ', converted, flags=re.MULTILINE)

    lines = converted.splitlines()
    out_lines = []
    table_buffer = []

    for line in lines:
        stripped = line.strip()
        is_table_row = stripped.startswith('|') and stripped.endswith('|')
        if is_table_row:
            table_buffer.append(stripped)
        else:
            if table_buffer:
                out_lines.append(render_table_as_slack_cards(table_buffer))
                table_buffer = []
            out_lines.append(line)

    if table_buffer:
        out_lines.append(render_table_as_slack_cards(table_buffer))

    return "\n".join(out_lines).strip()


def split_long_slack_message(text, max_len=SLACK_MAX_TEXT_LEN):
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]

    chunks = []
    current_lines = []
    current_len = 0

    for line in text.splitlines():
        if len(line) > max_len:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_len = 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i : i + max_len])
            continue

        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = len(line) + 1
        else:
            current_lines.append(line)
            current_len += len(line) + 1

    if current_lines:
        chunks.append("\n".join(current_lines))

    fixed_chunks = []
    for ch in chunks:
        if ch.count("```") % 2 != 0:
            ch = ch + "\n```"
        fixed_chunks.append(ch)
    return fixed_chunks


def safe_send_slack_answer(client, channel_id, temp_ts, full_slack_text, raw_markdown_answer, thread_ts=None):
    chunks = split_long_slack_message(full_slack_text, max_len=SLACK_MAX_TEXT_LEN)
    first_chunk = chunks[0]
    if len(chunks) > 1:
        first_chunk += "\n\n*(답변 내용이 길어 아래 메시지로 이어집니다 - 1/{0})*".format(len(chunks))

    try:
        client.chat_update(channel=channel_id, ts=temp_ts, text=first_chunk)
    except Exception:
        fallback_short = full_slack_text[:1800]
        if fallback_short.count("```") % 2 != 0:
            fallback_short += "\n```"
        fallback_short += "\n\n*(답변 전문은 아래 첨부된 마크다운 파일을 확인하세요)*"
        client.chat_update(channel=channel_id, ts=temp_ts, text=fallback_short)

    for idx, extra_chunk in enumerate(chunks[1:], start=2):
        post_kwargs = {
            "channel": channel_id,
            "text": "*[이어서 - {0}/{1}]*\n{2}".format(idx, len(chunks), extra_chunk)
        }
        if thread_ts:
            post_kwargs["thread_ts"] = thread_ts
        try:
            client.chat_postMessage(**post_kwargs)
        except Exception:
            pass

    excel_files = download_referenced_excel_files(raw_markdown_answer, max_files=2)
    for excel_name, excel_bytes in excel_files:
        upload_excel_kwargs = {
            "channel": channel_id,
            "content": excel_bytes,
            "filename": excel_name,
            "title": excel_name,
            "initial_comment": "참조된 엑셀 원본 파일입니다 (슬랙에서 바로 다운로드 가능): *{0}*".format(excel_name)
        }
        if thread_ts:
            upload_excel_kwargs["thread_ts"] = thread_ts
        try:
            client.files_upload_v2(**upload_excel_kwargs)
        except Exception:
            pass

    md_dl_url, md_perma_url = "", ""
    upload_kwargs = {
        "channel": channel_id,
        "content": raw_markdown_answer.encode("utf-8-sig"),
        "filename": "BIM_AI_답변전문.md",
        "title": "BIM_AI_답변전문.md",
        "initial_comment": "전체 상세 답변 및 마크다운 표 원본이 수록된 문서입니다."
    }
    if thread_ts:
        upload_kwargs["thread_ts"] = thread_ts
    try:
        up_res = client.files_upload_v2(**upload_kwargs)
        md_dl_url, md_perma_url = extract_uploaded_file_url(up_res)
    except Exception:
        pass

    return md_dl_url, md_perma_url


def fetch_thread_history(client, channel_id, thread_ts, current_ts):
    history = []
    try:
        res = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=8)
        for msg in res.get("messages", []):
            if msg.get("ts") == current_ts:
                continue
            text = re.sub(r'<@[A-Z0-9]+>', '', msg.get("text", "")).strip()
            if not text or "검색 및 분석 중입니다" in text:
                continue
            role = "assistant" if msg.get("bot_id") else "user"
            history.append({"role": role, "content": text})
    except Exception:
        pass
    return history


def infer_auto_task_type(query_text):
    """
    [정밀 의도 분기 패치]
    단순히 '공유 매개변수 파일 목록/링크 알려줘'라고 검색할 때는 통합 검색(search)으로 유지하고,
    명시적인 '!매개변수' 명령어 또는 '생성/만들어/추가해' 요구가 결합된 경우에만 파일 생성으로 분기합니다.
    """
    q_low = (query_text or "").strip().lower()
    if q_low.startswith("!매개변수"):
        return "param"
    if q_low.startswith("!세팅코드"):
        return "script"

    # 단순 목록/링크 탐색 질문이면 무조건 통합 검색(search)으로 유지
    is_search_intent = any(w in q_low for w in ["목록", "링크", "찾아줘", "알려줘", "어떤", "무엇", "비교해줘", "설명해줘"])
    has_gen_intent = any(w in q_low for w in ["생성", "만들어", "짜줘", "작성해", "추가해서 파일"])

    if has_gen_intent and not is_search_intent:
        if any(k in q_low for k in ["매개변수", "파라미터", ".txt"]):
            return "param"
        if any(k in q_low for k in ["세팅 코드", "세팅 스크립트", "파이썬 코드", "파이썬 스크립트", ".py"]):
            return "script"

    return "search"


def build_bim_modal_view(default_task="auto", default_channel=None):
    manual_list, dynamic_categories = get_fast_modal_meta()

    manual_options = []
    for fname in manual_list[:90]:
        display_name = fname[:70] + "..." if len(fname) > 70 else fname
        safe_val = fname[:75]
        manual_options.append({
            "text": {"type": "plain_text", "text": display_name},
            "value": safe_val
        })
    if not manual_options:
        manual_options.append({
            "text": {"type": "plain_text", "text": "현재 등록된 문서가 없습니다"},
            "value": "NONE"
        })

    task_options = [
        {"text": {"type": "plain_text", "text": "0. 전체 통합 검색 (AI 자동 판단 - 질문/비교/BIM파일/코드생성)"}, "value": "auto"},
        {"text": {"type": "plain_text", "text": "1. 매뉴얼/보고서 비교 및 BIM 패밀리 지정 검색"}, "value": "search"},
        {"text": {"type": "plain_text", "text": "2. Revit 공유 매개변수 파일 (.txt) 자동 생성"}, "value": "param"},
        {"text": {"type": "plain_text", "text": "3. Revit 템플릿/재료 초기 세팅 코드 (.py) 생성"}, "value": "script"}
    ]
    initial_task_option = next((opt for opt in task_options if opt["value"] == default_task), task_options[0])

    category_options = [{"text": {"type": "plain_text", "text": "전체 분류"}, "value": "전체 분류"}]
    for cat in dynamic_categories[:40]:
        category_options.append({"text": {"type": "plain_text", "text": cat[:70]}, "value": cat[:75]})

    short_placeholder = (
        "예1(검색/비교): 다룩스와 ACC 가이드북 차이점 비교해줘\n"
        "예2(매개변수): 조달청 기준 구조 필수 매개변수 파일 생성\n"
        "예3(세팅코드): 층고 4200mm 지상 5층 레벨 생성 코드"
    )

    blocks = [
        {
            "type": "input",
            "block_id": "task_block",
            "label": {"type": "plain_text", "text": "수행할 작업 종류 선택 (헷갈릴 경우 0번 '전체 통합 검색' 유지)"},
            "element": {
                "type": "static_select",
                "action_id": "task_select",
                "options": task_options,
                "initial_option": initial_task_option
            }
        },
        {
            "type": "input",
            "block_id": "category_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "업무 목적별 9대 문서 분류 필터 (선택 사항)"},
            "element": {
                "type": "static_select",
                "action_id": "category_select",
                "options": category_options,
                "initial_option": category_options[0]
            }
        },
        {
            "type": "input",
            "block_id": "manuals_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "비교/참조할 특정 문서 지정 (Vector DB 학습 완료 문서 목록)"},
            "element": {
                "type": "multi_static_select",
                "action_id": "manuals_select",
                "placeholder": {"type": "plain_text", "text": "비워두면 전체 문서 및 BIM 시트에서 자동 통합 검색"},
                "options": manual_options
            }
        },
        {
            "type": "input",
            "block_id": "query_block",
            "label": {"type": "plain_text", "text": "질문 내용 또는 파일 생성 요구사항 입력"},
            "element": {
                "type": "plain_text_input",
                "action_id": "query_input",
                "multiline": True,
                "placeholder": {
                    "type": "plain_text",
                    "text": short_placeholder[:145]
                }
            }
        }
    ]

    channel_element = {
        "type": "conversations_select",
        "action_id": "channel_select",
        "default_to_current_conversation": True,
        "placeholder": {"type": "plain_text", "text": "결과를 받을 채널 또는 DM 선택"}
    }
    if default_channel:
        channel_element["initial_conversation"] = default_channel

    blocks.append({
        "type": "input",
        "block_id": "channel_block",
        "optional": True,
        "label": {"type": "plain_text", "text": "결과를 전송받을 대화방 선택"},
        "element": channel_element
    })

    return {
        "type": "modal",
        "callback_id": "bim_assistant_modal_submit",
        "title": {"type": "plain_text", "text": "BIM 데이터 어시스턴트"},
        "submit": {"type": "plain_text", "text": "실행하기"},
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": blocks
    }


def publish_app_home(client, user_id):
    manual_list, _ = get_fast_modal_meta()
    try:
        bim_records = get_cached_sheet_records()
        bim_count = len(bim_records)
    except Exception:
        bim_count = 0

    manual_preview = ", ".join(manual_list[:10]) if manual_list else "등록된 문서 없음"

    home_blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "BIM 데이터 어시스턴트 제어판"}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "사내 9대 표준 문서(지침서, 검토보고서, 수량산출서, 견적서, 기술제안서 등) 비교 검색 및 Revit 세팅 파일(`.txt`, `.py`) 생성을 지원하는 AI 플랫폼입니다.\n"
                    "어느 채팅방에서든 `/bf` (또는 `/bim`)을 입력하거나 아래 버튼을 클릭하여 작업을 시작하세요."
                )
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*[실시간 데이터베이스 연동 현황]*\n"
                    "- *Vector DB 학습 완료 문서 수:* `{0}개` ({1})\n"
                    "- *Google Sheets 등록 BIM 자산(모델/패밀리/템플릿):* `{2}개`".format(len(manual_list), manual_preview, bim_count)
                )
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*[빠른 작업 실행 메뉴]*\n원하는 작업 버튼을 클릭하면 상세 설정 팝업창이 열립니다."}
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "전체 통합 검색 (AI 자동 판단)"},
                    "style": "primary",
                    "action_id": "open_modal_auto"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "문서 비교 및 지정 검색"},
                    "action_id": "open_modal_search"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "공유 매개변수(.txt) 생성"},
                    "action_id": "open_modal_param"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Revit 세팅 코드(.py) 생성"},
                    "action_id": "open_modal_script"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "시트 캐시 새로고침"},
                    "action_id": "refresh_sheet_cache_btn"
                }
            ]
        }
    ]

    client.views_publish(
        user_id=user_id,
        view={"type": "home", "blocks": home_blocks}
    )


def execute_task_and_post(client, channel_id, user_id, task_type, query_text, target_category="전체 분류", target_manuals=None, thread_ts=None, fallback_user_name=None, fallback_channel_name=None):
    post_kwargs = {"channel": channel_id}
    if thread_ts:
        post_kwargs["thread_ts"] = thread_ts

    effective_task = infer_auto_task_type(query_text) if task_type == "auto" else task_type
    manuals_str = ", ".join(target_manuals) if target_manuals else "전체 자동 분산"

    if effective_task == "param":
        temp_msg = client.chat_postMessage(
            text="<@{0}> 요청하신 기준으로 Revit 공유 매개변수(`.txt`) 파일을 생성하고 있습니다...".format(user_id),
            **post_kwargs
        )
        try:
            txt_content, param_list = generate_revit_shared_param_file(query_text, target_manuals=target_manuals)
            upload_kwargs = {
                "channel": channel_id,
                "content": txt_content.encode("utf-16"),
                "filename": "BF_Custom_SharedParams.txt",
                "title": "Revit 공유 매개변수 파일 (BF_Custom_SharedParams.txt)",
                "initial_comment": (
                    "*[공유 매개변수 파일 생성 완료]* (요청자: <@{0}>)\n"
                    "• *요구사항:* {1}\n"
                    "• *생성된 매개변수 수:* 총 `{2}개` (UTF-16 Revit 표준 포맷)".format(user_id, query_text, len(param_list))
                )
            }
            if thread_ts:
                upload_kwargs["thread_ts"] = thread_ts
            up_res = client.files_upload_v2(**upload_kwargs)
            dl_url, perma_url = extract_uploaded_file_url(up_res)
            client.chat_delete(channel=channel_id, ts=temp_msg["ts"])
            append_qa_log_to_sheet(
                client, channel_id, user_id, "공유 매개변수(.txt) 생성", target_category,
                manuals_str, query_text,
                "■ 1. 한 줄 핵심 결론\n총 {0}개 공유 매개변수(.txt) 파일 생성 완료\n■ 2. 상세 내역\nRevit 관리 탭 -> 공유 매개변수에서 파일을 연결해 사용하세요.\n■ 4. 관련 사내 문서 및 BIM 파일 바로가기\n- BF_Custom_SharedParams.txt".format(len(param_list)),
                md_download_url=dl_url, md_permalink=perma_url,
                fallback_user_name=fallback_user_name, fallback_channel_name=fallback_channel_name
            )
        except Exception as e:
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text="매개변수 파일 생성 중 오류가 발생했습니다: {0}".format(e))

    elif effective_task == "script":
        temp_msg = client.chat_postMessage(
            text="<@{0}> 요청하신 프로젝트 세팅 기준으로 Revit API 자동화 스크립트(`.py`)를 생성하고 있습니다...".format(user_id),
            **post_kwargs
        )
        try:
            full_desc, py_code, _ = generate_revit_setup_script(query_text, target_manuals=target_manuals)
            slack_desc = convert_markdown_to_slack(full_desc)
            upload_kwargs = {
                "channel": channel_id,
                "content": py_code.encode("utf-8-sig"),
                "filename": "Revit_Auto_Setup.py",
                "title": "Revit 자동 세팅 스크립트 (Revit_Auto_Setup.py)",
                "initial_comment": (
                    "*[Revit 자동 세팅 스크립트 생성 완료]* (요청자: <@{0}>)\n"
                    "• *요구사항:* {1}\n\n{2}".format(user_id, query_text, slack_desc[:1200])
                )
            }
            if thread_ts:
                upload_kwargs["thread_ts"] = thread_ts
            up_res = client.files_upload_v2(**upload_kwargs)
            dl_url, perma_url = extract_uploaded_file_url(up_res)
            client.chat_delete(channel=channel_id, ts=temp_msg["ts"])
            append_qa_log_to_sheet(
                client, channel_id, user_id, "Revit 세팅 코드(.py) 생성", target_category,
                manuals_str, query_text,
                "■ 1. 한 줄 핵심 결론\nRevit 세팅 코드(.py) 생성 완료: {0}\n\n{1}".format(query_text[:60], full_desc),
                md_download_url=dl_url, md_permalink=perma_url,
                fallback_user_name=fallback_user_name, fallback_channel_name=fallback_channel_name
            )
        except Exception as e:
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text="세팅 스크립트 생성 중 오류가 발생했습니다: {0}".format(e))

    else:
        mode_label = "AI 자동 통합 검색" if task_type == "auto" else "지정 검색"
        filter_info = "모드: {0} | 분류: {1} | 지정문서: {2}".format(
            mode_label, target_category, manuals_str
        )
        temp_msg = client.chat_postMessage(
            text="<@{0}> 사내 Vector DB와 구글 드라이브를 검색 및 분석 중입니다... (`{1}`)".format(user_id, filter_info),
            **post_kwargs
        )
        try:
            chat_history = fetch_thread_history(client, channel_id, thread_ts, temp_msg["ts"]) if thread_ts else None
            raw_ai_answer = get_ai_response(
                query_text,
                chat_history=chat_history,
                return_context=False,
                target_manuals=target_manuals,
                target_category=target_category
            )
            ai_answer = ensure_dual_links_in_text(raw_ai_answer)
            slack_formatted_answer = (
                "========================================\n"
                "*[질문]* {0}\n"
                "• *요청자:* <@{1}>  |  *검색 범위:* `{2}`\n"
                "========================================\n\n{3}".format(
                    query_text, user_id, filter_info, convert_markdown_to_slack(ai_answer)
                )
            )
            md_dl_url, md_perma_url = safe_send_slack_answer(
                client=client,
                channel_id=channel_id,
                temp_ts=temp_msg["ts"],
                full_slack_text=slack_formatted_answer,
                raw_markdown_answer=ai_answer,
                thread_ts=thread_ts
            )
            append_qa_log_to_sheet(
                client, channel_id, user_id, mode_label, target_category,
                manuals_str, query_text, ai_answer,
                md_download_url=md_dl_url, md_permalink=md_perma_url,
                fallback_user_name=fallback_user_name, fallback_channel_name=fallback_channel_name
            )
        except Exception as e:
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text="데이터베이스 검색 중 오류가 발생했습니다: {0}".format(e))


def process_bim_query(event, say, client):
    channel_id = event.get("channel")
    user_id = event.get("user")
    user_ts = event.get("ts")
    thread_ts = event.get("thread_ts", user_ts)

    raw_text = event.get("text", "")
    clean_query = re.sub(r'<@[A-Z0-9]+>', '', raw_text).strip()

    if not clean_query:
        say(
            text=(
                "안녕하세요! *BIM 데이터 어시스턴트*입니다.\n"
                "• *팝업 UI 호출:* 채팅창에 `/bf` (또는 `/bim`)을 입력하면 전체 통합 검색 및 9대 문서 분류 필터 창이 열립니다.\n"
                "• *일반 검색/비교:* `다룩스와 ACC 가이드북 비교해줘` 또는 `키움 단열 수량 비교표 찾아줘`\n"
                "• *공유 매개변수 생성:* `!매개변수 조달청 기준 구조/토목 필수 매개변수 파일 만들어줘`\n"
                "• *Revit 세팅 코드 생성:* `!세팅코드 층고 4.2m 지상 5층 레벨 및 8m 그리드 세팅 코드 만들어줘`"
            ),
            thread_ts=thread_ts
        )
        return

    execute_task_and_post(client, channel_id, user_id, "auto", clean_query, thread_ts=thread_ts)


@app.event("app_home_opened")
def handle_app_home_opened(client, event):
    user_id = event.get("user")
    if user_id:
        publish_app_home(client, user_id)


@app.command("/bim")
@app.command("/bf")
def handle_bim_slash_command(ack, body, client):
    ack()
    trigger_id = body.get("trigger_id")
    channel_id = body.get("channel_id")
    user_id = body.get("user_id")
    user_name = body.get("user_name")
    channel_name = body.get("channel_name")
    if user_id and user_name:
        _USER_NAME_CACHE[user_id] = user_name
    if channel_id and channel_name and channel_name not in {"directmessage", "privategroup"}:
        _CHANNEL_NAME_CACHE[channel_id] = channel_name

    try:
        modal_view = build_bim_modal_view(default_task="auto", default_channel=channel_id)
        client.views_open(trigger_id=trigger_id, view=modal_view)
    except Exception as e:
        print(" -> [모달 호출 오류] {0}".format(e))
        traceback.print_exc()


@app.action("open_modal_auto")
def handle_open_modal_auto(ack, body, client):
    ack()
    client.views_open(trigger_id=body["trigger_id"], view=build_bim_modal_view(default_task="auto"))


@app.action("open_modal_search")
def handle_open_modal_search(ack, body, client):
    ack()
    client.views_open(trigger_id=body["trigger_id"], view=build_bim_modal_view(default_task="search"))


@app.action("open_modal_param")
def handle_open_modal_param(ack, body, client):
    ack()
    client.views_open(trigger_id=body["trigger_id"], view=build_bim_modal_view(default_task="param"))


@app.action("open_modal_script")
def handle_open_modal_script(ack, body, client):
    ack()
    client.views_open(trigger_id=body["trigger_id"], view=build_bim_modal_view(default_task="script"))


@app.action("refresh_sheet_cache_btn")
def handle_refresh_sheet_cache(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    get_cached_sheet_records(force_refresh=True)
    refresh_modal_meta_cache()
    publish_app_home(client, user_id)


@app.view("bim_assistant_modal_submit")
def handle_modal_submission(ack, body, client, view):
    ack()
    user_obj = body.get("user", {})
    user_id = user_obj.get("id")
    fallback_user_name = user_obj.get("name") or user_obj.get("username")
    if user_id and fallback_user_name:
        _USER_NAME_CACHE[user_id] = fallback_user_name

    state_values = view["state"]["values"]

    task_type = state_values["task_block"]["task_select"]["selected_option"]["value"]
    cat_opt = state_values["category_block"]["category_select"].get("selected_option")
    target_category = cat_opt["value"] if cat_opt else "전체 분류"

    selected_man_opts = state_values["manuals_block"]["manuals_select"].get("selected_options", [])
    all_manuals, _ = get_fast_modal_meta()
    target_manuals = []
    for opt in selected_man_opts:
        val = opt["value"]
        if val == "NONE":
            continue
        matched = next((m for m in all_manuals if m == val or m.startswith(val)), val)
        target_manuals.append(matched)

    query_text = state_values["query_block"]["query_input"].get("value", "").strip()

    target_channel = state_values["channel_block"]["channel_select"].get("selected_conversation")
    if not target_channel:
        dm_res = client.conversations_open(users=user_id)
        target_channel = dm_res["channel"]["id"]

    threading.Thread(
        target=execute_task_and_post,
        kwargs={
            "client": client,
            "channel_id": target_channel,
            "user_id": user_id,
            "task_type": task_type,
            "query_text": query_text,
            "target_category": target_category,
            "target_manuals": target_manuals if target_manuals else None,
            "fallback_user_name": fallback_user_name,
            "fallback_channel_name": _CHANNEL_NAME_CACHE.get(target_channel)
        },
        daemon=True
    ).start()


@app.event("app_mention")
def handle_app_mention(event, say, client):
    process_bim_query(event, say, client)


@app.event("message")
def handle_direct_message(event, say, client):
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel", "").startswith("D"):
        process_bim_query(event, say, client)


if __name__ == "__main__":
    print("=========================================================================")
    print("=== [BIM 데이터 어시스턴트 v4.4 - 2배 고속 응답 & 슬랙 카드형 표 변환] ===")
    print("=========================================================================")
    run_startup_self_diagnosis()

    sync_thread = threading.Thread(target=background_auto_sync_loop, daemon=True)
    sync_thread.start()

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()