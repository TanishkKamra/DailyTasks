@echo off
setlocal enabledelayedexpansion
title Daily Tasks - All-in-One Installer

echo ====================================================================
echo               DAILY TASKS - AUTOMATED DEPENDENCY INSTALLER
echo ====================================================================
echo.

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

REM --- 1. Detect Python Installation -----------------------------------
echo [1/5] Detecting Python on system...
set "SYS_PY="

REM Check py launcher
where py >nul 2>&1
if not errorlevel 1 (
    set "SYS_PY=py -3"
    goto :found_python
)

REM Check python in PATH
where python >nul 2>&1
if not errorlevel 1 (
    set "SYS_PY=python"
    goto :found_python
)

REM Check python3 in PATH
where python3 >nul 2>&1
if not errorlevel 1 (
    set "SYS_PY=python3"
    goto :found_python
)

REM Check common Windows Python install locations
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python310\python.exe"
    "C:\Python311\python.exe"
    "C:\Python312\python.exe"
    "C:\Python310\python.exe"
) do (
    if exist "%%~P" (
        set "SYS_PY=%%~P"
        goto :found_python
    )
)

echo [ERROR] Python 3.10+ was not found on your system!
echo Please download and install Python from https://www.python.org/
echo (Important: Check "Add Python to PATH" during installation)
echo.
pause
exit /b 1

:found_python
for /f "tokens=*" %%i in ('%SYS_PY% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set "PY_VER=%%i"
echo   Found Python version: %PY_VER%

REM --- 2. Create Virtual Environment -----------------------------------
echo.
echo [2/5] Setting up isolated Python virtual environment (.venv)...
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    %SYS_PY% -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment!
        pause
        exit /b 1
    )
    echo   Created virtual environment at %ROOT%\.venv
) else (
    echo   Existing virtual environment detected.
)

set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "VENV_PIP=%ROOT%\.venv\Scripts\pip.exe"

REM --- 3. Install Required Dependencies ---------------------------------
echo.
echo [3/5] Installing all dependencies from requirements.txt...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel --no-warn-script-location >nul 2>&1

echo   Installing packages (OpenCV, RapidOCR, PyMuPDF, Flask, Pandas, openpyxl, requests)...
"%VENV_PIP%" install -r "%ROOT%\requirements.txt" --no-warn-script-location
if errorlevel 1 (
    echo [ERROR] Package installation failed!
    pause
    exit /b 1
)

REM --- 4. Prepare Runtime Directories -----------------------------------
echo.
echo [4/5] Preparing application folders...
if not exist "%ROOT%\uploads" mkdir "%ROOT%\uploads"
if not exist "%ROOT%\outputs" mkdir "%ROOT%\outputs"
if not exist "%ROOT%\log" mkdir "%ROOT%\log"
if not exist "%ROOT%\log\PANLOGS" mkdir "%ROOT%\log\PANLOGS"
if not exist "%ROOT%\log\AADHARLOGS" mkdir "%ROOT%\log\AADHARLOGS"
echo   Created runtime directories.

REM --- 5. Verify Core Packages ------------------------------------------
echo.
echo [5/5] Verifying installation...
"%VENV_PY%" -c "import flask, cv2, rapidocr_onnxruntime, pymupdf, pandas, openpyxl, requests; print('  [OK] All dependencies installed and verified successfully!')"
if errorlevel 1 (
    echo [ERROR] Verification failed. Some packages are missing.
    pause
    exit /b 1
)

echo.
echo ====================================================================
echo         INSTALLATION COMPLETED SUCCESSFULLY!
echo ====================================================================
echo.

REM If invoked with --gui or --silent parameter, exit cleanly
if /i "%1"=="--gui" exit /b 0
if /i "%1"=="--silent" exit /b 0

echo You can launch the application anytime by double-clicking:
echo   gui.bat  (Desktop Control Panel)
echo.
set /p LAUNCH="Would you like to launch the GUI Control Panel now? (Y/N, default Y): "
if /i "%LAUNCH%"=="N" (
    exit /b 0
)

start "" "%ROOT%\.venv\Scripts\pythonw.exe" "%ROOT%\server_gui.py"
exit /b 0
