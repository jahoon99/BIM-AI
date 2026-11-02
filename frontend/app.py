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

from backend.bim_agent import (
    get_ai_response, 
    get_cached_sheet_records, 
    get_indexed_manual_list,
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
from data_pipeline.ingest_manuals import ingest_manuals_to_chroma

st.set_page_config(page_title="BIM 데이터 어시스턴트", layout="wide")

with st.sidebar:
    st.header("시스템 제어판")
    if st.button("대화 내용 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()
        
    st.divider()
    st.subheader("관리자 데이터 동기화")
    admin_input = st.text_input("관리자 비밀번호 입력", type="password", placeholder="비밀번호를 입력하세요")
    
    if admin_input == ADMIN_PASSWORD:
        st.success("관리자 인증 완료")
        
        st.markdown("**1. BIM 파일 목록 동기화**")
        if st.button("BIM 파일 동기화 실행", use_container_width=True):
            with st.spinner("구글 드라이브 스캔 및 AI 메타데이터 생성 중..."):
                log_buffer = io.StringIO()
                try:
                    with redirect_stdout(log_buffer):
                        sync_drive_to_sheets()
                    get_cached_sheet_records(force_refresh=True)
                    st.success("BIM 파일 동기화 및 캐시 갱신 완료")
                except Exception as e:
                    st.error(f"동기화 중 오류 발생: {e}")
                with st.expander("실행 상세 로그 보기", expanded=True):
                    st.code(log_buffer.getvalue(), language="text")

        st.markdown("**2. 매뉴얼 지식 DB 동기화**")
        force_rebuild = st.checkbox("기존 Vector DB 초기화 후 표준 규격으로 전체 재구축")
        if st.button("매뉴얼 Vector DB 동기화", use_container_width=True):
            with st.spinner("표준 분류 태깅 및 경량화 임베딩 처리 중..."):
                log_buffer = io.StringIO()
                try:
                    with redirect_stdout(log_buffer):
                        ingest_manuals_to_chroma(force_rebuild=force_rebuild)
                    st.success("매뉴얼 Vector DB 동기화 완료")
                except Exception as e:
                    st.error(f"벡터 DB 동기화 중 오류 발생: {e}")
                with st.expander("실행 상세 로그 보기", expanded=True):
                    st.code(log_buffer.getvalue(), language="text")

        st.markdown("**3. 시트 캐시 새로고침**")
        if st.button("시트 데이터 즉시 반영", use_container_width=True):
            try:
                records = get_cached_sheet_records(force_refresh=True)
                st.success(f"캐시 갱신 완료 (총 {len(records)}개 파일)")
            except Exception as e:
                st.error(f"캐시 갱신 실패: {e}")
    elif admin_input != "":
        st.error("비밀번호가 일치하지 않습니다.")
    else:
        st.caption("데이터 동기화 기능은 관리자 암호 입력 후 활성화됩니다.")

st.title("BIM 데이터 어시스턴트")
st.caption("사내 표준 양식, 검토보고서 비교, 레빗 무개입 Codex 제어 및 실시간 양방향 모델 검수(Live QA)를 지원합니다.")

tab_chat, tab_generator, tab_catalog, tab_inspector = st.tabs([
    "AI 검색 및 문서 비교 챗봇", 
    "AI 레빗 무개입 Codex 제어 및 양방향 검수(QA)",
    "전체 BIM 파일 카탈로그", 
    "Vector DB 데이터 구조 및 검증"
])

# --- [탭 1: AI 검색 및 문서 비교 챗봇] ---
with tab_chat:
    manual_list = get_indexed_manual_list()
    col_cat, col_doc, col_info = st.columns([2, 2, 2])
    
    with col_cat:
        selected_category = st.selectbox(
            "업무 목적별 문서 분류 필터",
            options=["전체 분류", "표준양식/세팅기준", "스펙북/자재기준", "검토보고서", "검토리스트", "수량산출서", "매뉴얼/가이드북"]
        )
    with col_doc:
        selected_manuals = st.multiselect(
            "비교/참조할 특정 파일 지정 (복수 선택 시 1:1 균등 비교 작동)",
            options=manual_list,
            placeholder="비워두면 전체 문서에서 자동 분산 검색"
        )
    with col_info:
        st.caption(f"현재 Vector DB 학습 문서: **{len(manual_list)}개**")
        st.caption("비교할 문서 2개 이상을 선택하면 각 문서에서 균등하게 내용을 추출해 비교표를 작성합니다.")

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
                    file_name=f"BIM_AI_답변_{idx}.md",
                    mime="text/markdown",
                    key=f"dl_{idx}"
                )

    if prompt := st.chat_input("질문을 입력하세요 (예: 다룩스와 트림블 가이드북의 뷰어/필터 차이점과 초기 세팅법 비교해줘)"):
        previous_history = list(st.session_state.messages)
        
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("지식 저장소와 구글 드라이브를 교차 분석하고 있습니다..."):
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
                    
                    with st.expander("시스템 DB 검색 원본 결과 확인"):
                        st.markdown(f"**[적용 필터 - 분류: {selected_category} / 지정 문서: {selected_manuals if selected_manuals else '자동 분산 검색'}]**")
                        st.markdown("**[구글 시트 (BIM 파일) 검색 결과]**")
                        st.text(file_ctx)
                        st.markdown("**[ChromaDB (매뉴얼/산출서/세팅기준) 검색 결과]**")
                        st.text(manual_ctx)
                        
                except Exception as e:
                    response = f"데이터베이스 연결 또는 검색 중 오류가 발생했습니다.\n에러 내용: {e}"
                    st.error(response)
                    
        st.session_state.messages.append({"role": "assistant", "content": response})

# --- [탭 2: AI 레빗 무개입 Codex 제어 및 양방향 검수(QA)] ---
with tab_generator:
    st.subheader("표준 가이드라인 및 스펙북 기반 Revit 무개입 Codex 제어 및 양방향 검수기")
    
    dyn_json_str, dyn_saved_path, live_py_path = generate_dynamo_player_file()
    is_revit_online, revit_status_msg = check_revit_connection()
    
    with st.container(border=True):
        col_st1, col_st2, col_st3 = st.columns([2, 2, 2])
        with col_st1:
            st.markdown("#### 1. 실시간 Revit 연결 상태")
            if is_revit_online:
                st.success(f"무개입 Codex 활성: {revit_status_msg}")
            else:
                st.warning(f"상태: {revit_status_msg}")
            if st.button("연결 상태 새로고침", use_container_width=True):
                st.rerun()
                
        with col_st2:
            st.markdown("#### 2. 무개입 수신기 원클릭 설치 (pyRevit)")
            st.caption("클릭 시 `%APPDATA%\\pyRevit\\Extensions`에 양방향 수신기(/execute 및 /inspect)를 자동 설치합니다. 설치 후 Revit에서 pyRevit Reload를 실행하세요.")
            if st.button("pyRevit 양방향 수신기 자동 설치/업데이트", use_container_width=True):
                paths, _ = install_pyrevit_extension()
                st.success("수신기 설치 완료: " + ", ".join(paths))
                
        with col_st3:
            st.markdown("#### 3. Dynamo 플레이어 실행기 (대체용)")
            st.caption(f"pyRevit 없이 사용할 경우 Dynamo 플레이어에 `{os.path.dirname(dyn_saved_path)}` 폴더를 등록해 사용하세요.")
            st.download_button(
                label="Dynamo 실행기 다운로드 (.dyn)",
                data=dyn_json_str.encode("utf-8"),
                file_name="BF_AI세팅실행기.dyn",
                mime="application/json",
                use_container_width=True
            )

    st.divider()

    gen_manuals = st.multiselect(
        "참조 및 대조할 기준 문서 선택 (예: 조달청 가이드라인, 스펙북, BF_SM_공유 매개변수.txt, 검토리스트 등)",
        options=manual_list,
        placeholder="비워두면 관련 기준 문서를 AI가 자동으로 찾아 참조합니다",
        key="gen_multiselect"
    )
    
    col_gen1, col_gen2 = st.columns(2)
    
    # 좌측: 공유 매개변수(.txt) 파일 직접 생성기
    with col_gen1:
        st.markdown("### A. Revit 공유 매개변수 파일(`.txt`) 생성")
        st.caption("사내 공유 매개변수 포맷과 조달청/발주처 가이드라인을 결합하여 레빗에 바로 로드 가능한 신규 `.txt` 파일을 생성합니다.")
        param_req = st.text_area(
            "추가하거나 반영할 매개변수 요구사항 입력",
            value="기존 사내 공유 매개변수에 더해, 조달청 BIM 납품 기준에 필요한 구조/토목/건축 필수 속성(분류코드, 시공구역, 콘크리트강도, 물량산출여부 등)을 추가해서 공유 매개변수 파일을 만들어줘.",
            height=140
        )
        if st.button("공유 매개변수 (.txt) 파일 생성", use_container_width=True):
            with st.spinner("가이드라인 분석 및 Revit 표준 포맷(.txt) 조립 중..."):
                try:
                    txt_content, param_preview = generate_revit_shared_param_file(param_req, target_manuals=gen_manuals)
                    st.success(f"총 {len(param_preview)}개의 매개변수가 포함된 파일이 생성되었습니다.")
                    st.dataframe(param_preview, use_container_width=True, hide_index=True)
                    st.download_button(
                        label="생성된 공유 매개변수 파일 다운로드 (BF_Custom_SharedParams.txt)",
                        data=txt_content.encode("utf-16"),
                        file_name="BF_Custom_SharedParams.txt",
                        mime="text/plain",
                        use_container_width=True
                    )
                except Exception as e:
                    st.error(f"파일 생성 중 오류 발생: {e}")

    # 우측: 완전 무개입 Codex 실행기 (자동 세팅 + 에러 발생 시 AI 자율 디버깅 루프)
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
                    st.error(f"Codex 실행 중 오류 발생: {e}")

    st.divider()

    # 하단: 실시간 Revit 모델 양방향 검수 (Live Model QA & Auto-Fix)
    st.markdown("### C. 실시간 Revit 모델 양방향 검수 (Live Model QA & Auto-Fix)")
    st.caption("현재 열려 있는 Revit 모델의 내부 데이터(레벨, 그리드, 등록된 공유 매개변수, 빈 속성값, 재료 RGB 등)를 실시간으로 읽어와 Vector DB의 기준 문서와 대조 검수합니다.")
    
    qa_focus_input = st.text_input(
        "중점 검수 항목 입력",
        value="현재 열려 있는 Revit 모델의 프로젝트 정보, 레벨/그리드 구성, 사내 표준 공유 매개변수 바인딩 및 속성값 입력 누락 여부, 재료 세팅 상태를 기준 문서와 비교해서 검수표를 작성해줘."
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

# --- [탭 3: 전체 BIM 파일 카탈로그] ---
with tab_catalog:
    st.subheader("등록된 BIM 파일 및 템플릿 목록 (Google Sheets 연동)")
    try:
        records = get_cached_sheet_records()
        if records:
            categories = sorted(list({str(r.get("category", "미분류")) for r in records if r.get("category")}))
            selected_cat = st.selectbox("분류(Category) 필터", ["전체 보기"] + categories)
            
            filtered_records = records if selected_cat == "전체 보기" else [r for r in records if r.get("category") == selected_cat]
            st.caption(f"조회된 파일 수: {len(filtered_records)}개")
            
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
        st.error(f"카탈로그 데이터를 불러오는 중 오류가 발생했습니다: {e}")

# --- [탭 4: Vector DB 데이터 구조 및 시각적 검증 뷰어] ---
with tab_inspector:
    st.subheader("Vector DB(ChromaDB) 데이터 표준화 현황 및 청크(Chunk) 검증 뷰어")
    st.caption("문서가 어떤 기준으로 분류되고 어떤 세팅 주제로 벡터화되었는지 시각적으로 확인하고 데이터를 보완합니다.")
    
    summary_list, chunk_details = get_vector_db_status()
    
    if not summary_list:
        st.info("현재 Vector DB에 적재된 데이터가 없습니다.")
    else:
        total_docs = len(summary_list)
        total_chunks = len(chunk_details)
        total_chars = sum(c["char_length"] for c in chunk_details)
        avg_chunk_len = round(total_chars / total_chunks) if total_chunks > 0 else 0
        
        m1, m2, m3 = st.columns(3)
        m1.metric("총 벡터화 문서 수", f"{total_docs}개")
        m2.metric("총 분할 조각(Chunk) 수", f"{total_chunks}개")
        m3.metric("조각당 평균 글자 수", f"{avg_chunk_len}자")
        
        st.divider()
        
        st.markdown("**1. 문서별 표준 분류 및 벡터 조각 적재 현황**")
        st.dataframe(
            summary_list,
            column_order=("doc_category", "file_name", "file_ext", "chunk_count", "avg_chars", "drive_link"),
            column_config={
                "doc_category": st.column_config.TextColumn("표준 분류", width="small"),
                "file_name": st.column_config.TextColumn("문서명", width="large"),
                "file_ext": st.column_config.TextColumn("형식", width="small"),
                "chunk_count": st.column_config.NumberColumn("생성된 조각 수", width="small"),
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
            column_order=("doc_category", "setup_topic", "chunk_type", "file_name", "location", "char_length", "preview"),
            column_config={
                "doc_category": st.column_config.TextColumn("표준 분류", width="small"),
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
            selected_loc = st.selectbox(
                "전문(Full Text)을 확인할 조각 위치를 선택하세요",
                options=[f"[{c['location']}] ({c['setup_topic']} / {c['char_length']}자) - {c['file_name']}" for c in filtered_chunks]
            )
            target_idx = [f"[{c['location']}] ({c['setup_topic']} / {c['char_length']}자) - {c['file_name']}" for c in filtered_chunks].index(selected_loc)
            st.text_area(
                "실제 Vector DB에 저장된 텍스트 원문 (이 내용을 AI가 읽고 답변합니다)",
                value=filtered_chunks[target_idx]["full_content"],
                height=220,
                disabled=True
            )