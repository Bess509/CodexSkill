@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" test_bridge.py
pause
