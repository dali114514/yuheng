# -*- coding: utf-8 -*-
"""
形态监测（1 分钟 / 5 分钟）：信号K、组合K、标准形态、震荡区间和突破；历史概率；ABC 分级

  · 同一份识别代码给「历史统计」和「盘中监测」用，只用当时已经收盘的K线，不偷看未来
  · 历史概率：一年 1 分钟K，9:36–11:00 出现的形态，往形态方向看后面 5/15 根（5 分钟图：3/6 根）
      和「同一时间段随便一根K往同方向」的基准比；按日期切成前段 / 后段，看两段是不是都站得住
  · 每个形态一张卡片：一句话道理、样本数、胜率和基准、平均走多少（倍日线 ATR）、扣双倍成本、前后两段、结论
  · 盘中：c2watch 每分钟调用 live()，写 数据/热数据/形态.md，按设置弹提醒
  · ABC 分级：VWAP 同侧、C1 方向、QQQ 同向、在关键位、止损放得下（≤ 止损_美元）五条，全中 A，差一条 B，其余 C
  只读行情，不下单。设置在 设置.json →「形态监测」。
"""
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
DATA = HERE / "data"
STATS = DATA / ".pattern_stats.json"
HOTMD = BASE / "数据" / "热数据" / "形态.md"

DEFAULT = {
    "提醒": "突破和A/B级",          # 关 / 只记录 / 只提醒突破 / 突破和A级 / 突破和A/B级
    "震荡最少根数": 12,
    "震荡最大高度_ATR": 0.12,       # 区间高度不超过这么多倍日线 ATR 才算震荡
    "止损_美元": 1.0,               # 每张权利金亏 0.5 ÷ Delta 0.5 ≈ 股价 $1
    "关键位距离_ATR": 0.05,
    "同一只最短间隔_分钟": 5,
    "说明": ("形态监测（1/5 分钟）。提醒：关 / 只记录 / 只提醒突破 / 突破和A级 / 突破和A/B级。"
             "震荡 = 至少「震荡最少根数」根K挤在「震荡最大高度_ATR」倍日线 ATR 以内；收盘站出边界、下一根没收回才算突破。"
             "止损_美元 用来判断止损放不放得下（ABC 分级的一条）。"),
}
SPLIT = "2026-07-01"          # 历史统计的前段 / 后段分界
COST = 0.01                   # 每笔成本，倍日线 ATR（CLAUDE.md 里一直用的口径）
WIN = {"1分钟": (576, 660, (5, 15)), "5分钟": (580, 660, (3, 6))}     # 统计用的信号时间段（分钟数）、往后看几根

# 名字: (方向, 一句话道理)
CARDS = {
    "锤子线": (1, "打出近 10 根的新低后被买回来、留下长下影：下面有人接。"),
    "流星线": (-1, "冲出近 10 根的新高后被卖回去、留下长上影：上面有人卖。"),
    "看涨吞没": (1, "一根阳线把前一根阴线整根吃掉：买方一下子压过卖方。"),
    "看跌吞没": (-1, "一根阴线把前一根阳线整根吃掉：卖方一下子压过买方。"),
    "回调后越过前高": (1, "回调了两三根，又收盘越过前三根的高点：回调可能结束（就是 C2 的确认K）。"),
    "反弹后跌破前低": (-1, "反弹了两三根，又收盘跌破前三根的低点：反弹可能结束。"),
    "大阳线": (1, "实体是前 20 根平均的 2.5 倍以上：有人突然大举买入。"),
    "大阴线": (-1, "实体是前 20 根平均的 2.5 倍以上：有人突然大举卖出。"),
    "内包线向上突破": (1, "先缩成一根被包住的小K（犹豫），再收盘越过母线高点：犹豫结束、选了向上。"),
    "内包线向下突破": (-1, "先缩成一根被包住的小K（犹豫），再收盘跌破母线低点：犹豫结束、选了向下。"),
    "三连阳": (1, "连续三根阳线、一根比一根高：短线惯性向上。"),
    "三连阴": (-1, "连续三根阴线、一根比一根低：短线惯性向下。"),
    "十字星（涨后）": (-1, "涨了一段后出现开收几乎一样的K：买方没力气了，可能歇一歇或回头。"),
    "十字星（跌后）": (1, "跌了一段后出现开收几乎一样的K：卖方没力气了，可能歇一歇或回头。"),
    "双顶跌破颈线": (-1, "两次冲到差不多的高点都没过去，然后跌破中间的低点（颈线）：上面压力确认。"),
    "双底升破颈线": (1, "两次跌到差不多的低点都没破，然后升破中间的高点（颈线）：下面支撑确认。"),
    "震荡向上突破": (1, "价格挤在一个窄区间里，然后收盘站上上沿、下一根没跌回去：多方赢了这场拉锯。"),
    "震荡向下突破": (-1, "价格挤在一个窄区间里，然后收盘跌破下沿、下一根没收回去：空方赢了这场拉锯。"),
    "向上假突破": (-1, "收盘站上震荡上沿，下一根就跌回区间：上面的买单被困住，可能往下走。"),
    "向下假突破": (1, "收盘跌破震荡下沿，下一根就收回区间：下面的卖单被困住，可能往上走。"),
}
RANGE_NAMES = ("震荡向上突破", "震荡向下突破", "向上假突破", "向下假突破")
CTX = ("全部", "顺VWAP", "逆VWAP", "QQQ同向", "QQQ反向", "顺VWAP+QQQ同向", "贴着VWAP")


