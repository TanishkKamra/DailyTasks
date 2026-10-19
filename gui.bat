@echo off
setlocal
title Daily Tasks - Control Panel

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

REM Check if Python environment and dependencies exist
if not exist "%ROOT%\.venv\Scripts\pythonw.exe" goto :need_install

"%ROOT%\.venv\Scripts\python.exe" -c "import flask, rapidocr_onnxruntime, pymupdf, pandas" >nul 2>&1
if errorlevel 1 goto :need_install

goto :launch

:need_install
echo Setting up dependencies...
call "%ROOT%\install.bat" --gui
if errorlevel 1 (
    echo.
    echo Installation encountered an issue.
    pause
    exit /b 1
)

:launch
REM Launch GUI silently with pythonw in background and close batch terminal immediately
start "" "%ROOT%\.venv\Scripts\pythonw.exe" "%ROOT%\server_gui.py"
exit /b 0
