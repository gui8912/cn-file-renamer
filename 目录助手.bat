@echo off
cd /d "%~dp0"
title 中文文件批量改名器 - 拖放助手
chcp 65001 >nul
where pythonw.exe >nul 2>&1
if errorlevel 1 (
    start "" python.exe folder_bridge.py --drop
) else (
    start "" pythonw.exe folder_bridge.py --drop
)
