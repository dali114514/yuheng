# -*- coding: utf-8 -*-
"""
用长期 1 分钟K（2019 起）重新检验个人策略 B（VWAP 回踩，原打法 A）和个人策略 A（爆发，原打法 B） —— 样本外检验

  · 数据：history_long.db（2019-01 → 2025-11-11）+ history.db（2025-11-12 起），只要常规时段
  · 日线 ATR 用 1 分钟K合成的日线算，保证和 1 分钟是同一个复权口径（老数据是前复权价，所以全部用倍日线 ATR，不用美元）
  · C2 的过滤条件是 2026-11 看着 2025-11 → 2026-11 这一年挑的，所以 2025-11-11 以前的六年多是干净的样本外
  · 成本：每笔 0.01 倍日线 ATR，换算成 R 扣掉
  用法：arch -arm64 程序/.venv/bin/python 程序/longcheck.py   → 写 报告/长期验证_C2和打法B_<日期>.md
"""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import c2  # noqa: E402

LONG = HERE / "data" / "history_long.db"
SHORT = HERE / "data" / "history.db"
OOS_END = "2025-11-12"          # 这天以前 = 样本外
COST = 0.01


def bars(code):
    out = []
    for p in (LONG, SHORT):
        if not p.exists():
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            d = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE code = ? ORDER BY ts", con, params=[code])
        finally:
            con.close()
        if len(d):
            d.index = pd.to_datetime(d.pop("ts"))
            out.append(c2.session(d))
    if not out:
        return None
    b = pd.concat(out)
    return b[~b.index.duplicated(keep="last")].sort_index()


def daily_from(b):
    g = b.groupby(b.index.normalize())
    d = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(), "n": g.close.size()})
    pc = d.close.shift()
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean().shift()
    d["pc"], d["ph"] = pc, d.high.shift()
    return d


def codes_long():
    con = sqlite3.connect(f"file:{LONG}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT code FROM bars_1m GROUP BY code HAVING COUNT(DISTINCT substr(ts,1,10)) > 200")]
    finally:
        con.close()


def vwap(h, l, c, v):
    return np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)


def scan_b_day(o, h, l, c, v, t, a, ph, qa, min_dist_atr, s=c2.B_DEFAULT):
    """个人策略 A：爆发（和 c2.scan_b 同一套条件，只把「前高离现价 ≥ $1.5」换成倍 ATR）：返回第一根信号的位置和目标，或 None。"""
    vw = vwap(h, l, c, v)
    body = np.abs(c - o)
    rng = h - l
    for i in range(5, len(c) - 1):
        if t[i] > 660:
            return None
        avg = body[max(0, i - 20):i].mean()
        if not (c[i] > o[i] and body[i] >= s["实体_ATR"] * a and body[i] >= s["实体倍数"] * avg and rng[i] > 0
                and body[i] >= s["实体占比"] * rng[i]):
            continue
        if not (c[i] > vw[i] and (c[i - 1] < vw[i - 1] or o[i] < vw[i])):
            continue
        if qa is None or not qa[i]:
            continue
        above = [x for x in (h[:i].max(), ph) if x is not None and x == x and x > c[i]]
        if not above:
            continue
        tgt = min(above)
        if tgt - c[i] < min_dist_atr * a:
            continue
        return i, tgt
    return None


def trade(o, h, l, c, i, stop, tgt, d=1):
    """第 i 根收盘出信号，下一根开盘进；同一根碰到止损和目标算止损；16:00 按收盘。返回 (R, 风险 ATR 前的价差)。"""
    if i + 1 >= len(o):
        return None
    e = o[i + 1]
    risk = (e - stop) * d
    if risk <= 0:
        return None
    for k in range(i + 1, len(o)):
        if ((l[k] if d == 1 else h[k]) - stop) * d <= 0:
            return (-1.0 if (o[k] - stop) * d > 0 else d * (o[k] - e) / risk), risk
        if ((h[k] if d == 1 else l[k]) - tgt) * d >= 0:
            return d * (tgt - e) / risk, risk
    return d * (c[-1] - e) / risk, risk


