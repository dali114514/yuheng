"""VicAlpha「择时交易策略」第 7 集：箱体底部低吸。

规则（按视频原意，默认参数，不在本数据上调）：
  中轨 = 20 日均线；下轨 = 中轨 × (1 - 3%)；上轨 = 中轨 × (1 + 3%)。
  空仓时，收盘价 ≤ 下轨 → 下一个交易日开盘买入（全仓）。
  持仓时，收盘价 ≥ 上轨 → 下一个交易日开盘卖出。
  没有止损，只做多。成本：每笔来回扣 0.02%（ETF 免佣，主要是滑点）。
数据是复权价（含分红），空仓时现金收益按 0 算（偏保守，对策略略不利）。

对照：
  buy_hold —— 同期买入持有。
  random   —— 随机打乱入场日、保持每笔持仓天数不变，重复 500 次，
              看策略收益排在随机里的第几分位（检验"择时"本身有没有贡献，而不只是在场时间）。
"""
from __future__ import annotations

import random
import statistics as st

COST = 0.0002


def sma(x, n):
    out, s = [], 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def run(rows, start="2010-01-01", end="9999", k=0.03, n=20, stop=None, rearm=True):
    """stop：收盘比买入价跌超这个比例 → 次日开盘止损（如 0.05）。
    rearm：止损后要先收回下轨上方，再次碰到下轨才重新买（避免止损第二天又原地买回）。"""
    d = [r["date"] for r in rows]
    o = [r["open"] for r in rows]
    c = [r["close"] for r in rows]
    m = sma(c, n)
    i0 = next(i for i, t in enumerate(d) if t >= start)
    i1 = max(i for i, t in enumerate(d) if t < end)
    trades, pos, armed = [], None, True
    for i in range(max(i0, n), i1):
        if m[i] is None:
            continue
        if not armed and c[i] > m[i] * (1 - k):
            armed = True
        if pos is None and armed and c[i] <= m[i] * (1 - k):
            pos = {"e": i + 1, "entry": o[i + 1], "low": o[i + 1]}
        elif pos is not None:
            pos["low"] = min(pos["low"], c[i])
            if c[i] >= m[i] * (1 + k):
                x = i + 1
                trades.append(_close(d, pos, x, o[x]))
                pos = None
            elif stop and i >= pos["e"] and c[i] <= pos["entry"] * (1 - stop):
                x = i + 1
                t = _close(d, pos, x, o[x])
                t["stopped"] = True
                trades.append(t)
                pos = None
                armed = not rearm
    if pos is not None:                       # 数据结束时仍持仓：按最后收盘计
        trades.append(_close(d, pos, i1, c[i1], open_=True))
    return trades, (i0, i1)


def _close(d, pos, x, px, open_=False):
    ret = px / pos["entry"] - 1 - COST
    return {"in": d[pos["e"]], "out": d[x], "e": pos["e"], "x": x, "ret": ret,
            "days": x - pos["e"], "mae": pos["low"] / pos["entry"] - 1, "open": open_}


def equity(trades):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= 1 + t["ret"]
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return eq, mdd


def daily_mdd(rows, trades, i0, i1):
    """按日盯市的最大回撤（持仓期间的浮亏也算）。"""
    c = [r["close"] for r in rows]
    o = [r["open"] for r in rows]
    inpos = {}
    for t in trades:
        for j in range(t["e"], t["x"]):
            inpos[j] = t
    eq, peak, mdd, prev = 1.0, 1.0, 0.0, None
    for j in range(i0, i1 + 1):
        t = inpos.get(j)
        if t:
            ref = o[j] if j == t["e"] else c[j - 1]
            eq *= c[j] / ref
        elif prev and prev["x"] == j:         # 卖出日：前一天收盘 → 当天开盘
            eq *= o[j] / c[j - 1]
        prev = inpos.get(j) or prev
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return mdd


def bh(rows, i0, i1):
    c = [r["close"] for r in rows]
    peak, mdd = c[i0], 0.0
    for j in range(i0, i1 + 1):
        peak = max(peak, c[j])
        mdd = min(mdd, c[j] / peak - 1)
    return c[i1] / c[i0], mdd


def random_pctile(rows, trades, i0, i1, draws=500, seed=7):
    """保持每笔持仓天数不变、随机放置入场日（不重叠），看策略在随机分布里的分位。"""
    if not trades:
        return None
    o = [r["open"] for r in rows]
    rng = random.Random(seed)
    lens = [max(1, t["days"]) for t in trades]
    real = equity(trades)[0]
    span = i1 - i0
    sims = []
    for _ in range(draws):
        free = span - sum(lens)
        cuts = sorted(rng.randint(0, free) for _ in lens)
        order = lens[:]
        rng.shuffle(order)
        eq, cur, last = 1.0, i0, 0
        for gap_end, L in zip(cuts, order):
            cur += gap_end - last
            last = gap_end
            e, x = cur + 1, min(cur + 1 + L, i1)
            eq *= o[x] / o[e] - COST + 0 if x > e else 1
            cur += L
        sims.append(eq)
    return sum(s < real for s in sims) / len(sims), st.median(sims)


def summarize(rows, trades, i0, i1):
    yrs = (i1 - i0) / 252
    eq, _ = equity(trades)
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    in_days = sum(t["days"] for t in trades)
    b, bmdd = bh(rows, i0, i1)
    return {
        "n": len(trades), "per_year": round(len(trades) / yrs, 1),
        "win": round(len(wins) / len(rets), 3) if rets else 0,
        "avg": round(st.mean(rets), 4) if rets else 0,
        "worst": round(min(rets), 4) if rets else 0,
        "worst_mae": round(min(t["mae"] for t in trades), 4) if trades else 0,
        "max_days": max((t["days"] for t in trades), default=0),
        "avg_days": round(st.mean(t["days"] for t in trades), 1) if trades else 0,
        "exposure": round(in_days / (i1 - i0), 3),
        "mult": round(eq, 3), "cagr": round(eq ** (1 / yrs) - 1, 4),
        "mdd": round(daily_mdd(rows, trades, i0, i1), 3),
        "bh_mult": round(b, 3), "bh_cagr": round(b ** (1 / yrs) - 1, 4), "bh_mdd": round(bmdd, 3),
        "years": round(yrs, 1),
    }
