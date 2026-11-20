@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [FFCraft Studio] Python 3.10+ is required. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Starting FFCraft Studio...
python run.py %*
if errorlevel 1 pause
