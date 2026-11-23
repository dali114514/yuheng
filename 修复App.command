#!/bin/zsh
# 双击：关掉旧的控制台，重新生成「玉衡5.0.app」，然后打开它
cd "$(dirname "$0")"
echo "① 关闭旧的控制台……"
pkill -f "程序/console.py" 2>/dev/null; sleep 1
echo "② 重新生成 玉衡5.0.app……"
zsh 程序/build_app.sh || { echo "✗ 生成失败"; read "?按回车键关闭"; exit 1; }
echo "③ 打开……"
open "玉衡5.0.app"
sleep 6
if curl -s -m 2 http://127.0.0.1:8760/api/ping >/dev/null; then
  echo "✓ 控制台已启动。机器人、纪律程序、热数据、C2 监测会在 20 秒内自动开起来，看控制台的状态灯。"
else
  echo "✗ 控制台没有启动，把这个窗口截图发给 Claude。最后几行日志："; tail -5 "报告/控制台窗口输出.txt"
fi
echo
read "?按回车键关闭窗口"
