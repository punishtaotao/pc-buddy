@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 电脑管家 PC-Buddy

echo.
echo   ============================================
echo      电脑管家 PC-Buddy   正在启动
echo   ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo   [x] 没找到 python。请先安装 Python 3.10+，装的时候记得勾选 Add to PATH。
    echo.
    pause
    exit /b 1
)

if not exist "config.json" (
    if exist "config.example.json" copy /y "config.example.json" "config.json" >nul
    echo   [i] 已生成 config.json
)

echo   [*] 检查依赖...
python -c "import pyautogui, PIL, pygetwindow" >nul 2>nul
if errorlevel 1 (
    echo   [*] 缺少依赖，正在安装（第一次会慢一点）...
    python -m pip install -r requirements.txt --disable-pip-version-check
)
python -c "import qrcode" >nul 2>nul
if errorlevel 1 python -m pip install "qrcode[pil]" --quiet --disable-pip-version-check

echo   [*] 启动服务...
echo.
python server.py

echo.
echo   服务已退出。
pause