def settings(cfg=None):
    s = dict(DEFAULT)
    s.update((cfg or {}).get("形态监测") or {})
    return s


def _sh(a, k):
    a = np.asarray(a, dtype=float)
    if k <= 0:
        return a
    return np.r_[np.full(k, np.nan), a[:-k]] if len(a) > k else np.full(len(a), np.nan)


def vwap(h, l, c, v):
    return np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)


def to5(b):
    """1 分钟K（常规时段，index = 结束时间）合成 5 分钟K（结束时间 9:35、9:40 …），只要完整的 5 根。"""
    if b is None or not len(b):
        return b
    t = b.index.hour * 60 + b.index.minute
    g = (t - 571) // 5
    agg = b.groupby(g).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                           close=("close", "last"), volume=("volume", "sum"), n=("close", "size"))
    agg = agg[agg.n == 5]
    day = b.index[0].normalize()
    agg.index = [day + pd.Timedelta(minutes=int(570 + 5 * (k + 1))) for k in agg.index]
    return agg.drop(columns="n")


# ================================================================ 识别
def detect(o, h, l, c):
    """每根K是不是某个形态（只用这一根和它之前的K）。返回 {名字: bool 数组}。"""
    body, rng = np.abs(c - o), h - l
    up, dn = h - np.maximum(o, c), np.minimum(o, c) - l
    avgb = pd.Series(body).shift(1).rolling(20, min_periods=5).mean().values
    avgr = pd.Series(rng).shift(1).rolling(20, min_periods=5).mean().values
    lo10 = pd.Series(l).shift(1).rolling(10, min_periods=3).min().values
    hi10 = pd.Series(h).shift(1).rolling(10, min_periods=3).max().values
    o1, h1, l1, c1 = _sh(o, 1), _sh(h, 1), _sh(l, 1), _sh(c, 1)
    o2, h2, l2, c2 = _sh(o, 2), _sh(h, 2), _sh(l, 2), _sh(c, 2)
    h3, l3, o3, c3 = _sh(h, 3), _sh(l, 3), _sh(o, 3), _sh(c, 3)
    body1 = np.abs(c1 - o1)
    bull, bear = c > o, c < o
    with np.errstate(invalid="ignore"):
        nbear3 = (c1 < o1).astype(int) + (c2 < o2).astype(int) + (c3 < o3).astype(int)
        nbull3 = (c1 > o1).astype(int) + (c2 > o2).astype(int) + (c3 > o3).astype(int)
        out = {
            "锤子线": (rng >= 0.8 * avgr) & (dn >= 2 * body) & (up <= np.maximum(body, 0.15 * rng)) & (l < lo10),
            "流星线": (rng >= 0.8 * avgr) & (up >= 2 * body) & (dn <= np.maximum(body, 0.15 * rng)) & (h > hi10),
            "看涨吞没": bull & (c1 < o1) & (c >= o1) & (o <= c1) & (body > body1),
            "看跌吞没": bear & (c1 > o1) & (c <= o1) & (o >= c1) & (body > body1),
            "回调后越过前高": bull & (c > np.fmax(np.fmax(h1, h2), h3)) & (nbear3 >= 2),
            "反弹后跌破前低": bear & (c < np.fmin(np.fmin(l1, l2), l3)) & (nbull3 >= 2),
        }
        big = (body >= 2.5 * avgb) & (body >= 0.6 * rng)
        out["大阳线"], out["大阴线"] = big & bull, big & bear
        inside1 = (h1 < h2) & (l1 > l2)
        out["内包线向上突破"] = inside1 & (c > h2)
        out["内包线向下突破"] = inside1 & (c < l2)
        out["三连阳"] = bull & (c1 > o1) & (c2 > o2) & (c > c1) & (c1 > c2) & (h > h1) & (h1 > h2)
        out["三连阴"] = bear & (c1 < o1) & (c2 < o2) & (c < c1) & (c1 < c2) & (l < l1) & (l1 < l2)
        doji = (body <= 0.1 * rng) & (rng >= 0.8 * avgr)
        mv5 = c - _sh(c, 5)
        out["十字星（涨后）"] = doji & (mv5 > 1.5 * avgr)
        out["十字星（跌后）"] = doji & (mv5 < -1.5 * avgr)
    dt_, db_ = double_top_bottom(h, l, c, avgr)
    out["双顶跌破颈线"], out["双底升破颈线"] = dt_, db_
    for k in out:
        out[k] = np.nan_to_num(out[k].astype(float)).astype(bool)
    return out


