# -*- coding: utf-8 -*-
"""
玉衡5.0 · 日内策略库（1 分钟K + 期权换算）。日线版（stratlib.py）是给拿几天到几个月的人看的；这个才是你的打法：
9:36–11:00 开仓、最晚收盘平、做期权。

  数据：history_long.db（2019-01 起，常规时段）+ history.db（最近一年）的 1 分钟K，前复权价。
  每只股票每天、每条策略只取第一个信号；信号K收盘后，下一根开盘成交。

  日内「行情」（入场时已经知道的，不偷看后面）：
    - 开盘类型：跳空 =（开盘 − 昨收）÷ 前一天的日线 ATR14：平开（|跳空| < 0.25）、高开 / 低开（0.25–0.75）、大幅高开 / 大幅低开（≥ 0.75）
    - 日线行情：昨天收盘时的七种行情（和日线策略库同一套分类）

  策略（入场）：个人策略 B（VWAP 回踩，直接用 c2.scan）、个人策略 A（爆发：大实体站上 VWAP，条件同 c2.scan_b）、开盘区间突破 5 / 15 / 30 分钟、
               VWAP 收复、偏离 VWAP 回归、缺口回补、假突破收回、10 点趋势延续。每条都有自己的「结构止损」位置。
  止损 / 出场（同一个入场换 9 种）：权利金固定比例（今天的「每张亏 0.5」换算成百分比）、0.1 / 0.2 倍 ATR（目标 2R）、
               结构止损 + 2R、结构止损拿到收盘、VWAP 反穿、前 5 根低点移动止损、广撒网滚仓规则、11:00 时间止损。都最晚收盘平。

  期权换算（估算，没有历史期权报价）：平值、到期 3 天（你成交的中位数），波动率用这只股票前 20 天的已实现波动率 × 1.1；
    每根 K 线按 Black-Scholes 重新算期权价，所以 Delta / Gamma / 时间损耗都是随时变的（不是固定 0.5）；
    买卖各扣 1% 权利金的价差。结果用「权利金收益率」表示（前复权的老价格也能比），再按今天的期权价折成每张美元。

  检验：样本内 2019–2023，样本外 2024 起。输出到 程序/data/日内策略库/<代码>.json 和 index.json。只读 K 线库，不下单。

用法：arch -arm64 程序/.venv/bin/python 程序/intraday.py [代码 …]      （全部约几分钟，多进程）
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
DB = HERE / "data" / "history.db"
DB_LONG = HERE / "data" / "history_long.db"
OUT = HERE / "data" / "日内策略库"
SPLIT = "2024-01-01"
ENTRY_FROM, ENTRY_TO = 9 * 60 + 36, 11 * 60      # 信号K结束时间 ≥ 9:36；成交（下一根开盘）不晚于 11:00
DTE_DAYS = 3                                     # 期权到期天数（日历天）
HALF_SPREAD = 0.01                               # 买卖各扣权利金的 1%
MIN_DAYS = 60

GAPS = [("big_down", "大幅低开"), ("down", "低开"), ("flat", "平开"), ("up", "高开"), ("big_up", "大幅高开")]

STRATS = [
    dict(id="A", name="个人策略 B：VWAP 回踩", dir="多空",
         entry="第 6 根定方向（收在开盘价和 VWAP 同侧），回踩 VWAP / FVG 后出现确认组合，下一根进（c2.scan 原样）。", struct="C2 极值外"),
    dict(id="B", name="个人策略 A：爆发（大实体站上 VWAP）", dir="多",
         entry="大实体阳线从 VWAP 下方收上来（实体 ≥ 0.1 ATR、≥ 前 20 根平均 2.5 倍、占整根 ≥ 60%），QQQ 在开盘价和 VWAP 上方，上方前高离现价 ≥ 0.1 ATR。",
         struct="信号K低点"),
    dict(id="orb5", name="开盘区间突破（5 分钟）", dir="多空", entry="收盘突破前 5 根的最高 / 最低点。", struct="区间另一边"),
    dict(id="orb15", name="开盘区间突破（15 分钟）", dir="多空", entry="收盘突破前 15 根的最高 / 最低点。", struct="区间另一边"),
    dict(id="orb30", name="开盘区间突破（30 分钟）", dir="多空", entry="收盘突破前 30 根的最高 / 最低点。", struct="区间另一边"),
    dict(id="vwap_reclaim", name="VWAP 收复", dir="多空", entry="连续 10 根收在 VWAP 一侧后，第一次收到另一侧（顺着新的一侧做）。", struct="前 10 根极值"),
    dict(id="vwap_fade", name="偏离 VWAP 回归", dir="多空", entry="收盘离 VWAP ≥ 0.4 倍 ATR，反着做（赌回到 VWAP）。", struct="今天极值外 0.05 ATR"),
    dict(id="gap_fill", name="缺口回补", dir="多空", entry="跳空 ≥ 0.25 ATR、9:36 还没补，朝昨收方向做。", struct="今天极值外 0.05 ATR"),
    dict(id="fakeout", name="假突破收回（15 分钟区间）", dir="多空", entry="刺破开盘 15 分钟区间高 / 低点但收回区间内，反着做。", struct="刺破那根的极值"),
    dict(id="trend10", name="10 点趋势延续", dir="多空", entry="10:00 时离开盘价 ≥ 0.3 ATR，且收在今天区间的上 / 下 25%，顺势做。", struct="前 10 根极值"),
]
STRATS.append(dict(id="rand", name="对照：随机入场", dir="多空",
                   entry="9:36–11:00 之间随机挑一根、随机方向（固定随机种子）。用来检查模型本身偏不偏：它应该在 0 以下（扣了成本）。",
                   struct="0.3 倍 ATR"))
STOPS = [
    dict(id="prem", name="权利金固定比例", how="期权价跌到入场价 ×（1 − X）就走，X = 今天的「每张亏 0.5」÷ 今天平值 3 天期权价；目标 +2X。"),
    dict(id="atr10", name="0.1 倍 ATR", how="股价逆向 0.1 倍日线 ATR 止损，目标 2R。"),
    dict(id="atr20", name="0.2 倍 ATR", how="股价逆向 0.2 倍日线 ATR 止损，目标 2R。"),
    dict(id="struct2R", name="结构止损 + 2R", how="止损放在这条策略的结构位，目标 2R。"),
    dict(id="struct_close", name="结构止损，拿到收盘", how="只用结构止损，不设目标，收盘平。"),
    dict(id="vwap", name="VWAP 反穿", how="收盘回到 VWAP 另一侧就走（结构止损兜底），否则拿到收盘。"),
    dict(id="trail5", name="前 5 根移动止损", how="结构止损起步，之后跟着前 5 根最低（做空是最高）点只往有利方向挪。"),
    dict(id="planC", name="广撒网滚仓规则", how="结构止损；15 分钟还不到 +0.5R 就砍；到 +1R 挪保本，到 +2R 挪到 +1R；否则拿到收盘。"),
    dict(id="time11", name="11:00 时间止损", how="结构止损；11:00 还在就平。"),
]


# ================================================================ 期权（Black-Scholes，r = 0）

def _ncdf(x):
    from scipy.special import ndtr
    return ndtr(x)


def bs(S, K, T, iv, call):
    """S、T 可以是数组。返回 (价格, Delta)。"""
    S = np.asarray(S, float)
    T = np.maximum(np.asarray(T, float), 1e-6)
    sd = iv * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if call:
        return S * _ncdf(d1) - K * _ncdf(d2), _ncdf(d1)
    return K * _ncdf(-d2) - S * _ncdf(-d1), _ncdf(d1) - 1


YEAR_MIN = 252 * 390          # 「交易时间」时钟：一年 252 个交易日 × 390 分钟。期权的时间损耗和波动都按交易分钟算，
                              # 否则盘中只扣几小时日历时间的损耗、却拿到一整天的波动，会系统性高估拿得久的期权
DTE_TRADING = 2               # 到期 3 天（日历）≈ 今天剩下的时间 + 2 个完整交易日


# ================================================================ 数据

def load_1m(code):
    q = "SELECT ts, open, high, low, close, volume FROM bars_1m WHERE code=? AND substr(ts,12,5) BETWEEN '09:31' AND '16:00'"
    parts = []
    for path in (DB_LONG, DB):
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            parts.append(pd.read_sql_query(q, con, params=(code,)))
        finally:
            con.close()
    df = pd.concat(parts) if parts else pd.DataFrame()
    if df.empty:
        return df
    df = df.drop_duplicates("ts", keep="last").sort_values("ts")
    df.index = pd.to_datetime(df["ts"])
    df = df.drop(columns="ts").astype(float)
    df["volume"] = df["volume"].fillna(0)
    return df.dropna(subset=["open", "high", "low", "close"])


def daily_from_1m(df):
    g = df.groupby(df.index.date)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                      "n": g["close"].size()})
    d = d[d["n"] >= 300]                                    # 半天市等不完整的日子不算
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    rv = np.log(d["close"]).diff().rolling(20).std() * math.sqrt(252)
    d["atr_prev"], d["pc"], d["ph"], d["pl"] = atr.shift(1), pc, d["high"].shift(1), d["low"].shift(1)
    d["iv"] = (rv.shift(1) * 1.1).clip(0.12, 2.0)
    return d


def daily_regimes():
    """日线行情（stratlib.classify），按日期 → 昨天收盘时的行情 id。"""
    import stratlib
    vix = stratlib.load_vix(update=False, log=lambda *a: None)
    data = stratlib.load_bars()
    out = {}
    for code, rows in data.items():
        if len(rows) < 260:
            continue
        b = stratlib.Bars(rows, vix)
        reg = stratlib.classify(b)
        m = {}
        for i in range(1, b.n):
            if reg[i - 1]:
                m[b.d[i]] = reg[i - 1]["r"]
        out[code] = m
    return out


# ================================================================ 入场信号（每条策略当天第一个）

def _vwap(h, l, c, v):
    return np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)


def signals(day_df, row, qqq_day, c2s):
    """返回 [(策略id, 信号K下标 i, 方向 d, 结构止损价)]。成交在 i+1 根开盘。"""
    o, h, l, c, v = (day_df[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
    n = len(c)
    tmin = day_df.index.hour.values * 60 + day_df.index.minute.values
    atr = row["atr_prev"]
    vw = _vwap(h, l, c, v)
    ok = (tmin >= ENTRY_FROM) & (np.r_[tmin[1:], 10 ** 6] <= ENTRY_TO)   # 信号K ≥ 9:36，下一根开盘 ≤ 11:00
    idx = np.flatnonzero(ok)
    out = []
    if not len(idx):
        return out

    def first(mask):
        m = np.flatnonzero(mask & ok)
        return int(m[0]) if len(m) else None

    # A：c2.scan 原样（只看到 11:00 的K）
    try:
        import c2
        sig = c2.scan(day_df.iloc[:idx[-1] + 1], atr, s=c2s, prev_close=row["pc"])
        if sig.get("phase") == "入场" and sig.get("trig_idx") is not None:
            i = int(sig["trig_idx"])
            if ok[i]:
                out.append(("A", i, 1 if sig["dir"] == "多" else -1, float(sig["stop"])))
    except Exception:
        pass
    # B：只做多
    body = np.abs(c - o)
    avg20 = pd.Series(body).shift(1).rolling(20, min_periods=5).mean().values
    rng = h - l
    cond = ((c > o) & (body >= 0.1 * atr) & (body >= 2.5 * np.nan_to_num(avg20, nan=1e9)) & (rng > 0) & (body >= 0.6 * rng)
            & (c > vw) & ((np.r_[np.inf, c[:-1]] < np.r_[np.inf, vw[:-1]]) | (o < vw)))
    if qqq_day is not None and len(qqq_day):
        qo, qh, ql, qc, qv = (qqq_day[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        qvw = _vwap(qh, ql, qc, qv)
        qok = pd.Series((qc > qo[0]) & (qc > qvw), index=qqq_day.index).reindex(day_df.index).fillna(False).values.astype(bool)
        hh = np.maximum.accumulate(h)
        prior_high = np.r_[-np.inf, hh[:-1]]
        tgt = np.where(prior_high > c, prior_high, np.where(row["ph"] > c, row["ph"], np.nan))
        cond = cond & qok & ((tgt - c) >= 0.1 * atr)
        i = first(cond)
        if i is not None:
            out.append(("B", i, 1, float(l[i])))
    # 开盘区间突破
    for k, sid in ((5, "orb5"), (15, "orb15"), (30, "orb30")):
        if n <= k:
            continue
        hi, lo = h[:k].max(), l[:k].min()
        after = np.arange(n) >= k
        iu, idn = first(after & (c > hi)), first(after & (c < lo))
        cand = [(i, d) for i, d in ((iu, 1), (idn, -1)) if i is not None]
        if cand:
            i, d = min(cand)
            out.append((sid, i, d, float(lo if d == 1 else hi)))
    # VWAP 收复：前 10 根都在一侧，这一根收到另一侧
    side = np.sign(c - vw)
    cand = []
    for d in (1, -1):
        prev10 = pd.Series(side == -d).rolling(10).sum().shift(1).values == 10
        i = first(prev10 & (side == d))
        if i is not None:
            cand.append((i, d))
    if cand:
        i, d = min(cand)
        st = l[max(0, i - 10):i + 1].min() if d == 1 else h[max(0, i - 10):i + 1].max()
        out.append(("vwap_reclaim", i, d, float(st)))
    # 偏离 VWAP 回归（反着做）
    dev = (c - vw) / atr
    iu, idn = first(dev >= 0.4), first(dev <= -0.4)
    cand = [(i, d) for i, d in ((iu, -1), (idn, 1)) if i is not None]
    if cand:
        i, d = min(cand)
        st = (np.maximum.accumulate(h)[i] + 0.05 * atr) if d == -1 else (np.minimum.accumulate(l)[i] - 0.05 * atr)
        out.append(("vwap_fade", i, d, float(st)))
    # 缺口回补：只看 9:36 那一根
    gap = (o[0] - row["pc"]) / atr
    i0 = idx[0]
    if abs(gap) >= 0.25:
        d = -1 if gap > 0 else 1
        if (c[i0] - row["pc"]) * d < 0:            # 还没补
            st = (h[:i0 + 1].max() + 0.05 * atr) if d == -1 else (l[:i0 + 1].min() - 0.05 * atr)
            out.append(("gap_fill", i0, d, float(st)))
    # 假突破收回（15 分钟区间）
    if n > 15:
        hi, lo = h[:15].max(), l[:15].min()
        after = np.arange(n) >= 15
        closed_out_up = np.maximum.accumulate(np.where(after, c > hi, False))
        closed_out_dn = np.maximum.accumulate(np.where(after, c < lo, False))
        fu = first(after & (h > hi) & (c <= hi) & ~np.r_[False, closed_out_up[:-1]])
        fd = first(after & (l < lo) & (c >= lo) & ~np.r_[False, closed_out_dn[:-1]])
        cand = [(i, d) for i, d in ((fu, -1), (fd, 1)) if i is not None]
        if cand:
            i, d = min(cand)
            out.append(("fakeout", i, d, float(h[i] if d == -1 else l[i])))
    # 对照：随机入场（按日期定种子，可复现）
    import zlib
    rng_ = np.random.default_rng(zlib.crc32((str(day_df.index[0].date()) + f"{o[0]:.4f}").encode()))
    i = int(rng_.choice(idx))
    d = int(rng_.choice([1, -1]))
    out.append(("rand", i, d, float(c[i] - d * 0.3 * atr)))
    # 10 点趋势延续
    t10 = np.flatnonzero(tmin == 600)
    if len(t10) and ok[t10[0]]:
        i = int(t10[0])
        mv = (c[i] - o[0]) / atr
        hi, lo = h[:i + 1].max(), l[:i + 1].min()
        pos = (c[i] - lo) / max(hi - lo, 1e-9)
        d = 1 if (mv >= 0.3 and pos >= 0.75) else -1 if (mv <= -0.3 and pos <= 0.25) else 0
        if d:
            st = l[i - 9:i + 1].min() if d == 1 else h[i - 9:i + 1].max()
            out.append(("trend10", i, d, float(st)))
    return out


# ================================================================ 出场（9 种止损）

def simulate(day_df, vw, i, d, struct, atr, iv, prem_x):
    """信号K下标 i，方向 d，结构止损价 struct。返回 {止损id: (出场下标, 出场价)}；股价层面。"""
    o, h, l, c = (day_df[x].values.astype(float) for x in ("open", "high", "low", "close"))
    n = len(c)
    e = i + 1
    if e >= n:
        return {}
    px = o[e]
    tmin = day_df.index.hour.values * 60 + day_df.index.minute.values
    H, Lo, C, O = h[e:], l[e:], c[e:], o[e:]
    adverse = Lo if d == 1 else H                       # 每根对我们最不利的价
    favor = H if d == 1 else Lo
    res = {}

    def stop_hit(stops):
        """stops：每根K的止损价数组（可以比剩下的K短）。返回 (下标, 成交价) 或 None；跳空穿过按开盘价。"""
        adv = adverse[:len(stops)]
        hit = (adv <= stops) if d == 1 else (adv >= stops)
        k = np.flatnonzero(hit)
        if not len(k):
            return None
        k = int(k[0])
        fill = min(O[k], stops[k]) if d == 1 else max(O[k], stops[k])
        return k, fill

    def fixed(stop, tgt=None, last=None):
        last = len(C) - 1 if last is None else last
        stops = np.full(len(C), stop)
        s = stop_hit(stops[:last + 1])
        t = None
        if tgt is not None:
            th = np.flatnonzero((favor[:last + 1] >= tgt) if d == 1 else (favor[:last + 1] <= tgt))
            t = int(th[0]) if len(th) else None
        if s and (t is None or s[0] <= t):              # 同一根两个都碰到算止损
            return e + s[0], s[1]
        if t is not None:
            return e + t, max(O[t], tgt) if d == 1 else min(O[t], tgt)
        return e + last, C[last]

    risk_s = (px - struct) * d
    if risk_s > 0.005 * atr:
        res["struct2R"] = fixed(struct, px + d * 2 * risk_s)
        res["struct_close"] = fixed(struct)
        k11 = np.flatnonzero(tmin[e:] >= 11 * 60)
        res["time11"] = fixed(struct, None, int(k11[0]) if len(k11) else None)
        # VWAP 反穿（收盘到另一侧，下一根开盘走；用这根收盘近似）
        vws = vw[e:]
        cross = np.flatnonzero((C - vws) * d < 0)
        s = stop_hit(np.full(len(C), struct))
        if s and (not len(cross) or s[0] <= cross[0]):
            res["vwap"] = (e + s[0], s[1])
        elif len(cross):
            res["vwap"] = (e + int(cross[0]), C[cross[0]])
        else:
            res["vwap"] = (e + len(C) - 1, C[-1])
        # 前 5 根移动止损（用到上一根为止的 5 根，只往有利方向挪）
        roll = (pd.Series(l).rolling(5).min() if d == 1 else pd.Series(h).rolling(5).max()).shift(1).values[e:]
        roll = np.where(np.isnan(roll), struct, roll)
        trail = np.maximum.accumulate(np.maximum(roll, struct)) if d == 1 else np.minimum.accumulate(np.minimum(roll, struct))
        s = stop_hit(trail)
        res["trail5"] = (e + s[0], s[1]) if s else (e + len(C) - 1, C[-1])
        # 广撒网滚仓（原打法 C）
        r_close = (C - px) * d / risk_s
        r_best = np.maximum.accumulate((favor - px) * d / risk_s)
        stops = np.where(r_best >= 2, px + d * risk_s, np.where(r_best >= 1, px, struct))
        stops = np.r_[struct, stops[:-1]]                # 挪止损从下一根开始生效
        s = stop_hit(stops)
        cut = 15 if len(C) > 15 and r_close[14] < 0.5 and r_best[14] < 1 else None
        if s and (cut is None or s[0] <= 14):
            res["planC"] = (e + s[0], s[1])
        elif cut is not None:
            res["planC"] = (e + 14, C[14])
        else:
            res["planC"] = (e + len(C) - 1, C[-1])
    for sid, m in (("atr10", 0.1), ("atr20", 0.2)):
        res[sid] = fixed(px - d * m * atr, px + d * 2 * m * atr)
    # 权利金固定比例：按每根收盘的期权价判断（期权没有盘中高低点，只能用收盘）
    T0 = (DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / YEAR_MIN
    Tk = T0 - (tmin[e:] - tmin[e]) / YEAR_MIN
    opt, _ = bs(C, px, Tk, iv, d == 1)
    p0, _ = bs(px, px, T0, iv, d == 1)
    ret = opt / p0 - 1
    k = np.flatnonzero((ret <= -prem_x) | (ret >= 2 * prem_x))
    res["prem"] = (e + int(k[0]), C[k[0]]) if len(k) else (e + len(C) - 1, C[-1])
    return res


def option_return(day_df, e, x, px_in, px_out, d, iv):
    """平值、DTE_DAYS 天到期；入场 e 根开盘、出场 x 根（价 px_out）。返回 (权利金收益率, 入场 Delta, 入场权利金)。"""
    tmin = day_df.index.hour.values * 60 + day_df.index.minute.values
    T0 = (DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / YEAR_MIN
    T1 = T0 - (tmin[x] - tmin[e] + 1) / YEAR_MIN
    p0, dl = bs(px_in, px_in, T0, iv, d == 1)
    p1, _ = bs(px_out, px_in, T1, iv, d == 1)
    p0, p1 = float(p0), float(p1)
    if p0 <= 0:
        return None, None, None
    buy, sell = p0 * (1 + HALF_SPREAD), p1 * (1 - HALF_SPREAD)
    return sell / buy - 1, float(dl), p0


# ================================================================ 一只股票

def run_symbol(code, regimes=None, log=print):
    import c2
    c2s = c2.settings()
    c2s["最晚入场"] = "11:00"
    df = load_1m(code)
    if df.empty:
        return None
    days = daily_from_1m(df)
    if len(days) < MIN_DAYS:
        return None
    qqq = load_1m("QQQ") if code != "QQQ" else df
    qg = {k: g for k, g in qqq.groupby(qqq.index.date)} if len(qqq) else {}
    reg = (regimes or {}).get(code, {})
    # 今天的「每张亏 0.5」换算成权利金百分比：用最新价、最新波动率的平值 3 天期权
    last = days.iloc[-1]
    iv_last = days["iv"].dropna()
    p_now = float(bs(last["close"], last["close"], (DTE_TRADING + 1) * 390 / YEAR_MIN, float(iv_last.iloc[-1]) if len(iv_last) else 0.4, True)[0])
    prem_x = float(min(0.6, max(0.05, 0.5 / p_now))) if p_now > 0 else 0.2
    trades = []
    for day, g in df.groupby(df.index.date):
        if day not in days.index:
            continue
        row = days.loc[day]
        if not (row["atr_prev"] > 0) or not (row["iv"] > 0) or not (row["pc"] > 0) or len(g) < 300:
            continue
        o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        vw = _vwap(h, l, c, v)
        gap = (o[0] - row["pc"]) / row["atr_prev"]
        gcls = "big_down" if gap <= -0.75 else "down" if gap <= -0.25 else "big_up" if gap >= 0.75 else "up" if gap >= 0.25 else "flat"
        ds = str(day)
        for sid, i, d, struct in signals(g, row, qg.get(day), c2s):
            sims = simulate(g, vw, i, d, struct, row["atr_prev"], float(row["iv"]), prem_x)
            e = i + 1
            for stp, (x, pout) in sims.items():
                r, dl, p0 = option_return(g, e, x, o[e], pout, d, float(row["iv"]))
                if r is None:
                    continue
                risk = abs(o[e] - struct)
                trades.append((ds, gcls, reg.get(ds), sid, stp, d, round(r, 5), round((pout - o[e]) * d / risk, 4) if risk > 0 else None,
                               round(dl, 3), int(x - e), round(abs(o[e] - struct) / row["atr_prev"], 4)))
    cols = ["date", "gap", "regime", "sid", "stop", "d", "ret", "r_stock", "delta", "bars", "struct_atr"]
    t = pd.DataFrame(trades, columns=cols)
    return summarize_symbol(code, t, days, p_now, prem_x)


def _stats(x):
    n = len(x)
    if not n:
        return None
    r = x["ret"].values
    rs = x["r_stock"].dropna().values
    return {"n": n, "avg": round(float(r.mean()), 4), "win": round(float((r > 0).mean()), 3), "med": round(float(np.median(r)), 4),
            "worst": round(float(r.min()), 3), "avg_r": round(float(rs.mean()), 3) if len(rs) else None}


def summarize_symbol(code, t, days, p_now, prem_x):
    out = {"code": code, "first": str(days.index[0]), "last": str(days.index[-1]), "days": int(len(days)),
           "has_in": bool((pd.to_datetime(days.index.astype(str)) < pd.Timestamp(SPLIT)).sum() >= 250),
           "atr_now": round(float(days["atr_prev"].iloc[-1]), 3), "close_now": round(float(days["close"].iloc[-1]), 2),
           "iv_now": round(float(days["iv"].iloc[-1]), 3), "premium_now": round(p_now, 2), "prem_x": round(prem_x, 3),
           "usd_per_1atr": None, "combos": {}}
    if t.empty:
        return out
    t["half"] = np.where(t["date"] < SPLIT, "in", "out")
    for (sid, stp), g in t.groupby(["sid", "stop"]):
        cell = {"all": _stats(g), "in": _stats(g[g["half"] == "in"]), "out": _stats(g[g["half"] == "out"]),
                "last250": _stats(g[g["date"] >= str(days.index[max(0, len(days) - 250)])]),
                "delta": round(float(g["delta"].abs().mean()), 2), "bars": round(float(g["bars"].median()), 1),
                "struct_atr": round(float(g["struct_atr"].median()), 3), "gap": {}, "regime": {}}
        for k, gg in g.groupby("gap"):
            cell["gap"][k] = {"in": _stats(gg[gg["half"] == "in"]), "out": _stats(gg[gg["half"] == "out"]), "all": _stats(gg)}
        for k, gg in g.groupby("regime"):
            cell["regime"][k] = {"in": _stats(gg[gg["half"] == "in"]), "out": _stats(gg[gg["half"] == "out"]), "all": _stats(gg)}
        out["combos"][f"{sid}|{stp}"] = cell
    return out


# ================================================================ 全部

def _work(args):
    code, regimes = args
    try:
        return code, run_symbol(code, regimes, log=lambda *a: None), None
    except Exception as e:
        import traceback
        return code, None, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"


def all_codes():
    codes = set()
    for path in (DB_LONG, DB):
        if path.exists():
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                codes |= {r[0] for r in con.execute("SELECT DISTINCT code FROM bars_1m")}
            finally:
                con.close()
    return sorted(codes)


def run(codes=None, log=print):
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    codes = [c.upper() for c in codes] if codes else all_codes()
    log(f"日内策略库：{len(codes)} 只股票 × {len(STRATS)} 条策略 × {len(STOPS)} 种止损（读 1 分钟K，第一次要几分钟）")
    regimes = daily_regimes()
    errors, done = {}, []
    import multiprocessing as mp
    import os
    with mp.get_context("spawn").Pool(max(1, min(len(codes), (os.cpu_count() or 2) - 2))) as pool:
        for k, (code, res, err) in enumerate(pool.imap_unordered(_work, [(c, regimes) for c in codes]), 1):
            if err:
                errors[code] = err.splitlines()[0]
                log(f"[{k}/{len(codes)}] {code} 出错：{err}")
                continue
            if res is None:
                log(f"[{k}/{len(codes)}] {code}：1 分钟K不到 {MIN_DAYS} 天，跳过")
                continue
            (OUT / f"{code}.json").write_text(json.dumps(res, ensure_ascii=False), "utf-8")
            done.append(code)
            log(f"[{k}/{len(codes)}] {code}：{res['first']} → {res['last']}，{res['days']} 天，{len(res['combos'])} 组策略×止损")
    build_index(log)
    msg = f"日内策略库已更新：{len(done)} 只，用时 {time.time() - t0:.0f} 秒" + (f"；出错：{', '.join(errors)}" if errors else "")
    log(msg)
    return {"ok": not errors, "message": msg}


def build_index(log=print):
    """跨股票汇总：每组（策略, 止损）在全部 / 每种开盘类型 / 每种日线行情里，多少只股票样本内外都为正。"""
    syms = {}
    for f in sorted(OUT.glob("*.json")):
        if f.name in ("index.json", "playbook.json"):
            continue
        try:
            syms[f.stem] = json.loads(f.read_text("utf-8"))
        except Exception:
            pass

    def passes(cell, n_in=15, n_out=10):
        if not cell or not cell.get("in") or not cell.get("out"):
            return None
        a, b = cell["in"], cell["out"]
        if a["n"] < n_in or b["n"] < n_out:
            return None
        return a["avg"] > 0 and b["avg"] > 0

    keys = sorted({k for y in syms.values() for k in y["combos"]})
    rows = []
    for key in keys:
        sid, stp = key.split("|")
        r = {"sid": sid, "stop": stp, "ctx": {}}
        for ctx in ["all"] + [f"gap:{g}" for g, _ in GAPS] + [f"reg:{x}" for x in
                                                              ("up_strong", "up_mild", "range_narrow", "range_wide", "down_mild", "down_strong", "panic")]:
            v, avgs, ns = [], [], 0
            for y in syms.values():
                c = y["combos"].get(key)
                if not c:
                    continue
                cell = c if ctx == "all" else (c["gap"].get(ctx[4:]) if ctx.startswith("gap:") else c["regime"].get(ctx[4:]))
                p = passes(cell)
                if p is not None:
                    v.append(p)
                if cell and cell.get("all"):
                    avgs.append(cell["all"]["avg"])
                    ns += cell["all"]["n"]
            r["ctx"][ctx] = {"pass": sum(v), "of": len(v), "med_avg": round(float(np.median(avgs)), 4) if avgs else None, "n": ns}
        rows.append(r)
    idx = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "split": SPLIT, "dte": DTE_DAYS, "half_spread": HALF_SPREAD,
           "entry_window": "09:36–11:00", "strats": STRATS, "stops": STOPS, "gaps": [{"id": g, "name": n} for g, n in GAPS],
           "symbols": {c: {k: y[k] for k in ("first", "last", "days", "has_in", "atr_now", "close_now", "iv_now", "premium_now", "prem_x")}
                       for c, y in syms.items()},
           "combos": rows}
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False), "utf-8")
    return idx


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    r = run(args or None)
    sys.exit(0 if r["ok"] else 1)
