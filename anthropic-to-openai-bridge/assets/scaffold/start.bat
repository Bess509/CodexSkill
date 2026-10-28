@echo off
rem All-ASCII on purpose (cmd UTF-8 offset bug with multi-byte chars).
cd /d "%~dp0"

if not exist ".env" (
  if exist "env.example" (
    copy "env.example" ".env" >nul
    echo Created .env from template. Please fill in, then run this file again.
    notepad ".env"
    pause
    exit /b 1
  ) else (
    echo env.example not found. Scaffold incomplete.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo .venv missing. Run:  python -m venv .venv
  echo then:               .venv\Scripts\pip install "litellm[proxy]==1.106.4"
  pause
  exit /b 1
)

".venv\Scripts\python.exe" start_proxy.py
pause
