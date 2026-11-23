# -*- coding: utf-8 -*-
"""
开盘剧本统计（研究专用，只读数据库）——用 Patrick Nill 的「区间 / 公允价值 / P、b 型」框架写盘前剧本前，先看历史上各剧本多常见。

每只股票每天：
  昨天：常规时段最高 PDH、最低 PDL；成交量分布（典型价 40 格）→ 成交最密价 POC、70% 价值区 VAL–VAH；
        形状：POC 在全天区间上 1/3 = P，下 1/3 = b，中间 = D。
  今天开盘位置：开在昨高上方 / 昨低下方 / 价值区内 / 区间内但价值区外。
  结果（开在区间外，以上方为例，下方镜像）：
    · 当天 / 11:00 前回到区间内（最低 ≤ 昨高）的比例；回来后碰到 POC、碰到昨低的比例
    · 10:00 时的状态：「被接受」= 还没回过区间且收在昨高上方；「被拒绝」= 已经回到区间内
      之后 10:00 → 11:00、10:00 → 收盘，朝缺口方向走了多少倍日线 ATR
  结果（开在区间内）：先碰哪边、两边都碰、收在区间外；第一次碰到边缘后是收在外面还是走到另一边。
  P / b / D：第二天收盘相对昨收、开盘到收盘的方向。
日线 ATR：前 14 天常规时段（最高 − 最低）平均，用前一天的值。数据：history_long.db + history.db 的 1 分钟K。
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history                 # noqa: E402
from threetouch_bt import load_1m, LONG, SKIP   # noqa: E402


def profile(h, l, c, v):
    lo, hi = l.min(), h.max()
    if hi <= lo:
        return None
    tp = (h + l + c) / 3
    edges = np.linspace(lo, hi, 41)
    idx = np.clip(np.digitize(tp, edges) - 1, 0, 39)
    vol = np.bincount(idx, weights=v, minlength=40)
    p = int(vol.argmax())
    a, b, acc, tot = p, p, vol[p], vol.sum()
    while acc < 0.7 * tot and (a > 0 or b < 39):
        dn = vol[a - 1] if a > 0 else -1
        up = vol[b + 1] if b < 39 else -1
        if up >= dn:
            b += 1
            acc += up
        else:
            a -= 1
            acc += dn
    poc = (edges[p] + edges[p + 1]) / 2
    pos = (poc - lo) / (hi - lo)
    return dict(poc=poc, val=edges[a], vah=edges[b + 1], shape="P" if pos > 2 / 3 else "b" if pos < 1 / 3 else "D")


def run_code(code):
    m1 = load_1m(code)
    if not len(m1):
        return []
    days = m1.index.normalize()
    dh, dl = m1.groupby(days).high.max(), m1.groupby(days).low.min()
    atr = (dh - dl).rolling(14).mean().shift()
    rows, prev = [], None
    for day, b in m1.groupby(days):
        if len(b) != 390:
            prev = None
            continue
        o, h, l, c = (b[x].values.astype(float) for x in ("open", "high", "low", "close"))
        v = b.volume.values.astype(float)
        t = np.asarray(b.index.hour * 60 + b.index.minute)
        A = atr.get(day)
        if prev is not None and A == A and A > 0:
            P = prev
            op = o[0]
            r = dict(code=code, date=str(day.date()), A=A, shape=P["shape"],
                     d_close=(c[-1] - P["close"]) / A, oc=(c[-1] - op) / A,
                     close_in=int(P["pdl"] <= c[-1] <= P["pdh"]),
                     inside_day=int(h.max() <= P["pdh"] and l.min() >= P["pdl"]))
            i10, i11 = 29, 89                       # 10:00、11:00 收盘那根
            if op > P["pdh"] or op < P["pdl"]:
                s = 1 if op > P["pdh"] else -1
                E, F = (P["pdh"], P["pdl"]) if s == 1 else (P["pdl"], P["pdh"])
                X = (lambda arr: s * arr)
                lo_side = l if s == 1 else h        # 往区间方向的极值
                back = np.where(X(lo_side) <= s * E)[0]
                kb = int(back[0]) if len(back) else None
                r.update(loc="区间外", gap=s * (op - E) / A, back_day=int(kb is not None),
                         back_kz=int(kb is not None and kb <= i11))
                if kb is not None:
                    r["back_poc"] = int((X(lo_side[kb:]) <= s * P["poc"]).any())
                    r["back_other"] = int((X(lo_side[kb:]) <= s * F).any())
                if kb is None or kb > i10:
                    if s * c[i10] > s * E:
                        r["st10"] = "接受"
                        r["back_after10"] = int(kb is not None)
                else:
                    r["st10"] = "拒绝"
                    r["poc_after10"] = int((X(lo_side[i10 + 1:]) <= s * P["poc"]).any())
                    r["other_after10"] = int((X(lo_side[i10 + 1:]) <= s * F).any())
                if "st10" in r:
                    r["m10_11"] = s * (c[i11] - c[i10]) / A
                    r["m10_cl"] = s * (c[-1] - c[i10]) / A
            else:
                r["loc"] = "价值区内" if P["val"] <= op <= P["vah"] else "区间内"
                r["opos"] = (op - P["pdl"]) / (P["pdh"] - P["pdl"]) if P["pdh"] > P["pdl"] else 0.5
                th = np.where(h >= P["pdh"])[0]
                tl = np.where(l <= P["pdl"])[0]
                fh = int(th[0]) if len(th) else None
                fl = int(tl[0]) if len(tl) else None
                r.update(touch_hi=int(fh is not None), touch_lo=int(fl is not None),
                         touch_kz=int((fh is not None and fh <= i11) or (fl is not None and fl <= i11)),
                         both=int(fh is not None and fl is not None))
                if fh is not None or fl is not None:
                    first = 1 if (fl is None or (fh is not None and fh < fl)) else -1
                    r["first"] = first
                    r["first_kz"] = int((fh if first == 1 else fl) <= i11)
                    r["first_shape"] = first * (1 if P["shape"] == "P" else -1 if P["shape"] == "b" else 0)
                    edge, other = (P["pdh"], P["pdl"]) if first == 1 else (P["pdl"], P["pdh"])
                    r["edge_close_out"] = int(first * c[-1] > first * edge)
                    r["edge_to_other"] = r["both"]
            rows.append(r)
        pr = profile(h, l, c, v)
        prev = None if pr is None else dict(pr, pdh=h.max(), pdl=l.min(), close=c[-1])
    return rows


def run(codes=None):
    con = history.connect(LONG)
    try:
        cl = [r[0] for r in con.execute("SELECT DISTINCT code FROM bars_1m")]
    finally:
        con.close()
    codes = codes or [x for x in cl if x not in SKIP]
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for r in ex.map(run_code, codes):
            rows += r
    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = run()
    out = HERE / "data" / ".script_stats.pkl"
    df.to_pickle(out)
    print(len(df), "rows ->", out)