def double_top_bottom(h, l, c, avgr, k=2):
    """摆动高点 = 前后各 k 根里最高（右边 k 根收盘后才知道）。两个摆动高点相差不到 0.5 根平均振幅、
    相隔至少 5 根，之后收盘跌破两者之间的最低点 → 双顶（第一次跌破那根）。双底对称。"""
    n = len(c)
    top, bot = np.zeros(n, bool), np.zeros(n, bool)
    piv_h = [i for i in range(k, n - k) if h[i] == h[i - k:i + k + 1].max()]
    piv_l = [i for i in range(k, n - k) if l[i] == l[i - k:i + k + 1].min()]
    used = set()
    for a_i, a in enumerate(piv_h):
        for b in piv_h[a_i + 1:]:
            if b - a < 5:
                continue
            tol = 0.5 * (avgr[b] if avgr[b] == avgr[b] else h[b] - l[b])
            if abs(h[a] - h[b]) > tol or h[a + 1:b].max() > max(h[a], h[b]):
                continue
            neck = l[a:b + 1].min()
            for j in range(b + k, n):                  # 第 b+k 根收盘后才知道 b 是摆动高点
                if h[j] > max(h[a], h[b]):
                    break
                if c[j] < neck:
                    if j not in used:
                        top[j] = True
                        used.add(j)
                    break
            break
    used = set()
    for a_i, a in enumerate(piv_l):
        for b in piv_l[a_i + 1:]:
            if b - a < 5:
                continue
            tol = 0.5 * (avgr[b] if avgr[b] == avgr[b] else h[b] - l[b])
            if abs(l[a] - l[b]) > tol or l[a + 1:b].min() < min(l[a], l[b]):
                continue
            neck = h[a:b + 1].max()
            for j in range(b + k, n):
                if l[j] < min(l[a], l[b]):
                    break
                if c[j] > neck:
                    if j not in used:
                        bot[j] = True
                        used.add(j)
                    break
            break
    return top, bot


def boxes(h, l, atr, s):
    """每根K之前（不含这一根）最长的震荡区间：返回 hi, lo, 根数（没有 = 0）。"""
    n = len(h)
    bh, bl, bn = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, int)
    maxh = float(s["震荡最大高度_ATR"]) * atr
    for L in sorted({int(s["震荡最少根数"]), 20, 30, 45, 60}):
        hi = pd.Series(h).shift(1).rolling(L).max().values
        lo = pd.Series(l).shift(1).rolling(L).min().values
        ok = (hi - lo) <= maxh
        ok = np.nan_to_num(ok.astype(float)).astype(bool)
        bh[ok], bl[ok], bn[ok] = hi[ok], lo[ok], L
    return bh, bl, bn


def range_events(h, l, c, atr, s):
    """震荡突破：第 i 根收盘站出区间，第 i+1 根没收回 → 突破（事件在 i+1）；收回了 → 假突破。
    返回 [(j, 名字, 方向, 上沿, 下沿, 根数)]。同一个区间只算一次。"""
    bh, bl, bn = boxes(h, l, atr, s)
    ev, skip_to = [], -1
    for i in range(1, len(c) - 1):
        if i < skip_to or not bn[i]:
            continue
        j = i + 1
        if c[i] > bh[i]:
            ev.append((j, "震荡向上突破" if c[j] > bh[i] else "向上假突破", 1 if c[j] > bh[i] else -1, bh[i], bl[i], bn[i]))
        elif c[i] < bl[i]:
            ev.append((j, "震荡向下突破" if c[j] < bl[i] else "向下假突破", -1 if c[j] < bl[i] else 1, bh[i], bl[i], bn[i]))
        else:
            continue
        skip_to = j + int(s["震荡最少根数"])
    return ev


def current_box(h, l, atr, s):
    """最新一根K也算进去：现在是不是在震荡里。返回 (上沿, 下沿, 根数) 或 None。"""
    if len(h) < int(s["震荡最少根数"]) or not atr:
        return None
    maxh = float(s["震荡最大高度_ATR"]) * atr
    best = None
    for L in range(int(s["震荡最少根数"]), min(len(h), 90) + 1):
        hi, lo = h[-L:].max(), l[-L:].min()
        if hi - lo > maxh:
            break
        best = (float(hi), float(lo), L)
    return best


