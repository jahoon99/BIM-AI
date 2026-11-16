import os
import io
import re
import sys
import zipfile
import base64
from collections import defaultdict
import streamlit as st

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

STREAMLIT_CONFIG_DIR = os.path.join(BASE_DIR, ".streamlit")
STREAMLIT_CONFIG_PATH = os.path.join(STREAMLIT_CONFIG_DIR, "config.toml")
try:
    os.makedirs(STREAMLIT_CONFIG_DIR, exist_ok=True)
    with open(STREAMLIT_CONFIG_PATH, "w", encoding="utf-8") as cf:
        cf.write(
            '[theme]\n'
            'base="light"\n'
            'primaryColor="#0F172A"\n'
            'backgroundColor="#F8FAFC"\n'
            'secondaryBackgroundColor="#FFFFFF"\n'
            'textColor="#0F172A"\n'
            'font="sans serif"\n'
        )
except Exception:
    pass

from backend.bim_agent import (
    get_ai_response,
    get_cached_sheet_records,
    get_indexed_manual_list,
    find_referenced_server_file_paths,
    generate_revit_shared_param_file,
    generate_revit_setup_script
)
from data_pipeline.hybrid_server_sync import (
    SERVER_STORAGE_PATH,
    TAXONOMY_USAGE,
    TAXONOMY_METHOD,
    TAXONOMY_DISCIPLINE,
    TAXONOMY_TASK,
    scan_hybrid_server_and_update,
    load_showcase_catalog,
    load_showcase_slides
)

TAB1_TITLE = "산출물 아카이브"
TAB2_TITLE = "AI 지식 어시스턴트"
TAB3_TITLE = "Revit 자동 세팅"

st.set_page_config(
    page_title="비아이엠팩토리 사내 통합 자료 기반 AI 업무 지원 시스템",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body, [class*="css"], .stApp {
    font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif !important;
    background-color: #F8FAFC !important;
    color: #0F172A !important;
}

.block-container {
    padding-top: 2.2rem !important;
    padding-bottom: 3rem !important;
    max-width: 1440px !important;
}

.modern-header {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 24px 30px;
    margin-bottom: 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.03);
}
.modern-badge {
    display: inline-block;
    background-color: #F1F5F9;
    color: #475569 !important;
    font-size: 11px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 6px;
    letter-spacing: 0.8px;
    margin-bottom: 8px;
}
.modern-title {
    font-size: 24px;
    font-weight: 800;
    color: #0F172A !important;
    letter-spacing: -0.5px;
    margin: 0;
}
.modern-subtitle {
    font-size: 13.5px;
    color: #64748B !important;
    margin-top: 4px;
    font-weight: 400;
}

div[data-baseweb="tab-list"] {
    background-color: #E2E8F0 !important;
    padding: 5px !important;
    border-radius: 10px !important;
    gap: 6px !important;
    width: fit-content !important;
    border-bottom: none !important;
    margin-bottom: 18px !important;
}
button[data-baseweb="tab"] {
    background-color: transparent !important;
    border: none !important;
    border-radius: 7px !important;
    padding: 9px 22px !important;
    height: auto !important;
    box-shadow: none !important;
    transition: all 0.15s ease !important;
}
button[data-baseweb="tab"] p {
    font-size: 14.5px !important;
    font-weight: 600 !important;
    color: #475569 !important;
    margin: 0 !important;
}
button[data-baseweb="tab"]:hover {
    background-color: rgba(255, 255, 255, 0.5) !important;
}
button[data-baseweb="tab"][aria-selected="true"] {
    background-color: #FFFFFF !important;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.1), 0 1px 2px rgba(15, 23, 42, 0.06) !important;
}
button[data-baseweb="tab"][aria-selected="true"] p {
    color: #0F172A !important;
    font-weight: 700 !important;
}
div[data-baseweb="tab-highlight"] {
    display: none !important;
}

span[data-baseweb="tag"] {
    background-color: #F1F5F9 !important;
    border: 1px solid #CBD5E1 !important;
    border-radius: 6px !important;
    padding: 2px 8px !important;
}
span[data-baseweb="tag"] span {
    color: #0F172A !important;
    font-weight: 600 !important;
    font-size: 13px !important;
}
span[data-baseweb="tag"] svg {
    fill: #475569 !important;
    color: #475569 !important;
}

