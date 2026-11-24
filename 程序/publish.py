# -*- coding: utf-8 -*-
"""
玉衡 · 发布到 GitHub（公开仓库 dali114514/yuheng，main 分支 + 公开 Release）。

  每次更新：先改 VERSION（新功能 +0.1、小改 +0.0.1）和 CHANGELOG.md 最上面一段，再运行
      arch -arm64 程序/.venv/bin/python 程序/publish.py
  它会：
    1. 把「白名单」里的文件复制到 项目文件夹/.发布仓库/（这个仓库的本地副本）；
       只发代码、页面、标志、说明文档、研究报告；不发 设置.json、数据/、程序/data/、成交记录、日志、网站/、host_setup/、备份。
    2. 去掉个人信息（内网 / Tailscale 地址、本机用户路径），设置.json 换成密钥全清空的 设置.example.json。
    3. 安全检查：设置.json 里的每个密钥、存储口令、常见令牌格式只要在待发布的文件里出现一次，就停下、什么都不推。
    4. 提交、打标签 v<版本号>、推送 main，并用 CHANGELOG 里这一版的说明发一个公开 Release。
  版本号已经发布过（标签已存在）就停下：先改版本号。
  第一次运行时，会先把仓库原来的 main（另一台电脑上的期权价差机器人）完整保存到 v4-optbot 分支。

  需要 GitHub 命令行已登录（gh auth login）。只读本地数据，不碰交易。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = "dali114514/yuheng"
WORK = ROOT / ".发布仓库"
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"
OLD_BRANCH = "v4-optbot"
NAME, EMAIL = "dali114514", "dali114514@users.noreply.github.com"     # 提交署名用 GitHub 用户名，不用真实姓名 / 邮箱

INCLUDE = [
    "程序/*.py", "程序/*.html", "程序/*.css", "程序/*.js", "程序/*.sh", "程序/*.applescript", "程序/requirements.txt",
    "程序/brand/*",
    "玉衡5.0.command", "修复App.command", "玉衡5.0.bat",
    "VERSION", "CHANGELOG.md", "CONTRIBUTING.md", "CLAUDE.md", "数据结构.md", "使用说明.txt", "Discord机器人使用说明.md",
    "数据/说明.md", "数据/Claude使用说明.md", "数据/基本纪律.md",
    "报告/*回测*.md", "报告/开盘剧本统计_*.md", "报告/形态概率_*.md", "报告/功能测试和重复检查_*.md", "报告/AI泡沫周报_*.md",
]
EXCLUDE_NAMES = {"publish_readme.md"}
SECRET_KEY = re.compile(r"(ID|令牌|Webhook|密钥|口令|地址|项目名|token|key|secret|password)", re.I)
PRIVATE = [  # (正则, 替换)
    (re.compile(r"100\.118\.29\.12"), "<这台 Mac 的 Tailscale 地址>"),
    (re.compile(r"100\.100\.148\.1\b"), "<主机的 Tailscale 地址>"),
    (re.compile(r"192\.168\.1\.108"), "<主机的局域网地址>"),
    (re.compile(r"~/"), "~/"),
]
TOKEN_RE = re.compile(r"(sk-[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                      r"discord(app)?\.com/api/webhooks/\d+/[\w-]+|[\w-]{24}\.[\w-]{6}\.[\w-]{27})")


def sh(*cmd, cwd=None, check=True, capture=False):
    r = subprocess.run(list(cmd), cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise SystemExit(f"✗ {' '.join(cmd[:3])} 失败：{(r.stderr or r.stdout).strip()[-500:]}")
    return (r.stdout or "").strip() if capture else r


def version():
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def notes(v):
    t = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## v{re.escape(v)}\b.*?$(.*?)(?=^## v|\Z)", t, re.S | re.M)
    if not m:
        raise SystemExit(f"✗ CHANGELOG.md 里没有「## v{v}」这一段：先写更新说明")
    return m.group(1).strip()


def secrets():
    """要确保不会出现在公开文件里的值：设置.json 里的密钥类字段 + 存储口令文件。"""
    out = set()

    def walk(x, k=""):
        if isinstance(x, dict):
            for kk, vv in x.items():
                walk(vv, kk)
        elif isinstance(x, list):
            for vv in x:
                walk(vv, k)
        elif isinstance(x, str) and ((SECRET_KEY.search(k) and len(x) >= 8 and x not in ("127.0.0.1", "localhost"))
                                     or re.fullmatch(r"\d{15,20}", x)):
            out.add(x)
    try:
        walk(json.loads((ROOT / "设置.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    tok = ROOT / "host_setup" / "storage_token.txt"
    if tok.exists() and tok.read_text(encoding="utf-8").strip():
        out.add(tok.read_text(encoding="utf-8").strip())
    return out


def example_settings():
    cfg = json.loads((ROOT / "设置.json").read_text(encoding="utf-8"))

    def clean(x, k=""):
        if isinstance(x, dict):
            return {kk: clean(vv, kk) for kk, vv in x.items()}
        if isinstance(x, list):
            return [clean(vv, k) for vv in x]
        if isinstance(x, str) and SECRET_KEY.search(k) and x not in ("127.0.0.1", "localhost"):
            return ""
        if isinstance(x, str) and re.fullmatch(r"\d{15,20}", x):      # Discord 频道 / 用户 ID
            return ""
        return x
    cfg = clean(cfg)
    for k in ("Discord机器人", "Grok盘前", "网站分享", "存储", "重大新闻推送"):   # 要密钥的功能：示例里默认关，填好再开
        if isinstance(cfg.get(k), dict) and "启用" in cfg[k]:
            cfg[k]["启用"] = False
    cfg["说明"] = "示例设置：复制成 设置.json 再填自己的密钥（这里全部清空了）。" + str(cfg.get("说明") or "")
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def collect():
    files = []
    for pat in INCLUDE:
        for f in sorted(ROOT.glob(pat)):
            if f.is_file() and f.name not in EXCLUDE_NAMES and not f.name.startswith(".") and "周报_" != f.name[:3]:
                files.append(f)
    return files


def prepare(v, dry=False):
    """准备 .发布仓库：没有就克隆；第一次把原来的 main 存到 v4-optbot 分支。"""
    if not (WORK / ".git").exists():
        print(f"克隆 {REPO} → {WORK.name}")
        sh(GH, "repo", "clone", REPO, str(WORK))
    sh("git", "config", "user.name", NAME, cwd=WORK)
    sh("git", "config", "user.email", EMAIL, cwd=WORK)
    sh("git", "fetch", "--all", "--tags", cwd=WORK)
    if not dry and sh("git", "tag", "-l", f"v{v}", cwd=WORK, capture=True):
        raise SystemExit(f"✗ v{v} 已经发布过了：先改 VERSION（新功能 +0.1、小改 +0.0.1）和 CHANGELOG.md")
    remote_branches = sh("git", "branch", "-r", cwd=WORK, capture=True)
    first = f"origin/{OLD_BRANCH}" not in remote_branches
    if first:
        # 原来的 main 里没有 VERSION 文件 = 还是旧机器人：完整保存到 v4-optbot 分支
        old = sh("git", "ls-tree", "--name-only", "origin/main", cwd=WORK, capture=True).split()
        if "VERSION" not in old:
            print(f"第一次发布：把原来的 main（旧期权机器人）保存到 {OLD_BRANCH} 分支")
            sh("git", "push", "origin", f"origin/main:refs/heads/{OLD_BRANCH}", cwd=WORK)
    sh("git", "checkout", "-q", "-B", "main", "origin/main", cwd=WORK)
    return first


def build_tree(first):
    if first:   # 第一次：main 换成玉衡 5 的内容（旧内容已经在 v4-optbot 分支）
        for p in WORK.iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    else:       # 之后：只替换我们管理的部分，保证删掉的文件也同步删掉
        for p in WORK.iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    for f in collect():
        rel = f.relative_to(ROOT)
        dst = WORK / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix in (".png", ".ico", ".icns", ".mp3"):
            shutil.copy2(f, dst)
            continue
        t = f.read_text(encoding="utf-8")
        for rx, rep in PRIVATE:
            t = rx.sub(rep, t)
        dst.write_text(t, encoding="utf-8", newline="\r\n" if f.suffix == ".bat" else None)   # Windows 批处理要 CRLF 换行
        if f.suffix in (".sh", ".command"):
            dst.chmod(0o755)
    (WORK / "设置.example.json").write_text(example_settings(), encoding="utf-8")
    (WORK / "README.md").write_text((HERE / "publish_readme.md").read_text(encoding="utf-8").replace("{{VERSION}}", version()),
                                    encoding="utf-8")
    (WORK / ".gitattributes").write_text("*.bat text eol=crlf\n", encoding="utf-8")       # git 里也保持 CRLF
    (WORK / ".gitignore").write_text("设置.json\n程序/data/\n程序/.venv/\n__pycache__/\n*.pyc\n"
                                     "网站/\nhost_setup/\n.备份_改动前/\n*.app/\n.DS_Store\n", encoding="utf-8")


def scan():
    """发布前安全检查：任何密钥出现就停。"""
    bad = []
    sec = secrets()
    for f in WORK.rglob("*"):
        if ".git" in f.parts or not f.is_file() or f.suffix in (".png", ".ico", ".icns", ".mp3"):
            continue
        t = f.read_text(encoding="utf-8", errors="ignore")
        for s in sec:
            if s in t:
                bad.append(f"{f.relative_to(WORK)}：含有 设置.json 里的一个密钥（长度 {len(s)}）")
        m = TOKEN_RE.search(t)
        if m:
            bad.append(f"{f.relative_to(WORK)}：像是令牌 {m.group(0)[:6]}…")
        for rx, _ in PRIVATE:
            if rx.search(t):
                bad.append(f"{f.relative_to(WORK)}：还有个人地址")
    if bad:
        raise SystemExit("✗ 安全检查没通过，什么都没推：\n  " + "\n  ".join(bad))
    print(f"安全检查通过：查了 {len(sec)} 个密钥值和常见令牌格式")


def stamp_app(v):
    """本机 App 的版本号（Finder「显示简介」里看到的）跟 VERSION 对齐。App 不上传。"""
    app = ROOT / "玉衡5.0.app"
    plist = app / "Contents" / "Info.plist"
    if not plist.exists():
        return
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        if subprocess.run(["plutil", "-replace", key, "-string", v, str(plist)], capture_output=True).returncode != 0:
            subprocess.run(["plutil", "-insert", key, "-string", v, str(plist)], capture_output=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], capture_output=True)


def main():
    if not shutil.which(GH) and not Path(GH).exists():
        raise SystemExit("✗ 没有 GitHub 命令行（brew install gh）")
    if sh(GH, "auth", "status", check=False).returncode != 0:
        raise SystemExit("✗ GitHub 命令行还没登录：在终端运行 gh auth login")
    v = version()
    msg = notes(v)
    stamp_app(v)
    first = prepare(v)
    build_tree(first)
    scan()
    sh("git", "add", "-A", cwd=WORK)
    if not sh("git", "status", "--porcelain", cwd=WORK, capture=True):
        print("没有变化，不用发布")
        return
    files = sh("git", "diff", "--cached", "--name-only", cwd=WORK, capture=True).splitlines()
    sh("git", "commit", "-q", "-m", f"玉衡 v{v}\n\n{msg}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", cwd=WORK)
    sh("git", "tag", "-a", f"v{v}", "-m", f"玉衡 v{v}", cwd=WORK)
    if first:
        sh("git", "push", "--force", "origin", "main", cwd=WORK)     # 第一次：旧 main 已经存到 v4-optbot，覆盖
    else:
        sh("git", "push", "origin", "main", cwd=WORK)
    sh("git", "push", "origin", f"v{v}", cwd=WORK)
    sh(GH, "release", "create", f"v{v}", "--repo", REPO, "--title", f"玉衡 v{v}", "--notes", msg + "\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)", cwd=WORK)
    print(f"✓ 已发布 玉衡 v{v}（{len(files)} 个文件有变化）：https://github.com/{REPO}/releases/tag/v{v}")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:          # 只生成到 .发布仓库 并做安全检查，不提交不推送
        v = version()
        notes(v)
        first = prepare(v, dry=True)
        build_tree(first)
        scan()
        print(sh("git", "status", "--short", cwd=WORK, capture=True)[:3000])
    else:
        main()