# ================================================================ 历史统计
def _atr_series(d1):
    pc = d1["close"].shift()
    tr = pd.concat([d1["high"] - d1["low"], (d1["high"] - pc).abs(), (d1["low"] - pc).abs()], axis=1).max(axis=1)
    a = tr.rolling(14).mean().shift()
    a.index = a.index.normalize()
    return a


def _range_R(o, h, l, c, t, j, d, bh, bl, atr):
    """突破事件的交易结果（已扣成本 0.01 ATR）：下一根开盘进，止损在区间另一边外 0.01 ATR，目标 2R，16:00 前没结果按收盘算。"""
    if j + 1 >= len(o):
        return None
    e = o[j + 1]
    stop = (bl - 0.01 * atr) if d == 1 else (bh + 0.01 * atr)
    risk = (e - stop) * d
    if risk <= 0:
        return None
    tgt = e + d * 2 * risk
    cost = COST * atr / risk                       # 成本换算成 R：区间越窄，成本占比越大
    for k in range(j + 1, len(o)):
        if ((l[k] if d == 1 else h[k]) - stop) * d <= 0:
            return -1.0 - cost
        if ((h[k] if d == 1 else l[k]) - tgt) * d >= 0:
            return 2.0 - cost
    return d * (c[-1] - e) / risk - cost


