import streamlit as st
import sys
import os
import io
from contextlib import redirect_stdout
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
load_dotenv()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

from backend.bim_agent import (
    get_ai_response, 
    get_cached_sheet_records, 
    get_indexed_manual_list,
    get_vector_db_status
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
st.caption("사내 표준 양식, 검토보고서, 수량산출 기준 검색 및 BIM 패밀리를 찾아드립니다.")

tab_chat, tab_catalog, tab_inspector = st.tabs([
    "AI 검색 챗봇", 
    "전체 BIM 파일 카탈로그", 
    "Vector DB 데이터 구조 및 검증"
])

# --- [탭 1: AI 검색 챗봇] ---
with tab_chat:
    manual_list = get_indexed_manual_list()
    col_cat, col_doc, col_info = st.columns([2, 2, 2])
    
    with col_cat:
        selected_category = st.selectbox(
            "업무 목적별 문서 분류 필터",
            options=["전체 분류", "표준양식/템플릿", "검토보고서", "검토리스트", "수량산출서", "매뉴얼/가이드북"]
        )
    with col_doc:
        selected_manual = st.selectbox(
            "특정 파일 지정 필터",
            options=["전체 문서 검색"] + manual_list
        )
    with col_info:
        st.caption(f"현재 Vector DB 학습 문서: **{len(manual_list)}개**")
        st.caption("원하는 문서 분류를 선택하면 불필요한 데이터를 배제하고 핀포인트로 검색합니다.")

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

    if prompt := st.chat_input("질문을 입력하세요 (예: 단열 수량 비교표는 어떤 항목과 수식으로 만들었는지 알려줘)"):
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
                        target_manual=selected_manual,
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
                        st.markdown(f"**[적용 필터 - 분류: {selected_category} / 문서: {selected_manual}]**")
                        st.markdown("**[구글 시트 (BIM 파일) 검색 결과]**")
                        st.text(file_ctx)
                        st.markdown("**[ChromaDB (매뉴얼/산출서) 검색 결과]**")
                        st.text(manual_ctx)
                        
                except Exception as e:
                    response = f"데이터베이스 연결 또는 검색 중 오류가 발생했습니다.\n에러 내용: {e}"
                    st.error(response)
                    
        st.session_state.messages.append({"role": "assistant", "content": response})

# --- [탭 2: 전체 BIM 파일 카탈로그] ---
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

# --- [탭 3: Vector DB 데이터 구조 및 시각적 검증 뷰어] ---
with tab_inspector:
    st.subheader("Vector DB(ChromaDB) 데이터 표준화 현황 및 청크(Chunk) 검증 뷰어")
    st.caption("문서가 어떤 기준으로 분류되고 몇 개의 조각으로 쪼개져 벡터화되었는지 시각적으로 확인하고 데이터를 보완합니다.")
    
    summary_list, chunk_details = get_vector_db_status()
    
    if not summary_list:
        st.info("현재 Vector DB에 적재된 데이터가 없습니다.")
    else:
        # 1. 상단 핵심 요약 지표 (Metrics)
        total_docs = len(summary_list)
        total_chunks = len(chunk_details)
        total_chars = sum(c["char_length"] for c in chunk_details)
        avg_chunk_len = round(total_chars / total_chunks) if total_chunks > 0 else 0
        
        m1, m2, m3 = st.columns(3)
        m1.metric("총 벡터화 문서 수", f"{total_docs}개")
        m2.metric("총 분할 조각(Chunk) 수", f"{total_chunks}개")
        m3.metric("조각당 평균 글자 수", f"{avg_chunk_len}자")
        
        st.divider()
        
        # 2. 문서별 표준 분류 및 적재 현황 표
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
        
        # 3. 개별 문서 내부 청크(Chunk) 상세 해부 및 텍스트 검증기
        st.markdown("**2. 문서 내부 분할 조각(Chunk) 상세 검증기**")
        inspect_file = st.selectbox(
            "상세 구조를 확인할 문서를 선택하세요",
            options=["전체 조각 보기"] + [s["file_name"] for s in summary_list]
        )
        
        filtered_chunks = chunk_details if inspect_file == "전체 조각 보기" else [c for c in chunk_details if c["file_name"] == inspect_file]
        
        st.dataframe(
            filtered_chunks,
            column_order=("doc_category", "chunk_type", "file_name", "location", "char_length", "preview"),
            column_config={
                "doc_category": st.column_config.TextColumn("표준 분류", width="small"),
                "chunk_type": st.column_config.TextColumn("조각 유형", width="small"),
                "file_name": st.column_config.TextColumn("파일명", width="medium"),
                "location": st.column_config.TextColumn("추출 위치", width="small"),
                "char_length": st.column_config.NumberColumn("글자 수", width="small"),
                "preview": st.column_config.TextColumn("텍스트 미리보기 (앞 120자)", width="large"),
            },
            use_container_width=True,
            hide_index=True
        )
        
        # 선택한 조각의 전문(Full Text) 확인란
        if filtered_chunks:
            selected_loc = st.selectbox(
                "전문(Full Text)을 확인할 조각 위치를 선택하세요",
                options=[f"[{c['location']}] ({c['chunk_type']} / {c['char_length']}자) - {c['file_name']}" for c in filtered_chunks]
            )
            target_idx = [f"[{c['location']}] ({c['chunk_type']} / {c['char_length']}자) - {c['file_name']}" for c in filtered_chunks].index(selected_loc)
            st.text_area(
                "실제 Vector DB에 저장된 텍스트 원문 (이 내용을 AI가 읽고 답변합니다)",
                value=filtered_chunks[target_idx]["full_content"],
                height=220,
                disabled=True
            )