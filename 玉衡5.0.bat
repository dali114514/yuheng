@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "VENV=程序\.venv"
rem 玉衡5.0（Windows）：双击运行。第一次会建 Python 环境、装依赖；之后直接打开控制台（浏览器 http://127.0.0.1:8760）。
if not exist "%VENV%\Scripts\python.exe" (
  echo 首次运行：正在准备 Python 环境（约 1-2 分钟，只需一次）……
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv "%VENV%"
  ) else (
    python -m venv "%VENV%"
  )
)
if not exist "%VENV%\Scripts\python.exe" (
  echo 没有找到 Python 3.10 以上版本。请先从 https://www.python.org/downloads/ 安装（安装时勾选 Add python.exe to PATH），再双击运行。
  pause
  exit /b 1
)
"%VENV%\Scripts\python.exe" 程序\win_setup.py
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "%VENV%\Scripts\pythonw.exe" 程序\win_start.py
echo 控制台正在启动，稍等几秒会自动打开浏览器（http://127.0.0.1:8760）。这个窗口可以关掉。
timeout /t 5 >nul
