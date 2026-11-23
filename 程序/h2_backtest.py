"""熊猫教练「高二进场 + EMA20 移动止盈」回测。

规则翻译（按视频原意，参数不在本数据上调）：
  趋势过滤：EMA21 > EMA55 > EMA144（多头排列）才做多；空头排列才做空。
  高二（H2）：多头排列中，价格创出新高后开始回调。回调中第一次「本 K 线高点 > 前一根高点」记为高一，
             之后价格再向下走（出现高点降低的 K 线），第二次「高点 > 前一根高点」就是高二信号 K。
             下一根 K 线突破信号 K 高点时按突破价入场；没突破就作废。空头镜像为低二（L2）。
  初始止损：这段回调的最低点（做空为最高点）。
  EMA20 移动止盈：回调到 EMA20 附近（最低点 ≤ EMA20 + 0.25 ATR）出现强底分型
             （中间 K 高低点都低于两侧，第三根收盘站上第一根最高点）→ 止损上移到分型最低点，只上不下；
             整根 K 线跑到 EMA20 下方（最高点 < EMA20）→ 按收盘离场。
  成本：每笔来回扣入场价的 0.02%（手续费 + 滑点）。
对照组：
  aligned_any —— 同样的均线过滤和离场，但不等高二：只要空仓且均线排列就在下一根开盘进场（止损 = 近 10 根最低点）。
  buy_hold   —— 买入持有。
"""
from __future__ import annotations

import statistics as st

COST = 0.0002


def ema(x, n):
    k, out, e = 2 / (n + 1), [], None
    for v in x:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def atr(h, l, c, n=14):
    out, a = [], None
    for i in range(len(c)):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


class Series:
    def __init__(self, rows, key="date"):
        self.t = [r.get(key) or r.get("time") or r.get("date") for r in rows]
        self.day = [str(t)[:10] for t in self.t]
        self.o = [r["open"] for r in rows]
        self.h = [r["high"] for r in rows]
        self.l = [r["low"] for r in rows]
        self.c = [r["close"] for r in rows]
        self.e20, self.e21, self.e55, self.e144 = (ema(self.c, n) for n in (20, 21, 55, 144))
        self.atr = atr(self.h, self.l, self.c)

    def bull(self, i):
        return self.e21[i] > self.e55[i] > self.e144[i]

    def bear(self, i):
        return self.e21[i] < self.e55[i] < self.e144[i]


def _exit(s: Series, side, e, entry, stop, eod):
    """从入场 K（第 e 根）开始逐根管理，返回 (出场价, 出场索引, 原因)。"""
    n = len(s.c)
    long = side == "long"
    for j in range(e, n):
        # 1) 止损
        if (long and s.l[j] <= stop) or (not long and s.h[j] >= stop):
            gap = (s.o[j] < stop) if long else (s.o[j] > stop)
            return (s.o[j] if gap and j > e else stop), j, "止损/移动止损"
        # 2) 整根 K 线跑到 EMA20 另一侧 → 收盘离场
        if j > e and ((long and s.h[j] < s.e20[j]) or (not long and s.l[j] > s.e20[j])):
            return s.c[j], j, "跌破EMA20"
        # 3) EMA20 附近的强分型 → 上移止损
        if j >= e + 2:
            a, m, b = j - 2, j - 1, j
            if long:
                frac = s.l[m] < s.l[a] and s.l[m] < s.l[b] and s.h[m] < s.h[a] and s.h[m] < s.h[b] and s.c[b] > s.h[a]
                if frac and s.l[m] <= s.e20[m] + 0.25 * s.atr[m]:
                    stop = max(stop, s.l[m])
            else:
                frac = s.h[m] > s.h[a] and s.h[m] > s.h[b] and s.l[m] > s.l[a] and s.l[m] > s.l[b] and s.c[b] < s.l[a]
                if frac and s.h[m] >= s.e20[m] - 0.25 * s.atr[m]:
                    stop = min(stop, s.h[m])
        # 4) 日内：收盘前平仓
        if eod and (j + 1 >= n or s.day[j + 1] != s.day[j]):
            return s.c[j], j, "收盘平仓"
    return s.c[n - 1], n - 1, "数据结束"


def _trade(s, side, sig_i, e, entry, stop, eod, start):
    if s.t[e] < start:
        return None
    risk = (entry - stop) if side == "long" else (stop - entry)
    if risk <= 0 or risk > 3 * s.atr[sig_i]:
        return None
    px, x, why = _exit(s, side, e, entry, stop, eod)
    pnl = (px - entry) if side == "long" else (entry - px)
    pnl -= COST * entry
    return {"t": s.t[e], "side": side, "entry": entry, "stop": stop, "exit": px, "r": pnl / risk,
            "risk_pct": risk / entry, "why": why, "bars": x - e + 1, "e": e, "x": x}


