# -*- coding: utf-8 -*-
"""
「80% 胜率 最佳剥头皮策略」两个视频的回测（研究专用，只读数据库）

来源：B 站 up 主「Crypto-阿洋」合集《技术指标 完整教程》里的两集（2026-11-26 读字幕整理）：
  BV1F5yzzGEZ3「1 分钟」：平均K线图 + Chandelier Exit（ATR 周期 1、乘数 2，everget 版）+ ZLSMA(50)
  BV1JQy2zFEzV「3 分钟」：3 条 SMMA(21/50/200) + 威廉姆斯分形（n=2）+ RSI(14) 50 线
  他自己的测试是 BTC 5 分钟各 100 次（55% / 58% 胜率），所以周期也测 5 分钟。

程序化口径（做多；做空把价格取负，指标全部对称）：
  ce（1 分钟视频）：
    Chandelier Exit：longStop = 收盘 − 2×TR，shortStop = 收盘 + 2×TR（周期 1 时 ATR 就是当根 TR，用收盘算极值），
      按 everget 原代码向有利方向棘轮；dir 由「收盘越过上一根的 shortStop / longStop」翻转，dir 从 −1 翻到 1 = 买入信号。
    关键K = 买入信号那根，且是阳线、收盘在 ZLSMA(50) 上方。止损 = 关键K最低点。
    出场 fixed = 1.5R 全走；half = 1.5R 走一半，剩下等收盘跌破 ZLSMA（视频的「右侧止盈」），止损不挪。
  frac（3 分钟视频）：
    分形（向下）= 第 j 根的低点低于左右各 2 根；第 j+2 根收盘后才知道 = 关键K（视频说「箭头后的第二根」）。
    条件：关键K收盘时 SMMA21 > SMMA50 > SMMA200，分形那根的 RSI(14) > 50。止损 = 分形低点（阶段性低点）。
    出场 fixed = 2R；half = 2R 走一半，剩下等收盘跌破 SMMA21。
  K 线：
    real   = 普通K线算信号，下一根 1 分钟开盘成交，止损 / 目标按真实最高最低价（能真的成交的口径）；
    ha     = 平均K线（Heikin Ashi）算信号和止损，但按真实价格成交（照着视频在 HA 图上看信号、真实下单）；
    hafake = 全部用 HA 的价格算（HA 收盘进、HA 高低点碰止损目标）——这是在 HA 图上回放 / 回测会看到的「成绩」，实际成交不到。
  时段：9:36–15:30 开仓（「早盘」= 11:00 前），16:00 没走完按收盘平。同一组合上一笔没走完不开下一笔。同一根K两边都碰到算止损。
  指标跨天连续算（常规时段K线连起来，和 TradingView 只看常规时段的图一样）。
  成本：正股每笔 0.01 倍日线 ATR 换成 R 扣掉；期权按 intraday 的口径（平值、约 3 天到期、Black-Scholes、交易时间时钟、每边 1% 价差）。
  对照 rand：同一天、同方向、随机时间进场，止损距离和真信号一样、目标同样 1.5R / 2R（只给 real / ha 的 fixed 做）。
  止损离入场不到 0.02 倍日线 ATR 的单独统计（1 分钟K的低点常常只差几美分，扣成本会爆掉）。
数据：history_long.db（2019-01 起）+ history.db 的 1 分钟K，只用完整交易日（390 根）。样本外 2024 起。规则没有按数据调过参数。
"""
import json
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import lfilter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intraday   # noqa: E402

COST = 0.01
SKIP = {"ANT", "CRWV"}
SPLIT = "2024-01-01"
MIN_RISK = 0.02
OUT = HERE / "data" / ".scalp80_bt.pkl"
FIRST, LAST_AM, LAST_ALL = 576, 660, 930      # 9:36、11:00、15:30（开盘时间，分钟）

VARIANTS = []                                  # (策略, 周期, K线, 出场)
for _tf in (1, 5):
    for _cd in ("real", "ha", "hafake"):
        for _md in ("fixed", "half"):
            VARIANTS.append(("ce", _tf, _cd, _md))
for _tf in (3, 5):
    for _cd in ("real", "hafake"):
        for _md in ("fixed", "half"):
            VARIANTS.append(("frac", _tf, _cd, _md))
TGT = {"ce": 1.5, "frac": 2.0}


