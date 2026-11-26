# -*- coding: utf-8 -*-
"""
玉衡5.0 · 控制台（双击「玉衡5.0.app」打开）

  一个程序管全部：
  · 守护常驻程序：Discord 机器人（全部定时任务）、纪律程序、热数据、早盘 C2 监测
  · 进程意外退出会自动重启（10 分钟内连续失败 5 次就停下，状态灯变红）
  · 「工具」区：一键生成盘前报告、盘感判断、收盘复盘、周报、各种报告；不常用的收在「维护」里
  · 运行期间电脑不睡眠；点控制台右上角「退出」才停止全部任务
只监听本机（127.0.0.1），别的电脑访问不了。
"""
import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import osutil  # noqa: E402
BASE = HERE.parent
sys.path.insert(0, str(HERE))
REPORT_DIR = BASE / "报告"
CONFIG = BASE / "设置.json"
PY = sys.executable
PORT = 8760
ET = None
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    pass


def et_now():
    return dt.datetime.now(ET) if ET else dt.datetime.utcnow() - dt.timedelta(hours=5)


def log(text):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with (REPORT_DIR / "控制台日志.txt").open("a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")


def load_cfg():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ================================================================ 进程管理
def _arch():
    """M 芯片的 Mac：子进程强制用原生模式运行（避免被当成 Intel 程序，导致 numpy 等库加载失败）。"""
    if sys.platform != "darwin":
        return []
    try:
        r = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=3)
        return ["arch", "-arm64"] if r.stdout.strip() == "1" else []
    except Exception:
        return []


ARCH = _arch()


def find_procs(script):
    """正在用 Python 运行「程序/<script>」的进程（包括套在外面的 caffeinate）。macOS / Windows / Linux 都能用（osutil）。"""
    return osutil.find_script(script)


def kill_all(script, wait=4.0):
    return osutil.terminate(find_procs(script), wait)


CHILD_ENV = dict(PYTHONWARNINGS="ignore", PYTHONUNBUFFERED="1", PYTHONUTF8="1")   # UTF-8 模式：Windows 上读写中文文件不出错


def spawn(script, outname):
    logf = (REPORT_DIR / outname).open("a", encoding="utf-8")
    pre = osutil.caffeinate_prefix() + ARCH
    return subprocess.Popen(pre + [PY, str(HERE / script)], cwd=str(BASE), stdout=logf, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=dict(os.environ, **CHILD_ENV), **osutil.detach_kwargs())


class Supervisor:
    """守护常驻程序：没在跑就启动，挂了就重启。"""

    def __init__(self):
        self.fails = {"bot.py": [], "c2watch.py": [], "guard.py": [], "hot.py": [], "dash.py": [], "stratwatch.py": []}
        self.given_up = {}
        self.started = {}
        self.stop = threading.Event()
        self.synced_at = 0.0

    def bot_wanted(self):
        b = load_cfg().get("Discord机器人") or {}
        return bool(b.get("启用")) and bool(str(b.get("机器人令牌") or "").strip())

    def strat_wanted(self):
        return ((load_cfg().get("策略提醒") or {}).get("模式", "提醒")) != "关"

    def c2_wanted(self):
        return ((load_cfg().get("盘中C2") or {}).get("模式", "影子")) != "关"

    def ensure(self, script, outname):
        if self.given_up.get(script):
            return
        if find_procs(script):
            return
        now = time.time()
        f = [t for t in self.fails[script] if now - t < 600]
        if script in self.started:           # 之前启动过、现在没了 = 意外退出
            f.append(now)
            log(f"{script} 退出了，重新启动（10 分钟内第 {len(f)} 次）")
        self.fails[script] = f
        if len(f) >= 5:
            self.given_up[script] = f"10 分钟内连续退出 {len(f)} 次，已停止自动重启。看「报告/{outname}」最后几行。"
            log(f"{script}：{self.given_up[script]}")
            return
        spawn(script, outname)
        self.started[script] = now

    def tick(self):
        try:                                     # 定时同步到主机 E 盘（后台跑，不卡守护）
            import sync
            ss = sync.settings(load_cfg())
            if ss.get("启用") and time.time() - self.synced_at > float(ss.get("同步间隔_分钟") or 15) * 60:
                self.synced_at = time.time()
                run_job("E盘同步", lambda: sync.run(load_cfg()))
        except Exception:
            log("同步出错：" + traceback.format_exc())
        if self.bot_wanted():
            self.ensure("bot.py", "机器人窗口输出.txt")
        self.ensure("guard.py", "纪律程序_窗口输出.txt")
        self.ensure("hot.py", "热数据_窗口输出.txt")
        self.ensure("dash.py", "盯盘界面_窗口输出.txt")      # 盯盘界面常驻，同事随时能打开
        if self.c2_wanted():
            self.ensure("c2watch.py", "C2监测_窗口输出.txt")
        elif find_procs("c2watch.py"):
            kill_all("c2watch.py")
            self.started.pop("c2watch.py", None)
        if self.strat_wanted():                          # 策略提醒：按选定策略弹入场 / 出场
            self.ensure("stratwatch.py", "策略提醒_窗口输出.txt")
        elif find_procs("stratwatch.py"):
            kill_all("stratwatch.py")
            self.started.pop("stratwatch.py", None)

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log("守护出错：" + traceback.format_exc())
            self.stop.wait(20)

    def reset(self, script):
        self.fails[script] = []
        self.given_up.pop(script, None)
        self.started.pop(script, None)


