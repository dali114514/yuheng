# -*- coding: utf-8 -*-
"""
Discord 机器人：在指定频道里发文字命令，这台 Mac 自动执行；所有定时任务（盘前报告、夜盘、Grok、收盘复盘、周报……）也在这里

  命令和用法见根目录「Discord机器人使用说明.md」，在频道里发「帮助」也能看到。
  由控制台自动运行和守护；重启：控制台「Discord 机器人」→ 重启。设置：设置.json →「Discord机器人」
"""
import asyncio
import datetime as dt
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# python.org 版 Python 默认不带根证书：用 certifi 的证书库（子进程也会继承）
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))
except Exception:
    pass

HERE = Path(__file__).resolve().parent
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
BASE = HERE.parent
REPORT_DIR = BASE / "报告"
PY = sys.executable
sys.path.insert(0, str(HERE))
import macro_report as mr          # noqa: E402

BOT_LOG = REPORT_DIR / "机器人日志.txt"

CMDS = {
    "judge": ("盘感",),
    "plan": ("计划",),
    "reason": ("原因",),
    "review": ("复盘", "收盘复盘"),
    "backfill": ("回填历史",),
    "weekly": ("周报",),
    "news": ("归档新闻", "新闻归档"),
    "pack": ("简报", "今日资料", "盘前资料", "今日简报", "盘前报告"),
    "reddit_on": ("开启散户监测", "开始散户监测", "打开散户监测"),
    "reddit_off": ("停止散户监测", "关闭散户监测"),
    "reddit": ("散户",),
    "grok": ("grok", "雷达", "盘前消息", "盘前雷达"),
    "night": ("夜盘复盘", "夜盘"),
    "brief": ("快讯", "早报", "新闻", "盘前快讯"),
    "report": ("给我报告", "生成报告", "出报告", "报告"),
    "status": ("状态",),
    "help": ("帮助", "help", "命令"),
}
HELP = None                                  # 帮助文字见 help_text()


def log(text):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with BOT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    print(text)



# ---------------------------------------------------------------- 定时任务「今天做过没有」：记在文件里，重启机器人也不会再跑一遍
JOBS_FILE = HERE / "data" / ".bot_jobs.json"


def job_done(name, day):
    try:
        import json
        return json.loads(JOBS_FILE.read_text(encoding="utf-8")).get(name) == str(day)
    except Exception:
        return False


def job_mark(name, day):
    import json
    try:
        d = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d[name] = str(day)
    try:
        JOBS_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"记录定时任务状态失败：{e!r}")


def _ver():
    import version
    return version.get()


def settings():
    cfg = mr.load_config()
    s = dict(cfg.get("Discord机器人") or {})
    return cfg, s


def match(text):
    t = re.sub(r"<@!?\d+>", "", text).strip().lower()
    for cmd, words in CMDS.items():
        if any(t == w or t.startswith(w) for w in words):
            return cmd
    return None


def opend_running():
    import osutil
    pat = re.compile(r"moomoo_OpenD|OpenD\.app|FutuOpenD|OpenD(\.exe)?$", re.I)
    return any(pat.search(" ".join(cl)) for _, cl in osutil.list_procs())


