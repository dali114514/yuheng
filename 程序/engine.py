# -*- coding: utf-8 -*-
"""
盘感引擎（新规则引擎）：盘中「现在这只股票接下来更可能怎么走、该追 / 等 / 观察 / 不做」，给出概率和理由

  怎么算（全部换算成「倍日线 ATR」，ATR 用前一天的 14 日 ATR，不同价位的股票才能放在一起比）：
  ① 预先算好历史库里每只股票、每天、开盘后每 5 分钟的「走势特征」和「之后的真实结果」（缓存，收盘复盘后自动更新）
       特征：对开盘涨跌、离 VWAP、最近 15 / 5 分钟涨跌、跳空、今日振幅、在今日区间的位置、成交量比平时、QQQ 对开盘、QQQ 离 VWAP
       结果：从下一根开盘价进场，止损 0.2 倍 ATR、目标 2R（0.4 倍 ATR），做多 / 做空各算一遍真实 R
             （同一根先碰止损还是目标分不清时按止损算；到收盘还没碰到按收盘价平）
  ② 盘中问某只股票：算出它「现在」的同一组特征，在历史里同一时刻（开盘后第几分钟）找最像的情形
       只用今天之前的日子；每个历史日期只取最像的一只股票（同一天的股票走势高度相关，算一个样本才不会虚高）
  ③ 统计这些相似情形：做多 / 做空的赢率、平均 R（扣掉成本）、95% 区间，和「同一时刻所有日子」的基准对比
       样本少时把平均 R 往基准收缩（收缩后的数才拿来下结论），避免几天的运气被当成规律
  ④ 结合今天的纪律设置（名单、方向、认错价、最晚开仓、笔数和亏损额度）、Kill Zone、C2 进度，给结论：
       可以追多 / 可以追空 / 等回踩做多 / 等反弹做空 / 观察（偏多 / 偏空 / 没倾向）/ 不做 / 认错
  信息面（今天的新闻、推特、Reddit 热度）列出来给你和 Claude 判断，不进概率——历史新闻存档太短，没法验证它的作用。

  用法：
    python engine.py NVDA                         现在（读热数据）
    python engine.py NVDA --date 2026-11-10 --time 10:15   拿历史某天测试
    python engine.py --build                      重建缓存（收盘复盘后会自动做）
    python engine.py --validate                   走前验证：只用过去预测未来，看这套概率到底准不准
    python engine.py --kz NVDA AMD                各 Kill Zone 的历史统计
  只读行情和数据库，不下单。
"""
import argparse
import datetime as dt
import json
import math
import pickle
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
DB = HERE / "data" / "history.db"
CACHE = HERE / "data" / "engine_cache.pkl"
HOT = BASE / "数据" / "热数据"
VERSION = 3

DEFAULT = {
    "止损_ATR": 0.2,            # 衡量用的止损距离（倍日线 ATR）
    "目标_R": 2.0,
    "成本_R": 0.05,             # 每笔成本（约 0.01 倍 ATR ÷ 0.2）
    "相似日数": 40,             # 取最像的多少个历史日期
    "最少样本": 20,
    "收缩强度": 40,             # 平均 R 往基准收缩：收缩后 =（N×样本平均 + 强度×基准）÷（N + 强度）
    "追单最小优势_R": 0.10,     # 收缩后的平均 R 至少这么多、且 95% 区间下沿 > 0，才算「有优势」
    "偏向门槛_R": 0.05,         # 收缩后平均 R 超过这个、但不显著 → 观察（偏多 / 偏空）
    "离VWAP太远_ATR": 0.6,      # 顺方向离 VWAP 超过这个 → 等回踩 / 反弹，不追
    "说明": "盘感引擎参数。门槛是事先定的、没有按历史结果调过；改之前先跑「引擎体检」看验证结果。",
}
STEP = 5                                   # 每 5 分钟一个检查点
NS = list(range(5, 386, STEP))             # 开盘后第 5、10 … 385 分钟（15:55）
FEATS = ["ret", "vw", "r15", "r5", "gap", "rng", "pos", "rv", "q_ret", "q_vw"]
WEIGHTS = np.array([1.5, 1.0, 1.0, 0.7, 0.7, 0.7, 0.5, 0.5, 1.0, 0.7])
FEAT_CN = {"ret": "对开盘", "vw": "离 VWAP", "r15": "最近 15 分钟", "r5": "最近 5 分钟", "gap": "跳空", "rng": "今日振幅",
           "pos": "在今日区间的位置（0 = 最低，1 = 最高）", "rv": "成交量比平时（对数）", "q_ret": "QQQ 对开盘", "q_vw": "QQQ 离 VWAP"}


# ================================================================ 设置
def settings(cfg=None):
    s = dict(DEFAULT)
    if cfg is None:
        try:
            cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    s.update(cfg.get("盘感引擎") or {})
    return s


def zones(cfg=None):
    """Kill Zone（美东，分钟数区间 (a, b]，K 线结束时间在里面就算）。和盘感训练共用 设置.json → 盘感训练 → Kill_Zone。"""
    if cfg is None:
        try:
            cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    out = []
    for z in (cfg.get("盘感训练") or {}).get("Kill_Zone") or ["09:30-11:00", "13:30-16:00"]:
        try:
            a, b = [int(x[:2]) * 60 + int(x[3:5]) for x in str(z).replace("–", "-").split("-")]
            if 570 <= a < b <= 960:
                out.append((a, b))
        except Exception:
            pass
    return out or [(570, 660), (810, 960)]


def zone_name(z):
    return f"{z[0] // 60:02d}:{z[0] % 60:02d}–{z[1] // 60:02d}:{z[1] % 60:02d}"


def in_zone(minute, zs):
    return any(a < minute <= b for a, b in zs)


