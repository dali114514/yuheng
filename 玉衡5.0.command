#!/bin/zsh
# 双击运行：拉取数据 → 分析 → 在「报告」文件夹生成 Word 报告
cd "$(dirname "$0")"
APP="程序"
VENV="$APP/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for p in "$HOME/.pyenv/shims/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="$p"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "没有找到 Python 3.9 以上版本。请先从 https://www.python.org/downloads/ 安装，再双击运行。"
    read "?按回车键关闭窗口"; exit 1
  fi
  echo "首次运行：正在准备环境（约 1–2 分钟，只需一次）……"
  "$PY" -m venv "$VENV" || { read "?创建环境失败，按回车键关闭"; exit 1; }
fi

# 首次安装依赖（或程序更新后依赖有变化）；之后每 30 天自动更新一次 yfinance（Yahoo 接口常变）
if [ ! -f "$VENV/.installed" ] || [ "$APP/requirements.txt" -nt "$VENV/.installed" ]; then
  echo "正在安装/更新依赖……"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r "$APP/requirements.txt" && touch "$VENV/.installed" \
    || { read "?安装依赖失败，请检查网络后重试。按回车键关闭"; exit 1; }
elif [ -n "$(find "$VENV/.installed" -mtime +30 2>/dev/null)" ]; then
  echo "正在更新数据接口……"
  "$VENV/bin/python" -m pip install -q -U yfinance && touch "$VENV/.installed"
fi

PYTHONWARNINGS=ignore "$VENV/bin/python" "$APP/macro_report.py"
echo
read "?按回车键关闭窗口"
