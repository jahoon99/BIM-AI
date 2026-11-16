import os
import re
import stat
import shutil
import hashlib

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

GUIDELINE_KEYWORDS = [
    "지침", "가이드", "guide", "매뉴얼", "manual", "요령", "수행계획", "bep",
    "스펙북", "기준서", "표준공종", "사전 검토 사항", "사전검토사항", "체크리스트 양식"
]

OUTPUT_KEYWORDS = [
    "검토보고서", "결과보고서", "준공", "납품", "수량", "산출서", "비교표",
    "견적", "내역", "시뮬레이션", "간섭"
]


def read_server_path():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8-sig") as f:
            for line in f:
                line_s = line.strip()
                if not line_s or line_s.startswith("#"):
                    continue
                m = re.match(r"^SERVER_STORAGE_PATH\s*=\s*(.*)$", line_s)
                if m:
                    val = m.group(1).strip().strip('"').strip("'").strip()
                    val = val.replace("/", "\\")
                    if val.startswith("\\") and not val.startswith("\\\\"):
                        val = "\\" + val
                    elif re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\\", val):
                        val = "\\\\" + val
                    return val.rstrip("\\")
    return ""


def get_file_hash(filepath):
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def force_remove_dir(dir_path):
    if not os.path.exists(dir_path):
        return
    for root, dirs, files in os.walk(dir_path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception:
                pass
        for fn in files:
            try:
                os.chmod(os.path.join(root, fn), stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
    shutil.rmtree(dir_path, ignore_errors=True)


def is_manual_or_guideline(rel_folder, fname):
    f_low = fname.lower()
    rel_low = rel_folder.lower()
    ext_low = os.path.splitext(f_low)[1]

    if any(gk in f_low for gk in GUIDELINE_KEYWORDS):
        if not any(ok in f_low for ok in OUTPUT_KEYWORDS):
            return True
    if any(gk in rel_low for gk in ["manual", "매뉴얼", "지침", "가이드", "스펙북"]):
        if not any(ok in f_low for ok in OUTPUT_KEYWORDS) and ext_low not in {".xlsx", ".xls", ".mp4", ".avi", ".mov"}:
            return True
    return False


def run_full_reorganization():
    server_root = read_server_path()
    if not server_root:
        print("[오류] .env 파일에 SERVER_STORAGE_PATH가 설정되어 있지 않습니다.")
        return

    os.makedirs(server_root, exist_ok=True)
    idx_dir = os.path.join(server_root, "00_시스템인덱스")
    out_dir = os.path.join(server_root, "01_프로젝트_산출물")
    arc_dir = os.path.join(server_root, "99_bf_archive")
    bim_arc_dir = os.path.join(arc_dir, "BIM_File_Folder")
    man_arc_dir = os.path.join(arc_dir, "Manual_Folder")

    for d in (
        os.path.join(idx_dir, "chroma_db"),
        os.path.join(idx_dir, "doc_markdown"),
        os.path.join(out_dir, "01_설계검토_보고서"),
        os.path.join(out_dir, "02_수량산출서_내역비교표"),
        os.path.join(out_dir, "03_공정시뮬레이션_시각자료"),
        bim_arc_dir,
        man_arc_dir
    ):
        os.makedirs(d, exist_ok=True)

    print("==================================================================")
    print("[1단계] 로컬 PC 불필요 폴더/스크립트 정리 및 사내 서버 이관")
    print("==================================================================")

    # 구버전 스크립트 삭제
    obsolete_scripts = [
        os.path.join(BASE_DIR, "data_pipeline", "ingest_manuals.py"),
        os.path.join(BASE_DIR, "data_pipeline", "sync_bim_metadata.py"),
        os.path.join(BASE_DIR, "ingest_manuals.py"),
        os.path.join(BASE_DIR, "sync_bim_metadata.py"),
        os.path.join(BASE_DIR, "viewer_showcase_links.json"),
        os.path.join(BASE_DIR, ".env.portable")
    ]
    for sp in obsolete_scripts:
        if os.path.exists(sp) and os.path.isfile(sp):
            try:
                os.remove(sp)
                print(" - [구버전 파일 삭제]:", os.path.relpath(sp, BASE_DIR))
            except Exception:
                pass

    # 로컬 db_docs 및 server_storage 내부 파일을 사내 서버 99_bf_archive로 이동 후 로컬 폴더 삭제
    for local_folder_name in ("db_docs", "server_storage"):
        local_p = os.path.join(BASE_DIR, local_folder_name)
        if os.path.exists(local_p) and os.path.abspath(local_p).lower() != os.path.abspath(server_root).lower():
            for root, _, files in os.walk(local_p):
                rel = os.path.relpath(root, local_p)
                for fn in files:
                    if fn.startswith(("~$", ".")) or "관리대장" in fn:
                        continue
                    src_f = os.path.join(root, fn)
                    target_sub = man_arc_dir if is_manual_or_guideline(rel, fn) else bim_arc_dir
                    dst_f = os.path.join(target_sub, fn)
                    if not os.path.exists(dst_f):
                        try:
                            shutil.copy2(src_f, dst_f)
                        except Exception:
                            pass
            force_remove_dir(local_p)
            print(" - [로컬 폴더 이관 및 삭제 완료]:", local_folder_name)

    # 로컬 chroma_db 계열 및 showcase_slides 폴더 정리
    dest_chroma = os.path.join(idx_dir, "chroma_db")
    for cname in ("chroma_db", "chroma_db_runtime", "chroma_db_clean", "chroma_db_active"):
        local_c = os.path.join(BASE_DIR, cname)
        if os.path.exists(local_c):
            for root, _, files in os.walk(local_c):
                rel = os.path.relpath(root, local_c)
                t_root = dest_chroma if rel == "." else os.path.join(dest_chroma, rel)
                os.makedirs(t_root, exist_ok=True)
                for fn in files:
                    if fn.endswith(("-wal", "-shm", ".lock")):
                        continue
                    sf = os.path.join(root, fn)
                    df = os.path.join(t_root, fn)
                    if not os.path.exists(df):
                        try:
                            shutil.copy2(sf, df)
                        except Exception:
                            pass
            force_remove_dir(local_c)
            print(" - [로컬 Vector DB 사내 서버 이관 및 삭제 완료]:", cname)

    for sname in ("showcase_slides", "showcase_cache"):
        local_s = os.path.join(BASE_DIR, sname)
        if os.path.exists(local_s):
            force_remove_dir(local_s)
            print(" - [불필요한 로컬 분할장표/캐시 폴더 삭제 완료]:", sname)

    for jname in ("showcase_catalog.json", "showcase_slides_index.json"):
        local_j = os.path.join(BASE_DIR, jname)
        if os.path.exists(local_j):
            try:
                os.remove(local_j)
            except Exception:
                pass

    print("\n==================================================================")
    print("[2단계] 사내 서버 폴더를 3대 체계(00_시스템인덱스 / 01_프로젝트_산출물 / 99_bf_archive)로 통합")
    print("==================================================================")

    # 사내 서버 내 00_시스템인덱스 내부의 구버전 showcase_slides 폴더도 삭제 (실시간 메모리 추출로 전환됨)
    old_srv_slides = os.path.join(idx_dir, "showcase_slides")
    if os.path.exists(old_srv_slides):
        force_remove_dir(old_srv_slides)
        print(" - [00_시스템인덱스/showcase_slides 물리적 분할폴더 제거 완료 (실시간 메모리 추출 적용)]")

    # 사내 서버 루트에 흩어져 있던 기존 폴더(01_영업쇼케이스..., 02_..., 03_..., 04_..., BIM_File_Folder, Manual_Folder)를
    # 99_bf_archive 하위(BIM_File_Folder, Manual_Folder)로 중복 없이 통합
    candidates = []
    for root, dirs, files in os.walk(server_root):
        dirs[:] = [d for d in dirs if d not in {"00_시스템인덱스", "01_프로젝트_산출물"} and not d.startswith(".")]
        for fn in files:
            if fn.startswith(("~$", ".")) or fn.endswith(".json") or "관리대장" in fn:
                continue
            full_p = os.path.join(root, fn)
            rel_folder = os.path.relpath(root, server_root)
            candidates.append((full_p, rel_folder, fn))

    seen_hashes = {}
    moved_cnt = 0
    dup_cnt = 0

    for full_p, rel_folder, fn in candidates:
        if not os.path.exists(full_p):
            continue
        # 말머리 태그([프로젝트_용도_공법])가 붙어 있던 복사본 파일은 원본 파일명으로 복원하여 비교
        clean_fn = re.sub(r'^\[.*?\]\s*', '', fn).strip()
        f_hash = get_file_hash(full_p)
        dedup_key = (clean_fn.lower(), f_hash) if f_hash else (clean_fn.lower(), os.path.getsize(full_p))

        target_archive_sub = man_arc_dir if is_manual_or_guideline(rel_folder, clean_fn) else bim_arc_dir
        dest_p = os.path.join(target_archive_sub, clean_fn)

        if dedup_key in seen_hashes:
            kept_p = seen_hashes[dedup_key]
            if os.path.abspath(full_p).lower() != os.path.abspath(kept_p).lower():
                try:
                    os.chmod(full_p, stat.S_IWRITE | stat.S_IREAD)
                    os.remove(full_p)
                    dup_cnt += 1
                except Exception:
                    pass
            continue

        if os.path.abspath(full_p).lower() != os.path.abspath(dest_p).lower():
            try:
                if os.path.exists(dest_p):
                    os.chmod(dest_p, stat.S_IWRITE | stat.S_IREAD)
                    os.remove(dest_p)
                shutil.move(full_p, dest_p)
                moved_cnt += 1
            except Exception:
                continue

        seen_hashes[dedup_key] = dest_p

    # 사내 서버 루트에서 3대 표준 폴더(00_시스템인덱스, 01_프로젝트_산출물, 99_bf_archive) 이외의 구버전 폴더 일괄 삭제
    allowed_top = {"00_시스템인덱스", "01_프로젝트_산출물", "99_bf_archive"}
    for item in os.listdir(server_root):
        item_p = os.path.join(server_root, item)
        if os.path.isdir(item_p) and item not in allowed_top and not item.startswith("."):
            force_remove_dir(item_p)
            print(" - [사내 서버 구버전 중복 폴더 삭제 완료]:", item)

    # 01_프로젝트_산출물 내부의 구버전 용도별 폴더도 깨끗이 비운 뒤 3분류만 유지
    allowed_out_subs = {"01_설계검토_보고서", "02_수량산출서_내역비교표", "03_공정시뮬레이션_시각자료"}
    for item in os.listdir(out_dir):
        item_p = os.path.join(out_dir, item)
        if os.path.isdir(item_p) and item not in allowed_out_subs:
            force_remove_dir(item_p)

    print(" - [99_bf_archive 마스터 통합 완료] 이동: {0}건 | 중복 파일 제거: {1}건".format(moved_cnt, dup_cnt))

    print("\n==================================================================")
    print("[3단계] BF 파일 분석 & 동기화 실행 (01_프로젝트_산출물 자동 배포 + MD 분석서 + 관리대장 생성)")
    print("==================================================================")
    from data_pipeline.hybrid_server_sync import scan_hybrid_server_and_update
    items, slides, sheet_ok = scan_hybrid_server_and_update(sync_sheets=True, ingest_new_docs=True)
    print("==================================================================")
    print("[전체 완료] 사내 산출물: {0}건 | 인덱싱 장표: {1}장 | 구글시트/엑셀대장 반영 완료".format(len(items), len(slides)))
    print("==================================================================")


if __name__ == "__main__":
    run_full_reorganization()
