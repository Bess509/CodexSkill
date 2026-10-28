@echo off
set "TASKNAME=AnthropicOpenAIBridgeBoot"

schtasks /Delete /TN "%TASKNAME%" /F
if errorlevel 1 (
  echo Task not found or delete failed - maybe never registered.
) else (
  echo Removed logon task %TASKNAME%.
)
echo Note: this only cancels auto-start. A running daemon is unaffected; use stop.bat
pause
