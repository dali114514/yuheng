# -*- coding: utf-8 -*-
"""
网站分享：把 Word 报告转成网页 → 上传到 Cloudflare Pages（同事用浏览器看）
          + 把报告摘要和盘中提醒推送到工作群（企业微信 / 飞书 / 钉钉 / Slack / Discord）。

设置在「设置.json → 网站分享」。没开启或没填好时什么都不做，不影响原来的报告和弹窗。
手动测试：控制台 → 工具 →「测试网站和推送」。
"""
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
REPORT_DIR = BASE / "报告"
SITE = BASE / "网站"

DEFAULT = {
    "启用": False,
    "说明": "填好 Cloudflare 的三项后把「启用」改成 true。API令牌只放在你自己电脑上，不要发给别人。",
    "Cloudflare账户ID": "",
    "Cloudflare_API令牌": "",
    "网站项目名": "macro-fengxiangbiao",
    "保留报告份数": 60,
    "群推送": {
        "类型": "",
        "Webhook地址": "",
        "钉钉加签密钥": "",
        "推送报告摘要": True,
        "推送盘中提醒": True,
        "说明": "类型填：企业微信 / 飞书 / 钉钉 / Slack / Discord 之一；不需要推送就留空。"
    },
}


def settings(cfg):
    s = dict(DEFAULT)
    s.update(cfg.get("网站分享") or {})
    p = dict(DEFAULT["群推送"])
    p.update(s.get("群推送") or {})
    s["群推送"] = p
    return s


def site_url(s):
    return f"https://{s['网站项目名']}.pages.dev"


# ================================================================ Word → 网页
def docx_to_html(docx_path, img_dir, img_prefix):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(docx_path))
    img_dir.mkdir(parents=True, exist_ok=True)

    def run_html(r):
        out = ""
        for blip in r._element.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            part = doc.part.related_parts.get(rid)
            if part is None:
                continue
            ext = Path(part.partname).suffix or ".png"
            name = hashlib.sha1(part.blob).hexdigest()[:16] + ext
            (img_dir / name).write_bytes(part.blob)
            out += f'<img src="{img_prefix}{name}" alt="走势图">'
        t = html.escape(r.text or "")
        if t:
            style = []
            try:
                rgb = r.font.color.rgb if r.font.color is not None and r.font.color.type is not None else None
            except Exception:
                rgb = None
            if rgb is not None:
                style.append(f"color:#{rgb}")
            if r.font.size:
                style.append(f"font-size:{r.font.size.pt / 10.5:.2f}em")
            if r.bold:
                t = f"<b>{t}</b>"
            if style:
                t = f'<span style="{";".join(style)}">{t}</span>'
            out += t
        return out

    def para_inner(p):
        return "".join(run_html(r) for r in p.runs)

    parts, in_list = [], False
    heads = {"Title": "h1", "Heading 1": "h2", "Heading 2": "h3", "Heading 3": "h4"}
    for el in doc.element.body.iterchildren():
        tag = el.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(el, doc)
            style = p.style.name if p.style is not None else ""
            inner = para_inner(p)
            is_li = style.startswith("List")
            if in_list and not is_li:
                parts.append("</ul>")
                in_list = False
            if not inner.strip():
                continue
            if is_li:
                if not in_list:
                    parts.append("<ul>")
                    in_list = True
                parts.append(f"<li>{inner}</li>")
            elif style in heads:
                h = heads[style]
                parts.append(f"<{h}>{inner}</{h}>")
            else:
                parts.append(f"<p>{inner}</p>")
        elif tag == "tbl":
            if in_list:
                parts.append("</ul>")
                in_list = False
            t = Table(el, doc)
            rows = []
            for i, row in enumerate(t.rows):
                cell_tag = "th" if i == 0 else "td"
                cells = "".join(f"<{cell_tag}>" + "<br>".join(para_inner(p) for p in c.paragraphs if p.runs)
                                + f"</{cell_tag}>" for c in row.cells)
                rows.append(f"<tr>{cells}</tr>")
            parts.append('<div class="tw"><table>' + "".join(rows) + "</table></div>")
    if in_list:
        parts.append("</ul>")
    return "\n".join(parts)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7280;--line:#e3e6eb;--head:#1f3a5f;--th:#eef2f7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:16px}
