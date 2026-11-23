# -*- coding: utf-8 -*-
"""
信号结果：收盘后用当天 1 分钟K（没有就用 5 分钟K）判断每条信号的结果，存进 signal_outcomes

  起点      第一次提醒的时间（没提醒过就用第一次出现的时间），之后的K线才算
  待扫      价格先要碰到关键位才算「已触发」，没碰到记「未触发」
  结果      先到第一目标 = 目标；先到失效位 = 失效；同一根K线里两个都碰到，保守记失效；到收盘都没碰到 = 都没到
  MFE/MAE   起点之后最大有利 / 不利波动，用「风险倍数 R」表示（R = |入场价 − 失效位|）
只算正股价格；期权的实际涨跌要等以后存了期权K线再加。被 AI 否决、没弹窗的信号也照样算，这样才能比较。
"""
import datetime as dt

import pandas as pd

import history
import store

SCHEMA = """CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id TEXT PRIMARY KEY, evaluated_ts TEXT, freq TEXT, start_ts TEXT, triggered INTEGER, trigger_ts TEXT,
    hit TEXT, hit_ts TEXT, minutes_to_hit REAL, ref_price REAL, close_price REAL, close_ret_pct REAL,
    mfe_r REAL, mae_r REAL, mfe_pct REAL, mae_pct REAL)"""


def connect(path=None):
    con = store.connect(path)
    con.execute(SCHEMA)
    return con


def evaluate_one(sig, bars, ambiguous="失效"):
    """sig: dict(direction, entry, stop, target1, status_first, start)；bars：当天常规时段K线（index=结束时间）。
    ambiguous：同一根K线里目标和失效位都碰到时记什么（分钟K默认保守记失效；用日线评估时记「不确定」）。"""
    d = 1 if sig["direction"] == "多" else -1
    entry, stop, tg = sig["entry"], sig["stop"], sig["target1"]
    b = bars[bars.index > pd.Timestamp(sig["start"])]
    if not len(b) or entry is None or stop is None or tg is None:
        return None
    out = dict(start_ts=str(sig["start"]), ref_price=float(b["open"].iloc[0]), close_price=float(b["close"].iloc[-1]),
               triggered=1, trigger_ts=None, hit="都没到", hit_ts=None, minutes_to_hit=None)
    if str(sig.get("status_first") or "").startswith("待扫"):
        touch = b[b["high"] >= entry] if d == -1 else b[b["low"] <= entry]
        if not len(touch):
            out.update(triggered=0, hit="未触发")
        else:
            out["trigger_ts"] = str(touch.index[0])
            b = b[b.index >= touch.index[0]]
            out["ref_price"] = entry
    risk = abs(entry - stop) or None
    if out["triggered"]:
        for ts, r in b.iterrows():
            up, dn = r["high"], r["low"]
            hit_stop = (dn <= stop) if d == 1 else (up >= stop)
            hit_tg = (up >= tg) if d == 1 else (dn <= tg)
            if hit_stop or hit_tg:
                out["hit"] = (ambiguous if hit_tg else "失效") if hit_stop else "目标"
                out["hit_ts"] = str(ts)
                out["minutes_to_hit"] = (ts - pd.Timestamp(out["trigger_ts"] or sig["start"])).total_seconds() / 60
                break
        ref = out["ref_price"]
        fav = (b["high"].max() - ref) if d == 1 else (ref - b["low"].min())
        adv = (ref - b["low"].min()) if d == 1 else (b["high"].max() - ref)
        out.update(mfe_pct=fav / ref * 100, mae_pct=adv / ref * 100,
                   mfe_r=fav / risk if risk else None, mae_r=adv / risk if risk else None)
    out["close_ret_pct"] = d * (out["close_price"] - out["ref_price"]) / out["ref_price"] * 100
    return out


def evaluate_day(day, path=None, hist_path=None):
    """评估某天全部有价位的信号。返回 {结果: 数量}。"""
    con = connect(path)
    counts = {}
    try:
        rows = con.execute("SELECT signal_id, code, direction, entry, stop, target1, status_first, alerted_ts, first_ts "
                           "FROM signals WHERE date = ? AND source = '规则'", (str(day),)).fetchall()
        cache = {}
        for sid, code, direction, entry, stop, tg, status, alerted, first in rows:
            if code not in cache:
                lo, hi = f"{day} 09:30:00", f"{day} 16:00:00"
                bars, freq = history.load(code, "1m", lo, hi, path=hist_path), "1m"
                if len(bars) < 300:
                    b5 = history.load(code, "5m", lo, hi, path=hist_path)
                    if len(b5) > len(bars) / 5:
                        bars, freq = b5, "5m"
                cache[code] = (bars, freq)
            bars, freq = cache[code]
            if not len(bars):
                counts["没有K线"] = counts.get("没有K线", 0) + 1
                continue
            res = evaluate_one(dict(direction=direction, entry=entry, stop=stop, target1=tg, status_first=status,
                                    start=alerted or first), bars)
            if res is None:
                continue
            res.update(signal_id=sid, evaluated_ts=dt.datetime.now().isoformat(timespec="seconds"), freq=freq)
            cols = list(res)
            with con:
                con.execute(f"INSERT OR REPLACE INTO signal_outcomes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [res[c] for c in cols])
            counts[res["hit"]] = counts.get(res["hit"], 0) + 1
    finally:
        con.close()
    return counts