def latest_report():
    files = sorted(set(REPORT_DIR.glob("赚钱小助手_*.docx")) | set(REPORT_DIR.glob("玉衡5.0_*.docx")), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


# 每个命令属于哪个频道（设置.json → Discord机器人 → 频道分工）
CMD_CHANNEL = {"reddit_on": "散户", "reddit_off": "散户", "reddit": "散户", "grok": "快讯", "night": "快讯", "brief": "快讯", "report": "报告"}
CMD_WORD = {"reddit_on": "开启散户监测", "reddit_off": "停止散户监测", "reddit": "散户", "grok": "雷达", "night": "夜盘", "brief": "快讯", "report": "给我报告"}


def route(s):
    """{"快讯": [id…], "报告": […], "监测": […]}，都转成字符串。"""
    r = s.get("频道分工") or {}
    return {k: [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
            for k, v in r.items() if k != "说明"}


def channel_for(s, kind):
    ids = route(s).get(kind) or []
    return ids[0] if ids else None


def wrong_channel(msg, s, cmd):
    """命令发错频道时返回该去的频道 ID，否则返回 None。"""
    kind = CMD_CHANNEL.get(cmd)
    ids = route(s).get(kind) or []
    if kind and ids and str(msg.channel.id) not in ids:
        return ids[0]
    return None


def help_text(s):
    r = route(s)
    ch = lambda k: f"<#{r[k][0]}>" if r.get(k) else "任意频道"
    return (f"**玉衡5.0 机器人 · v{_ver()}** 常用命令（直接发文字）：\n"
            "· `简报`：一键生成今天的盘前报告（新闻、夜盘、Grok、Reddit、全部自选股、Kill Zone 数据），精简版回到这里（几分钟）\n"
            "· `盘感 NVDA`：这只股票现在的盘感判断——历史相似情形的概率 + 追 / 等 / 观察 / 不做（盘中用，几秒）\n"
            "· `复盘`：收盘复盘　· `周报`：本周总结　· `状态`：看连接情况\n"
            "· `计划 …` / `原因 …`：快速记一句计划 / 给计划外交易补原因\n"
            f"· {ch('报告')} `给我报告`：Word 报告　{ch('快讯')} `快讯` / `雷达` / `夜盘`　{ch('散户')} `散户` / `散户 TSLA`\n"
            "完整说明见电脑上的「Discord机器人使用说明.md」。程序只读行情，不下单；结论仅供参考，不构成投资建议。")


def allowed(msg, s):
    chans = [str(c).strip() for c in (s.get("允许的频道ID") or []) if str(c).strip()]
    chans += [c for ids in route(s).values() for c in ids]
    if chans and str(msg.channel.id) not in chans:
        return False
    roles = [str(r).strip() for r in (s.get("允许的身份组") or []) if str(r).strip()]
    if roles:
        names = {r.name for r in getattr(msg.author, "roles", [])} | {str(r.id) for r in getattr(msg.author, "roles", [])}
        if not names & set(roles):
            return False
    return True


# ================================================================ 命令
REPORT_LOCK = asyncio.Lock()


async def do_report(msg, discord):
    if REPORT_LOCK.locked():
        await msg.reply("已经在生成报告了，稍等一下，好了会发在这里。")
        return
    async with REPORT_LOCK:
        await msg.reply("收到，正在生成报告（一般 30 秒–2 分钟）……")
        before = latest_report()
        env = dict(os.environ, NO_OPEN_REPORT="1", SHARE_NO_PUSH="1", PYTHONWARNINGS="ignore")
        proc = await asyncio.create_subprocess_exec(PY, str(HERE / "macro_report.py"), cwd=str(BASE), env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            await msg.reply("❌ 生成报告超时（15 分钟），请稍后再试。")
            log("报告超时")
            return
        text = out.decode("utf-8", "ignore")
        after = latest_report()
        if proc.returncode != 0 or after is None or after == before:
            tail = "\n".join(text.strip().splitlines()[-6:])[-800:]
            await msg.reply(f"❌ 报告生成失败。\n```\n{tail}\n```")
            log("报告失败\n" + text[-3000:])
            return
        lines = [l.strip() for l in text.splitlines()
                 if l.strip().startswith(("结论：", "[高]", "[中]")) or "交易机会（" in l]
        cfg, _ = settings()
        sh = cfg.get("网站分享") or {}
        link = ""
        if sh.get("启用") and "✓ 已发布" in text:
            key = re.search(r"(\d{4}-\d{2}-\d{2}_\d{4})", after.stem)
            link = f"\n网页版：https://{sh.get('网站项目名')}.pages.dev/r/{key.group(1)}.html" if key else ""
        body = f"**玉衡5.0 v{_ver()}｜{dt.datetime.now():%m-%d %H:%M} 报告**\n" + "\n".join(lines[:8]) + link
        body += "\n_仅供参考，不构成投资建议。_"
        await msg.reply(body[:1900], file=discord.File(str(after)))
        log(f"已发送报告 {after.name} 给 {msg.author}")


BRIEF_LOCK = asyncio.Lock()


async def do_brief(msg):
    if BRIEF_LOCK.locked():
        await msg.reply("快讯正在整理中，稍等一下。")
        return
    async with BRIEF_LOCK:
        await msg.reply("收到，正在整理快讯和交易机会（一般 1–2 分钟）……")
        import briefing
        cfg, _ = settings()
        msgs = [m for m in await asyncio.to_thread(briefing.build, cfg) if m and m.strip()]
        for i, m in enumerate(msgs):
            if i == 0:
                await msg.reply(m)
            else:
                await msg.channel.send(m)
        log(f"已发送快讯给 {msg.author}")


REDDIT_LOCK = asyncio.Lock()


async def do_reddit(msg, cmd):
    import reddit_watch as RW
    cfg, _ = settings()
    if cmd == "reddit_on":
        RW.set_on(True)
        s = RW.settings(cfg)
        await msg.reply(f"✅ 散户监测已开启：每个美股交易日盘前 {'–'.join(s['运行时段_美东'])}（美东）每 {s['扫描间隔_分钟']} 分钟扫一次，"
                        "自选股热度飙升或出现新黑马会发到这里。会一直保持开启，直到你发「停止散户监测」。")
        log(f"{msg.author} 开启了散户监测")
        return
    if cmd == "reddit_off":
        RW.set_on(False)
        await msg.reply("⏹ 散户监测已停止。")
        log(f"{msg.author} 停止了散户监测")
        return
    arg = re.sub(r"<@!?\d+>", "", msg.content or "").strip()
    arg = re.sub(r"^散户", "", arg).strip().lstrip("$")
    arg = arg.upper() if arg.isascii() else arg
    if REDDIT_LOCK.locked():
        await msg.reply("正在查询 Reddit，稍等一下。")
        return
    async with REDDIT_LOCK:
        if arg:
            await msg.reply(f"收到，正在读 Reddit 上关于 {arg} 的讨论（约 30 秒–1 分钟）……")
        msgs = await asyncio.to_thread(RW.snapshot_text, cfg, arg or None, log)
        for i, m in enumerate(msgs):
            if m.strip():
                await (msg.reply(m) if i == 0 and not arg else msg.channel.send(m))


async def auto_reddit_loop(client):
    import reddit_watch as RW
    last = 0.0
    while True:
        try:
            cfg, s = settings()
            rs = RW.settings(cfg)
            if rs.get("启用") and RW.is_on() and RW.in_window(rs) and time.time() - last >= float(rs["扫描间隔_分钟"]) * 60 - 5:
                last = time.time()
                async with REDDIT_LOCK:
                    alerts = await asyncio.to_thread(RW.scan, cfg, log)
                if alerts and rs.get("发到Discord", True):
                    target = channel_for(s, "散户") or channel_for(s, "监测")
                    ch = client.get_channel(int(target)) if target else None
                    for a in alerts:
                        if ch:
                            await ch.send(RW.fmt_alert(a))
                    log(f"散户监测：{len(alerts)} 条提醒")
        except Exception as e:
            log(f"散户监测出错：{e!r}")
        await asyncio.sleep(20)


GROK_LOCK = asyncio.Lock()
CLIENT = None


async def _grok_publish(fallback_send=None):
    """跑一次 Grok 盘前雷达：简讯 → #快讯，报告摘要 + Word → #报告。
    设置.json → Grok盘前 → 发到Discord = false 时，自动运行只抓取归档（内容合进今日简报），不单独发。"""
    import discord
    import grokscan
    cfg, s = settings()
    res = await asyncio.to_thread(grokscan.run, cfg, log)
    if fallback_send is None and not (cfg.get("Grok盘前") or {}).get("发到Discord", True):
        log(f"Grok 盘前雷达已运行（不单独发，合进今日简报）：{len(res.get('posts') or [])} 条帖子")
        try:
            import news
            await asyncio.to_thread(news.collect, cfg, 24, log, ("grok",))
        except Exception as e:
            log(f"Grok 结果归档出错：{e!r}")
        return
    if not res.get("ok"):
        if fallback_send:
            await fallback_send(res.get("msg") or "Grok 雷达没有结果。")
        return
    ch_brief = CLIENT.get_channel(int(channel_for(s, "快讯"))) if channel_for(s, "快讯") else None
    ch_rep = CLIENT.get_channel(int(channel_for(s, "报告"))) if channel_for(s, "报告") else None
    for m in grokscan.brief(res):
        if m.strip():
            await (ch_brief.send(m) if ch_brief else fallback_send(m))
    path = None
    try:
        path = await asyncio.to_thread(grokscan.word_report, res, BASE / "报告")
    except Exception as e:
        log(f"Grok 报告 Word 生成失败：{e!r}")
    msgs = [m for m in grokscan.report_summary(res, path) if m.strip()]
    target = ch_rep or ch_brief
    for i, m in enumerate(msgs):
        last = i == len(msgs) - 1
        if target:
            await target.send(m, file=discord.File(str(path)) if (last and path) else None)
        elif fallback_send:
            await fallback_send(m)
    log(f"Grok 盘前雷达已发送：{len(res['posts'])} 条帖子，约 ${res.get('cost', 0):.2f}")


async def do_grok(msg):
    if GROK_LOCK.locked():
        await msg.reply("Grok 雷达正在扫描，稍等一下。")
        return
    async with GROK_LOCK:
        await msg.reply("收到，Grok 正在扫描全部关注账号并写报告（一般 2–5 分钟）。简讯发 #快讯，报告发 #报告。")
        await _grok_publish(lambda m: msg.channel.send(m))


async def auto_grok_loop(client):
    """每个美股交易日美东「报告时间」跑一次。"""
    sent = set()
    while True:
        try:
            cfg, s = settings()
            g = cfg.get("Grok盘前") or {}
            if g.get("启用") and g.get("API密钥"):
                now = dt.datetime.now(ET)
                hh, mm = [int(x) for x in str(g.get("报告时间_美东") or "09:00").split(":")]
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                try:
                    import grokscan
                    gday = (grokscan.load_cache().get("latest") or {}).get("day")
                except Exception:
                    gday = None
                if (now.weekday() < 5 and 0 <= (now - due).total_seconds() < 900 and now.date() not in sent
                        and not job_done("grok", now.date()) and gday != str(now.date())):
                    sent.add(now.date())
                    job_mark("grok", now.date())
                    async with GROK_LOCK:
                        await _grok_publish()
        except Exception as e:
            log(f"自动 Grok 雷达出错：{e!r}")
        await asyncio.sleep(30)


NIGHT_LOCK = asyncio.Lock()


async def do_night(msg):
    if NIGHT_LOCK.locked():
        await msg.reply("夜盘复盘正在整理中，稍等一下。")
        return
    async with NIGHT_LOCK:
        await msg.reply("收到，正在读取夜盘 1 分钟K（一般 30 秒–1 分钟）……")
        import nightly
        cfg, _ = settings()
        msgs = [m for m in await asyncio.to_thread(nightly.build, cfg, log) if m and m.strip()]
        _save_nightly(msgs)
        for i, m in enumerate(msgs):
            await (msg.reply(m) if i == 0 else msg.channel.send(m))
        log(f"已发送夜盘复盘给 {msg.author}")


async def auto_night_loop(client):
    """夜盘结束后（默认美东 04:10，周一到周五）自动把夜盘复盘发到 #快讯。"""
    from zoneinfo import ZoneInfo
    sent_on = None
    while True:
        try:
            _, s = settings()
            a = s.get("自动夜盘复盘") or {}
            if a.get("启用"):
                tz = ZoneInfo(a.get("时区") or "America/New_York")
                hh, mm = [int(x) for x in str(a.get("时间") or "04:10").split(":")]
                now = dt.datetime.now(tz)
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                et_day = now.astimezone(ZoneInfo("America/New_York")).weekday()
                if (now >= due and (now - due).total_seconds() < 600 and sent_on != now.date() and et_day < 5
                        and not job_done("night", now.date())):
                    sent_on = now.date()
                    job_mark("night", now.date())
                    target = channel_for(s, "快讯")
                    ch = client.get_channel(int(target)) if target else None
                    if ch is None:
                        log("自动夜盘复盘：找不到 #快讯 频道")
                    else:
                        async with NIGHT_LOCK:
                            import nightly
                            cfg, _ = settings()
                            msgs = [m for m in await asyncio.to_thread(nightly.build, cfg, log) if m and m.strip()]
                            _save_nightly(msgs)
                            if a.get("发到Discord", True):
                                for m in msgs:
                                    await ch.send(m)
                        log(f"自动夜盘复盘已完成（{'已发到 #' + ch.name if a.get('发到Discord', True) else '只存进今日文件夹，内容合进今日简报'}）")
        except Exception as e:
            log(f"自动夜盘复盘出错：{e!r}")
        await asyncio.sleep(30)


def find_procs(script):
    """这台电脑上所有运行「程序/<script>」的进程（不含自己和自己的父进程）。macOS / Windows / Linux 都能用。"""
    import osutil
    return osutil.find_script(script, exclude={os.getpid(), os.getppid()})


def kill_all(script, wait=3.0):
    """先礼后兵：先请它退出，等几秒还在就强制结束。返回清理掉的进程数。"""
    import osutil
    return osutil.terminate(find_procs(script), wait)


async def do_status(msg):
    r = latest_report()
    rep = "还没有"
    if r:
        rep = f"{r.stem.replace('赚钱小助手_', '').replace('玉衡5.0_', '')}（{dt.datetime.fromtimestamp(r.stat().st_mtime):%m-%d %H:%M} 生成）"
    import today
    day = today.session_day()
    p = today.load_plan(day)
    pack = (today.ROOT / str(day) / "盘前资料.md").exists()
    await msg.reply(f"· 行情连接（OpenD）：{'🟢 在线' if opend_running() else '🔴 未运行'}\n"
                    f"· {day} 盘前报告：{'✅ 已生成' if pack else '还没生成（发「简报」）'}\n"
                    f"· 纪律设置：{(p.get('状态') + '，' + str(p.get('偏向'))) if p and p.get('观察名单') else '还没定'}\n"
                    f"· 最近一份 Word 报告：{rep}")


async def do_judge(msg):
    import engine
    arg = re.sub(r"<@!?\d+>", "", msg.content or "").strip()
    arg = re.sub(r"^盘感", "", arg).strip().lstrip("$").upper()
    if not arg or len(arg) > 10 or not all(c.isalnum() or c == "." for c in arg):
        await msg.reply("格式：`盘感 NVDA`（盘中用；股票要在今天的观察名单里或者是 QQQ / SPY，才有实时热数据）")
        return
    text, res = await asyncio.to_thread(engine.judge, arg)
    if res is None:
        await msg.reply(text)
        return
    L, S = res["long"], res["short"]
    short = (f"**盘感 · {arg} · {res['t']}（美东）　结论：{res['verdict']}**\n"
             f"现价 {res['price']:.2f}；相似情形 {res['N']} 个：做多平均 {L['mean']:+.2f}R（收缩后 {L['shrunk']:+.2f}），"
             f"做空平均 {S['mean']:+.2f}R（收缩后 {S['shrunk']:+.2f}）\n"
             + "\n".join(f"· {x}" for x in text.split("## 结论：", 1)[1].split("## 现在是什么情况")[0].splitlines()[1:] if x.strip().startswith("- ")).replace("· - ", "· ")
             + f"\n-# 完整分析：数据/热数据/盘感_{arg}_{res['t'].replace(':', '')}.md　·　盘感引擎走前验证显示预测力很弱，只当背景参考")
    for part in _chunks(short):
        await msg.channel.send(part)


# ================================================================ 每天定时发快讯
REVIEW_LOCK = asyncio.Lock()


def _save_nightly(msgs):
    try:                                          # 也存进「今日」文件夹给 Claude 读
        import today
        today.save_text("夜盘.md", "# 夜盘复盘\n\n" + "\n\n".join(msgs))
    except Exception as e:
        log(f"保存夜盘复盘到今日文件夹出错：{e!r}")


async def do_weekly(msg, channel=None):
    import weekly
    cfg, _ = settings()
    text, _ = await asyncio.to_thread(weekly.build, None, None, True, cfg)
    ch = channel or msg.channel
    for part in _chunks(text):
        await ch.send(part)


async def do_news(msg):
    import news
    cfg, _ = settings()
    await msg.reply("开始抓新闻并归档（约 1 分钟）……")
    text = await asyncio.to_thread(news.collect, cfg, 24, log)
    await msg.reply("✓ " + text + "\n文件在「数据/冷数据/信息面」。")


PACK_LOCK = asyncio.Lock()


async def do_pack(msg):
    import today
    if PACK_LOCK.locked():
        await msg.reply("盘前报告正在生成，稍等一下。")
        return
    async with PACK_LOCK:
        await msg.reply("开始生成盘前报告：抓新闻 → 补夜盘和 Grok（今天没跑过才跑）→ 读 Reddit → 写报告，几分钟……")
        cfg, _ = settings()
        text, short = await asyncio.to_thread(today.make_full, cfg, log, True, True)
        for i, m in enumerate(short):
            await msg.channel.send(m)
        await msg.channel.send("-# " + text.replace("\n", "　"))


async def auto_pack_loop(client):
    """今日简报：每个交易日美东「时间」（默认 8:45，Grok 雷达之后）整理盘前资料，精简版发到 #快讯。
    设置.json → Discord机器人 → 今日简报：{启用, 时间}。Grok 还没跑完就再等最多 10 分钟。"""
    done = None
    while True:
        try:
            cfg, s = settings()
            a = s.get("今日简报") or {"启用": True, "时间": "08:45"}
            now = dt.datetime.now(ET)
            hh, mm_ = [int(x) for x in str(a.get("时间") or "08:45").split(":")]
            due = now.replace(hour=hh, minute=mm_, second=0, microsecond=0)
            if (a.get("启用", True) and now.weekday() < 5 and now >= due and now.hour < 16 and done != now.date()
                    and not job_done("pack", now.date())):
                g = (cfg.get("Grok盘前") or {})
                try:
                    import grokscan
                    gday = (grokscan.load_cache().get("latest") or {}).get("day")
                except Exception:
                    gday = None
                if g.get("启用") and gday != str(now.date()) and (now - due).total_seconds() < 600:
                    await asyncio.sleep(30)
                    continue
                done = now.date()
                job_mark("pack", now.date())
                import today
                async with PACK_LOCK:
                    text, short = await asyncio.to_thread(today.make_full, cfg, log, False, True)
                target = channel_for(s, "快讯")
                ch = client.get_channel(int(target)) if target else None
                if ch:
                    for m in short:
                        await ch.send(m)
                log("今日简报已发送")
        except Exception as e:
            log(f"今日简报出错：{e!r}")
        await asyncio.sleep(30)


async def auto_news_loop(client):
    """信息面归档：美东工作日 4:00–20:00 每 20 分钟一轮，另外每天 22:00 补一轮（抓过去 24 小时，自动去重）。"""
    last = 0.0
    done_night = None
    while True:
        try:
            now = dt.datetime.now(ET)
            m = now.hour * 60 + now.minute
            due = now.weekday() < 5 and 240 <= m <= 1200 and time.time() - last >= 20 * 60
            night = m >= 1320 and done_night != now.date()
            if due or night:
                import news
                cfg, _ = settings()
                last = time.time()
                if night:
                    done_night = now.date()
                await asyncio.to_thread(news.collect, cfg, 24, log)
        except Exception as e:
            log(f"信息面归档出错：{e!r}")
        await asyncio.sleep(60)


async def auto_newsalert_loop(client):
    """重大新闻马上推送（newsalert.py）：美东工作日 4:00–20:00，每 90 秒看一次华尔街见闻快讯，重大的发到 #快讯，并马上归档。"""
    import newsalert
    while True:
        wait = 90
        try:
            cfg, s = settings()
            na = newsalert.settings(cfg)
            wait = max(30, int(na.get("间隔_秒") or 90))
            if na.get("启用", True) and newsalert.in_window(na):
                msgs = await asyncio.to_thread(newsalert.scan, cfg, log)
                if msgs:
                    target = channel_for(s, na.get("频道") or "快讯") or channel_for(s, "快讯")
                    ch = client.get_channel(int(target)) if target else None
                    for m in msgs:
                        if ch:
                            await ch.send(m[:1990])
                    log(f"重大新闻推送：{len(msgs)} 条" + ("" if ch else "（找不到 #快讯 频道，没发出去）"))
                    try:                              # 马上归档，不用等 20 分钟那一轮
                        import news
                        await asyncio.to_thread(news.collect, cfg, 2, None, ("wscn",))
                    except Exception as e:
                        log(f"重大新闻归档出错：{e!r}")
        except Exception as e:
            log(f"重大新闻推送出错：{e!r}")
        await asyncio.sleep(wait)


async def auto_weekly_loop(client):
    """每周五美东 17:00（收盘复盘之后）自动发周报到 #报告。"""
    sent_on = None
    while True:
        try:
            _, s = settings()
            a = s.get("自动周报") or {"启用": True, "时间": "17:00"}
            if a.get("启用", True):
                hh, mm_ = [int(x) for x in str(a.get("时间") or "17:00").split(":")]
                now = dt.datetime.now(ET)
                due = now.replace(hour=hh, minute=mm_, second=0, microsecond=0)
                if (now.weekday() == 4 and now >= due and (now - due).total_seconds() < 3600 and sent_on != now.date()
                        and not job_done("weekly", now.date())):
                    sent_on = now.date()
                    job_mark("weekly", now.date())
                    target = channel_for(s, "报告")
                    ch = client.get_channel(int(target)) if target else None
                    if ch is None:
                        log("自动周报：找不到 #报告 频道")
                    else:
                        await do_weekly(None, channel=ch)
                        log(f"自动周报已发送到 #{ch.name}")
        except Exception as e:
            log(f"自动周报出错：{e!r}")
        await asyncio.sleep(60)


def _chunks(text, n=1900):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def _plan_day():
    """计划写给哪一天：美东 16:00 前 = 今天，之后 / 周末 = 下一个交易日。"""
    n = dt.datetime.now(ET)
    d = n.date() + dt.timedelta(days=1 if n.hour >= 16 else 0)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


async def do_plan(msg):
    import journal
    text = re.sub(r"<@!?\d+>", "", msg.content or "").strip()
    text = re.sub(r"^计划[:：\s]*", "", text)
    day = _plan_day()
    if not text:
        p = await asyncio.to_thread(journal.get_plan, day)
        await msg.reply(f"{day} 的计划：{p['text']}" if p else
                        f"{day} 还没写计划。格式：`计划 偏多，最多 3 笔，QQQ 跌破 500 认错`")
        return
    bias, mx = await asyncio.to_thread(journal.save_plan, day, text)
    await msg.reply(f"✓ 已记下 {day}（美东）的计划：方向 {bias or '未识别'}，最多 {mx if mx else '未写'} 笔。"
                    + ("" if bias else "\n（方向请写「偏多 / 偏空 / 中性」之一，复盘时才能对照）"))


async def do_reason(msg):
    import journal
    text = re.sub(r"<@!?\d+>", "", msg.content or "").strip()
    text = re.sub(r"^原因[:：\s]*", "", text)
    await msg.reply(await asyncio.to_thread(journal.set_reason, text))


async def do_review(msg, channel=None):
    import review
    if REVIEW_LOCK.locked():
        if msg:
            await msg.reply("复盘正在进行中，稍等一下。")
        return
    async with REVIEW_LOCK:
        if msg:
            await msg.reply("开始收盘复盘：K 线入库 → 拉成交 → 匹配信号 → 评估结果，约 1–3 分钟……")
        cfg, _ = settings()
        text = await asyncio.to_thread(review.run, cfg, log)
        ch = channel or msg.channel
        for part in _chunks(text):
            await ch.send(part)
        return text


async def do_backfill(msg):
    import history
    n = dt.datetime.now(ET)
    slow = n.weekday() < 5 and 9.5 <= n.hour + n.minute / 60 < 16
    if REVIEW_LOCK.locked():
        await msg.reply("复盘或回填正在进行中，稍等一下。")
        return
    async with REVIEW_LOCK:
        await msg.reply("开始回填历史 K 线（日线 10 年、5 分钟最多 1 年、1 分钟最多 30 天），先补完所有股票的日线，再补 5 分钟和 1 分钟。"
                        + ("\n现在是交易时段：用慢速回填，给热数据留出接口额度，大概要 1–2 小时。" if slow
                           else "\n第一次大概要几十分钟。") + "完成后回复你。")
        cfg, _ = settings()
        text = await asyncio.to_thread(history.update, cfg, log, None, True, ("1d", "5m", "1m"), None, slow)
        await msg.reply("✓ " + text + "\n接着重建盘感引擎缓存……")
        try:
            import engine
            await msg.channel.send("-# " + await asyncio.to_thread(engine.build_cache, None))
        except Exception as e:
            log(f"盘感引擎缓存出错：{e}")
        try:
            import cold
            r = await asyncio.to_thread(cold.export, None, True, None)
            await msg.channel.send("-# " + r)
        except Exception as e:
            log(f"冷数据导出出错：{e}")
        try:
            import sync
            r = await asyncio.to_thread(sync.run, cfg, True)
            await msg.channel.send("-# " + r)
        except Exception as e:
            log(f"回填后同步出错：{e}")


async def auto_close_loop(client):
    """收盘后（默认美东 16:20，周一到周五）自动做收盘复盘，发到 #报告。
    失败（网络断、OpenD 断、K 线没入库、Discord 发不出去）就 5 分钟后再试，收盘后 3 小时内一直重试到成功。"""
    sent_on, next_try = None, 0.0
    while True:
        try:
            _, s = settings()
            a = s.get("自动收盘复盘") or {"启用": True, "时间": "16:20"}
            if a.get("启用", True):
                hh, mm_ = [int(x) for x in str(a.get("时间") or "16:20").split(":")]
                now = dt.datetime.now(ET)
                due = now.replace(hour=hh, minute=mm_, second=0, microsecond=0)
                if (now.weekday() < 5 and now >= due and (now - due).total_seconds() < 3 * 3600 and sent_on != now.date()
                        and not job_done("close", now.date()) and time.time() >= next_try):
                    sent_on = now.date()
                    job_mark("close", now.date())
                    ok = False
                    try:
                        target = channel_for(s, "报告")
                        ch = client.get_channel(int(target)) if target else None
                        if ch is None:
                            log("自动收盘复盘：找不到 #报告 频道")
                            ok = True                         # 设置问题，重试也没用
                        else:
                            text = await do_review(None, channel=ch) or ""
                            ok = not any(x in text for x in ("✗ K 线入库", "K 线没有入库", "1 分钟 0 根"))
                            log(f"自动收盘复盘已发送到 #{ch.name}" + ("" if ok else "（K 线没入库，5 分钟后重试）"))
                    except Exception as e:
                        log(f"自动收盘复盘出错：{e!r}（5 分钟后重试）")
                    if not ok:
                        sent_on, next_try = None, time.time() + 300
                        job_mark("close", "")
        except Exception as e:
            log(f"自动收盘复盘出错：{e!r}")
        await asyncio.sleep(30)


async def auto_brief_loop(client):
    from zoneinfo import ZoneInfo
    sent_on = None
    while True:
        try:
            _, s = settings()
            a = s.get("自动快讯") or {}
            if a.get("启用"):
                tz = ZoneInfo(a.get("时区") or "Australia/Sydney")
                hh, mm = [int(x) for x in str(a.get("时间") or "01:00").split(":")]
                now = dt.datetime.now(tz)
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                us_day = now.astimezone(ZoneInfo("America/New_York")).weekday() < 5
                if (now >= due and (now - due).total_seconds() < 600 and sent_on != now.date()
                        and (us_day or not a.get("只在美股交易日", True)) and not job_done("brief", now.date())):
                    sent_on = now.date()
                    job_mark("brief", now.date())
                    target = channel_for(s, "快讯") or next(
                        (str(c).strip() for c in (s.get("允许的频道ID") or []) if str(c).strip()), None)
                    ch = client.get_channel(int(target)) if target else None
                    if ch is None:
                        log("自动快讯：找不到要发送的频道（请在设置里填「允许的频道ID」）")
                    else:
                        import briefing
                        cfg, _ = settings()
                        msgs = [m for m in await asyncio.to_thread(briefing.build, cfg) if m and m.strip()]
                        for m in msgs:
                            await ch.send(m)
                        log(f"自动快讯已发送到 #{ch.name}")
        except Exception as e:
            log(f"自动快讯出错：{e!r}")
        await asyncio.sleep(30)


# ================================================================ 主程序
def main():
    # 同一时间只留一个机器人：再次双击「启动机器人」会先关掉旧的（否则每条命令会被执行两遍）
    old = find_procs("bot.py")
    if old:
        n = kill_all("bot.py")
        print(f"发现 {n} 个旧的机器人进程，已关闭，只保留这一个。")
        log(f"启动时关闭了 {n} 个旧的机器人进程")
    try:
        import discord
    except ImportError:
        print("缺少 discord.py，请双击「玉衡5.0.command」（它会自动安装）。")
        sys.exit(1)
    cfg, s = settings()
    token = (s.get("机器人令牌") or "").strip()
    if not s.get("启用") or not token:
        print("请先在「设置.json → Discord机器人」里填好「机器人令牌」，并把「启用」改成 true。")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    global CLIENT
    CLIENT = client

    @client.event
    async def on_ready():
        log(f"机器人已上线：{client.user}")
        if not getattr(client, "_auto_started", False):
            client._auto_started = True
            client.loop.create_task(auto_brief_loop(client))
            client.loop.create_task(auto_night_loop(client))
            client.loop.create_task(auto_grok_loop(client))
            client.loop.create_task(auto_reddit_loop(client))
            client.loop.create_task(auto_close_loop(client))
            client.loop.create_task(auto_weekly_loop(client))
            client.loop.create_task(auto_news_loop(client))
            client.loop.create_task(auto_newsalert_loop(client))
            client.loop.create_task(auto_pack_loop(client))
        for g in client.guilds:
            chans = ", ".join(f"#{c.name}({c.id})" for c in g.text_channels
                              if c.permissions_for(g.me).view_channel)
            log(f"  服务器「{g.name}」里机器人能看到的文字频道：{chans or '无'}")
        if not client.guilds:
            log("  ⚠ 机器人还没有加入任何服务器")

    @client.event
    async def on_message(msg):
        if msg.author.bot:
            return
        cmd = match(msg.content or "")
        _, s2 = settings()
        ok = allowed(msg, s2)
        log(f"[收到] 服务器={getattr(msg.guild, 'name', '私信')} 频道={getattr(msg.channel, 'name', '')}"
            f"({msg.channel.id}) 内容长度={len(msg.content or '')} 命令={cmd} 允许={ok}")
        if not cmd or not ok:
            return
        go = wrong_channel(msg, s2, cmd)
        if go:
            await msg.reply(f"「{CMD_WORD[cmd]}」请到 <#{go}> 频道发哦～")
            return
        try:
            if cmd == "judge":
                await do_judge(msg)
            elif cmd == "plan":
                await do_plan(msg)
            elif cmd == "reason":
                await do_reason(msg)
            elif cmd == "review":
                await do_review(msg)
            elif cmd == "backfill":
                await do_backfill(msg)
            elif cmd == "weekly":
                await do_weekly(msg)
            elif cmd == "news":
                await do_news(msg)
            elif cmd == "pack":
                await do_pack(msg)
            elif cmd in ("reddit", "reddit_on", "reddit_off"):
                await do_reddit(msg, cmd)
            elif cmd == "grok":
                await do_grok(msg)
            elif cmd == "night":
                await do_night(msg)
            elif cmd == "brief":
                await do_brief(msg)
            elif cmd == "report":
                await do_report(msg, discord)
            elif cmd == "status":
                await do_status(msg)
            else:
                await msg.reply(help_text(s2))
        except Exception as e:
            log(f"处理命令出错：{e!r}")
            await msg.reply("❌ 出错了，请让管理员看一下电脑上的「报告/机器人日志.txt」。")

    print("=" * 50)
    print(f"  玉衡5.0 v{_ver()} · Discord 机器人（运行中）")
    print("  关掉这个窗口或按 Control+C 即停止")
    print("=" * 50)
    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        print("✗ 机器人令牌不对。请到 Discord 开发者后台重新复制（Bot → Reset Token），填进设置.json。")
    except discord.PrivilegedIntentsRequired:
        print("✗ 还没打开「Message Content Intent」。请到 Discord 开发者后台 → Bot 页面打开它，保存后重试。")


if __name__ == "__main__":
    main()
