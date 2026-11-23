# -*- coding: utf-8 -*-
"""
盘中 C2 监测（1 分钟图）：开盘到 11:00，每根 1 分钟K收盘后，用 c2.py 的规则检查观察名单里每只股票

  每只股票的状态：等信号 → C1（第 6 根确认方向）→ 回踩中 → 入场 / 作废 / 过时 / 不做
  出现「入场」时：记录入场价、止损、目标
  收盘后（美东 16:05）自动算出每个入场信号的结果（R），给周报和以后校准用

  设置.json →「盘中C2」
    模式       关 / 影子（只记录、在控制台显示，默认）/ 提醒（入场时弹通知）
    观察名单   要盯的股票；留空 = 盘感训练里的大盘股名单
  只读行情，不下单。数据库表 c2_live。状态文件 data/.c2_status.json（控制台读）。
"""
import datetime as dt
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import c2      # noqa: E402
import store   # noqa: E402

DATA = HERE / "data"
STATUS = DATA / ".c2_status.json"
BT_CACHE = DATA / ".c2_backtest.json"
PID_FILE = HERE / ".c2watch.pid"
DEFAULT = {"模式": "影子", "观察名单": [],
           "说明": "C1/C2/C3 打法盘中监测（1 分钟图，开盘到 11:00）。模式：关 / 影子（只记录，控制台显示）/ 提醒（入场时弹通知）。观察名单留空 = 大盘股名单。"}
SCHEMA = """CREATE TABLE IF NOT EXISTS c2_live (
    date TEXT, code TEXT, ts TEXT, phase TEXT, dir TEXT, entry REAL, stop REAL, target REAL, reasons TEXT,
    ai_stance TEXT, ai_reason TEXT, r_result REAL, hit TEXT, PRIMARY KEY (date, code, phase))"""


def cfg_all():
    try:
        return json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def wset(cfg):
    s = dict(DEFAULT)
    s.update(cfg.get("盘中C2") or {})
    return s


def watchlist(cfg):
    lst = []
    try:                                        # 今天和 Claude 定下的观察名单优先
        import today
        lst = today.watch_codes()
    except Exception:
        lst = []
    lst = lst or [str(x).strip().upper() for x in (wset(cfg).get("观察名单") or []) if str(x).strip()]
    if not lst:
        import history
        lst = sorted(history.big_list())
    if "QQQ" not in lst:
        lst.append("QQQ")                       # QQQ 一直算，用来看大盘方向
    return lst


def et_now():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York"))


def log(t):
    print(f"[{et_now():%H:%M:%S} 美东] {t}", flush=True)


# ================================================================ 行情
class Feed:
    """moomoo 1 分钟K：先订阅实时K线（不占历史K线额度），不行再用历史K线接口。"""

    def __init__(self, cfg, codes):
        import stocks
        self.mm = stocks.Moomoo(cfg.get("moomoo") or {}, lambda *a, **k: None)
        self.sub = False
        if self.mm.ok:
            try:
                ret, _ = self.mm.ctx.subscribe(["US." + c for c in codes], [self.mm.ft.SubType.K_1M], subscribe_push=False)
                self.sub = ret == self.mm.ft.RET_OK
            except Exception:
                self.sub = False

    @property
    def ok(self):
        return self.mm.ok

    def bars(self, code, now):
        import pandas as pd
        ft = self.mm.ft
        df = None
        if self.sub:
            try:
                ret, d = self.mm.ctx.get_cur_kline("US." + code, 400, ft.SubType.K_1M, ft.AuType.QFQ)
                if ret == ft.RET_OK and len(d):
                    df = d
            except Exception:
                df = None
        if df is None:
            ret, d, _ = self.mm.ctx.request_history_kline("US." + code, start=str(now.date()), end=str(now.date()),
                                                          ktype=ft.KLType.K_1M, autype=ft.AuType.QFQ, max_count=1000)
            if ret != ft.RET_OK:
                return None
            df = d
        df = df.copy()
        df.index = pd.to_datetime(df["time_key"])
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].astype(float)
        df = df[df.index.normalize() == pd.Timestamp(now.date())]
        df = df[df.index <= pd.Timestamp(now.replace(tzinfo=None))]          # 只要已经收盘的K线（时间 = 结束时间）
        return c2.session(df.sort_index())

    def close(self):
        try:
            self.mm.close()
        except Exception:
            pass