nav{display:flex;gap:16px;flex-wrap:wrap;align-items:center;padding:12px 0;border-bottom:1px solid var(--line);margin-bottom:16px}
nav b{color:var(--head);font-size:17px;margin-right:auto}
nav a{color:var(--head);text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 24px;margin-bottom:16px}
h1{color:var(--head);font-size:26px;margin:0 0 8px}
h2{color:var(--head);font-size:20px;margin:28px 0 10px;padding-top:12px;border-top:1px solid var(--line)}
h3{color:var(--head);font-size:16px;margin:18px 0 8px}
p{margin:6px 0}
ul{margin:6px 0;padding-left:22px}
.tw{overflow-x:auto;margin:8px 0 12px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--th);white-space:nowrap}
img{max-width:100%;height:auto;display:block;margin:10px 0}
.list a{display:block;padding:6px 0;color:var(--head);text-decoration:none;border-bottom:1px dashed var(--line)}
pre{white-space:pre-wrap;font:13.5px/1.6 ui-monospace,Menlo,monospace;margin:0}
.mut{color:var(--mut);font-size:13px}
"""


def page(title, body, prefix=""):
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<nav><b>玉衡5.0</b><a href="{prefix}index.html">最新报告</a><a href="{prefix}history.html">历史报告</a><a href="{prefix}alerts.html">盘中提醒</a></nav>
{body}
<p class="mut">报告按固定规则生成，仅用于辅助复盘，不构成投资建议。</p>
</div></body></html>"""


def _report_key(docx_path):
    m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{4})", Path(docx_path).stem)
    return m.group(1) if m else dt.datetime.now().strftime("%Y-%m-%d_%H%M")


def _label(key):
    d, t = key.split("_")
    return f"{d} {t[:2]}:{t[2:]}"


def build_alerts_page():
    logs = sorted(REPORT_DIR.glob("盘中监测_*.txt"), reverse=True)[:5]
    blocks = []
    for f in logs:
        day = f.stem.replace("盘中监测_", "")
        txt = f.read_text(encoding="utf-8", errors="ignore").strip()
        # 只展示真实提醒（不展示开启/测试记录）
        entries = re.split(r"\n(?=\[\d{2}:\d{2}:\d{2} 美东\])", txt)
        keep = [e for e in entries if "新提醒" in e]
        body = html.escape("\n\n".join(keep)) if keep else "当天没有提醒。"
        blocks.append(f'<div class="card"><h3>{day}</h3><pre>{body}</pre></div>')
    if not blocks:
        blocks.append('<div class="card"><p>还没有盘中提醒记录。</p></div>')
    (SITE / "alerts.html").write_text(page("盘中提醒 · 玉衡5.0",
                                          '<div class="card"><h1>盘中提醒</h1><p class="mut">最近 5 个交易日，时间为美东时间。'
                                          '这些是按规则筛出的候选，不是买卖建议。</p></div>' + "".join(blocks)),
                                     encoding="utf-8")


def build_site(docx_path, s):
    SITE.mkdir(exist_ok=True)
    rdir = SITE / "r"
    rdir.mkdir(exist_ok=True)
    key = _report_key(docx_path)
    body = docx_to_html(docx_path, SITE / "img", "../img/")
    (rdir / f"{key}.html").write_text(page(f"玉衡5.0 {_label(key)}", f'<div class="card">{body}</div>', "../"),
                                      encoding="utf-8")

    # 只保留最近 N 份
    reports = sorted(rdir.glob("*.html"), reverse=True)
    for old in reports[int(s.get("保留报告份数", 60)):]:
        old.unlink()
    reports = sorted(rdir.glob("*.html"), reverse=True)

    # 首页 = 最新一份
    latest = reports[0]
    txt = latest.read_text(encoding="utf-8").replace('src="../img/', 'src="img/')
    txt = re.sub(r'href="\.\./', 'href="', txt)
    (SITE / "index.html").write_text(txt, encoding="utf-8")

    items = "".join(f'<a href="r/{p.name}">{_label(p.stem)}</a>' for p in reports)
    (SITE / "history.html").write_text(page("历史报告 · 玉衡5.0",
                                            f'<div class="card"><h1>历史报告</h1><div class="list">{items}</div></div>'),
                                       encoding="utf-8")
    build_alerts_page()

    # 清理不再被引用的图片
    used = set()
    for f in [SITE / "index.html", *reports]:
        used |= set(re.findall(r'img/([0-9a-f]{16}\.\w+)', f.read_text(encoding="utf-8")))
    for img in (SITE / "img").glob("*"):
        if img.name not in used:
            img.unlink()

    (SITE / "_headers").write_text("/*\n  X-Robots-Tag: noindex\n  Cache-Control: no-cache\n", encoding="utf-8")
    return key


