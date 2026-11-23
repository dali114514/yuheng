@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Yuheng Console
rem if already running, just open the browser
netstat -ano | findstr /r /c:"127.0.0.1:8765 .*LISTENING" >nul && (start "" http://127.0.0.1:8765 & exit /b)
".venv\Scripts\python.exe" run.py dashboard
pause
