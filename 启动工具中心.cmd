@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
".venv\Scripts\python.exe" -c "import cryptography, psutil, requests" >nul 2>&1
if errorlevel 1 (
  echo 首次启动，正在准备工具中心组件……
  ".venv\Scripts\python.exe" -X utf8 launcher.py --prepare
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -X utf8 launcher.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动未完成，请检查 Python 安装与网络连接，或查看 state\hub.log。
pause
exit /b 1