def run_h2(s: Series, start="2010-01-01", eod=False, sides=("long", "short")):
    trades, n, i = [], len(s.c), 150
    st_long = {"hi": None, "cnt": 0, "down": False, "pb": None}
    st_short = {"lo": None, "cnt": 0, "up": False, "pb": None}
    pos_until = -1
    while i < n - 1:
        sig = None
        # ---- 多头：数高一 / 高二
        if s.bull(i):
            L = st_long
            if L["hi"] is None or s.h[i] > L["hi"]:
                L.update(hi=s.h[i], cnt=0, down=False, pb=None)
            else:
                L["pb"] = s.l[i] if L["pb"] is None else min(L["pb"], s.l[i])
                if s.h[i] < s.h[i - 1]:
                    L["down"] = True
                elif s.h[i] > s.h[i - 1] and L["down"]:
                    L["cnt"] += 1
                    L["down"] = False
                    if L["cnt"] == 2 and "long" in sides:
                        sig = ("long", s.h[i], L["pb"])
                        L.update(cnt=0)
        else:
            st_long.update(hi=None, cnt=0, down=False, pb=None)
        # ---- 空头：数低一 / 低二
        if s.bear(i) and not sig:
            S = st_short
            if S["lo"] is None or s.l[i] < S["lo"]:
                S.update(lo=s.l[i], cnt=0, up=False, pb=None)
            else:
                S["pb"] = s.h[i] if S["pb"] is None else max(S["pb"], s.h[i])
                if s.l[i] > s.l[i - 1]:
                    S["up"] = True
                elif s.l[i] < s.l[i - 1] and S["up"]:
                    S["cnt"] += 1
                    S["up"] = False
                    if S["cnt"] == 2 and "short" in sides:
                        sig = ("short", s.l[i], S["pb"])
                        S.update(cnt=0)
        elif not s.bear(i):
            st_short.update(lo=None, cnt=0, up=False, pb=None)

        if sig and i > pos_until:
            side, trig, stop = sig
            e = i + 1
            if eod and s.day[e] != s.day[i]:          # 日内：信号 K 是当天最后一根，不隔夜挂单
                i += 1
                continue
            hit = s.h[e] > trig if side == "long" else s.l[e] < trig
            if hit:
                entry = max(s.o[e], trig) if side == "long" else min(s.o[e], trig)
                t = _trade(s, side, i, e, entry, stop, eod, start)
                if t:
                    trades.append(t)
                    pos_until = t["x"]
        i += 1
    return trades


def run_aligned_any(s: Series, start="2010-01-01", eod=False):
    """对照：均线多头排列、空仓时，下一根开盘直接做多；止损 = 近 10 根最低点。离场规则相同。"""
    trades, n, i = [], len(s.c), 150
    while i < n - 1:
        if s.bull(i):
            e = i + 1
            if not (eod and s.day[e] != s.day[i]):
                t = _trade(s, "long", i, e, s.o[e], min(s.l[i - 9:i + 1]), eod, start)
                if t:
                    trades.append(t)
                    i = t["x"] + 1
                    continue
        i += 1
    return trades


def stats(ts, years):
    if not ts:
        return {"n": 0}
    rs = [t["r"] for t in ts]
    cum = peak = mdd = 0.0
    eq, eq_peak, eq_mdd = 1.0, 1.0, 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
        eq *= 1 + 0.01 * r                    # 每笔拿 1% 本金冒险、复利
        eq_peak = max(eq_peak, eq)
        eq_mdd = min(eq_mdd, eq / eq_peak - 1)
    wins = [r for r in rs if r > 0]
    loss = [r for r in rs if r <= 0]
    return {"n": len(rs), "per_year": round(len(rs) / years, 1), "win": round(len(wins) / len(rs), 3),
            "avg_r": round(st.mean(rs), 3), "avg_win_r": round(st.mean(wins), 2) if wins else 0,
            "avg_loss_r": round(st.mean(loss), 2) if loss else 0, "total_r": round(sum(rs), 1),
            "max_dd_r": round(mdd, 1), "eq_mult_1pct": round(eq, 3), "eq_mdd_1pct": round(eq_mdd, 3),
            "avg_bars": round(st.mean(t["bars"] for t in ts), 1)}
