import os
import re
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)


def _clean_env(key, default=""):
    val = os.getenv(key, default)
    return val.strip().strip('"').strip("'").strip() if val else default


SLACK_BOT_TOKEN = _clean_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _clean_env("SLACK_APP_TOKEN")
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

from backend.bim_agent import (
    get_ai_response,
    download_referenced_excel_files,
    generate_revit_shared_param_file,
    generate_revit_setup_script
)
from data_pipeline.hybrid_server_sync import (
    TAXONOMY_USAGE,
    TAXONOMY_METHOD,
    TAXONOMY_DISCIPLINE,
    TAXONOMY_TASK
)

app = App(token=SLACK_BOT_TOKEN)


def convert_md_to_slack_mrkdwn(text):
    if not text:
        return ""
    lines = text.splitlines()
    converted = []
    in_table = False
    for line in lines:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            if not in_table:
                converted.append("```")
                in_table = True
            converted.append(line)
            continue
        else:
            if in_table:
                converted.append("```")
                in_table = False
        line_clean = re.sub(r'^#{1,6}\s*(.+)$', r'*\1*', line)
        line_clean = re.sub(r'\*\*(.+?)\*\*', r'*\1*', line_clean)
        converted.append(line_clean)
    if in_table:
        converted.append("```")
    return "\n".join(converted)


def split_for_slack_blocks(mrkdwn_text, limit=2800):
    chunks = []
    current = []
    cur_len = 0
    for line in mrkdwn_text.splitlines():
        if cur_len + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current = [line]
            cur_len = len(line) + 1
        else:
            current.append(line)
            cur_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_ai_guide_and_attachments(client, channel_id, thread_ts, user_query, ai_answer):
    """
    1) 신입사원 눈높이로 작성된 4단계 가이드를 슬랙 블록 한계(3000자)에 맞춰 분할 전송합니다.
    2) 슬랙 텍스트 한계를 보완하기 위해 전체 마크다운 전문 파일(BF_AI_실무가이드_전문.md)을 첨부합니다.
    3) 사내 서버에서 참조된 원본 파일(PDF 지침서·보고서, 엑셀 산출서 등)을 자동으로 찾아 함께 첨부합니다.
    """
    slack_text = convert_md_to_slack_mrkdwn(ai_answer)
    text_chunks = split_for_slack_blocks(slack_text, limit=2800)

    for idx, ch in enumerate(text_chunks):
        header_prefix = "*[BF AI 실무 가이드 답변 ({0}/{1})]*\n".format(idx + 1, len(text_chunks)) if len(text_chunks) > 1 else ""
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=header_prefix + ch
        )

    # 1. 상세 답변 전문 .md 파일 첨부 (슬랙 텍스트 한계 보완)
    try:
        md_content = "# 질의 내용: {0}\n\n{1}".format(user_query, ai_answer)
        client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            content=md_content.encode("utf-8-sig"),
            filename="BF_AI_실무가이드_전문.md",
            title="BF_AI_실무가이드_전문.md",
            initial_comment="슬랙 글자 수 제한 없이 열람하실 수 있는 상세 가이드 마크다운(.md) 전문입니다."
        )
    except Exception as e:
        print("[슬랙 .md 전문 첨부 경고]:", e)

    # 2. 참조된 사내 서버 원본 파일(PDF, XLSX, DOCX, PPTX 등) 즉시 첨부
    ref_files = download_referenced_excel_files(ai_answer, max_files=3)
    for fname, fbytes in ref_files:
        try:
            client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                content=fbytes,
                filename=fname,
                title=fname,
                initial_comment="사내 서버에서 자동 발췌된 관련 원본 자료입니다: `{0}`".format(fname)
            )
        except Exception as e:
            print("[슬랙 사내서버 파일 첨부 경고] {0}: {1}".format(fname, e))


