@echo off
chcp 65001 > nul
cd /d "%~dp0"

call "venv\Scripts\activate.bat"
python -u slack_bot.py >> server_log.txt 2>&1