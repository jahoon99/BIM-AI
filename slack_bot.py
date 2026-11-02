import os
import re
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from backend.bim_agent import (
    get_ai_response,
    get_cached_sheet_records,
    get_indexed_manual_list,
    generate_revit_shared_param_file,
    generate_revit_setup_script
)

# 1. 환경변수 로드 및 토큰 검증
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise ValueError(".env 파일에 SLACK_BOT_TOKEN(xoxb-...)과 SLACK_APP_TOKEN(xapp-...)이 설정되어 있는지 확인하세요.")

app = App(token=SLACK_BOT_TOKEN)


def convert_markdown_to_slack(md_text):
    """표준 마크다운 문법을 슬랙 전용 mrkdwn 문법으로 변환합니다."""
    if not md_text:
        return ""
    
    # 1. 마크다운 링크 [텍스트](URL) -> 슬랙 링크 <URL|텍스트> 변환
    converted = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<\2|\1>', md_text)
    
    # 2. 굵은 글씨 **텍스트** -> *텍스트* 변환
    converted = re.sub(r'\*\*([^\*]+)\*\*', r'*\1*', converted)
    
    # 3. 제목 ### 텍스트 -> *텍스트* 변환
    converted = re.sub(r'^#{1,4}\s*(.+)$', r'*\1*', converted, flags=re.MULTILINE)
    
    # 4. 마크다운 표(| 컬럼 |)가 있을 경우 슬랙에서 줄이 어긋나지 않도록 코드블록(```)으로 감싸기
    lines = converted.splitlines()
    out_lines = []
    in_table = False
    
    for line in lines:
        stripped = line.strip()
        is_table_row = stripped.startswith('|') and stripped.endswith('|')
        if is_table_row and not in_table:
            out_lines.append("```")
            in_table = True
        elif not is_table_row and in_table:
            out_lines.append("```")
            in_table = False
        out_lines.append(line)
        
    if in_table:
        out_lines.append("```")
        
    return "\n".join(out_lines)


def fetch_thread_history(client, channel_id, thread_ts, current_ts):
    """슬랙 스레드의 이전 대화 내역을 가져와 bim_agent의 chat_history 포맷으로 변환합니다."""
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


def build_bim_modal_view(default_task="search", default_channel=None):
    """슬래시 커맨드(/bim) 및 홈 탭 버튼 클릭 시 띄워줄 Block Kit 모달 팝업창 UI를 생성합니다."""
    manual_list = get_indexed_manual_list()
    
    # 슬랙 옵션 텍스트는 최대 75자 제한이 있으므로 안전하게 슬라이싱 처리
    manual_options = []
    for fname in manual_list[:90]:
        display_name = fname[:70] + "..." if len(fname) > 70 else fname
        manual_options.append({
            "text": {"type": "plain_text", "text": display_name},
            "value": fname
        })
    if not manual_options:
        manual_options.append({
            "text": {"type": "plain_text", "text": "현재 등록된 문서가 없습니다"},
            "value": "NONE"
        })

    task_options = [
        {"text": {"type": "plain_text", "text": "1. 매뉴얼/보고서 비교 및 BIM 패밀리 통합 검색"}, "value": "search"},
        {"text": {"type": "plain_text", "text": "2. Revit 공유 매개변수 파일 (.txt) 자동 생성"}, "value": "param"},
        {"text": {"type": "plain_text", "text": "3. Revit 템플릿/재료 초기 세팅 코드 (.py) 생성"}, "value": "script"}
    ]
    initial_task_option = next((opt for opt in task_options if opt["value"] == default_task), task_options[0])

    category_options = [
        {"text": {"type": "plain_text", "text": "전체 분류"}, "value": "전체 분류"},
        {"text": {"type": "plain_text", "text": "표준양식/세팅기준"}, "value": "표준양식/세팅기준"},
        {"text": {"type": "plain_text", "text": "스펙북/자재기준"}, "value": "스펙북/자재기준"},
        {"text": {"type": "plain_text", "text": "검토보고서"}, "value": "검토보고서"},
        {"text": {"type": "plain_text", "text": "검토리스트"}, "value": "검토리스트"},
        {"text": {"type": "plain_text", "text": "수량산출서"}, "value": "수량산출서"},
        {"text": {"type": "plain_text", "text": "매뉴얼/가이드북"}, "value": "매뉴얼/가이드북"}
    ]

    blocks = [
        {
            "type": "input",
            "block_id": "task_block",
            "label": {"type": "plain_text", "text": "수행할 작업 종류 선택"},
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
            "label": {"type": "plain_text", "text": "업무 목적별 문서 분류 필터 (검색 시 적용)"},
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
            "label": {"type": "plain_text", "text": "비교/참조할 특정 문서 지정 (복수 선택 시 1:1 균등 비교)"},
            "element": {
                "type": "multi_static_select",
                "action_id": "manuals_select",
                "placeholder": {"type": "plain_text", "text": "비워두면 전체 문서에서 자동 분산 검색합니다"},
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
                    "text": "예시 1 (검색/비교): 다룩스와 트림블 가이드북의 뷰어 필터 차이점과 초기 세팅법 비교해줘\n예시 2 (매개변수): 조달청 기준 구조/토목 필수 매개변수 추가해서 파일 만들어줘\n예시 3 (세팅코드): 층고 4200mm 지상 5층 레벨 및 8000mm 간격 그리드 생성 코드 만들어줘"
                }
            }
        }
    ]

    # 결과를 전송받을 채널 선택기 (기본값: 명령어를 실행한 채널 또는 본인 DM)
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
    """사용자가 슬랙에서 봇의 '홈(Home)' 탭을 클릭했을 때 대시보드 화면을 렌더링합니다."""
    manual_list = get_indexed_manual_list()
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
                    "사내 표준 양식, 검토보고서 비교, 수량산출 기준 검색 및 Revit 초기 세팅 파일(`.txt`, `.py`) 생성을 지원하는 AI 플랫폼입니다.\n"
                    "어느 채팅방에서든 `/bim`을 입력하거나 아래 버튼을 클릭하여 작업을 시작하세요."
                )
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*[실시간 데이터베이스 연동 현황]*\n"
                    f"- *Vector DB 학습 문서 수:* `{len(manual_list)}개` ({manual_preview})\n"
                    f"- *Google Sheets 등록 BIM 자산(패밀리/템플릿):* `{bim_count}개`"
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
                    "text": {"type": "plain_text", "text": "통합 검색 및 문서 비교"},
                    "style": "primary",
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
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*[채팅창 직접 호출 가이드]*\n"
                    "1. *슬래시 커맨드:* 채팅창에 `/bim` 입력 시 필터 선택 팝업창 호출\n"
                    "2. *채널 멘션:* 채널에서 `@BIM 데이터 어시스턴트 질문내용` 입력\n"
                    "3. *단축 명령어:* `!매개변수 요구사항` 또는 `!세팅코드 요구사항` 입력 시 즉시 파일 첨부"
                )
            }
        }
    ]

    client.views_publish(
        user_id=user_id,
        view={"type": "home", "blocks": home_blocks}
    )


