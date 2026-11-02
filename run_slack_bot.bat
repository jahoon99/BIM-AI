@echo off
chcp 65001 > nul
title BIM 데이터 어시스턴트 - Slack 챗봇 서버

echo ========================================================
echo [시스템 시작] 사내 Slack 연동 BIM AI 챗봇 서버를 구동합니다.
echo 이 창을 켜두면 사내 모든 직원이 Slack에서 챗봇을 사용할 수 있습니다.
echo ========================================================
echo.

cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else (
    echo [오류] venv 가상환경 폴더를 찾을 수 없습니다. 경로를 확인하세요.
    pause
    exit /b
)

python slack_bot.py

pause