# ================================================================ 指标

def rma(x, n):
    return pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().values


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up, dn = rma(np.maximum(d, 0), n), rma(np.maximum(-d, 0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(dn > 0, 100 - 100 / (1 + up / dn), 100.0)


def linreg_end(y, n):
    """TradingView ta.linreg(y, n, 0)：最近 n 根最小二乘直线在最后一根的值。前面不够 n 根为 nan。"""
    out = np.full(len(y), np.nan)
    ok = np.nonzero(~np.isnan(y))[0]
    if len(ok) < n:
        return out
    s = ok[0]
    yy = y[s:]
    x = np.arange(n, dtype=float)
    xm = (n - 1) / 2
    sxx = ((x - xm) ** 2).sum()
    num = np.convolve(yy, (x - xm)[::-1], mode="valid")
    ym = np.convolve(yy, np.ones(n) / n, mode="valid")
    out[s + n - 1:] = ym + num / sxx * (n - 1 - xm)
    return out


def zlsma(c, n=50):
    a = linreg_end(c, n)
    b = linreg_end(a, n)
    return a + (a - b)


def chandelier_dir(h, l, c, mult=2.0):
    """everget Chandelier Exit，length=1、useClose=true。返回 dir（1 / -1）。"""
    n = len(c)
    c1 = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - c1), np.abs(l - c1)))
    ls0 = (c - mult * tr).tolist()
    ss0 = (c + mult * tr).tolist()
    cl = c.tolist()
    out = [1] * n
    lsp, ssp, dr = ls0[0], ss0[0], 1
    for i in range(1, n):
        ls = ls0[i]
        ss = ss0[i]
        if cl[i - 1] > lsp:
            ls = max(ls, lsp)
        if cl[i - 1] < ssp:
            ss = min(ss, ssp)
        if cl[i] > ssp:
            dr = 1
        elif cl[i] < lsp:
            dr = -1
        out[i] = dr
        lsp, ssp = ls, ss
    return np.array(out, np.int8)


def heikin(o, h, l, c):
    hc = (o + h + l + c) / 4
    # haO[i] = (haO[i-1] + haC[i-1]) / 2，haO[0] = (o0 + c0) / 2
    ho = np.empty_like(hc)
    ho[0] = (o[0] + c[0]) / 2
    if len(hc) > 1:
        ho[1:], _ = lfilter([0.5], [1, -0.5], hc[:-1], zi=[0.5 * ho[0]])
    hh = np.maximum(h, np.maximum(ho, hc))
    hl = np.minimum(l, np.minimum(ho, hc))
    return ho, hh, hl, hc


# ================================================================ 走一笔

def walk(o, h, l, c, xs, e, px, stop, tgt, end, next_open):
    """从第 e 根（含）开始，入场价 px。tgt=None 不设目标；xs = 收盘出场信号（None 不用）。
    next_open=True 时收盘信号在下一根开盘走，否则按这根收盘走。返回 (出场根, 出场价, 是否到目标)。"""
    BIG = 10 ** 9
    ls = l[e:end]
    hs = h[e:end]
    a = np.nonzero(ls <= stop)[0]
    s = a[0] if len(a) else BIG
    t = BIG
    if tgt is not None:
        a = np.nonzero(hs >= tgt)[0]
        t = a[0] if len(a) else BIG
    x = BIG
    if xs is not None:
        a = np.nonzero(xs[e:end])[0]
        x = a[0] if len(a) else BIG
    m = min(s, t, x)
    if m == BIG:
        return end - 1, c[end - 1], False
    k = e + m
    if s == m:
        return k, (o[k] if (k > e and o[k] < stop) else stop), False
    if t == m:
        return k, (o[k] if (k > e and o[k] > tgt) else tgt), True
    if next_open and k + 1 < end:
        return k + 1, o[k + 1], False
    return k, c[k], False


