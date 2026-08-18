@echo off
title Remittance Converter Launcher

echo ================================================
echo   Remittance Converter - Starting...
echo ================================================
echo.

REM Get the folder where this .bat file lives
cd /d "%~dp0"

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Make sure Python is installed and added to PATH.
    pause
    exit /b 1
)

echo Starting File Converter + WhatsApp Sender (app.py)...
start "Remittance - App" cmd /k "cd /d "%~dp0" && python app.py"

REM Small delay so app.py initialises first
timeout /t 2 /nobreak >nul

echo Starting File Downloader + Scheduler (scheduler.py)...
start "Remittance - Scheduler" cmd /k "cd /d "%~dp0" && python scheduler.py"

echo.
echo ================================================
echo   Both scripts are now running.
echo   Two terminal windows have opened:
echo     1. "Remittance - App"       (converter + WhatsApp sender)
echo     2. "Remittance - Scheduler" (downloads TXT files on schedule)
echo.
echo   To stop: close both terminal windows.
echo ================================================
echo.
pause