# ================================================================ 单日计算（缓存和盘中共用同一套代码，避免两边算法不一致）
def day_arrays(b):
    """b：一天常规时段 1 分钟K（按时间排好）。返回 numpy 数组 dict。"""
    o, h, l, c, v = (b[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
    tp = (h + l + c) / 3
    cv = np.cumsum(v)
    ctp = np.cumsum(tp * v)
    vw = np.where(cv > 0, ctp / np.maximum(cv, 1e-12), np.cumsum(tp) / np.arange(1, len(tp) + 1))
    return dict(o=o, h=h, l=l, c=c, v=v, cv=cv, vw=vw, hi=np.maximum.accumulate(h), lo=np.minimum.accumulate(l))


def feats_at(d, n, A, pc):
    """用前 n 根算特征（只用已经收盘的K线）。"""
    i = n - 1
    cn = d["c"][i]
    o0 = d["o"][0]
    rng = d["hi"][i] - d["lo"][i]
    back = lambda k: d["c"][i - k] if i - k >= 0 else o0
    return dict(ret=(cn - o0) / A, vw=(cn - d["vw"][i]) / A, r15=(cn - back(15)) / A, r5=(cn - back(5)) / A,
                gap=((o0 - pc) / A) if pc and pc == pc else 0.0, rng=rng / A,
                pos=((cn - d["lo"][i]) / rng) if rng > 0 else 0.5, cumv=d["cv"][i], price=cn, vwap=d["vw"][i])


def trade_R(d, n, A, s, side):
    """从第 n+1 根开盘价进场（side = 1 做多 / -1 做空），止损 s 倍 ATR、目标 目标_R 倍止损。返回 (R, 结果)。"""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    if n >= len(o):
        return None, None
    e = o[n]
    risk = s["止损_ATR"] * A
    stop = e - side * risk
    tgt = e + side * s["目标_R"] * risk
    hs, ls = h[n:], l[n:]
    hit_stop = (ls <= stop) if side > 0 else (hs >= stop)
    hit_tgt = (hs >= tgt) if side > 0 else (ls <= tgt)
    js = int(np.argmax(hit_stop)) if hit_stop.any() else None
    jt = int(np.argmax(hit_tgt)) if hit_tgt.any() else None
    if js is not None and (jt is None or js <= jt):                  # 同一根两个都碰到：按止损算（保守）
        oj = o[n + js]
        gapped = (oj <= stop) if side > 0 else (oj >= stop)
        return (side * (oj - e) / risk if gapped else -1.0), "止损"
    if jt is not None:
        return s["目标_R"], "目标"
    return side * (c[-1] - e) / risk, "到收盘"


def day_rows(code, day, b, A, pc, s, zs):
    """一天的全部检查点：特征 + 结果。b 必须是完整的一天（≥ 385 根）。"""
    d = day_arrays(b)
    L = len(d["c"])
    rows = []
    for n in NS:
        if n >= L:
            break
        f = feats_at(d, n, A, pc)
        cn = f["price"]
        rl, hl = trade_R(d, n, A, s, 1)
        rs, hs_ = trade_R(d, n, A, s, -1)
        rows.append(dict(code=code, date=day, n=n, **{k: f[k] for k in ("ret", "vw", "r15", "r5", "gap", "rng", "pos", "cumv")},
                         R_long=rl, R_short=rs, win_long=hl == "目标", win_short=hs_ == "目标",
                         to_close=(d["c"][-1] - cn) / A, up=(d["h"][n:].max() - cn) / A, down=(cn - d["l"][n:].min()) / A,
                         m30=(d["c"][min(n - 1 + 30, L - 1)] - cn) / A, m60=(d["c"][min(n - 1 + 60, L - 1)] - cn) / A,
                         kz=in_zone(570 + n, zs)))
    # 当天的 Kill Zone 统计
    t = 570 + np.arange(1, L + 1)
    hod, lod = int(np.argmax(d["h"])), int(np.argmin(d["l"]))
    ds = dict(code=code, date=day, atr=A, open=d["o"][0], close=d["c"][-1], c6=d["c"][min(5, L - 1)],
              hod_t=int(t[hod]), lod_t=int(t[lod]), day_rng=(d["h"].max() - d["l"].min()) / A,
              gap=((d["o"][0] - pc) / A) if pc and pc == pc else np.nan)
    for k, (a, bnd) in enumerate(zs):
        m = (t > a) & (t <= bnd)
        if not m.any():
            continue
        idx = np.where(m)[0]
        start_px = d["o"][0] if a == 570 else d["c"][idx[0] - 1]
        ds[f"z{k}_move"] = (d["c"][idx[-1]] - start_px) / A
        ds[f"z{k}_rng"] = (d["h"][idx].max() - d["l"][idx].min()) / A
        ds[f"z{k}_end"] = d["c"][idx[-1]]
        ds[f"z{k}_start"] = start_px
    return rows, ds


# ================================================================ 缓存
def _con():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)


def db_last_day():
    con = _con()
    try:
        r = con.execute("SELECT MAX(ts) FROM bars_1m").fetchone()
    finally:
        con.close()
    return str(r[0])[:10] if r and r[0] else None


def daily_ref():
    """(code, 日期) → (前一天 ATR14, 前一天收盘)。ATR 只用这一天之前的 14 根日线。"""
    con = _con()
    try:
        d1 = pd.read_sql_query("SELECT code, ts, high, low, close FROM bars_1d ORDER BY code, ts", con)
    finally:
        con.close()
    d1["day"] = pd.to_datetime(d1["ts"]).dt.normalize()
    out = {}
    for code, g in d1.groupby("code"):
        pc = g["close"].shift()
        tr = pd.concat([g["high"] - g["low"], (g["high"] - pc).abs(), (g["low"] - pc).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().shift()
        for t, a, p in zip(g["day"], atr, pc):
            out[(code, t)] = (a, p)
    return out


def build_cache(log=print, s=None):
    s = s or settings()
    zs = zones()
    con = _con()
    try:
        df = pd.read_sql_query("SELECT code, ts, open, high, low, close, volume FROM bars_1m ORDER BY code, ts", con)
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts"])
    m = df.ts.dt.hour * 60 + df.ts.dt.minute
    df = df[(m > 570) & (m <= 960)]
    df["day"] = df.ts.dt.normalize()
    ref = daily_ref()
    rows, days, skipped = [], [], 0
    for (code, day), b in df.groupby(["code", "day"], sort=True):
        if len(b) < 385 or b["ts"].iloc[0].hour * 60 + b["ts"].iloc[0].minute != 571:
            skipped += 1                                  # 不完整的一天（半天市、缺数据）不要
            continue
        A, pc = ref.get((code, day), (None, None))
        if not A or A != A or A <= 0:
            skipped += 1
            continue
        r, ds = day_rows(code, day, b.set_index("ts"), A, pc, s, zs)
        rows += r
        days.append(ds)
    R = pd.DataFrame(rows)
    # 成交量比平时：同一只股票、同一时刻，过去 20 天累计成交量的中位数（只用之前的日子）
    R = R.sort_values(["code", "n", "date"])
    med = R.groupby(["code", "n"])["cumv"].transform(lambda x: x.rolling(20, min_periods=10).median().shift(1))
    R["rv"] = np.log(np.where((R["cumv"] > 0) & (med > 0), R["cumv"] / med, np.nan))
    # QQQ 同一天同一时刻
    q = R[R.code == "QQQ"][["date", "n", "ret", "vw"]].rename(columns={"ret": "q_ret", "vw": "q_vw"})
    R = R.merge(q, on=["date", "n"], how="left")
    R = R.sort_values(["date", "n", "code"]).reset_index(drop=True)
    for c in FEATS + ["R_long", "R_short", "to_close", "up", "down", "m30", "m60"]:
        R[c] = R[c].astype("float32")
    D = pd.DataFrame(days)
    obj = dict(version=VERSION, rows=R, days=D, zones=zs, last_day=db_last_day(), built=dt.datetime.now().isoformat(timespec="seconds"),
               params={k: s[k] for k in ("止损_ATR", "目标_R")})
    tmp = CACHE.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(CACHE)
    msg = (f"盘感引擎缓存：{R['code'].nunique()} 只股票，{D['date'].nunique()} 个交易日，{len(R):,} 个检查点"
           f"（跳过不完整的 {skipped} 天）")
    if log:
        log(msg)
    return msg


_MEM = {}


def load_cache(auto=True, log=None):
    """读缓存；历史库有新数据、参数或 Kill Zone 改过时自动重建。"""
    s = settings()
    stale = True
    if CACHE.exists():
        try:
            mt = CACHE.stat().st_mtime
            if _MEM.get("mt") == mt:
                obj = _MEM["obj"]
            else:
                with open(CACHE, "rb") as f:
                    obj = pickle.load(f)
                _MEM.update(mt=mt, obj=obj)
            stale = (obj.get("version") != VERSION or obj.get("zones") != zones()
                     or obj.get("params") != {k: s[k] for k in ("止损_ATR", "目标_R")}
                     or (db_last_day() or "") > (obj.get("last_day") or ""))
        except Exception:
            stale = True
    if stale:
        if not auto:
            return None
        build_cache(log=log)
        with open(CACHE, "rb") as f:
            obj = pickle.load(f)
        _MEM.update(mt=CACHE.stat().st_mtime, obj=obj)
    return _MEM["obj"]


# ================================================================ 统计工具
def wilson(k, n, z=1.96):
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (mid - half, mid + half)


def mean_ci(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 2:
        return (float("nan"),) * 3
    from scipy.stats import t
    m, sd = x.mean(), x.std(ddof=1)
    h = t.ppf(0.975, n - 1) * sd / math.sqrt(n)
    return m, m - h, m + h


# ================================================================ 找相似
def analogs(R, q, n, before, s, k=None, exclude_code_day=None):
    """在 R（缓存行）里找和 q（特征 dict）最像的情形：同一检查点 n、日期 < before、每个日期只取最像的一只。
    返回 (相似样本 DataFrame, 基准 DataFrame)。"""
    k = k or int(s["相似日数"])
    P = R[(R["n"] == n) & (R["date"] < before)]
    P = P.dropna(subset=["ret", "vw", "r15", "r5", "rng", "pos", "R_long", "R_short"])
    if not len(P):
        return P, P
    X = P[FEATS].to_numpy(dtype="float64")
    sd = np.nanstd(X, axis=0)
    sd[~(sd > 1e-9)] = 1.0
    qv = np.array([q.get(f, np.nan) for f in FEATS], dtype="float64")
    diff = (X - qv) / sd
    diff[np.isnan(diff)] = 0.0                          # 缺的特征（例如没有 QQQ、成交量）不参与比较
    dist = np.sqrt((diff ** 2 * WEIGHTS).sum(axis=1) / WEIGHTS.sum())
    P = P.assign(dist=dist)
    best = P.sort_values("dist").drop_duplicates("date", keep="first")
    return best.head(k), P


def stats(top, base, s):
    """相似样本 vs 基准：做多 / 做空 赢率、平均 R（扣成本）、95% 区间、收缩后的平均 R。"""
    cost, m = float(s["成本_R"]), float(s["收缩强度"])
    out = dict(N=len(top), N_base=len(base))
    for side in ("long", "short"):
        r = top[f"R_{side}"].to_numpy(float) - cost
        rb = base[f"R_{side}"].to_numpy(float) - cost
        mu, lo, hi = mean_ci(r)
        mb = float(np.nanmean(rb)) if len(rb) else float("nan")
        N = len(r)
        shrunk = (N * mu + m * mb) / (N + m) if N else mb
        w = int(top[f"win_{side}"].sum())
        out[side] = dict(mean=mu, lo=lo, hi=hi, base=mb, shrunk=shrunk, win=w / N if N else float("nan"),
                         win_ci=wilson(w, N), win_base=float(base[f"win_{side}"].mean()) if len(base) else float("nan"))
    up = int((top["to_close"] > 0).sum())
    out["close_up"] = (up / len(top) if len(top) else float("nan"), wilson(up, len(top)),
                       float((base["to_close"] > 0).mean()) if len(base) else float("nan"))
    for c in ("to_close", "m30", "m60"):
        out[c] = (float(top[c].median()), float(base[c].median()))
    out["up"], out["down"] = float(top["up"].mean()), float(top["down"].mean())
    return out


def lean(st, s):
    """纯统计的方向：('多' / '空' / None, 是否显著)。"""
    L, S = st["long"], st["short"]
    edge = float(s["追单最小优势_R"])
    cands = []
    if L["shrunk"] >= edge and L["lo"] > 0:
        cands.append(("多", L["shrunk"], True))
    if S["shrunk"] >= edge and S["lo"] > 0:
        cands.append(("空", S["shrunk"], True))
    if cands:
        return max(cands, key=lambda x: x[1])[0], True
    lv = float(s["偏向门槛_R"])
    if max(L["shrunk"], S["shrunk"]) >= lv:
        return ("多" if L["shrunk"] >= S["shrunk"] else "空"), False
    return None, False


# ================================================================ 今天的数据
def _hm(x):
    h, m = str(x).split(":")
    return int(h) * 60 + int(m)


def _ref_for(code, day):
    import c2
    return c2.daily_atr(code, str(day)), c2.prev_close(code, str(day))


def load_bars(code, date=None, time_=None):
    """返回 (当天到目前的 1 分钟K DataFrame（index = 结束时间）, 日期) 或 (None, 说明)。"""
    if date:
        con = _con()
        try:
            b = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE code = ? AND ts > ? AND ts <= ? "
                                  "ORDER BY ts", con, params=[code, f"{date} 09:30:00", f"{date} 16:00:00"])
        finally:
            con.close()
        if not len(b):
            return None, f"历史库里没有 {code} {date} 的 1 分钟K"
        b.index = pd.to_datetime(b.pop("ts"))
        day = pd.Timestamp(date)
    else:
        p = HOT / "1分钟" / f"{code}.csv"
        if not p.exists():
            return None, f"没有 {code} 的热数据（盘中热数据程序只写观察名单 + QQQ、SPY；不在名单里的股票盘中读不到）"
        b = pd.read_csv(p, parse_dates=["time_et"]).set_index("time_et")[["open", "high", "low", "close", "volume"]]
        if not len(b):
            return None, f"{code} 今天还没有 K 线"
        day = b.index[0].normalize()
    if time_:
        b = b[b.index <= day + pd.Timedelta(minutes=_hm(time_))]
    return b, day


def features_now(code, b, day, R, date=None):
    """今天到目前的特征（和缓存用同一个函数算）。返回 (特征 dict, 检查点 n, 说明)。"""
    A, pc = _ref_for(code, day)
    if not A:
        return None, None, f"{code} 缺少日线 ATR（日线不够 15 天）"
    t_last = b.index[-1]
    n_live = (t_last.hour * 60 + t_last.minute) - 570
    if n_live < 5:
        return None, None, f"开盘才 {n_live} 分钟，至少等到 9:35"
    d = day_arrays(b)
    f = feats_at(d, len(b), A, pc)
    f.update(atr=A, n_live=n_live, t=t_last)
    n = int(min(NS, key=lambda x: abs(x - n_live)))
    # 成交量比平时：用缓存里这只股票同一时刻、之前 20 天的中位数
    hist = R[(R["code"] == code) & (R["n"] == n) & (R["date"] < day)].sort_values("date").tail(20)
    med = float(hist["cumv"].median()) if len(hist) >= 10 else float("nan")
    scale = n / n_live if n_live else 1.0
    f["rv"] = math.log(f["cumv"] * scale / med) if med and med == med and f["cumv"] > 0 else float("nan")
    # QQQ
    if code == "QQQ":
        f["q_ret"], f["q_vw"] = f["ret"], f["vw"]
    else:
        qb, _ = load_bars("QQQ", date, f"{t_last:%H:%M}")
        if qb is not None and len(qb) >= 5:
            qa, qpc = _ref_for("QQQ", day)
            if qa:
                qf = feats_at(day_arrays(qb), len(qb), qa, qpc)
                f["q_ret"], f["q_vw"] = qf["ret"], qf["vw"]
                f["q_price"], f["q_vwap"] = qf["price"], qf["vwap"]
    return f, n, ""


# ================================================================ 今天的计划和信息面
def context(code, day, cfg, until=None):
    import today
    out = dict(plan=None, w=None, guard={}, c2={}, news=[], heat=None, tw=None)
    try:
        p = today.load_plan(day.date() if hasattr(day, "date") else day)
        out["plan"] = p
        if p:
            out["w"] = next((w for w in p["观察名单"] if w["代码"] == code), None)
    except Exception:
        pass
    try:
        g = json.loads((HERE / "data" / ".guard_status.json").read_text(encoding="utf-8"))
        if g.get("day") == str(day.date() if hasattr(day, "date") else day):
            out["guard"] = g
    except Exception:
        pass
    try:
        c = json.loads((HERE / "data" / ".c2_status.json").read_text(encoding="utf-8"))
        if c.get("date") == str(day.date() if hasattr(day, "date") else day):
            out["c2"] = (c.get("codes") or {}).get(code) or {}
    except Exception:
        pass
    out["news"] = stock_news(code, day, until=until)
    try:
        import brief
        items = brief.info_items(24)
        out["heat"] = brief.reddit_heat(items).get(code)
        _, sides = brief.grok_view()
        out["tw"] = sides.get(code)
    except Exception:
        pass
    return out


def stock_news(code, day, max_n=4, until=None):
    """按股票/代码.md 里当天和前一天的条目标题。"""
    p = BASE / "数据" / "冷数据" / "信息面" / "按股票" / f"{code}.md"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    d0 = pd.Timestamp(day).date()
    want = {str(d0), str(d0 - dt.timedelta(days=1))}
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
        elif cur in want and line.startswith("- **"):
            t = line[4:9]
            if until and cur == str(d0) and t > until:          # 拿历史某天测试时，不列当时还没出现的新闻
                continue
            title = line.split("]", 1)[-1].split("　[原文]")[0].strip() if "]" in line else line
            if title and title not in [x[2] for x in out]:
                out.append((cur, t, title[:90]))
    return out[:max_n]


# ================================================================ 结论
def decide(code, f, st, ctx, s, cfg, now_min):
    """返回 (结论, 理由列表, 价位 dict)。"""
    zs = zones(cfg)
    A, px = f["atr"], f["price"]
    risk = float(s["止损_ATR"]) * A
    dir_, sig = lean(st, s)
    why = []
    plan, w, g = ctx["plan"], ctx["w"], ctx["guard"]
    lim = (plan or {}).get("限制") or {}
    lv = {}
    if dir_:
        side = 1 if dir_ == "多" else -1
        lv = dict(entry=px, stop=px - side * risk, target=px + side * float(s["目标_R"]) * risk)
    if st["N"] < int(s["最少样本"]):
        return "数据不足", [f"相似情形只有 {st['N']} 个（至少要 {s['最少样本']} 个），不下结论"], lv
    # 认错价
    if w and w.get("认错价"):
        wd = w.get("方向")
        if (wd == "多" and px <= w["认错价"]) or (wd == "空" and px >= w["认错价"]):
            return "认错", [f"现价 {px:.2f} 已经过了今天定的认错价 {w['认错价']}（计划方向 {wd}）：手上有仓位按纪律离场，今天这只的判断作废"], lv
    blocks = []
    if not in_zone(570 + f["n_live"], zs):
        blocks.append(f"现在不在 Kill Zone（{'、'.join(zone_name(z) for z in zs)}）")
    if lim.get("最晚开仓") and now_min > _hm(lim["最晚开仓"]):
        blocks.append(f"已过今天的最晚开仓时间 {lim['最晚开仓']}")
    if g:
        if g.get("max") and g.get("opens") is not None and int(g["opens"]) >= int(g["max"]):
            blocks.append(f"今天已经开了 {g['opens']} 笔，到了上限 {g['max']} 笔")
        if g.get("max_loss") and g.get("pnl") is not None and g["pnl"] <= -float(g["max_loss"]):
            blocks.append(f"今日亏损 ${-g['pnl']:,.0f} 已到上限 ${float(g['max_loss']):,.0f}")
    if plan:
        if w is None and lim.get("只做名单内"):
            blocks.append(f"{code} 不在今天的观察名单里")
        elif w and dir_ and lim.get("只做名单方向") and w.get("方向") in ("多", "空") and w["方向"] != dir_:
            blocks.append(f"统计偏{dir_}，但今天计划只做{w['方向']}——方向冲突本身就是提醒：回头看计划的前提还在不在")
    else:
        why.append("今天还没有定纪律设置，下面只是统计结论")
    if blocks:
        return "不做", blocks + [f"（统计上{'偏' + dir_ if dir_ else '没有倾向'}{'，而且显著' if sig else ''}，只供参考）"], lv
    if dir_ and sig:
        side = 1 if dir_ == "多" else -1
        if side * f["vw"] > float(s["离VWAP太远_ATR"]):
            return ("等回踩做多" if dir_ == "多" else "等反弹做空"), why + [
                f"统计上{dir_}方有优势，但现价离 VWAP 已经 {abs(f['vw']):.2f} 倍 ATR（超过 {s['离VWAP太远_ATR']}），"
                f"现在追进去的位置差；等回到 VWAP（{f['vwap']:.2f}）附近出现确认再进"], lv
        return ("可以追多" if dir_ == "多" else "可以追空"), why, lv
    if dir_:
        return f"观察（偏{dir_}）", why + [f"统计上略偏{dir_}，但和平常比差别不显著（95% 区间包含 0），不够成为进场理由"], lv
    return "观察（没倾向）", why + ["相似情形里做多做空都没有优势，现在进场等于掷硬币"], lv


# ================================================================ 盘感判断（主入口）
def judge(code, date=None, time_=None, cfg=None, log_call=True, out_dir=None):
    code = code.upper().strip()
    if cfg is None:
        try:
            cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    s = settings(cfg)
    obj = load_cache(log=None)
    R = obj["rows"]
    b, day = load_bars(code, date, time_)
    if b is None:
        return day, None
    f, n, err = features_now(code, b, day, R, date)
    if f is None:
        return err, None
    top, base = analogs(R, f, n, day, s)
    if not len(top):
        return f"历史里没有 {code} 这个时刻可比的样本", None
    st = stats(top, base, s)
    ctx = context(code, day, cfg, until=f"{f['t']:%H:%M}")
    now_min = f["t"].hour * 60 + f["t"].minute
    verdict, why, lv = decide(code, f, st, ctx, s, cfg, now_min)
    text = render(code, f, n, st, top, ctx, verdict, why, lv, s, cfg)
    res = dict(code=code, day=str(day.date()), t=f"{f['t']:%H:%M}", n=n, verdict=verdict, price=f["price"], atr=f["atr"],
               N=st["N"], long=st["long"], short=st["short"], lean=lean(st, s))
    if log_call and not date:
        log_judgement(res)
    png = None
    try:
        png = plot_analogs(code, b, f, top, st, verdict, out_dir=out_dir or HOT)
        text += f"\n![相似走势]({png.name})\n\n图：`数据/热数据/{png.name}`（上：今天的 5 分钟K和 VWAP；下：最像的 10 天叠在一起，灰线是「现在」之后的走法）\n"
        res["png"] = str(png)
    except Exception as e:
        text += f"\n（相似走势图没画出来：{e}）\n"
    try:
        od = Path(out_dir or HOT)
        od.mkdir(parents=True, exist_ok=True)
        (od / f"盘感_{code}_{f['t']:%H%M}.md").write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text, res


def plot_analogs(code, b, f, top, st, verdict, out_dir=None, k=10):
    """画图：上 = 今天到现在的 5 分钟K + VWAP；下 = 今天和最像的 k 天，都按（价格 - 开盘价）÷ 日线 ATR 画在同一张图上。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import c2
    import history
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti TC", "Arial Unicode MS", "STHeiti", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    A = f["atr"]
    t_now = f["t"]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8.5), gridspec_kw=dict(height_ratios=[1, 1.25]))
    # 上：今天 5 分钟K
    bb = c2.session(b)
    t = bb.index.hour * 60 + bb.index.minute
    g = bb.groupby((t - 571) // 5).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    hh, ll, cc, vv = (bb[x].values.astype(float) for x in ("high", "low", "close", "volume"))
    vw = np.cumsum((hh + ll + cc) / 3 * vv) / np.maximum(np.cumsum(vv), 1e-9)
    for i, (_, r) in enumerate(g.iterrows()):
        up = r.close >= r.open
        col = "#2a9d55" if up else "#d1453b"
        ax1.vlines(i, r.low, r.high, color=col, lw=1)
        ax1.add_patch(plt.Rectangle((i - 0.35, min(r.open, r.close)), 0.7, max(abs(r.close - r.open), 1e-6), color=col))
    xv = (np.arange(len(vw)) + 1) / 5 - 1
    ax1.plot(xv, vw, color="#7a5af5", lw=1.2, label="VWAP")
    ax1.axhline(float(bb["open"].iloc[0]), color="#999", lw=0.8, ls=":", label="开盘价")
    ax1.set_xlim(-1, 78)
    ax1.set_xticks(range(0, 79, 6))
    ax1.set_xticklabels([f"{(570 + 5 * (x + 1)) // 60}:{(570 + 5 * (x + 1)) % 60:02d}" for x in range(0, 79, 6)], fontsize=8)
    ax1.set_title(f"{code} 今天 · 5 分钟K（到 {t_now:%H:%M}）　现价 {f['price']:.2f}　VWAP {f['vwap']:.2f}　日线 ATR {A:.2f}", fontsize=11)
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(alpha=0.2)
    # 下：相似日叠图
    n_now = len(bb)
    o0 = float(bb["open"].iloc[0])
    paths = []
    for _, r in top.head(k).iterrows():
        d = pd.Timestamp(r["date"])
        try:
            m = c2.session(history.load(r["code"], "1m", start=f"{d:%Y-%m-%d} 09:30:00", end=f"{d:%Y-%m-%d} 16:00:00"))
            a = c2.daily_atr(r["code"], f"{d:%Y-%m-%d}")
        except Exception:
            continue
        if not len(m) or not a:
            continue
        y = (m["close"].values.astype(float) - float(m["open"].iloc[0])) / a
        x = np.arange(1, len(y) + 1)
        ax2.plot(x[:n_now], y[:n_now], color="#9aa5b1", lw=0.8, alpha=0.8)
        ax2.plot(x[n_now - 1:], y[n_now - 1:], color="#c4c9cf", lw=0.8, alpha=0.9)
        ax2.annotate(f"{r['code']} {d:%m-%d}", (x[-1], y[-1]), fontsize=6.5, color="#6b7480")
        paths.append(np.pad(y, (0, max(0, 390 - len(y))), constant_values=np.nan)[:390])
    if paths:
        P = np.vstack(paths)
        med = np.nanmedian(P, axis=0)
        xs = np.arange(1, 391)
        ax2.plot(xs[n_now - 1:], med[n_now - 1:], color="#e08a1e", lw=2, ls="--", label=f"相似日的中位数（之后）")
    y_now = (cc - o0) / A
    ax2.plot(np.arange(1, len(y_now) + 1), y_now, color="#1f6feb", lw=2.2, label=f"今天 {code}")
    ax2.axvline(n_now, color="#1f6feb", lw=0.8, ls=":")
    ax2.axhline(0, color="#999", lw=0.6)
    ax2.set_xlim(0, 391)
    ticks = list(range(0, 391, 30))
    ax2.set_xticks(ticks)
    ax2.set_xticklabels([f"{(570 + x) // 60}:{(570 + x) % 60:02d}" for x in ticks], fontsize=8)
    ax2.set_ylabel("（价格 - 开盘价）÷ 日线 ATR")
    L, S = st["long"], st["short"]
    cu = st["close_up"]
    ax2.set_title(f"最像的 {min(k, len(top))} 天（共比了 {st['N']} 个）：做多平均 {L['mean']:+.2f}R（基准 {L['base']:+.2f}），"
                  f"做空 {S['mean']:+.2f}R（基准 {S['base']:+.2f}），到收盘上涨 {cu[0] * 100:.0f}%（基准 {cu[2] * 100:.0f}%）",
                  fontsize=10)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(alpha=0.2)
    fig.text(0.5, 0.005, f"引擎结论：{verdict}。2026-11-17 走前验证：相似匹配对后续走势没有预测力，图只说明「历史上这种走势通常怎么样」，不是信号。",
             ha="center", fontsize=8.5, color="#8a3b12")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    out_dir = Path(out_dir or HOT)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"盘感_{code}_{t_now:%H%M}.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    return p


def _p(x):
    return "—" if x is None or x != x else f"{x * 100:.0f}%"


def _r(x):
    return "—" if x is None or x != x else f"{x:+.2f}R"


def render(code, f, n, st, top, ctx, verdict, why, lv, s, cfg):
    A = f["atr"]
    L, S = st["long"], st["short"]
    risk = float(s["止损_ATR"])
    cu = st["close_up"]
    lines = [f"# 盘感判断 · {code} · {f['t']:%Y-%m-%d %H:%M}（美东）", "",
             f"## 结论：{verdict}", ""]
    lines += [f"- {x}" for x in why] or []
    if lv and verdict.startswith(("可以追", "等回")):
        d = "多" if "多" in verdict else "空"
        lines += [f"- 衡量用的价位：现价约 {lv['entry']:.2f}，止损 {lv['stop']:.2f}（{risk} 倍 ATR），"
                  f"目标 {lv['target']:.2f}（{s['目标_R']}R）。这是统计用的尺子，实际按你的打法（C2 极值外止损）来"]
        if verdict.startswith("等回"):
            lines.append(f"- 等的位置：VWAP {f['vwap']:.2f} 附近出现确认组合（收盘越过前一根高点 / 吞没）再进；"
                         f"回踩时如果{'跌破' if d == '多' else '站上'} VWAP 后收不回来，这次就算了")
    # 现在
    lines += ["", "## 现在是什么情况", "",
              f"- 现价 {f['price']:.2f}，VWAP {f['vwap']:.2f}，日线 ATR {A:.2f}（前一天的 14 日 ATR）",
              f"- 对开盘 {f['ret']:+.2f} 倍 ATR，离 VWAP {f['vw']:+.2f}，最近 15 分钟 {f['r15']:+.2f}、5 分钟 {f['r5']:+.2f}，"
              f"跳空 {f['gap']:+.2f}，今日振幅 {f['rng']:.2f}，在今日区间的 {f['pos'] * 100:.0f}% 位置",
              f"- 成交量：" + (f"约是平时同一时刻的 {math.exp(f['rv']):.1f} 倍" if f.get("rv") == f.get("rv") and f.get("rv") is not None else "没有足够历史比较"),
              ("- QQQ：对开盘 {:+.2f} 倍 ATR，离 VWAP {:+.2f}".format(f["q_ret"], f["q_vw"]) if f.get("q_ret") is not None
               and f.get("q_ret") == f.get("q_ret") else "- QQQ：读不到（不参与比较）")]
    c2 = ctx.get("c2") or {}
    if c2:
        lines.append(f"- 早盘 C2 监测：{c2.get('phase', '')}{(' · ' + c2['dir']) if c2.get('dir') else ''}　{c2.get('reason', '')}")
    zs = zones(cfg)
    lines.append(f"- Kill Zone：{'在里面' if in_zone(570 + f['n_live'], zs) else '不在'}（{'、'.join(zone_name(z) for z in zs)}）")
    # 统计
    lines += ["", f"## 历史上最像的 {st['N']} 个情形后来怎么走", "",
              f"同一时刻（开盘后约 {n} 分钟）、只用 {f['t']:%Y-%m-%d} 之前的日子；每个日期只取最像的一只股票。"
              f"基准 = 同一时刻所有 {st['N_base']:,} 个样本。", "",
              "| 从下一根开盘进场（止损 {0} 倍 ATR，目标 {1}R，扣成本 {2}R） | 相似情形 | 95% 区间 | 基准 | 收缩后 |".format(
                  risk, s["目标_R"], s["成本_R"]),
              "| --- | --- | --- | --- | --- |",
              f"| 做多：赢率（先到目标） | {_p(L['win'])} | {_p(L['win_ci'][0])}–{_p(L['win_ci'][1])} | {_p(L['win_base'])} | |",
              f"| 做多：平均每笔 | {_r(L['mean'])} | {_r(L['lo'])} ~ {_r(L['hi'])} | {_r(L['base'])} | **{_r(L['shrunk'])}** |",
              f"| 做空：赢率（先到目标） | {_p(S['win'])} | {_p(S['win_ci'][0])}–{_p(S['win_ci'][1])} | {_p(S['win_base'])} | |",
              f"| 做空：平均每笔 | {_r(S['mean'])} | {_r(S['lo'])} ~ {_r(S['hi'])} | {_r(S['base'])} | **{_r(S['shrunk'])}** |",
              f"| 到收盘上涨的比例 | {_p(cu[0])} | {_p(cu[1][0])}–{_p(cu[1][1])} | {_p(cu[2])} | |",
              f"| 到收盘涨跌（中位数，倍 ATR） | {st['to_close'][0]:+.2f} | | {st['to_close'][1]:+.2f} | |",
              f"| 30 / 60 分钟后（中位数，倍 ATR） | {st['m30'][0]:+.2f} / {st['m60'][0]:+.2f} | | {st['m30'][1]:+.2f} / {st['m60'][1]:+.2f} | |",
              f"| 之后最多往上 / 往下（平均，倍 ATR） | {st['up']:.2f} / {st['down']:.2f} | | | |", "",
              f"怎么读：赢率 = 先碰到目标的比例；有一部分单子到收盘都没碰到止损或目标、按收盘价平，所以看「平均每笔」最准。"
              f"「收缩后」= 把相似情形的平均 R 往基准拉（样本越少拉得越多），结论只看它和 95% 区间："
              f"收缩后 ≥ {s['追单最小优势_R']}R 而且区间下沿 > 0 才算有优势。"]
    # 信息面
    lines += ["", "## 信息面（列出来给你判断，不进概率）", ""]
    news = ctx.get("news") or []
    lines += [f"- {d[5:]} {t} {title}" for d, t, title in news] or ["- 今天和昨天没有这只股票的新闻"]
    tw, heat = ctx.get("tw"), ctx.get("heat")
    if tw:
        lines.append(f"- 推特（今天 Grok 雷达）：看涨 {tw[0]}、看跌 {tw[1]}")
    if heat:
        lines.append(f"- Reddit：提及 {heat[0]}，是 24 小时前的 {heat[1]:.1f} 倍")
    w = ctx.get("w")
    if ctx.get("plan"):
        p = ctx["plan"]
        lines.append(f"- 今天的计划（{p.get('状态')}）：{p.get('偏向')}，{p.get('一句话') or ''}"
                     + (f"；{code} 方向 {w.get('方向')}，认错价 {w.get('认错价')}" if w else f"；{code} 不在名单里"))
    # 什么情况说明错了
    lines += ["", "## 哪种情况说明判断错了", ""]
    if verdict.startswith(("可以追", "等回")):
        d = "多" if "多" in verdict else "空"
        lines.append(f"- 价格碰到 {lv['stop']:.2f}（衡量用止损）就是错了，不要加仓摊平")
        lines.append(f"- {'跌回 VWAP 下方' if d == '多' else '站回 VWAP 上方'}并收稳 2–3 根：动能没了")
        if f.get("q_vw") is not None and f.get("q_vw") == f.get("q_vw"):
            lines.append(f"- QQQ {'跌破' if d == '多' else '站上'}自己的 VWAP：大盘不配合，相似情形的前提变了")
    elif verdict.startswith("观察"):
        lines.append("- 不用认错（没进场）。过 5–10 分钟再问一次；走出 C2 回踩确认，或者离 VWAP 的位置明显变化时，结论可能会变")
    elif verdict == "不做":
        lines.append("- 上面挡住的条件不变，就一直不做；不要为了这个统计结论去改纪律")
    # 相似明细
    lines += ["", "## 最像的 10 个情形", "", "| 股票 | 日期 | 距离 | 当时对开盘 | 离 VWAP | 做多 R | 做空 R | 到收盘 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for _, r in top.head(10).iterrows():
        lines.append(f"| {r['code']} | {pd.Timestamp(r['date']):%Y-%m-%d} | {r['dist']:.2f} | {r['ret']:+.2f} | {r['vw']:+.2f} | "
                     f"{r['R_long']:+.2f} | {r['R_short']:+.2f} | {r['to_close']:+.2f} |")
    lines += ["", "说明：样本是历史里「看起来像」的情形，不是同一件事重演；同一时期的样本彼此相关，真实的不确定性比区间显示的还大一些。"
                  "历史新闻存档时间还很短，所以相似日当天有没有消息暂时对照不了。"]
    return "\n".join(lines) + "\n"


# ================================================================ 记录和事后评估
SCHEMA = """CREATE TABLE IF NOT EXISTS engine_calls (
    ts TEXT, date TEXT, t TEXT, code TEXT, n INTEGER, verdict TEXT, lean TEXT, sig INTEGER, price REAL, atr REAL,
    long_shrunk REAL, short_shrunk REAL, long_lo REAL, short_lo REAL, n_analogs INTEGER,
    r_long REAL, r_short REAL, evaluated INTEGER DEFAULT 0)"""


def log_judgement(res):
    try:
        import store
        con = store.connect()
        try:
            con.execute(SCHEMA)
            with con:
                con.execute("INSERT INTO engine_calls (ts, date, t, code, n, verdict, lean, sig, price, atr, long_shrunk, short_shrunk, "
                            "long_lo, short_lo, n_analogs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (dt.datetime.now().isoformat(timespec="seconds"), res["day"], res["t"], res["code"], res["n"],
                             res["verdict"], res["lean"][0], int(res["lean"][1]), res["price"], res["atr"],
                             res["long"]["shrunk"], res["short"]["shrunk"], res["long"]["lo"], res["short"]["lo"], res["N"]))
        finally:
            con.close()
    except Exception:
        pass


def evaluate_calls(day, cfg=None):
    """收盘后：用当天完整的 1 分钟K算每次盘感判断之后真实的做多 / 做空 R。返回一段文字。"""
    import store
    s = settings(cfg)
    con = store.connect()
    try:
        con.execute(SCHEMA)
        rows = con.execute("SELECT rowid, code, t, atr FROM engine_calls WHERE date = ? AND evaluated = 0", (str(day),)).fetchall()
        done = 0
        for rid, code, t, A in rows:
            b, _ = load_bars(code, str(day))
            if b is None or len(b) < 385:
                continue
            d = day_arrays(b)
            n = (int(t[:2]) * 60 + int(t[3:5])) - 570
            rl, _ = trade_R(d, n, A, s, 1)
            rs, _ = trade_R(d, n, A, s, -1)
            with con:
                con.execute("UPDATE engine_calls SET r_long = ?, r_short = ?, evaluated = 1 WHERE rowid = ?", (rl, rs, rid))
            done += 1
        allr = con.execute("SELECT verdict, r_long, r_short FROM engine_calls WHERE date = ? AND evaluated = 1", (str(day),)).fetchall()
    finally:
        con.close()
    if not allr:
        return None
    cost = float(s["成本_R"])
    L = [f"盘感判断 {len(allr)} 次"]
    for v, rl, rs in allr:
        if v.startswith("可以追") or v.startswith("等回"):
            r = (rl if "多" in v else rs)
            if r is not None:
                L.append(f"{v}：之后照衡量尺子做 {r - cost:+.2f}R")
    return "；".join(L)


# ================================================================ 走前验证
def validate(days=120, per_day=6, seed=7, cfg=None, log=print):
    """只用过去预测未来：抽最近 days 个交易日、每天 per_day 个（股票, Kill Zone 里的时刻），用当时之前的历史算结论，
    再和真实结果对比。返回文字报告。"""
    s = settings(cfg)
    obj = load_cache(log=log)
    R = obj["rows"]
    zs = obj["zones"]
    rng = np.random.default_rng(seed)
    all_days = sorted(R["date"].unique())
    test_days = all_days[-days:]
    if len(all_days) - len(test_days) < 60:
        return "历史太短，没法做走前验证（测试日之前至少要 60 个交易日）"
    kz_ns = [n for n in NS if in_zone(570 + n, zs) and n >= 10]
    out = []
    for D in test_days:
        rows = R[(R["date"] == D) & (R["n"].isin(kz_ns))].dropna(subset=["ret", "vw", "R_long", "R_short"])
        if not len(rows):
            continue
        pick = rows.iloc[rng.choice(len(rows), size=min(per_day, len(rows)), replace=False)]
        for _, q in pick.iterrows():
            top, base = analogs(R, q.to_dict(), int(q["n"]), D, s)
            if len(top) < int(s["最少样本"]):
                continue
            assert (top["date"] < D).all() and (base["date"] < D).all()        # 不能偷看未来
            st = stats(top, base, s)
            d, sig = lean(st, s)
            out.append(dict(date=D, code=q["code"], n=int(q["n"]), lean=d, sig=sig, vw=float(q["vw"]),
                            pl=st["long"]["shrunk"], ps=st["short"]["shrunk"], bl=st["long"]["base"], bs=st["short"]["base"],
                            wl=st["long"]["win"], wl_base=st["long"]["win_base"],
                            rl=float(q["R_long"]) - float(s["成本_R"]), rs=float(q["R_short"]) - float(s["成本_R"]),
                            win_l=bool(q["win_long"])))
        if log and len(out) % 120 < per_day:
            log(f"  · 验证到 {pd.Timestamp(D):%Y-%m-%d}，{len(out)} 个")
    V = pd.DataFrame(out)
    if not len(V):
        return "没有可验证的样本"
    return validation_text(V, s, test_days)


def validation_text(V, s, test_days):
    cost = float(s["成本_R"])
    def grp(mask, side):
        x = V.loc[mask, "rl" if side == "多" else "rs"]
        if not len(x):
            return "0 次"
        m, lo, hi = mean_ci(x.values) if len(x) > 1 else (x.mean(), float("nan"), float("nan"))
        return f"{len(x)} 次，平均 {m:+.2f}R（95% 区间 {lo:+.2f} ~ {hi:+.2f}），赚钱 {(x > 0).mean() * 100:.0f}%"
    edge = float(s["离VWAP太远_ATR"])
    sig_long = (V["lean"] == "多") & V["sig"]
    sig_short = (V["lean"] == "空") & V["sig"]
    chase_long = sig_long & (V["vw"] <= edge)
    chase_short = sig_short & (-V["vw"] <= edge)
    # 预测值和真实结果的关系
    from scipy.stats import spearmanr
    rho_l = spearmanr(V["pl"], V["rl"]).correlation
    rho_s = spearmanr(V["ps"], V["rs"]).correlation
    brier = float(((V["wl"] - V["win_l"].astype(float)) ** 2).mean())
    brier0 = float(((V["wl_base"] - V["win_l"].astype(float)) ** 2).mean())
    V["bucket"] = pd.qcut(V["pl"] - V["ps"], 5, labels=False, duplicates="drop")
    L = [f"# 盘感引擎体检（走前验证）", "",
         f"测试区间：{pd.Timestamp(test_days[0]):%Y-%m-%d} ~ {pd.Timestamp(test_days[-1]):%Y-%m-%d}，"
         f"{V['date'].nunique()} 个交易日，{len(V)} 个随机抽的「股票 × Kill Zone 时刻」。每个都只用当天之前的历史算结论。",
         f"衡量尺子：下一根开盘进场，止损 {s['止损_ATR']} 倍 ATR，目标 {s['目标_R']}R，已扣成本 {cost}R。", "",
         "## 结论分组后的真实结果", "",
         f"- 所有样本照做多：{grp(V.index == V.index, '多')}",
         f"- 所有样本照做空：{grp(V.index == V.index, '空')}",
         f"- 引擎说「做多有优势」：{grp(sig_long, '多')}",
         f"　其中离 VWAP 不远、会给「可以追多」的：{grp(chase_long, '多')}",
         f"- 引擎说「做空有优势」：{grp(sig_short, '空')}",
         f"　其中会给「可以追空」的：{grp(chase_short, '空')}",
         f"- 引擎说「观察（偏多）」照做多：{grp((V['lean'] == '多') & ~V['sig'], '多')}",
         f"- 引擎说「观察（偏空）」照做空：{grp((V['lean'] == '空') & ~V['sig'], '空')}",
         f"- 引擎说「没倾向」：照做多 {grp(V['lean'].isna(), '多')}", "",
         "## 预测数字准不准", "",
         f"- 预测的做多平均 R 和真实做多 R 的秩相关：{rho_l:+.3f}；做空：{rho_s:+.3f}（0 = 没关系，越大越好）",
         f"- 做多赢率的 Brier 分数：引擎 {brier:.4f}，直接用基准 {brier0:.4f}"
         f"（越小越好；引擎比基准小才说明相似情形真的提供了信息）", "",
         "按「预测做多 − 预测做空」从低到高分 5 组，每组真实的做多 / 做空 R：", "",
         "| 组 | 次数 | 预测差 | 真实做多 | 真实做空 |", "| --- | --- | --- | --- | --- |"]
    for k, g in V.groupby("bucket"):
        L.append(f"| {int(k) + 1} | {len(g)} | {(g['pl'] - g['ps']).mean():+.2f} | {g['rl'].mean():+.2f}R | {g['rs'].mean():+.2f}R |")
    by_m = V.assign(m=pd.to_datetime(V["date"]).dt.strftime("%Y-%m"))
    act = by_m[sig_long | sig_short]
    if len(act):
        act = act.assign(r=np.where(act["lean"] == "多", act["rl"], act["rs"]))
        L += ["", "「有优势」的结论按月：" + "；".join(f"{m} {len(g)} 次 {g['r'].mean():+.2f}R" for m, g in act.groupby("m"))]
    L += ["", "怎么读：「有优势」那几行的平均 R 明显 > 0、而且比「所有样本」高，才说明引擎的结论有用；"
              "次数少于 30 的组别当参考。秩相关接近 0、Brier 不比基准好，就说明这套相似匹配目前没有预测力，"
              "盘中只能把它当「历史上这种走势通常怎么样」的背景，不能当信号。门槛是事先定的，没有按这份结果调过。"]
    return "\n".join(L) + "\n"


# ================================================================ Kill Zone 统计（盘前报告用）
def kz_stats(codes=None, lookback=60, cfg=None):
    """每只股票、每个 Kill Zone 的历史统计。返回 (DataFrame, Kill Zone 列表)。"""
    obj = load_cache(log=None)
    D = obj["days"].copy()
    zs = obj["zones"]
    if codes:
        D = D[D["code"].isin(codes)]
    out = []
    for code, g in D.sort_values("date").groupby("code"):
        g = g.tail(lookback)
        if not len(g):
            continue
        row = dict(code=code, N=len(g), last=g["date"].max())
        o = g["open"].values
        cl = g["close"].values
        c6 = g["c6"].values
        row["c1_hold"] = float(np.mean(np.sign(cl - o) == np.sign(c6 - o)))
        row["hod_lod_first"] = float(np.mean(np.minimum(g["hod_t"], g["lod_t"]) <= zs[0][1])) if zs else float("nan")
        row["day_rng"] = float(g["day_rng"].mean())
        for k, z in enumerate(zs):
            if f"z{k}_move" not in g:
                continue
            mv = g[f"z{k}_move"].values
            row[f"z{k}_rng"] = float(np.nanmean(g[f"z{k}_rng"]))
            row[f"z{k}_absmove"] = float(np.nanmean(np.abs(mv)))
            row[f"z{k}_hl"] = float(np.mean(((g["hod_t"] > z[0]) & (g["hod_t"] <= z[1])) | ((g["lod_t"] > z[0]) & (g["lod_t"] <= z[1]))))
            if k == 0:
                rest = (cl - g[f"z{k}_end"].values) / g["atr"].values
                row["z0_cont"] = float(np.mean(np.sign(rest) == np.sign(mv)))
            else:
                row[f"z{k}_same"] = float(np.mean(np.sign(mv) == np.sign(g["z0_move"].values))) if "z0_move" in g else float("nan")
        last = g.iloc[-1]
        row["y_moves"] = [float(last.get(f"z{k}_move", np.nan)) for k in range(len(zs))]
        row["y_hod"], row["y_lod"] = int(last["hod_t"]), int(last["lod_t"])
        out.append(row)
    return pd.DataFrame(out), zs


def _t(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def kz_markdown(codes, lookback=60, cfg=None):
    df, zs = kz_stats(codes, lookback, cfg)
    if not len(df):
        return "没有 1 分钟历史数据"
    names = [zone_name(z) for z in zs]
    head = ["代码", "天数", "C1 方向保持"]
    for k, nm in enumerate(names):
        head += [f"KZ{k + 1} 振幅", f"KZ{k + 1} 出高/低点"]
        head += ["KZ1 后延续"] if k == 0 else [f"KZ{k + 1} 与 KZ1 同向"]
    head += ["全天振幅", "昨天 " + " / ".join(f"KZ{k + 1}" for k in range(len(zs))), "昨天高 / 低点时间"]
    L = [f"Kill Zone：" + "；".join(f"KZ{k + 1} = {nm}" for k, nm in enumerate(names)) + f"（美东）。统计最近 {lookback} 个交易日，单位：倍日线 ATR。", "",
         "| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    for _, r in df.sort_values("code").iterrows():
        cells = [r["code"], str(r["N"]), _p(r["c1_hold"])]
        for k in range(len(zs)):
            cells += [f"{r.get(f'z{k}_rng', float('nan')):.2f}", _p(r.get(f"z{k}_hl"))]
            cells += [_p(r.get("z0_cont"))] if k == 0 else [_p(r.get(f"z{k}_same"))]
        cells += [f"{r['day_rng']:.2f}", " / ".join(f"{x:+.2f}" for x in r["y_moves"]), f"{_t(r['y_hod'])} / {_t(r['y_lod'])}"]
        L.append("| " + " | ".join(cells) + " |")
    L += ["", "怎么读：",
          "- C1 方向保持 = 第 6 根收在开盘价哪一侧，收盘也在同一侧的比例（50% = 没规律）",
          "- KZx 振幅 = 这段时间内最高到最低平均几倍 ATR；出高/低点 = 全天最高或最低点出现在这段里的比例",
          "- KZ1 后延续 = KZ1 结束时的方向，到收盘继续同方向的比例；KZx 与 KZ1 同向 = 这段的涨跌方向和 KZ1 一样的比例",
          "- 昨天 KZx = 昨天这段时间的涨跌（倍 ATR）",
          f"- {lookback} 天的比例，±13 个百分点以内都可能只是运气（95% 区间），别把 55% 当规律"]
    return "\n".join(L)


# ================================================================ 命令行
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="*")
    ap.add_argument("--date")
    ap.add_argument("--time")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--kz", action="store_true")
    a = ap.parse_args()
    if a.build:
        print(build_cache(log=None))
    elif a.validate:
        print(validate(days=a.days))
    elif a.kz:
        print(kz_markdown([c.upper() for c in a.code] or None))
    elif a.code:
        text, _ = judge(a.code[0], a.date, a.time)
        print(text)
    else:
        ap.print_help()