def execute_task_and_post(client, channel_id, user_id, task_type, query_text, target_category="전체 분류", target_manuals=None, thread_ts=None):
    """모달 팝업창 또는 채팅 명령어로 접수된 작업을 수행하고 지정된 슬랙 채널에 결과와 파일을 전송합니다."""
    post_kwargs = {"channel": channel_id}
    if thread_ts:
        post_kwargs["thread_ts"] = thread_ts

    if task_type == "param":
        temp_msg = client.chat_postMessage(
            text=f"<@{user_id}> 요청하신 기준으로 Revit 공유 매개변수(`.txt`) 파일을 생성하고 있습니다...",
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
                    f"*[공유 매개변수 파일 생성 완료]* (요청자: <@{user_id}>)\n"
                    f"- *요구사항:* {query_text}\n"
                    f"- *생성된 매개변수 수:* 총 `{len(param_list)}개` (UTF-16 Revit 표준 포맷)"
                )
            }
            if thread_ts:
                upload_kwargs["thread_ts"] = thread_ts
            client.files_upload_v2(**upload_kwargs)
            client.chat_delete(channel=channel_id, ts=temp_msg["ts"])
        except Exception as e:
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text=f"매개변수 파일 생성 중 오류가 발생했습니다: {e}")

    elif task_type == "script":
        temp_msg = client.chat_postMessage(
            text=f"<@{user_id}> 요청하신 프로젝트 세팅 기준으로 Revit API 자동화 스크립트(`.py`)를 생성하고 있습니다...",
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
                    f"*[Revit 자동 세팅 스크립트 생성 완료]* (요청자: <@{user_id}>)\n"
                    f"- *요구사항:* {query_text}\n\n{slack_desc[:1500]}"
                )
            }
            if thread_ts:
                upload_kwargs["thread_ts"] = thread_ts
            client.files_upload_v2(**upload_kwargs)
            client.chat_delete(channel=channel_id, ts=temp_msg["ts"])
        except Exception as e:
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text=f"세팅 스크립트 생성 중 오류가 발생했습니다: {e}")

    else:
        filter_info = f"분류: {target_category} | 지정문서: {', '.join(target_manuals) if target_manuals else '전체 자동 분산'}"
        temp_msg = client.chat_postMessage(
            text=f"<@{user_id}> 사내 Vector DB와 구글 드라이브를 검색 및 분석 중입니다... (`{filter_info}`)",
            **post_kwargs
        )
        try:
            chat_history = fetch_thread_history(client, channel_id, thread_ts, temp_msg["ts"]) if thread_ts else None
            ai_answer = get_ai_response(
                query_text,
                chat_history=chat_history,
                return_context=False,
                target_manuals=target_manuals,
                target_category=target_category
            )
            slack_formatted_answer = (
                f"*[질문]* {query_text} (요청자: <@{user_id}> | `{filter_info}`)\n\n"
                f"{convert_markdown_to_slack(ai_answer)}"
            )
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text=slack_formatted_answer)

            if "|" in ai_answer and "---" in ai_answer:
                upload_kwargs = {
                    "channel": channel_id,
                    "content": ai_answer.encode("utf-8-sig"),
                    "filename": "BIM_AI_검토결과_표원본.md",
                    "title": "BIM_AI_검토결과_표원본.md",
                    "initial_comment": "표(Table)가 포함된 원본 마크다운 문서입니다."
                }
                if thread_ts:
                    upload_kwargs["thread_ts"] = thread_ts
                client.files_upload_v2(**upload_kwargs)
        except Exception as e:
            client.chat_update(channel=channel_id, ts=temp_msg["ts"], text=f"데이터베이스 검색 중 오류가 발생했습니다: {e}")


