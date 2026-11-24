# -*- coding: utf-8 -*-
"""
C1 / C2 / C3 打法（1 分钟图）：规则判断，回测、盘中监测、盘感训练共用同一份代码

  C1 = 开盘后第一段（上涨或下跌）　C2 = 回调　C3 = 第二段（和 C1 同方向）
  ① 开盘前 5 根不做，第 6 根（9:35–9:36）是信号K：
       方向 = 第 6 根收盘在开盘价上方 → 做多，下方 → 做空
       第 6 根要收在 VWAP 同一侧、颜色和方向一致、离开盘价至少 C1最小幅度×日线ATR
  ② C2：价格回踩到 VWAP（或第 6 根附近的 FVG）。碰到那一根不进
       回调越过 C1 起点（做多 = 跌破开盘后最低点）→ 这一波作废
  ③ 入场：回踩之后，VWAP 附近出现确认组合（收盘越过前一根整根高点，或吞没），
       而且收在 VWAP 正确一侧 → 下一根开盘价入场
  ④ 止损：C2 的极值外一点；目标：2R（同时给出 C1 极值）；11:00 还没入场就放弃
  只用当时已经收盘的K线，不会用到未来数据。参数在 设置.json →「C2打法」。
"""
import datetime as dt

import numpy as np
import pandas as pd

DEFAULT = {
    "C1最小幅度_ATR": 0.05,       # 第 6 根离开盘价至少多少倍日线 ATR
    "信号K颜色一致": True,
    "回踩区域": "VWAP或FVG",       # VWAP / VWAP或FVG
    "回踩容差_ATR": 0.0,
    "确认组合": "越过前高或吞没",  # 越过前高 / 吞没 / 越过前高或吞没
    "确认离VWAP_ATR": 0.05,       # 确认K收盘离 VWAP 不超过多少倍 ATR
    "回踩后最多等_根": 30,
    "止损缓冲_ATR": 0.01,
    "C1最小长度_ATR": 0.3,        # 入场时 C1 整段（起点到极值）至少多少倍 ATR；太短的 C1 后面常常走不出 C3
    "止损最小_ATR": 0.05,         # 止损比这个还近就不做（太近容易被噪音扫掉，成本占比也太高）
    "跳空逆向不做_ATR": 0.3,      # 开盘跳空方向和 C1 相反、且超过这么多倍 ATR → 不做（留空 = 不看跳空）
    "最晚入场": "11:00",
    "目标_R": 2.0,
    "说明": ("C1/C2/C3 打法的参数（1 分钟图）。2026-11-11 用 17 只大盘股一年的 1 分钟K校准：不加过滤平均每笔 +0.08R（扣成本后约 0），"
             "加上 C1 长度、止损距离、跳空三个过滤后 +0.16R。过滤条件是看全年数据挑的，还要靠影子监测继续验证。"),
}
SIG_IDX = 5                        # 第 6 根（从 0 数）


def settings(cfg=None):
    s = dict(DEFAULT)
    s.update((cfg or {}).get("C2打法") or {})
    return s


def _hm(x):
    h, m = str(x).split(":")
    return int(h) * 60 + int(m)