def run(log=print):
    cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    s_f = c2.settings(cfg)                                          # 现在用的：加过滤
    s_u = dict(s_f, **{"C1最小长度_ATR": 0.0, "止损最小_ATR": 0.02, "跳空逆向不做_ATR": None})   # 不加过滤
    qb = bars("QQQ")
    qd = {}
    for day, g in qb.groupby(qb.index.normalize()):
        h, l, c, v = (g[x].values.astype(float) for x in ("high", "low", "close", "volume"))
        qd[day] = pd.Series((c > vwap(h, l, c, v)) & (c > float(g.open.iloc[0])), index=g.index)
    rows_a, rows_b = [], []
    for code in codes_long():
        b = bars(code)
        if b is None:
            continue
        D = daily_from(b)
        for day, g in b.groupby(b.index.normalize()):
            r = D.loc[day]
            a = r.atr
            if a != a or not a or r.n < 300:
                continue
            o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
            t = g.index.hour * 60 + g.index.minute
            ds = str(day.date())
            for name, s in (("不加过滤", s_u), ("加过滤", s_f)):
                sig = c2.scan(g, a, s=s, prev_close=r.pc)
                if sig["phase"] != "入场":
                    continue
                res = c2.outcome(g, sig)
                if not res or res.get("R") is None:
                    continue
                risk = abs(res["entry"] - sig["stop"]) / a
                rows_a.append(dict(code=code, date=ds, ver=name, dir=sig["dir"], R=res["R"], risk=risk,
                                   cost=COST / risk if risk > 0 else np.nan))
            if code in ("QQQ", "SPY"):
                continue
            q = qd.get(day)
            qa = q.reindex(g.index).fillna(False).values if q is not None else None
            for md in (0.0, 0.1, 0.2, 0.3):
                x = scan_b_day(o, h, l, c, v, t, a, r.ph, qa, md)
                if not x:
                    continue
                i, tgt = x
                for st in (0.1, 0.2):
                    tr = trade(o, h, l, c, i, o[i + 1] - st * a if i + 1 < len(o) else 0, tgt)
                    if tr:
                        R, risk = tr
                        rows_b.append(dict(code=code, date=ds, dist=md, stop=st, R=R, risk=risk / a,
                                           cost=COST / (risk / a), far=(tgt - c[i]) / a))
        log(f"  · {code}")
    A, B = pd.DataFrame(rows_a), pd.DataFrame(rows_b)
    A.to_pickle(HERE / "data" / ".longcheck_A.pkl")
    B.to_pickle(HERE / "data" / ".longcheck_B.pkl")
    return A, B


def line(g):
    if not len(g):
        return "0 笔"
    net = g.R - g.cost
    d = g.assign(net=net).groupby("date").net.mean()
    m, se = d.mean(), d.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else np.nan
    ym = g.assign(net=net).groupby(g.date.str[:7]).net.mean()
    return (f"{len(g):,} 笔 · 赚钱 {(g.R > 0).mean() * 100:.0f}% · 毛 {g.R.mean():+.3f}R · 净 **{net.mean():+.3f}R**"
            f"（按日 95%：{m - 1.96 * se:+.3f} ~ {m + 1.96 * se:+.3f}）· {(ym > 0).sum()}/{len(ym)} 个月为正")


def report(A, B):
    L = [f"# 长期验证：个人策略 B（VWAP 回踩）和个人策略 A（爆发）（{dt.date.today()}）", "",
         f"数据：{A.code.nunique() if len(A) else 0} 只股票 / ETF，2019-01-02 起的常规时段 1 分钟K。"
         f"**样本外 = {OOS_END} 以前**（这段数据从没参与挑参数）；样本内 = 之后这一年。成本每笔 0.01 倍日线 ATR，已换算成 R 扣掉。", ""]
    for ver in ("不加过滤", "加过滤"):
        g = A[A.ver == ver]
        L += [f"## 个人策略 B：VWAP 回踩（C2）· {ver}", "",
              f"- 样本外：{line(g[g.date < OOS_END])}",
              f"- 样本内：{line(g[g.date >= OOS_END])}", "", "| 年份 | 结果 |", "|---|---|"]
        for y, gy in g.groupby(g.date.str[:4]):
            L.append(f"| {y} | {line(gy)} |")
        o = g[g.date < OOS_END]
        L += ["", f"样本外分方向：多 {line(o[o.dir == '多'])}；空 {line(o[o.dir == '空'])}", ""]
    L += ["## 个人策略 A：爆发（大实体站上 VWAP 追多，QQQ 同向，目标前高）", "",
          "原来的条件「前高离现价 ≥ $1.5」在前复权老数据上没法用，换成倍日线 ATR 的几档一起列出（不挑最好的）；止损 0.1 / 0.2 倍 ATR。", "",
          "| 前高距离 ≥ | 止损 | 样本外 | 样本内 |", "|---|---|---|---|"]
    for (md, st), g in B.groupby(["dist", "stop"]):
        L.append(f"| {md:g} ATR | {st:g} ATR | {line(g[g.date < OOS_END])} | {line(g[g.date >= OOS_END])} |")
    L += ["", "说明：同一天的股票彼此相关，所以区间按「每天平均」算。样本外为正、区间不含 0、而且多数年份为正，才算站得住。"]
    p = BASE / "报告" / f"长期验证_C2和打法B_{dt.date.today()}.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


if __name__ == "__main__":
    A, B = run()
    print(report(A, B))
