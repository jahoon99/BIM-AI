@echo off
chcp 65001 > nul
title BIM AI - 웹 대시보드 (필요시 실행)

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [오류] venv 가상환경 폴더를 찾을 수 없습니다.
    pause
    exit /b
)

call "venv\Scripts\activate.bat"
streamlit run frontend/app.py
pause