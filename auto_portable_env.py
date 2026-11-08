import os
import re
import glob

def sync_portable_env():
    """어느 PC(C드라이브, G드라이브 등)에서 실행하든 현재 폴더 기준으로 .env 내의 파일 경로를 자동 치유합니다."""
    base_dir = os.path.abspath(os.path.dirname(__file__))
    env_path = os.path.join(base_dir, ".env")
    backup_path = os.path.join(base_dir, ".env.portable")

    # 1. 만약 다른 PC로 이동 중 .env 숨김 파일이 누락되었다면 백업본(.env.portable)에서 즉시 자동 복구
    if not os.path.exists(env_path) and os.path.exists(backup_path):
        with open(backup_path, "r", encoding="utf-8") as src, open(env_path, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print("[포터블 복구] 백업 파일(.env.portable)로부터 환경변수를 자동 복원했습니다.")

    if not os.path.exists(env_path):
        print("[경고] 현재 폴더에 .env 파일이 없습니다. 최초 1회 토큰 정보가 포함된 .env 파일이 필요합니다.")
        return

    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 2. CHROMA_DB_PATH를 무조건 현재 실행 중인 PC의 프로젝트 폴더 내 chroma_db로 자동 고정
    local_chroma = os.path.join(base_dir, "chroma_db")
    if re.search(r'^CHROMA_DB_PATH=', content, flags=re.MULTILINE):
        content = re.sub(
            r'^CHROMA_DB_PATH=.*$',
            lambda _: "CHROMA_DB_PATH=" + local_chroma,
            content,
            flags=re.MULTILINE
        )
    else:
        content = content.rstrip() + "\nCHROMA_DB_PATH=" + local_chroma + "\n"

    # 3. GOOGLE_APPLICATION_CREDENTIALS 경로가 현재 PC에 맞지 않으면 폴더 내 JSON 인증키를 자동 탐색해 연결
    cred_match = re.search(r'^GOOGLE_APPLICATION_CREDENTIALS=(.*)$', content, flags=re.MULTILINE)
    curr_cred = cred_match.group(1).strip().strip('"').strip("'") if cred_match else ""

    if not curr_cred or not os.path.exists(curr_cred):
        json_candidates = [
            p for p in glob.glob(os.path.join(base_dir, "*.json"))
            if not os.path.basename(p).lower().startswith(("package", "tsconfig", "manifest"))
        ]
        if json_candidates:
            new_cred = json_candidates[0]
            if re.search(r'^GOOGLE_APPLICATION_CREDENTIALS=', content, flags=re.MULTILINE):
                content = re.sub(
                    r'^GOOGLE_APPLICATION_CREDENTIALS=.*$',
                    lambda _: "GOOGLE_APPLICATION_CREDENTIALS=" + new_cred,
                    content,
                    flags=re.MULTILINE
                )
            else:
                content = content.rstrip() + "\nGOOGLE_APPLICATION_CREDENTIALS=" + new_cred + "\n"

    # 4. 치유된 설정을 .env와 구글 드라이브 동기화용 백업(.env.portable)에 동시 저장
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(content)

    print("[포터블 환경 준비 완료] 현재 PC 작업 경로 자동 연결:", base_dir)


if __name__ == "__main__":
    sync_portable_env()