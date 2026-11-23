"""杰明「黑板干货」价格行为打法的日线回测（SPY / QQQ）。

规则翻译（尽量按视频原意，参数用他说的或常用默认值，不在本数据上调参）：
  trend_pullback  趋势回踩 MA20：MA200 向上且价在其上，K 线回踩 MA20 后收回并收阳（空头镜像）   —— 第 25 集
  sweep           假突破 / 流动性扫荡：刺破前 20 日低点后收回其上（做多）；刺破前 20 日高点后收回其下（做空） —— 第 3、30 集
  capitulation    投降反转：10 日跌 ≥5% 后出现放量巨阴线（量 ≥2 倍均量、振幅 ≥1.5 ATR），5 日内放量阳线收回被砸穿位置（顶部镜像） —— 第 36 集
  rsi_w           RSI 画 W 底：RSI 上穿 30，且最近低点高于前一个低点（顶部镜像） —— 第 17 集
  squeeze         波动压缩后突破：ATR10/ATR50 < 0.7 后，大实体 K（振幅 ≥1.2 ATR、收在上 30%）突破 20 日高点（镜像） —— 第 7 集
  baseline_long   对照组：只要空仓就做多（止损用 10 日低点）

统一出场（他反复讲的风控）：
  入场 = 信号次日开盘价；止损 = 结构点外再留 0.5 ATR；目标 = 2R；最多持有 20 个交易日（到期按收盘价平仓）。
  同一根 K 线同时碰到止损和目标 → 按先止损算（保守）。
  一个标的同一时间只持有一笔。
"""
from __future__ import annotations

import math
import statistics as st

SPLIT = "2018-01-01"
TARGET_R = 2.0
MAX_DAYS = 20
ATR_PAD = 0.5


# ---------------------------------------------------------------- 指标

def _sma(x, n, i):
    return sum(x[i - n + 1:i + 1]) / n if i >= n - 1 else None


