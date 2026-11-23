# -*- coding: utf-8 -*-
"""
Patrick Nill「三次触碰」打法回测（研究专用，只读数据库）

来源：B 站 BV1R8Yg6ME2X 访谈（2026-11-20 读字幕整理）。他说出来的规则 → 这里的程序化口径：
  区间：一段推动（≤6 根内走 ≥ IMP 倍日线 ATR）之后的盘整；宽度 ≤ 推动的一半；
        区间上沿或下沿被碰到第 3 次（碰到 = 进入离边缘 15% 宽度以内，连着的几根算一次）才算成立，成立后区间冻结。
        他说「画区间的规则保密」，这里只能用这个近似。
  三种入场（做多；做空把价格取负，完全对称）：
    S1 区间：在区间下沿挂限价买，止损下沿下方 max(25% 宽度, 0.03 ATR)，目标区间上沿，一次走完。
    S2 突破+确认：一根收盘站上区间上沿，下一根收得比它更高 → 再下一根开盘进；止损 = 这两根的最低点；
        最终目标 = 上沿 + 推动幅度（他说没有现成目标时「把第一段复制一遍」）。
    S3 假突破收回：跌破区间下沿后 3 根内收回区间 → 下一根开盘进；止损 = 跌破那段的最低点 − 0.01 ATR；
        最终目标 = 上沿 + 区间宽度。
  S2/S3 只做盈亏比 ≥ 3（到最终目标）的；出场两种比：
    one   ：全部在最终目标走；
    scale ：分三份，1/3 在半路（S3 是区间上沿），到了就把止损挪到保本；1/3 在最终目标；最后 1/3 用最近 3 根最低点跟随止损。
  共同：只做日内（他说傍晚成形的不做；这里 15:00 后不进新单，16:00 按收盘价平）。
        同一根K同时碰到止损和目标算止损；限价单成交那根只看止损不看目标（保守）。
        每只股票每天每种入场每个方向只取第一笔。
  成本：每笔 0.01 倍日线 ATR，换成 R 扣掉。
  日线 ATR：前 14 天常规时段（最高 − 最低）平均，用前一天的值（长期库是前复权，这样两库口径一致）。
数据：history_long.db（2019-01 → 2025-11）+ history.db（2025-11 → 现在）的 1 分钟K，合成 5 / 15 分钟。
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history   # noqa: E402

LONG = HERE / "data" / "history_long.db"
COST = 0.01
IMP = 0.4          # 推动至少 0.4 倍日线 ATR
LAST_ENTRY = 900   # 15:00
SKIP = {"ANT", "CRWV"}


def load_1m(code):
    parts = []
    if LONG.exists():
        parts.append(history.load(code, "1m", path=LONG))
    parts.append(history.load(code, "1m"))
    df = pd.concat([p for p in parts if len(p)])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    t = df.index.hour * 60 + df.index.minute
    return df[(t > 570) & (t <= 960)]


def touches(x, edge, tol, top):
    hit = (x >= edge - tol) if top else (x <= edge + tol)
    return int(((hit[1:] & ~hit[:-1]).sum()) + hit[0]) if len(hit) else 0


def sim(o, h, l, c, k0, e, s, T1, T2, mode, skip_first_targets=False):
    n = len(o)
    risk = e - s
    legs = 1 if mode == "one" else 3
    booked, stop, got1, got2 = [], s, False, False
    for k in range(k0, n):
        if l[k] <= stop:
            px = o[k] if (o[k] < stop and k > k0) else stop
            return (sum(booked) + (legs - len(booked)) * px - legs * e) / (legs * risk)
        if not (k == k0 and skip_first_targets):
            if mode == "one":
                if h[k] >= T2:
                    return (T2 - e) / risk
            else:
                if not got1 and h[k] >= T1:
                    booked.append(T1)
                    got1 = True
                    stop = max(stop, e)
                if got1 and not got2 and h[k] >= T2:
                    booked.append(T2)
                    got2 = True
        if got2:
            stop = max(stop, l[max(k0, k - 2):k + 1].min())
    px = c[-1]
    return (sum(booked) + (legs - len(booked)) * px - legs * e) / (legs * risk)


def long_day(o, h, l, c, t, A):
    """一天、一个方向（做多口径）。返回 [(setup, mode, R, risk, with_imp, entry_t)]"""
    n = len(c)
    out = []
    done = set()
    st = None
    min_p = 6
    for i in range(n):
        if st is None:
            for p in range(i - 3, max(min_p, i - 24) - 1, -1):
                lo6 = max(0, p - 6)
                up = h[p] - l[lo6:p].min()
                dn = h[lo6:p].max() - l[p]
                if up >= dn:
                    if h[p] < h[lo6:p + 1].max():
                        continue
                    imp, d = up, 1
                else:
                    if l[p] > l[lo6:p + 1].min():
                        continue
                    imp, d = dn, -1
                if imp < IMP * A:
                    continue
                hi, lo = h[p:i + 1].max(), l[p:i + 1].min()
                w = hi - lo
                if w > 0.5 * imp or w < 0.05 * A:
                    break
                tol = 0.15 * w
                th = touches(h[p:i + 1], hi, tol, True)
                tl = touches(l[p:i + 1], lo, tol, False)
                if max(th, tl) >= 3:
                    st = dict(hi=hi, lo=lo, w=w, imp=imp, d=d, est=i, bo=None, brk=None, brk_low=None)
                break
            continue
        hi, lo, w = st["hi"], st["lo"], st["w"]
        reset = False
        # S1 区间：下沿限价买
        if "S1" not in done and t[i] <= LAST_ENTRY and l[i] <= lo:
            stop = lo - max(0.25 * w, 0.03 * A)
            e = min(o[i], lo)
            if e > stop:
                R = sim(o, h, l, c, i, e, stop, hi, hi, "one", skip_first_targets=True)
                out.append(("S1区间", "one", R, e - stop, st["d"], t[i]))
            done.add("S1")
        # S3 假突破收回
        if l[i] < lo:
            if st["brk"] is None:
                st["brk"], st["brk_low"] = i, l[i]
            else:
                st["brk_low"] = min(st["brk_low"], l[i])
        if st["brk"] is not None and c[i] > lo and "S3" not in done and i + 1 < n and t[i + 1] <= LAST_ENTRY:
            if i - st["brk"] <= 3:
                e = o[i + 1]
                stop = st["brk_low"] - 0.01 * A
                T1, T2 = hi, hi + w
                if e > stop and T1 > e and (T2 - e) / (e - stop) >= 3:
                    for mode in ("one", "scale"):
                        R = sim(o, h, l, c, i + 1, e, stop, T1, T2, mode)
                        out.append(("S3假突破收回", mode, R, e - stop, st["d"], t[i + 1]))
                done.add("S3")
            st["brk"], st["brk_low"] = None, None
        if c[i] < lo - 0.5 * w:
            reset = True
        # S2 突破 + 确认
        if st["bo"] is not None and st["bo"] == i - 1:
            j = st["bo"]
            if c[i] > c[j] and "S2" not in done and i + 1 < n and t[i + 1] <= LAST_ENTRY:
                e = o[i + 1]
                stop = min(l[j], l[i])
                T2 = hi + st["imp"]
                T1 = (e + T2) / 2
                if e > stop and T2 > e and (T2 - e) / (e - stop) >= 3:
                    for mode in ("one", "scale"):
                        R = sim(o, h, l, c, i + 1, e, stop, T1, T2, mode)
                        out.append(("S2突破确认", mode, R, e - stop, st["d"], t[i + 1]))
                done.add("S2")
                reset = True
            st["bo"] = None
        if c[i] > hi and st["bo"] is None and not reset:
            st["bo"] = i
        if c[i] > hi + w or i - st["est"] > 30:
            reset = True
        if reset:
            st = None
            min_p = i
        if len(done) == 3:
            break
    return out


def run_code(args):
    code, tf = args
    m1 = load_1m(code)
    if not len(m1):
        return []
    days = m1.index.normalize()
    dh = m1.groupby(days).high.max()
    dl = m1.groupby(days).low.min()
    atr = (dh - dl).rolling(14).mean().shift()
    rows = []
    for day, b in m1.groupby(days):
        A = atr.get(day)
        if A is None or A != A or len(b) != 390:
            continue
        g = np.arange(390) // tf
        o = b.open.values[::tf].astype(float)
        c = b.close.values[tf - 1::tf].astype(float)
        h = pd.Series(b.high.values).groupby(g).max().values.astype(float)
        l = pd.Series(b.low.values).groupby(g).min().values.astype(float)
        tt = b.index.hour * 60 + b.index.minute
        t = np.asarray(tt)[tf - 1::tf] - tf          # 这根的开始时间（进场在开盘）
        for d in (1, -1):
            if d == 1:
                res = long_day(o, h, l, c, t, A)
            else:
                res = long_day(-o, -l, -h, -c, t, A)
            for setup, mode, R, risk, imp_d, et in res:
                rows.append(dict(code=code, date=str(day.date()), tf=tf, dir=d, setup=setup, mode=mode, R=R,
                                 risk_atr=risk / A, risk_usd=risk, cost_R=COST * A / risk,
                                 with_imp=int(imp_d == 1), t=int(et)))
    return rows


def run(tfs=(15, 5), codes=None):
    con = history.connect(LONG)
    try:
        cl = [r[0] for r in con.execute("SELECT DISTINCT code FROM bars_1m")]
    finally:
        con.close()
    codes = codes or [x for x in cl if x not in SKIP]
    jobs = [(c, tf) for tf in tfs for c in codes]
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for r in ex.map(run_code, jobs):
            rows += r
    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = run()
    out = HERE / "data" / ".threetouch_bt.pkl"
    df.to_pickle(out)
    print(len(df), "rows ->", out)
