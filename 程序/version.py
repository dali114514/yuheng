# -*- coding: utf-8 -*-
"""玉衡5.0 的版本号：项目根目录的 VERSION 文件（每次更新都要改，见 CLAUDE.md「版本号和发布」）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get():
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "5.0.0"
    except Exception:
        return "5.0.0"


def page(path):
    """读一个页面文件，把 {{VERSION}} 换成当前版本号。"""
    return Path(path).read_bytes().replace(b"{{VERSION}}", get().encode())
