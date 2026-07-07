@echo off
rem Launch the Telegram bot from its own folder, logging to bot.log.
cd /d "%~dp0"
".venv\Scripts\python.exe" -u bot.py >> "bot.log" 2>&1
