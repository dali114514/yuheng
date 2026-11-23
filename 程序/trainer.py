# -*- coding: utf-8 -*-
"""
盘感训练：从历史 K 线库抽一段行情，遮住后面的K线，你先判断（做多 / 做空 / 不做 + 止损），再揭晓结果

  · 一半以上的题目抽自规则引擎在历史上真正出过信号的位置（形态统计的 pattern_samples），其余随机抽
  · 股票名和日期在揭晓前隐藏；价格换算成「最后一根收盘 = 100」，看不出是哪只票
  · 结果用和信号结果一样的规则判断：先到目标（默认 2 倍风险）还是先到止损
  · 每道题存进 drills 表；统计页告诉你：整体准确率、你和规则引擎意见不同时谁对得多
  · 题目只出在 Kill Zone 里、只用大盘股（设置.json → 盘感训练：Kill_Zone、大盘股名单，可以自己改）
      整体判断：判断时间点落在 Kill Zone 内；逐根入场：K 线照常一根根翻，但只有最新一根在 Kill Zone 里才能入场
  · 逐根入场（练入场点）：只给前一天的K线，当天从开盘起一根一根往后翻（「下一根」），
    你觉得是入场点了就选做多 / 做空、点止损目标、确认入场（按最新一根收盘价成交），之后再一根根看这笔单怎么走。
    揭晓时告诉你：结果几 R、等了几根、入场后最大逆向几 R、当天最理想的入场点在哪、比你早 / 晚几根
控制台右上角「盘感训练」打开，浏览器里做题。
"""
import datetime as dt
import json
import random
import re
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import c2         # noqa: E402
import history    # noqa: E402
import outcomes   # noqa: E402
import store      # noqa: E402

PORT = 8765
VERSION = int(Path(__file__).stat().st_mtime)          # 控制台用它判断正在跑的是不是最新版
SCHEMA = """CREATE TABLE IF NOT EXISTS drills (
    drill_id TEXT PRIMARY KEY, ts TEXT, source TEXT, code TEXT, date TEXT, start TEXT, call TEXT, stop REAL, target REAL,
    hit TEXT, mfe_r REAL, mae_r REAL, close_r REAL, rule_play TEXT, rule_dir TEXT, rule_hit TEXT, correct INTEGER)"""
PENDING = {}
DEFAULT_SET = {
    "Kill_Zone": ["09:30-11:00", "13:30-16:00"],          # 美东时间；纽约早盘 / 午后两段
    "大盘股名单": ["AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO", "TSLA", "AMD", "NFLX",
                 "MU", "ARM", "INTC", "KO", "QQQ", "SPY"],
}


def tset():
    s = dict(DEFAULT_SET)
    try:
        s.update((json.loads((HERE.parent / "设置.json").read_text(encoding="utf-8")).get("盘感训练") or {}))
    except Exception:
        pass
    zones = []
    for z in s.get("Kill_Zone") or []:
        try:
            a, b = [int(x[:2]) * 60 + int(x[3:5]) for x in str(z).replace("–", "-").split("-")]
            zones.append((a, b))
        except Exception:
            pass
    codes = {str(c).strip().upper() for c in (s.get("大盘股名单") or []) if str(c).strip()}
    return zones or [(570, 660), (810, 960)], codes


def _zone_minutes(zones):
    """可以当判断时间点的分钟：在 Kill Zone 内、当天至少看过 3 根、之后还有 1 小时可评估。"""
    mins = [m for a, b in zones for m in range(max(a + 15, 585), min(b, 900) + 1, 5)]
    return mins or list(range(600, 871, 5))


def in_zone(ts, zones):
    """K线结束时间 ts 是否在 Kill Zone 里（9:35 结束的那根 = 9:30–9:35，算在 09:30 开始的区间里）。"""
    m = ts.hour * 60 + ts.minute
    return any(a < m <= b for a, b in zones)
EXTRA_COLS = {"mode": "TEXT", "wait_bars": "INTEGER", "entry_px": "REAL", "r_result": "REAL"}


