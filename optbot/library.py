"""策略库 + 行情分类 + 止损实验（控制台「行情与策略库」页的数据来源）。

做的事：
  1. 行情分类：每天收盘后，把 SPY / QQQ 归到 7 种行情之一（只用当天及以前的数据），外加若干标签。
  2. 策略库：这几天学过的所有规则 + 主流规则，全部写成同一种格式：收盘出信号，下一个交易日开盘成交。
     每条都在 SPY/QQQ 2010→今 上回测，给出：年化、回撤、在场时间、样本内外、随机入场分位、
     以及「在每一种行情里」的表现。
  3. 今日状态：每条策略现在是持有、空仓，还是今天收盘刚触发、明天开盘执行。
  4. 止损实验：同一个入场（趋势突破 / 低吸），换 13 种止损，看各自的代价。

这里只做研究和展示，不下单。机器人本身的交易规则仍然只由 config.toml + strategy.py 决定。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backtest" / "library.json"
COST = 0.0001          # 单边成本（ETF 免佣，主要是滑点）；来回 0.02%
START = "2010-01-01"
SPLIT = "2018-01-01"
UNDERLYINGS = ("US.SPY", "US.QQQ")


# ================================================================ 指标

def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    k, out, e = 2 / (n + 1), [], None
    for v in x:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def wilder(x, n):
    out, a = [], None
    for v in x:
        a = v if a is None else (a * (n - 1) + v) / n
        out.append(a)
    return out


def stdev(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out


def rsi(c, n):
    up = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    dn = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    au, ad = wilder(up, n), wilder(dn, n)
    return [None if i < n else (100.0 if ad[i] == 0 else 100 - 100 / (1 + au[i] / ad[i])) for i in range(len(c))]


def true_range(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def atr(h, l, c, n=14):
    return wilder(true_range(h, l, c), n)


def adx(h, l, c, n=14):
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if h[i] - h[i - 1] > l[i - 1] - l[i] else 0.0 for i in range(1, len(c))]
    mdm = [0.0] + [max(l[i - 1] - l[i], 0) if l[i - 1] - l[i] > h[i] - h[i - 1] else 0.0 for i in range(1, len(c))]
    tr = wilder(true_range(h, l, c), n)
    p, m = wilder(pdm, n), wilder(mdm, n)
    pdi = [100 * p[i] / tr[i] if tr[i] else 0 for i in range(len(c))]
    mdi = [100 * m[i] / tr[i] if tr[i] else 0 for i in range(len(c))]
    dx = [100 * abs(pdi[i] - mdi[i]) / (pdi[i] + mdi[i]) if pdi[i] + mdi[i] else 0 for i in range(len(c))]
    return wilder(dx, n), pdi, mdi


def kdj(h, l, c, n=9):
    k = d = 50.0
    K, D = [], []
    for i in range(len(c)):
        lo, hi = min(l[max(0, i - n + 1):i + 1]), max(h[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi == lo else (c[i] - lo) / (hi - lo) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        K.append(k)
        D.append(d)
    return K, D


def wma(x, n):
    out = [None] * len(x)
    w = n * (n + 1) / 2
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        if None in seg:
            continue
        out[i] = sum(v * (k + 1) for k, v in enumerate(seg)) / w
    return out


def hma(x, n):
    a, b = wma(x, n // 2), wma(x, n)
    diff = [None if a[i] is None or b[i] is None else 2 * a[i] - b[i] for i in range(len(x))]
    first = next(i for i, v in enumerate(diff) if v is not None)
    tail = wma(diff[first:], int(math.sqrt(n)))
    return [None] * first + tail


def psar(h, l, step=0.02, mx=0.2):
    n = len(h)
    up, sar, ep, af = True, l[0], h[0], step
    out, trend = [sar], [True]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, mx)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, mx)
        out.append(sar)
        trend.append(up)
    return out, trend


def supertrend(h, l, c, n=10, m=3.0):
    a = atr(h, l, c, n)
    up_b, dn_b, line, trend = [], [], [], []
    for i in range(len(c)):
        mid = (h[i] + l[i]) / 2
        ub, lb = mid + m * a[i], mid - m * a[i]
        if i:
            lb = max(lb, dn_b[-1]) if c[i - 1] > dn_b[-1] else lb
            ub = min(ub, up_b[-1]) if c[i - 1] < up_b[-1] else ub
            t = trend[-1]
            if t and c[i] < dn_b[-1]:
                t = False
            elif not t and c[i] > up_b[-1]:
                t = True
        else:
            t = True
        up_b.append(ub)
        dn_b.append(lb)
        trend.append(t)
        line.append(lb if t else ub)
    return line, trend


def atr_trail(c, a, m):
    """收盘价版 ATR 追踪线：在价格下方 m×ATR，只上不下；收盘跌破就翻空（反之亦然）。"""
    line, trend = [c[0] - m * a[0]], [True]
    for i in range(1, len(c)):
        prev, t = line[-1], trend[-1]
        if t:
            if c[i] < prev:
                t, nl = False, c[i] + m * a[i]
            else:
                nl = max(prev, c[i] - m * a[i])
        else:
            if c[i] > prev:
                t, nl = True, c[i] - m * a[i]
            else:
                nl = min(prev, c[i] + m * a[i])
        line.append(nl)
        trend.append(t)
    return line, trend


def roll_max(x, n):
    return [max(x[max(0, i - n + 1):i + 1]) for i in range(len(x))]


def roll_min(x, n):
    return [min(x[max(0, i - n + 1):i + 1]) for i in range(len(x))]


def trailing_pct(x, n=252):
    """x[i] 在过去 n 天里的分位（0~1），只用过去数据。"""
    out = [None] * len(x)
    for i in range(len(x)):
        if x[i] is None or i < 60:
            continue
        w = [v for v in x[max(0, i - n + 1):i + 1] if v is not None]
        out[i] = sum(v <= x[i] for v in w) / len(w)
    return out


class Bars:
    """一只标的的日线 + 按需计算、缓存的指标。"""

    def __init__(self, rows, vix=None):
        self.d = [r["date"] for r in rows]
        self.o = [r["open"] for r in rows]
        self.h = [r["high"] for r in rows]
        self.l = [r["low"] for r in rows]
        self.c = [r["close"] for r in rows]
        self.v = [r.get("volume", 0) for r in rows]
        self.n = len(self.c)
        vix = vix or {}
        last = None
        self.vix = []
        for day in self.d:
            last = vix.get(day, last)
            self.vix.append(last)
        self._k = {}

    def get(self, key, fn):
        if key not in self._k:
            self._k[key] = fn()
        return self._k[key]

    def sma(self, n): return self.get(("sma", n), lambda: sma(self.c, n))
    def ema(self, n): return self.get(("ema", n), lambda: ema(self.c, n))
    def rsi(self, n): return self.get(("rsi", n), lambda: rsi(self.c, n))
    def atr(self, n=14): return self.get(("atr", n), lambda: atr(self.h, self.l, self.c, n))
    def std(self, n): return self.get(("std", n), lambda: stdev(self.c, n))
    def adx(self): return self.get(("adx",), lambda: adx(self.h, self.l, self.c, 14))
    def kdj(self): return self.get(("kdj",), lambda: kdj(self.h, self.l, self.c, 9))
    def hma(self, n): return self.get(("hma", n), lambda: hma(self.c, n))
    def psar(self): return self.get(("psar",), lambda: psar(self.h, self.l))
    def st(self): return self.get(("st",), lambda: supertrend(self.h, self.l, self.c, 10, 3.0))
    def hh(self, n): return self.get(("hh", n), lambda: roll_max(self.h, n))
    def ll(self, n): return self.get(("ll", n), lambda: roll_min(self.l, n))
    def chand(self): return self.get(("chand",), lambda: [self.hh(22)[i] - 3 * self.atr(22)[i] for i in range(self.n)])

    def boll(self, n=20, k=2.0):
        def f():
            m, s = self.sma(n), self.std(n)
            return ([None if m[i] is None else m[i] + k * s[i] for i in range(self.n)], m,
                    [None if m[i] is None else m[i] - k * s[i] for i in range(self.n)])
        return self.get(("boll", n, k), f)

    def macd(self):
        def f():
            dif = [a - b for a, b in zip(ema(self.c, 12), ema(self.c, 26))]
            return dif, ema(dif, 9)
        return self.get(("macd",), f)

    def wvf(self):
        """Williams VIX Fix：离 22 天最高收盘跌了多深（恐慌度）。"""
        def f():
            hc = roll_max(self.c, 22)
            return [(hc[i] - self.l[i]) / hc[i] * 100 for i in range(self.n)]
        return self.get(("wvf",), f)

    def start_index(self, start=START, warm=260):
        return max(warm, next(i for i, t in enumerate(self.d) if t >= start))


# ================================================================ 行情分类

REGIMES = [
    {"id": "up_strong", "name": "强势上涨", "desc": "ADX ≥ 25（趋势强）、+DI > −DI、收盘在 50 日线上方。单边上攻，回调浅。"},
    {"id": "up_mild", "name": "温和上涨", "desc": "收盘在 50 日、200 日线上方，50 日线向上，ADX 18–25。慢牛、边涨边震。"},
    {"id": "range_narrow", "name": "窄幅震荡", "desc": "没有明确趋势，布林带宽处在过去一年的低 35%。波动小、来回磨。"},
    {"id": "range_wide", "name": "宽幅震荡", "desc": "没有明确趋势，但布林带宽处在过去一年的高 65%。大起大落、方向反复。"},
    {"id": "down_mild", "name": "温和下跌", "desc": "收盘在 50 日线下方、50 日线向下，ADX 不到 25。阴跌、反弹无力。"},
    {"id": "down_strong", "name": "强势下跌", "desc": "ADX ≥ 25、−DI > +DI、收盘在 50 日线下方。单边下杀。"},
    {"id": "panic", "name": "恐慌急跌", "desc": "VIX ≥ 30，或离 60 日高点跌超 10% 且波动率处在一年内最高 10%。"},
]
RID = [r["id"] for r in REGIMES]

TAGS = {
    "squeeze": "波动收缩（布林带宽一年内最低 10%，常在大行情之前）",
    "overbought": "超买（RSI14 ≥ 70）",
    "oversold": "超卖（RSI14 ≤ 30）",
    "high_vol": "高波动（VIX ≥ 25）",
    "below_200": "跌破 200 日线（长期趋势转弱）",
    "new_high": "创一年新高",
}


def classify(b: Bars):
    up, mid, lo = b.boll(20, 2.0)
    bw = [None if mid[i] is None else (up[i] - lo[i]) / mid[i] for i in range(b.n)]
    bwp = trailing_pct(bw)
    a14 = b.atr(14)
    atrp = trailing_pct([a14[i] / b.c[i] for i in range(b.n)])
    ad, pdi, mdi = b.adx()
    s50, s200, r14 = b.sma(50), b.sma(200), b.rsi(14)
    hi60, hi252 = roll_max(b.c, 60), roll_max(b.c, 252)
    out = []
    for i in range(b.n):
        if s200[i] is None or bwp[i] is None or atrp[i] is None:
            out.append(None)
            continue
        vix = b.vix[i]
        dd60 = b.c[i] / hi60[i] - 1
        slope = s50[i] - s50[i - 10]
        if (vix is not None and vix >= 30) or (dd60 <= -0.10 and atrp[i] >= 0.9):
            r = "panic"
        elif ad[i] >= 25 and pdi[i] > mdi[i] and b.c[i] > s50[i]:
            r = "up_strong"
        elif ad[i] >= 25 and mdi[i] > pdi[i] and b.c[i] < s50[i]:
            r = "down_strong"
        elif ad[i] >= 18 and b.c[i] > s50[i] and slope > 0 and b.c[i] > s200[i]:
            r = "up_mild"
        elif ad[i] >= 18 and b.c[i] < s50[i] and slope < 0:
            r = "down_mild"
        else:
            r = "range_narrow" if bwp[i] <= 0.35 else "range_wide"
        tags = []
        if bwp[i] <= 0.10:
            tags.append("squeeze")
        if r14[i] is not None and r14[i] >= 70:
            tags.append("overbought")
        if r14[i] is not None and r14[i] <= 30:
            tags.append("oversold")
        if vix is not None and vix >= 25:
            tags.append("high_vol")
        if b.c[i] < s200[i]:
            tags.append("below_200")
        if b.c[i] >= hi252[i]:
            tags.append("new_high")
        out.append({"r": r, "tags": tags, "adx": round(ad[i], 1), "bw_pct": round(bwp[i], 2),
                    "atr_pct": round(atrp[i], 2), "vix": vix, "rsi": round(r14[i], 1),
                    "vs50": round(b.c[i] / s50[i] - 1, 4), "vs200": round(b.c[i] / s200[i] - 1, 4),
                    "dd60": round(dd60, 4)})
    return out


# ================================================================ 统一的执行器：收盘出信号 → 次日开盘成交

def machine(b: Bars, target, start=None):
    """target(i, pos) 返回收盘后想要的仓位：1 多 / 0 空仓 / -1 空。
    pos 是当前持仓 dict（含 side/e/entry/sig/hi/lo/days，以及策略自己存的状态）。
    返回 (trades, 当前持仓, 明天开盘待执行的目标仓位或 None)。"""
    n = b.n
    start = b.start_index() if start is None else start
    trades, pos, pending = [], None, None
    for i in range(start, n):
        if pos and i >= pos["e"]:
            pos["hi"] = max(pos["hi"], b.h[i])
            pos["lo"] = min(pos["lo"], b.l[i])
            pos["hc"] = max(pos["hc"], b.c[i])
            pos["days"] = i - pos["e"] + 1
        want = target(i, pos)
        cur = pos["side"] if pos else 0
        if want == cur:
            continue
        if i == n - 1:
            pending = want
            break
        x = i + 1
        if pos:
            trades.append(_trade(pos, x, b.o[x]))
            pos = None
        if want:
            pos = {"side": want, "e": x, "entry": b.o[x], "sig": i, "hi": b.o[x], "lo": b.o[x],
                   "hc": b.o[x], "days": 0}
    return trades, pos, pending


def _trade(pos, x, px, open_=False):
    return {"side": pos["side"], "e": pos["e"], "entry": pos["entry"], "x": x, "exit": px, "open": open_}


def state_rule(enter, exit_, side=1):
    """把「入场条件 / 出场条件」写成 target 函数。"""
    def t(i, pos):
        if pos is None:
            return side if enter(i) else 0
        return 0 if exit_(i, pos) else side
    return t


def flip_rule(direction):
    """direction(i) → 1/0/-1，直接就是目标仓位。"""
    return lambda i, pos: direction(i)


# ================================================================ 策略定义

def _g(x, i):
    return x[i] is not None


def s_buyhold(b):
    return machine(b, lambda i, p: 1)


def s_ma_cross(b):
    f, s = b.sma(10), b.sma(20)
    return machine(b, flip_rule(lambda i: 1 if f[i] > s[i] else 0))


def s_ema_cross(b):
    f, s = b.ema(10), b.ema(20)
    return machine(b, flip_rule(lambda i: 1 if f[i] > s[i] else 0))


def s_macd(b):
    dif, dea = b.macd()
    return machine(b, flip_rule(lambda i: 1 if dif[i] > dea[i] else 0))


def s_ma200(b):
    s = b.sma(200)
    return machine(b, flip_rule(lambda i: 1 if b.c[i] > s[i] else 0))


def s_tsmom(b):
    return machine(b, flip_rule(lambda i: 1 if b.c[i] > b.c[i - 252] else 0))


def s_kunge(b):
    """机器人当前规则在正股上的翻译：出信号就按方向持有 30 个交易日（≈ 45DTE 到 14DTE 的持有期）。"""
    m5, m10, m20, m60, m120, r = b.sma(5), b.sma(10), b.sma(20), b.sma(60), b.sma(120), b.rsi(14)

    def sig(i):
        above = b.c[i] > m120[i]
        if above and (r[i] <= 35 or (m5[i] > m10[i] > m20[i] > m60[i] and r[i] < 65)):
            return 1
        if not above and (r[i] >= 65 or (m5[i] < m10[i] < m20[i] < m60[i] and r[i] > 35)):
            return -1
        return 0

    def t(i, pos):
        if pos is None:
            return sig(i)
        return 0 if pos["days"] >= 30 else pos["side"]
    return machine(b, t)


def s_rsi35(b):
    r = b.rsi(14)
    return machine(b, state_rule(lambda i: r[i] <= 35, lambda i, p: p["days"] >= 20))


def s_supertrend(b):
    _, tr = b.st()
    return machine(b, flip_rule(lambda i: 1 if tr[i] else 0))


def s_atr_trail(b):
    line, tr = b.get(("atrtrail", 1), lambda: atr_trail(b.c, b.atr(14), 1.0))
    return machine(b, flip_rule(lambda i: 1 if tr[i] else 0))


def s_chandelier(b):
    ch = b.chand()
    lo_line = [b.ll(22)[i] + 3 * b.atr(22)[i] for i in range(b.n)]
    state = {"t": 1}

    def d(i):
        if b.c[i] < ch[i - 1]:
            state["t"] = 0
        elif b.c[i] > lo_line[i - 1]:
            state["t"] = 1
        return state["t"]
    return machine(b, flip_rule(d))


def s_psar(b):
    _, tr = b.psar()
    return machine(b, flip_rule(lambda i: 1 if tr[i] else 0))


def s_hma(b):
    h = b.hma(40)
    return machine(b, flip_rule(lambda i: 1 if h[i] > h[i - 2] else 0))


def s_adx(b):
    ad, p, m = b.adx()
    return machine(b, state_rule(lambda i: ad[i] >= 25 and p[i] > m[i], lambda i, pos: ad[i] < 20 or m[i] > p[i]))


def _turtle(b, n_in, n_out):
    hh, ll = b.hh(n_in), b.ll(n_in)
    xh, xl = b.hh(n_out), b.ll(n_out)
    a = b.atr(20)

    def t(i, pos):
        if pos is None:
            if b.c[i] > hh[i - 1]:
                return 1
            if b.c[i] < ll[i - 1]:
                return -1
            return 0
        s = pos["side"]
        n0 = a[pos["sig"]]
        if s == 1 and (b.c[i] < xl[i - 1] or b.c[i] < pos["entry"] - 2 * n0):
            return 0
        if s == -1 and (b.c[i] > xh[i - 1] or b.c[i] > pos["entry"] + 2 * n0):
            return 0
        return s
    return machine(b, t)


def s_turtle20(b): return _turtle(b, 20, 10)
def s_turtle55(b): return _turtle(b, 55, 20)


def s_boll_break(b):
    up, mid, _ = b.boll()
    return machine(b, state_rule(lambda i: b.c[i] > up[i], lambda i, p: b.c[i] < mid[i]))


def s_box_break(b):
    m = b.sma(20)
    return machine(b, state_rule(lambda i: b.c[i] > m[i] * 1.03, lambda i, p: b.c[i] < m[i]))


def s_box_bottom(b):
    m = b.sma(20)
    return machine(b, state_rule(lambda i: b.c[i] <= m[i] * 0.97, lambda i, p: b.c[i] >= m[i] * 1.03))


def s_boll_bottom(b):
    up, _, lo = b.boll()
    return machine(b, state_rule(lambda i: b.c[i] <= lo[i], lambda i, p: b.c[i] >= up[i]))


def s_rsi_30_70(b):
    r = b.rsi(14)
    return machine(b, state_rule(lambda i: r[i] < 30, lambda i, p: r[i] > 70))


def s_rsi2(b):
    r, s200, s5 = b.rsi(2), b.sma(200), b.sma(5)
    return machine(b, state_rule(lambda i: b.c[i] > s200[i] and r[i] < 10, lambda i, p: b.c[i] > s5[i]))


def s_kdj(b):
    K, D = b.kdj()
    return machine(b, state_rule(lambda i: K[i] < 20 and D[i] < 20 and K[i - 1] <= D[i - 1] and K[i] > D[i],
                                 lambda i, p: K[i] > 80 and D[i] > 80 and K[i - 1] >= D[i - 1] and K[i] < D[i]))


def s_rsi_dual(b):
    f, s = b.rsi(6), b.rsi(24)
    return machine(b, state_rule(lambda i: f[i] < 50 and s[i] < 50 and f[i - 1] <= s[i - 1] and f[i] > s[i],
                                 lambda i, p: f[i] > 50 and s[i] > 50 and f[i - 1] >= s[i - 1] and f[i] < s[i]))


def s_panic(b):
    w = b.wvf()
    m, sd = sma(w, 20), stdev(w, 20)
    mx = roll_max(w, 50)

    def enter(i):
        return w[i] >= m[i] + 2 * sd[i] or w[i] >= 0.85 * mx[i - 1]

    def exit_(i, p):
        return w[i] < m[i] or b.c[i] <= p["entry"] * 0.95 or b.c[i] >= p["entry"] * 1.15
    return machine(b, state_rule(enter, exit_))


def s_bear_trend(b):
    e20, e21, e55, e144 = b.ema(20), b.ema(21), b.ema(55), b.ema(144)
    return machine(b, state_rule(lambda i: e21[i] < e55[i] < e144[i] and b.c[i] < e20[i],
                                 lambda i, p: b.c[i] > e20[i], side=-1))


def s_ma200_short(b):
    s = b.sma(200)
    return machine(b, flip_rule(lambda i: -1 if b.c[i] < s[i] else 0))


def s_boll_upper_short(b):
    up, mid, _ = b.boll()
    return machine(b, state_rule(lambda i: b.c[i] >= up[i], lambda i, p: b.c[i] <= mid[i], side=-1))


# ---- 用到以前写好的回测模块（盘中止损 / 止盈的打法）

def _from_pa(name):
    def f(b: Bars, rows):
        from . import pa_backtest as pa
        pb = pa.Bars(rows)
        fn = pa.SIGNALS[name][1]
        ts = pa.simulate(pb, fn, START)
        trades = []
        n = b.n
        pos = None
        for t in ts:
            tr = {"side": 1 if t["side"] == "long" else -1, "e": t["i_entry"], "entry": t["entry"],
                  "x": t["i_exit"], "exit": t["exit"], "open": False}
            if t["reason"] == "到时平仓" and t["i_exit"] == n - 1 and t["i_entry"] + pa.MAX_DAYS - 1 > n - 1:
                tr["open"] = True
                pos = {"side": tr["side"], "e": tr["e"], "entry": tr["entry"], "days": n - tr["e"]}
            trades.append(tr)
        s = fn(pb, n - 1) if pos is None else None
        pending = (1 if s[0] == "long" else -1) if s else None
        if pos:
            trades = trades[:-1]
        return trades, pos, pending
    return f


def _from_h2(sides):
    def f(b: Bars, rows):
        from . import h2_backtest as h2
        ts = h2.run_h2(h2.Series(rows), START, False, sides=sides)
        trades, pos = [], None
        for t in ts:
            tr = {"side": 1 if t["side"] == "long" else -1, "e": t["e"], "entry": t["entry"],
                  "x": t["x"], "exit": t["exit"], "open": False}
            if t["why"] == "数据结束":
                pos = {"side": tr["side"], "e": tr["e"], "entry": tr["entry"], "days": b.n - tr["e"]}
            else:
                trades.append(tr)
        return trades, pos, None
    return f


FAMILIES = {
    "benchmark": "基准",
    "trend": "趋势跟踪",
    "breakout": "突破",
    "meanrev": "均值回归 / 低吸",
    "pa": "价格行为",
    "bear": "看跌 / 做空",
}

# fit：按规则本身的设计意图，它「应该」适合哪些行情（不是回测挑出来的）
STRATEGIES = [
    dict(id="buyhold", name="买入持有", family="benchmark", dir="多", source="基准", fn=s_buyhold,
         fit=["up_strong", "up_mild"],
         entry="第一天买入，一直不卖。", exit="不卖。", stop="无。",
         fears="长期熊市（2000–2002、2008 这类，回撤 50% 以上、几年才回本）。",
         options="长期看涨：远月深度实值看涨期权（LEAPS）可近似持股，但有时间损耗。"),
    dict(id="kunge", name="坤哥规则（机器人当前规则）", family="trend", dir="多空", source="期权坤哥", fn=s_kunge,
         fit=["up_mild", "up_strong", "down_mild", "down_strong"],
         entry="站上 MA120 且（RSI ≤ 35 或 均线多头排列且 RSI < 65）→ 看涨；跌破 MA120 镜像 → 看跌。",
         exit="正股版：持有 30 个交易日（≈ 期权从 45DTE 拿到 14DTE）。", stop="期权版：价差亏 50% 止损、赚 50% 止盈、14DTE 平仓。",
         fears="震荡市（信号来回切换）；期权版还怕买卖价差和小预算。",
         options="机器人里用牛市看涨价差 / 熊市看跌价差（现金账户做不了价差，只能单买 Call / Put）。"),
    dict(id="ma_cross", name="均线金叉（10/20）", family="trend", dir="多", source="VicAlpha E01", fn=s_ma_cross,
         fit=["up_strong", "up_mild"],
         entry="10 日线上穿 20 日线。", exit="10 日线下穿 20 日线（死叉就是止损）。", stop="无单独止损。",
         fears="窄幅震荡：两线缠绕、反复金叉死叉。", options="金叉后买 30–60 天、Delta 0.5 左右的看涨期权。"),
    dict(id="ema_cross", name="EMA 金叉（10/20）", family="trend", dir="多", source="VicAlpha E03", fn=s_ema_cross,
         fit=["up_strong", "up_mild"],
         entry="EMA10 上穿 EMA20（近期价格权重更大，更灵敏）。", exit="EMA10 下穿 EMA20。", stop="无单独止损。",
         fears="窄幅震荡。", options="同上。"),
    dict(id="macd", name="MACD 金叉（12/26/9）", family="trend", dir="多", source="VicAlpha E03", fn=s_macd,
         fit=["up_strong", "up_mild"],
         entry="DIF 上穿 DEA。", exit="DIF 下穿 DEA。", stop="无单独止损。",
         fears="窄幅震荡：快慢线贴得最紧，磨损最重。", options="同上。"),
    dict(id="ma200", name="200 日线过滤", family="trend", dir="多", source="主流（Faber 等）", fn=s_ma200,
         fit=["up_strong", "up_mild"],
         entry="收盘站上 200 日线。", exit="收盘跌破 200 日线。", stop="无单独止损。",
         fears="在 200 日线附近来回穿（2015、2016、2018 年那种）。",
         options="作为大方向过滤：线上只考虑看涨结构，线下只考虑看跌 / 观望。"),
    dict(id="tsmom", name="12 个月时间序列动量", family="trend", dir="多", source="学术（Moskowitz 等）", fn=s_tsmom,
         fit=["up_strong", "up_mild"],
         entry="收盘高于 252 个交易日前。", exit="收盘低于 252 个交易日前。", stop="无单独止损。",
         fears="V 形反转（2020 年 3 月那种），信号滞后很多。", options="同上，作为大方向过滤。"),
    dict(id="supertrend", name="SuperTrend（10, 3）", family="trend", dir="多", source="VicAlpha E04", fn=s_supertrend,
         fit=["up_strong", "up_mild"],
         entry="收盘突破 SuperTrend 上轨（线翻到价格下方）。", exit="收盘跌破下方的 SuperTrend 线。",
         stop="SuperTrend 线本身就是移动止损（3×ATR，只上不下）。",
         fears="震荡：离得远（3 ATR），每次被扫都亏得深。", options="翻多买看涨，翻空平仓；线的位置可当期权止损参考。"),
    dict(id="atr_trail", name="ATR 追踪线（1×ATR）", family="trend", dir="多", source="VicAlpha E04", fn=s_atr_trail,
         fit=["up_strong"],
         entry="收盘上穿追踪线。", exit="收盘跌破追踪线（价格下方 1×ATR，只上不下）。", stop="追踪线本身。",
         fears="震荡：离得近，被止损次数最多，但每刀浅。", options="短线方向单，止损很紧，适合单买期权的小仓位。"),
    dict(id="chandelier", name="吊灯止损翻转（22, 3）", family="trend", dir="多", source="VicAlpha E04", fn=s_chandelier,
         fit=["up_strong", "up_mild"],
         entry="收盘上穿 22 日最低 + 3ATR。", exit="收盘跌破 22 日最高 − 3ATR（吊在最高点下方）。", stop="吊灯线本身。",
         fears="窄幅盘整或波动骤增。", options="同上。"),
    dict(id="psar", name="抛物线 SAR", family="trend", dir="多", source="VicAlpha E05", fn=s_psar,
         fit=["up_strong"],
         entry="价格上穿 SAR。", exit="价格跌破 SAR（越涨越快地贴近价格）。", stop="SAR 点本身。",
         fears="大趋势里的正常回调（被甩下车再追高）。", options="短波段方向单。"),
    dict(id="hma", name="赫尔均线方向（40）", family="trend", dir="多", source="VicAlpha E05", fn=s_hma,
         fit=["up_strong", "up_mild"],
         entry="HMA40 比两天前高（拐头向上）。", exit="HMA40 比两天前低。", stop="无单独止损。",
         fears="无趋势震荡：频繁拐头。", options="同上。"),
    dict(id="adx", name="ADX 趋势确认", family="trend", dir="多", source="主流（Wilder）", fn=s_adx,
         fit=["up_strong"],
         entry="ADX ≥ 25 且 +DI > −DI。", exit="ADX < 20 或 −DI > +DI。", stop="无单独止损。",
         fears="ADX 滞后：确认时行情常已走了大半。", options="ADX 高时买方向期权，ADX 低时考虑卖方结构（需保证金）。"),
    dict(id="turtle20", name="海龟系统 1（20/10）", family="trend", dir="多空", source="海龟交易法", fn=s_turtle20,
         fit=["up_strong", "down_strong"],
         entry="收盘创 20 日新高做多 / 20 日新低做空。", exit="反向 10 日突破。", stop="入场价 ∓ 2N（N = 20 日 ATR）。",
         fears="单一品种的震荡；它本来是为 20+ 个期货品种同时做设计的。",
         options="新高买看涨 / 新低买看跌，2N 作为止损参考。"),
    dict(id="turtle55", name="海龟系统 2（55/20）", family="trend", dir="多空", source="海龟交易法", fn=s_turtle55,
         fit=["up_strong", "down_strong"],
         entry="收盘创 55 日新高做多 / 55 日新低做空。", exit="反向 20 日突破。", stop="入场价 ∓ 2N。",
         fears="同上，信号更少更慢。", options="同上。"),
    dict(id="h2", name="高二进场 + EMA20 移动止盈", family="pa", dir="多", source="熊猫交易讲堂", fn=_from_h2(("long",)),
         fit=["up_strong", "up_mild"], needs_rows=True,
         entry="EMA21>55>144 多头排列中，回调里第二次「高点高于前一根」，下一根突破信号 K 高点入场。",
         exit="整根 K 线跑到 EMA20 下方按收盘离场。", stop="回调最低点；EMA20 附近出现强底分型就上移。",
         fears="日线上信号少；样本外（2022 年后）失效。", options="入场买看涨，止损位当期权止损参考。"),
    dict(id="trend_pullback", name="趋势回踩 MA20", family="pa", dir="多空", source="杰明（价格行为）", fn=_from_pa("trend_pullback"),
         fit=["up_mild", "down_mild"], needs_rows=True,
         entry="200 日线向上时，回踩 MA20 收阳；向下时镜像做空。", exit="2R 止盈或 20 天到期。", stop="信号 K 低点 − 0.5ATR（盘中触发）。",
         fears="趋势末端的深回调。", options="回踩买看涨 / 反抽买看跌。"),
    dict(id="sweep", name="假突破 / 流动性扫荡", family="pa", dir="多空", source="杰明（价格行为）", fn=_from_pa("sweep"),
         fit=["range_narrow", "range_wide"], needs_rows=True,
         entry="刺破 20 日低点后收回 → 做多；刺破高点后收回 → 做空。", exit="2R 止盈或 20 天到期。", stop="刺破的极点外 0.5ATR。",
         fears="真突破（刺破后继续走）。", options="箱体边缘的反向单。"),
    dict(id="capitulation", name="投降反转", family="pa", dir="多空", source="杰明（价格行为）", fn=_from_pa("capitulation"),
         fit=["panic"], needs_rows=True,
         entry="放量长阴砸穿支撑后 5 天内，出现强势收复的大阳线。", exit="2R 止盈或 20 天到期。", stop="恐慌低点外 0.5ATR。",
         fears="下跌中继（反弹后继续跌）。", options="恐慌后买看涨，但隐含波动率很高，期权很贵。"),
    dict(id="boll_break", name="布林开口追涨", family="breakout", dir="多", source="VicAlpha E06", fn=s_boll_break,
         fit=["up_strong"],
         entry="收盘升破布林上轨（20, 2）。", exit="收盘跌回中轨。", stop="中轨就是止损。",
         fears="慢牛小波动：刚冲出上轨就回落，一刀一刀磨。", options="突破买看涨。"),
    dict(id="box_break", name="箱体突破（20 日线 +3%）", family="breakout", dir="多", source="VicAlpha E07", fn=s_box_break,
         fit=["up_strong"],
         entry="收盘高于 20 日线 3%。", exit="收盘跌回 20 日线。", stop="20 日线。",
         fears="固定百分比在高波动标的上信号过多。", options="突破买看涨。"),
    dict(id="squeeze", name="波动压缩后大 K 突破", family="breakout", dir="多空", source="杰明（价格行为）", fn=_from_pa("squeeze"),
         fit=["range_narrow"], needs_rows=True,
         entry="ATR10/ATR50 < 0.7（压缩）后，大 K 线收在 20 日高点上方（或低点下方）。", exit="2R 止盈或 20 天到期。",
         stop="突破 K 线另一端外 0.5ATR。", fears="假突破。",
         options="压缩期隐含波动率低，买跨式 / 宽跨式（同时买 Call 和 Put）赌大波动。"),
    dict(id="box_bottom", name="箱体底部低吸（20 日线 ±3%）", family="meanrev", dir="多", source="VicAlpha E07（A 股天梯榜第一）", fn=s_box_bottom,
         fit=["range_narrow", "range_wide", "up_mild"],
         entry="收盘 ≤ 20 日线 × 97%。", exit="收盘 ≥ 20 日线 × 103%。", stop="无（加止损的效果见「止损实验」）。",
         fears="单边下跌（接飞刀）；大牛市里大部分时间空仓。", options="下轨附近买看涨；或卖看跌（需保证金 / 现金担保）。"),
    dict(id="boll_bottom", name="布林下轨低吸", family="meanrev", dir="多", source="VicAlpha E06", fn=s_boll_bottom,
         fit=["range_narrow", "range_wide"],
         entry="收盘碰到布林下轨。", exit="收盘碰到布林上轨。", stop="无。",
         fears="沿下轨一路阴跌。", options="同上。"),
    dict(id="rsi_30_70", name="RSI 超跌反弹（30/70）", family="meanrev", dir="多", source="VicAlpha E08", fn=s_rsi_30_70,
         fit=["range_wide"],
         entry="RSI14 < 30。", exit="RSI14 > 70。", stop="无。",
         fears="单边行情：下跌时买早、上涨时卖早，然后长期空仓。", options="同上。"),
    dict(id="rsi35", name="RSI ≤ 35 回调做多（持有 20 天）", family="meanrev", dir="多", source="本项目信号研究", fn=s_rsi35,
         fit=["up_mild", "range_wide"],
         entry="RSI14 ≤ 35。", exit="持有 20 个交易日。", stop="无。",
         fears="熊市里的连续超卖。", options="研究里唯一样本内外都为正的期权信号（但 $30 预算下仍亏）。"),
    dict(id="rsi2", name="RSI(2) 两日超跌（Connors）", family="meanrev", dir="多", source="主流 / VicAlpha E08", fn=s_rsi2,
         fit=["up_mild", "range_narrow"],
         entry="收盘在 200 日线上方且 RSI2 < 10。", exit="收盘站上 5 日线。", stop="无。",
         fears="很少在场，收益薄。", options="持有期只有几天，期权的买卖价差占比太高，不适合。"),
    dict(id="kdj", name="KDJ 低位金叉", family="meanrev", dir="多", source="VicAlpha E08", fn=s_kdj,
         fit=["range_narrow", "range_wide"],
         entry="K、D 都 < 20 且 K 上穿 D。", exit="K、D 都 > 80 且 K 下穿 D。", stop="无。",
         fears="单边行情钝化。", options="同上。"),
    dict(id="rsi_dual", name="RSI 双线金叉（6/24）", family="meanrev", dir="多", source="VicAlpha E08", fn=s_rsi_dual,
         fit=["range_wide", "up_mild"],
         entry="两条 RSI 都 < 50 且快线上穿慢线。", exit="两条都 > 50 且快线下穿慢线（等掉头再卖，拿得更久）。", stop="无。",
         fears="极端单边。", options="同上。"),
    dict(id="panic", name="恐慌指数反转（WVF）", family="meanrev", dir="多", source="VicAlpha E09", fn=s_panic,
         fit=["panic", "range_wide"],
         entry="恐慌度（离 22 日最高收盘的跌幅）碰到自己的布林上轨或 50 日高位的 85%。",
         exit="恐慌度回到 20 日均值以下；或收盘 +15% 止盈。", stop="收盘 −5%。",
         fears="一路新高的主升浪里没有恐慌，长期不在场。", options="恐慌时 IV 很高，买期权很贵；卖看跌更合适但需要保证金。"),
    dict(id="bear_trend", name="空头排列做空（EMA20 离场）", family="bear", dir="空", source="熊猫（镜像）", fn=s_bear_trend,
         fit=["down_strong", "down_mild"],
         entry="EMA21 < 55 < 144 且收盘在 EMA20 下方。", exit="收盘站上 EMA20。", stop="EMA20 本身。",
         fears="空头排列里的暴力反弹；长期向上的指数做空天然逆风。", options="现金账户：买看跌期权（Put）。"),
    dict(id="ma200_short", name="跌破 200 日线做空", family="bear", dir="空", source="主流（镜像）", fn=s_ma200_short,
         fit=["down_strong", "down_mild", "panic"],
         entry="收盘跌破 200 日线。", exit="收盘站回 200 日线。", stop="200 日线。",
         fears="熊市里的反弹和 V 形反转。", options="买看跌；或持股时买保护性看跌。"),
    dict(id="boll_upper_short", name="布林上轨做空", family="bear", dir="空", source="均值回归（镜像）", fn=s_boll_upper_short,
         fit=["range_wide", "down_mild"],
         entry="收盘碰到布林上轨。", exit="收盘回到中轨。", stop="无。",
         fears="上涨趋势里沿上轨一路走（会被轧空）。", options="买看跌；或卖看涨价差（需保证金）。"),
    dict(id="l2", name="低二做空（熊猫镜像）", family="bear", dir="空", source="熊猫交易讲堂", fn=_from_h2(("short",)),
         fit=["down_strong", "down_mild"], needs_rows=True,
         entry="空头排列中反弹里第二次「低点低于前一根」，跌破信号 K 低点入场。", exit="整根 K 线在 EMA20 上方按收盘离场。",
         stop="反弹最高点，EMA20 附近强顶分型下移。", fears="指数长期向上，做空回测里一直亏。", options="买看跌。"),
]


# ================================================================ 评估

def daily_returns(b: Bars, trades, pos_open=None):
    """把交易摊成每天的收益和持仓方向。最后一笔未平仓按最新收盘盯市。"""
    n = b.n
    dr, side = [0.0] * n, [0] * n
    allt = list(trades)
    if pos_open:
        allt.append({"side": pos_open["side"], "e": pos_open["e"], "entry": pos_open["entry"],
                     "x": n - 1, "exit": b.c[n - 1], "open": True})
    for t in allt:
        s, e, x = t["side"], t["e"], t["x"]
        if e >= n:
            continue
        for j in range(e, x + 1):
            if j == e and j == x:
                r = t["exit"] / t["entry"] - 1
            elif j == e:
                r = b.c[j] / t["entry"] - 1
            elif j == x:
                r = t["exit"] / b.c[j - 1] - 1
            else:
                r = b.c[j] / b.c[j - 1] - 1
            dr[j] += s * r
            if j < x or t.get("open"):
                side[j] = s
        dr[e] -= COST
        if not t.get("open"):
            dr[x] -= COST
    return dr, side, allt


def perf(dr, i0, i1):
    eq = peak = 1.0
    mdd = 0.0
    for j in range(i0, i1 + 1):
        eq *= 1 + dr[j]
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    yrs = (i1 - i0 + 1) / 252
    return eq, (eq ** (1 / yrs) - 1 if eq > 0 else -1.0), mdd


def random_pctile(b: Bars, allt, i0, i1, real_mult, draws=300, seed=11):
    if not allt:
        return None
    rng = random.Random(seed)
    lens = [max(1, t["x"] - t["e"]) for t in allt]
    sides = [t["side"] for t in allt]
    free = (i1 - i0) - sum(lens)
    if free <= 0:
        return None
    sims = []
    for _ in range(draws):
        cuts = sorted(rng.randint(0, free) for _ in lens)
        order = list(zip(lens, sides))
        rng.shuffle(order)
        eq, cur, last = 1.0, i0, 0
        for cut, (L, s) in zip(cuts, order):
            cur += cut - last
            last = cut
            e, x = min(cur + 1, i1), min(cur + 1 + L, i1)
            eq *= 1 + s * (b.o[x] / b.o[e] - 1) - 2 * COST
            cur += L
        sims.append(eq)
    return round(sum(v < real_mult for v in sims) / len(sims), 3)


def regime_perf(dr, side, reg, i0, i1):
    """每种行情里：策略的年化（按这些天整体算，含空仓的 0 收益）、在场比例、天数。行情用前一天收盘时的分类。"""
    acc = {r: [0.0, 0, 0] for r in RID}
    for j in range(max(i0, 1), i1 + 1):
        g = reg[j - 1]
        if not g:
            continue
        a = acc[g["r"]]
        a[0] += math.log(max(1e-9, 1 + dr[j]))
        a[1] += 1
        a[2] += 1 if side[j] else 0
    return {r: {"ann": round(math.exp(v[0] / v[1] * 252) - 1, 4) if v[1] else None,
                "days": v[1], "in": round(v[2] / v[1], 3) if v[1] else None} for r, v in acc.items()}


def evaluate(b: Bars, reg, trades, pos, i0, i1, split_i, with_random=True):
    dr, side, allt = daily_returns(b, trades, pos)
    mult, cagr, mdd = perf(dr, i0, i1)
    _, cin, _ = perf(dr, i0, split_i - 1)
    _, cout, mout = perf(dr, split_i, i1)
    rets = [t["side"] * (t["exit"] / t["entry"] - 1) - 2 * COST for t in allt if not t.get("open")]
    in_days = sum(1 for j in range(i0, i1 + 1) if side[j])
    return {
        "n": len(allt), "per_year": round(len(allt) / ((i1 - i0 + 1) / 252), 1),
        "win": round(sum(r > 0 for r in rets) / len(rets), 3) if rets else None,
        "avg": round(st.mean(rets), 4) if rets else None,
        "worst": round(min(rets), 4) if rets else None,
        "avg_days": round(st.mean(max(1, t["x"] - t["e"]) for t in allt), 1) if allt else None,
        "exposure": round(in_days / (i1 - i0 + 1), 3),
        "mult": round(mult, 3), "cagr": round(cagr, 4), "mdd": round(mdd, 3),
        "cagr_in": round(cin, 4), "cagr_out": round(cout, 4), "mdd_out": round(mout, 3),
        "rand": random_pctile(b, allt, i0, i1, mult) if with_random else None,
        "regimes": regime_perf(dr, side, reg, i0, i1),
        "equity": _equity_series(b, dr, i0, i1),
    }


def _equity_series(b, dr, i0, i1, step=10):
    eq, out = 1.0, []
    for j in range(i0, i1 + 1):
        eq *= 1 + dr[j]
        if (j - i0) % step == 0 or j == i1:
            out.append([b.d[j], round(eq, 4)])
    return out


def live_state(b: Bars, trades, pos, pending):
    n = b.n
    st_ = {"position": 0, "pending": pending, "entry_date": None, "entry": None, "days": None, "pnl": None,
           "last_exit": None}
    if pos:
        st_.update(position=pos["side"], entry_date=b.d[pos["e"]] if pos["e"] < n else None,
                   entry=round(pos["entry"], 2), days=pos.get("days"),
                   pnl=round(pos["side"] * (b.c[-1] / pos["entry"] - 1), 4))
    closed = [t for t in trades if not t.get("open")]
    if closed:
        t = closed[-1]
        st_["last_exit"] = {"date": b.d[t["x"]], "ret": round(t["side"] * (t["exit"] / t["entry"] - 1), 4)}
    return st_


# ================================================================ 止损实验

STOPS = [
    dict(id="none", name="不加止损（原出场）",
         how="只靠入场规则自带的出场：突破入场 → 收盘跌破 50 日线；低吸入场 → 收盘回到 20 日线 +3%。",
         good="不会被正常波动洗出去。", bad="单笔亏损没有上限，靠扛。"),
    dict(id="fix5", name="固定 5%", how="收盘比买入价跌超 5%。", good="简单、亏损可预期。",
         bad="不看波动率：波动大时太紧、波动小时太松。"),
    dict(id="fix10", name="固定 10%", how="收盘比买入价跌超 10%。", good="很少被洗。", bad="一刀很深。"),
    dict(id="atr2", name="初始 2×ATR（海龟 2N）", how="买入价 − 2×ATR(20)，固定不动。",
         good="距离跟着波动率走。", bad="赚了钱也不上移，利润会还回去。"),
    dict(id="atr3trail", name="3×ATR 追踪", how="持仓以来最高收盘 − 3×ATR(14)，只上不下。",
         good="能锁利润，又不太容易被正常回调扫掉。", bad="回吐 3 个 ATR 才走。"),
    dict(id="chandelier", name="吊灯止损（22, 3）", how="22 日最高价 − 3×ATR(22)，持仓期间只上不下。",
         good="跟着新高往上提。", bad="窄幅盘整里容易被扫。"),
    dict(id="supertrend", name="SuperTrend 线", how="SuperTrend(10, 3) 翻到价格上方。",
         good="趋势里很稳。", bad="震荡里每次都亏得深。"),
    dict(id="psar", name="抛物线 SAR", how="价格跌破 SAR（越涨越贴近）。",
         good="趋势后段锁利润快。", bad="大趋势的正常回调就会被甩下车。"),
    dict(id="ema20", name="跌破 EMA20（熊猫）", how="收盘跌破 EMA20。", good="直观。", bad="震荡里反复。"),
    dict(id="donchian10", name="10 日低点（海龟出场）", how="收盘跌破前 10 日最低价。",
         good="结构清晰。", bad="窄幅箱体里频繁触发。"),
    dict(id="structure", name="结构止损（前 10 日低点）", how="买入前 10 日最低价，固定不动。",
         good="放在「市场证明你错了」的位置。", bad="离得可能很远。"),
    dict(id="time20", name="时间止损（20 天）", how="持有满 20 个交易日就走，不管盈亏。",
         good="不让资金卡在不动的仓位里。", bad="可能刚好卖在行情启动前。"),
    dict(id="breakeven", name="保本止损", how="浮盈超过 1×ATR 后，止损移到买入价。",
         good="赚过的单不让它变亏。", bad="正常回踩就被保本出局，错过后面的趋势。"),
]


def _stop_hit(b: Bars, sid, i, pos):
    c = b.c[i]
    a14 = b.atr(14)
    if sid == "none":
        return False
    if sid == "fix5":
        return c <= pos["entry"] * 0.95
    if sid == "fix10":
        return c <= pos["entry"] * 0.90
    if sid == "atr2":
        return c <= pos["entry"] - 2 * b.atr(20)[pos["sig"]]
    if sid == "atr3trail":
        return c <= pos["hc"] - 3 * a14[i]
    if sid == "chandelier":
        pos["ch"] = max(pos.get("ch", -1e9), b.chand()[i])
        return c < pos["ch"]
    if sid == "supertrend":
        return not b.st()[1][i]
    if sid == "psar":
        return not b.psar()[1][i]
    if sid == "ema20":
        return c < b.ema(20)[i]
    if sid == "donchian10":
        return c < b.ll(10)[i - 1]
    if sid == "structure":
        return c < b.ll(10)[pos["sig"]]
    if sid == "time20":
        return pos["days"] >= 20
    if sid == "breakeven":
        if pos["hc"] >= pos["entry"] + a14[pos["sig"]]:
            pos["be"] = True
        return pos.get("be", False) and c <= pos["entry"]
    return False


def stop_test(b: Bars, entry_kind, sid):
    m20, m50, hh20 = b.sma(20), b.sma(50), b.hh(20)
    if entry_kind == "breakout":
        enter = lambda i: b.c[i] > hh20[i - 1]
        native = lambda i, p: b.c[i] < m50[i]
    else:
        enter = lambda i: b.c[i] <= m20[i] * 0.97
        native = lambda i, p: b.c[i] >= m20[i] * 1.03
    armed = {"ok": True}

    def t(i, pos):
        if pos is None:
            if not armed["ok"]:
                if not enter(i):
                    armed["ok"] = True
                return 0
            return 1 if enter(i) else 0
        if native(i, pos):
            return 0
        if _stop_hit(b, sid, i, pos):
            armed["ok"] = False
            return 0
        return 1
    return machine(b, t)


# ================================================================ 期权结构（静态资料）

OPTION_STRUCTURES = [
    dict(name="买入看涨 Long Call", view="看涨", fit=["up_strong", "up_mild"], cash=True,
         loss="最多亏掉权利金", gain="理论上不封顶",
         note="方向对还得快：时间损耗每天在吃。选 30–60 天、Delta 0.4–0.6。$50 只够买很便宜的虚值合约（彩票）。"),
    dict(name="买入看跌 Long Put", view="看跌", fit=["down_strong", "down_mild", "panic"], cash=True,
         loss="最多亏掉权利金", gain="到 0 为止",
         note="现金账户唯一的做空工具。恐慌时 IV 高、Put 最贵，最好在平静时买。"),
    dict(name="牛市看涨价差 Bull Call Spread", view="温和看涨", fit=["up_mild"], cash=False,
         loss="净权利金", gain="两个行权价之差 − 成本",
         note="机器人现在的结构。需要保证金账户（价差权限），你的现金账户做不了。"),
    dict(name="熊市看跌价差 Bear Put Spread", view="温和看跌", fit=["down_mild"], cash=False,
         loss="净权利金", gain="行权价之差 − 成本", note="同上。"),
    dict(name="买入跨式 / 宽跨式 Long Straddle / Strangle", view="要大波动、不知方向", fit=["range_narrow"], cash=True,
         loss="两份权利金", gain="大幅单边时不封顶",
         note="适合「波动收缩」标签：IV 低时买。要求两腿权利金都付得起，$50 基本不够。"),
    dict(name="铁鹰 Iron Condor", view="不涨不跌", fit=["range_narrow"], cash=False,
         loss="翼宽 − 收到的权利金", gain="收到的权利金",
         note="窄幅震荡的经典卖方结构，需要保证金账户。小赚多次、偶尔大亏。"),
    dict(name="卖出宽跨式 Short Strangle", view="不涨不跌", fit=["range_narrow"], cash=False,
         loss="理论上不封顶", gain="收到的权利金", note="风险极大，新手不要碰。"),
    dict(name="现金担保卖看跌 Cash-Secured Put", view="愿意在低位买入", fit=["range_wide", "up_mild", "panic"], cash=True,
         loss="行权价 × 100 − 权利金（相当于在行权价买入 100 股）", gain="收到的权利金",
         note="需要准备「行权价 × 100」的现金。SPY 一张要几万美元，$50 做不了。"),
    dict(name="备兑看涨 Covered Call", view="持股、温和看涨", fit=["up_mild", "range_narrow"], cash=True,
         loss="同持股（减去权利金）", gain="封顶在行权价", note="需要先有 100 股。"),
    dict(name="保护性看跌 Protective Put", view="持股、怕暴跌", fit=["down_mild", "panic"], cash=True,
         loss="封底在行权价", gain="同持股（减去权利金）", note="给持仓买保险。平静时买便宜，恐慌时买很贵。"),
    dict(name="日历价差 Calendar", view="短期不动、长期有波动", fit=["range_narrow"], cash=False,
         loss="净权利金", gain="有限", note="卖近月买远月，赚近月时间损耗。需要价差权限。"),
]


# ================================================================ 汇总

def build(data: dict[str, list[dict]], vix: dict[str, float]) -> dict:
    meta_s = [{k: v for k, v in s.items() if k not in ("fn", "needs_rows")} for s in STRATEGIES]
    res = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "regimes": REGIMES, "tags": TAGS,
           "families": FAMILIES, "strategies": meta_s, "stops": STOPS, "options": OPTION_STRUCTURES,
           "cost_roundtrip": COST * 2, "start": START, "split": SPLIT, "symbols": {}}
    for und, rows in data.items():
        b = Bars(rows, vix)
        reg = classify(b)
        i0 = b.start_index()
        i1 = b.n - 1
        si = next(i for i, t in enumerate(b.d) if t >= SPLIT)
        sym = {"last_date": b.d[-1], "last_close": b.c[-1], "start_date": b.d[i0], "now": reg[-1],
               "history": [[b.d[i], reg[i]["r"] if reg[i] else None, round(b.c[i], 2)] for i in range(max(i0, b.n - 260), b.n)],
               "share": {}, "bh_regime": None, "strategies": {}, "stops": {}}
        cnt = {r: 0 for r in RID}
        for i in range(i0, b.n):
            if reg[i]:
                cnt[reg[i]["r"]] += 1
        tot = sum(cnt.values())
        sym["share"] = {r: round(v / tot, 3) for r, v in cnt.items()}
        # 当前行情已经持续了多少天
        cur, k = reg[-1]["r"], b.n - 1
        while k > 0 and reg[k - 1] and reg[k - 1]["r"] == cur:
            k -= 1
        sym["now_since"] = b.d[k]
        for s in STRATEGIES:
            trades, pos, pending = s["fn"](b, rows) if s.get("needs_rows") else s["fn"](b)
            ev = evaluate(b, reg, trades, pos, i0, i1, si, with_random=s["id"] != "buyhold")
            ev["live"] = live_state(b, trades, pos, pending)
            sym["strategies"][s["id"]] = ev
        sym["bh_regime"] = sym["strategies"]["buyhold"]["regimes"]
        for kind in ("breakout", "dip"):
            for stp in STOPS:
                trades, pos, _ = stop_test(b, kind, stp["id"])
                ev = evaluate(b, reg, trades, pos, i0, i1, si, with_random=False)
                ev.pop("equity")
                ev.pop("regimes")
                sym["stops"][f"{kind}:{stp['id']}"] = ev
        res["symbols"][und] = sym
    return res


def load_cached():
    from . import backtest as bt
    data = {}
    for u in UNDERLYINGS:
        p = bt.DATA / f"{u.replace('.', '_')}_ohlcv.json"
        data[u] = json.loads(p.read_text("utf-8"))
    vp = bt.DATA / "US__VIX.json"
    vix = dict(json.loads(vp.read_text("utf-8"))) if vp.exists() else {}
    return data, vix


def refresh(cfg=None, fetch=True) -> dict:
    """拉最新日线（可选）→ 重算 → 写 backtest/library.json。返回 {ok, message}。"""
    note = ""
    if fetch and cfg is not None:
        try:
            from . import backtest as bt
            from .cli import Cli
            cli = Cli(cfg)
            for u in UNDERLYINGS:
                bt.fetch_ohlcv(cli, u)
            bt.fetch_daily(cli, "US..VIX")
        except Exception as e:                   # 拉不到就用缓存，照样算
            note = f"（拉取最新行情失败，用本地缓存：{e}）"
    data, vix = load_cached()
    res = build(data, vix)
    OUT.write_text(json.dumps(res, ensure_ascii=False), "utf-8")
    last = min(s["last_date"] for s in res["symbols"].values())
    return {"ok": True, "message": f"策略库已更新：数据截至 {last}，{len(STRATEGIES)} 条策略 × {len(UNDERLYINGS)} 个标的{note}"}