SUP = Supervisor()


def restart_bot():
    SUP.started.pop("bot.py", None)
    kill_all("bot.py")
    SUP.reset("bot.py")
    if SUP.bot_wanted():
        SUP.ensure("bot.py", "机器人窗口输出.txt")


# ================================================================ 状态

def c2_status():
    """早盘 C2 监测的状态（c2watch.py 写的文件）+ 历史回测摘要。"""
    d = {}
    try:
        d = json.loads((HERE / "data" / ".c2_status.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        d["backtest"] = json.loads((HERE / "data" / ".c2_backtest.json").read_text(encoding="utf-8")).get("text")
    except Exception:
        d["backtest"] = None
    codes = d.get("codes") or {}
    n_in = [c for c, v in codes.items() if v.get("phase") == "入场"]
    n_pb = [c for c, v in codes.items() if v.get("phase") == "回踩中"]
    if codes:
        d["brief"] = (f"入场 {len(n_in)} 只" + (f"（{'、'.join(n_in[:4])}）" if n_in else "")
                      + f"，回踩中 {len(n_pb)} 只，更新 {d.get('at', '')}")
    return d


def _port(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except Exception:
        return False


def _tail(path, n=400):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 60000))
            return f.read().decode("utf-8", "ignore").splitlines()[-n:]
    except Exception:
        return []


def session_name(n=None):
    n = n or et_now()
    t = n.hour + n.minute / 60
    if n.weekday() >= 5:
        return "周末休市"
    if 4 <= t < 9.5:
        return "盘前"
    if 9.5 <= t < 16:
        return "盘中"
    if 16 <= t < 20:
        return "盘后"
    return "夜盘"


def status():
    cfg = load_cfg()
    m = cfg.get("moomoo") or {}
    out = []
    opend = _port(m.get("OpenD地址", "127.0.0.1"), m.get("OpenD端口", 11111))
    out.append(dict(key="opend", name="moomoo OpenD", ok=opend,
                    text="已连接" if opend else "没有运行：请打开 OpenD 并登录（行情、期权、成交都靠它）"))
    bot = find_procs("bot.py")
    last = next((l for l in reversed(_tail(REPORT_DIR / "机器人日志.txt", 50)) if l.startswith("[")), "")
    if SUP.given_up.get("bot.py"):
        out.append(dict(key="bot", name="Discord 机器人", ok=False, text=SUP.given_up["bot.py"]))
    elif not SUP.bot_wanted():
        out.append(dict(key="bot", name="Discord 机器人", ok=False, warn=True, text="设置里没启用或没填令牌"))
    else:
        out.append(dict(key="bot", name="Discord 机器人", ok=bool(bot),
                        text=("运行中" if bot else "没有运行，正在启动……") + (f"　最近：{last[1:20]}" if last else "")))
    try:
        gs = json.loads((HERE / "data" / ".guard_status.json").read_text(encoding="utf-8"))
    except Exception:
        gs = {}
    gw = find_procs("guard.py")
    if SUP.given_up.get("guard.py"):
        out.append(dict(key="guard", name="今日纪律", ok=False, text=SUP.given_up["guard.py"]))
    elif not gw:
        out.append(dict(key="guard", name="今日纪律", ok=False, text="纪律程序没有运行，正在启动……"))
    elif gs.get("note"):
        out.append(dict(key="guard", name="今日纪律", ok=False, warn=True, text=gs["note"]))
    elif not gs.get("names"):
        out.append(dict(key="guard", name="今日纪律", ok=False, warn=True,
                        text="还没定今天的打法：在 Claude 里说「开始今天」"))
    else:
        ev = gs.get("events") or []
        out.append(dict(key="guard", name="今日纪律", ok=bool(gs.get("locked")) and not ev, warn=not gs.get("locked") or bool(ev),
                        text=(f"{'已锁定' if gs.get('locked') else '草稿'}：{gs.get('bias')}，{'、'.join(gs.get('names') or [])}；"
                              f"已开 {gs.get('opens')}/{gs.get('max') or '—'} 笔" + (f"；最近：{ev[-1]}" if ev else ""))[:120]))
    c2m = (cfg.get("盘中C2") or {}).get("模式", "影子")
    if c2m == "关":
        out.append(dict(key="c2", name="早盘 C2 监测", ok=False, warn=True, off=True, text="已关闭（设置里打开）"))
    elif SUP.given_up.get("c2watch.py"):
        out.append(dict(key="c2", name="早盘 C2 监测", ok=False, text=SUP.given_up["c2watch.py"]))
    else:
        cw = find_procs("c2watch.py")
        cs = c2_status()
        out.append(dict(key="c2", name="早盘 C2 监测", ok=bool(cw), text=(f"{c2m}模式：" + (cs.get("note") or cs.get("brief") or "运行中"))
                        if cw else "没有运行，正在启动……"))
    sm = (cfg.get("策略提醒") or {}).get("模式", "提醒")
    if sm == "关":
        out.append(dict(key="strat", name="策略提醒", ok=False, warn=True, off=True, text="已关闭（设置.json → 策略提醒 → 模式）"))
    elif SUP.given_up.get("stratwatch.py"):
        out.append(dict(key="strat", name="策略提醒", ok=False, text=SUP.given_up["stratwatch.py"]))
    else:
        sp = find_procs("stratwatch.py")
        try:
            ss_ = json.loads((HERE / "data" / ".strat_status.json").read_text(encoding="utf-8"))
        except Exception:
            ss_ = {}
        out.append(dict(key="strat", name="策略提醒", ok=bool(sp), text=(f"{sm}模式：" + (ss_.get("note") or f"盯 {len(ss_.get('stocks') or {})} 只"))[:120]
                        if sp else "没有运行，正在启动……"))
    if SUP.given_up.get("hot.py"):
        out.append(dict(key="hot", name="热数据", ok=False, text=SUP.given_up["hot.py"]))
    else:
        hp = find_procs("hot.py")
        out.append(dict(key="hot", name="热数据", ok=bool(hp),
                        text=("运行中（盘中每分钟写「数据/热数据」）" if hp else "没有运行，正在启动……")))
    try:
        import sync
        ss, sst = sync.settings(cfg), sync.status()
        if not ss.get("启用"):
            out.append(dict(key="sync", name="E 盘同步", ok=False, warn=True, off=True,
                            text="没开启（设置 → 数据 → 同步到主机 E 盘）"))
        else:
            last = (sst.get("last_ok") or "")[5:16].replace("T", " ")
            failed = sst.get("last_err") and (sst.get("last_try") or "") > (sst.get("last_ok") or "")
            partial = sst.get("partial") and sst.get("last_err")
            out.append(dict(key="sync", name="E 盘同步", ok=bool(last) and not failed, warn=bool(failed or partial),
                            text=(f"上次成功 {last}" if last else "还没同步过")
                            + (f"（{sst['route']}）" if sst.get("route") and sst.get("route") != "连不上" else "")
                            + (f"；最近一次失败：{sst['last_err'][:50]}" if failed else
                               f"；个别文件没传上，下一轮自动补（{sst['last_err'][:40]}）" if partial else "")))
    except Exception as e:
        out.append(dict(key="sync", name="E 盘同步", ok=False, warn=True, text=f"读不到状态：{e}"))
    try:
        import history
        con = history.connect()
        try:
            r = con.execute("SELECT COUNT(DISTINCT code), MAX(ts) FROM bars_1d").fetchone()
            r5 = con.execute("SELECT MIN(ts) FROM bars_5m").fetchone()
        finally:
            con.close()
        if r and r[0]:
            out.append(dict(key="data", name="历史 K 线", ok=True,
                            text=f"{r[0]} 只，日线到 {str(r[1])[:10]}" + (f"，5 分钟从 {str(r5[0])[:10]}" if r5 and r5[0] else "")))
        else:
            out.append(dict(key="data", name="历史 K 线", ok=False, warn=True, text="还没有：收盘后在 Discord 发「回填历史」"))
    except Exception as e:
        out.append(dict(key="data", name="历史 K 线", ok=False, warn=True, text=f"读不到：{e}"))
    n = et_now()
    return dict(lights=out, et=n.strftime("%m-%d %H:%M"), session=session_name(n),
                local=dt.datetime.now().strftime("%H:%M"), jobs={k: dict(v, out=v.get("out", "")[-20000:]) for k, v in JOBS.items()})


# ================================================================ 今日数据
def today():
    import store
    import today as T
    day = T.session_day()
    res = dict(day=str(day))
    p = T.load_plan(day)
    if p:
        res["plan"] = dict(state=p.get("状态"), bias=p.get("偏向"), style=p.get("打法"), one=p.get("一句话"),
                           names=[dict(code=w["代码"], dir=w.get("方向"), entry=w.get("入场"), stop=w.get("认错价"),
                                       levels=[f"{x.get('价格')} {x.get('说明') or ''}" for x in w.get("关键价位") or []])
                                  for w in p["观察名单"]],
                           lim=p.get("限制") or {})
    rp = T.ROOT / str(day) / "盘前资料.md"
    res["pack"] = dt.datetime.fromtimestamp(rp.stat().st_mtime).strftime("%m-%d %H:%M") if rp.exists() else None
    res["analysis"] = (T.ROOT / str(day) / "盘前分析.md").exists()
    try:
        cfg = load_cfg()
        m = cfg.get("moomoo") or {}
        if _port(m.get("OpenD地址", "127.0.0.1"), m.get("OpenD端口", 11111)):
            import risk
            res["risk"] = risk.text(risk.today(cfg), day).replace("**", "")
    except Exception as e:
        res["risk"] = f"今日盈亏：读不到（{e}）"
    con = store.connect()
    try:
        import journal
        journal.connect().close()
        tr = con.execute("SELECT contract, direction, open_ts, close_ts, qty, pnl, tag, your_reason, plan_note FROM trades "
                         "WHERE date = ? ORDER BY open_ts", (str(day),)).fetchall()
        res["trades"] = [dict(c=t[0], dir=t[1], open=(t[2] or "")[11:16], close=(t[3] or "")[11:16], qty=t[4], pnl=t[5],
                              tag=t[6], reason=t[7], note=t[8]) for t in tr]
        try:
            import engine
            con.execute(engine.SCHEMA)
            res["calls"] = [dict(t=r[0], code=r[1], v=r[2], px=r[3]) for r in con.execute(
                "SELECT t, code, verdict, price FROM engine_calls WHERE date = ? ORDER BY ts DESC LIMIT 8", (str(day),))]
        except Exception:
            res["calls"] = []
    except Exception as e:
        res["db_err"] = str(e)
    finally:
        con.close()
    return res


# ================================================================ 设置
FIELDS = [
    ("早盘 C2", "盘中C2.模式", "早盘 C2 监测（1 分钟图）", "select", ["影子", "提醒", "关"], "影子 = 只记录、在控制台显示；提醒 = 入场时弹通知"),
    ("数据", "工作流.研究名单", "研究名单（逗号分隔，留空 = 全部自选）", "list", None, "存 K 线的范围"),
    ("数据", "存储.启用", "同步到主机 E 盘", "bool", None, "要先在主机上装存储服务（host_setup → install_storage.cmd）"),
    ("数据", "存储.同步间隔_分钟", "同步间隔（分钟）", "number", None, "历史 K 线库每天收盘复盘后同步一次"),
    ("数据", "存储.主机地址", "主机地址（局域网 IP）", "text", None, "在家时走这个"),
    ("数据", "存储.外网地址", "主机外网地址（Tailscale）", "text", None, "100. 开头；不在家时自动走这个"),
    ("自动任务", "Discord机器人.今日简报.启用", "每天自动盘前报告", "bool", None, "美东 8:45，精简版发到 #快讯"),
    ("自动任务", "Discord机器人.自动收盘复盘.启用", "每天收盘自动复盘", "bool", None, "美东 16:20"),
    ("自动任务", "Discord机器人.自动周报.启用", "每周五自动发周报", "bool", None, "美东 17:00，收盘复盘之后"),
    ("自动任务", "Discord机器人.自动夜盘复盘.启用", "每天自动夜盘复盘", "bool", None, "美东 4:10"),
    ("自动任务", "Discord机器人.自动快讯.启用", "每天自动开盘前快讯", "bool", None, "悉尼 01:00"),
    ("自动任务", "Grok盘前.发到Discord", "Grok 雷达结果发到 Discord", "bool", None, "关 = 只归档，内容合进盘前报告"),
    ("自动任务", "Discord机器人.自动夜盘复盘.发到Discord", "夜盘复盘发到 Discord", "bool", None, "关 = 只存进今日文件夹"),
    ("自动任务", "Reddit散户.发到Discord", "散户热度提醒发到 Discord", "bool", None, "要先在 #散户 发「开启散户监测」"),
]


def _get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def settings_view():
    cfg = load_cfg()
    return [dict(group=g, path=p, label=lab, type=t, options=o, help=h, value=_get(cfg, p)) for g, p, lab, t, o, h in FIELDS]


def settings_save(path, value):
    spec = {p: (t, o) for _, p, _, t, o, _ in FIELDS}
    if path not in spec:
        raise ValueError("不能修改这一项")
    t, opts = spec[path]
    if t == "bool":
        value = bool(value)
    elif t == "number":
        value = None if value in (None, "") else float(value)
        if value is not None and value == int(value):
            value = int(value)
    elif t == "select":
        if value not in opts:
            raise ValueError(f"只能选 {'/'.join(opts)}")
    elif t == "list":
        value = [x.strip().upper() for x in str(value or "").replace("，", ",").split(",") if x.strip()]
    else:
        value = str(value or "").strip()
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    d = raw
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    old = d.get(keys[-1])
    d[keys[-1]] = value
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)
    log(f"设置 {path}：{old!r} → {value!r}")
    try:
        import store
        con = store.connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS param_changes (ts TEXT, key TEXT, old TEXT, new TEXT, reason TEXT)")
            with con:
                con.execute("INSERT INTO param_changes VALUES (?,?,?,?,?)", (dt.datetime.now().isoformat(timespec="seconds"),
                            path, json.dumps(old, ensure_ascii=False), json.dumps(value, ensure_ascii=False), "控制台"))
        finally:
            con.close()
    except Exception:
        pass
    return value


