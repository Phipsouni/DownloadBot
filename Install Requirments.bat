@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade "pip>=26.2"
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install --upgrade -r Requirements.txt
if errorlevel 1 (
    echo Installation failed.
    pause
    exit /b 1
)
echo Dependencies installed. Run Start DL-06.bat.
pause
