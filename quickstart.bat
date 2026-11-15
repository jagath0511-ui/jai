@echo off
title JAI Browser - 1-Click Setup & Launch
cd /d "%~dp0"

echo ======================================================================
echo             JAI Browser - Autonomous Web Agent Setup
echo ======================================================================
echo.

:: 1. Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not found in your system PATH.
    echo.
    echo Please install Python 3.10, 3.11, or 3.12 from:
    echo https://www.python.org/downloads/
    echo.
    echo *IMPORTANT*: Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: 2. One-Time Package Installation
echo [1/3] Verifying and installing JAI dependencies...
python -m pip install -e .
if %errorlevel% neq 0 (
    echo [WARNING] Pip install had issues. Attempting to continue...
)

:: 3. Run Preflight Health Check
echo.
echo [2/3] Running preflight system diagnostic...
python scripts/doctor.py

:: 4. Launch Interactive Browser CLI
echo.
echo [3/3] Starting JAI Browser...
echo.
python src/cli.py

echo.
pause