# ================================================================ 后台任务
JOBS = {}


def run_job(name, fn):
    if JOBS.get(name, {}).get("running"):
        return False
    JOBS[name] = dict(running=True, start=dt.datetime.now().strftime("%H:%M:%S"), out="")

    def go():
        try:
            out = fn()
            JOBS[name].update(running=False, ok=True, out=str(out or "完成"), end=dt.datetime.now().strftime("%H:%M:%S"))
        except Exception as e:
            JOBS[name].update(running=False, ok=False, out=f"出错：{type(e).__name__}: {e}", end=dt.datetime.now().strftime("%H:%M:%S"))
            log(f"任务 {name} 出错：" + traceback.format_exc())
    threading.Thread(target=go, daemon=True).start()
    return True


# ================================================================ 工具（原来的 .command 和 Discord 命令）
_PRE = "import sys; sys.path.insert(0, '程序'); import macro_report as mr; cfg = mr.load_config(); "
# key: (分组, 按钮名, 参数, 说明, 是否动 K 线库)。参数是 ("脚本.py", 参数…) 或 ("-c", 代码)
TOOLS = {
    "pack":     ("盘前", "生成盘前报告", ("today.py", "--full"),
                 "抓新闻、补夜盘和 Grok 推特（今天没跑过才跑，按用量收费）、读 Reddit，写「今日/日期/盘前资料.md」", False),
    "judge":    ("盘中", "盘感判断", ("engine.py", "{code}"), "现在这只股票：相似情形的概率 + 追 / 等 / 观察 / 不做，并画相似走势图（先填代码）", False),
    "walls":    ("盘前", "期权墙", ("flows.py", "{code}"), "这只股票最近 7 天到期期权的看涨墙 / 看跌墙、净 gamma，和现在的资金流向（先填代码）", False),
    "patstat":  ("维护", "形态概率重算", ("patterns.py", "--build"), "用历史 1 分钟K重算信号K / 组合K / 震荡突破的概率，写 报告/形态概率_日期.md（约 2 分钟；收盘后自动）", True),
    "review":   ("收盘后", "收盘复盘", ("review.py",), "K 线入库、拉成交、对照纪律、评估今天的盘感判断；平时美东 16:20 自动", True),
    "weekly":   ("收盘后", "周报", ("-c", _PRE + "import weekly; print(weekly.build(None, None, False, cfg)[0].replace('**', ''))"),
                 "本周交易、纪律、盘感引擎和 C2 的结果；平时周五 17:00 自动", False),
    "report":   ("报告", "Word 报告", ("macro_report.py",), "宏观 + 自选股 Word 报告，存进「报告」；开着网站分享时会上传并推送", False),
    "brief":    ("报告", "开盘前快讯", ("-c", _PRE + "import briefing; print('\\n\\n'.join(briefing.build(cfg)).replace('**', ''))"),
                 "日历、宏观、自选股行情和消息（只显示在这里，不发 Discord）", False),
    "grok":     ("报告", "Grok 推特雷达", ("-c", _PRE + "import grokscan, news; r = grokscan.run(cfg, print); "
                                            "print(r.get('msg') or f\"{len(r.get('posts') or [])} 条帖子，约 ${r.get('cost', 0):.2f}\"); "
                                            "print(news.collect(cfg, 24, print, ('grok',)))"),
                 "重新扫一遍关注的 X 账号并归档；按 Grok 用量收费", False),
    "news":     ("维护", "归档新闻", ("news.py",), "平时每 20 分钟自动", False),
    "night":    ("维护", "夜盘复盘", ("nightly.py",), "平时美东 4:10 自动", False),
    "reddit":   ("维护", "散户热度", ("-c", _PRE + "import reddit_watch as RW; print('\\n\\n'.join(RW.snapshot_text(cfg, None, print)))"),
                 "Reddit 热度榜和自选股热度", False),
    "cold":     ("维护", "导出冷数据", ("cold.py",), "收盘复盘时自动", True),
    "c2bt":     ("维护", "C2 回测", ("-c", "import sys; sys.path.insert(0, '程序'); import c2; d = c2.backtest(); print(c2.summary(d))"),
                 "全部有 1 分钟 K 的股票跑一遍 C1/C2/C3 规则", False),
    "intraday": ("收盘后", "日内策略库重算", ("intraday.py",),
                 "1 分钟K × 12 条入场（多空）× 7 种止损 × 8 种止盈，换算成期权收益（约 2 分钟；收盘复盘后自动）。结果看「策略库」页的日内部分", False),
    "dtbt":     ("维护", "盘中认形态回测", ("daytype_bt.py",),
                 "9:45 / 10:00 / 10:30 / 11:00 只用当时的K线认今天的形态，照着做期权赚不赚（约 1 分钟）。写 报告/盘中认形态回测_日期.md", False),
    "regbt":    ("维护", "按形态匹配打法回测", ("regime_bt.py",),
                 "盘中认出形态后，用这种形态下最好的入场 + 止损 + 止盈去做，样本外赚不赚（约 6 分钟）。写 报告/按形态匹配打法回测_日期.md", True),
    "structbt": ("维护", "市场结构回测", ("structure_bt.py",),
                 "B 站视频的「外部结构定方向 + 内部结构转回顺势进场」（箱体法、收盘为准），34 只股票 2019 起，和随机时间比（约 5 分钟）。写 报告/市场结构回测_日期.md", True),
    "scalp80":  ("维护", "80% 胜率剥头皮回测", ("scalp80_bt.py",),
                 "B 站「Crypto-阿洋」两个「80% 胜率剥头皮」（1 分钟 CE + ZLSMA；3 分钟 SMMA + 分形 + RSI），普通K线 / 平均K线图对比，和随机时间比（约 2 分钟）。写 报告/80胜率剥头皮回测_日期.md", True),
    "sizing":   ("收盘后", "仓位方案回测", ("sizing.py",),
                 "只做 1 张 / 固定美元风险 / 金字塔 / 分批出场 / 广撒网滚仓 + 熔断，只用 2019–2023 挑组合、2024 起检验（约 2 分钟；收盘复盘后自动）。写 报告/仓位方案回测_日期.md", False),
    "playbook": ("盘前", "今日预案", ("-c", "import sys; sys.path.insert(0, '程序'); import playbook; playbook.build(live=True)"),
                 "按开盘类型和行情从策略库挑策略，写 今日/日期/预案.md；读实时期权 Delta（要 OpenD）。盘前报告时自动", False),
    "stratlib": ("收盘后", "策略库重算", ("stratlib.py",),
                 "玉衡策略库：全部自选股的日线 × 34 条策略回测、行情分类、今天触发的信号（约 10 秒；收盘复盘后自动）。结果看「策略库」页", False),
    "engcheck": ("维护", "盘感引擎体检", ("engine.py", "--validate"), "走前验证：只用过去预测未来，看概率准不准（约 10 秒）", False),
    "engbuild": ("维护", "重建引擎缓存", ("engine.py", "--build"), "收盘复盘时自动；改了 Kill Zone 或引擎参数后会自动重建", True),
    "backfill": ("维护", "回填历史 K 线", ("review.py", "--backfill"), "很慢（几十分钟），请在收盘后点", True),
    "check":    ("维护", "检查行情和账户", ("review.py", "--check"), "OpenD、交易账户（只读）、成交、K 线能不能读到", False),
    "share":    ("维护", "测试网站和推送", ("share.py", "--test"), "用最新一份报告测试上传网站和群推送（会真的推送）", False),
}


