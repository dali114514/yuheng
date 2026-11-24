# -*- coding: utf-8 -*-
"""
Windows 启动控制台（由「玉衡5.0.bat」用 pythonw 调用，不留黑窗口）：输出写进 报告/控制台窗口输出.txt，然后运行 console.py。
macOS 用 玉衡5.0.app（app_start.sh）。
"""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONWARNINGS", "ignore")
os.chdir(BASE)
(BASE / "报告").mkdir(exist_ok=True)
log = open(BASE / "报告" / "控制台窗口输出.txt", "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log
sys.path.insert(0, str(HERE))
sys.argv = [str(HERE / "console.py")] + sys.argv[1:]
runpy.run_path(str(HERE / "console.py"), run_name="__main__")
