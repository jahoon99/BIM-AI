@echo off
chcp 65001 > nul
title BIM AI - 슬랙 챗봇 및 자동 동기화 서버 (완전 포터블 모드)

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

REM 현재 PC 폴더 경로에 맞춰 .env 내의 인증키 및 DB 경로를 0.1초 만에 자동 보정
%PY_CMD% auto_portable_env.py

%PY_CMD% -u slack_bot.py
pause