def run_tool(key, code=None):
    """在后台跑一个工具，输出一行行写进 JOBS[key]。返回 (是否开始, 说明)。"""
    if key not in TOOLS:
        return False, "未知工具"
    group, label, args, _, heavy = TOOLS[key]
    if JOBS.get(key, {}).get("running"):
        return False, f"「{label}」正在运行"
    if heavy:
        busy = [TOOLS[k][1] for k, j in JOBS.items() if k in TOOLS and TOOLS[k][4] and j.get("running")]
        if busy:
            return False, f"「{busy[0]}」正在用 K 线库，等它跑完再点"
    if "{code}" in args:
        code = str(code or "").strip().upper().lstrip("$")
        if not code or len(code) > 10 or not all(ch.isalnum() or ch == "." for ch in code):
            return False, "先填股票代码，例如 NVDA"
        args = tuple(code if a == "{code}" else a for a in args)
    cmd = osutil.caffeinate_prefix(heavy) + ARCH + [PY]
    cmd += ["-c", args[1]] if args[0] == "-c" else [str(HERE / args[0])] + list(args[1:])
    title = label + (f" {code}" if code and "{code}" in TOOLS[key][2] else "")
    JOBS[key] = dict(running=True, label=title, start=dt.datetime.now().strftime("%H:%M:%S"), out="")

    def go():
        try:
            p = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 text=True, encoding="utf-8", errors="replace",
                                 env=dict(os.environ, **CHILD_ENV), **osutil.detach_kwargs())
            JOBS[key]["pid"] = p.pid
            for line in p.stdout:
                JOBS[key]["out"] = (JOBS[key]["out"] + line)[-30000:]
            rc = p.wait()
            JOBS[key].update(running=False, ok=rc == 0, end=dt.datetime.now().strftime("%H:%M:%S"),
                             out=JOBS[key]["out"].strip() or ("完成" if rc == 0 else f"出错（退出码 {rc}）"))
        except Exception as e:
            JOBS[key].update(running=False, ok=False, out=f"出错：{type(e).__name__}: {e}", end=dt.datetime.now().strftime("%H:%M:%S"))
            log(f"工具 {label} 出错：" + traceback.format_exc())
        log(f"工具 {title}：{'完成' if JOBS[key].get('ok') else '失败'}")
    threading.Thread(target=go, daemon=True).start()
    log(f"工具 {title}：开始")
    return True, f"开始：{title}"


