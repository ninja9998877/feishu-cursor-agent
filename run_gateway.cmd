@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
chcp 65001 >nul
:loop
echo gateway starting...>> "logs\gateway.log"
".venv\Scripts\python.exe" main.py >> "logs\gateway.log" 2>&1
echo gateway exited errorlevel=%errorlevel%>> "logs\gateway.log"
timeout /t 5 /nobreak >nul
goto loop