div[data-baseweb="select"] > div {
    background-color: #FFFFFF !important;
    border: 1px solid #CBD5E1 !important;
    border-radius: 8px !important;
    min-height: 40px !important;
    box-shadow: none !important;
}
div[data-baseweb="select"] > div:hover {
    border-color: #64748B !important;
}

div.stButton > button, div.stDownloadButton > button {
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 13.5px !important;
    padding: 0.5rem 1rem !important;
    border: 1px solid #CBD5E1 !important;
    background-color: #FFFFFF !important;
    color: #1E293B !important;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03) !important;
    transition: all 0.15s ease !important;
}
div.stButton > button:hover, div.stDownloadButton > button:hover {
    background-color: #F8FAFC !important;
    border-color: #94A3B8 !important;
    color: #0F172A !important;
}
div.stButton > button[kind="primary"], div.stDownloadButton > button[kind="primary"] {
    background-color: #0F172A !important;
    color: #FFFFFF !important;
    border: 1px solid #0F172A !important;
}
div.stButton > button[kind="primary"]:hover, div.stDownloadButton > button[kind="primary"]:hover {
    background-color: #1E293B !important;
    border-color: #1E293B !important;
}
div.stButton > button[kind="primary"] *, div.stDownloadButton > button[kind="primary"] * {
    color: #FFFFFF !important;
}

div[data-testid="stVerticalBlockBorderWrapper"] {
    background-color: #FFFFFF !important;
    border: 1px solid #E2E8F0 !important;
    border-radius: 12px !important;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.02) !important;
}

