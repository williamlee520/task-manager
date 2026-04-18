#!/bin/bash
cd "$(dirname "$0")"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║        📋 任务管理器 - 启动中        ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# 检查 Python3
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "  ❌ 未检测到 Python，请先安装 Python 3.8+"
    exit 1
fi

# 检查并安装依赖
$PY -c "import uvicorn" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "  📦 正在安装依赖（首次运行需要）..."
    $PY -m pip install uvicorn fastapi pydantic -q
    echo "  ✅ 依赖安装完成"
    echo ""
fi

# 获取本机IP
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')

echo "  ✅ 服务已启动！"
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │  本机访问:  http://localhost:8787        │"
echo "  │  局域网访问: http://$IP:8787  │"
echo "  └─────────────────────────────────────────┘"
echo ""
echo "  📱 手机请在浏览器中打开「局域网访问」地址"
echo "  💡 可添加到主屏幕当 App 用"
echo ""
echo "  按 Ctrl+C 停止服务"
echo ""

$PY -m uvicorn server:app --host 0.0.0.0 --port 8787
