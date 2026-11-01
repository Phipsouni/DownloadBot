@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" DL-06.py
) else (
    python DL-06.py
)
pause
