# -*- coding: utf-8 -*-
"""
Windows 首次安装 / 更新依赖（由根目录的「玉衡5.0.bat」调用；macOS 用「玉衡5.0.command」）。
  1. 检查 Python 版本（要 3.10 以上）
  2. requirements.txt 比上次安装新（或第一次）→ 装依赖
  3. 没有 设置.json → 从 设置.example.json 复制一份，并提示去填
成功返回 0；失败返回 1（bat 会停住让你看错误）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent


def main():
    if sys.version_info < (3, 10):
        print(f"Python 版本太旧（{sys.version.split()[0]}），要 3.10 以上：https://www.python.org/downloads/")
        return 1
    req = HERE / "requirements.txt"
    mark = Path(sys.prefix) / ".installed"
    if not mark.exists() or req.stat().st_mtime > mark.stat().st_mtime:
        print("正在安装 / 更新依赖（第一次约几分钟）……", flush=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--upgrade", "pip"])
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)])
        if r.returncode != 0:
            print("安装依赖失败：请检查网络后再双击一次。")
            return 1
        mark.write_text("ok", encoding="utf-8")
    cfg, ex = BASE / "设置.json", BASE / "设置.example.json"
    if not cfg.exists() and ex.exists():
        shutil.copy(ex, cfg)
        print("已经建好 设置.json（从 设置.example.json 复制）。至少确认 moomoo 的 OpenD 地址和端口（默认 127.0.0.1:11111），"
              "其他要密钥的功能默认是关的。详见 README。")
    (BASE / "报告").mkdir(exist_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
