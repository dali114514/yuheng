# -*- coding: utf-8 -*-
"""
「像专业交易员一样阅读市场结构」回测（研究专用，只读数据库）

来源：B 站 BV1PfxZ6uEDN（up 主「不聪明钱交易」，2026-11-26 读字幕整理）。他说出来的规则 → 这里的程序化口径：

  结构（箱体法，只看收盘，影线不算）——同一个函数既算外部结构（15 分钟 / 5 分钟K，跨天连续）也算内部结构（1 分钟K）：
    多头里：一段上涨的最高点之后出现一根没再创新高的K（「回撤」，多小都算）→ 这个最高点成为摆动高点；
            收盘站上摆动高点 = 结构突破（BOS）；突破时，从摆动高点那根到突破这根之间的最低点 = 新的摆动低点；
            收盘跌破摆动低点 = 结构转换（CHoCH），转成空头；转换时，从摆动低点那根到这根之间的最高点 = 空头的摆动高点。
    空头完全对称（价格取负实现）。
  进场（做多；做空把价格取负，完全对称）：
    外部结构多头（只用已经收盘的外部K）＋ 内部结构从空头转回多头（1 分钟 CHoCH 向上）→ 内外对齐。
    入场两种：mkt = 下一根 1 分钟开盘直接进；poc = 在外部这段上涨（摆动低点 → 这段最高点）的成交量最大价位挂限价，
              最多等 60 根，先碰到目标或止损就取消。
    止损两种：ext = 外部摆动低点 − 0.01 ATR；int = 内部那段回调的最低点（内部 CHoCH 时的摆动低点）− 0.01 ATR。
    目标：外部这段上涨的最高点（「外部摆动高点」）；入场已经在目标上方就不做。16:00 没结果按收盘平（只做日内）。
    流动性过滤（他说前一日低点 / 纽约时段低点没被扫就别做多）：昨低在入场价和外部摆动低点之间、今天还没跌破昨低 → 不做。
  时段：9:36 起进；「早盘」= 11:00 前（交易者的时段），「全天」= 15:30 前。
  同一只股票同一个组合，上一笔没走完不开下一笔。同一根K同时碰到止损和目标算止损。
  成本：正股每笔 0.01 倍日线 ATR 换成 R 扣掉；期权按 intraday 的口径（平值、约 3 天到期、Black-Scholes、交易时间时钟、每边 1% 价差）。
  对照：
    rand = 同一天、同方向、随机时间进场，止损 / 目标离入场的距离和真信号一样 → 看「内外对齐」这个时机有没有用；
    inner = 视频说的错误做法：外部多头时跟着内部结构转空去做空（同样的止损 / 目标口径，镜像）。
  另外统计：外部多头里，1 分钟K影线刺破外部摆动低点但收盘没破（「顺势方向的假突破」）之后 30 分钟的涨跌，和外部多头里所有K比。
数据：history_long.db（2019-01 起）+ history.db 的 1 分钟K，只用完整交易日（390 根）。样本外 2024 起。
"""
import json
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intraday   # noqa: E402

COST = 0.01
SKIP = {"ANT", "CRWV"}
SPLIT = "2024-01-01"
MIN_RISK = 0.02                                # 止损离入场不到 0.02 倍日线 ATR 的不算（几美分，不现实，扣成本会爆掉）
OUT = HERE / "data" / ".structure_bt.pkl"
FIRST, LAST_AM, LAST_ALL = 576, 660, 930      # 9:36、11:00、15:30（开盘时间，分钟）