def process_bim_query(event, say, client):
    """일반 채팅창(@멘션 또는 DM)에서 텍스트로 입력된 질문을 처리합니다."""
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
                "- *팝업 UI 호출:* 채팅창에 `/bim`을 입력하면 문서 필터 및 기능 선택 창이 열립니다.\n"
                "- *일반 검색/비교:* `키움 사옥 단열 수량 비교표 찾아줘` 또는 `다룩스와 트림블 차이점 비교해줘`\n"
                "- *공유 매개변수 생성:* `!매개변수 조달청 기준 구조/토목 필수 매개변수 파일 만들어줘`\n"
                "- *Revit 세팅 코드 생성:* `!세팅코드 층고 4.2m 지상 5층 레벨 및 8m 그리드 세팅 코드 만들어줘`"
            ),
            thread_ts=thread_ts
        )
        return

    if clean_query.startswith("!매개변수"):
        req_text = clean_query.replace("!매개변수", "", 1).strip() or "사내 표준 및 조달청 기준 필수 공유 매개변수 구성"
        execute_task_and_post(client, channel_id, user_id, "param", req_text, thread_ts=thread_ts)
        return

    if clean_query.startswith("!세팅코드"):
        req_text = clean_query.replace("!세팅코드", "", 1).strip() or "기본 레벨 및 그리드, 재료 초기 세팅"
        execute_task_and_post(client, channel_id, user_id, "script", req_text, thread_ts=thread_ts)
        return

    execute_task_and_post(client, channel_id, user_id, "search", clean_query, thread_ts=thread_ts)


# --- [슬랙 이벤트 및 인터랙션 핸들러 등록] ---

# 1. 슬랙 홈 탭(App Home) 진입 시 대시보드 렌더링
@app.event("app_home_opened")
def handle_app_home_opened(client, event):
    user_id = event.get("user")
    if user_id:
        publish_app_home(client, user_id)


# 2. 슬래시 커맨드(/bim) 입력 시 모달 팝업창 호출
@app.command("/bim")
def handle_bim_slash_command(ack, body, client):
    ack()
    trigger_id = body.get("trigger_id")
    channel_id = body.get("channel_id")
    modal_view = build_bim_modal_view(default_task="search", default_channel=channel_id)
    client.views_open(trigger_id=trigger_id, view=modal_view)


# 3. 홈 탭(App Home)의 빠른 실행 버튼 4종 처리
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
    publish_app_home(client, user_id)


# 4. 모달 팝업창에서 [실행하기] 버튼을 눌렀을 때 데이터 추출 및 작업 실행
@app.view("bim_assistant_modal_submit")
def handle_modal_submission(ack, body, client, view):
    ack()
    user_id = body["user"]["id"]
    state_values = view["state"]["values"]

    task_type = state_values["task_block"]["task_select"]["selected_option"]["value"]
    
    cat_opt = state_values["category_block"]["category_select"].get("selected_option")
    target_category = cat_opt["value"] if cat_opt else "전체 분류"

    selected_man_opts = state_values["manuals_block"]["manuals_select"].get("selected_options", [])
    target_manuals = [opt["value"] for opt in selected_man_opts if opt["value"] != "NONE"]

    query_text = state_values["query_block"]["query_input"].get("value", "").strip()

    target_channel = state_values["channel_block"]["channel_select"].get("selected_conversation")
    if not target_channel:
        # 채널을 지정하지 않은 경우(홈 탭에서 실행 등) 요청자와의 1:1 DM 채널을 자동으로 열어 전송
        dm_res = client.conversations_open(users=user_id)
        target_channel = dm_res["channel"]["id"]

    execute_task_and_post(
        client=client,
        channel_id=target_channel,
        user_id=user_id,
        task_type=task_type,
        query_text=query_text,
        target_category=target_category,
        target_manuals=target_manuals if target_manuals else None
    )


# 5. 일반 채널 멘션(@봇이름) 및 1:1 DM 메시지 처리
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
    print("=================================================================")
    print("=== [BIM 데이터 어시스턴트 - Slack 홈 탭 & /bim 모달 서버 가동] ===")
    print("1. 슬랙 좌측 앱 목록에서 봇 클릭 -> [홈(Home)] 탭 대시보드 확인")
    print("2. 어느 채팅방에서든 '/bim' 입력 -> 대화형 팝업창(Modal) 실행")
    print("=================================================================")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()