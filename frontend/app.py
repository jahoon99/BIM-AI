import streamlit as st
import sys
import os
import io
from contextlib import redirect_stdout
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
load_dotenv()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

from backend.bim_agent import get_ai_response, get_cached_sheet_records, get_indexed_manual_list
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
        if st.button("매뉴얼 Vector DB 동기화", use_container_width=True):
            with st.spinner("문서 다운로드 및 벡터 임베딩 처리 중..."):
                log_buffer = io.StringIO()
                try:
                    with redirect_stdout(log_buffer):
                        ingest_manuals_to_chroma()
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
st.caption("사내 매뉴얼, 검토보고서 검색 및 구글 드라이브 내 BIM 패밀리를 찾아드립니다.")

tab_chat, tab_catalog = st.tabs(["AI 검색 챗봇", "전체 BIM 파일 카탈로그"])

# --- [탭 1: AI 검색 챗봇] ---
with tab_chat:
    # [신규 기능] 현재 ChromaDB에 학습된 문서 목록 확인 및 타겟 필터링
    manual_list = get_indexed_manual_list()
    col_filter, col_info = st.columns([2, 3])
    with col_filter:
        selected_manual = st.selectbox(
            "매뉴얼 검색 범위 지정 (Metadata Filter)",
            options=["전체 문서 검색"] + manual_list
        )
    with col_info:
        st.caption(f"현재 Vector DB에 학습된 매뉴얼/보고서 수: **{len(manual_list)}개**")
        if manual_list:
            st.caption("등록된 문서: " + ", ".join(manual_list))

    st.divider()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for idx, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            # AI 답변마다 마크다운 파일(.md)로 내려받을 수 있는 버튼 제공
            if message["role"] == "assistant":
                st.download_button(
                    label="답변 내용 다운로드 (.md)",
                    data=message["content"].encode("utf-8-sig"),
                    file_name=f"BIM_AI_답변_{idx}.md",
                    mime="text/markdown",
                    key=f"dl_{idx}"
                )

    if prompt := st.chat_input("질문을 입력하세요 (예: 키움 검토보고서 주요 지적사항 표로 정리해줘)"):
        previous_history = list(st.session_state.messages)
        
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("지식 저장소와 구글 드라이브를 검색하고 있습니다..."):
                try:
                    response, manual_ctx, file_ctx = get_ai_response(
                        prompt, 
                        chat_history=previous_history, 
                        return_context=True,
                        target_manual=selected_manual
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
                        st.markdown(f"**[적용된 매뉴얼 필터: {selected_manual}]**")
                        st.markdown("**[구글 시트 (BIM 파일) 검색 결과]**")
                        st.text(file_ctx)
                        st.markdown("**[ChromaDB (매뉴얼) 검색 결과]**")
                        st.text(manual_ctx)
                        
                except Exception as e:
                    response = f"데이터베이스 연결 또는 검색 중 오류가 발생했습니다.\n에러 내용: {e}"
                    st.error(response)
                    
        st.session_state.messages.append({"role": "assistant", "content": response})

# --- [탭 2: 전체 BIM 파일 카탈로그] ---
with tab_catalog:
    st.subheader("등록된 BIM 파일 및 템플릿 목록")
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