@echo off
chcp 65001 > nul
title BIM AI - 슬랙 챗봇 및 6시간 자동 동기화 통합 서버

cd /d "%~dp0"

set PY_CMD=python
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import sys" > nul 2>&1
    if not errorlevel 1 (
        set PY_CMD="venv\Scripts\python.exe"
    )
)

%PY_CMD% -c "import dotenv, chromadb, gspread, slack_bolt" > nul 2>&1
if errorlevel 1 (
    echo [환경 자동 구성] 현재 PC에 필요한 파이썬 패키지를 자동으로 설치합니다...
    %PY_CMD% -m pip install python-dotenv streamlit langchain-google-genai langchain-core langchain-text-splitters chromadb gspread google-api-python-client google-auth pypdf python-docx python-pptx openpyxl slack-bolt
)

%PY_CMD% -u slack_bot.py
pause