def stop_tool(key):
    pid = JOBS.get(key, {}).get("pid")
    if pid and JOBS[key].get("running"):
        osutil.kill_tree(pid)
        return True
    return False


def tools_view():
    return [dict(key=k, group=g, label=lab, help=h, input="{code}" in a) for k, (g, lab, a, h, _) in TOOLS.items()]


def open_path(p):
    p.mkdir(parents=True, exist_ok=True)
    osutil.open_path(p)


def _trainer_current():
    """正在跑的盘感训练是不是最新版（程序更新后旧进程还在跑，新页面会调用旧程序没有的接口 → not found）。"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8765/api/version", timeout=2) as r:
            v = json.loads(r.read().decode("utf-8")).get("version")
        return v == int((HERE / "trainer.py").stat().st_mtime)
    except Exception:
        return False


def open_trainer():
    if find_procs("trainer.py") and not _trainer_current():
        log("盘感训练程序已更新，重启它")
        kill_all("trainer.py")
    if not find_procs("trainer.py"):
        p = subprocess.Popen(ARCH + [PY, str(HERE / "trainer.py"), "--no-browser"], cwd=str(BASE),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, **CHILD_ENV), **osutil.detach_kwargs())
        time.sleep(1.5)
    open_url("http://127.0.0.1:8765/")


def _dash_current():
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8767/api/version", timeout=2) as r:
            v = json.loads(r.read().decode("utf-8")).get("version")
        return v == int((HERE / "dash.py").stat().st_mtime)
    except Exception:
        return False


def open_dash():
    """盯盘界面（dash.py，网页 8767）：没开就开，程序更新过就重启。"""
    if find_procs("dash.py") and not _dash_current():
        log("盯盘界面程序已更新，重启它")
        kill_all("dash.py")
    if not find_procs("dash.py"):
        subprocess.Popen(ARCH + [PY, str(HERE / "dash.py"), "--no-browser"], cwd=str(BASE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, **CHILD_ENV), **osutil.detach_kwargs())
        time.sleep(2)
    open_url("http://127.0.0.1:8767/")


def open_url(url, app=False):
    chrome = "/Applications/Google Chrome.app"
    if sys.platform == "darwin" and app and os.path.exists(chrome):
        subprocess.Popen(["open", "-na", chrome, "--args", f"--app={url}", "--window-size=1320,900"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        webbrowser.open(url)


def quit_all():
    log("退出：停止机器人、纪律程序、热数据、C2 监测、盘感训练")
    try:                                         # 退出前最后同步一次
        import sync
        if sync.settings(load_cfg()).get("启用"):
            log(sync.run(load_cfg()))
    except Exception:
        pass
    SUP.stop.set()
    for s in ("bot.py", "trainer.py", "dash.py", "c2watch.py", "guard.py", "hot.py", "stratwatch.py"):
        kill_all(s)
    threading.Timer(0.5, lambda: os._exit(0)).start()


# ================================================================ 网页
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                import version
                self._send(200, version.page(HERE / "console.html"), "text/html; charset=utf-8")
            elif self.path in ("/cyber.css", "/cyber.js", "/yuheng.css"):
                self._send(200, (HERE / self.path[1:]).read_bytes(),
                           "text/css; charset=utf-8" if self.path.endswith(".css") else "application/javascript; charset=utf-8")
            elif self.path.startswith("/brand/"):       # 玉衡标志素材（只认 brand 目录下的文件名）
                name = self.path.removeprefix("/brand/").split("?")[0]
                f = HERE / "brand" / name
                if "/" in name or ".." in name or not f.is_file():
                    return self._send(404, {"error": "not found"})
                self._send(200, f.read_bytes(), "image/png" if name.endswith(".png") else "image/x-icon")
            elif self.path.split("?")[0] in ("/pre", "/premarket.html"):     # 盘前分析：每只股票一块，点开看形态概率
                import version
                self._send(200, version.page(HERE / "premarket.html"), "text/html; charset=utf-8")
            elif self.path.split("?")[0] == "/api/pre":
                import premarket
                self._send(200, premarket.build(max_age=0 if "force=1" in self.path else 60))
            elif self.path.split("?")[0] in ("/lib", "/lib.html"):
                import version
                self._send(200, version.page(HERE / "lib.html"), "text/html; charset=utf-8")
            elif self.path == "/api/intraday":           # 日内策略库汇总（intraday.py 生成）
                f = HERE / "data" / "日内策略库" / "index.json"
                self._send(200, f.read_bytes() if f.exists() else {"missing": True})
            elif self.path == "/api/sizing":             # 仓位方案回测（sizing.py 生成）
                f = HERE / "data" / "日内策略库" / "sizing.json"
                self._send(200, f.read_bytes() if f.exists() else {"missing": True})
            elif self.path == "/api/playbook":           # 今日预案（playbook.py 生成）
                f = HERE / "data" / "策略库" / "playbook.json"
                self._send(200, f.read_bytes() if f.exists() else {"missing": True})
            elif self.path == "/api/lib":                # 策略库：汇总（stratlib.py 生成）
                f = HERE / "data" / "策略库" / "index.json"
                self._send(200, f.read_bytes() if f.exists() else {"missing": True})
            elif self.path.startswith("/api/lib/"):      # 策略库：单只股票
                code = urllib.parse.unquote(self.path.removeprefix("/api/lib/")).upper()
                f = HERE / "data" / "策略库" / f"{code}.json"
                if not code.replace(".", "").isalnum() or code == "INDEX" or not f.exists():
                    return self._send(404, {"error": f"没有 {code} 的策略库结果"})
                self._send(200, f.read_bytes())
            elif self.path.startswith("/api/job"):       # 单个工具的输出（策略库页轮询用）
                key = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("key", [""])[0]
                j = JOBS.get(key, {})
                self._send(200, {"running": bool(j.get("running")), "ok": j.get("ok"), "out": j.get("out", "")[-20000:]})
            elif self.path == "/api/pos":
                import roll
                self._send(200, roll.snapshot(None))
            elif self.path == "/api/ping":
                self._send(200, {"ok": True})
            elif self.path == "/api/status":
                self._send(200, status())
            elif self.path == "/api/c2":
                self._send(200, c2_status())
            elif self.path == "/api/today":
                self._send(200, today())
            elif self.path == "/api/settings":
                self._send(200, settings_view())
            elif self.path == "/api/tools":
                self._send(200, tools_view())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            log("网页出错：" + traceback.format_exc())
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/settings":
                return self._send(200, {"value": settings_save(d.get("path"), d.get("value"))})
            if self.path == "/api/action":
                a = d.get("name")
                if a == "restart_bot":
                    restart_bot()
                elif a == "tool":
                    ok, msg = run_tool(d.get("key"), d.get("code"))
                    return self._send(200 if ok else 400, {"ok": True, "msg": msg} if ok else {"error": msg})
                elif a == "tool_stop":
                    return self._send(200, {"ok": stop_tool(d.get("key"))})
                elif a == "data":
                    open_path(BASE / "数据")
                elif a == "today_dir":
                    import today as T
                    open_path(T.folder())
                elif a == "trainer":
                    threading.Thread(target=open_trainer, daemon=True).start()
                elif a == "dash":
                    threading.Thread(target=open_dash, daemon=True).start()
                elif a == "sync_now":
                    import sync
                    run_job("E盘同步", lambda: sync.run(load_cfg()))
                elif a == "reports":
                    osutil.open_path(REPORT_DIR)
                elif a == "quit":
                    self._send(200, {"ok": True})
                    quit_all()
                    return
                else:
                    return self._send(400, {"error": "未知操作"})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as e:
            log("网页出错：" + traceback.format_exc())
            self._send(500, {"error": f"{type(e).__name__}: {e}"})




def clean_start():
    """打开新的控制台前清场：先让旧控制台正常退出（它会先同步一次 E 盘），再关掉所有还在跑的 程序/ 进程。"""
    keep = {os.getpid(), os.getppid()}
    if _port("127.0.0.1", PORT):
        try:
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/action", data=b'{"name": "quit"}', method="POST",
                                         headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{PORT}"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 30 and _port("127.0.0.1", PORT):
            time.sleep(0.5)

    # 程序/ 下的脚本（常驻程序、工具任务、旧控制台）、控制台「工具」里用 -c 跑的任务、套在外面的 caffeinate
    found = osutil.find_all_program_procs(exclude=keep)
    pids = [p for p, _ in found]
    names = [n for _, n in found]
    osutil.terminate(pids, wait=5.0)
    log("打开时清场：" + (f"关掉 {len(pids)} 个旧进程（{'、'.join(sorted(set(names)))}）" if pids else "没有旧进程"))


def main(open_window=True, supervise=True):
    url = f"http://127.0.0.1:{PORT}/"
    clean_start()                                # 每次打开都从干净的状态开始
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    log("控制台启动")
    osutil.keep_awake()                          # 控制台开着时电脑不睡眠（macOS caffeinate / Windows 系统接口）
    signal.signal(signal.SIGTERM, lambda *a: quit_all())
    if supervise:
        threading.Thread(target=SUP.run, daemon=True).start()
    if open_window:
        threading.Timer(1.0, lambda: open_url(url, app=True)).start()
    import version
    print(f"玉衡5.0 v{version.get()} 控制台：{url}")
    srv.serve_forever()


if __name__ == "__main__":
    main(open_window="--no-window" not in sys.argv, supervise="--no-supervise" not in sys.argv)
