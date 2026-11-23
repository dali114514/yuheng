# -*- coding: utf-8 -*-
"""
六行看盘：最近 1 / 3 / 5 / 10 / 15 / 30 根 1 分钟K，各用一句大白话说清楚，再附「历史上这样之后 15 分钟涨的比例」（「合起来」那句由 dash.py 的结论卡给出）

  分类（每个窗口只用这一段和它之前的K线）：
    1 根：大阳线 / 大阴线 / 长下影 / 长上影 / 十字星 / 小K线 / 阳线 / 阴线
    3 根以上：单边涨 / 单边跌 / 先跌后收回 / 先涨后回落 / 偏涨 / 偏跌 / 来回
      效率 = |收 - 开| ÷ (最高 - 最低)；≥ 0.6 单边，0.3–0.6 偏涨 / 偏跌；
      最低点在中间、收在区间上部 30% 以内 = 先跌后收回（反过来是先涨后回落）；其余 = 来回
  历史统计：一年 1 分钟K、9:36–11:00 的每一分钟，存 data/.ladder_stats.json（ladder.py --build，约 1 分钟）
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STATS = HERE / "data" / ".ladder_stats.json"
WINS = (1, 3, 5, 10, 15, 30)
H = 15
NAME = {1: "最近 1 根", 3: "最近 3 根", 5: "最近 5 根", 10: "最近 10 根", 15: "最近 15 根", 30: "最近 30 根"}
UPS = {"大阳线", "长下影", "阳线", "单边涨", "先跌后收回", "偏涨"}
DOWNS = {"大阴线", "长上影", "阴线", "单边跌", "先涨后回落", "偏跌"}


def classify(o, h, l, c, i, N, avgb, avgr):
    """第 i 根收盘时，最近 N 根是什么状态。返回类别名或 None（不够 N 根）。"""
    if i - N + 1 < 0:
        return None
    if N == 1:
        rng, body = h[i] - l[i], abs(c[i] - o[i])
        dn, up = min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])
        if rng <= 0:
            return "小K线"
        if avgb == avgb and body >= 2 * avgb and body >= 0.6 * rng:
            return "大阳线" if c[i] > o[i] else "大阴线"
        if avgr == avgr and rng < 0.5 * avgr:
            return "小K线"
        if dn >= 2 * body and dn >= 0.5 * rng:
            return "长下影"
        if up >= 2 * body and up >= 0.5 * rng:
            return "长上影"
        if body <= 0.1 * rng:
            return "十字星"
        return "阳线" if c[i] > o[i] else "阴线"
    a = i - N + 1
    hi, lo = h[a:i + 1].max(), l[a:i + 1].min()
    rg = hi - lo
    if rg <= 0:
        return "来回"
    net = c[i] - o[a]
    eff = abs(net) / rg
    pos = (c[i] - lo) / rg
    if eff >= 0.6:
        return "单边涨" if net > 0 else "单边跌"
    k_lo, k_hi = a + int(np.argmin(l[a:i + 1])), a + int(np.argmax(h[a:i + 1]))
    if a < k_lo < i and pos >= 0.7:
        return "先跌后收回"
    if a < k_hi < i and pos <= 0.3:
        return "先涨后回落"
    if eff >= 0.3:
        return "偏涨" if net > 0 else "偏跌"
    return "来回"


def describe(cat, N, o, h, l, c, i, atr):
    """一句大白话（带价格）。"""
    if N == 1:
        txt = {"大阳线": "大阳线，实体是平时的两倍以上：这一分钟买方一下子压过卖方",
               "大阴线": "大阴线，实体是平时的两倍以上：这一分钟卖方一下子压过买方",
               "长下影": f"长下影，打到 {l[i]:.2f} 又被买回来：下面有人接",
               "长上影": f"长上影，冲到 {h[i]:.2f} 又被卖回去：上面有人卖",
               "十字星": "十字星，开收几乎一样：这一分钟买卖打平",
               "小K线": "很小的一根：这一分钟没什么人动",
               "阳线": "普通阳线", "阴线": "普通阴线"}[cat]
        return txt
    a = i - N + 1
    hi, lo = h[a:i + 1].max(), l[a:i + 1].min()
    mv = (c[i] - o[a]) / atr if atr else 0
    if cat == "单边涨":
        return f"一路往上，从 {o[a]:.2f} 到 {c[i]:.2f}（{mv:+.2f} 倍 ATR），回头很少"
    if cat == "单边跌":
        return f"一路往下，从 {o[a]:.2f} 到 {c[i]:.2f}（{mv:+.2f} 倍 ATR），反弹很少"
    if cat == "先跌后收回":
        return f"先跌到 {lo:.2f}，又收回到 {c[i]:.2f}（区间上部）"
    if cat == "先涨后回落":
        return f"先冲到 {hi:.2f}，又落回 {c[i]:.2f}（区间下部）"
    if cat == "偏涨":
        return f"偏涨，但来回比较多（{lo:.2f}–{hi:.2f}）"
    if cat == "偏跌":
        return f"偏跌，但来回比较多（{lo:.2f}–{hi:.2f}）"
    return f"在 {lo:.2f}–{hi:.2f} 之间来回，没有方向（高 ${hi - lo:.2f}）"


def _avg(x, i):
    s = x[max(0, i - 20):i]
    return float(s.mean()) if len(s) >= 5 else float("nan")


def rows(b, atr):
    """b：今天到现在的 1 分钟K（index = 结束时间）。返回 6 行 dict(win, name, cat, text, stat)。"""
    o, h, l, c = (b[x].values.astype(float) for x in ("open", "high", "low", "close"))
    i = len(c) - 1
    body, rng = np.abs(c - o), h - l
    st = load_stats()
    out = []
    for N in WINS:
        cat = classify(o, h, l, c, i, N, _avg(body, i), _avg(rng, i))
        if cat is None:
            out.append(dict(win=N, name=NAME[N], cat=None, text=f"开盘才 {i + 1} 根，还不够 {N} 根", stat=None))
            continue
        s = ((st or {}).get(str(N)) or {}).get(cat)
        out.append(dict(win=N, name=NAME[N], cat=cat, text=describe(cat, N, o, h, l, c, i, atr),
                        lean=1 if cat in UPS else -1 if cat in DOWNS else 0, stat=s))
    return out


def build_stats(log=print):
    import c2
    import history
    import patterns as P
    con = history.connect()
    try:
        codes = [r[0] for r in con.execute("SELECT code FROM bars_1m GROUP BY code HAVING COUNT(*) > 50000")]
    finally:
        con.close()
    acc = {}
    for code in codes:
        m1 = c2.session(history.load(code, "1m"))
        atr = P._atr_series(history.load(code, "1d"))
        for day, b in m1.groupby(m1.index.normalize()):
            a = atr.get(day)
            if a is None or a != a or len(b) < 300:
                continue
            o, h, l, c = (b[x].values.astype(float) for x in ("open", "high", "low", "close"))
            t = b.index.hour * 60 + b.index.minute
            body, rng = np.abs(c - o), h - l
            for i in np.where((t >= 576) & (t <= 660))[0]:
                if i + H >= len(c):
                    continue
                f = c[i + H] - c[i]
                ab, ar = _avg(body, i), _avg(rng, i)
                for N in WINS:
                    cat = classify(o, h, l, c, i, N, ab, ar)
                    if cat is None:
                        continue
                    x = acc.setdefault((N, cat), [0, 0, 0.0])
                    x[0] += 1
                    x[1] += f > 0
                    x[2] += f / a
        if log:
            log(f"  · {code}")
    out = {}
    for (N, cat), (n, up, s) in acc.items():
        out.setdefault(str(N), {})[cat] = dict(n=n, up=round(up / n * 100, 1), mean=round(s / n, 4))
    STATS.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


_C = {}


def load_stats():
    try:
        m = STATS.stat().st_mtime
        if _C.get("m") != m:
            _C.update(m=m, d=json.loads(STATS.read_text(encoding="utf-8")))
        return _C["d"]
    except Exception:
        return None


if __name__ == "__main__":
    if "--build" in sys.argv:
        build_stats()
        print("ok", STATS)
