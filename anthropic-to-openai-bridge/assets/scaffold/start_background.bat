@echo off
rem All-ASCII on purpose: cmd parser has a known offset bug with UTF-8 + chcp 65001
rem when the batch contains multi-byte characters. English messages keep it portable.
cd /d "%~dp0"

if not exist ".env" (
  if exist "env.example" (
    copy "env.example" ".env" >nul
    echo Created .env from template. Please fill it in, then run this file again.
    notepad ".env"
    pause
    exit /b 1
  ) else (
    echo env.example not found. Scaffold incomplete.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo .venv missing. Run:  python -m venv .venv
  echo then:               .venv\Scripts\pip install "litellm[proxy]==1.106.4"
  pause
  exit /b 1
)

rem pythonw is a GUI-subsystem binary: cmd does not wait, no window appears.
rem daemon.py itself checks port conflicts and writes logs\daemon.pid.
".venv\Scripts\pythonw.exe" daemon.py

timeout /t 3 /nobreak >nul
".venv\Scripts\python.exe" daemon_ctl.py status
echo.
echo Logs: logs\daemon.log  logs\proxy.log   Stop: stop.bat
pause
