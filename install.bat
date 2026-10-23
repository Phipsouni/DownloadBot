@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo Dependencies installed.
pause
exit /b 0
:error
echo Installation failed. Check that Python 3 is installed.
pause
exit /b 1
