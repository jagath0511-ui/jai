@echo off
title JAI - Personal AI Assistant & Studio
color 0B
cd /d "%~dp0"

echo ======================================================================
echo       _   _   ___ 
echo      ^| ^| /_\ ^|_ _^|       Personal Local-First AI Assistant
echo   _  ^| ^|/ _ \ ^| ^|  
echo  ^| ^|_^| / _ \ \^| ^|        JAI Studio Launcher
echo   \___/_/ \_\___^|        Web UI: http://127.0.0.1:8000
echo ======================================================================
echo.
echo [1] Launch JAI Web Studio & API Server (Opens in Browser)
echo [2] Launch Full Background Orchestrator (Voice, Inbox Watcher, Telegram)
echo [3] Run System Quick Health & Test Run
echo [4] Check System Status via CLI
echo [5] Open JAI Interactive Command Console (Type commands to JAI)
echo [6] Launch Headless Voice Assistant (Wake Word: "JAI", Zero Widgets)
echo.
set /p choice="Select an option (1-6, or Enter for 1): "

if "%choice%"=="2" goto LAUNCH_ORCHESTRATOR
if "%choice%"=="3" goto TEST_RUN
if "%choice%"=="4" goto CLI_STATUS
if "%choice%"=="5" goto INTERACTIVE_COMMAND
if "%choice%"=="6" goto LAUNCH_VOICE

:LAUNCH_SERVER
echo.
echo Launching JAI Web Studio on http://127.0.0.1:8000 ...
python src/server.py --open-browser
goto END

:LAUNCH_ORCHESTRATOR
echo.
echo Launching JAI Master Background Orchestrator...
python run_jai.py
goto END

:TEST_RUN
echo.
echo Running JAI Self-Test...
python run_jai.py --test-run
pause
goto END

:CLI_STATUS
echo.
python -m src.cli status
pause
goto END

:INTERACTIVE_COMMAND
echo.
python command_jai.py
goto END

:LAUNCH_VOICE
echo.
echo Launching JAI Headless Voice Assistant (Zero Widgets)...
python voice_listener.py
goto END

:END
pause