# ================================================================ 记录
def record(day, code, st, ai=None):
    con = store.connect()
    try:
        con.execute(SCHEMA)
        with con:
            con.execute("INSERT OR IGNORE INTO c2_live (date, code, ts, phase, dir, entry, stop, target, reasons, ai_stance, ai_reason) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (str(day), code, dt.datetime.now().isoformat(timespec="seconds"), st["phase"], st.get("dir"),
                         st.get("entry"), st.get("stop"), st.get("target"), "；".join(st.get("reasons") or []),
                         (ai or {}).get("stance"), (ai or {}).get("reason")))
    finally:
        con.close()


def set_ai(day, code, ai):
    con = store.connect()
    try:
        with con:
            con.execute("UPDATE c2_live SET ai_stance = ?, ai_reason = ? WHERE date = ? AND code = ? AND phase = '入场'",
                        (ai.get("stance"), ai.get("reason"), str(day), code))
    finally:
        con.close()


def backtest_note():
    try:
        return json.loads(BT_CACHE.read_text(encoding="utf-8")).get("text") or ""
    except Exception:
        return ""


# ================================================================ 状态
STATE = {}


def write_status(day, note=""):
    try:
        STATUS.write_text(json.dumps(dict(date=str(day), at=f"{et_now():%H:%M}", note=note, codes=STATE),
                                     ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def notify(title, text):
    if sys.platform != "darwin":
        return
    import subprocess
    t = text.replace('"', "'")[:200]
    subprocess.Popen(["osascript", "-e", f'display notification "{t}" with title "{title}"'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    snd = HERE / "sounds" / "alert.mp3"                     # 交易者选的提示音；没有就用系统声音
    subprocess.Popen(["afplay", str(snd) if snd.exists() else "/System/Library/Sounds/Glass.aiff"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_PDH = {}


def b_codes(day):
    """打法 B 只做多：纪律设置里方向是「多」或「双向」的股票。"""
    try:
        import today
        p = today.load_plan(day)
        return {w["代码"] for w in (p or {}).get("观察名单") or [] if w.get("方向") in ("多", "双向")}
    except Exception:
        return set()


def scan_b_all(cfg, feed, codes, now, atr):
    """打法 B 信号：每只每天只报第一个。"""
    day = now.date()
    if _PDH.get("day") != day:
        import brief
        _PDH.clear()
        _PDH.update(day=day, lv=brief.prev_day_levels(codes))
    qb = feed.bars("QQQ", now)
    for code in b_codes(day) & set(codes):
        item = STATE.setdefault(code, {})
        if item.get("b"):
            continue
        b = feed.bars(code, now)
        sig = c2.scan_b(b, atr.get(code), qqq=qb, prev_high=(_PDH["lv"].get(code) or (None,))[0])
        if not sig:
            continue
        item["b"] = dict(t=f"{now:%H:%M}", **sig)
        record(day, code, dict(phase="B信号", dir="多", entry=sig["entry"], stop=None, target=sig["target"], reasons=sig["reasons"]))
        log(f"{code} 打法 B 信号：{sig['reasons'][0]}")
        if wset(cfg).get("模式") == "提醒":
            notify("打法 B：大实体站上 VWAP", f"{code} 约 {sig['entry']:.2f}，前高 {sig['target']:.2f}（距离 ${sig['dist']:.2f}）。"
                                         f"QQQ 同向。期权亏 0.5 就走")


_VT = {}
AWAY_ATR = 0.05         # 先离开 VWAP 至少这么多倍 ATR，再碰回来才算一次回踩


def vwap_touch(b, atr_d, side, st):
    """b：到目前为止已收盘的 1 分钟K；side = 1（看多：从上方回踩）/ -1（看空：从下方反弹）。
    st：这只股票的状态 {away: 是否已经离开过}。碰到了返回 VWAP 价，否则 None。前 5 根不算。"""
    if b is None or len(b) < 6 or not atr_d:
        return None
    import numpy as np
    h, l, c, v = (b[x].values.astype(float) for x in ("high", "low", "close", "volume"))
    vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)
    i = len(c) - 1
    if side * (c[i] - vw[i]) >= AWAY_ATR * atr_d:
        st["away"] = True
    if st.get("away") and l[i] <= vw[i] <= h[i]:
        st["away"] = False
        return float(vw[i])
    return None


def vwap_touch_all(cfg, feed, codes, now):
    """纪律设置名单里的每只：按计划方向，正股回踩到 VWAP 就马上通知（每次回踩一次）。"""
    import today
    p = today.load_plan(now.date())
    if not p:
        return
    for w in p.get("观察名单") or []:
        code, d = w["代码"], w.get("方向")
        if code not in codes:
            continue
        sides = [1] if d == "多" else [-1] if d == "空" else [1, -1]
        b = feed.bars(code, now)
        a = c2.daily_atr(code, str(now.date())) if code not in _VT.get("atr", {}) else _VT["atr"][code]
        _VT.setdefault("atr", {})[code] = a
        for side in sides:
            st = _VT.setdefault((str(now.date()), code, side), {})
            vwp = vwap_touch(b, a, side, st)
            if vwp is None:
                continue
            px = float(b["close"].iloc[-1])
            what = "从上方回踩 VWAP（看多）" if side == 1 else "从下方反弹到 VWAP（看空）"
            log(f"{code} {what}：VWAP {vwp:.2f}，现价 {px:.2f}")
            if wset(cfg).get("模式") != "提醒":           # 影子模式只记录，不弹（交易者 2026-11-19 决定：盘中只留纪律弹窗）
                continue
            notify(f"{code} 回踩 VWAP", f"{what} {vwp:.2f}，现价 {px:.2f}。碰到的这根不进，等确认 K（{'收盘越过前一根高点或吞没' if side == 1 else '收盘跌破前一根低点或看跌吞没'}）")


_PAT = {"recent": [], "last": {}}


def pattern_scan(cfg, feed, codes, now, atr, c1dirs):
    """形态和震荡（patterns.py）：写 热数据/形态.md、记录、按「形态监测 → 提醒」设置弹通知。"""
    import patterns
    import today
    ps = patterns.settings(cfg)
    mode = ps.get("提醒") or "突破和A/B级"
    if mode == "关":
        return
    day = now.date()
    if _PAT.get("day") != day:
        import brief
        _PAT.update(day=day, recent=[], last={}, seen=set(), pdl=brief.prev_day_levels(codes))
    plan = today.load_plan(day) or {}
    items = {w["代码"]: w for w in plan.get("观察名单") or []}
    qb = feed.bars("QQQ", now)
    results = {}
    for code in codes:
        b = feed.bars(code, now)
        if plan:
            it = items.get(code)
            pdir = (it or {}).get("方向")
        else:
            it, pdir = None, "双向"                      # 今天还没定纪律：只打分，不卡名单
        r = patterns.live(code, b, atr.get(code), qb, it, pdir, c1dirs.get(code), _PAT["pdl"].get(code), cfg)
        results[code] = r
        if not plan and r["events"]:
            for e in r["events"]:
                e["checks"].append("（今天的纪律设置还没定，没核对名单）")
        tell = []
        seen = _PAT.setdefault("seen", set())
        for e in r["events"]:
            key = (code, e["frame"], e["name"], e["t"])      # 同一根K只算一次（行情卡住时最新一根不会变）
            if key in seen:
                continue
            seen.add(key)
            _PAT["recent"].append((code, e))
            is_range = e["name"] in patterns.RANGE_NAMES
            ok = (mode != "只记录" and e["grade"] not in ("纪律不允许", "震荡中") and
                  (is_range or (mode == "突破和A级" and e["grade"] == "A") or (mode == "突破和A/B级" and e["grade"] in ("A", "B"))))
            if ok and not is_range:
                last = _PAT["last"].get(code)
                gap = float(ps.get("同一只最短间隔_分钟") or 5)
                if last and (now - last).total_seconds() < gap * 60:
                    ok = False
            patterns.record(day, code, e, ok)
            if ok:
                tell.append(e)
        if tell:
            _PAT["last"][code] = now
            rank = {"A": 0, "B": 1, "C": 2}
            tell.sort(key=lambda e: (e["name"] not in patterns.RANGE_NAMES, rank.get(e["grade"], 3)))
            e = tell[0]
            names = "、".join(dict.fromkeys(x["name"] for x in tell))
            arrow = "看多" if e["dir"] == 1 else "看空"
            extra = ""
            if e.get("box"):
                bh, bl, L = e["box"]
                extra = f"区间 {bl:.2f}–{bh:.2f}，高 ${bh - bl:.2f}。"
            log(f"{code} 形态：{names}（{e['grade']} 级）")
            notify(f"{code} {e['grade']} 级 · {names}", f"{arrow}，价 {e['price']:.2f}。{extra}止损约 ${e['stop_usd']:.2f}。"
                                                      + (e["prob"].split("，结论")[0] if e.get("prob") else ""))
    patterns.write_hot(day, now, results, _PAT["recent"])


def scan_once(cfg, feed, codes, now, atr, pcs=None):
    day = now.date()
    s = c2.settings(cfg)
    qqq = {}
    c1dirs = {}
    try:
        vwap_touch_all(cfg, feed, codes, now)
    except Exception:
        log("VWAP 回踩检查出错：" + traceback.format_exc().splitlines()[-1])
    try:
        scan_b_all(cfg, feed, codes, now, atr)
    except Exception:
        log("打法 B 检查出错：" + traceback.format_exc().splitlines()[-1])
    for code in codes:
        b = feed.bars(code, now)
        if b is None:
            STATE.setdefault(code, {}).update(phase="读不到行情")
            continue
        st = c2.scan(b, atr.get(code), s=s, prev_close=(pcs or {}).get(code))
        if st["phase"] in ("C1", "回踩中", "入场", "过时"):
            c1dirs[code] = 1 if st.get("dir") == "多" else -1
        prev = STATE.get(code, {}).get("phase")
        item = STATE.setdefault(code, {})
        item.update(phase=st["phase"], dir=st.get("dir"), reason=(st.get("reasons") or [""])[0] + (
                        f"　｜ 打法 B {item['b']['t']}：前高 {item['b']['target']:.2f}" if item.get("b") else ""),
                    entry=st.get("entry"), stop=st.get("stop"), target=st.get("target"), t=f"{now:%H:%M}")
        if code == "QQQ":
            qqq = dict(phase=st["phase"], dir=st.get("dir"), vwap=st.get("vwap"),
                       above=bool(len(b)) and st.get("vwap") is not None and float(b["close"].iloc[-1]) > st["vwap"])
        if st["phase"] != prev and st["phase"] not in ("等信号",):
            record(day, code, st)
            log(f"{code} → {st['phase']}：{item['reason']}")
            if st["phase"] == "入场":
                if wset(cfg).get("模式") == "提醒":
                    notify("C2 入场信号", f"{code} {'做多' if st['dir'] == '多' else '做空'}：入场约 {st['entry']:.2f}，"
                                         f"止损 {st['stop']:.2f}，目标 {st['target']:.2f}")
    try:
        pattern_scan(cfg, feed, codes, now, atr, c1dirs)
    except Exception:
        log("形态检查出错：" + traceback.format_exc().splitlines()[-1])
    write_status(day)


def evaluate_today(cfg, feed, day):
    """收盘后：用当天完整的 1 分钟K算每个入场信号的结果。"""
    con = store.connect()
    try:
        con.execute(SCHEMA)
        rows = con.execute("SELECT code FROM c2_live WHERE date = ? AND phase = '入场' AND r_result IS NULL", (str(day),)).fetchall()
    finally:
        con.close()
    now = et_now()
    for (code,) in rows:
        b = feed.bars(code, now)
        a = c2.daily_atr(code, str(day))
        if b is None or not a:
            continue
        st = c2.scan(b, a, cfg=cfg, prev_close=c2.prev_close(code, str(day)))
        if st["phase"] != "入场":
            continue
        r = c2.outcome(b, st)
        if r and r.get("R") is not None:
            con = store.connect()
            try:
                with con:
                    con.execute("UPDATE c2_live SET r_result = ?, hit = ? WHERE date = ? AND code = ? AND phase = '入场'",
                                (round(r["R"], 3), r["hit"], str(day), code))
            finally:
                con.close()
            log(f"{code} 结果：{r['hit']} {r['R']:+.2f}R")


def refresh_backtest(cfg):
    """用历史 1 分钟K重算一次这个打法的回测（每天收盘后），给控制台和 AI 引用。"""
    try:
        import history
        df = c2.backtest(None, cfg=cfg)                 # 历史库里有 1 分钟K的全部股票
        by = {}
        for code, g in df.groupby("code") if len(df) else []:
            lo, sh = g[g.dir == "多"], g[g.dir == "空"]
            by[code] = dict(n=len(g), hit=round((g.hit == "目标").mean() * 100), avg=round(float(g.R.mean()), 3),
                            n_long=len(lo), avg_long=round(float(lo.R.mean()), 3) if len(lo) else 0.0,
                            n_short=len(sh), avg_short=round(float(sh.R.mean()), 3) if len(sh) else 0.0)
        (DATA / ".c2_by_code.json").write_text(json.dumps(by, ensure_ascii=False), encoding="utf-8")
        BT_CACHE.write_text(json.dumps(dict(text=c2.summary(df), at=dt.date.today().isoformat()), ensure_ascii=False),
                            encoding="utf-8")
        log("回测已更新：" + c2.summary(df))
    except Exception:
        log("回测出错：" + traceback.format_exc().splitlines()[-1])


def main():
    PID_FILE.write_text(str(os.getpid()))

    def bye(*_):
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    log("C2 监测启动")
    done_eval = None
    feed = None
    while True:
        cfg = cfg_all()
        if wset(cfg).get("模式") == "关":
            write_status(et_now().date(), "已关闭")
            time.sleep(30)
            continue
        n = et_now()
        m = n.hour * 60 + n.minute
        if n.weekday() >= 5:
            write_status(n.date(), "周末休市")
            time.sleep(300)
            continue
        if 571 <= m <= 662:                                   # 9:31–11:02
            try:
                codes = watchlist(cfg)
                if feed is None or not feed.ok:
                    feed = Feed(cfg, codes)
                    if not feed.ok:
                        write_status(n.date(), "OpenD 没连上")
                        feed = None
                        time.sleep(20)
                        continue
                    STATE.clear()
                atr = {c: c2.daily_atr(c, str(n.date())) for c in codes}
                pcs = {c: c2.prev_close(c, str(n.date())) for c in codes}
                if not any(atr.values()):
                    write_status(n.date(), "历史日线不够，算不出 ATR")
                while 571 <= m <= 662:
                    scan_once(cfg, feed, codes, n, atr, pcs)
                    time.sleep(max(1.0, 62 - et_now().second))   # 下一分钟的第 2 秒（等这一根确实收盘）
                    n = et_now()
                    m = n.hour * 60 + n.minute
                    cfg = cfg_all()
                    if wset(cfg).get("模式") == "关":
                        break
            except Exception:
                log("出错：" + traceback.format_exc().splitlines()[-1])
                time.sleep(20)
            continue
        if m >= 965 and done_eval != n.date():                # 16:05 之后：算结果、更新回测
            try:
                if feed is None or not feed.ok:
                    feed = Feed(cfg, watchlist(cfg))
                if feed.ok:
                    evaluate_today(cfg, feed, n.date())
                    try:
                        import patterns
                        k = patterns.evaluate_day(n.date(), lambda c: feed.bars(c, et_now()),
                                                  lambda c: c2.daily_atr(c, str(n.date())))
                        log(f"形态记录已评估 {k} 条")
                    except Exception:
                        log("形态评估出错：" + traceback.format_exc().splitlines()[-1])
                refresh_backtest(cfg)
                try:
                    import patterns
                    patterns.build_stats(cfg=cfg, log=lambda *a: None)
                except Exception:
                    log("形态概率更新出错：" + traceback.format_exc().splitlines()[-1])
                try:
                    import ladder
                    ladder.build_stats(log=None)
                except Exception:
                    log("六行看盘统计更新出错：" + traceback.format_exc().splitlines()[-1])
            except Exception:
                log("收盘后处理出错：" + traceback.format_exc().splitlines()[-1])
            done_eval = n.date()
            if feed is not None:
                feed.close()
                feed = None
        note = "等开盘（9:31 开始）" if m < 571 else ("今天早盘已结束" if m > 662 else "")
        write_status(n.date(), note)
        time.sleep(20)


if __name__ == "__main__":
    main()