# ================================================================ 上传到 Cloudflare Pages
def _find_npx():
    for p in (shutil.which("npx"), "/opt/homebrew/bin/npx", "/usr/local/bin/npx",
              str(Path.home() / ".volta/bin/npx")):
        if p and os.path.exists(p):
            return p
    nvm = sorted((Path.home() / ".nvm/versions/node").glob("*/bin/npx"))
    return str(nvm[-1]) if nvm else None


class NotReady(Exception):
    pass


def deploy(s, log=print):
    acc, token, name = s["Cloudflare账户ID"].strip(), s["Cloudflare_API令牌"].strip(), s["网站项目名"].strip()
    if not (acc and token and name):
        raise NotReady("「设置.json → 网站分享」里的 Cloudflare 账户ID、API令牌、网站项目名还没填全。")
    npx = _find_npx()
    if not npx:
        raise NotReady("这台电脑还没有安装 Node.js（上传网站要用）。请到 https://nodejs.org 下载 LTS 版安装，装好后重试。")
    env = dict(os.environ, CLOUDFLARE_API_TOKEN=token, CLOUDFLARE_ACCOUNT_ID=acc,
               PATH=os.path.dirname(npx) + os.pathsep + os.environ.get("PATH", ""), WRANGLER_SEND_METRICS="false")
    base = [npx, "--yes", "wrangler@4"]
    marker = HERE / ".pages_project"
    if not marker.exists() or marker.read_text().strip() != name:
        r = subprocess.run(base + ["pages", "project", "create", name, "--production-branch", "main"],
                           env=env, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 or "already exists" in (r.stdout + r.stderr).lower():
            marker.write_text(name)
        else:
            raise RuntimeError("创建网站项目失败：\n" + (r.stderr or r.stdout)[-1500:])
    r = subprocess.run(base + ["pages", "deploy", str(SITE), "--project-name", name, "--branch", "main",
                               "--commit-dirty=true"], env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("上传网站失败：\n" + (r.stderr or r.stdout)[-1500:])
    return site_url(s)


# ================================================================ 群推送
def _ssl_ctx():
    """python.org 版 Python 默认不带根证书，用 certifi 的证书库（yfinance 依赖里已有）。"""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def push(s, title, text, link=None, hook_key="Webhook地址"):
    p = s["群推送"]
    kind = (p.get("类型") or "").strip().lower()
    hook = (p.get(hook_key) or "").strip() or (p.get("Webhook地址") or "").strip()
    if not kind or not hook:
        return False
    content = f"{title}\n{text}".strip()
    if link:
        content += f"\n查看：{link}"
    if kind in ("企业微信", "wecom", "weixin"):
        b = content.encode("utf-8")[:2000].decode("utf-8", "ignore")
        body = {"msgtype": "text", "text": {"content": b}}
    elif kind in ("飞书", "lark", "feishu"):
        body = {"msg_type": "text", "content": {"text": content[:4000]}}
    elif kind in ("钉钉", "dingtalk"):
        body = {"msgtype": "text", "text": {"content": content[:4000]}}
        secret = (p.get("钉钉加签密钥") or "").strip()
        if secret:
            ts = str(round(time.time() * 1000))
            sign = base64.b64encode(hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest())
            hook += ("&" if "?" in hook else "?") + f"timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
    elif kind == "slack":
        body = {"text": content[:3500]}
    elif kind == "discord":
        body = {"content": content[:1900]}
    else:
        raise NotReady(f"群推送类型「{p.get('类型')}」不认识，请填 企业微信 / 飞书 / 钉钉 / Slack / Discord。")
    req = urllib.request.Request(hook, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "MacroFengxiangbiao/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
        reply = resp.read().decode("utf-8", "ignore")
    # 企业微信/钉钉/飞书出错时也返回 200，要看返回内容
    try:
        j = json.loads(reply) if reply.strip().startswith("{") else {}
    except Exception:
        j = {}
    code = j.get("errcode", j.get("code", j.get("StatusCode", 0)))
    if code not in (0, None):
        raise RuntimeError(f"群推送被拒绝：{reply[:300]}")
    return True


def _err(msg):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with (REPORT_DIR / "错误日志.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] 网站分享\n{msg}\n")


# ================================================================ 对外入口
def after_report(cfg, docx_path, A=None, top_lines=None, log=print):
    """报告生成后调用：转网页 → 上传 → 推送摘要。任何一步出错只打印提示，不影响报告本身。"""
    s = settings(cfg)
    if not s.get("启用"):
        return
    log("")
    log("  网站分享：")
    link = None
    try:
        key = build_site(docx_path, s)
        log("  · 正在上传网页（一般 10–40 秒）……")
        deploy(s, log)
        link = f"{site_url(s)}/r/{key}.html"
        log(f"  ✓ 已发布：{site_url(s)}")
    except NotReady as e:
        log(f"  ⚠ {e}")
        return
    except Exception as e:
        _err(str(e))
        log("  ✗ 网页上传失败（详情见 报告/错误日志.txt）；报告本身已正常生成。")
    if s["群推送"].get("推送报告摘要") and A and not os.environ.get("SHARE_NO_PUSH"):
        lines = [f"市场环境：{A['verdict']}（综合分 {A['score']:+d}）"]
        for lvl, txt in A.get("alerts", [])[:4]:
            lines.append(f"[{lvl}] {txt}")
        lines += list(top_lines or [])
        try:
            if push(s, f"玉衡5.0｜新报告 {dt.datetime.now():%m-%d %H:%M}", "\n".join(lines), link or site_url(s),
                    hook_key="报告Webhook地址"):
                log("  ✓ 已推送到工作群")
        except Exception as e:
            _err(str(e))
            log(f"  ✗ 群推送失败：{str(e)[:120]}")


def after_alert(cfg, title, body):
    """盘中出现新提醒时调用：推送到群 + 后台更新网站的「盘中提醒」页。不阻塞监测。"""
    s = settings(cfg)
    if not s.get("启用"):
        return

    def work():
        if s["群推送"].get("推送盘中提醒"):
            try:
                push(s, f"玉衡5.0｜{title}", body + "\n\n按规则筛出的候选，不是买卖建议。", f"{site_url(s)}/alerts.html")
            except Exception as e:
                _err(str(e))
        try:
            if (SITE / "index.html").exists():
                build_alerts_page()
                deploy(s)
        except Exception as e:
            _err(str(e))
    threading.Thread(target=work, daemon=True).start()


def test():
    sys.path.insert(0, str(HERE))
    import macro_report as mr
    cfg = mr.load_config()
    s = settings(cfg)
    print("=" * 50)
    print("  测试网站分享和群推送")
    print("=" * 50)
    if not s.get("启用"):
        print("「设置.json → 网站分享 → 启用」还是 false。填好 Cloudflare 信息后改成 true 再试。")
        return
    reports = sorted(list(REPORT_DIR.glob("玉衡5.0_*.docx")) + list(REPORT_DIR.glob("玉衡5.0_*.docx")), key=lambda p: p.stat().st_mtime)
    if not reports:
        print("「报告」文件夹里还没有报告，请先双击「玉衡5.0.command」生成一份。")
        return
    print(f"用最新一份报告测试：{reports[-1].name}")
    try:
        key = build_site(reports[-1], s)
        print("· 网页已生成，正在上传……")
        deploy(s)
        print(f"✓ 网站已更新：{site_url(s)}")
    except NotReady as e:
        print(f"⚠ {e}")
    except Exception as e:
        print(f"✗ {e}")
    try:
        if push(s, "玉衡5.0｜测试消息", "如果你在群里看到这条，说明推送设置正确。", site_url(s)):
            print("✓ 测试消息已发到工作群")
        else:
            print("· 没有设置群推送（类型或 Webhook 地址为空），跳过。")
    except Exception as e:
        print(f"✗ 群推送失败：{e}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