def structure(h, l, c):
    """箱体法市场结构（收盘为准）。返回每根收盘后的：trend(1/-1/0)、ev(+1 BOS 多 / +2 CHoCH 多 / -1 / -2)、
    sl、sli（多头摆动低点和位置）、legh、leghi（多头这段最高点和位置）；空头的对应量用 sh、shi、legl、legli。"""
    n = len(c)
    trend = np.zeros(n, np.int8)
    ev = np.zeros(n, np.int8)
    SL = np.full(n, np.nan); SLI = np.zeros(n, np.int64); LH = np.full(n, np.nan); LHI = np.zeros(n, np.int64)
    SH = np.full(n, np.nan); SHI = np.zeros(n, np.int64); LL = np.full(n, np.nan); LLI = np.zeros(n, np.int64)
    tr = 0
    sh, shi, sl, sli = h[0], 0, l[0], 0          # 当前有效的摆动高 / 低
    mh, mhi, ml, mli = h[0], 0, l[0], 0          # 这段的极值
    sh_ok = sl_ok = True                         # 摆动点已被回撤确认、可以用来判断 BOS
    for i in range(1, n):
        e = 0
        if tr >= 0 and tr == 1:
            if c[i] < sl:                                        # CHoCH 向下
                j = sli + int(np.argmax(h[sli:i + 1]))
                sh, shi = h[j], j
                ml, mli, sl_ok = l[i], i, False
                tr, e = -1, -2
            else:
                if sh_ok and c[i] > sh:                          # BOS 向上
                    j = shi + int(np.argmin(l[shi:i + 1]))
                    sl, sli = l[j], j
                    mh, mhi, sh_ok = h[i], i, False
                    e = 1
                elif h[i] > mh:
                    mh, mhi = h[i], i
                elif not sh_ok:                                  # 回撤出现：这段最高点成为摆动高点
                    sh, shi, sh_ok = mh, mhi, True
        elif tr == -1:
            if c[i] > sh:                                        # CHoCH 向上
                j = shi + int(np.argmin(l[shi:i + 1]))
                sl, sli = l[j], j
                mh, mhi, sh_ok = h[i], i, False
                tr, e = 1, 2
            else:
                if sl_ok and c[i] < sl:                          # BOS 向下
                    j = sli + int(np.argmax(h[sli:i + 1]))
                    sh, shi = h[j], j
                    ml, mli, sl_ok = l[i], i, False
                    e = -1
                elif l[i] < ml:
                    ml, mli = l[i], i
                elif not sl_ok:
                    sl, sli, sl_ok = ml, mli, True
        else:                                                    # 还没有方向：第一次收盘越过起点高 / 低
            if c[i] > sh:
                j = int(np.argmin(l[:i + 1])); sl, sli = l[j], j
                mh, mhi, sh_ok, tr, e = h[i], i, False, 1, 2
            elif c[i] < sl:
                j = int(np.argmax(h[:i + 1])); sh, shi = h[j], j
                ml, mli, sl_ok, tr, e = l[i], i, False, -1, -2
            else:
                if h[i] > mh:
                    mh, mhi = h[i], i
                if l[i] < ml:
                    ml, mli = l[i], i
        trend[i], ev[i] = tr, e
        if tr == 1:
            SL[i], SLI[i], LH[i], LHI[i] = sl, sli, max(mh, sh), (mhi if mh >= sh else shi)
        elif tr == -1:
            SH[i], SHI[i], LL[i], LLI[i] = sh, shi, min(ml, sl), (mli if ml <= sl else sli)
    return trend, ev, SL, SLI, LH, LHI, SH, SHI, LL, LLI


def poc(lo, hi, h, l, c, v, bins=40):
    if not (hi > lo):
        return np.nan
    tp = (h + l + c) / 3
    k = np.clip(((tp - lo) / (hi - lo) * bins).astype(int), 0, bins - 1)
    vol = np.bincount(k, weights=v, minlength=bins)
    return lo + (np.argmax(vol) + 0.5) * (hi - lo) / bins


def walk(o, h, l, c, k0, e, stop, tgt, end):
    """从第 k0 根（含）开始，入场价 e，返回 (出场根, 出场价)。同一根两边都碰到算止损。"""
    hs, ls = h[k0:end], l[k0:end]
    hit_s = np.nonzero(ls <= stop)[0]
    hit_t = np.nonzero(hs >= tgt)[0]
    s = hit_s[0] if len(hit_s) else 10 ** 9
    t = hit_t[0] if len(hit_t) else 10 ** 9
    if s == 10 ** 9 and t == 10 ** 9:
        return end - 1, c[end - 1]
    if s <= t:
        k = k0 + s
        return k, (o[k] if (k > k0 and o[k] < stop) else stop)
    k = k0 + t
    return k, (o[k] if (k > k0 and o[k] > tgt) else tgt)