def open_bf_modal(client, trigger_id, channel_id, initial_text=""):
    usage_opts = [{"text": {"type": "plain_text", "text": k}, "value": k} for k in TAXONOMY_USAGE.keys()]
    method_opts = [{"text": {"type": "plain_text", "text": k}, "value": k} for k in TAXONOMY_METHOD.keys()]
    disc_opts = [{"text": {"type": "plain_text", "text": k}, "value": k} for k in TAXONOMY_DISCIPLINE.keys()]
    task_opts = [{"text": {"type": "plain_text", "text": k}, "value": k} for k in TAXONOMY_TASK.keys()]

    modal_view = {
        "type": "modal",
        "callback_id": "bf_ai_modal_submit",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "비아이엠팩토리 AI 업무 지원"},
        "submit": {"type": "plain_text", "text": "AI 검색 및 파일 요청"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "input",
                "block_id": "blk_usage",
                "optional": True,
                "label": {"type": "plain_text", "text": "① 건물 용도 선택 (다중 선택 가능)"},
                "element": {
                    "type": "multi_static_select",
                    "action_id": "act_usage",
                    "placeholder": {"type": "plain_text", "text": "데이터센터, 공동주택, 업무시설 등"},
                    "options": usage_opts
                }
            },
            {
                "type": "input",
                "block_id": "blk_method",
                "optional": True,
                "label": {"type": "plain_text", "text": "② 적용 공법 선택 (다중 선택 가능)"},
                "element": {
                    "type": "multi_static_select",
                    "action_id": "act_method",
                    "placeholder": {"type": "plain_text", "text": "탑다운, 순타, PC, 모듈"},
                    "options": method_opts
                }
            },
            {
                "type": "input",
                "block_id": "blk_disc",
                "optional": True,
                "label": {"type": "plain_text", "text": "③ 관련 공종 선택 (다중 선택 가능)"},
                "element": {
                    "type": "multi_static_select",
                    "action_id": "act_disc",
                    "placeholder": {"type": "plain_text", "text": "RC, 철골, 단열공사, 방수공사 등"},
                    "options": disc_opts
                }
            },
            {
                "type": "input",
                "block_id": "blk_task",
                "optional": True,
                "label": {"type": "plain_text", "text": "④ 업무 구분 선택 (다중 선택 가능)"},
                "element": {
                    "type": "multi_static_select",
                    "action_id": "act_task",
                    "placeholder": {"type": "plain_text", "text": "설계검토, 수량, 공정시뮬레이션 등"},
                    "options": task_opts
                }
            },
            {
                "type": "input",
                "block_id": "blk_query",
                "optional": True,
                "label": {"type": "plain_text", "text": "⑤ 상세 질문 또는 필요한 파일 입력"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "act_query",
                    "multiline": True,
                    "initial_value": initial_text,
                    "placeholder": {"type": "plain_text", "text": "예: 데이터센터 단열재 수량 산출 기준과 관련 산출서 파일 찾아줘"}
                }
            }
        ]
    }
    client.views_open(trigger_id=trigger_id, view=modal_view)


@app.command("/bf")
def handle_bf_command(ack, body, client):
    ack()
    open_bf_modal(client, body["trigger_id"], body["channel_id"], body.get("text", ""))


@app.command("/bim")
def handle_bim_command(ack, body, client):
    ack()
    open_bf_modal(client, body["trigger_id"], body["channel_id"], body.get("text", ""))


@app.view("bf_ai_modal_submit")
def handle_modal_submission(ack, body, client, view):
    ack()
    channel_id = view.get("private_metadata")
    vals = view["state"]["values"]

    usages = [o["value"] for o in vals["blk_usage"]["act_usage"].get("selected_options", [])]
    methods = [o["value"] for o in vals["blk_method"]["act_method"].get("selected_options", [])]
    discs = [o["value"] for o in vals["blk_disc"]["act_disc"].get("selected_options", [])]
    tasks = [o["value"] for o in vals["blk_task"]["act_task"].get("selected_options", [])]
    q_txt = (vals["blk_query"]["act_query"].get("value") or "").strip()

    tag_parts = []
    if usages:
        tag_parts.append("용도({0})".format(", ".join(usages)))
    if methods:
        tag_parts.append("공법({0})".format(", ".join(methods)))
    if discs:
        tag_parts.append("공종({0})".format(", ".join(discs)))
    if tasks:
        tag_parts.append("업무({0})".format(", ".join(tasks)))

    full_query = "{0} 관련 사내 기준, 수행 사례 및 산출물 파일을 신입사원도 이해하기 쉽게 설명해줘. {1}".format(
        " / ".join(tag_parts) if tag_parts else "전체 표준",
        q_txt
    ).strip()

    init_msg = client.chat_postMessage(
        channel=channel_id,
        text="질문 분석 및 사내 서버 자료 탐색 중입니다...\n> *요청 내용:* `{0}`".format(full_query)
    )
    thread_ts = init_msg["ts"]

    ans = get_ai_response(full_query, channel="Slack_Modal")
    send_ai_guide_and_attachments(client, channel_id, thread_ts, full_query, ans)


@app.event("app_mention")
def handle_app_mention(event, client):
    channel_id = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    raw_q = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
    if not raw_q:
        return
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text="사내 서버 지식베이스 및 산출물을 검색하고 있습니다...")
    ans = get_ai_response(raw_q, channel="Slack_Mention")
    send_ai_guide_and_attachments(client, channel_id, thread_ts, raw_q, ans)


@app.event("message")
def handle_direct_message(event, client):
    if event.get("subtype") or event.get("bot_id"):
        return
    if event.get("channel_type") != "im":
        return
    channel_id = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    raw_q = (event.get("text") or "").strip()
    if not raw_q:
        return
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text="사내 서버 지식베이스 및 산출물을 검색하고 있습니다...")
    ans = get_ai_response(raw_q, channel="Slack_DM")
    send_ai_guide_and_attachments(client, channel_id, thread_ts, raw_q, ans)


if __name__ == "__main__":
    print("[비아이엠팩토리 슬랙 AI 어시스턴트 가동 시작]")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
