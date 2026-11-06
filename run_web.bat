@echo off
chcp 65001 > nul
title BIM AI - 웹 대시보드 (경로 자동 호환 모드)

cd /d "%~dp0"

REM 1. 가상환경 파이썬이 정상 작동하는지 확인하고, 다른 PC여서 경로가 깨졌으면 시스템 파이썬 사용
set PY_CMD=python
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import sys" > nul 2>&1
    if not errorlevel 1 (
        set PY_CMD="venv\Scripts\python.exe"
    )
)

REM 2. 필수 모듈(dotenv, streamlit 등) 설치 여부 확인 후 없으면 자동 설치
%PY_CMD% -c "import dotenv, streamlit, chromadb, gspread, slack_bolt" > nul 2>&1
if errorlevel 1 (
    echo [환경 자동 구성] 현재 PC에 필요한 파이썬 패키지를 자동으로 설치합니다...
    %PY_CMD% -m pip install python-dotenv streamlit langchain-google-genai langchain-core langchain-text-splitters chromadb gspread google-api-python-client google-auth pypdf python-docx python-pptx openpyxl slack-bolt
)

REM 3. 절대경로에 영향받지 않는 python -m 방식으로 Streamlit 실행
%PY_CMD% -m streamlit run frontend/app.py
pause