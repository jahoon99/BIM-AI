@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==================================================================
echo [비아이엠팩토리 AI 업무 지원 시스템] BF 파일 분석 및 슬랙 봇 가동
echo ==================================================================
python data_pipeline/hybrid_server_sync.py
echo.
echo [슬랙 AI 어시스턴트 실시간 수신 대기 시작...]
python slack_bot.py
pause