.summary-bar {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    padding: 8px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.summary-label {
    font-size: 13.5px;
    font-weight: 700;
    color: #334155 !important;
}
.summary-chip {
    display: inline-block;
    background-color: #F8FAFC;
    color: #0F172A !important;
    border: 1px solid #E2E8F0;
    font-weight: 600;
    font-size: 12.5px;
    padding: 3px 10px;
    border-radius: 6px;
    margin-left: 6px;
}

.meta-chip-dark { background-color: #1E293B; color: #FFFFFF !important; padding: 3px 9px; border-radius: 5px; font-size: 12px; font-weight: 600; margin-right: 5px; display: inline-block; }
.meta-chip { background-color: #F1F5F9; color: #334155 !important; border: 1px solid #E2E8F0; padding: 3px 9px; border-radius: 5px; font-size: 12px; font-weight: 600; margin-right: 5px; display: inline-block; }

div[data-testid="stAlert"] {
    background-color: #F8FAFC !important;
    border: 1px solid #E2E8F0 !important;
    color: #334155 !important;
    border-radius: 8px !important;
}
</style>
""", unsafe_allow_html=True)


def resolve_local_file_path(item):
    for key in ("parent_path", "slide_pdf_path", "server_path", "distributed_path", "drive_link"):
        p = str(item.get(key, "")).strip()
        if not p:
            continue
        if os.path.exists(p) and os.path.isfile(p):
            return p
    return ""


def extract_single_pdf_page_bytes(pdf_path, page_num=1):
    """
    [실시간 메모리 장표 추출 (On-Demand In-Memory Slicing)]
    사내 서버에 수백 개의 낱장 PDF 파일을 물리적으로 저장하지 않고,
    사용자가 열람하거나 다운로드하는 순간 해당 페이지(page_num) 1장만 메모리(io.BytesIO)로 즉시 추출합니다.
    """
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        total_p = len(reader.pages)
        idx = max(0, min(int(page_num) - 1, total_p - 1))
        writer = pypdf.PdfWriter()
        writer.add_page(reader.pages[idx])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        try:
            with open(pdf_path, "rb") as f:
                return f.read()
        except Exception:
            return b""


def render_pdf_bytes_iframe(pdf_bytes, page_num=1, height=780):
    b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
    iframe_html = (
        '<iframe src="data:application/pdf;base64,{0}#page={1}&toolbar=1&navpanes=0&view=FitH" '
        'width="100%" height="{2}px" style="border: 1px solid #E2E8F0; border-radius: 8px; background: #FFFFFF;"></iframe>'
    ).format(b64_pdf, page_num, height)
    st.markdown(iframe_html, unsafe_allow_html=True)


def render_visual_media_viewer(item, target_page=1, is_slide_mode=False):
    fname = item.get("file_name", item.get("parent_file", "샘플자료"))
    proj_name = item.get("project_name", "사내 수행 프로젝트")
    slide_title = item.get("slide_title", "")
    page_num = item.get("page_num", target_page)
    ext_low = os.path.splitext(fname)[1].lower()
    local_path = resolve_local_file_path(item)

    head_col1, head_col2 = st.columns([2.7, 1.0])
    with head_col1:
        title_display = "{0} ({1}p · {2})".format(fname, page_num, slide_title) if slide_title else fname
        st.markdown("##### {0}".format(title_display))
        st.markdown(
            "<span class='meta-chip-dark'>{0}</span>"
            "<span class='meta-chip'>용도: {1}</span>"
            "<span class='meta-chip'>공법: {2}</span>"
            "<span class='meta-chip'>공종: {3}</span>"
            "<span class='meta-chip'>업무: {4}</span>".format(
                proj_name,
                ", ".join(item.get("usages", [item.get("usage", "일반")])),
                ", ".join(item.get("methods", [item.get("method_type", "일반")])),
                ", ".join(item.get("disciplines", [item.get("discipline", "RC")])),
                ", ".join(item.get("tasks", [item.get("task_type", "설계검토")]))
            ),
            unsafe_allow_html=True
        )

    if not local_path:
        st.warning("사내 서버에서 원본 파일 경로를 찾을 수 없습니다: {0}".format(item.get("server_path", "")))
        return

    if ext_low in {".mp4", ".webm", ".mov", ".avi"}:
        with head_col2:
            with open(local_path, "rb") as f_v:
                st.download_button("영상 원본 다운로드", data=f_v, file_name=fname, use_container_width=True)
        st.video(local_path)
        return

    # 개별 장표 모드이고 PDF인 경우 해당 1페이지만 메모리에서 실시간 추출하여 고속 렌더링
    if ext_low == ".pdf" and is_slide_mode:
        display_bytes = extract_single_pdf_page_bytes(local_path, page_num=page_num)
        dl_name = "{0}_{1:02d}p.pdf".format(os.path.splitext(fname)[0], page_num)
    else:
        with open(local_path, "rb") as f_dl:
            display_bytes = f_dl.read()
        dl_name = os.path.basename(local_path)

    with head_col2:
        st.download_button(
            label="현재 장표/파일 다운로드",
            data=display_bytes,
            file_name=dl_name,
            key="dl_btn_{0}_{1}_{2}".format(fname, page_num, is_slide_mode),
            use_container_width=True
        )

    st.write("")
    if ext_low in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        st.image(display_bytes, caption=fname, use_container_width=True)
    elif ext_low == ".pdf":
        # 실시간 1페이지 추출 시에는 추출된 파일의 1페이지가 곧 해당 장표임
        render_pdf_bytes_iframe(display_bytes, page_num=1 if is_slide_mode else target_page, height=760)
    elif ext_low in {".xlsx", ".xls", ".csv"}:
        try:
            import openpyxl
            if ext_low == ".csv":
                import csv as py_csv
                text_data = display_bytes.decode("utf-8-sig", errors="replace").splitlines()
                reader = list(py_csv.reader(text_data))
                if reader:
                    st.dataframe(reader[:250], use_container_width=True, height=620)
            else:
                wb = openpyxl.load_workbook(io.BytesIO(display_bytes), data_only=True)
                sheet_tabs = st.tabs(wb.sheetnames)
                for s_tab, s_name in zip(sheet_tabs, wb.sheetnames):
                    with s_tab:
                        ws = wb[s_name]
                        rows_data = []
                        for r in ws.iter_rows(values_only=True):
                            if any(cell is not None for cell in r):
                                rows_data.append(["" if c is None else str(c) for c in r])
                        if rows_data:
                            st.dataframe(rows_data[:250], use_container_width=True, height=580)
        except Exception as e:
            st.warning("엑셀 표 미리보기 오류: {0}".format(e))
    else:
        st.info("전용 소프트웨어 원본 파일({0})입니다. 우측 상단 다운로드 버튼을 눌러 활용하세요.".format(ext_low))


def match_multi_categories(item_list_field, selected_list):
    if not selected_list:
        return True
    item_set = set(item_list_field or [])
    return any(sel in item_set for sel in selected_list)


def build_multi_project_merged_pdf(proj_slides_map, max_slides_per_project=8):
    """원본 PDF에서 조건에 맞는 페이지 번호들만 실시간 메모리 추출하여 1개의 브리핑북 PDF로 병합합니다."""
    import pypdf
    merger = pypdf.PdfWriter()
    total_added = 0
    reader_cache = {}

    for proj_name, s_list in proj_slides_map.items():
        cnt = 0
        for s in s_list:
            if cnt >= max_slides_per_project:
                break
            sp = resolve_local_file_path(s)
            p_num = int(s.get("page_num", 1))
            if sp and sp.lower().endswith(".pdf") and os.path.exists(sp):
                try:
                    if sp not in reader_cache:
                        reader_cache[sp] = pypdf.PdfReader(sp)
                    reader = reader_cache[sp]
                    idx = max(0, min(p_num - 1, len(reader.pages) - 1))
                    merger.add_page(reader.pages[idx])
                    cnt += 1
                    total_added += 1
                except Exception:
                    continue
    if total_added == 0:
        return None, 0
    out_buf = io.BytesIO()
    merger.write(out_buf)
    return out_buf.getvalue(), total_added


def build_multi_project_structured_zip(proj_files_map, proj_slides_map, summary_md_text=""):
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if summary_md_text:
            zf.writestr("00_프로젝트별_산출물_AI비교분석.md", summary_md_text.encode("utf-8-sig"))

        merged_pdf_bytes, slide_cnt = build_multi_project_merged_pdf(proj_slides_map, max_slides_per_project=8)
        if merged_pdf_bytes:
            zf.writestr("01_핵심장표_통합발췌본({0}장).pdf".format(slide_cnt), merged_pdf_bytes)

        for proj_name, f_list in proj_files_map.items():
            safe_proj = re.sub(r'[\\/:*?"<>|]', '_', proj_name)
            for it in f_list[:15]:
                lp = resolve_local_file_path(it)
                if lp and os.path.exists(lp):
                    try:
                        zf.write(lp, arcname="{0}/원본파일/{1}".format(safe_proj, it["file_name"]))
                    except Exception:
                        pass

        for proj_name, s_list in proj_slides_map.items():
            safe_proj = re.sub(r'[\\/:*?"<>|]', '_', proj_name)
            for s in s_list[:10]:
                sp = resolve_local_file_path(s)
                p_num = int(s.get("page_num", 1))
                p_fname = os.path.splitext(s.get("parent_file", "slide"))[0]
                if sp and sp.lower().endswith(".pdf") and os.path.exists(sp):
                    single_bytes = extract_single_pdf_page_bytes(sp, page_num=p_num)
                    if single_bytes:
                        zf.writestr(
                            "{0}/발췌장표/{1}_{2:02d}p.pdf".format(safe_proj, p_fname, p_num),
                            single_bytes
                        )
    return zip_buf.getvalue()


# ==============================================================================
# [상단 헤더 영역]
# ==============================================================================
st.markdown("""
<div class="modern-header">
    <div>
        <span class="modern-badge">BIM FACTORY INTERNAL SYSTEM</span>
        <h1 class="modern-title">비아이엠팩토리 사내 통합 자료 기반 AI 업무 지원 시스템</h1>
        <div class="modern-subtitle">프로젝트 산출물 아카이브 · 사내 표준 지식 AI 검색 · Revit 세팅 자동화</div>
    </div>
</div>
""", unsafe_allow_html=True)

# ==============================================================================
# [좌측 사이드바: BF 파일 분석 & 동기화]
# ==============================================================================
with st.sidebar:
    st.markdown("#### 사내 서버 동기화 제어")
    st.caption("연결 경로: {0}".format(SERVER_STORAGE_PATH))

    if st.button("BF 파일 분석 & 동기화", use_container_width=True, type="primary"):
        with st.spinner("사내 서버(99_bf_archive) 분석, 산출물 배포 및 벡터 DB·관리대장 갱신 중..."):
            items, slides, _ = scan_hybrid_server_and_update(sync_sheets=True, ingest_new_docs=True)
            get_cached_sheet_records(force_refresh=True)
        st.success("동기화 완료: 산출물 {0}건 / 장표 {1}장".format(len(items), len(slides)))

    if st.button("사내 서버 폴더 열기", use_container_width=True):
        try:
            os.startfile(SERVER_STORAGE_PATH)
        except Exception:
            pass

# ==============================================================================
# [3대 핵심 카테고리 탭]
# ==============================================================================
tab_archive, tab_qa, tab_revit = st.tabs([
    TAB1_TITLE,
    TAB2_TITLE,
    TAB3_TITLE
])

# ==============================================================================
# 1. 산출물 아카이브
# ==============================================================================
with tab_archive:
    with st.container(border=True):
        fc1, fc2, fc3, fc4, fc5 = st.columns([1.2, 1.1, 1.2, 1.2, 1.15])
        with fc1:
            sel_usages = st.multiselect("① 용도 선택", list(TAXONOMY_USAGE.keys()), placeholder="데이터센터, 공동주택 등")
        with fc2:
            sel_methods = st.multiselect("② 공법 선택", list(TAXONOMY_METHOD.keys()), placeholder="탑다운, 순타, PC, 모듈")
        with fc3:
            sel_discs = st.multiselect("③ 공종 선택", list(TAXONOMY_DISCIPLINE.keys()), placeholder="RC, 철골, 단열, 방수 등")
        with fc4:
            sel_tasks = st.multiselect("④ 업무 선택", list(TAXONOMY_TASK.keys()), placeholder="설계검토, 수량, 공정시뮬레이션")
        with fc5:
            view_mode = st.radio("열람 기준", ["개별 장표(슬라이드)", "원본 파일(엑셀·영상·문서)"], index=0)

    all_slides = load_showcase_slides()
    all_items = load_showcase_catalog()

    matched_slides = [
        s for s in all_slides
        if match_multi_categories(s.get("usages"), sel_usages)
        and match_multi_categories(s.get("methods"), sel_methods)
        and match_multi_categories(s.get("disciplines"), sel_discs)
        and match_multi_categories(s.get("tasks"), sel_tasks)
    ]

    matched_files = [
        it for it in all_items
        if match_multi_categories(it.get("usages"), sel_usages)
        and match_multi_categories(it.get("methods"), sel_methods)
        and match_multi_categories(it.get("disciplines"), sel_discs)
        and match_multi_categories(it.get("tasks"), sel_tasks)
    ]

    proj_files_map = defaultdict(list)
    for it in matched_files:
        proj_files_map[it.get("project_name") or "사내 수행 프로젝트"].append(it)

    proj_slides_map = defaultdict(list)
    for s in matched_slides:
        proj_slides_map[s.get("project_name") or "사내 수행 프로젝트"].append(s)

    all_matched_projects = sorted(list(set(list(proj_files_map.keys()) + list(proj_slides_map.keys()))))

    bar_left, bar_btn1, bar_btn2, bar_btn3 = st.columns([1.85, 1.05, 1.05, 1.05])
    with bar_left:
        st.markdown(
            "<div class='summary-bar'>"
            "<span class='summary-label'>조회된 사내 산출물</span>"
            "<div>"
            "<span class='summary-chip'>프로젝트 {0}개</span>"
            "<span class='summary-chip'>장표 {1}장</span>"
            "<span class='summary-chip'>파일 {2}건</span>"
            "</div>"
            "</div>".format(len(all_matched_projects), len(matched_slides), len(matched_files)),
            unsafe_allow_html=True
        )
    with bar_btn1:
        merged_pdf_data, merged_cnt = build_multi_project_merged_pdf(proj_slides_map, max_slides_per_project=8)
        if merged_pdf_data:
            st.download_button(
                label="발췌 장표 PDF 다운로드 ({0}장)".format(merged_cnt),
                data=merged_pdf_data,
                file_name="비아이엠팩토리_산출물_발췌장표.pdf",
                mime="application/pdf",
                use_container_width=True,
                type="primary"
            )
    with bar_btn2:
        if matched_files or matched_slides:
            zip_pkg_data = build_multi_project_structured_zip(
                proj_files_map,
                proj_slides_map,
                summary_md_text=st.session_state.get("multi_proj_summary_md", "")
            )
            st.download_button(
                label="프로젝트별 통합 ZIP 다운로드",
                data=zip_pkg_data,
                file_name="비아이엠팩토리_산출물_통합패키지.zip",
                mime="application/zip",
                use_container_width=True
            )
    with bar_btn3:
        if st.button("AI 프로젝트 통합 비교·분석", use_container_width=True):
            if not all_matched_projects:
                st.warning("비교 분석할 사내 산출물 프로젝트가 없습니다.")
            else:
                proj_context_lines = []
                for p_name in all_matched_projects:
                    p_files = proj_files_map.get(p_name, [])
                    p_slides = proj_slides_map.get(p_name, [])
                    f_names = [f["file_name"] for f in p_files[:8]]
                    s_previews = [
                        "({0}p) {1}: {2}".format(s["page_num"], s["slide_title"], s["slide_text"][:160])
                        for s in p_slides[:5]
                    ]
                    proj_context_lines.append(
                        "■ 사내 프로젝트명: {0}\n- 산출물 파일: {1}\n- 주요 장표 내용:\n  {2}".format(
                            p_name, ", ".join(f_names), "\n  ".join(s_previews)
                        )
                    )
                synth_q = (
                    "발주처 지침서가 아닌 당사(비아이엠팩토리)가 실제 수행한 아래 {0}개 프로젝트({1})의 사내 산출물 자료를 바탕으로, "
                    "프로젝트별 주요 설계검토/간섭사례, 적용 공법·공종 특징, 산출물 구성을 영업 브리핑용으로 비교·분석해줘.\n\n{2}"
                ).format(len(all_matched_projects), ", ".join(all_matched_projects), "\n\n".join(proj_context_lines))
                with st.spinner("발췌된 사내 프로젝트 산출물들을 AI가 통합 비교·분석하고 있습니다..."):
                    ai_comp_ans = get_ai_response(synth_q, channel="Web_Archive_Compare")
                    st.session_state["multi_proj_summary_md"] = ai_comp_ans

    if st.session_state.get("multi_proj_summary_md"):
        with st.expander("AI 다중 프로젝트 산출물 비교·분석 리포트 보기", expanded=True):
            st.markdown(st.session_state["multi_proj_summary_md"])

    if view_mode.startswith("개별 장표"):
        if not matched_slides:
            st.info(
                "선택하신 조건에 해당하는 사내 산출물 장표가 없습니다. "
                "(발주처 지침서 및 가이드북은 자동 제외되었으며, 좌측 사이드바에서 'BF 파일 분석 & 동기화'를 실행하면 최신 기준이 반영됩니다.)"
            )
        else:
            col_nav, col_stage = st.columns([1.05, 2.55])
            with col_nav:
                with st.container(border=True):
                    st.markdown("##### 1) 프로젝트 필터")
                    proj_options = ["전체 프로젝트 ({0}장)".format(len(matched_slides))] + [
                        "{0} ({1}장)".format(p, len(proj_slides_map[p])) for p in sorted(proj_slides_map.keys())
                    ]
                    sel_proj_label = st.selectbox("프로젝트 선택", proj_options, label_visibility="collapsed")

                    if sel_proj_label.startswith("전체 프로젝트"):
                        active_slides = matched_slides
                    else:
                        chosen_p_name = sel_proj_label.rsplit(" (", 1)[0]
                        active_slides = proj_slides_map[chosen_p_name]

                    st.markdown("##### 2) 산출물 장표 선택")
                    slide_labels = [
                        "[{0}p] {1} ({2})".format(s["page_num"], s["slide_title"][:24], s.get("project_name", "")[:10])
                        for s in active_slides
                    ]
                    chosen_s_idx = st.radio(
                        "장표 목록",
                        range(len(active_slides)),
                        format_func=lambda i: slide_labels[i],
                        label_visibility="collapsed"
                    )
                    cur_slide = active_slides[chosen_s_idx]

            with col_stage:
                with st.container(border=True):
                    render_visual_media_viewer(cur_slide, target_page=cur_slide.get("page_num", 1), is_slide_mode=True)

    else:
        if not matched_files:
            st.info("선택하신 필터 조건에 해당하는 사내 산출물 원본 파일이 없습니다.")
        else:
            col_nav, col_stage = st.columns([1.05, 2.55])
            with col_nav:
                with st.container(border=True):
                    st.markdown("##### 1) 프로젝트 필터")
                    f_proj_options = ["전체 프로젝트 ({0}건)".format(len(matched_files))] + [
                        "{0} ({1}건)".format(p, len(proj_files_map[p])) for p in sorted(proj_files_map.keys())
                    ]
                    sel_f_proj = st.selectbox("파일 프로젝트 선택", f_proj_options, label_visibility="collapsed")

                    if sel_f_proj.startswith("전체 프로젝트"):
                        active_files = matched_files
                    else:
                        chosen_fp_name = sel_f_proj.rsplit(" (", 1)[0]
                        active_files = proj_files_map[chosen_fp_name]

                    st.markdown("##### 2) 산출물 원본 파일 선택")
                    f_labels = [
                        "[{0}] {1}".format(it.get("media_type", "")[:6], it["file_name"])
                        for it in active_files
                    ]
                    chosen_f_idx = st.radio(
                        "파일 목록",
                        range(len(active_files)),
                        format_func=lambda i: f_labels[i],
                        label_visibility="collapsed"
                    )
                    sel_f = active_files[chosen_f_idx]
                    page_jump = 1
                    if sel_f.get("file_name", "").lower().endswith(".pdf") and sel_f.get("slide_count", 0) > 1:
                        page_jump = st.number_input(
                            "이동할 페이지 번호",
                            min_value=1,
                            max_value=max(1, sel_f.get("slide_count", 50)),
                            value=1
                        )

            with col_stage:
                with st.container(border=True):
                    render_visual_media_viewer(sel_f, target_page=page_jump, is_slide_mode=False)

# ==============================================================================
# 2. AI 지식 어시스턴트 (참조 원본 파일 및 .md 리포트 원클릭 다운로드 포함)
# ==============================================================================
with tab_qa:
    with st.container(border=True):
        st.markdown("#### 사내 표준 지침 및 프로젝트 사례 AI 검색")
        st.caption("발주처 지침서(조달청·LH 등)를 포함한 전체 표준 문서와 과거 검토보고서를 통합 검색하고 관련 원본 파일을 즉시 다운로드합니다.")

        qa_c1, qa_c2, qa_c3, qa_c4 = st.columns(4)
        with qa_c1:
            q_usages = st.multiselect("① 용도", list(TAXONOMY_USAGE.keys()), key="qu", placeholder="전체 용도")
        with qa_c2:
            q_methods = st.multiselect("② 공법", list(TAXONOMY_METHOD.keys()), key="qm", placeholder="전체 공법")
        with qa_c3:
            q_discs = st.multiselect("③ 공종", list(TAXONOMY_DISCIPLINE.keys()), key="qd", placeholder="전체 공종")
        with qa_c4:
            q_tasks = st.multiselect("④ 업무", list(TAXONOMY_TASK.keys()), key="qt", placeholder="전체 업무")

        q_col_input, q_col_doc = st.columns([2.2, 1.2])
        with q_col_input:
            extra_query = st.text_input(
                "질문 또는 요청사항 입력",
                placeholder="예: 데이터센터 프로젝트들의 주요 간섭 검토 항목과 단열재 수량 산출 기준 알려줘"
            )
        with q_col_doc:
            manuals_all = get_indexed_manual_list()
            sel_mans_qa = st.multiselect("특정 문서만 지정 검색 (선택사항)", manuals_all, placeholder="비워두면 전체 자동 검색")

        if st.button("AI 실무 가이드 및 관련 자료 검색", type="primary", use_container_width=True):
            tag_parts = []
            if q_usages:
                tag_parts.append("용도({0})".format(", ".join(q_usages)))
            if q_methods:
                tag_parts.append("공법({0})".format(", ".join(q_methods)))
            if q_discs:
                tag_parts.append("공종({0})".format(", ".join(q_discs)))
            if q_tasks:
                tag_parts.append("업무({0})".format(", ".join(q_tasks)))

            synthesized_query = "{0} 관련 사내 기준, 주요 검토 사례 및 활용 가능한 산출물 파일을 정리해줘. {1}".format(
                " / ".join(tag_parts) if tag_parts else "전체 표준",
                extra_query.strip()
            ).strip()

            with st.spinner("사내 지식베이스를 분석하고 관련 원본 파일을 탐색하고 있습니다..."):
                ans, m_ctx, f_ctx = get_ai_response(
                    synthesized_query,
                    return_context=True,
                    target_manuals=sel_mans_qa if sel_mans_qa else None,
                    channel="Web_QA"
                )
                ref_file_tuples = find_referenced_server_file_paths(ans, m_ctx, f_ctx, max_files=6)
            st.session_state["last_qa_answer"] = ans
            st.session_state["last_qa_ctx"] = (m_ctx, f_ctx)
            st.session_state["last_qa_files"] = ref_file_tuples

    if st.session_state.get("last_qa_answer"):
        with st.container(border=True):
            st.markdown(st.session_state["last_qa_answer"])
            st.divider()

            # AI 답변 전문(.md) 및 참조된 사내 서버 파일 원클릭 다운로드 영역
            st.markdown("##### 관련 사내 서버 문서 및 AI 가이드 즉시 다운로드")
            ref_files = st.session_state.get("last_qa_files", [])
            dl_cols = st.columns(min(4, max(1, len(ref_files) + 1)))

            with dl_cols[0]:
                st.download_button(
                    label="AI 답변 가이드 전문 (.md)",
                    data=st.session_state["last_qa_answer"].encode("utf-8-sig"),
                    file_name="BF_AI_실무가이드_답변전문.md",
                    mime="text/markdown",
                    use_container_width=True,
                    type="primary"
                )

            for idx, (r_fname, r_fpath) in enumerate(ref_files):
                if os.path.exists(r_fpath):
                    col_target = dl_cols[(idx + 1) % len(dl_cols)]
                    with col_target:
                        try:
                            with open(r_fpath, "rb") as rf:
                                st.download_button(
                                    label="원본: {0}".format(r_fname[:22]),
                                    data=rf.read(),
                                    file_name=r_fname,
                                    key="qa_ref_dl_{0}_{1}".format(idx, r_fname),
                                    use_container_width=True
                                )
                        except Exception:
                            pass

            with st.expander("참조된 사내 문서 원문 조각 확인"):
                m_c, f_c = st.session_state.get("last_qa_ctx", ("", ""))
                st.text(m_c)
                st.text(f_c)

# ==============================================================================
# 3. Revit 자동 세팅
# ==============================================================================
with tab_revit:
    r_col1, r_col2 = st.columns(2)
    with r_col1:
        with st.container(border=True):
            st.markdown("#### 공유 매개변수(.txt) 자동 생성기")
            st.caption("사내 및 발주처(조달청·LH 등) 기준에 맞춘 UTF-16 표준 공유 매개변수 파일을 즉시 생성합니다.")
            param_req = st.text_area(
                "추가할 매개변수 조건 입력",
                placeholder="예: 조달청 기준 구조 및 마감 물량 산출용 필수 공유 매개변수 세트 생성",
                height=110
            )
            if st.button("공유 매개변수 파일(.txt) 생성", type="primary", use_container_width=True):
                if param_req.strip():
                    with st.spinner("Revit 공유 매개변수 규격 파일 생성 중..."):
                        txt_out, p_list = generate_revit_shared_param_file(param_req.strip())
                    st.success("총 {0}개 매개변수 세트 생성 완료".format(len(p_list)))
                    st.download_button(
                        "BF_Custom_SharedParams.txt 다운로드",
                        data=txt_out.encode("utf-16"),
                        file_name="BF_Custom_SharedParams.txt",
                        use_container_width=True
                    )
    with r_col2:
        with st.container(border=True):
            st.markdown("#### Revit 초기 세팅 코드(.py) 생성기")
            st.caption("프로젝트 층고·그리드·뷰 세팅 요구사항을 입력하면 즉시 실행 가능한 Revit API 파이썬 코드를 생성합니다.")
            script_req = st.text_area(
                "레벨 및 그리드 세팅 조건 입력",
                placeholder="예: 지하 2층(층고 4500mm) ~ 지상 5층(층고 4200mm) 레벨 및 8000mm 간격 그리드 자동 생성",
                height=110
            )
            if st.button("Revit 자동 세팅 코드(.py) 생성", type="primary", use_container_width=True):
                if script_req.strip():
                    with st.spinner("Revit API 트랜잭션 스크립트 작성 중..."):
                        desc_out, py_out, _ = generate_revit_setup_script(script_req.strip())
                    st.download_button(
                        "Revit_Auto_Setup.py 다운로드",
                        data=py_out.encode("utf-8-sig"),
                        file_name="Revit_Auto_Setup.py",
                        use_container_width=True
                    )
                    with st.expander("생성된 세팅 요약 및 파이썬 코드 보기", expanded=True):
                        st.markdown(desc_out)