def _atr_series(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    out = [None] * len(c)
    if len(c) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def _rsi_series(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    lo = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100 - 100 / (1 + g / lo) if lo else 100
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        lo = (lo * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + g / lo) if lo else 100
    return out


class Bars:
    def __init__(self, rows):
        self.d = [r["date"] for r in rows]
        self.o = [r["open"] for r in rows]
        self.h = [r["high"] for r in rows]
        self.l = [r["low"] for r in rows]
        self.c = [r["close"] for r in rows]
        self.v = [r["volume"] for r in rows]
        self.atr = _atr_series(self.h, self.l, self.c, 14)
        self.atr10 = _atr_series(self.h, self.l, self.c, 10)
        self.atr50 = _atr_series(self.h, self.l, self.c, 50)
        self.rsi = _rsi_series(self.c, 14)
        n = len(self.c)
        self.ma20 = [_sma(self.c, 20, i) for i in range(n)]
        self.ma200 = [_sma(self.c, 200, i) for i in range(n)]
        self.vavg20 = [_sma(self.v, 20, i) for i in range(n)]


# ---------------------------------------------------------------- 信号：返回 (方向, 止损价) 或 None

def sig_trend_pullback(b: Bars, i):
    if i < 220 or b.ma200[i] is None:
        return None
    up = b.ma200[i] > b.ma200[i - 20] and b.c[i] > b.ma200[i]
    dn = b.ma200[i] < b.ma200[i - 20] and b.c[i] < b.ma200[i]
    if up and b.l[i] <= b.ma20[i] < b.c[i] and b.c[i] > b.o[i]:
        return "long", b.l[i] - ATR_PAD * b.atr[i]
    if dn and b.h[i] >= b.ma20[i] > b.c[i] and b.c[i] < b.o[i]:
        return "short", b.h[i] + ATR_PAD * b.atr[i]
    return None


def sig_sweep(b: Bars, i):
    if i < 30:
        return None
    lo20, hi20 = min(b.l[i - 20:i]), max(b.h[i - 20:i])
    if b.l[i] < lo20 and b.c[i] > lo20:
        return "long", b.l[i] - ATR_PAD * b.atr[i]
    if b.h[i] > hi20 and b.c[i] < hi20:
        return "short", b.h[i] + ATR_PAD * b.atr[i]
    return None


def _climax(b: Bars, k, down: bool):
    if k < 30 or b.vavg20[k - 1] is None:
        return False
    rng = b.h[k] - b.l[k]
    big = rng >= 1.5 * b.atr[k - 1] and b.v[k] >= 2.0 * b.vavg20[k - 1]
    if down:
        return big and b.c[k] < b.o[k] and b.c[k] <= b.c[k - 10] * 0.95
    return big and b.c[k] > b.o[k] and b.c[k] >= b.c[k - 10] * 1.05


def sig_capitulation(b: Bars, i):
    if i < 40:
        return None
    strong = (b.h[i] - b.l[i]) >= b.atr[i - 1] and b.v[i] > b.vavg20[i - 1]
    for k in range(i - 5, i):
        if _climax(b, k, down=True):
            broken = min(b.l[k - 20:k])                       # 被砸穿的位置
            if strong and b.c[i] > b.o[i] and b.c[i] > broken and (b.c[i] - b.l[i]) >= 0.7 * (b.h[i] - b.l[i]):
                return "long", min(b.l[k:i + 1]) - ATR_PAD * b.atr[i]
        if _climax(b, k, down=False):
            broken = max(b.h[k - 20:k])
            if strong and b.c[i] < b.o[i] and b.c[i] < broken and (b.h[i] - b.c[i]) >= 0.7 * (b.h[i] - b.l[i]):
                return "short", max(b.h[k:i + 1]) + ATR_PAD * b.atr[i]
    return None


def sig_rsi_w(b: Bars, i):
    if i < 40 or b.rsi[i] is None or b.rsi[i - 1] is None:
        return None
    r = b.rsi
    recent, earlier = min(r[i - 4:i + 1]), min(r[i - 20:i - 4])
    if r[i - 1] <= 30 < r[i] and earlier < 30 and recent > earlier:
        return "long", min(b.l[i - 10:i + 1]) - ATR_PAD * b.atr[i]
    recent_h, earlier_h = max(r[i - 4:i + 1]), max(r[i - 20:i - 4])
    if r[i - 1] >= 70 > r[i] and earlier_h > 70 and recent_h < earlier_h:
        return "short", max(b.h[i - 10:i + 1]) + ATR_PAD * b.atr[i]
    return None


def sig_squeeze(b: Bars, i):
    if i < 60 or b.atr50[i - 1] is None:
        return None
    if b.atr10[i - 1] / b.atr50[i - 1] >= 0.7:
        return None
    rng = b.h[i] - b.l[i]
    if rng < 1.2 * b.atr[i - 1]:
        return None
    if b.c[i] > max(b.h[i - 20:i]) and (b.c[i] - b.l[i]) >= 0.7 * rng:
        return "long", b.l[i] - ATR_PAD * b.atr[i]
    if b.c[i] < min(b.l[i - 20:i]) and (b.h[i] - b.c[i]) >= 0.7 * rng:
        return "short", b.h[i] + ATR_PAD * b.atr[i]
    return None


def sig_baseline_long(b: Bars, i):
    if i < 220:
        return None
    return "long", min(b.l[i - 9:i + 1]) - ATR_PAD * b.atr[i]


SIGNALS = {
    "trend_pullback": ("趋势回踩 MA20", sig_trend_pullback),
    "sweep": ("假突破 / 流动性扫荡", sig_sweep),
    "capitulation": ("投降反转（抄大底 / 摸大顶）", sig_capitulation),
    "rsi_w": ("RSI 画 W 底 / M 顶", sig_rsi_w),
    "squeeze": ("波动压缩后大 K 突破", sig_squeeze),
    "baseline_long": ("对照：只要空仓就做多", sig_baseline_long),
}


# ---------------------------------------------------------------- 模拟（正股层面，R 倍数）

def simulate(b: Bars, fn, start="2010-01-01", target_r=TARGET_R, max_days=MAX_DAYS):
    trades = []
    i = 0
    n = len(b.c)
    while i < n - 2:
        if b.d[i] < start:
            i += 1
            continue
        s = fn(b, i)
        if not s:
            i += 1
            continue
        side, stop = s
        e = i + 1                                   # 次日开盘入场
        entry = b.o[e]
        risk = (entry - stop) if side == "long" else (stop - entry)
        if risk <= 0 or risk > 4 * b.atr[i]:        # 跳空越过止损 / 风险过大：放弃
            i += 1
            continue
        target = entry + target_r * risk if side == "long" else entry - target_r * risk
        exit_px, exit_i, reason = None, None, None
        for j in range(e, min(e + max_days, n)):
            hit_stop = b.l[j] <= stop if side == "long" else b.h[j] >= stop
            hit_tgt = b.h[j] >= target if side == "long" else b.l[j] <= target
            if hit_stop:
                gap = b.o[j] < stop if side == "long" else b.o[j] > stop
                exit_px, exit_i, reason = (b.o[j] if gap and j > e else stop), j, "止损"
                break
            if hit_tgt:
                exit_px, exit_i, reason = target, j, "止盈"
                break
        if exit_px is None:
            exit_i = min(e + max_days - 1, n - 1)
            exit_px, reason = b.c[exit_i], "到时平仓"
        r = ((exit_px - entry) if side == "long" else (entry - exit_px)) / risk
        trades.append({"date": b.d[i], "entry_date": b.d[e], "exit_date": b.d[exit_i], "side": side,
                       "entry": entry, "stop": stop, "exit": exit_px, "r": round(r, 3), "reason": reason,
                       "days": exit_i - e + 1, "i_entry": e, "i_exit": exit_i})
        i = exit_i + 1                              # 平仓后才看下一个信号
    return trades


def r_stats(ts):
    if not ts:
        return {"n": 0}
    rs = [t["r"] for t in ts]
    cum = peak = mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {"n": len(rs), "win": round(sum(r > 0 for r in rs) / len(rs), 3),
            "avg_r": round(st.mean(rs), 3), "total_r": round(sum(rs), 1), "max_dd_r": round(mdd, 1),
            "avg_days": round(st.mean(t["days"] for t in ts), 1)}


# ---------------------------------------------------------------- 换算成买期权的收益

def option_returns(b: Bars, ts, vix_map, iv_mult, dte=30, target_delta=None, fee=1.0):
    """每笔交易：入场日买 call（做多）/ put（做空），出场日卖出。返回每笔收益率（含买卖价差与手续费）。"""
    from . import backtest as bt
    out = []
    for t in ts:
        e, x = t["i_entry"], t["i_exit"]
        iv_in = vix_map.get(b.d[e])
        iv_out = vix_map.get(b.d[x])
        if not iv_in or not iv_out:
            continue
        iv_in, iv_out = iv_in / 100 * iv_mult, iv_out / 100 * iv_mult
        call = t["side"] == "long"
        spot = t["entry"]
        if target_delta is None:
            k = spot
        else:                                       # 找 |Delta| 最接近目标的行权价
            k, best = spot, 9
            step = spot * 0.0025
            for m in range(0, 80):
                kk = spot + (m * step if call else -m * step)
                _, d = bt.bs(spot, kk, dte / 365, iv_in, call)
                if abs(abs(d) - target_delta) < best:
                    k, best = kk, abs(abs(d) - target_delta)
        mid_in, _ = bt.bs(spot, k, dte / 365, iv_in, call)
        cost = mid_in + bt.half_spread(mid_in)
        days_held = (int(b.d[x][:4]) - int(b.d[e][:4])) * 365 + _doy(b.d[x]) - _doy(b.d[e])
        mid_out, _ = bt.bs(t["exit"], k, max(dte - days_held, 0) / 365, iv_out, call)
        proceeds = max(mid_out - bt.half_spread(mid_out), 0)
        ret = (proceeds * 100 - fee - cost * 100 - fee) / (cost * 100 + fee)
        out.append({"ret": ret, "cost": cost * 100 + fee})
    return out


def _doy(d):
    import datetime as dt
    return dt.date.fromisoformat(d).timetuple().tm_yday
