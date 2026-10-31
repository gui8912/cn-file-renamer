@echo off
cd /d "%~dp0"
title 中文文件批量改名器
chcp 65001 >nul
python app.py
if errorlevel 1 (
    echo.
    echo Startup failed. Install Python 3.9+ and ensure it is on PATH.
    pause
)
