@echo off
rem All-ASCII on purpose (cmd UTF-8 offset bug with multi-byte chars).
rem Registers a per-user logon-start scheduled task that launches the
rem daemon directly via pythonw (no console window, no pause).
cd /d "%~dp0"

set "TASKNAME=AnthropicOpenAIBridgeBoot"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "DAEMON=%~dp0daemon.py"

if not exist "%PYW%" (
  echo .venv missing. Run:  python -m venv .venv
  echo then:               .venv\Scripts\pip install "litellm[proxy]==1.106.4"
  pause
  exit /b 1
)
if not exist "%DAEMON%" (
  echo daemon.py not found. Run this file from the scaffold directory.
  pause
  exit /b 1
)
if not exist ".env" (
  echo .env not found. Create it from env.example and fill it in first.
  pause
  exit /b 1
)

schtasks /Create /TN "%TASKNAME%" /TR "\"%PYW%\" \"%DAEMON%\"" /SC ONLOGON /DELAY 0000:30 /RL LIMITED /F
if errorlevel 1 (
  echo.
  echo Registration failed. Fallback without /DELAY:
  schtasks /Create /TN "%TASKNAME%" /TR "\"%PYW%\" \"%DAEMON%\"" /SC ONLOGON /RL LIMITED /F
  if errorlevel 1 (
    echo Registration failed twice. Register manually in Task Scheduler.
    pause
    exit /b 1
  )
)

echo.
echo Registered logon task %TASKNAME%.
echo It starts the daemon 30s after logon, no window, auto-restart on crash.
echo Verify in Task Scheduler, or:  schtasks /Query /TN %TASKNAME%
echo Uninstall: run uninstall_boot.bat
pause
