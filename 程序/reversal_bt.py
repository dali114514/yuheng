# -*- coding: utf-8 -*-
"""
扫流动性反转模型回测（PA / SMC / ICT 报告的口径，搬到美股 1 分钟K）

  看多（看空完全对称，用价格取负实现）：
  ① 扫低收回：一根K的最低价刺破「已知的低点」（今天的摆动低点：前后各 2 根里最低、右边 2 根收完才算已知；或昨低），
     而且这根收盘回到该低点上方。信号时间 9:36–11:00
  ② MSS：之后 30 根内，收盘越过结构高点（被扫的低点和扫低那根之间的最高价；昨低则用最近一个已知摆动高点）。
     中途有收盘跌破扫低极值 → 作废
  ③ FVG：扫低之后、MSS 后 3 根以内形成的看涨 FVG（第 1 根高点 < 第 3 根低点），取 MSS 时已知的最近一个，没有就等后面 3 根
  ④ 下单（三种入场一起比）：
     L：FVG 中位挂限价，FVG 和 MSS 都成立后的下一根开始有效，最多等 20 根、11:00 后取消；成交前先碰到目标或止损 → 取消
         止损 = FVG 下沿 − 0.01 ATR
     M：MSS 收盘后下一根开盘直接进，止损 = 扫低极值 − 0.01 ATR
  目标：2R；另一组用「扫低前今天的最高点」（前面的流动性），离进场不到 1R 就不做
  同一根K同时碰到止损和目标算止损；成交那根如果也碰到止损算止损；16:00 没结果按收盘价平
  成本：每笔 0.01 倍日线 ATR，换算成 R 扣掉（止损越窄扣得越多）
  每只股票每天每个方向只取第一笔。只用当时已收盘的K线。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import c2        # noqa: E402
import history   # noqa: E402

COST = 0.01
T0, T1 = 576, 660


def pivots(x, k=2, low=True):
    n = len(x)
    if low:
        return [i for i in range(k, n - k) if x[i] == x[i - k:i + k + 1].min()]
    return [i for i in range(k, n - k) if x[i] == x[i - k:i + k + 1].max()]


def sim(o, h, l, c, t, e, stop, tgt, start):
    """从 start 这根开始持仓（e 已成交），返回毛 R。"""
    risk = e - stop
    for k in range(start, len(o)):
        if l[k] <= stop:
            return -1.0 if o[k] > stop else (o[k] - e) / risk
        if h[k] >= tgt:
            return (tgt - e) / risk
    return (c[-1] - e) / risk


def bull(o, h, l, c, t, a, pdl):
    """返回这一天看多方向的第一笔 setup 的各种入场结果（dict）或 None。"""
    n = len(c)
    pl, ph = pivots(l, low=True), pivots(h, low=False)
    active = {}                                    # 价位来源 → (价格, 形成位置)
    if pdl:
        active["昨低"] = (pdl, -1)
    for i in range(1, n):
        for k in pl:                               # 第 k 根在第 k+2 根收盘后才知道
            if k + 2 == i - 1:
                active[f"摆动{k}"] = (l[k], k)
        if t[i] > T1:
            return None
        hit = [(name, px, k) for name, (px, k) in active.items() if l[i] < px]
        for name, _, _ in hit:
            active.pop(name)
        if t[i] < T0:
            continue
        ok = [(name, px, k) for name, px, k in hit if c[i] > px]
        if not ok:
            continue
        name, px, k = min(ok, key=lambda x: x[1])  # 扫掉的最低那一个
        sweep_low = l[i]
        if k >= 0 and i - k >= 2:
            struct = h[k + 1:i].max()
        else:
            known = [p for p in ph if p + 2 <= i - 1]
            struct = h[known[-1]] if known else h[max(0, i - 10):i].max()
        if struct <= c[i]:
            continue                               # 扫低那根已经收在结构上方，算不出 MSS
        j = None
        for jj in range(i + 1, min(n, i + 31)):
            if t[jj] > T1 or c[jj] < sweep_low:
                break
            if c[jj] > struct:
                j = jj
                break
        if j is None:
            continue
        day_hi = h[:i].max()
        out = dict(t_sweep=int(t[i]), t_mss=int(t[j]), level=("昨低" if name == "昨低" else "摆动低点"),
                   sweep_atr=(c[i] - sweep_low) / a)
        # M：MSS 后下一根开盘直接进
        if j + 1 < n and t[j + 1] <= T1 + 1:
            e = o[j + 1]
            stop = sweep_low - 0.01 * a
            if e > stop:
                r = e - stop
                out["M_2R"] = (sim(o, h, l, c, t, e, stop, e + 2 * r, j + 1), r / a)
                if day_hi - e >= r:
                    out["M_LQ"] = (sim(o, h, l, c, t, e, stop, day_hi, j + 1), r / a)
        # L：FVG 中位限价
        fv = [m for m in range(i + 1, min(n, j + 4)) if m >= 2 and l[m] > h[m - 2]]
        known = [m for m in fv if m <= j]
        m = known[-1] if known else (fv[0] if fv else None)
        if m is not None:
            lo, hi = h[m - 2], l[m]
            lim = (lo + hi) / 2
            s = max(j, m) + 1
            sx = min(sweep_low, l[i:s].min()) - 0.01 * a           # 扫低极值止损
            plans = []
            for sname, stop in (("L", lo - 0.01 * a), ("LX", sx)):
                r = lim - stop
                if r <= 0:
                    continue
                plans += [(sname + "_2R", stop, r, lim + 2 * r), (sname + "_LQ", stop, r, day_hi if day_hi - lim >= r else None)]
            for tg_name, stop, r, tgt in plans:
                if tgt is None:
                    continue
                res = None
                for k2 in range(s, min(n, s + 20)):
                    if t[k2] > T1:
                        break
                    if l[k2] <= lim:
                        e = min(o[k2], lim)
                        rr = e - stop
                        res = (-1.0, rr / a) if l[k2] <= stop else (sim(o, h, l, c, t, e, stop, e + 2 * rr if tg_name.endswith("_2R") else tgt, k2 + 1), rr / a)
                        break
                    if h[k2] >= tgt:
                        break                      # 没回踩就走了，错过
                out[tg_name] = res if res else None
            out["fvg_atr"] = (hi - lo) / a
        return out
    return None


def run(codes=None, log=print):
    con = history.connect()
    try:
        codes = codes or [r[0] for r in con.execute("SELECT code FROM bars_1m GROUP BY code HAVING COUNT(*) > 50000")]
    finally:
        con.close()
    q = c2.session(history.load("QQQ", "1m"))
    qd = {d: g for d, g in q.groupby(q.index.normalize())}
    rows = []
    for code in codes:
        m1 = c2.session(history.load(code, "1m"))
        d1 = history.load(code, "1d")
        pc = d1["close"].shift()
        tr = pd.concat([d1["high"] - d1["low"], (d1["high"] - pc).abs(), (d1["low"] - pc).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().shift()
        atr.index = atr.index.normalize()
        ph, pl = d1["high"].shift(), d1["low"].shift()
        ph.index, pl.index = ph.index.normalize(), pl.index.normalize()
        for day, b in m1.groupby(m1.index.normalize()):
            a = atr.get(day)
            if a is None or a != a or len(b) < 300:
                continue
            o, h, l, c = (b[x].values.astype(float) for x in ("open", "high", "low", "close"))
            t = b.index.hour * 60 + b.index.minute
            qq = qd.get(day)
            for d in (1, -1):
                if d == 1:
                    res = bull(o, h, l, c, t, a, pl.get(day))
                else:
                    res = bull(-o, -l, -h, -c, t, a, -ph.get(day) if ph.get(day) == ph.get(day) else None)
                if not res:
                    continue
                qa = None
                if qq is not None:
                    x = qq[qq.index.hour * 60 + qq.index.minute <= res["t_mss"]]
                    if len(x):
                        v = x["volume"].values
                        vw = ((x.high + x.low + x.close) / 3 * v).sum() / max(v.sum(), 1)
                        qc = float(x.close.iloc[-1])
                        qa = 1 if (qc > vw and qc > float(x.open.iloc[0])) else -1 if (qc < vw and qc < float(x.open.iloc[0])) else 0
                for key in ("M_2R", "M_LQ", "L_2R", "L_LQ", "LX_2R", "LX_LQ"):
                    v = res.get(key)
                    if key in res and v is None and key.startswith("L"):
                        rows.append(dict(code=code, date=str(day.date()), dir=d, model=key, filled=False))
                        continue
                    if not v:
                        continue
                    R, risk_atr = v
                    rows.append(dict(code=code, date=str(day.date()), dir=d, model=key, filled=True, R=R, risk_atr=risk_atr,
                                     risk_usd=risk_atr * a, cost_R=COST / risk_atr if risk_atr > 0 else np.nan,
                                     qqq=(qa or 0) * d, level=res["level"], t_sweep=res["t_sweep"], t_mss=res["t_mss"]))
        log(f"  · {code}")
    return pd.DataFrame(rows)


def ci(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return (np.nan, np.nan, np.nan)
    m, se = x.mean(), x.std(ddof=1) / np.sqrt(len(x))
    return m, m - 1.96 * se, m + 1.96 * se


def day_ci(df, col):
    """按交易日聚合后的区间（同一天的股票彼此相关，这个更诚实）。"""
    g = df.groupby("date")[col].mean()
    return ci(g.values)


def line(df):
    if not len(df):
        return "0 笔"
    net = df.R - df.cost_R
    m, lo, hi = day_ci(df.assign(net=net), "net")
    months = df.assign(net=net).groupby(df.date.str[:7]).net.mean()
    return (f"{len(df)} 笔 · 赚钱 {(df.R > 0).mean() * 100:.0f}% · 毛 {df.R.mean():+.3f}R · 成本 {df.cost_R.mean():.3f}R · "
            f"**净 {net.mean():+.3f}R**（按日 95%：{lo:+.3f} ~ {hi:+.3f}）· 扣两倍成本 {(df.R - 2 * df.cost_R).mean():+.3f}R · "
            f"{(months > 0).sum()}/{len(months)} 个月为正 · 止损中位 ${df.risk_usd.median():.2f}")


if __name__ == "__main__":
    df = run()
    out = HERE / "data" / ".reversal_bt.pkl"
    df.to_pickle(out)
    print(len(df), "rows ->", out)
