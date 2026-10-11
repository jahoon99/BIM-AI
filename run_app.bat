@echo off
chcp 65001 > nul
title BIM 데이터 어시스턴트 서버

echo ========================================================
echo [시스템 시작] BIM 데이터 어시스턴트 웹 서버를 구동합니다.
echo 이 검은색 창(서버)을 닫으면 웹 화면 연결이 종료됩니다.
echo ========================================================
echo.

:: 1. 배치 파일이 위치한 프로젝트 최상위 폴더로 이동
cd /d "%~dp0"

:: 2. 파이썬 가상환경(venv) 활성화
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else (
    echo [오류] venv 가상환경 폴더를 찾을 수 없습니다. 경로를 확인하세요.
    pause
    exit /b
)

:: 3. Streamlit 웹 애플리케이션 실행
streamlit run frontend/app.py

pause