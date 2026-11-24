# -*- coding: utf-8 -*-
"""
玉衡5.0 · 日内策略库（1 分钟K + 期权换算），三层结构：入场 × 出场（止损 × 止盈）。仓位方案是第三层，在 sizing.py 里。
日线版（stratlib.py）是给拿几天到几个月的人看的；这个才是你的打法：9:36–11:00 开仓、最晚收盘平、做期权。

  数据：history_long.db（2019-01 起，常规时段）+ history.db（最近一年）的 1 分钟K，前复权价。
  每只股票每天、每条入场、每个方向只取第一个信号；信号K收盘后，下一根开盘成交。
  **所有入场都做多空两个方向**（做多 = 买 CALL，做空 = 买 PUT），结果分开统计。

  日内「行情」（入场时已经知道的，不偷看后面）：开盘类型 = 跳空 ÷ 前一天的日线 ATR14：
    平开（|跳空| < 0.25）、高开 / 低开（0.25–0.75）、大幅高开 / 大幅低开（≥ 0.75）

  入场：个人策略 A（爆发：大实体站上 / 跌破 VWAP）、个人策略 B（VWAP 回踩，c2.scan 原样）、开盘区间突破 5 / 15 / 30 分钟、
        VWAP 收复、偏离 VWAP 回归、缺口回补、缺口延续（缺口回补的镜像）、假突破收回、10 点趋势延续、随机入场对照。
  止损（7 种）：权利金固定比例、0.1 / 0.2 倍 ATR、结构止损、VWAP 反穿、前 5 根移动、11:00 时间止损。
  止盈（8 种）：2R、前高 / 前低流动性、斐波那契 1.0 / 1.272 / 1.618 / 2.0 / 2.618、不设止盈拿到收盘。
  每个信号把 7 × 8 = 56 种出场都算一遍。同一根K线里止损和止盈都碰到，按止损算（保守）；
  VWAP 反穿、11:00 这种「收盘才知道」的出场，和止盈在同一根时按止盈算（止盈价在盘中先到）。

  期权换算（估算，没有历史期权报价）：平值、到期 3 天（你成交的中位数），波动率用这只股票前 20 天的已实现波动率 × 1.1；
    按 Black-Scholes 重新算出场时的期权价（Delta / Gamma / 时间损耗都随时变），时间按交易分钟算；
    买卖各扣 1% 权利金的价差。结果用「权利金收益率」表示（前复权的老价格也能比）。

  检验：样本内 2019–2023，样本外 2024 起。输出到 程序/data/日内策略库/<代码>.json 和 index.json。只读 K 线库，不下单。

用法：arch -arm64 程序/.venv/bin/python 程序/intraday.py [代码 …]      （全部要十几分钟，多进程）
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
    dict(id="A", name="个人策略 A：爆发", dir="多空",
         entry="做多：大实体阳线从 VWAP 下方收上来（实体 ≥ 0.1 ATR、≥ 前 20 根平均 2.5 倍、占整根 ≥ 60%），QQQ 在开盘价和 VWAP 上方，"
               "上方前高（今天高点或昨高）离现价 ≥ 0.1 ATR。做空镜像：大实体阴线从 VWAP 上方收下去，QQQ 在开盘价和 VWAP 下方，下方前低 ≥ 0.1 ATR。",
         struct="信号K另一端", fib="今天最低点 → 爆发K最高点（做空反过来）"),
    dict(id="B", name="个人策略 B：VWAP 回踩", dir="多空",
         entry="第 6 根定方向（收在开盘价和 VWAP 同侧），回踩 VWAP / FVG 后出现确认组合，下一根进（c2.scan 原样）。",
         struct="C2 极值外", fib="从 C2 回调极值起量 C1 段的长度"),
    dict(id="orb5", name="开盘区间突破（5 分钟）", dir="多空", entry="收盘突破前 5 根的最高 / 最低点。", struct="区间另一边"),
    dict(id="orb15", name="开盘区间突破（15 分钟）", dir="多空", entry="收盘突破前 15 根的最高 / 最低点。", struct="区间另一边"),
    dict(id="orb30", name="开盘区间突破（30 分钟）", dir="多空", entry="收盘突破前 30 根的最高 / 最低点。", struct="区间另一边"),
    dict(id="vwap_reclaim", name="VWAP 收复", dir="多空", entry="连续 10 根收在 VWAP 一侧后，第一次收到另一侧（顺着新的一侧做）。", struct="前 10 根极值"),
    dict(id="vwap_fade", name="偏离 VWAP 回归", dir="多空", entry="收盘离 VWAP ≥ 0.4 倍 ATR，反着做（赌回到 VWAP）。", struct="今天极值外 0.05 ATR"),
    dict(id="gap_fill", name="缺口回补", dir="多空", entry="跳空 ≥ 0.25 ATR、9:36 还没补，朝昨收方向做。", struct="今天极值外 0.05 ATR"),
    dict(id="gap_go", name="缺口延续（缺口回补的镜像）", dir="多空", entry="跳空 ≥ 0.25 ATR、9:36 还没补，顺着缺口方向做。",
         struct="和缺口回补同样的距离，放在另一边"),
    dict(id="fakeout", name="假突破收回（15 分钟区间）", dir="多空", entry="刺破开盘 15 分钟区间高 / 低点但收回区间内，反着做。", struct="刺破那根的极值"),
    dict(id="trend10", name="10 点趋势延续", dir="多空", entry="10:00 时离开盘价 ≥ 0.3 ATR，且收在今天区间的上 / 下 25%，顺势做。", struct="前 10 根极值"),
]
for _s in STRATS:
    _s.setdefault("fib", "今天开盘以来的最低点 → 最高点（做空从最高点往下量）")
STRATS.append(dict(id="rand", name="对照：随机入场", dir="多空",
                   entry="9:36–11:00 之间随机挑一根，多空两个方向都算（固定随机种子）。用来检查模型本身偏不偏：它应该在 0 以下（扣了成本）。",
                   struct="0.3 倍 ATR", fib="今天开盘以来的最低点 → 最高点"))

STOPS = [
    dict(id="prem", name="权利金固定比例", how="期权价跌到入场价 ×（1 − X）就走，X = 每张亏 0.5 ÷ 今天平值 3 天期权价（按收盘的期权价判断）。"),
    dict(id="atr10", name="0.1 倍 ATR", how="股价逆向 0.1 倍日线 ATR 止损。"),
    dict(id="atr20", name="0.2 倍 ATR", how="股价逆向 0.2 倍日线 ATR 止损。"),
    dict(id="struct", name="结构止损", how="止损放在这条入场自带的结构位（见入场说明）。"),
    dict(id="vwap", name="VWAP 反穿", how="收盘回到 VWAP 另一侧就走；结构止损兜底。"),
    dict(id="trail5", name="前 5 根移动止损", how="结构止损起步，之后跟着前 5 根最低（做空是最高）点只往有利方向挪。"),
    dict(id="time11", name="11:00 时间止损", how="结构止损；11:00 还在就平。"),
]
FIBS = [1.0, 1.272, 1.618, 2.0, 2.618]
TARGETS = [
    dict(id="r2", name="2R", how="入场价 + 2 × 止损距离（权利金止损：期权涨 2X）。"),
    dict(id="liq", name="前高 / 前低流动性", how="做多：今天开盘以来的最高点和昨高里，离现价最近、在上方的那个；做空反过来。都在后面就拿到收盘。"),
] + [dict(id=f"fib{str(k).replace('.', '')}", name=f"斐波那契 {k:g}", level=k,
          how=f"推动段的起点 + {k:g} × 推动段长度（每条入场的推动段见入场说明）。目标已经在入场价后面就用下一档；都在后面就拿到收盘。")
     for k in FIBS] + [
    dict(id="close", name="不设止盈，拿到收盘", how="只看止损，收盘平。"),
]
EXITS = [(s["id"], t["id"]) for s in STOPS for t in TARGETS]



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
    """返回 [(入场id, 信号K下标 i, 方向 d, 结构止损价, 斐波那契起点, 推动段长度)]。
    每条入场每个方向当天只取第一个；成交在 i+1 根开盘。斐波那契目标 = 起点 + d × 档位 × 长度。"""
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
    lo_run, hi_run = np.minimum.accumulate(l), np.maximum.accumulate(h)

    def first(mask):
        m = np.flatnonzero(mask & ok)
        return int(m[0]) if len(m) else None

    def add(sid, i, d, st, fib=None):
        if i is None:
            return
        a, ln = fib if fib else ((lo_run[i], hi_run[i] - lo_run[i]) if d == 1 else (hi_run[i], hi_run[i] - lo_run[i]))
        out.append((sid, int(i), int(d), float(st), float(a), float(ln)))

    # A 爆发：多空镜像
    body = np.abs(c - o)
    avg20 = pd.Series(body).shift(1).rolling(20, min_periods=5).mean().values
    rng = h - l
    big = (body >= 0.1 * atr) & (body >= 2.5 * np.nan_to_num(avg20, nan=1e9)) & (rng > 0) & (body >= 0.6 * rng)
    pc_, pvw = np.r_[np.nan, c[:-1]], np.r_[np.nan, vw[:-1]]
    if qqq_day is not None and len(qqq_day):
        qo, qh, ql, qc, qv = (qqq_day[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        qvw = _vwap(qh, ql, qc, qv)
        qup = pd.Series((qc > qo[0]) & (qc > qvw), index=qqq_day.index).reindex(day_df.index).fillna(False).values.astype(bool)
        qdn = pd.Series((qc < qo[0]) & (qc < qvw), index=qqq_day.index).reindex(day_df.index).fillna(False).values.astype(bool)
        prior_high, prior_low = np.r_[-np.inf, hi_run[:-1]], np.r_[np.inf, lo_run[:-1]]
        tgt_up = np.where(prior_high > c, prior_high, np.where(row["ph"] > c, row["ph"], np.nan))
        tgt_dn = np.where(prior_low < c, prior_low, np.where(row["pl"] < c, row["pl"], np.nan))
        up = big & (c > o) & (c > vw) & ((pc_ < pvw) | (o < vw)) & qup & ((tgt_up - c) >= 0.1 * atr)
        dn = big & (c < o) & (c < vw) & ((pc_ > pvw) | (o > vw)) & qdn & ((c - tgt_dn) >= 0.1 * atr)
        i = first(up)
        if i is not None:
            add("A", i, 1, l[i], (lo_run[i], h[i] - lo_run[i]))
        i = first(dn)
        if i is not None:
            add("A", i, -1, h[i], (hi_run[i], hi_run[i] - l[i]))
    # B VWAP 回踩：c2.scan 原样（只看到 11:00 的K）；方向由第 6 根定，多空都有
    try:
        import c2
        sig = c2.scan(day_df.iloc[:idx[-1] + 1], atr, s=c2s, prev_close=row["pc"])
        if sig.get("phase") == "入场" and sig.get("trig_idx") is not None:
            i = int(sig["trig_idx"])
            if ok[i]:
                d = 1 if sig["dir"] == "多" else -1
                c2x = float(sig["stop"]) + d * c2s["止损缓冲_ATR"] * atr
                add("B", i, d, float(sig["stop"]), (c2x, abs(float(sig["c1_ext"]) - float(sig["c1_origin"]))))
    except Exception:
        pass
    # 开盘区间突破：两个方向各取第一个
    for k, sid in ((5, "orb5"), (15, "orb15"), (30, "orb30")):
        if n <= k:
            continue
        hi, lo = h[:k].max(), l[:k].min()
        after = np.arange(n) >= k
        add(sid, first(after & (c > hi)), 1, lo)
        add(sid, first(after & (c < lo)), -1, hi)
    # VWAP 收复
    side = np.sign(c - vw)
    for d in (1, -1):
        prev10 = pd.Series(side == -d).rolling(10).sum().shift(1).values == 10
        i = first(prev10 & (side == d))
        if i is not None:
            add("vwap_reclaim", i, d, l[max(0, i - 10):i + 1].min() if d == 1 else h[max(0, i - 10):i + 1].max())
    # 偏离 VWAP 回归（反着做）
    dev = (c - vw) / atr
    i = first(dev >= 0.4)
    if i is not None:
        add("vwap_fade", i, -1, hi_run[i] + 0.05 * atr)
    i = first(dev <= -0.4)
    if i is not None:
        add("vwap_fade", i, 1, lo_run[i] - 0.05 * atr)
    # 缺口回补 / 缺口延续（镜像）：只看 9:36 那一根
    gap = (o[0] - row["pc"]) / atr
    i0 = int(idx[0])
    if abs(gap) >= 0.25:
        d = -1 if gap > 0 else 1
        if (c[i0] - row["pc"]) * d < 0:            # 还没补
            st = (hi_run[i0] + 0.05 * atr) if d == -1 else (lo_run[i0] - 0.05 * atr)
            add("gap_fill", i0, d, st)
            add("gap_go", i0, -d, c[i0] - (st - c[i0]))
    # 假突破收回（15 分钟区间）
    if n > 15:
        hi, lo = h[:15].max(), l[:15].min()
        after = np.arange(n) >= 15
        closed_out_up = np.maximum.accumulate(np.where(after, c > hi, False))
        closed_out_dn = np.maximum.accumulate(np.where(after, c < lo, False))
        fu = first(after & (h > hi) & (c <= hi) & ~np.r_[False, closed_out_up[:-1]])
        fd = first(after & (l < lo) & (c >= lo) & ~np.r_[False, closed_out_dn[:-1]])
        if fu is not None:
            add("fakeout", fu, -1, h[fu])
        if fd is not None:
            add("fakeout", fd, 1, l[fd])
    # 10 点趋势延续
    t10 = np.flatnonzero(tmin == 600)
    if len(t10) and ok[t10[0]]:
        i = int(t10[0])
        mv = (c[i] - o[0]) / atr
        pos = (c[i] - lo_run[i]) / max(hi_run[i] - lo_run[i], 1e-9)
        d = 1 if (mv >= 0.3 and pos >= 0.75) else -1 if (mv <= -0.3 and pos <= 0.25) else 0
        if d:
            add("trend10", i, d, l[i - 9:i + 1].min() if d == 1 else h[i - 9:i + 1].max())
    # 对照：随机入场（按日期定种子，可复现），同一根两个方向都算
    import zlib
    rng_ = np.random.default_rng(zlib.crc32((str(day_df.index[0].date()) + f"{o[0]:.4f}").encode()))
    i = int(rng_.choice(idx))
    for d in (1, -1):
        add("rand", i, d, c[i] - d * 0.3 * atr)
    return out


# ================================================================ 出场：7 种止损 × 8 种止盈

NONE = 10 ** 9


def exits(day_df, vw, tmin, i, d, struct, atr, iv, prem_x, fib_a, fib_len, ph, pl):
    """返回 (e, px, [(止损id, 止盈id, 出场下标, 出场价, 止盈不可用)])，股价层面；e 是成交那根。"""
    o, h, l, c = (day_df[x].values for x in ("open", "high", "low", "close"))
    n = len(c)
    e = i + 1
    if e >= n:
        return None
    px = o[e]
    H, Lo, C, O = h[e:], l[e:], c[e:], o[e:]
    m = len(C)
    last = m - 1
    adverse = Lo if d == 1 else H
    favor = H if d == 1 else Lo

    def stop_hit(stops):
        hit = (adverse <= stops) if d == 1 else (adverse >= stops)
        k = np.flatnonzero(hit)
        if not len(k):
            return NONE, None
        k = int(k[0])
        return k, (min(O[k], stops[k]) if d == 1 else max(O[k], stops[k]))

    # ---- 止损事件：sid -> (k, 成交价, 种类 'px' / 'close', 1R 的股价距离)
    ev = {}
    risk_s = (px - struct) * d
    if risk_s > 0.005 * atr:
        full = np.full(m, struct)
        ks, fs = stop_hit(full)
        ev["struct"] = (ks, fs, "px", risk_s)
        k11 = np.flatnonzero(tmin[e:] >= 11 * 60)
        k11 = int(k11[0]) if len(k11) else NONE
        ev["time11"] = (ks, fs, "px", risk_s) if ks <= k11 else (k11, C[k11], "close", risk_s)
        cross = np.flatnonzero((C - vw[e:]) * d < 0)
        cr = int(cross[0]) if len(cross) else NONE
        ev["vwap"] = (ks, fs, "px", risk_s) if ks <= cr else (cr, C[cr], "close", risk_s)
        roll = (pd.Series(l).rolling(5).min() if d == 1 else pd.Series(h).rolling(5).max()).shift(1).values[e:]
        roll = np.where(np.isnan(roll), struct, roll)
        trail = np.maximum.accumulate(np.maximum(roll, struct)) if d == 1 else np.minimum.accumulate(np.minimum(roll, struct))
        k, f = stop_hit(trail)
        ev["trail5"] = (k, f, "px", risk_s)
    for sid, mm in (("atr10", 0.1), ("atr20", 0.2)):
        k, f = stop_hit(np.full(m, px - d * mm * atr))
        ev[sid] = (k, f, "px", mm * atr)
    # 权利金：按每根收盘的期权价
    T0 = (DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / YEAR_MIN
    Tk = T0 - (tmin[e:] - tmin[e]) / YEAR_MIN
    opt, _ = bs(C, px, Tk, iv, d == 1)
    p0, _ = bs(px, px, T0, iv, d == 1)
    ret = opt / p0 - 1
    k = np.flatnonzero(ret <= -prem_x)
    ev["prem"] = (int(k[0]), C[k[0]], "close", None) if len(k) else (NONE, None, "close", None)
    k = np.flatnonzero(ret >= 2 * prem_x)
    prem_tgt = (int(k[0]), C[k[0]]) if len(k) else (NONE, None)

    # ---- 止盈价（和止损无关的先算好）
    def tgt_hit(tg):
        hit = np.flatnonzero((favor >= tg) if d == 1 else (favor <= tg))
        if not len(hit):
            return NONE, None
        t = int(hit[0])
        return t, (max(O[t], tg) if d == 1 else min(O[t], tg))

    fixed_t = {}
    if d == 1:
        cand = [x for x in (h[:e].max(), ph) if x == x and x > px]
        liq = min(cand) if cand else None
    else:
        cand = [x for x in (l[:e].min(), pl) if x == x and x < px]
        liq = max(cand) if cand else None
    fixed_t["liq"] = tgt_hit(liq) if liq is not None else None
    for j, kf in enumerate(FIBS):
        tid = f"fib{str(kf).replace('.', '')}"
        tg = None
        if fib_len > 0:
            for lev in FIBS[j:]:
                x = fib_a + d * lev * fib_len
                if (x - px) * d > 0:
                    tg = x
                    break
        fixed_t[tid] = tgt_hit(tg) if tg is not None else None
    fixed_t["close"] = (NONE, None)

    res = []
    for sid, (ks, fs, kind, r1) in ev.items():
        for t in TARGETS:
            tid = t["id"]
            if tid == "r2":
                th = prem_tgt if sid == "prem" else tgt_hit(px + d * 2 * r1)
                tnone = False
            else:
                th = fixed_t[tid]
                tnone = th is None and tid != "close"
                if th is None:
                    th = (NONE, None)
            kt, ft = th
            if ks == NONE and kt == NONE:
                x, p = last, C[last]
            elif kt < ks or (kt == ks and kind == "close"):
                x, p = kt, ft
            else:
                x, p = ks, fs
            res.append((sid, tid, e + x, float(p), tnone))
    return e, px, res


def option_return(day_df, e, x, px_in, px_out, d, iv):
    """平值、DTE_DAYS 天到期；入场 e 根开盘、出场 x 根（价 px_out）。x、px_out 可以是数组。返回 (权利金收益率, 入场 Delta, 入场权利金)。"""
    tmin = day_df.index.hour.values * 60 + day_df.index.minute.values
    T0 = (DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / YEAR_MIN
    T1 = T0 - (tmin[np.asarray(x)] - tmin[e] + 1) / YEAR_MIN
    p0, dl = bs(px_in, px_in, T0, iv, d == 1)
    p1, _ = bs(px_out, px_in, T1, iv, d == 1)
    p0 = float(p0)
    if p0 <= 0:
        return None, None, None
    return np.asarray(p1) * (1 - HALF_SPREAD) / (p0 * (1 + HALF_SPREAD)) - 1, float(dl), p0


# ================================================================ 一只股票

GAP_IDS = [g for g, _ in GAPS]
EXIT_IDX = {k: j for j, k in enumerate(EXITS)}


def _gap_class(gap):
    return "big_down" if gap <= -0.75 else "down" if gap <= -0.25 else "big_up" if gap >= 0.75 else "up" if gap >= 0.25 else "flat"


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
    # 今天的「每张亏 0.5」换算成权利金百分比：用最新价、最新波动率的平值 3 天期权
    last = days.iloc[-1]
    iv_last = days["iv"].dropna()
    p_now = float(bs(last["close"], last["close"], (DTE_TRADING + 1) * 390 / YEAR_MIN, float(iv_last.iloc[-1]) if len(iv_last) else 0.4, True)[0])
    prem_x = float(min(0.6, max(0.05, 0.5 / p_now))) if p_now > 0 else 0.2
    sid_idx = {s["id"]: j for j, s in enumerate(STRATS)}
    cols = {k: [] for k in ("date", "gap", "sid", "ex", "d", "ret", "r", "delta", "bars", "satr", "tnone")}
    for day, g in df.groupby(df.index.date):
        if day not in days.index:
            continue
        row = days.loc[day]
        if not (row["atr_prev"] > 0) or not (row["iv"] > 0) or not (row["pc"] > 0) or len(g) < 300:
            continue
        o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        vw = _vwap(h, l, c, v)
        tmin = g.index.hour.values * 60 + g.index.minute.values
        atr, iv = float(row["atr_prev"]), float(row["iv"])
        gcls = GAP_IDS.index(_gap_class((o[0] - row["pc"]) / atr))
        ds = str(day)
        for sid, i, d, struct, fa, fl in signals(g, row, qg.get(day), c2s):
            r = exits(g, vw, tmin, i, d, struct, atr, iv, prem_x, fa, fl, row["ph"], row["pl"])
            if not r:
                continue
            e, px, res = r
            xs = np.array([x for *_, x, _p, _t in res])
            ps = np.array([p for *_, p, _t in res])
            rets, dl, p0 = option_return(g, e, xs, px, ps, d, iv)
            if rets is None:
                continue
            risk = abs(px - struct)
            k = len(res)
            cols["date"] += [ds] * k
            cols["gap"] += [gcls] * k
            cols["sid"] += [sid_idx[sid]] * k
            cols["ex"] += [EXIT_IDX[(a, b)] for a, b, *_ in res]
            cols["d"] += [d] * k
            cols["ret"].append(rets)
            cols["r"].append((ps - px) * d / risk if risk > 0 else np.full(k, np.nan))
            cols["delta"] += [abs(dl)] * k
            cols["bars"].append(xs - e)
            cols["satr"] += [risk / atr] * k
            cols["tnone"] += [t for *_, t in res]
    for k in ("ret", "r", "bars"):
        cols[k] = np.concatenate(cols[k]) if cols[k] else np.array([])
    t = pd.DataFrame(cols)
    return summarize_symbol(code, t, days, p_now, prem_x)


def _stats_frame(g):
    """groupby 对象 → DataFrame(n, avg, win, worst, avg_r)。"""
    a = g.agg(n=("ret", "size"), avg=("ret", "mean"), win=("pos", "mean"), worst=("ret", "min"), avg_r=("r", "mean"))
    return a


def _st(row):
    return {"n": int(row["n"]), "avg": round(float(row["avg"]), 4), "win": round(float(row["win"]), 3),
            "worst": round(float(row["worst"]), 3), "avg_r": None if row["avg_r"] != row["avg_r"] else round(float(row["avg_r"]), 3)}


def combo_key(sid, stop, tgt, d):
    return f"{sid}|{stop}|{tgt}|{'L' if d == 1 else 'S'}"


def summarize_symbol(code, t, days, p_now, prem_x):
    out = {"code": code, "first": str(days.index[0]), "last": str(days.index[-1]), "days": int(len(days)),
           "has_in": bool((pd.to_datetime(days.index.astype(str)) < pd.Timestamp(SPLIT)).sum() >= 250),
           "atr_now": round(float(days["atr_prev"].iloc[-1]), 3), "close_now": round(float(days["close"].iloc[-1]), 2),
           "iv_now": round(float(days["iv"].iloc[-1]), 3), "premium_now": round(p_now, 2), "prem_x": round(prem_x, 3),
           "combos": {}}
    if t.empty:
        return out
    t["half"] = np.where(t["date"] < SPLIT, 0, 1)
    t["pos"] = (t["ret"] > 0).astype(float)
    K = ["sid", "ex", "d"]
    base = _stats_frame(t.groupby(K))
    extra = t.groupby(K).agg(delta=("delta", "mean"), bars=("bars", "median"), satr=("satr", "median"), tnone=("tnone", "mean"))
    by_half = _stats_frame(t.groupby(K + ["half"]))
    by_gap = _stats_frame(t.groupby(K + ["gap"]))
    by_gap_half = _stats_frame(t.groupby(K + ["gap", "half"]))
    cells = {}
    for key, row in base.iterrows():
        sid, ex, d = key
        x = extra.loc[key]
        cells[key] = {"all": _st(row), "in": None, "out": None, "delta": round(float(x["delta"]), 2), "bars": round(float(x["bars"]), 1),
                      "struct_atr": round(float(x["satr"]), 3), "tgt_none": round(float(x["tnone"]), 3), "gap": {}}
    for (sid, ex, d, hf), row in by_half.iterrows():
        cells[(sid, ex, d)]["in" if hf == 0 else "out"] = _st(row)
    for (sid, ex, d, gp), row in by_gap.iterrows():
        cells[(sid, ex, d)]["gap"][GAP_IDS[gp]] = {"all": _st(row), "in": None, "out": None}
    for (sid, ex, d, gp, hf), row in by_gap_half.iterrows():
        cells[(sid, ex, d)]["gap"][GAP_IDS[gp]]["in" if hf == 0 else "out"] = _st(row)
    for (sid, ex, d), cell in cells.items():
        stp, tgt = EXITS[ex]
        out["combos"][combo_key(STRATS[sid]["id"], stp, tgt, d)] = cell
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
    log(f"日内策略库：{len(codes)} 只股票 × {len(STRATS)} 条入场（多空）× {len(STOPS)} 种止损 × {len(TARGETS)} 种止盈（读 1 分钟K，要十几分钟）")
    errors, done = {}, []
    import multiprocessing as mp
    import os
    with mp.get_context("spawn").Pool(max(1, min(len(codes), (os.cpu_count() or 2) - 2))) as pool:
        for k, (code, res, err) in enumerate(pool.imap_unordered(_work, [(c, None) for c in codes]), 1):
            if err:
                errors[code] = err.splitlines()[0]
                log(f"[{k}/{len(codes)}] {code} 出错：{err}")
                continue
            if res is None:
                log(f"[{k}/{len(codes)}] {code}：1 分钟K不到 {MIN_DAYS} 天，跳过")
                continue
            (OUT / f"{code}.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), "utf-8")
            done.append(code)
            log(f"[{k}/{len(codes)}] {code}：{res['first']} → {res['last']}，{res['days']} 天，{len(res['combos'])} 组（入场 × 止损 × 止盈 × 方向）")
    build_index(log)
    msg = f"日内策略库已更新：{len(done)} 只，用时 {time.time() - t0:.0f} 秒" + (f"；出错：{', '.join(errors)}" if errors else "")
    log(msg)
    return {"ok": not errors, "message": msg}


def build_index(log=print):
    """跨股票汇总：每组（入场, 止损, 止盈, 方向）在全部 / 每种开盘类型里，多少只股票样本内外都为正。"""
    syms = {}
    for f in sorted(OUT.glob("*.json")):
        if f.name in ("index.json", "playbook.json"):
            continue
        try:
            y = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if any(k.count("|") != 3 for k in list(y.get("combos", {}))[:3]):
            continue                                    # 旧格式（改三层之前）的结果不混进来
        syms[f.stem] = y

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
        sid, stp, tgt, dd = key.split("|")
        r = {"sid": sid, "stop": stp, "tgt": tgt, "dir": dd, "ctx": {}}
        for ctx in ["all"] + [f"gap:{g}" for g, _ in GAPS]:
            v, avgs, ns = [], [], 0
            for y in syms.values():
                c = y["combos"].get(key)
                if not c:
                    continue
                cell = c if ctx == "all" else c["gap"].get(ctx[4:])
                p = passes(cell)
                if p is not None:
                    v.append(p)
                if cell and cell.get("all"):
                    avgs.append(cell["all"]["avg"])
                    ns += cell["all"]["n"]
            r["ctx"][ctx] = {"pass": sum(v), "of": len(v), "med_avg": round(float(np.median(avgs)), 4) if avgs else None, "n": ns}
        rows.append(r)
    idx = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "split": SPLIT, "dte": DTE_DAYS, "half_spread": HALF_SPREAD,
           "entry_window": "09:36–11:00", "layers": 3, "strats": STRATS, "stops": STOPS, "targets": TARGETS,
           "gaps": [{"id": g, "name": n} for g, n in GAPS],
           "symbols": {c: {k: y[k] for k in ("first", "last", "days", "has_in", "atr_now", "close_now", "iv_now", "premium_now", "prem_x")}
                       for c, y in syms.items()},
           "combos": rows}
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return idx


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--index" in sys.argv:
        build_index()
        sys.exit(0)
    r = run(args or None)
    sys.exit(0 if r["ok"] else 1)
