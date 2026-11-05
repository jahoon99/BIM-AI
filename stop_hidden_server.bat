@echo off
chcp 65001 > nul
echo 백그라운드에서 실행 중인 BIM AI 슬랙 및 동기화 서버를 종료합니다...
wmic process where "name='python.exe' and commandline like '%%slack_bot.py%%'" call terminate > nul 2>&1
echo [완료] 백그라운드 서버가 안전하게 종료되었습니다.
pause