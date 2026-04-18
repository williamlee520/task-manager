@echo off
chcp 65001 >nul 2>&1
title 任务管理器
echo.
echo  ╔══════════════════════════════════════╗
echo  ║        📋 任务管理器 - 启动中        ║
echo  ╚══════════════════════════════════════╝
echo.

:: 检查 Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  ❌ 未检测到 Python，请先安装 Python 3.8+
    echo     下载地址: https://www.python.org/downloads/
    echo.
    echo  安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

:: 检查并安装依赖
python -c "import uvicorn" >nul 2>&1
if %errorlevel% neq 0 (
    echo  📦 正在安装依赖（首次运行需要）...
    pip install uvicorn fastapi pydantic -q
    if %errorlevel% neq 0 (
        echo  ❌ 依赖安装失败，请尝试手动执行:
        echo     pip install uvicorn fastapi pydantic
        pause
        exit /b 1
    )
    echo  ✅ 依赖安装完成
    echo.
)

:: 获取本机IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set IP=%%a
    goto :foundip
)
:foundip
set IP=%IP: =%

echo  ✅ 服务已启动！
echo.
echo  ┌─────────────────────────────────────────┐
echo  │  本机访问:  http://localhost:8787        │
echo  │  局域网访问: http://%IP%:8787   │
echo  └─────────────────────────────────────────┘
echo.
echo  📱 手机请在浏览器中打开「局域网访问」地址
echo  💡 可添加到主屏幕当 App 用
echo.
echo  按 Ctrl+C 停止服务
echo.

cd /d "%~dp0"
python -m uvicorn server:app --host 0.0.0.0 --port 8787
pause
