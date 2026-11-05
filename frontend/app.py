import streamlit as st
import sys
import os
import io
import json
from contextlib import redirect_stdout
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
load_dotenv()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")
GOOGLE_KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SHEET_ID = os.getenv("SPREADSHEET_ID")

from backend.bim_agent import (
    get_collection,
    get_ai_response,
    get_cached_sheet_records,
    get_indexed_manual_list,
    get_indexed_categories,
    get_vector_db_status,
    generate_revit_shared_param_file,
    generate_revit_setup_script,
    generate_dynamo_player_file,
    install_pyrevit_extension,
    check_revit_connection,
    execute_in_revit_with_self_healing,
    run_live_model_qa
)
from data_pipeline.sync_bim_metadata import sync_drive_to_sheets
from data_pipeline.ingest_manuals import (
    ingest_manuals_to_chroma,
    export_vector_db_documentation,
    EXCEL_REPORT_PATH,
    MD_SPECS_DIR
)

st.set_page_config(page_title="BIM 데이터 어시스턴트", layout="wide")

# --- [좌측 사이드바: 일반 제어 및 관리자 모드 인증] ---
with st.sidebar:
    st.header("시스템 메뉴")
    if st.button("대화 내용 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    st.divider()
    st.subheader("관리자 모드 전환")
    admin_input = st.text_input("관리자 비밀번호 입력", type="password", placeholder="비밀번호 입력 시 관리 탭 활성화")
    is_admin = (admin_input == ADMIN_PASSWORD)

    if is_admin:
        st.success("관리자 모드 활성화됨 (상단 관리 탭 3종 잠금 해제)")

        st.markdown("**1. BIM 파일 목록 동기화 (Sheets)**")
        if st.button("BIM 파일 동기화 실행", use_container_width=True):
            with st.spinner("구글 드라이브 스캔 및 AI 메타데이터 생성 중..."):
                log_buffer = io.StringIO()
                try:
                    with redirect_stdout(log_buffer):
                        sync_drive_to_sheets()
                    get_cached_sheet_records(force_refresh=True)
                    st.success("BIM 파일 동기화 및 캐시 갱신 완료")
                except Exception as e:
                    st.error("동기화 중 오류 발생: {0}".format(e))
                with st.expander("실행 상세 로그 보기", expanded=True):
                    st.code(log_buffer.getvalue(), language="text")

        st.markdown("**2. 매뉴얼/문서 지식 DB 동기화**")
        force_rebuild = st.checkbox("기존 Vector DB 초기화 후 v3.2 규격으로 전체 재구축")
        if st.button("매뉴얼 Vector DB 동기화 (+자동 문서화)", use_container_width=True):
            with st.spinner("9대 표준 분류 태깅, 임베딩 및 3중 관리 문서(Sheets/Excel/MD) 생성 중..."):
                log_buffer = io.StringIO()
                try:
                    with redirect_stdout(log_buffer):
                        ingest_manuals_to_chroma(force_rebuild=force_rebuild)
                    st.success("Vector DB 동기화 및 3중 문서화 완료")
                except Exception as e:
                    st.error("벡터 DB 동기화 중 오류 발생: {0}".format(e))
                with st.expander("실행 상세 로그 보기", expanded=True):
                    st.code(log_buffer.getvalue(), language="text")

        st.markdown("**3. 시트 캐시 및 문서화 즉시 갱신**")
        if st.button("시트 캐시 & DB 관리대장 즉시 갱신", use_container_width=True):
            try:
                records = get_cached_sheet_records(force_refresh=True)
                col = get_collection()
                if col:
                    export_vector_db_documentation(col, GOOGLE_KEY_PATH, SHEET_ID)
                st.success("갱신 완료 (BIM 파일 {0}개 및 Vector DB 문서 대장 최신화)".format(len(records)))
            except Exception as e:
                st.error("갱신 실패: {0}".format(e))
    elif admin_input != "":
        st.error("비밀번호가 일치하지 않습니다.")
    else:
        st.caption("일반 이용자 모드입니다. 관리자 암호 입력 시 모델 검수(QA), 파일 카탈로그, Vector DB 구조 검증 탭이 열립니다.")


def render_main_chatbot_ui(manual_list, category_list, admin_mode=False):
    """일반 이용자 및 관리자 공통 메인 화면: AI 검색 및 문서 비교 챗봇 UI를 렌더링합니다."""
    col_cat, col_doc, col_info = st.columns([2, 2, 2])

    with col_cat:
        selected_category = st.selectbox(
            "업무 목적별 9대 문서 분류 필터",
            options=["전체 분류"] + category_list
        )
    with col_doc:
        selected_manuals = st.multiselect(
            "비교/참조할 특정 파일 지정 (복수 선택 시 1:1 균등 비교 작동)",
            options=manual_list,
            placeholder="비워두면 전체 문서에서 자동 분산 검색"
        )
    with col_info:
        st.caption("현재 학습된 표준 문서: **{0}개**".format(len(manual_list)))
        st.caption("비교할 문서 2개 이상을 선택하거나 질문하면 표로 비교·요약해 드립니다.")

    st.divider()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for idx, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant":
                st.download_button(
                    label="답변 내용 다운로드 (.md)",
                    data=message["content"].encode("utf-8-sig"),
                    file_name="BIM_AI_답변_{0}.md".format(idx),
                    mime="text/markdown",
                    key="dl_{0}".format(idx)
                )

    if prompt := st.chat_input("질문을 입력하세요 (예: 조달청 지침서와 사내 기준 비교해줘 / 키움 단열 수량산출서 수식 알려줘)"):
        previous_history = list(st.session_state.messages)

        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("사내 지식 저장소와 구글 드라이브를 교차 분석하고 있습니다..."):
                try:
                    response, manual_ctx, file_ctx = get_ai_response(
                        prompt,
                        chat_history=previous_history,
                        return_context=True,
                        target_manuals=selected_manuals,
                        target_category=selected_category
                    )
                    st.markdown(response)

                    st.download_button(
                        label="답변 내용 다운로드 (.md)",
                        data=response.encode("utf-8-sig"),
                        file_name="BIM_AI_최신답변.md",
                        mime="text/markdown",
                        key="dl_latest"
                    )

                    if admin_mode:
                        with st.expander("[관리자 전용] 시스템 DB 검색 원본 결과 확인"):
                            st.markdown("**[적용 필터 - 분류: {0} / 지정 문서: {1}]**".format(
                                selected_category, selected_manuals if selected_manuals else "자동 분산 검색"
                            ))
                            st.markdown("**[구글 시트 (BIM 파일) 검색 결과]**")
                            st.text(file_ctx)
                            st.markdown("**[ChromaDB (매뉴얼/산출서/세팅기준) 검색 결과]**")
                            st.text(manual_ctx)

                except Exception as e:
                    response = "데이터베이스 연결 또는 검색 중 오류가 발생했습니다.\n에러 내용: {0}".format(e)
                    st.error(response)

        st.session_state.messages.append({"role": "assistant", "content": response})


# --- [메인 화면 헤더] ---
st.title("BIM 데이터 어시스턴트")
manual_list = get_indexed_manual_list()
category_list = get_indexed_categories()

# --- [역할 기반 화면 분기: 일반 이용자(단독 메인 화면) vs 관리자(4대 관리 탭 활성)] ---
if not is_admin:
    st.caption("사내 표준 지침서, 검토보고서 비교, 수량산출/견적 기준 검색 및 BIM 패밀리/템플릿 링크를 찾아드립니다.")
    render_main_chatbot_ui(manual_list, category_list, admin_mode=False)

else:
    st.caption("[관리자 모드 실행 중] 챗봇 메인 화면 외에 Revit 제어·모델 검수(QA), 전체 파일 카탈로그, Vector DB 구조 검증 및 문서화 탭을 관리할 수 있습니다.")
    tab_chat, tab_generator, tab_catalog, tab_inspector = st.tabs([
        "AI 검색 및 문서 비교 챗봇 (메인)",
        "[관리자] Revit 무개입 제어 및 양방향 검수(QA)",
        "[관리자] 전체 BIM 파일 카탈로그",
        "[관리자] Vector DB 데이터 구조 및 문서화 관리"
    ])

    # 탭 1: 메인 챗봇
    with tab_chat:
        render_main_chatbot_ui(manual_list, category_list, admin_mode=True)

    # 탭 2: 관리자 전용 Revit 제어 및 실시간 모델 검수(QA)
    with tab_generator:
        st.subheader("표준 가이드라인 및 스펙북 기반 Revit 무개입 Codex 제어 및 양방향 검수기")

        dyn_json_str, dyn_saved_path, live_py_path = generate_dynamo_player_file()
        is_revit_online, revit_status_msg = check_revit_connection()

        with st.container(border=True):
            col_st1, col_st2, col_st3 = st.columns([2, 2, 2])
            with col_st1:
                st.markdown("#### 1. 실시간 Revit 연결 상태")
                if is_revit_online:
                    st.success("무개입 Codex 활성: {0}".format(revit_status_msg))
                else:
                    st.warning("상태: {0}".format(revit_status_msg))
                if st.button("연결 상태 새로고침", use_container_width=True):
                    st.rerun()

            with col_st2:
                st.markdown("#### 2. 무개입 수신기 원클릭 설치 (pyRevit)")
                st.caption("클릭 시 `%APPDATA%\\pyRevit\\Extensions`에 양방향 수신기(/execute 및 /inspect)를 자동 설치합니다.")
                if st.button("pyRevit 양방향 수신기 자동 설치/업데이트", use_container_width=True):
                    paths, _ = install_pyrevit_extension()
                    st.success("수신기 설치 완료: " + ", ".join(paths))

            with col_st3:
                st.markdown("#### 3. Dynamo 플레이어 실행기 (대체용)")
                st.caption("pyRevit 없이 사용할 경우 Dynamo 플레이어에 `{0}` 폴더를 등록해 사용하세요.".format(os.path.dirname(dyn_saved_path)))
                st.download_button(
                    label="Dynamo 실행기 다운로드 (.dyn)",
                    data=dyn_json_str.encode("utf-8"),
                    file_name="BF_AI세팅실행기.dyn",
                    mime="application/json",
                    use_container_width=True
                )

        st.divider()

        gen_manuals = st.multiselect(
            "참조 및 대조할 기준 문서 선택 (예: 조달청 지침서, 스펙북, BF_SM_공유 매개변수.txt, 검토리스트 등)",
            options=manual_list,
            placeholder="비워두면 관련 기준 문서를 AI가 자동으로 찾아 참조합니다",
            key="gen_multiselect"
        )

        col_gen1, col_gen2 = st.columns(2)

        with col_gen1:
            st.markdown("### A. Revit 공유 매개변수 파일(`.txt`) 생성")
            st.caption("사내 공유 매개변수 포맷과 조달청/발주처 지침을 결합하여 레빗에 바로 로드 가능한 신규 `.txt` 파일을 생성합니다.")
            param_req = st.text_area(
                "추가하거나 반영할 매개변수 요구사항 입력",
                value="기존 사내 공유 매개변수에 더해, 조달청 BIM 납품 기준에 필요한 구조/토목/건축 필수 속성(분류코드, 시공구역, 콘크리트강도, 물량산출여부 등)을 추가해서 공유 매개변수 파일을 만들어줘.",
                height=140
            )
            if st.button("공유 매개변수 (.txt) 파일 생성", use_container_width=True):
                with st.spinner("가이드라인 분석 및 Revit 표준 포맷(.txt) 조립 중..."):
                    try:
                        txt_content, param_preview = generate_revit_shared_param_file(param_req, target_manuals=gen_manuals)
                        st.success("총 {0}개의 매개변수가 포함된 파일이 생성되었습니다.".format(len(param_preview)))
                        st.dataframe(param_preview, use_container_width=True, hide_index=True)
                        st.download_button(
                            label="생성된 공유 매개변수 파일 다운로드 (BF_Custom_SharedParams.txt)",
                            data=txt_content.encode("utf-16"),
                            file_name="BF_Custom_SharedParams.txt",
                            mime="text/plain",
                            use_container_width=True
                        )
                    except Exception as e:
                        st.error("파일 생성 중 오류 발생: {0}".format(e))

        with col_gen2:
            st.markdown("### B. 템플릿(레벨/그리드/뷰) 및 재료 완전 무개입 Codex 실행")
            st.caption("버튼을 누르면 열려 있는 Revit에 AI가 직접 접속해 모델을 세팅하며, 에러 발생 시 최대 3회까지 스스로 코드를 고쳐 재실행합니다.")
            script_req = st.text_area(
                "프로젝트 초기 세팅 및 스펙북 재료 요구사항 입력",
                value="프로젝트명 '신규 사옥 신축공사', 층고 4200mm 기준 지상 1층~지상 5층 레벨 생성, X축 8000mm 간격 그리드 5개(1~5열) 및 Y축 8000mm 간격 그리드 4개(A~D열) 생성, 스펙북 기준 외부 화강석(RGB 190,190,185) 및 로이복층유리(RGB 150,200,220, 투명도 70) 재료를 자동 생성해줘.",
                height=140
            )
            if st.button("완전 무개입 Codex 실행 (Revit 실시간 직접 제어)", use_container_width=True):
                with st.spinner("AI 코드 생성 및 Revit 실시간 주입/자율 디버깅 수행 중..."):
                    try:
                        success, logs, full_desc, final_code = execute_in_revit_with_self_healing(
                            script_req,
                            target_manuals=gen_manuals,
                            max_attempts=3
                        )
                        if success:
                            st.success("Revit 무개입 자동 세팅이 완료되었습니다! Revit 화면을 확인하세요.")
                        else:
                            st.warning("실시간 직접 제어 대신 로컬 브릿지(live_setup.py)에 코드가 저장되었습니다. 아래 로그를 확인하세요.")

                        with st.expander("Codex 실시간 실행 및 자율 디버깅 로그", expanded=True):
                            st.code("\n".join(logs), language="text")

                        st.download_button(
                            label="최종 검증된 Revit 세팅 스크립트 다운로드 (Revit_Auto_Setup.py)",
                            data=final_code.encode("utf-8-sig"),
                            file_name="Revit_Auto_Setup.py",
                            mime="text/x-python",
                            use_container_width=True
                        )
                        with st.expander("세팅 내역 설명 및 최종 파이썬 코드 확인"):
                            st.markdown(full_desc)
                            st.code(final_code, language="python")
                    except Exception as e:
                        st.error("Codex 실행 중 오류 발생: {0}".format(e))

        st.divider()

        st.markdown("### C. 실시간 Revit 모델 양방향 검수 (Live Model QA & Auto-Fix)")
        st.caption("현재 열려 있는 Revit 모델의 내부 데이터(레벨, 그리드, 공유 매개변수 바인딩, 재료 RGB 등)를 실시간으로 읽어와 Vector DB 기준 문서와 대조 검수합니다.")

        qa_focus_input = st.text_input(
            "중점 검수 항목 입력",
            value="현재 열려 있는 Revit 모델의 프로젝트 정보, 레벨/그리드 구성, 사내 표준 공유 매개변수 바인딩 여부, 재료 세팅 상태를 기준 문서와 비교해서 검수표를 작성해줘."
        )

        if st.button("열려 있는 Revit 모델 실시간 스캔 및 AI 표준 검수 실행", use_container_width=True):
            with st.spinner("실행 중인 Revit 모델 내부 데이터를 스캔하고 Vector DB 기준 문서와 대조 중입니다..."):
                qa_ok, snapshot_data, qa_report_md, fix_code = run_live_model_qa(qa_focus_input, target_manuals=gen_manuals)
                st.session_state["last_qa_ok"] = qa_ok
                st.session_state["last_qa_snapshot"] = snapshot_data
                st.session_state["last_qa_report"] = qa_report_md
                st.session_state["last_qa_fix_code"] = fix_code

        if "last_qa_report" in st.session_state and st.session_state["last_qa_report"]:
            if st.session_state.get("last_qa_ok"):
                st.success("실시간 Revit 모델 스캔 및 AI 양방향 검수 완료")
                with st.expander("1. Revit에서 실시간 추출된 모델 원본 스냅샷 (JSON 데이터)", expanded=False):
                    st.json(st.session_state["last_qa_snapshot"])

                st.markdown("#### 2. AI 표준 대조 검수 보고서")
                st.markdown(st.session_state["last_qa_report"])

                col_qa_btn1, col_qa_btn2 = st.columns(2)
                with col_qa_btn1:
                    st.download_button(
                        label="AI 모델 검수 보고서 다운로드 (Revit_Live_QA_Report.md)",
                        data=st.session_state["last_qa_report"].encode("utf-8-sig"),
                        file_name="Revit_Live_QA_Report.md",
                        mime="text/markdown",
                        use_container_width=True
                    )
                with col_qa_btn2:
                    if st.session_state.get("last_qa_fix_code"):
                        if st.button("검수 지적사항 Revit 모델에 즉시 자동 보정(Auto-Fix) 실행", use_container_width=True):
                            with st.spinner("검수 지적사항 보정 코드를 Revit에 실시간 주입하고 있습니다..."):
                                fix_ok, fix_logs, _, _ = execute_in_revit_with_self_healing(
                                    "양방향 검수 지적사항 자동 보정",
                                    target_manuals=gen_manuals,
                                    max_attempts=3,
                                    custom_code=st.session_state["last_qa_fix_code"]
                                )
                                if fix_ok:
                                    st.success("검수 지적사항이 Revit 모델에 자동으로 보정되었습니다!")
                                else:
                                    st.warning("자동 보정 중 일부 항목을 브릿지에 저장했습니다. 로그를 확인하세요.")
                                with st.expander("자동 보정 실행 로그", expanded=True):
                                    st.code("\n".join(fix_logs), language="text")
            else:
                st.error(st.session_state["last_qa_report"])

    # 탭 3: 관리자 전용 전체 BIM 파일 카탈로그
    with tab_catalog:
        st.subheader("등록된 BIM 파일 및 템플릿 목록 (Google Sheets 1번째 탭 연동)")
        try:
            records = get_cached_sheet_records()
            if records:
                categories = sorted(list({str(r.get("category", "미분류")) for r in records if r.get("category")}))
                selected_cat = st.selectbox("분류(Category) 필터", ["전체 보기"] + categories)

                filtered_records = records if selected_cat == "전체 보기" else [r for r in records if r.get("category") == selected_cat]
                st.caption("조회된 파일 수: {0}개".format(len(filtered_records)))

                st.dataframe(
                    filtered_records,
                    column_order=("file_name", "category", "extension", "software_version", "description", "drive_link", "updated_at"),
                    column_config={
                        "file_name": st.column_config.TextColumn("파일명", width="medium"),
                        "category": st.column_config.TextColumn("분류", width="small"),
                        "extension": st.column_config.TextColumn("확장자", width="small"),
                        "software_version": st.column_config.TextColumn("버전", width="small"),
                        "description": st.column_config.TextColumn("상세 설명", width="large"),
                        "drive_link": st.column_config.LinkColumn("다운로드", display_text="구글 드라이브 열기", width="small"),
                        "updated_at": st.column_config.TextColumn("수정일", width="small"),
                    },
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info("현재 시트에 등록된 BIM 파일 데이터가 없습니다.")
        except Exception as e:
            st.error("카탈로그 데이터를 불러오는 중 오류가 발생했습니다: {0}".format(e))

    # 탭 4: 관리자 전용 Vector DB 데이터 구조 및 3중 문서화 관리
    with tab_inspector:
        st.subheader("Vector DB(ChromaDB v3.2) 데이터 구조 검증 및 3중 문서화 관리")
        st.caption("웹 서버를 켜지 않아도 언제든 확인 가능하도록 Google Sheets('VectorDB_관리대장' 탭), 통합 엑셀(.xlsx), 파일별 1:1 마크다운(.md) 명세서로 자동 문서화합니다.")

        with st.container(border=True):
            st.markdown("#### [Vector DB 3중 문서화 파일 생성 및 다운로드]")
            col_doc_btn1, col_doc_btn2 = st.columns(2)
            with col_doc_btn1:
                if st.button("지금 즉시 3중 관리 문서(Sheets + Excel + 1:1 MD) 최신화 생성", use_container_width=True):
                    with st.spinner("ChromaDB 데이터를 추출하여 Google Sheets, Excel, Markdown 파일을 생성 중입니다..."):
                        col = get_collection()
                        if col:
                            export_vector_db_documentation(col, GOOGLE_KEY_PATH, SHEET_ID)
                            st.success("생성 완료! 구글 시트 'VectorDB_관리대장' 탭 및 로컬 db_docs 폴더를 확인하세요.")
            with col_doc_btn2:
                if os.path.exists(EXCEL_REPORT_PATH):
                    with open(EXCEL_REPORT_PATH, "rb") as ef:
                        st.download_button(
                            label="통합 엑셀 대장 다운로드 (VectorDB_통합관리대장.xlsx)",
                            data=ef.read(),
                            file_name="VectorDB_통합관리대장.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True
                        )
                else:
                    st.info("좌측 버튼을 누르거나 동기화를 실행하면 엑셀 대장 다운로드 버튼이 활성화됩니다.")
            st.caption("1:1 마크다운 명세서 로컬 저장 경로: `{0}`".format(MD_SPECS_DIR))

        summary_list, chunk_details = get_vector_db_status()

        if not summary_list:
            st.info("현재 Vector DB에 적재된 데이터가 없습니다.")
        else:
            total_docs = len(summary_list)
            total_chunks = len(chunk_details)
            total_chars = sum(c["char_length"] for c in chunk_details)
            avg_chunk_len = round(total_chars / total_chunks) if total_chunks > 0 else 0

            m1, m2, m3 = st.columns(3)
            m1.metric("총 벡터화 문서 수", "{0}개".format(total_docs))
            m2.metric("총 분할 조각(Chunk) 수", "{0}개".format(total_chunks))
            m3.metric("조각당 평균 글자 수", "{0}자".format(avg_chunk_len))

            st.divider()

            st.markdown("**1. 문서별 4대 도메인 · 9대 표준 분류 및 드라이브 폴더 경로 현황**")
            st.dataframe(
                summary_list,
                column_order=("domain_group", "doc_category", "folder_path", "file_name", "file_ext", "chunk_count", "avg_chars", "drive_link"),
                column_config={
                    "domain_group": st.column_config.TextColumn("상위 도메인", width="small"),
                    "doc_category": st.column_config.TextColumn("9대 표준 분류", width="medium"),
                    "folder_path": st.column_config.TextColumn("드라이브 폴더 경로", width="medium"),
                    "file_name": st.column_config.TextColumn("문서명", width="large"),
                    "file_ext": st.column_config.TextColumn("형식", width="small"),
                    "chunk_count": st.column_config.NumberColumn("조각 수", width="small"),
                    "avg_chars": st.column_config.NumberColumn("평균 글자 수", width="small"),
                    "drive_link": st.column_config.LinkColumn("원본 파일", display_text="원본 열기", width="small"),
                },
                use_container_width=True,
                hide_index=True
            )

            st.divider()

            st.markdown("**2. 문서 내부 분할 조각(Chunk) 상세 검증기**")
            inspect_file = st.selectbox(
                "상세 구조를 확인할 문서를 선택하세요",
                options=["전체 조각 보기"] + [s["file_name"] for s in summary_list]
            )

            filtered_chunks = chunk_details if inspect_file == "전체 조각 보기" else [c for c in chunk_details if c["file_name"] == inspect_file]

            st.dataframe(
                filtered_chunks,
                column_order=("doc_category", "folder_path", "setup_topic", "chunk_type", "file_name", "location", "char_length", "preview"),
                column_config={
                    "doc_category": st.column_config.TextColumn("표준 분류", width="small"),
                    "folder_path": st.column_config.TextColumn("폴더 경로", width="small"),
                    "setup_topic": st.column_config.TextColumn("세팅/활용 주제", width="medium"),
                    "chunk_type": st.column_config.TextColumn("조각 유형", width="small"),
                    "file_name": st.column_config.TextColumn("파일명", width="medium"),
                    "location": st.column_config.TextColumn("추출 위치", width="small"),
                    "char_length": st.column_config.NumberColumn("글자 수", width="small"),
                    "preview": st.column_config.TextColumn("텍스트 미리보기 (앞 120자)", width="large"),
                },
                use_container_width=True,
                hide_index=True
            )

            if filtered_chunks:
                options_list = ["[{0}] ({1} / {2}자) - {3}".format(c['location'], c['setup_topic'], c['char_length'], c['file_name']) for c in filtered_chunks]
                selected_loc = st.selectbox("전문(Full Text)을 확인할 조각 위치를 선택하세요", options=options_list)
                target_idx = options_list.index(selected_loc)
                st.text_area(
                    "실제 Vector DB에 저장된 텍스트 원문 (상단 문맥 헤더 포함)",
                    value=filtered_chunks[target_idx]["full_content"],
                    height=220,
                    disabled=True
                )
