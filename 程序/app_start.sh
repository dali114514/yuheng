#!/bin/zsh
# 由 玉衡5.0.app 调用：用 M 芯片原生模式启动控制台
APP="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$(dirname "$APP")"
PY="程序/.venv/bin/python"
if [ ! -x "$PY" ]; then
  osascript -e 'display dialog "第一次使用：请先双击「玉衡5.0.command」完成环境安装，再打开玉衡5.0。" buttons {"好"} default button 1 with title "玉衡5.0"'
  exit 1
fi
ARCH=()
[ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && ARCH=(arch -arm64)
if [ ! -f "程序/.venv/.installed" ] || [ "程序/requirements.txt" -nt "程序/.venv/.installed" ]; then
  "${ARCH[@]}" "$PY" -m pip install -q -r "程序/requirements.txt" && touch "程序/.venv/.installed"
fi
mkdir -p 报告
export PYTHONWARNINGS=ignore PYTHONUNBUFFERED=1
nohup "${ARCH[@]}" "$PY" "程序/console.py" >> "报告/控制台窗口输出.txt" 2>&1 &
