@echo off
setlocal
title Daily Tasks - Server

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

REM Check if .venv exists
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo Virtual environment not found! Running installer first...
    call "%ROOT%\install.bat"
    exit /b %errorlevel%
)

echo Starting Daily Tasks on http://0.0.0.0:5000 ...
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\app.py"

if errorlevel 1 (
    echo.
    echo Server exited with an error.
    pause
)