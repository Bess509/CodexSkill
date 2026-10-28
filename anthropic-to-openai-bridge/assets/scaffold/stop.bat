@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" daemon_ctl.py stop
pause