def scan(bars, atr_d, cfg=None, s=None, prev_close=None):
    """bars：当天常规时段 1 分钟K（index = 结束时间，按时间排好，只含已收盘的），atr_d：前一天的日线 ATR14。
    返回 dict：phase（等信号 / 不做 / C1 / 回踩中 / 入场 / 作废 / 过时）、dir、说明 reasons、以及各个价位。"""
    s = s or settings(cfg)
    out = dict(phase="等信号", dir=None, reasons=[], vwap=None, entry=None, stop=None, target=None, c1_ext=None,
               c1_origin=None, touch_t=None, trig_t=None, entry_t=None, fvg=None)
    if bars is None or not len(bars) or not atr_d or atr_d != atr_d:
        out["reasons"].append("数据不足")
        return out
    o, h, l, c, v = (bars[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
    t = bars.index.hour * 60 + bars.index.minute
    n = len(o)
    tp = (h + l + c) / 3
    vw = np.cumsum(tp * v) / np.maximum(np.cumsum(v), 1e-9)
    out["vwap"] = float(vw[-1])
    if n <= SIG_IDX:
        out["reasons"].append(f"开盘前 5 根不做，等第 6 根收盘（已收 {n} 根）")
        return out
    k = SIG_IDX
    open0 = o[0]
    d = 1 if c[k] > open0 else -1
    out["dir"] = "多" if d == 1 else "空"
    move = d * (c[k] - open0)
    word = "上方" if d == 1 else "下方"
    if move < s["C1最小幅度_ATR"] * atr_d:
        out.update(phase="不做", reasons=[f"第 6 根离开盘价只有 {move / atr_d:.2f} 倍 ATR，C1 不够明显"])
        return out
    if (c[k] - vw[k]) * d <= 0:
        out.update(phase="不做", reasons=[f"第 6 根没有收在 VWAP {word}"])
        return out
    if s["信号K颜色一致"] and (c[k] - o[k]) * d <= 0:
        out.update(phase="不做", reasons=["第 6 根颜色和 C1 方向相反"])
        return out
    gx = s.get("跳空逆向不做_ATR")
    if gx not in (None, "") and prev_close:
        gap = (open0 - prev_close) / atr_d
        if -gap * d > float(gx):
            out.update(phase="不做", reasons=[f"开盘{'低开' if d == 1 else '高开'} {abs(gap):.2f} 倍 ATR，和 C1 方向相反，历史上这种情况走不出 C3"])
            return out
    c1_origin = l[:k + 1].min() if d == 1 else h[:k + 1].max()
    ext = h[:k + 1].max() if d == 1 else l[:k + 1].min()
    fvg = None
    if k + 1 < n:
        if d == 1 and h[k - 1] < l[k + 1]:
            fvg = (float(h[k - 1]), float(l[k + 1]))
        if d == -1 and l[k - 1] > h[k + 1]:
            fvg = (float(h[k + 1]), float(l[k - 1]))
    out.update(phase="C1", c1_origin=float(c1_origin), c1_ext=float(ext), fvg=fvg,
               reasons=[f"C1 {'向上' if d == 1 else '向下'}：第 6 根收在开盘价和 VWAP {word}，等回踩 VWAP"
                        + ("或 FVG" if s["回踩区域"] == "VWAP或FVG" else "")])
    tol = s["回踩容差_ATR"] * atr_d
    last_entry = _hm(s["最晚入场"])
    touched, c2_ext, wait = None, None, 0
    for i in range(k + 1, n):
        if touched is None:
            if t[i] > last_entry:
                out.update(phase="过时", reasons=[f"{s['最晚入场']} 前没有回踩，今天不做"])
                return out
            ext = max(ext, h[i]) if d == 1 else min(ext, l[i])
            hit_vw = (l[i] <= vw[i] + tol) if d == 1 else (h[i] >= vw[i] - tol)
            hit_fvg = fvg is not None and ((l[i] <= fvg[1]) if d == 1 else (h[i] >= fvg[0]))
            if hit_vw or (s["回踩区域"] == "VWAP或FVG" and hit_fvg):
                touched, c2_ext, wait = i, (l[i] if d == 1 else h[i]), 0
                out.update(phase="回踩中", touch_t=f"{bars.index[i]:%H:%M}", c1_ext=float(ext),
                           reasons=[f"{bars.index[i]:%H:%M} 回踩到{'VWAP' if hit_vw else 'FVG'}，碰到那一根不进，等确认组合"])
            continue
        c2_ext = min(c2_ext, l[i]) if d == 1 else max(c2_ext, h[i])
        if (c2_ext - c1_origin) * d <= 0:
            out.update(phase="作废", reasons=[f"回调越过 C1 起点 {c1_origin:.2f}，这一波作废"])
            return out
        wait += 1
        if wait > s["回踩后最多等_根"] or t[i] > last_entry:
            out.update(phase="过时", reasons=["回踩后等太久没出确认组合，今天不做"])
            return out
        bull = (c[i] - o[i]) * d > 0
        prev_bear = (c[i - 1] - o[i - 1]) * d < 0
        brk = (c[i] - (h[i - 1] if d == 1 else l[i - 1])) * d > 0
        engulf = bull and prev_bear and (c[i] - o[i - 1]) * d >= 0 and (o[i] - c[i - 1]) * d <= 0
        near = abs(c[i] - vw[i]) <= s["确认离VWAP_ATR"] * atr_d
        mode = s["确认组合"]
        pat = (brk if "前高" in mode else False) or (engulf if "吞没" in mode else False)
        if pat and near and (c[i] - vw[i]) * d > 0:
            stop = c2_ext - d * s["止损缓冲_ATR"] * atr_d
            ref = c[i]                                   # 盘中按这根收盘价估算；实际成交按下一根开盘
            risk = (ref - stop) * d
            if risk <= float(s.get("止损最小_ATR") or 0.02) * atr_d:
                continue
            c1len = abs(ext - c1_origin) / atr_d
            if c1len < float(s.get("C1最小长度_ATR") or 0):
                out.update(phase="不做", reasons=[f"确认出现了，但 C1 只有 {c1len:.2f} 倍 ATR，太短，历史上后面常常走不出 C3"])
                return out
            name = "收盘越过前一根" + ("高点" if d == 1 else "低点") if brk else "吞没"
            out.update(phase="入场", trig_t=f"{bars.index[i]:%H:%M}", entry=float(ref), stop=float(stop),
                       target=float(ref + d * s["目标_R"] * risk), c1_ext=float(ext), trig_idx=i,
                       reasons=[f"{bars.index[i]:%H:%M} VWAP 附近出现确认（{name}），"
                                f"下一根开盘入场；止损 {stop:.2f}（C2 {'低' if d == 1 else '高'}点外），目标 {s['目标_R']:g}R"])
            return out
    return out


def outcome(bars, sig, tend="16:00"):
    """入场之后的结果（R）。bars 为当天全部常规时段 1 分钟K；sig 为 scan 返回的「入场」状态。
    下一根开盘价成交；同一根止损和目标都碰到算止损。"""
    i = sig.get("trig_idx")
    if i is None or i + 1 >= len(bars):
        return None
    d = 1 if sig["dir"] == "多" else -1
    o, h, l, c = (bars[x].values.astype(float) for x in ("open", "high", "low", "close"))
    t = bars.index.hour * 60 + bars.index.minute
    e = o[i + 1]
    stop = sig["stop"]
    risk = (e - stop) * d
    if risk <= 0:
        return dict(R=None, hit="开盘价已过止损", entry=e)
    target = e + d * (abs(sig["target"] - sig["entry"]) / abs(sig["entry"] - sig["stop"])) * risk
    for j in range(i + 1, len(o)):
        if t[j] > _hm(tend):
            return dict(R=d * (c[j - 1] - e) / risk, hit="到时间", entry=e)
        if (o[j] - stop) * d <= 0:
            return dict(R=d * (o[j] - e) / risk, hit="止损", entry=e)
        if ((l[j] if d == 1 else h[j]) - stop) * d <= 0:
            return dict(R=-1.0, hit="止损", entry=e)
        if ((h[j] if d == 1 else l[j]) - target) * d >= 0:
            return dict(R=d * (target - e) / risk, hit="目标", entry=e)
    return dict(R=d * (c[-1] - e) / risk, hit="到收盘", entry=e)


def prev_close(code, day, path=None):
    import history
    k = history.load(code, "1d", end=f"{day} 00:00:00", path=path)
    k = k[k.index.normalize() < pd.Timestamp(day)]
    return float(k["close"].iloc[-1]) if len(k) else None


def daily_atr(code, day, path=None):
    """前一天的日线 ATR14。"""
    import history
    k = history.load(code, "1d", end=f"{day} 00:00:00", path=path)
    k = k[k.index.normalize() < pd.Timestamp(day)].tail(20)
    if len(k) < 15:
        return None
    pc = k["close"].shift()
    tr = pd.concat([k["high"] - k["low"], (k["high"] - pc).abs(), (k["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def session(df):
    t = df.index.hour * 60 + df.index.minute
    return df[(t > 570) & (t <= 960)]


def backtest(codes=None, cfg=None, path=None, log=None):
    """用历史 1 分钟K回放，返回每笔交易 DataFrame（code, date, dir, trig_t, R, hit）。"""
    import history
    s = settings(cfg)
    con = history.connect(path)
    try:
        codes = codes or [r[0] for r in con.execute("SELECT DISTINCT code FROM bars_1m")]
    finally:
        con.close()
    rows = []
    for code in codes:
        m1 = session(history.load(code, "1m", path=path))
        d1 = history.load(code, "1d", path=path)
        pc = d1["close"].shift()
        tr = pd.concat([d1["high"] - d1["low"], (d1["high"] - pc).abs(), (d1["low"] - pc).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().shift()
        atr.index = atr.index.normalize()
        pcl = pc.copy()
        pcl.index = pcl.index.normalize()
        for day, b in m1.groupby(m1.index.normalize()):
            a = atr.get(day)
            if a is None or a != a or len(b) < 60:
                continue
            sig = scan(b, a, s=s, prev_close=pcl.get(day))
            if sig["phase"] != "入场":
                continue
            r = outcome(b, sig)
            if r and r.get("R") is not None:
                rows.append(dict(code=code, date=str(day.date()), dir=sig["dir"], trig_t=sig["trig_t"], R=r["R"], hit=r["hit"]))
        if log:
            log(f"  · C2 回测 {code}")
    return pd.DataFrame(rows)


def summary(df):
    if df is None or not len(df):
        return "还没有样本"
    days = df["date"].nunique()
    months = df.groupby(df["date"].str[:7]).R.mean()
    return (f"{len(df)} 笔（{days} 个交易日）：到目标 {(df.hit == '目标').mean() * 100:.0f}%，"
            f"赚钱 {(df.R > 0).mean() * 100:.0f}%，平均每笔 {df.R.mean():+.2f}R（未扣成本），"
            f"{(months > 0).sum()}/{len(months)} 个月是正的")


# ================================================================ 个人策略 A：爆发（原「打法 B」，大实体收盘站上 VWAP 追多，2026-11-17 交易者定义）
B_DEFAULT = {"实体_ATR": 0.10, "实体倍数": 2.5, "实体占比": 0.6, "最晚": "11:00", "前高最小距离_美元": 1.5}


def scan_b(bars, atr_d, qqq=None, prev_high=None, s=None):
    """只看最新收盘的那一根。条件：前 5 根之后、最晚之前；实体 ≥ 实体_ATR×日线 ATR 且 ≥ 前 20 根平均实体×实体倍数、
    实体占整根 ≥ 实体占比；阳线，收盘在 VWAP 上方、前一根收盘（或本根开盘）在 VWAP 下方；
    QQQ 同一分钟在开盘价和 VWAP 上方；前高（今天开盘以来最高点、昨高中离现价最近且在上方的）离收盘 ≥ 前高最小距离。
    返回 None（不满足）或 dict(entry 参考价, target 前高, dist, reasons)。只读，不下单。"""
    s = dict(B_DEFAULT, **(s or {}))
    if bars is None or len(bars) < 6 or not atr_d or atr_d != atr_d:
        return None
    t = bars.index[-1]
    if t.hour * 60 + t.minute > _hm(s["最晚"]):
        return None
    o, h, l, c, v = (bars[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
    tp = (h + l + c) / 3
    vw = np.cumsum(tp * v) / np.maximum(np.cumsum(v), 1e-9)
    i = len(c) - 1
    body = np.abs(c - o)
    avg = body[max(0, i - 20):i].mean()
    rng = h[i] - l[i]
    if not (c[i] > o[i] and body[i] >= s["实体_ATR"] * atr_d and body[i] >= s["实体倍数"] * avg and rng > 0
            and body[i] >= s["实体占比"] * rng):
        return None
    if not (c[i] > vw[i] and (c[i - 1] < vw[i - 1] or o[i] < vw[i])):
        return None
    if qqq is not None and len(qqq):
        qo, qh, ql, qc, qv = (qqq[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        qvw = np.cumsum((qh + ql + qc) / 3 * qv) / np.maximum(np.cumsum(qv), 1e-9)
        if not (qc[-1] > qo[0] and qc[-1] > qvw[-1]):
            return None
    else:
        return None
    above = [x for x in (h[:i].max() if i > 0 else None, prev_high) if x is not None and x > c[i]]
    if not above:
        return None
    tgt = min(above)
    if tgt - c[i] < s["前高最小距离_美元"]:
        return None
    return dict(entry=float(c[i]), target=float(tgt), dist=float(tgt - c[i]), vwap=float(vw[i]),
                reasons=[f"{t:%H:%M} 大实体阳线站上 VWAP {vw[i]:.2f}，实体是前 20 根平均的 {body[i] / max(avg, 1e-9):.1f} 倍；"
                         f"QQQ 同向；前高 {tgt:.2f}，距离 ${tgt - c[i]:.2f}"])
