import streamlit as st
import sys
import os
import io
from contextlib import redirect_stdout

# 1. 상위 폴더 경로 추가 (backend 및 data_pipeline 폴더 인식을 위함)
sys.path.append(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from backend.bim_agent import get_ai_response, get_cached_sheet_records
from data_pipeline.sync_bim_metadata import sync_drive_to_sheets
from data_pipeline.ingest_manuals import ingest_manuals_to_chroma

# 2. 웹 페이지 기본 설정
st.set_page_config(
    page_title="BIM 데이터 어시스턴트",
    layout="centered"
)

# 3. 좌측 사이드바: 관리자 데이터 동기화 제어판
with st.sidebar:
    st.header("시스템 데이터 관리")
    st.caption("구글 드라이브나 시트에 변경사항이 있을 때 아래 버튼을 눌러 최신화하세요.")
    
    st.divider()
    
    # [버튼 1] BIM 파일 메타데이터 동기화 (Step 1-A) + 시트 캐시 갱신
    st.subheader("1. BIM 파일 목록 동기화")
    st.caption("드라이브의 신규 패밀리/템플릿을 스캔하여 AI가 시트에 분류와 설명을 자동 기입합니다.")
    if st.button("BIM 파일 동기화 실행", use_container_width=True):
        with st.spinner("구글 드라이브 스캔 및 AI 메타데이터 생성 중..."):
            log_buffer = io.StringIO()
            try:
                # 터미널 출력(print)을 가로채서 웹 화면에 표시
                with redirect_stdout(log_buffer):
                    sync_drive_to_sheets()
                # 시트가 업데이트되었으므로 백엔드 메모리 캐시도 즉시 강제 새로고침
                get_cached_sheet_records(force_refresh=True)
                st.success("BIM 파일 동기화 및 캐시 갱신 완료")
            except Exception as e:
                st.error(f"동기화 중 오류 발생: {e}")
            
            with st.expander("실행 상세 로그 보기", expanded=True):
                st.code(log_buffer.getvalue(), language="text")

    st.divider()

    # [버튼 2] 매뉴얼 및 보고서 Vector DB 동기화 (Step 1-B)
    st.subheader("2. 매뉴얼 지식 DB 동기화")
    st.caption("매뉴얼 폴더의 추가/수정/삭제된 문서를 감지하여 로컬 ChromaDB를 최신화합니다.")
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

    st.divider()

    # [버튼 3] 구글 시트 수동 수정 시 캐시만 즉시 새로고침
    st.subheader("3. 시트 캐시 새로고침")
    st.caption("구글 스프레드시트의 설명을 사람이 직접 수정했을 때 10분 대기 없이 즉시 반영합니다.")
    if st.button("시트 데이터 즉시 반영", use_container_width=True):
        try:
            records = get_cached_sheet_records(force_refresh=True)
            st.success(f"캐시 갱신 완료 (총 {len(records)}개 파일 로드됨)")
        except Exception as e:
            st.error(f"캐시 갱신 실패: {e}")
            
    st.divider()
    if st.button("대화 내용 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# 4. 메인 채팅 화면 헤더
st.title("BIM 데이터 어시스턴트")
st.caption("사내 매뉴얼, 검토보고서 검색 및 구글 드라이브 내 BIM 패밀리를 찾아드립니다.")

# 5. 세션 상태에 채팅 기록 저장 배열 생성
if "messages" not in st.session_state:
    st.session_state.messages = []

# 6. 기존 채팅 기록 화면에 출력
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 7. 사용자 채팅 입력부 처리
if prompt := st.chat_input("질문을 입력하세요 (예: T-Bar 패밀리 파일 찾아줘)"):
    
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 8. AI 응답 생성 및 화면 출력
    with st.chat_message("assistant"):
        with st.spinner("지식 저장소와 구글 드라이브를 검색하고 있습니다..."):
            try:
                response, manual_ctx, file_ctx = get_ai_response(prompt, return_context=True)
                st.markdown(response)
                
                with st.expander("시스템 DB 검색 원본 결과 확인"):
                    st.markdown("**[구글 시트 (BIM 파일) 검색 결과]**")
                    st.text(file_ctx)
                    st.markdown("**[ChromaDB (매뉴얼) 검색 결과]**")
                    st.text(manual_ctx)
                    
            except Exception as e:
                response = f"데이터베이스 연결 또는 검색 중 오류가 발생했습니다.\n에러 내용: {e}"
                st.error(response)
                
    st.session_state.messages.append({"role": "assistant", "content": response})