def opt_ret(tmin, e, x, px_in, px_out, d, iv):
    T0 = (intraday.DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / intraday.YEAR_MIN
    T1 = T0 - (tmin[x] - tmin[e] + 1) / intraday.YEAR_MIN
    p0, _ = intraday.bs(px_in, px_in, T0, iv, d == 1)
    p1, _ = intraday.bs(px_out, px_in, T1, iv, d == 1)
    p0, p1 = float(p0), float(p1)
    if p0 <= 0:
        return np.nan
    return p1 * (1 - intraday.HALF_SPREAD) / (p0 * (1 + intraday.HALF_SPREAD)) - 1


# ================================================================ 一只股票

def run_dir(df, dinfo, d, rng):
    sgn = 1.0 if d == 1 else -1.0
    o = df.open.values * sgn
    if d == 1:
        h, l = df.high.values.copy(), df.low.values.copy()
    else:
        h, l = -df.low.values, -df.high.values
    c = df.close.values * sgn
    n = len(c)
    tmin = (df.index.hour.values * 60 + df.index.minute.values).astype(int)
    dayid = dinfo["dayid"]
    day_end = dinfo["day_end"]
    rows = []
    for tf in (1, 3, 5):
        if not any(v[1] == tf for v in VARIANTS):
            continue
        if tf == 1:
            ends = np.arange(n)
            O, H, L, C = o, h, l, c
        else:
            starts = np.arange(0, n, tf)
            ends = starts + tf - 1
            O = o[starts]
            H = np.maximum.reduceat(h, starts)
            L = np.minimum.reduceat(l, starts)
            C = c[ends]
        nb = len(C)
        ha = heikin(O, H, L, C)
        for cd in ("real", "ha", "hafake"):
            vs = [(vi, v) for vi, v in enumerate(VARIANTS) if v[1] == tf and v[2] == cd]
            if not vs:
                continue
            XO, XH, XL, XC = (O, H, L, C) if cd == "real" else ha
            for strat in sorted({v[0] for _, v in vs}):
                if strat == "ce":
                    dr = chandelier_dir(XH, XL, XC)
                    line = zlsma(XC, 50)
                    sig = np.zeros(nb, bool)
                    sig[1:] = (dr[1:] == 1) & (dr[:-1] == -1)
                    keys = np.nonzero(sig & (XC > line) & (XC > XO))[0]
                    stops = XL[keys]
                else:
                    s21, s50, s200 = rma(XC, 21), rma(XC, 50), rma(XC, 200)
                    r = rsi(XC, 14)
                    j = np.arange(2, nb - 2)
                    frac = ((XL[j] < XL[j - 1]) & (XL[j] < XL[j - 2]) & (XL[j] <= XL[j + 1]) & (XL[j] <= XL[j + 2]))
                    j = j[frac]
                    k = j + 2
                    okk = (s21[k] > s50[k]) & (s50[k] > s200[k]) & (r[j] > 50) & (k >= 600)   # SMMA200 要先热身
                    keys, stops = k[okk], XL[j[okk]]
                    line = s21
                xs_bar = XC < line                                     # 收盘跌破线 = 右侧出场
                xs1 = None
                if cd != "hafake":
                    xs1 = np.zeros(n, bool)
                    xs1[ends] = xs_bar
                R_T = TGT[strat]
                for vi, (_, _, _, md) in [(vi, v) for vi, v in vs if v[0] == strat]:
                    busy = -1
                    for kk, stop in zip(keys.tolist(), stops.tolist()):
                        e1 = ends[kk] + 1                                # 真实成交的那根 1 分钟
                        if e1 >= n or dayid[e1] != dayid[ends[kk]]:
                            continue
                        st = tmin[e1] - 1
                        if st < FIRST or st > LAST_ALL:
                            continue
                        di = dayid[e1]
                        A = dinfo["atr"][di]
                        iv = dinfo["iv"][di]
                        dend = day_end[di] + 1
                        if cd == "hafake":
                            if kk <= busy:
                                continue
                            px = XC[kk]
                            if not (px > stop):
                                continue
                            risk = px - stop
                            bend = (dend - 1) // tf + 1                 # 这天最后一根 tf 周期K之后
                            x1, p1, hit = walk(XO, XH, XL, XC, None, kk + 1, px, stop, px + R_T * risk, bend, False)
                            if md == "half":
                                x2, p2, _ = walk(XO, XH, XL, XC, xs_bar, kk + 1, px, stop, None, bend, False)
                                R = ((p1 - px) + (p2 - px)) / 2 / risk
                                busy = max(x1, x2)
                            else:
                                R = (p1 - px) / risk
                                busy = x1
                            rows.append((vi, 0, di, st <= LAST_AM, d, risk / A, R, np.nan, hit))
                            continue
                        if e1 <= busy:
                            continue
                        px = o[e1]
                        if not (px > stop):
                            continue
                        risk = px - stop
                        x1, p1, hit = walk(o, h, l, c, None, e1, px, stop, px + R_T * risk, dend, True)
                        op1 = opt_ret(tmin, e1, x1, px * sgn, p1 * sgn, d, iv)
                        if md == "half":
                            x2, p2, _ = walk(o, h, l, c, xs1, e1, px, stop, None, dend, True)
                            op2 = opt_ret(tmin, e1, x2, px * sgn, p2 * sgn, d, iv)
                            R = ((p1 - px) + (p2 - px)) / 2 / risk
                            op = (op1 + op2) / 2
                            busy = max(x1, x2)
                        else:
                            R = (p1 - px) / risk
                            op = op1
                            busy = x1
                        rows.append((vi, 0, di, st <= LAST_AM, d, risk / A, R, op, hit))
                        if md == "fixed":                                # 随机时间对照
                            d0 = dinfo["day_start"][di]
                            lo_k, hi_k = d0 + (FIRST - 570) + 1, min(dend - 1, d0 + (LAST_ALL - 570) + 1)
                            if hi_k > lo_k:
                                r0 = int(rng.integers(lo_k, hi_k))
                                rp = o[r0]
                                rx, rxp, rhit = walk(o, h, l, c, None, r0, rp, rp - risk, rp + R_T * risk, dend, True)
                                rows.append((vi, 1, di, (tmin[r0] - 1) <= LAST_AM, d, risk / A, (rxp - rp) / risk,
                                             opt_ret(tmin, r0, rx, rp * sgn, rxp * sgn, d, iv), rhit))
    return rows


def run_code(code):
    df = intraday.load_1m(code)
    if df.empty:
        return code, None
    days = intraday.daily_from_1m(df)
    keep = [dd for dd, gg in df.groupby(df.index.date) if len(gg) == 390 and dd in days.index
            and days.loc[dd, "atr_prev"] > 0 and days.loc[dd, "iv"] > 0]
    df = df[np.isin(df.index.date, keep)]
    if not len(df):
        return code, None
    uniq, dayid = np.unique(df.index.date, return_inverse=True)
    starts = np.searchsorted(dayid, np.arange(len(uniq)))
    dinfo = dict(dayid=dayid, day_start=starts, day_end=np.r_[starts[1:] - 1, len(df) - 1],
                 atr=days.loc[uniq, "atr_prev"].values.astype(float), iv=days.loc[uniq, "iv"].values.astype(float))
    rng = np.random.default_rng(zlib.crc32(code.encode()))
    rows = []
    for d in (1, -1):
        rows += run_dir(df, dinfo, d, rng)
    t = pd.DataFrame(rows, columns=["vid", "rand", "di", "am", "d", "risk_atr", "R", "opt", "hit"])
    if t.empty:
        return code, None
    t["date"] = np.array([str(x) for x in uniq])[t["di"].values]
    t["ok"] = t.risk_atr >= MIN_RISK
    t["Rn"] = t.R - COST / t.risk_atr
    t["win"] = (t.R > 0).astype(int)
    t["hit"] = t.hit.astype(int)
    t["has_opt"] = t.opt.notna().astype(int)
    t["opt"] = t.opt.fillna(0.0)
    t["Rn_c"] = t.Rn.clip(-20, None)                                # 极小止损的 Rn 可以到 -100，单独留一列截断版
    t["n"] = 1
    g = t.groupby(["vid", "rand", "date", "am", "d", "ok"], as_index=False)[
        ["n", "R", "Rn", "Rn_c", "opt", "has_opt", "win", "hit", "risk_atr"]].sum()
    g["code"] = code
    return code, g


def run(codes=None, log=print):
    codes = codes or [c for c in intraday.all_codes() if c not in SKIP]
    parts = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for code, g in ex.map(run_code, codes):
            if g is not None:
                parts.append(g)
                log(f"{code}: {int(g[g.rand == 0].n.sum())} 笔")
    df = pd.concat(parts, ignore_index=True)
    pd.to_pickle(df, OUT)
    return df


# ================================================================ 汇总

def _ci(g, col, ncol="n"):
    s = g.groupby("date")[[col, ncol]].sum()
    s = s[s[ncol] > 0]
    if s[ncol].sum() == 0:
        return np.nan, np.nan, np.nan
    m = s[col].sum() / s[ncol].sum()
    per = s[col] / s[ncol]
    w = s[ncol] / s[ncol].sum()
    se = np.sqrt(((per - m) ** 2 * w ** 2).sum()) if len(per) > 1 else np.nan
    return m, m - 1.96 * se, m + 1.96 * se


def _stats(g):
    n = int(g.n.sum())
    if n < 20:
        return None
    m, lo, hi = _ci(g, "Rn")
    mc, _, _ = _ci(g, "Rn_c")
    om, olo, ohi = _ci(g, "opt", "has_opt") if g.has_opt.sum() > 20 else (np.nan,) * 3
    return dict(n=n, days=int(g.date.nunique()), per_day=n / max(g.date.nunique(), 1), R=g.R.sum() / n, Rn=m, Rn_lo=lo, Rn_hi=hi,
                Rn_c=mc, win=g.win.sum() / n, hit=g.hit.sum() / n, risk_atr=g.risk_atr.sum() / n,
                opt=om, opt_lo=olo, opt_hi=ohi,
                long=(g[g.d == 1].Rn.sum() / max(g[g.d == 1].n.sum(), 1)), short=(g[g.d == -1].Rn.sum() / max(g[g.d == -1].n.sum(), 1)))


def summary(df):
    df = df.copy()
    df["oos"] = df.date >= SPLIT
    df["year"] = df.date.str[:4]
    out = []
    for vi, v in enumerate(VARIANTS):
        for rnd in (0, 1):
            g0 = df[(df.vid == vi) & (df.rand == rnd)]
            if not len(g0):
                continue
            for sess in ("all", "am"):
                g1 = g0 if sess == "all" else g0[g0.am]
                for rk in ("all", "ok"):
                    g2 = g1 if rk == "all" else g1[g1.ok]
                    rec = dict(vid=vi, strat=v[0], tf=v[1], candle=v[2], mode=v[3], rand=rnd, sess=sess, risk=rk,
                               full=_stats(g2), oos=_stats(g2[g2.oos]), ins=_stats(g2[~g2.oos]))
                    if sess == "all" and rk == "ok":
                        rec["years"] = {y: (lambda s: s and dict(n=s["n"], Rn=s["Rn"], opt=s["opt"], win=s["win"]))(_stats(gy))
                                        for y, gy in g2.groupby("year")}
                    out.append(rec)
    return out


NAME = {"ce": "1 分钟视频（CE + ZLSMA）", "frac": "3 分钟视频（SMMA + 分形 + RSI）"}
CAND = {"real": "普通K线", "ha": "HA 出信号、真实成交", "hafake": "HA 价格（回放看到的）"}
MODE = {"fixed": "固定目标", "half": "一半目标 + 一半右侧"}


def _f(x, pct=False):
    if x is None or x != x:
        return "—"
    return f"{x * 100:+.1f}%" if pct else f"{x:+.3f}"


def report(s, path=None):
    import datetime as _dt
    today = _dt.date.today().isoformat()
    path = path or (HERE.parent / "报告" / f"80胜率剥头皮回测_{today}.md")
    idx = {(r["vid"], r["rand"], r["sess"], r["risk"]): r for r in s}
    L = [f"# 「80% 胜率 最佳剥头皮策略」两个视频回测 · {today}", "",
         "来源：B 站「Crypto-阿洋」BV1F5yzzGEZ3（1 分钟：Chandelier Exit + ZLSMA，平均K线图）、BV1JQy2zFEzV（3 分钟：3 条 SMMA + 分形 + RSI）。"
         "程序 `程序/scalp80_bt.py`，规则口径写在程序开头；规则照视频写，没有按数据调参数。",
         "34 只股票（ANT、CRWV 除外）2019-01 起 1 分钟K，9:36–15:30 开仓、收盘前平；多空都做。",
         "R 已扣成本（每笔 0.01 倍日线 ATR）；期权 = 平值约 3 天到期的模型估算，扣价差。95% 区间按日期聚合。",
         "「胜率」= 这笔最后赚钱的比例（不扣成本），可以直接和视频里的 55% / 58% 比。", ""]
    for rk, title in (("ok", f"止损 ≥ {MIN_RISK} 倍日线 ATR 的笔（主表）"), ("all", "全部笔（含止损只有几美分的）")):
        L += [f"## {title}：2019-01 起全部", "",
              "| 视频 | 周期 | K 线 | 出场 | 笔数 | 每天每只 | 胜率 | 到目标 | 毛 R | 扣成本 R（95% 区间） | 期权每笔（95% 区间） | 随机对照 R | 随机对照期权 | 样本外 R | 样本外期权 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for vi, v in enumerate(VARIANTS):
            r = idx.get((vi, 0, "all", rk))
            if not r or not r["full"]:
                continue
            a, b = r["full"], r["oos"]
            rr = idx.get((vi, 1, "all", rk))
            ra = rr["full"] if rr else None
            ncode = 34 - len(SKIP)
            L.append(f"| {NAME[v[0]]} | {v[1]} 分钟 | {CAND[v[2]]} | {MODE[v[3]]} | {a['n']} | {a['n'] / a['days'] / ncode:.1f} | "
                     f"{a['win'] * 100:.0f}% | {a['hit'] * 100:.0f}% | {_f(a['R'])} | {_f(a['Rn'])}（{_f(a['Rn_lo'])} ~ {_f(a['Rn_hi'])}） | "
                     f"{_f(a['opt'], True)}（{_f(a['opt_lo'], True)} ~ {_f(a['opt_hi'], True)}） | "
                     f"{_f(ra['Rn']) if ra else '—'} | {_f(ra['opt'], True) if ra else '—'} | "
                     f"{_f(b['Rn']) if b else '—'} | {_f(b['opt'], True) if b else '—'} |")
        L.append("")
    L += ["## 早盘（11:00 前开仓，止损 ≥ 0.02 ATR）", "",
          "| 视频 | 周期 | K 线 | 出场 | 笔数 | 胜率 | 扣成本 R（95% 区间） | 期权每笔 | 随机对照 R | 随机对照期权 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for vi, v in enumerate(VARIANTS):
        r = idx.get((vi, 0, "am", "ok"))
        if not r or not r["full"] or v[2] == "hafake":
            continue
        a = r["full"]
        rr = idx.get((vi, 1, "am", "ok"))
        ra = rr["full"] if rr else None
        L.append(f"| {NAME[v[0]]} | {v[1]} 分钟 | {CAND[v[2]]} | {MODE[v[3]]} | {a['n']} | {a['win'] * 100:.0f}% | "
                 f"{_f(a['Rn'])}（{_f(a['Rn_lo'])} ~ {_f(a['Rn_hi'])}） | {_f(a['opt'], True)} | "
                 f"{_f(ra['Rn']) if ra else '—'} | {_f(ra['opt'], True) if ra else '—'} |")
    L += ["", "## 逐年（普通K线、固定目标、止损 ≥ 0.02 ATR）", ""]
    yrs = sorted({y for r in s if r.get("years") for y in r["years"]})
    L += ["| 视频 | 周期 | " + " | ".join(yrs) + " |", "|---|---|" + "---|" * len(yrs)]
    for vi, v in enumerate(VARIANTS):
        if v[2] != "real" or v[3] != "fixed":
            continue
        r = idx.get((vi, 0, "all", "ok"))
        if not r or not r.get("years"):
            continue
        cells = []
        for y in yrs:
            x = r["years"].get(y)
            cells.append(f"{_f(x['Rn'])} / {_f(x['opt'], True)}" if x else "—")
        L.append(f"| {NAME[v[0]]} | {v[1]} 分钟 | " + " | ".join(cells) + " |")
    L += ["", "（每格：扣成本 R / 期权每笔）", ""]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--test":
        code = args[1] if len(args) > 1 else "NVDA"
        _, g = run_code(code)
        s = summary(g)
        for r in s:
            if r["sess"] == "all" and r["risk"] == "ok" and r["full"]:
                f = r["full"]
                print(VARIANTS[r["vid"]], "随机" if r["rand"] else "信号", f["n"], f"胜率 {f['win']:.0%}",
                      f"R {f['R']:+.3f} Rn {f['Rn']:+.3f} 期权 {f['opt'] * 100:+.1f}%")
        sys.exit(0)
    df = run()
    s = summary(df)
    (HERE / "data" / ".scalp80_bt_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1, default=float),
                                                            encoding="utf-8")
    print(int(df[df.rand == 0].n.sum()), "笔 ->", OUT)
    print("报告 ->", report(s))
