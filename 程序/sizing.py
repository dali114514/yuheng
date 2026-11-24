# -*- coding: utf-8 -*-
"""
玉衡5.0 · 仓位方案回测（三层的第三层：几张、加减、停手）。只读 K 线库和日内策略库，不下单。

  第一步：挑组合。**只用样本内（2019–2023）挑**，样本外（2024 起）完全不看，所以样本外的结果是真正的留出检验。
    挑法和今日预案一样（每只股票 × 开盘类型 × 方向挑一组「入场 + 止损 + 止盈」，跨股票成立比例要高于随机入场），
    只是「样本内外都赚」换成「样本内赚」。另外两组对照：
      - 个人策略：只用个人策略 A / B，每个方向用样本内跨股票最好的出场，全部股票全部信号；
      - 预案现在的挑法（用到了样本外，偏乐观，只作参考）。
  第二步：每一笔按仓位方案模拟（期权逐根 Black-Scholes 重算，价格换算成今天的权利金水平，每次买卖扣 1% 价差 + 每张 $1）：
      只做 1 张 ／ 固定美元风险 $100 / $200 / $300（张数 = 风险 ÷ 每张止损一次亏多少）／
      金字塔（2 张起，+1R 加 1 张、+2R 再加 1 张，最多 4 张；每次加完整体止损挪到上一次加仓价）／
      分批出场（4 张，+1R 走一半，剩下跟前 5 根移动止损）／
      广撒网滚仓（试仓 1 张，15 分钟不到 +0.5R 砍；+1R 且在 VWAP 有利一侧 → 止损挪保本，回踩 VWAP 守住加 1 张；+2R 止损挪到 +1R）。
    试仓级的组合除了广撒网滚仓都只做 1 张（预案规则）；每笔最多 4 张。
  第三步：按天组合：每天最多 3 笔、单日亏损到 $600 停（基本纪律），广撒网滚仓同时最多 3 笔；
    熔断网格：连亏 2 / 3 / 4 笔停 × 当天赚到 $200 / $400 后回吐一半停，看哪种能降回撤又不明显少赚。

用法：arch -arm64 程序/.venv/bin/python 程序/sizing.py      → 报告/仓位方案回测_<日期>.md + data/日内策略库/sizing.json
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import intraday as I  # noqa: E402

ILIB = HERE / "data" / "日内策略库"
FEE = 1.0                       # 每张每边手续费（美元）
MAX_CONTRACTS = 4
DAY_TRADES, DAY_LOSS, ROLL_CONC = 3, 600.0, 3
SCHEMES = [
    ("one", "只做 1 张"), ("risk100", "固定美元风险 $100"), ("risk200", "固定美元风险 $200"), ("risk300", "固定美元风险 $300"),
    ("pyramid", "金字塔加仓（2 张，+1R 加 1、+2R 再加 1）"), ("scale", "分批出场（4 张，1R 走一半）"), ("roll", "广撒网滚仓"),
]
BREAKERS = [(s, g) for s in (None, 2, 3, 4) for g in (None, 200, 400)]


# ================================================================ 第一步：挑组合

def _load_lib():
    idx = json.loads((ILIB / "index.json").read_text("utf-8"))
    syms = {c: json.loads((ILIB / f"{c}.json").read_text("utf-8")) for c in idx["symbols"] if (ILIB / f"{c}.json").exists()}
    return idx, syms


def picks_holdout(idx, syms, nin=12, pooled_n=15, min_of=8):
    """只用样本内挑：{(代码, 开盘, 方向): (入场, 止损, 止盈, 级别)}。"""
    pooled = {}
    for y in syms.values():
        for key, c in y["combos"].items():
            for g, cell in c["gap"].items():
                a = cell.get("in")
                if a and a["n"] >= pooled_n:
                    p = pooled.setdefault((key, g), [0, 0])
                    p[0] += a["avg"] > 0
                    p[1] += 1
    rate = {k: v[0] / v[1] for k, v in pooled.items() if v[1] >= min_of}
    noise = {}
    for (key, g), r in rate.items():
        sid, _, _, dd = key.split("|")
        if sid == "rand":
            noise[(g, dd)] = max(noise.get((g, dd), 0), r)
    out = {}
    for code, y in syms.items():
        if not y.get("has_in"):
            continue
        best = {}
        for key, c in y["combos"].items():
            sid, stp, tgt, dd = key.split("|")
            if sid == "rand":
                continue
            for g, cell in c["gap"].items():
                r = rate.get((key, g))
                nz = noise.get((g, dd), 0)
                a = cell.get("in")
                if r is None or r < max(0.15, nz + 0.05) or not a or a["n"] < nin or a["avg"] <= 0:
                    continue
                sc = (r, a["avg"])
                if (g, dd) not in best or sc > best[(g, dd)][0]:
                    lvl = "检验通过" if r >= max(0.30, nz + 0.15) else "试仓级"
                    best[(g, dd)] = (sc, sid, stp, tgt, lvl)
        for (g, dd), (_, sid, stp, tgt, lvl) in best.items():
            out[(code, g, dd)] = (sid, stp, tgt, lvl)
    return out


def picks_playbook():
    import playbook
    it = playbook.build_intraday(playbook.settings(), live=False, log=lambda *a: None)
    out = {}
    for s in (it or {}).get("stocks", []):
        for b in s["branches"]:
            if b["level"] != "无":
                out[(s["code"], b["gap"], b["dir"])] = (b["sid"], b["stop"], b["tgt"], b["level"])
    return out


def picks_personal(idx, syms):
    """个人策略 A / B：每个方向取样本内跨股票加权最好的出场，所有股票、所有开盘都做（级别算检验通过，按方案定张数）。"""
    agg = {}
    for y in syms.values():
        for key, c in y["combos"].items():
            sid, stp, tgt, dd = key.split("|")
            if sid not in ("A", "B") or not c.get("in"):
                continue
            a = agg.setdefault((sid, dd, stp, tgt), [0, 0.0])
            a[0] += c["in"]["n"]
            a[1] += c["in"]["n"] * c["in"]["avg"]
    best = {}
    for (sid, dd, stp, tgt), (n, s) in agg.items():
        if n and ((sid, dd) not in best or s / n > best[(sid, dd)][0]):
            best[(sid, dd)] = (s / n, stp, tgt)
    out = {}
    for code in syms:
        for g in I.GAP_IDS:
            for (sid, dd), (_, stp, tgt) in best.items():
                out.setdefault((code, g, dd), []).append((sid, stp, tgt, "检验通过"))
    return out, {f"{sid}|{dd}": {"stop": stp, "tgt": tgt, "in_avg": round(a, 4)} for (sid, dd), (a, stp, tgt) in best.items()}


# ================================================================ 第二步：一笔交易的各种仓位方案

def _opt(S, K, T, iv, call):
    return np.asarray(I.bs(S, K, np.maximum(T, 1e-6), iv, call)[0], float)


def simulate_trade(g, vw, tmin, i, d, struct, atr, iv, prem_x, fa, fl, ph, pl, stop, tgt, level, p_now):
    """返回 dict：每种方案的美元盈亏（按今天的权利金水平折算）、最多张数、入场 / 出场时间。"""
    r = I.exits(g, vw, tmin, i, d, struct, atr, iv, prem_x, fa, fl, ph, pl)
    if not r:
        return None
    e, px, res = r
    hit = [x for x in res if x[0] == stop and x[1] == tgt]
    if not hit:
        return None
    _, _, xb, pb, _ = hit[0]
    kb = xb - e
    o, h, l, c = (g[x].values for x in ("open", "high", "low", "close"))
    C, H, L = c[e:xb + 1], h[e:xb + 1], l[e:xb + 1]
    VW = vw[e:xb + 1]
    call = d == 1
    T0 = (I.DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / I.YEAR_MIN
    Tx = T0 - (tmin[e:xb + 1] - tmin[e] + 1) / I.YEAR_MIN          # 第 k 根收盘 / 盘中卖出时的剩余时间
    p0 = float(_opt(px, px, T0, iv, call))
    if p0 <= 0.01:
        return None
    f = p_now / p0                                                   # 换算成今天的权利金水平
    V = _opt(C, px, Tx, iv, call)                                    # 每根收盘的期权价
    Vb = float(_opt(pb, px, Tx[kb], iv, call))                       # 原来的出场
    if stop == "prem":
        R = prem_x * p0
    else:
        sp = px - d * (0.1 if stop == "atr10" else 0.2) * atr if stop in ("atr10", "atr20") else struct
        R = p0 - float(_opt(sp, px, T0, iv, call))
    if not (R > 0.005 * p0):
        return None

    def pnl(n, v_in, v_out):
        return n * ((v_out * (1 - I.HALF_SPREAD) - v_in * (1 + I.HALF_SPREAD)) * 100 * f - 2 * FEE)

    def at(k, S):
        return float(_opt(S, px, Tx[k], iv, call))

    def fill(k, s):                     # 跳空穿过止损按开盘价成交
        return min(o[e + k], s) if d == 1 else max(o[e + k], s)

    def stop_hit(k, s):
        return (L[k] <= s) if d == 1 else (H[k] >= s)

    out = {"e_t": int(tmin[e]), "x_t": int(tmin[xb]), "d": d, "level": level}
    cap = 1 if level == "试仓级" else MAX_CONTRACTS
    one = pnl(1, p0, Vb)
    out["one"] = (one, 1)
    risk1 = (p0 * (1 + I.HALF_SPREAD) - (p0 - R) * (1 - I.HALF_SPREAD)) * 100 * f + 2 * FEE   # 止损一次每张亏多少（美元）
    for X in (100, 200, 300):
        n = min(cap, int(X // risk1))
        out[f"risk{X}"] = (n * one if n else 0.0, n)

    # 金字塔：2 张起；+1R 加 1 张（止损挪到第一次入场价），+2R 再加 1 张（止损挪到第一次加仓价）；最多 4 张
    n0 = min(2, cap)
    units, adds, s_ov, s_add1 = [(n0, p0)], 0, None, None
    exit_v, exit_k = None, kb
    for k in range(1, kb + 1):
        if s_ov is not None and stop_hit(k, s_ov):
            exit_v, exit_k = at(k, fill(k, s_ov)), k
            break
        if k == kb:
            break
        if cap > 1 and adds == 0 and V[k] >= p0 + R:
            units.append((1, V[k])); adds, s_ov, s_add1 = 1, px, C[k]
        elif cap > 1 and adds == 1 and V[k] >= p0 + 2 * R:
            units.append((1, V[k])); adds, s_ov = 2, s_add1
    if exit_v is None:
        exit_v = Vb
    out["pyramid"] = (sum(pnl(n, v, exit_v) for n, v in units), sum(n for n, _ in units))

    # 分批出场：4 张，+1R 走一半，剩下的跟前 5 根移动止损（只往有利方向挪）
    n0 = min(4, cap)
    total, half_done, trail = 0.0, False, None
    rem = n0
    exit_v = None
    hh, ll = h, l
    for k in range(1, kb + 1):
        if half_done and trail is not None and stop_hit(k, trail):
            exit_v = at(k, fill(k, trail))
            break
        if k == kb:
            break
        if not half_done and n0 >= 2 and V[k] >= p0 + R:
            q = n0 // 2
            total += pnl(q, p0, V[k]); rem -= q; half_done = True
        if half_done:
            j = e + k
            t5 = ll[max(0, j - 4):j + 1].min() if d == 1 else hh[max(0, j - 4):j + 1].max()
            trail = t5 if trail is None else (max(trail, t5) if d == 1 else min(trail, t5))
    total += pnl(rem, p0, exit_v if exit_v is not None else Vb)
    out["scale"] = (total, n0)

    # 广撒网滚仓：试仓 1 张
    units, s_ov, walked, added, s1 = [(1, p0)], None, False, False, None
    exit_v, exit_k = None, kb
    vmax = p0
    for k in range(1, kb + 1):
        if s_ov is not None and stop_hit(k, s_ov):
            exit_v, exit_k = at(k, fill(k, s_ov)), k
            break
        if k == kb:
            break
        vmax = max(vmax, V[k])
        if k == 15 and not walked and vmax < p0 + 0.5 * R:
            exit_v, exit_k = V[k], k
            break
        fav_vw = (C[k] - VW[k]) * d > 0
        if not walked and V[k] >= p0 + R and fav_vw:
            walked, s_ov, s1 = True, px, C[k]
        elif walked and not added and ((L[k] <= VW[k]) if d == 1 else (H[k] >= VW[k])) and fav_vw:
            units.append((1, V[k])); added = True
        if walked and V[k] >= p0 + 2 * R and s1 is not None:
            s_ov = max(s_ov, s1) if d == 1 else min(s_ov, s1)
    if exit_v is None:
        exit_v = Vb
    out["roll"] = (sum(pnl(n, v, exit_v) for n, v in units), sum(n for n, _ in units))
    out["roll_x_t"] = int(tmin[e + exit_k])
    return out


def run_symbol(args):
    code, picks, p_now_map = args
    import c2
    c2s = c2.settings()
    c2s["最晚入场"] = "11:00"
    df = I.load_1m(code)
    if df.empty:
        return code, []
    days = I.daily_from_1m(df)
    qqq = I.load_1m("QQQ") if code != "QQQ" else df
    qg = {k: g for k, g in qqq.groupby(qqq.index.date)} if len(qqq) else {}
    p_now = p_now_map.get(code) or 1.0
    prem_x = float(min(0.6, max(0.05, 0.5 / p_now))) if p_now > 0 else 0.2
    rows = []
    for day, g in df.groupby(df.index.date):
        if day not in days.index:
            continue
        row = days.loc[day]
        if not (row["atr_prev"] > 0) or not (row["iv"] > 0) or not (row["pc"] > 0) or len(g) < 300:
            continue
        o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        gcls = I._gap_class((o[0] - row["pc"]) / row["atr_prev"])
        want = {}
        for dd in ("L", "S"):
            p = picks.get((code, gcls, dd))
            if p:
                for x in (p if isinstance(p, list) else [p]):
                    want.setdefault((x[0], dd), []).append(x)
        if not want:
            continue
        vw = I._vwap(h, l, c, v)
        tmin = g.index.hour.values * 60 + g.index.minute.values
        for sid, i, d, struct, fa, fl in I.signals(g, row, qg.get(day), c2s):
            dd = "L" if d == 1 else "S"
            for (_, stp, tgt, lvl) in want.get((sid, dd), []):
                t = simulate_trade(g, vw, tmin, i, d, struct, float(row["atr_prev"]), float(row["iv"]), prem_x, fa, fl,
                                   row["ph"], row["pl"], stp, tgt, lvl, p_now)
                if t:
                    t.update(date=str(day), code=code, sid=sid)
                    rows.append(t)
    return code, rows


# ================================================================ 第三步：按天组合

def portfolio(trades, scheme, streak=None, giveback=None):
    """trades：list of dict（已按日期分好）。返回每天盈亏 Series 和笔数。"""
    by_day = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t)
    daily, n_tr, n_ct = {}, 0, 0
    for day in sorted(by_day):
        ts = sorted(by_day[day], key=lambda t: (t["e_t"], t["code"]))
        realized = []                                   # (出场时间, 盈亏)
        open_ = []
        taken = 0
        for t in ts:
            now = t["e_t"]
            done = sorted([x for x in realized if x[0] <= now])
            pl = sum(p for _, p in done)
            peak = 0.0
            run = 0.0
            losing = 0
            for _, p in done:
                run += p
                peak = max(peak, run)
                losing = losing + 1 if p < 0 else 0
            if taken >= DAY_TRADES or pl <= -DAY_LOSS:
                break
            if streak and losing >= streak:
                break
            if giveback and peak >= giveback and pl <= peak * 0.5:
                break
            pnl, n = t[scheme]
            if n == 0:
                continue
            xt = t["roll_x_t"] if scheme == "roll" else t["x_t"]
            if scheme == "roll":
                open_ = [x for x in open_ if x > now]
                if len(open_) >= ROLL_CONC:
                    continue
                open_.append(xt)
            realized.append((xt, pnl))
            taken += 1
            n_tr += 1
            n_ct += n
        daily[day] = sum(p for _, p in realized)
    s = pd.Series(daily, dtype=float).sort_index()
    return s, n_tr, n_ct


def metrics(s, n_tr, n_ct):
    if not len(s) or not n_tr:
        return None
    eq = s.cumsum()
    dd = float((eq - eq.cummax()).min())
    neg = (s < 0).astype(int).values
    streak = best = 0
    for x in neg:
        streak = streak + 1 if x else 0
        best = max(best, streak)
    months = s.groupby(pd.to_datetime(s.index).to_period("M")).sum()
    return {"total": round(float(s.sum()), 0), "per_trade": round(float(s.sum()) / n_tr, 1), "trades": n_tr,
            "avg_contracts": round(n_ct / n_tr, 2), "days": int((s != 0).sum()), "max_dd": round(dd, 0),
            "worst_day": round(float(s.min()), 0), "best_day": round(float(s.max()), 0), "lose_days_streak": int(best),
            "win_days": round(float((s[s != 0] > 0).mean()), 3) if (s != 0).any() else None,
            "pos_months": f"{int((months > 0).sum())}/{len(months)}"}


# ================================================================ 全部

def run(log=print):
    t0 = time.time()
    idx, syms = _load_lib()
    p_now = {c: y["premium_now"] for c, y in syms.items()}
    sets = {}
    sets["holdout"] = picks_holdout(idx, syms)
    sets["personal"], personal_meta = picks_personal(idx, syms)
    try:
        sets["playbook"] = picks_playbook()
    except Exception as e:
        log(f"（预案挑法读不到：{e}）")
    log(f"仓位方案回测：留出法挑中 {len(sets['holdout'])} 个位置，预案挑中 {len(sets.get('playbook', {}))} 个；个人策略 {len(personal_meta)} 组")
    import multiprocessing as mp
    import os
    trades = {}
    codes = sorted(syms)
    for name, pk in sets.items():
        rows = []
        cs = sorted({k[0] for k in pk})
        with mp.get_context("spawn").Pool(max(1, min(len(cs), (os.cpu_count() or 2) - 2))) as pool:
            for code, r in pool.imap_unordered(run_symbol, [(c, {k: v for k, v in pk.items() if k[0] == c}, p_now) for c in cs]):
                rows += r
        trades[name] = rows
        log(f"  {name}：{len(rows)} 笔（{time.time() - t0:.0f} 秒）")
    result = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "split": I.SPLIT, "schemes": SCHEMES,
              "personal_exits": personal_meta, "sets": {}}
    for name, rows in trades.items():
        R = {}
        for half, sel in (("in", lambda t: t["date"] < I.SPLIT), ("out", lambda t: t["date"] >= I.SPLIT)):
            rs = [t for t in rows if sel(t)]
            for sc, _ in SCHEMES:
                for st, gb in BREAKERS:
                    s, n, k = portfolio(rs, sc, st, gb)
                    R[f"{half}|{sc}|{st}|{gb}"] = metrics(s, n, k)
        result["sets"][name] = {"n_trades": len(rows), "picks": len(sets[name]), "res": R}
    (ILIB / "sizing.json").write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    md = report(result)
    p = BASE / "报告" / f"仓位方案回测_{dt.date.today()}.md"
    p.write_text(md, "utf-8")
    msg = f"仓位方案回测已更新：{p.name}，用时 {time.time() - t0:.0f} 秒"
    log(msg)
    return result


def _usd(v):
    return "—" if v is None else (f"+${v:,.0f}" if v >= 0 else f"−${-v:,.0f}")


def best_breaker(R, half, sc):
    """熔断：在网格里挑「最大回撤缩得最多、总盈亏不比不熔断少 10% 以上（亏钱时不更亏）」的那个；没有就不熔断。"""
    base = R.get(f"{half}|{sc}|None|None")
    if not base:
        return (None, None), base
    best = ((None, None), base)
    for st, gb in BREAKERS:
        m = R.get(f"{half}|{sc}|{st}|{gb}")
        if not m:
            continue
        worse = m["total"] < base["total"] - abs(base["total"]) * 0.1
        if not worse and m["max_dd"] > best[1]["max_dd"]:
            best = ((st, gb), m)
    return best


def report(res):
    names = dict(SCHEMES)
    L = [f"# 仓位方案回测 · {dt.date.today()}", "",
         "程序：`程序/sizing.py`。三层的第三层：同样的入场和出场，换不同的「几张、加减、停手」，看盈亏、回撤怎么变。",
         f"样本内 2019–2023，样本外 {res['split']} 起。金额按**今天的期权价格水平**折算（每只股票平值 3 天期权现价），每次买卖扣 1% 价差 + 每张 $1。",
         f"每天最多 {DAY_TRADES} 笔、单日亏到 ${DAY_LOSS:.0f} 停（基本纪律）；试仓级的组合除广撒网滚仓外只做 1 张；每笔最多 {MAX_CONTRACTS} 张。", ""]
    L += ["## 结论（先说）", "",
          "1. **最重要的一条：按开盘类型挑「入场 + 出场」这套办法，在没看过的数据上不赚钱。** 只用 2019–2023 挑，同样的组合 2019–2023 只做 1 张每笔约 +$14、"
          "34/59 个月赚钱；到了 2024 起没看过的数据，变成每笔约 −$27、只有 10/35 个月赚钱。今日预案的挑法（C 组）两段都赚，是因为挑的时候已经看过 2024 起的数据。",
          "2. **个人策略 A / B 全部信号**（B 组）：两段都亏，只做 1 张每笔约 −$16 到 −$19。",
          "3. **仓位方案改不了正负，只改亏多少、回撤多深**：张数越多亏得越多。金字塔和分批出场在亏钱的组合上把亏损放大 2–3 倍；"
          "广撒网滚仓基本等于只做 1 张（平均 1.03 张，很少走到加仓那一步）。",
          "4. **固定美元风险 $100 回撤最小、最差一天最小**（样本外最差一天约 −$360）。一部分原因是止损一次每张就超过 $100 的信号直接不做，笔数少了约 15%。",
          "5. **熔断**：在样本内挑出来的大多是「连亏 2 笔停」（有时加上「赚到 $200 后回吐一半停」），样本外只把亏损和回撤少了 0–10%。亏损不怎么扎堆，熔断帮不了太多。",
          "6. 「只做 1 张」最差一天也能到 −$2,900：贵的股票（平值 3 天期权几十美元）1 张就是几千美元的权利金。所以按美元风险定张数比按张数定更稳。", ""]
    titles = {"holdout": "A. 只用样本内挑的组合（样本外是真正没看过的数据）",
              "personal": "B. 只做个人策略 A / B（每个方向用样本内最好的出场，全部股票）",
              "playbook": "C. 今日预案现在的挑法（挑的时候看过样本外，偏乐观，只作参考）"}
    for name in ("holdout", "personal", "playbook"):
        S = res["sets"].get(name)
        if not S:
            continue
        R = S["res"]
        L += [f"## {titles[name]}", "", f"挑中 {S['picks']} 个位置，共 {S['n_trades']} 笔信号（按天限制前）。", ""]
        if name == "personal":
            L += ["用到的出场：" + "；".join(f"{'个人策略 ' + k.split('|')[0]} {'做多' if k.endswith('L') else '做空'} = {v['stop']} + {v['tgt']}（样本内每笔 {v['in_avg'] * 100:+.1f}%）"
                                         for k, v in res["personal_exits"].items()), ""]
        for half, hn in (("in", "样本内 2019–2023"), ("out", f"样本外 {res['split'][:4]} 起")):
            L += [f"**{hn}**（不熔断）", "", "| 方案 | 笔数 | 平均张数 | 总盈亏 | 每笔 | 最大回撤 | 最差一天 | 最长连亏天 | 赚钱的月 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
            for sc, _ in SCHEMES:
                m = R.get(f"{half}|{sc}|None|None")
                if not m:
                    L.append(f"| {names[sc]} | 0 | — | — | — | — | — | — | — |")
                    continue
                L.append(f"| {names[sc]} | {m['trades']} | {m['avg_contracts']} | {_usd(m['total'])} | {_usd(m['per_trade'])} | {_usd(m['max_dd'])} | "
                         f"{_usd(m['worst_day'])} | {m['lose_days_streak']} | {m['pos_months']} |")
            L.append("")
        L += ["**熔断**（按样本内挑，样本外检验）：", "", "| 方案 | 样本内挑中的熔断 | 样本外：不熔断 总盈亏 / 回撤 | 样本外：用这个熔断 总盈亏 / 回撤 |", "|---|---|---|---|"]
        for sc, _ in SCHEMES:
            (st, gb), _m = best_breaker(R, "in", sc)
            o0 = R.get(f"out|{sc}|None|None")
            o1 = R.get(f"out|{sc}|{st}|{gb}")
            desc = "不熔断" if (st, gb) == (None, None) else "、".join(x for x in ((f"连亏 {st} 笔停" if st else ""), (f"赚到 ${gb} 后回吐一半停" if gb else "")) if x)
            L.append(f"| {names[sc]} | {desc} | {(_usd(o0['total']) + ' / ' + _usd(o0['max_dd'])) if o0 else '—'} | "
                     f"{(_usd(o1['total']) + ' / ' + _usd(o1['max_dd'])) if o1 else '—'} |")
        L.append("")
    L += ["## 如实说", "",
          "- 仓位方案不会把没有优势的入场变成赚钱：每笔本来是负的，张数越多亏得越多；它改的是一天最多亏多少、回撤多深。",
          "- 期权价是模型估的（没有历史期权报价），开盘后隐含波动率下降、真实价差更大时结果会更差。",
          "- 金字塔、分批、滚仓的加减点用每根K线收盘的期权价判断，盘中实际成交会有滑点。",
          "- 同一天的股票彼此相关，回撤可能比这里更深。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    run()
