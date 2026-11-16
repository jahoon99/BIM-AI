@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==================================================================
echo [비아이엠팩토리 사내 통합 자료 기반 AI 업무 지원 시스템] 웹 포털 실행
echo ==================================================================
streamlit run frontend/app.py --theme.base "light" --theme.primaryColor "#0F172A" --theme.backgroundColor "#F8FAFC" --theme.secondaryBackgroundColor "#FFFFFF" --theme.textColor "#0F172A"
pause
