@echo off
chcp 65001 >nul
title JAI Autonomous Assistant
cd /d "%~dp0"
python launch_jai.py %*