def _ensure(con):
    con.execute(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(drills)")}
    for c, t in EXTRA_COLS.items():
        if c not in have:
            con.execute(f"ALTER TABLE drills ADD COLUMN {c} {t}")


def _days(code):
    con = history.connect()
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT substr(ts, 1, 10) FROM bars_5m WHERE code = ? ORDER BY 1", (code,))]
    finally:
        con.close()


def _session_bars(code, day):
    b = history.load(code, "5m", f"{day} 09:30:01", f"{day} 16:00:00")
    return b


def _pick_signal():
    """规则引擎出过信号、而且有 5 分钟K的位置。"""
    try:
        con = store.connect()
        try:
            rows = con.execute("SELECT code, play, direction, date, start, hit FROM pattern_samples "
                               "WHERE freq = '5m' AND triggered = 1").fetchall()
            zones, big = tset()
            rows = [r for r in rows if (not big or r[0] in big) and
                    (pd.Timestamp(r[4]).hour >= 16 or in_zone(pd.Timestamp(r[4]), zones))]   # 收盘后的信号 = 第二天开盘判断
        finally:
            con.close()
    except Exception:
        rows = []
    if not rows:
        return None
    code, play, d, date, start, hit = random.choice(rows)
    days = _days(code)
    if date not in days:
        return None
    i = days.index(date)
    st = pd.Timestamp(start)
    if st.hour >= 16:                         # 日线打法：当天收盘后出信号，看第二天
        if i + 1 >= len(days):
            return None
        show_days, rev_day, cut = days[max(0, i - 1):i + 1], days[i + 1], None
    else:                                     # 消息爆发：开盘后某个时间点
        show_days, rev_day, cut = days[max(0, i - 1):i + 1], date, st
    return dict(source="信号位置", code=code, show_days=show_days, rev_day=rev_day, cut=cut,
                rule=dict(play=play, dir=d, hit=hit))


def _pick_random():
    con = history.connect()
    try:
        codes = [r[0] for r in con.execute("SELECT code FROM bars_5m GROUP BY code HAVING COUNT(*) > 2000")]
        zones, big = tset()
        codes = [c for c in codes if not big or c in big]
    finally:
        con.close()
    if not codes:
        return None
    code = random.choice(codes)
    days = _days(code)
    if len(days) < 3:
        return None
    i = random.randint(1, len(days) - 1)
    cut = pd.Timestamp(days[i]) + pd.Timedelta(minutes=random.choice(_zone_minutes(zones)))
    return dict(source="随机", code=code, show_days=days[i - 1:i + 1], rev_day=days[i], cut=cut, rule=None)


C2_ZONE = [(575, 660)]          # C2 专项：第 6 根收盘后（9:36）到 11:00 可以入场
C2_END = 750                    # 显示和评估到 12:30


def _days_1m(code):
    con = history.connect()
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT substr(ts, 1, 10) FROM bars_1m WHERE code = ? ORDER BY 1", (code,))]
    finally:
        con.close()


def new_c2():
    """C2 专项：1 分钟图，只给开盘后 6 根（到信号K），之后一根根翻。"""
    _, big = tset()
    con = history.connect()
    try:
        codes = [r[0] for r in con.execute("SELECT code FROM bars_1m GROUP BY code HAVING COUNT(*) > 3000")]
    finally:
        con.close()
    codes = [c for c in codes if not big or c in big]
    for _ in range(30):
        if not codes:
            break
        code = random.choice(codes)
        days = _days_1m(code)
        if not days:
            continue
        day = random.choice(days)
        b = history.load(code, "1m", f"{day} 09:30:01", f"{day} 16:00:00")
        if len(b) < 200:
            continue
        atr = c2.daily_atr(code, day)
        if not atr:
            continue
        b = b[(b.index.hour * 60 + b.index.minute) <= C2_END]
        show, rev = _add_vwap(b.iloc[:c2.SIG_IDX + 1], b.iloc[c2.SIG_IDX + 1:])
        k = 100.0 / float(show["close"].iloc[-1])
        did = uuid.uuid4().hex[:12]
        rule = c2.scan(b, atr, prev_close=c2.prev_close(code, day))
        rule_r = c2.outcome(b, rule, tend="12:30") if rule["phase"] == "入场" else None
        PENDING[did] = dict(source="C2专项", code=code, rev_day=day, show=show, rev=rev, k=k, start=str(show.index[-1]),
                            mode="step", c2=True, zones=C2_ZONE, pos=0, atr=atr, rule=None, c2rule=rule, c2r=rule_r)
        bars = _pack(show, k, with_day=True)
        for x in bars:
            x["d"] = 0
        return dict(id=did, bars=bars, n_reveal=len(rev), atr=round(float(atr * k), 3), mode="step", c2=True,
                    zones=[list(z) for z in C2_ZONE])
    return dict(error="1 分钟K还不够：先在 Discord 发「回填历史」把大盘股的 1 分钟K补齐。")


def new_drill(mode="judge"):
    if mode == "c2":
        return new_c2()
    for _ in range(20):
        q = _pick_signal() if random.random() < 0.6 else None
        q = q or _pick_random()
        if q is None:
            continue
        if mode == "step":                    # 逐根：前一天全部 + 当天从开盘第一根起翻
            i = q["show_days"].index(q["rev_day"]) if q["rev_day"] in q["show_days"] else len(q["show_days"])
            q["show_days"] = q["show_days"][:i][-1:]
            q["cut"] = None
            if not q["show_days"]:
                continue
        elif q["cut"] is None:                # 收盘后的日线信号：改成第二天 Kill Zone 里某个时间点判断
            q["show_days"] = q["show_days"][-1:] + [q["rev_day"]]
            q["cut"] = pd.Timestamp(q["rev_day"]) + pd.Timedelta(minutes=random.choice(_zone_minutes(tset()[0])))
        show = pd.concat([_session_bars(q["code"], d) for d in q["show_days"]])
        rev = _session_bars(q["code"], q["rev_day"])
        if mode == "step":
            if len(rev) < 30 or len(show) < 30:
                continue
            show = pd.concat([show, rev.iloc[:1]])          # 当天开盘第一根先给你看
            rev = rev.iloc[1:]
        if q["cut"] is not None:
            show = show[show.index <= q["cut"]]
            rev = rev[rev.index > q["cut"]]
        if len(show) < 40 or len(rev) < 12:
            continue
        show, rev = _add_vwap(show, rev)
        show = show.tail(160)
        base = float(show["close"].iloc[-1])
        k = 100.0 / base
        tr = (show["high"] - show["low"]).tail(30).mean() * k
        did = uuid.uuid4().hex[:12]
        PENDING[did] = dict(q, show=show, rev=rev, k=k, start=str(show.index[-1]), mode=mode, pos=0)

        bars = _pack(show, k, with_day=True)
        dmap = {d: n for n, d in enumerate(sorted({b["d"] for b in bars}))}
        for b in bars:
            b["d"] = dmap[b["d"]]                 # 日期换成「第几天」，揭晓前不显示真实日期
        zones, _ = tset()
        return dict(id=did, bars=bars, n_reveal=len(rev), atr=round(float(tr), 3), mode=mode,
                    zones=[[a, b] for a, b in zones])
    return dict(error="历史库里还没有足够的 5 分钟K。请先在收盘后运行「回填历史K线」。")


def _add_vwap(show, rev):
    """每天从开盘重新累计的 VWAP（典型价 ×成交量）。整段一起算，只用到每根K线自己和它之前的数据。"""
    allb = pd.concat([show, rev])
    allb = allb[~allb.index.duplicated(keep="first")].sort_index()
    tp = (allb["high"] + allb["low"] + allb["close"]) / 3
    v = allb["volume"].fillna(0).clip(lower=0)
    day = allb.index.normalize()
    cv = v.groupby(day).cumsum()
    vw = (tp * v).groupby(day).cumsum() / cv.where(cv > 0)
    vw = vw.fillna(tp)                                   # 开头几根没成交量时用典型价
    show, rev = show.copy(), rev.copy()
    show["vwap"] = vw.reindex(show.index).values
    rev["vwap"] = vw.reindex(rev.index).values
    return show, rev


def _pack(df, k, with_day=False):
    out = []
    for ix, r in df.iterrows():
        b = dict(t=f"{ix:%H:%M}", o=round(r.open * k, 3), h=round(r.high * k, 3), l=round(r.low * k, 3),
                 c=round(r.close * k, 3))
        if "vwap" in df.columns and r["vwap"] == r["vwap"]:
            b["vw"] = round(float(r["vwap"]) * k, 3)
        if with_day:
            b["d"] = str(ix.date())
        out.append(b)
    return out


def step(did):
    """逐根模式：再翻一根。翻到收盘还没入场 = 这题不做。"""
    q = PENDING.get(did)
    if q is None or q.get("mode") != "step":
        return dict(error="题目已过期，请点「下一题」")
    if q["pos"] >= len(q["rev"]):
        return dict(end=True)
    nxt = q["rev"].index[q["pos"]]
    if nxt.hour * 60 + nxt.minute > max(b for a, b in (q.get("zones") or tset()[0])):
        return dict(end=True)                             # 最后一个 Kill Zone 已经结束
    bar = _pack(q["rev"].iloc[q["pos"]:q["pos"] + 1], q["k"])[0]
    q["pos"] += 1
    return dict(bar=bar, pos=q["pos"], left=len(q["rev"]) - q["pos"])


def _ideal(rev_all, k, d):
    """当天最理想的一笔（事后看）：做多 = 某根的最低点进、之后某根的最高点出，差价最大的那一对；做空反过来。"""
    hi, lo = rev_all["high"].values, rev_all["low"].values
    best, bj, bm, j0 = 0.0, 0, 0, 0
    for m in range(1, len(hi)):
        if d == 1:
            if lo[m - 1] < lo[j0]:
                j0 = m - 1
            gain = hi[m] - lo[j0]
        else:
            if hi[m - 1] > hi[j0]:
                j0 = m - 1
            gain = hi[j0] - lo[m]
        if gain > best:
            best, bj, bm = gain, j0, m
    px = (lo[bj] if d == 1 else hi[bj]) * k
    ext = (hi[bm] if d == 1 else lo[bm]) * k
    return dict(idx=bj, t=f"{rev_all.index[bj]:%H:%M}", px=round(float(px), 3), ext=round(float(ext), 3),
                ext_t=f"{rev_all.index[bm]:%H:%M}")


def answer_step(did, call, stop=None, target=None):
    q = PENDING.pop(did, None)
    if q is None or q.get("mode") != "step":
        return dict(error="题目已过期，请点「下一题」")
    k, pos = q["k"], q["pos"]
    seen = pd.concat([q["show"].iloc[-1:], q["rev"].iloc[:pos]])     # 开盘第一根 + 你翻过的
    if call in ("多", "空") and not in_zone(seen.index[-1], q.get("zones") or tset()[0]):
        PENDING[did] = q
        return dict(error="现在不在可入场的时段里，继续往后翻" if q.get("c2") else "现在不在 Kill Zone 里，不能入场，继续往后翻")
    entry = float(seen["close"].iloc[-1] * k)
    after = q["rev"].iloc[pos:]
    tr = float((q["show"]["high"] - q["show"]["low"]).tail(30).mean() * k)
    if q.get("c2"):
        tr = float(q["atr"] * k * 0.1)                   # 1 分钟图：默认止损 = 0.15 倍日线 ATR
    res, hit, correct, r_res = None, None, None, None
    if call in ("多", "空") and len(after):
        sgn = 1 if call == "多" else -1
        if stop is None or (stop - entry) * sgn >= 0:
            stop = entry - sgn * 1.5 * tr
        risk = abs(entry - stop)
        if target is None or (target - entry) * sgn <= 0:
            target = entry + sgn * 2 * risk
        elif abs(target - entry) < risk:
            target = entry + sgn * risk
        rv = after.copy()
        for col in ("open", "high", "low", "close"):
            rv[col] = rv[col] * k
        res = outcomes.evaluate_one(dict(direction=call, entry=entry, stop=stop, target1=target, status_first="",
                                         start=str(rv.index[0] - pd.Timedelta(minutes=1))), rv)
        hit = res["hit"] if res else None
        correct = 1 if hit == "目标" else 0 if hit == "失效" else None
        # 按入场价算 R（evaluate_one 用下一根开盘当参考价，这里统一用你的成交价）
        cl = float(rv["close"].iloc[-1])
        r_res = (abs(target - entry) / risk if hit == "目标" else -1.0 if hit == "失效" else sgn * (cl - entry) / risk)
        held = rv[rv.index <= pd.Timestamp(res["hit_ts"])] if res and res.get("hit_ts") else rv   # 只算持仓期间
        res["mae_r"] = float(max(0.0, (entry - held["low"].min()) if sgn == 1 else (held["high"].max() - entry)) / risk)
        if hit == "失效":
            res["mae_r"] = min(res["mae_r"], 1.0)               # 止损成交在止损价，不算K线穿过去的部分
        res["mfe_r"] = float(max(0.0, (held["high"].max() - entry) if sgn == 1 else (entry - held["low"].min())) / risk)
    elif call in ("多", "空"):
        call = "不做"                                    # 翻到收盘才点：没有后面的K线可评估
    rev_all = pd.concat([q["show"].iloc[-1:], q["rev"]])
    ideal = {c: _ideal(rev_all, k, 1 if c == "多" else -1) for c in ("多", "空")}
    rule = q["rule"]
    row = dict(drill_id=did, ts=dt.datetime.now().isoformat(timespec="seconds"), source=q["source"], code=q["code"],
               date=q["rev_day"], start=str(seen.index[-1]), call=call, stop=stop, target=target, hit=hit,
               mfe_r=(res or {}).get("mfe_r"), mae_r=(res or {}).get("mae_r"), close_r=None,
               rule_play=rule["play"] if rule else None, rule_dir=rule["dir"] if rule else None,
               rule_hit=rule["hit"] if rule else None, correct=correct,
               mode="c2" if q.get("c2") else "step", wait_bars=pos, entry_px=entry, r_result=r_res)
    if q.get("c2"):
        cr, rr_ = q["c2rule"], q.get("c2r")
        row.update(rule_play="C2规则", rule_dir=cr.get("dir") if cr["phase"] == "入场" else None,
                   rule_hit=(f"{rr_['R']:+.2f}R" if rr_ and rr_.get("R") is not None else cr["phase"]))
    con = store.connect()
    try:
        _ensure(con)
        with con:
            cols = list(row)
            con.execute(f"INSERT OR REPLACE INTO drills ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [row[c] for c in cols])
    finally:
        con.close()
    if q.get("c2"):
        cr, rr_ = q["c2rule"], q.get("c2r")
        if cr["phase"] == "入场" and rr_ and rr_.get("R") is not None:
            rtxt = (f"程序（C2 规则）：{cr['trig_t']} 在 VWAP 附近确认后{'做多' if cr['dir'] == '多' else '做空'}，"
                    f"下一根开盘入场，结果 {rr_['hit']} {rr_['R']:+.2f}R")
        else:                                            # 去掉真实价格（图上是换算后的价格）
            rtxt = "程序（C2 规则）今天不做：" + re.sub(r"\s*\d+\.\d+", "", "；".join(cr["reasons"]))
        rule = dict(dir=cr["dir"]) if cr["phase"] == "入场" else None
    elif rule:
        rtxt = f"规则引擎{'当天开盘后' if rule['play'] == '消息爆发' else '前一天收盘后'}的信号：{rule['play']}{'做多' if rule['dir'] == '多' else '做空'}，结果 {rule['hit']}"
    else:
        rtxt = "这天规则引擎没有信号（随机抽的）"
    return dict(bars=_pack(after, k), code=q["code"], date=q["rev_day"], call=call, entry=round(entry, 3), wait=pos,
                entry_t=f"{seen.index[-1]:%H:%M}", hit=hit, correct=correct, stop=stop, target=target, r=r_res,
                rr=abs(target - entry) / abs(entry - stop) if call in ("多", "空") else None,
                mae_r=(res or {}).get("mae_r"), mfe_r=(res or {}).get("mfe_r"),
                hit_t=(res or {}).get("hit_ts", "")[11:16] if res and res.get("hit_ts") else None,
                ideal=ideal, rule=rtxt, rule_dir=rule["dir"] if rule else None, stats=stats(),
                c2_rule=(dict(t=q["c2rule"]["trig_t"], dir=q["c2rule"]["dir"], entry=round(q["c2rule"]["entry"] * k, 3))
                         if q.get("c2") and q["c2rule"]["phase"] == "入场" else None))


def answer(did, call, stop=None, target=None):
    q = PENDING.pop(did, None)
    if q is None:
        return dict(error="题目已过期，请点「下一题」")
    k, rev = q["k"], q["rev"]
    entry = 100.0
    res, hit, correct = None, None, None
    tr = float((q["show"]["high"] - q["show"]["low"]).tail(30).mean() * k)
    if call in ("多", "空"):
        sgn = 1 if call == "多" else -1
        if stop is None or (stop - entry) * sgn >= 0:
            stop = entry - sgn * 1.5 * tr                  # 没点止损（或点在错的一边）：默认 1.5 倍平均K线幅度
        risk = abs(entry - stop)
        if target is None or (target - entry) * sgn <= 0:
            target = entry + sgn * 2 * risk                # 默认 2 倍风险
        elif abs(target - entry) < risk:
            target = entry + sgn * risk                    # 目标至少 1 倍风险，避免把目标点得很近刷准确率
        rv = rev.copy()
        for col in ("open", "high", "low", "close"):
            rv[col] = rv[col] * k
        res = outcomes.evaluate_one(dict(direction=call, entry=entry, stop=stop, target1=target, status_first="",
                                         start=str(rv.index[0] - pd.Timedelta(minutes=1))), rv)
        hit = res["hit"] if res else None
        correct = 1 if hit == "目标" else 0 if hit == "失效" else None
    up = float(rev["high"].max() * k - entry)
    dn = float(entry - rev["low"].min() * k)
    close_chg = float(rev["close"].iloc[-1] * k - entry)
    rule = q["rule"]
    row = dict(drill_id=did, ts=dt.datetime.now().isoformat(timespec="seconds"), source=q["source"], code=q["code"],
               date=q["rev_day"], start=q["start"], call=call, stop=stop, target=target, hit=hit,
               mfe_r=(res or {}).get("mfe_r"), mae_r=(res or {}).get("mae_r"),
               close_r=(close_chg * (1 if call == "多" else -1) / abs(entry - stop)) if call in ("多", "空") else None,
               rule_play=rule["play"] if rule else None, rule_dir=rule["dir"] if rule else None,
               rule_hit=rule["hit"] if rule else None, correct=correct)
    row["mode"] = "judge"
    con = store.connect()
    try:
        _ensure(con)
        with con:
            cols = list(row)
            con.execute(f"INSERT OR REPLACE INTO drills ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [row[c] for c in cols])
    finally:
        con.close()
    bars = _pack(rev, k)
    if rule:
        rtxt = f"规则引擎在这里：{rule['play']}{'做多' if rule['dir'] == '多' else '做空'}，结果 {rule['hit']}"
    else:
        rtxt = "规则引擎在这里没有信号"
    rr = abs(target - entry) / abs(entry - stop) if call in ("多", "空") else None
    return dict(bars=bars, code=q["code"], date=q["rev_day"], hit=hit, correct=correct, stop=stop, target=target, rr=rr,
                up=round(up, 2), dn=round(dn, 2), close_chg=round(close_chg, 2), rule=rtxt,
                rule_dir=rule["dir"] if rule else None, stats=stats())


def stats():
    con = store.connect()
    try:
        _ensure(con)
        rows = con.execute("SELECT call, hit, rule_dir, rule_hit, source FROM drills WHERE COALESCE(mode, 'judge') = 'judge'").fetchall()
        st = con.execute("SELECT call, hit, wait_bars, r_result, mae_r FROM drills WHERE mode = 'step'").fetchall()
        sc = con.execute("SELECT call, hit, wait_bars, r_result, mae_r, rule_dir, rule_hit FROM drills WHERE mode = 'c2'").fetchall()
    finally:
        con.close()
    def rate(rs):
        w = sum(1 for r in rs if r[1] == "目标")
        l_ = sum(1 for r in rs if r[1] == "失效")
        return dict(n=len(rs), w=w, l=l_, rate=round(w / (w + l_) * 100) if w + l_ else None)
    dirs = [r for r in rows if r[0] in ("多", "空")]
    agree = [r for r in dirs if r[2] and r[2] == r[0]]
    oppose = [r for r in dirs if r[2] and r[2] != r[0]]
    skipped_sig = [r for r in rows if r[0] == "不做" and r[2]]
    tr = [r for r in st if r[0] in ("多", "空") and r[3] is not None]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    step_s = dict(n=len(st), trades=len(tr), skip=len(st) - len(tr),
                  win=sum(1 for r in tr if r[1] == "目标"), loss=sum(1 for r in tr if r[1] == "失效"),
                  avg_r=avg([r[3] for r in tr]), sum_r=round(sum(r[3] for r in tr), 1) if tr else None,
                  avg_wait=avg([r[2] for r in tr]), avg_mae=avg([r[4] for r in tr if r[4] is not None]),
                  early=avg([r[3] for r in tr if (r[2] or 0) <= 6]), late=avg([r[3] for r in tr if (r[2] or 0) > 6]))
    ct = [r for r in sc if r[0] in ("多", "空") and r[3] is not None]
    rule_rs = []
    for r in sc:
        try:
            rule_rs.append(float(str(r[6]).rstrip("R")))
        except Exception:
            pass
    same = [r for r in ct if r[5] and r[5] == r[0]]
    c2_s = dict(n=len(sc), trades=len(ct), skip=len(sc) - len(ct), win=sum(1 for r in ct if r[1] == "目标"),
                loss=sum(1 for r in ct if r[1] == "失效"), avg_r=avg([r[3] for r in ct]),
                sum_r=round(sum(r[3] for r in ct), 1) if ct else None, avg_wait=avg([r[2] for r in ct]),
                avg_mae=avg([r[4] for r in ct if r[4] is not None]),
                rule_n=len(rule_rs), rule_avg=avg(rule_rs), rule_sum=round(sum(rule_rs), 1) if rule_rs else None,
                same=len(same), same_avg=avg([r[3] for r in same]))
    return dict(step=step_s, c2=c2_s, total=len(rows), dirs=rate(dirs), agree=rate(agree), oppose=rate(oppose),
                long=rate([r for r in dirs if r[0] == "多"]), short=rate([r for r in dirs if r[0] == "空"]),
                skip=len([r for r in rows if r[0] == "不做"]),
                skip_sig=dict(n=len(skipped_sig), rule_win=sum(1 for r in skipped_sig if r[3] == "目标")))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                import version
                self._send(200, version.page(HERE / "trainer.html"), "text/html; charset=utf-8")
            elif self.path == "/yuheng.css":
                self._send(200, (HERE / "yuheng.css").read_bytes(), "text/css; charset=utf-8")
            elif self.path.startswith("/brand/"):       # 玉衡标志素材（只认 brand 目录下的文件名）
                name = self.path.removeprefix("/brand/").split("?")[0]
                f = HERE / "brand" / name
                if "/" in name or ".." in name or not f.is_file():
                    return self._send(404, {"error": "not found"})
                self._send(200, f.read_bytes(), "image/png" if name.endswith(".png") else "image/x-icon")
            elif self.path.startswith("/api/new"):
                self._send(200, new_drill("c2" if "mode=c2" in self.path else "step" if "mode=step" in self.path else "judge"))
            elif self.path.startswith("/api/step"):
                self._send(200, step(self.path.split("id=")[-1]))
            elif self.path.startswith("/api/version"):
                self._send(200, dict(version=VERSION))
            elif self.path.startswith("/api/stats"):
                self._send(200, stats())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            f = lambda x: float(x) if x not in (None, "") else None
            if self.path.startswith("/api/answer_step"):
                self._send(200, answer_step(d.get("id"), d.get("call"), f(d.get("stop")), f(d.get("target"))))
            elif self.path.startswith("/api/answer"):
                self._send(200, answer(d.get("id"), d.get("call"), f(d.get("stop")), f(d.get("target"))))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main(open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 50)
    print("  玉衡5.0 · 盘感训练")
    print(f"  浏览器打开 {url}")
    print("  关掉这个窗口即停止")
    print("=" * 50)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main("--no-browser" not in sys.argv)