def build_stats(codes=None, log=print, path=None, cfg=None):
    """用历史 1 分钟K算全部形态的概率，写 data/.pattern_stats.json 和 报告/形态概率_<日期>.md。"""
    import c2
    import history
    s = settings(cfg)
    con = history.connect(path)
    try:
        codes = codes or [r[0] for r in con.execute("SELECT code FROM bars_1m GROUP BY code HAVING COUNT(*) > 50000")]
    finally:
        con.close()
    qm = c2.session(history.load("QQQ", "1m", path=path))
    qdays = {d: g for d, g in qm.groupby(qm.index.normalize())}
    ev_rows, base = [], {f: [] for f in WIN}
    for code in codes:
        m1 = c2.session(history.load(code, "1m", path=path))
        atr = _atr_series(history.load(code, "1d", path=path))
        for day, b in m1.groupby(m1.index.normalize()):
            a = atr.get(day)
            q = qdays.get(day)
            if a is None or a != a or len(b) < 300 or q is None:
                continue
            for frame, (t0, t1, hz) in WIN.items():
                bb = b if frame == "1分钟" else to5(b)
                qq = q if frame == "1分钟" else to5(q)
                if bb is None or len(bb) < 20:
                    continue
                o, h, l, c, v = (bb[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
                t = bb.index.hour * 60 + bb.index.minute
                vw = vwap(h, l, c, v)
                qa = qq.reindex(bb.index)
                qvw = pd.Series(vwap(*(qq[x].values.astype(float) for x in ("high", "low", "close", "volume"))),
                                index=qq.index).reindex(bb.index).values
                qc, qo = qa["close"].values.astype(float), float(qq["open"].iloc[0])
                with np.errstate(invalid="ignore"):
                    qup = np.where((qc > qvw) & (qc > qo), 1, np.where((qc < qvw) & (qc < qo), -1, 0))
                f1 = (_sh(c[::-1], hz[0])[::-1] - c) / a       # 往后 hz 根的收盘 - 现在收盘
                f2 = (_sh(c[::-1], hz[1])[::-1] - c) / a
                side = np.sign(c - vw)
                near = np.abs(c - vw) <= float(s["关键位距离_ATR"]) * a
                mask = (t >= t0) & (t <= t1)
                ds = str(day.date())
                for i in np.where(mask)[0]:
                    base[frame].append((ds, f1[i], f2[i], side[i], qup[i], near[i]))
                det = detect(o, h, l, c)
                for name, arr in det.items():
                    d = CARDS[name][0]
                    for i in np.where(arr & mask)[0]:
                        ev_rows.append((frame, name, d, code, ds, d * f1[i], d * f2[i], side[i] * d, qup[i] * d, near[i], np.nan))
                for (j, name, d, bh, bl, L) in range_events(h, l, c, a, s):
                    if not (t0 <= t[j] <= t1):
                        continue
                    r = _range_R(o, h, l, c, t, j, d, bh, bl, a) if name in ("震荡向上突破", "震荡向下突破") else np.nan
                    ev_rows.append((frame, name, d, code, ds, d * f1[j], d * f2[j], side[j] * d, qup[j] * d, near[j],
                                    np.nan if r is None else r))
        log(f"  · 形态统计 {code}")
    ev = pd.DataFrame(ev_rows, columns=["frame", "name", "dir", "code", "date", "f1", "f2", "vs", "qq", "near", "R"])
    out = dict(at=dt.date.today().isoformat(), split=SPLIT, codes=codes, frames={})
    for frame, (t0, t1, hz) in WIN.items():
        bs = pd.DataFrame(base[frame], columns=["date", "f1", "f2", "side", "qup", "near"]).dropna(subset=["f2"])
        out["frames"][frame] = dict(hz=hz, cards={})
        e = ev[(ev.frame == frame)].dropna(subset=["f2"])
        for name, (d, why) in CARDS.items():
            g = e[e.name == name]
            card = dict(dir=d, why=why, ctx={})
            for cx in CTX:
                gg, bb = _ctx(g, cx, False), _ctx(_flip(bs, d), cx, True)
                card["ctx"][cx] = _cell(gg, bb)
            card["verdict"] = _verdict(card["ctx"]["全部"])
            out["frames"][frame]["cards"][name] = card
    DATA.mkdir(exist_ok=True)
    STATS.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    p = BASE / "报告" / f"形态概率_{out['at']}.md"
    p.write_text(report_md(out), encoding="utf-8")
    log(f"形态概率已更新：{p.name}（{len(ev)} 个历史形态）")
    return out


def _flip(bs, d):
    x = bs.copy()
    x["f1"], x["f2"] = bs.f1 * d, bs.f2 * d
    x["vs"], x["qq"] = bs.side * d, bs.qup * d
    return x


def _ctx(g, cx, is_base):
    if cx == "全部":
        return g
    if cx == "顺VWAP":
        return g[g.vs > 0]
    if cx == "逆VWAP":
        return g[g.vs < 0]
    if cx == "QQQ同向":
        return g[g.qq > 0]
    if cx == "QQQ反向":
        return g[g.qq < 0]
    if cx == "顺VWAP+QQQ同向":
        return g[(g.vs > 0) & (g.qq > 0)]
    if cx == "贴着VWAP":
        return g[g.near.astype(bool)]
    return g


def _stat(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if not len(x):
        return None
    m = float(x.mean())
    se = float(x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 1 else 0.0
    return dict(n=int(len(x)), win=round(float((x > 0).mean()) * 100, 1), mean=round(m, 4),
                lo=round(m - 1.96 * se, 4), hi=round(m + 1.96 * se, 4))


def _cell(g, b):
    st = _stat(g.f2)
    if not st:
        return dict(n=0)
    bt = _stat(b.f2) or dict(win=50.0, mean=0.0)
    st.update(base_win=bt["win"], base_mean=bt["mean"], s1=_stat(g.f1),
              days=int(g.date.nunique()))
    for half, gg, bb in (("前段", g[g.date < SPLIT], b[b.date < SPLIT]), ("后段", g[g.date >= SPLIT], b[b.date >= SPLIT])):
        a1, b1 = _stat(gg.f2), _stat(bb.f2)
        st[half] = dict(n=a1["n"], win=a1["win"], mean=a1["mean"], base_win=(b1 or {}).get("win", 50.0),
                        base_mean=(b1 or {}).get("mean", 0.0)) if a1 else dict(n=0)
    if "R" in g and g.R.notna().any():
        st["R"] = _stat(g.R)
    return st


def _verdict(c):
    """结论：样本不够 / 有一点证据 / 不稳定 / 没优势 / 反向（按形态方向做反而亏）。"""
    if not c.get("n") or c["n"] < 50:
        return "样本不够"
    ok = []
    for half in ("前段", "后段"):
        h = c.get(half) or {}
        if not h.get("n") or h["n"] < 25:
            ok.append(None)
            continue
        ok.append(h["win"] - h["base_win"] >= 3 and h["mean"] - h["base_mean"] - COST > 0)
    net2 = c["mean"] - c["base_mean"] - 2 * COST
    if c["hi"] < c["base_mean"]:
        return "反向"
    if all(x is True for x in ok) and net2 > 0:
        return "有一点证据"
    if any(x is True for x in ok):
        return "不稳定"
    return "没优势"


def _fmt_cell(c, hz2):
    if not c.get("n"):
        return "没有样本"
    t = (f"{c['n']} 次：后 {hz2} 根往形态方向 {c['win']:.0f}%（基准 {c['base_win']:.0f}%），"
         f"平均 {c['mean']:+.3f} ATR（基准 {c['base_mean']:+.3f}）")
    if c.get("R"):
        t += f"；按区间另一边止损、2R 目标做，扣成本后平均 {c['R']['mean']:+.2f}R（{c['R']['lo']:+.2f}～{c['R']['hi']:+.2f}；区间窄，成本约占 0.07R）"
    return t


def report_md(st):
    L = [f"# 形态概率（{st['at']}）", "",
         f"数据：{len(st['codes'])} 只股票的历史 1 分钟K（5 分钟图由 1 分钟合成），只看 9:36–11:00 出现的形态。",
         "",
         "**怎么读：**",
         "- 「往形态方向 X%」= 形态出现后，后面几根收在形态方向的比例；「基准」= 同一时间段随便一根K往同方向的比例。差 3 个百分点以内基本等于随便猜。",
         "- 「平均」= 后面几根平均走了多少，单位是倍日线 ATR。成本约 0.01 ATR 一笔，期权的价差和时间损耗更大。",
         f"- 前段 = {st['split']} 以前，后段 = 之后。两段都比基准好、扣两倍成本还为正，才写「有一点证据」。",
         "- **同时检验了二十种形态 × 七种情况 × 两种周期，几百个格子里总有几个碰巧好看。** 单个格子好看不算数，要看「全部」那一行和前后两段。",
         "- 同一天的股票走势相关，真实的不确定性比数字看起来大。", ""]
    for frame, F in st["frames"].items():
        hz = F["hz"]
        L += [f"## {frame}图（往后看 {hz[0]} / {hz[1]} 根）", "",
              "| 形态 | 方向 | 次数 | 往方向（基准） | 平均 ATR（基准） | 前段 | 后段 | 结论 |", "|---|---|---|---|---|---|---|---|"]
        for name, cd in F["cards"].items():
            c = cd["ctx"]["全部"]
            if not c.get("n"):
                L.append(f"| {name} | {'多' if cd['dir'] > 0 else '空'} | 0 | | | | | 样本不够 |")
                continue
            hf = lambda h: f"{h['win']:.0f}%/{h['base_win']:.0f}%（{h['n']}）" if h.get("n") else "—"
            L.append(f"| {name} | {'多' if cd['dir'] > 0 else '空'} | {c['n']} | {c['win']:.0f}%（{c['base_win']:.0f}%） | "
                     f"{c['mean']:+.3f}（{c['base_mean']:+.3f}） | {hf(c.get('前段') or {})} | {hf(c.get('后段') or {})} | {cd['verdict']} |")
        L.append("")
        L.append(f"### {frame}图 · 每个形态的卡片")
        L.append("")
        for name, cd in F["cards"].items():
            L.append(f"**{name}**（{'看多' if cd['dir'] > 0 else '看空'}）— {cd['why']}　结论：**{cd['verdict']}**")
            L.append("")
            for cx in CTX:
                L.append(f"- {cx}：{_fmt_cell(cd['ctx'][cx], hz[1])}")
            L.append("")
    return "\n".join(L)


_ST = {}


def load_stats():
    try:
        m = STATS.stat().st_mtime
        if _ST.get("m") != m:
            _ST.update(m=m, d=json.loads(STATS.read_text(encoding="utf-8")))
        return _ST["d"]
    except Exception:
        return None


def prob_text(frame, name, ctx_list):
    """盘中用：这个形态在历史上的表现（全部 + 现在所在的情况）。"""
    st = load_stats()
    if not st:
        return "（还没有历史统计：收盘后自动生成，或手动跑 patterns.py --build）"
    F = st["frames"].get(frame) or {}
    cd = (F.get("cards") or {}).get(name)
    if not cd:
        return ""
    hz2 = F["hz"][1]
    c = cd["ctx"]["全部"]
    if not c.get("n"):
        return "历史上没出现过"
    t = f"历史 {c['n']} 次，后 {hz2} 根往方向 {c['win']:.0f}%（基准 {c['base_win']:.0f}%），结论「{cd['verdict']}」"
    for cx in ctx_list:
        cc = cd["ctx"].get(cx) or {}
        if cc.get("n", 0) >= 30:
            t += f"；{cx}时 {cc['n']} 次 {cc['win']:.0f}%（基准 {cc['base_win']:.0f}%）"
            break
    return t


# ================================================================ ABC 分级
def grade(d, plan_dir, side_ok, c1_dir, qqq_ok, at_level, stop_usd, s, tmin):
    """返回 (级别, 逐条核对)。级别：A / B / C / 纪律不允许。"""
    checks = []
    if tmin < 576:
        return "纪律不允许", ["✗ 开盘前 5 根不做"]
    if tmin > 660:
        return "纪律不允许", ["✗ 11:00 以后不开新仓"]
    want = {"多": 1, "空": -1}.get(plan_dir)
    if plan_dir is None:
        return "纪律不允许", ["✗ 不在今天的观察名单里"]
    if want is not None and want != d:
        return "纪律不允许", [f"✗ 和名单方向（{plan_dir}）相反"]
    items = [("收在 VWAP " + ("上方" if d == 1 else "下方"), side_ok),
             ("和 C1 方向一致" if c1_dir else "C1 还没定（或今天不做 C1）", c1_dir == d),
             ("QQQ 同向（在开盘价和 VWAP " + ("上方）" if d == 1 else "下方）"), qqq_ok),
             ("在关键位附近" + (f"（{at_level}）" if at_level else ""), bool(at_level)),
             (f"止损放得下（约 ${stop_usd:.2f}，上限 ${float(s['止损_美元']):.2f}）" if stop_usd is not None else "止损距离算不出", stop_usd is not None and stop_usd <= float(s["止损_美元"]))]
    miss = sum(1 for _, ok in items if not ok)
    checks = [("✓ " if ok else "✗ ") + t for t, ok in items]
    return ("A" if miss == 0 else "B" if miss == 1 else "C"), checks


# ================================================================ 盘中
def _levels(code, b, plan_item, prev_hl, extra=()):
    lv = []
    for x in (plan_item or {}).get("关键价位") or []:
        try:
            lv.append((float(x["价格"]), x.get("说明") or "关键价位"))
        except Exception:
            pass
    if plan_item and plan_item.get("认错价"):
        lv.append((float(plan_item["认错价"]), "认错价"))
    if prev_hl:
        if prev_hl[0]:
            lv.append((float(prev_hl[0]), "昨高"))
        if len(prev_hl) > 1 and prev_hl[1]:
            lv.append((float(prev_hl[1]), "昨低"))
    if len(b):
        lv.append((float(b["open"].iloc[0]), "开盘价"))
    lv += list(extra)
    return lv


def live(code, b, atr, qb, plan_item, plan_dir, c1_dir, prev_hl, cfg=None):
    """b：今天到现在已收盘的 1 分钟K；qb：QQQ 同样的K。返回 dict(状态文字, events=[…])，events 只含最新一根K上新出现的。"""
    s = settings(cfg)
    res = dict(code=code, box=None, events=[], lines=[])
    if b is None or len(b) < 6 or not atr:
        return res
    tmin = b.index[-1].hour * 60 + b.index[-1].minute
    for frame in ("1分钟", "5分钟"):
        bb = b if frame == "1分钟" else to5(b)
        if bb is None or len(bb) < 3:
            continue
        if frame == "5分钟" and bb.index[-1] != b.index[-1]:
            continue                                   # 5 分钟K刚好收完那一分钟才看
        o, h, l, c, v = (bb[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        vw = vwap(h, l, c, v)
        i = len(c) - 1
        if qb is not None and len(qb):
            qq = qb if frame == "1分钟" else to5(qb)
            qv = vwap(*(qq[x].values.astype(float) for x in ("high", "low", "close", "volume")))
            qc, qo = float(qq["close"].iloc[-1]), float(qq["open"].iloc[0])
            q_dir = 1 if (qc > qv[-1] and qc > qo) else -1 if (qc < qv[-1] and qc < qo) else 0
        else:
            q_dir = 0
        found = [(n, CARDS[n][0], None) for n, arr in detect(o, h, l, c).items() if arr[i]]
        for (j, name, d, bh, bl, L) in range_events(h, l, c, atr, s):
            if j == i:
                found.append((name, d, (bh, bl, L)))
        if frame == "1分钟":
            cb = current_box(h, l, atr, s)
            if cb:
                hgt = cb[0] - cb[1]
                res["box"] = cb
                res["lines"].append(f"震荡中：{cb[2]} 根挤在 {cb[1]:.2f}–{cb[0]:.2f}（高度 ${hgt:.2f}，"
                                    + ("止损放得下" if hgt <= float(s["止损_美元"]) else "比止损宽，突破后也难放止损") + "）。区间里不做，等收盘站出去。")
        for name, d, box in found:
            near_lv = []
            tol = float(s["关键位距离_ATR"]) * atr
            if abs(c[i] - vw[i]) <= tol or (l[i] <= vw[i] <= h[i]):
                near_lv.append("VWAP")
            for px, what in _levels(code, bb, plan_item, prev_hl):
                if l[i] - tol <= px <= h[i] + tol:
                    near_lv.append(what)
            if box:
                near_lv.append("震荡边界")
                stop = abs(c[i] - (box[1] if d == 1 else box[0]))
            else:
                stop = abs(c[i] - (min(l[i], l[i - 1] if i else l[i]) if d == 1 else max(h[i], h[i - 1] if i else h[i])))
            ctxs = []
            side_ok = (c[i] - vw[i]) * d > 0
            if side_ok and q_dir == d:
                ctxs.append("顺VWAP+QQQ同向")
            if "VWAP" in near_lv:
                ctxs.append("贴着VWAP")
            ctxs.append("顺VWAP" if side_ok else "逆VWAP")
            ctxs.append("QQQ同向" if q_dir == d else "QQQ反向")
            g, checks = grade(d, plan_dir, side_ok, c1_dir, q_dir == d, "、".join(dict.fromkeys(near_lv)), stop, s, tmin)
            if res["box"] and not box and frame == "1分钟":
                g = "震荡中"
            res["events"].append(dict(frame=frame, name=name, dir=d, grade=g, checks=checks, price=float(c[i]),
                                      stop_usd=round(stop, 2), why=CARDS[name][1], prob=prob_text(frame, name, ctxs),
                                      box=box, t=f"{b.index[-1]:%H:%M}"))
    return res


def event_text(code, e):
    arrow = "看多" if e["dir"] == 1 else "看空"
    t = f"{e['t']} {code} {e['frame']}「{e['name']}」{arrow}　级别 {e['grade']}　价 {e['price']:.2f}"
    if e.get("box"):
        bh, bl, L = e["box"]
        t += f"　区间 {bl:.2f}–{bh:.2f}（{L} 根，高 ${bh - bl:.2f}）"
    return t


def write_hot(day, now, results, recent):
    """数据/热数据/形态.md：每只股票现在的震荡状态 + 今天出现过的形态（最近 30 条）。"""
    L = [f"# 形态监测（{day} {now:%H:%M} 美东）", "",
         "历史概率只是参考：大部分单根K和组合K和随便猜差不多，详见 报告/形态概率_*.md。级别按 VWAP 同侧、C1、QQQ、关键位、止损五条打分。", ""]
    for code, r in results.items():
        if r.get("lines"):
            L += [f"- **{code}**：" + "；".join(r["lines"])]
    L += ["", "## 今天出现的形态（新的在上）", ""]
    for code, e in list(reversed(recent))[:30]:
        L.append("- " + event_text(code, e))
        if e["grade"] == "纪律不允许":
            L[-1] += "（" + e["checks"][0].lstrip("✗ ") + "）"
            continue
        L.append(f"  - {e['why']}")
        if e.get("prob"):
            L.append(f"  - {e['prob']}")
        L.append("  - " + "　".join(e["checks"]))
    try:
        HOTMD.parent.mkdir(parents=True, exist_ok=True)
        HOTMD.write_text("\n".join(L) + "\n", encoding="utf-8")
    except Exception:
        pass


# ================================================================ 记录和收盘评估
SCHEMA = """CREATE TABLE IF NOT EXISTS pattern_live (
    date TEXT, t TEXT, code TEXT, frame TEXT, name TEXT, dir INTEGER, grade TEXT, price REAL, stop_usd REAL,
    notified INTEGER, f15 REAL, PRIMARY KEY (date, t, code, frame, name))"""


def record(day, code, e, notified):
    import store
    con = store.connect()
    try:
        con.execute(SCHEMA)
        with con:
            con.execute("INSERT OR IGNORE INTO pattern_live (date, t, code, frame, name, dir, grade, price, stop_usd, notified) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)", (str(day), e["t"], code, e["frame"], e["name"], e["dir"], e["grade"],
                                                           e["price"], e["stop_usd"], int(bool(notified))))
    finally:
        con.close()


def evaluate_day(day, bars_of, atr_of):
    """收盘后：每个记录下来的形态，往形态方向 15 分钟后走了多少倍 ATR（周报用来看提醒有没有用）。"""
    import store
    con = store.connect()
    try:
        con.execute(SCHEMA)
        rows = con.execute("SELECT t, code, frame, name, dir, price FROM pattern_live WHERE date = ? AND f15 IS NULL",
                           (str(day),)).fetchall()
    finally:
        con.close()
    cache = {}
    upd = []
    for t, code, frame, name, d, px in rows:
        if code not in cache:
            cache[code] = bars_of(code)
        b, a = cache[code], atr_of(code)
        if b is None or not len(b) or not a:
            continue
        ts = pd.Timestamp(f"{day} {t}") + pd.Timedelta(minutes=15)
        after = b[b.index <= ts]
        if not len(after) or after.index[-1] < ts - pd.Timedelta(minutes=1):
            continue
        upd.append((round(d * (float(after["close"].iloc[-1]) - px) / a, 4), str(day), t, code, frame, name))
    if upd:
        con = store.connect()
        try:
            with con:
                con.executemany("UPDATE pattern_live SET f15 = ? WHERE date = ? AND t = ? AND code = ? AND frame = ? AND name = ?", upd)
        finally:
            con.close()
    return len(upd)


def weekly_md(days=7):
    """最近一周盘中提醒的效果，按级别分（周报用）。"""
    import store
    con = store.connect()
    try:
        con.execute(SCHEMA)
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        df = pd.read_sql_query("SELECT * FROM pattern_live WHERE date >= ? AND f15 IS NOT NULL", con, params=[since])
    finally:
        con.close()
    if not len(df):
        return "形态监测：这周还没有可评估的记录。"
    L = ["形态监测（往形态方向 15 分钟后走了多少倍 ATR；样本少时只看个大概）："]
    for g, x in df.groupby("grade"):
        L.append(f"- {g} 级 {len(x)} 次：往方向 {(x.f15 > 0).mean() * 100:.0f}%，平均 {x.f15.mean():+.3f} ATR")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    if "--build" in sys.argv:
        cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        build_stats(cfg=cfg)
    else:
        print("用法：python 程序/patterns.py --build   （用历史 1 分钟K重算形态概率）")