def opt_ret(tmin, e, x, px_in, px_out, d, iv):
    T0 = (intraday.DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / intraday.YEAR_MIN
    T1 = T0 - (tmin[x] - tmin[e] + 1) / intraday.YEAR_MIN
    p0, _ = intraday.bs(px_in, px_in, T0, iv, d == 1)
    p1, _ = intraday.bs(px_out, px_in, T1, iv, d == 1)
    p0, p1 = float(p0), float(p1)
    if p0 <= 0:
        return np.nan
    return p1 * (1 - intraday.HALF_SPREAD) / (p0 * (1 + intraday.HALF_SPREAD)) - 1


def run_dir(df, dinfo, d, tf, rng):
    """一个方向（d=1 做多原价；d=-1 做空用取负的价格），一个外部周期。"""
    sgn = 1.0 if d == 1 else -1.0
    o = df.open.values * sgn
    if d == 1:
        h, l = df.high.values.copy(), df.low.values.copy()
    else:
        h, l = -df.low.values, -df.high.values
    c = df.close.values * sgn
    v = df.volume.values
    n = len(c)
    tmin = (df.index.hour.values * 60 + df.index.minute.values).astype(int)   # K 线结束时间
    dayid = dinfo["dayid"]
    # 外部K（每天从 9:30 起每 tf 分钟一根，跨天连续算结构）
    g = np.arange(n) // tf                       # 每天 390 根，能被 5/15 整除，组不跨天
    eo_end = np.r_[np.nonzero(np.diff(g))[0], n - 1]
    eh = np.maximum.reduceat(h, np.r_[0, eo_end[:-1] + 1])
    el = np.minimum.reduceat(l, np.r_[0, eo_end[:-1] + 1])
    ec = c[eo_end]
    etr, eev, eSL, eSLI, eLH, eLHI, *_ = structure(eh, el, ec)
    # 每根 1 分钟K收盘时，能用的最近一根已收盘外部K
    last_ext = np.searchsorted(eo_end, np.arange(n), side="right") - 1
    itr, iev, iSL, iSLI, iLH, iLHI, iSH, iSHI, iLL, iLLI = structure(h, l, c)
    ext_start = np.r_[0, eo_end[:-1] + 1]
    rows = []
    open_until = {}
    # 「顺势假突破」统计：外部多头里影线刺破外部摆动低点、收盘没破
    fb = []
    for i in range(1, n - 1):
        k = last_ext[i]
        if k < 0 or etr[k] != 1:
            continue
        di = dayid[i]
        day_end = dinfo["day_end"][di]
        A = dinfo["atr"][di]
        if not (A > 0):
            continue
        if i + 30 <= day_end:
            if l[i] < eSL[k] <= c[i]:
                fb.append(((c[i + 30] - c[i]) / A, d, tf, dinfo["date"][di], 1))
            elif i % 15 == 0:
                fb.append(((c[i + 30] - c[i]) / A, d, tf, dinfo["date"][di], 0))
        sig_in = iev[i] == 2                         # 内部转回顺势（视频的做法）
        sig_inner = iev[i] == -2                     # 内部转逆势（视频说的错误做法，镜像做反方向）
        if not (sig_in or sig_inner):
            continue
        e = i + 1
        if e > day_end or tmin[e] - 1 < FIRST or tmin[e] - 1 > LAST_ALL:
            continue
        iv = dinfo["iv"][di]
        ext_sl, ext_lh = eSL[k], eLH[k]
        pdl = dinfo["pl"][di] * (1.0 if d == 1 else 0) + (-dinfo["ph"][di] if d == -1 else 0)
        day0 = dinfo["day_start"][di]
        today_low = l[day0:i + 1].min()
        if sig_in:
            ein = o[e]
            if not (ext_lh > ein):
                continue
            liq = bool(ext_sl < pdl < ein and today_low > pdl)
            seg = slice(ext_start[eSLI[k]], eo_end[eLHI[k]] + 1)
            p = poc(ext_sl, ext_lh, h[seg], l[seg], c[seg], v[seg])
            stops = {"ext": ext_sl - 0.01 * A, "int": iSL[i] - 0.01 * A}
            for sname, stop in stops.items():
                if not (ein > stop):
                    continue
                for ent in ("mkt", "poc"):
                    key = ("in", tf, ent, sname)
                    if open_until.get(key, -1) >= e:
                        continue
                    if ent == "mkt":
                        k0, px = e, ein
                    else:
                        if not (stop < p < ein):
                            continue
                        lim_end = min(e + 60, day_end + 1)
                        fill = None
                        for q in range(e, lim_end):
                            if tmin[q] - 1 > LAST_ALL:
                                break
                            if l[q] <= p:
                                fill = q
                                break
                            if h[q] >= ext_lh:
                                break
                        if fill is None:
                            continue
                        k0, px = fill, min(o[fill], p)
                    x, xp = walk(o, h, l, c, k0, px, stop, ext_lh, day_end + 1)
                    risk = (ein if ent == "mkt" else p) - stop       # 计划的风险（开盘跳到止损下方成交时不让分母变成 0）
                    R = (xp - px) / risk
                    ret = opt_ret(tmin, k0, x, px * sgn, xp * sgn, d, iv)
                    open_until[key] = x
                    rows.append(dict(kind="in", tf=tf, ent=ent, stop=sname, d=d, date=dinfo["date"][di], t=int(tmin[k0] - 1),
                                     R=R, Rn=R - COST * A / risk, opt=ret, risk_atr=risk / A, rr=(ext_lh - px) / risk,
                                     liq=liq, hit=int(xp >= ext_lh)))
                    # 随机对照：同一天同方向，随机时间进，止损 / 目标距离相同（只给 mkt 做）
                    if ent == "mkt":
                        last = min(day_end, day0 + (LAST_ALL - 570) + 1)
                        lo_k = day0 + (FIRST - 570) + 1
                        if last > lo_k:
                            r0 = int(rng.integers(lo_k, last))
                            rp = o[r0]
                            rx, rxp = walk(o, h, l, c, r0, rp, rp - risk, rp + (ext_lh - px), day_end + 1)
                            rR = (rxp - rp) / risk
                            rows.append(dict(kind="rand", tf=tf, ent=ent, stop=sname, d=d, date=dinfo["date"][di],
                                             t=int(tmin[r0] - 1), R=rR, Rn=rR - COST * A / risk,
                                             opt=opt_ret(tmin, r0, rx, rp * sgn, rxp * sgn, d, iv), risk_atr=risk / A,
                                             rr=(ext_lh - px) / risk, liq=liq, hit=int(rxp >= rp + (ext_lh - px))))
        else:
            # 外部多头时跟着内部转空做空：在「取负价格」里就是做多 -价格。
            # 止损 = 内部摆动高点（内部 CHoCH 时的 sh）+ 0.01 ATR；目标 = 外部摆动低点
            key = ("inner", tf)
            if open_until.get(key, -1) >= e:
                continue
            ein = o[e]
            stop = iSH[i] + 0.01 * A
            tgt = ext_sl
            if not (stop > ein > tgt):
                continue
            # 在取负空间里走一遍
            x, xp = walk(-o, -l, -h, -c, e, -ein, -stop, -tgt, day_end + 1)
            xp = -xp
            risk = stop - ein
            R = (ein - xp) / risk
            open_until[key] = x
            rows.append(dict(kind="inner", tf=tf, ent="mkt", stop="int", d=-d, date=dinfo["date"][di], t=int(tmin[e] - 1),
                             R=R, Rn=R - COST * A / risk, opt=opt_ret(tmin, e, x, ein * sgn, xp * sgn, -d, iv),
                             risk_atr=risk / A, rr=(ein - tgt) / risk, liq=False, hit=int(xp <= tgt)))
    return rows, fb


def run_code(code):
    df = intraday.load_1m(code)
    if df.empty:
        return code, [], []
    days = intraday.daily_from_1m(df)
    keep = [d for d, gg in df.groupby(df.index.date) if len(gg) == 390 and d in days.index
            and days.loc[d, "atr_prev"] > 0 and days.loc[d, "iv"] > 0 and days.loc[d, "pl"] > 0]
    df = df[np.isin(df.index.date, keep)]
    if not len(df):
        return code, [], []
    dates = df.index.date
    uniq, dayid = np.unique(dates, return_inverse=True)
    starts = np.searchsorted(dayid, np.arange(len(uniq)))
    dinfo = dict(dayid=dayid, day_start=starts, day_end=np.r_[starts[1:] - 1, len(df) - 1],
                 date=[str(x) for x in uniq],
                 atr=days.loc[uniq, "atr_prev"].values.astype(float), iv=days.loc[uniq, "iv"].values.astype(float),
                 pl=days.loc[uniq, "pl"].values.astype(float), ph=days.loc[uniq, "ph"].values.astype(float))
    rng = np.random.default_rng(zlib.crc32(code.encode()))
    rows, fbs = [], []
    for tf in (15, 5):
        for d in (1, -1):
            r, fb = run_dir(df, dinfo, d, tf, rng)
            rows += r
            fbs += fb
    for r in rows:
        r["code"] = code
    return code, rows, [dict(code=code, fwd=a, d=b, tf=t, date=dt, ev=x) for a, b, t, dt, x in fbs]


def run(codes=None, log=print):
    codes = codes or [c for c in intraday.all_codes() if c not in SKIP]
    rows, fbs = [], []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for code, r, fb in ex.map(run_code, codes):
            rows += r
            fbs += fb
            log(f"{code}: {len(r)} 笔")
    df = pd.DataFrame(rows)
    fb = pd.DataFrame(fbs)
    pd.to_pickle({"trades": df, "fb": fb}, OUT)
    return df, fb


def ci(g, col):
    """按日期聚成一天一个数再算 95% 区间（同一天的股票彼此相关）。"""
    s = g.groupby("date")[col].sum()
    nday = g.groupby("date")[col].size()
    m = s.sum() / nday.sum()
    per = s / nday
    w = nday / nday.sum()
    se = np.sqrt(((per - m) ** 2 * w ** 2).sum()) if len(per) > 1 else np.nan
    return m, m - 1.96 * se, m + 1.96 * se


def summary(df, fb):
    out = {}
    df = df[df.risk_atr >= MIN_RISK].copy()
    df["oos"] = df["date"] >= SPLIT
    df["am"] = df["t"] <= LAST_AM
    groups = []
    for kind in ("in", "rand", "inner"):
        for tf in (15, 5):
            for ent in ("mkt", "poc"):
                for st in ("ext", "int"):
                    g0 = df[(df.kind == kind) & (df.tf == tf) & (df.ent == ent) & (df.stop == st)]
                    if not len(g0):
                        continue
                    for sess in ("am", "all"):
                        g1 = g0[g0.am] if sess == "am" else g0
                        for flt in ("无", "流动性", "盈亏比≥1", "两个都加"):
                            if kind == "inner" and flt in ("流动性", "两个都加"):
                                continue
                            g2 = g1
                            if flt in ("流动性", "两个都加"):
                                g2 = g2[~g2.liq]
                            if flt in ("盈亏比≥1", "两个都加"):
                                g2 = g2[g2.rr >= 1]
                            rec = dict(kind=kind, tf=tf, ent=ent, stop=st, sess=sess, flt=flt)
                            for part, g in (("is", g2[~g2.oos]), ("oos", g2[g2.oos])):
                                if len(g) < 20:
                                    rec[part] = None
                                    continue
                                m, lo, hi = ci(g, "Rn")
                                gg = g.dropna(subset=["opt"])
                                om, olo, ohi = ci(gg, "opt") if len(gg) > 20 else (np.nan,) * 3
                                rec[part] = dict(n=len(g), days=g.date.nunique(), R=g.R.mean(), Rn=m, Rn_lo=lo, Rn_hi=hi,
                                                 win=(g.R > 0).mean(), hit=g.hit.mean(), risk_atr=g.risk_atr.median(),
                                                 rr=g.rr.median(), opt=om, opt_lo=olo, opt_hi=ohi,
                                                 long=g[g.d == 1].Rn.mean(), short=g[g.d == -1].Rn.mean())
                            groups.append(rec)
    out["groups"] = groups
    if len(fb):
        fb = fb.copy()
        out["fakebreak"] = {f"{tf}_{'假突破' if ev else '基准'}": dict(n=int(len(x)), up=float((x.fwd > 0).mean()),
                                                                     mean_atr=float(x.fwd.mean()))
                            for (tf, ev), x in fb.groupby(["tf", "ev"])}
    return out


KIND = {"in": "视频做法（内外对齐）", "rand": "随机时间对照", "inner": "跟内部结构逆势做"}
ENT = {"mkt": "直接进", "poc": "POC 挂单"}
STOP = {"ext": "外部摆动点", "int": "内部摆动点"}


def _f(x, pct=False):
    if x is None or x != x:
        return "—"
    return f"{x * 100:+.1f}%" if pct else f"{x:+.3f}"


def report(s, path=None):
    import datetime as _dt
    today = _dt.date.today().isoformat()
    path = path or (HERE.parent / "报告" / f"市场结构回测_{today}.md")
    L = [f"# 市场结构（内外结构对齐）回测 · {today}", "",
         "来源：B 站 BV1PfxZ6uEDN「像专业交易员一样阅读市场结构」。程序 `程序/structure_bt.py`，口径写在程序开头。",
         "34 只股票（ANT、CRWV 除外）2019-01 起 1 分钟K；外部结构用 15 分钟 / 5 分钟K，内部结构用 1 分钟K；样本内 2019–2023，样本外 2024 起。",
         "R 已扣成本（每笔 0.01 倍日线 ATR）；止损离入场不到 0.02 倍 ATR 的不算；期权 = 平值约 3 天到期的模型估算，扣价差。95% 区间按日期聚合（同一天的股票彼此相关）。", "",
         "## 主表（不加筛选）", "",
         "| 做法 | 外部 | 入场 | 止损 | 时段 | 样本内笔数 | 样本内 R | 样本外笔数 | 样本外 R（95% 区间） | 样本外胜率 | 样本外到目标 | 止损中位（ATR） | 盈亏比中位 | 样本外期权每笔 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for g in s["groups"]:
        if g["flt"] != "无":
            continue
        a, b = g.get("is"), g.get("oos")
        if not b:
            continue
        L.append(f"| {KIND[g['kind']]} | {g['tf']} 分钟 | {ENT[g['ent']]} | {STOP[g['stop']]} | {'早盘' if g['sess'] == 'am' else '全天'} | "
                 f"{a['n'] if a else '—'} | {_f(a['Rn']) if a else '—'} | {b['n']} | {_f(b['Rn'])}（{_f(b['Rn_lo'])} ~ {_f(b['Rn_hi'])}） | "
                 f"{b['win'] * 100:.0f}% | {b['hit'] * 100:.0f}% | {b['risk_atr']:.2f} | {b['rr']:.2f} | {_f(b['opt'], True)} |")
    L += ["", "## 加筛选（只看视频做法，样本外）", "",
          "| 外部 | 入场 | 止损 | 时段 | 筛选 | 笔数 | R（95% 区间） | 随机对照 R | 期权每笔 | 随机对照期权 |", "|---|---|---|---|---|---|---|---|---|---|"]
    idx = {(g["kind"], g["tf"], g["ent"], g["stop"], g["sess"], g["flt"]): g for g in s["groups"]}
    for g in s["groups"]:
        if g["kind"] != "in" or g["flt"] == "无" or not g.get("oos"):
            continue
        b = g["oos"]
        r = idx.get(("rand", g["tf"], "mkt", g["stop"], g["sess"], g["flt"]))
        rb = r.get("oos") if r and g["ent"] == "mkt" else None
        L.append(f"| {g['tf']} 分钟 | {ENT[g['ent']]} | {STOP[g['stop']]} | {'早盘' if g['sess'] == 'am' else '全天'} | {g['flt']} | {b['n']} | "
                 f"{_f(b['Rn'])}（{_f(b['Rn_lo'])} ~ {_f(b['Rn_hi'])}） | {_f(rb['Rn']) if rb else '—'} | {_f(b['opt'], True)} | "
                 f"{_f(rb['opt'], True) if rb else '—'} |")
    fbk = s.get("fakebreak", {})
    if fbk:
        L += ["", "## 顺势方向的假突破（外部顺势时，影线刺破外部摆动点、收盘没破）之后 30 分钟", "",
              "| 外部 | 情况 | 次数 | 往顺势方向走的比例 | 平均（倍 ATR） |", "|---|---|---|---|---|"]
        for k, v in fbk.items():
            tf, ev = k.split("_")
            L.append(f"| {tf} 分钟 | {ev} | {v['n']} | {v['up'] * 100:.1f}% | {v['mean_atr']:+.4f} |")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    df, fb = run()
    s = summary(df, fb)
    (HERE / "data" / ".structure_bt_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1, default=float),
                                                              encoding="utf-8")
    print(len(df), "笔 ->", OUT)
    print("报告 ->", report(s))
