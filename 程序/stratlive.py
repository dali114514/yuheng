# -*- coding: utf-8 -*-
"""
玉衡5.0 · 策略提醒的核心（盘中和回放共用）：按交易者选定的「入场 + 止损 + 止盈 + 仓位方案」，
只用到现在为止已经收盘的 1 分钟K，算出：有没有入场信号、入场价 / 止损价 / 止盈价、建议的期权合约和张数、
之后有没有到止损 / 止盈 / 加仓 / 走一半 / 砍。规则和日内策略库（intraday.py）、仓位方案回测（sizing.py）是同一套。

只读，不下单。盘中由 stratwatch.py 每分钟调用并弹窗；盯盘界面（dash.py）调用它显示结论，回放任意一天也能看。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import intraday as I  # noqa: E402

DEFAULT = {"模式": "提醒", "入场提醒有效_分钟": 2,
           "说明": "策略提醒（stratwatch.py）：按纪律设置里每只股票选定的策略，盘中弹窗提醒入场和出场。模式：提醒 / 影子（只记录、不弹窗）/ 关。"}
DEFAULT_POS = {"方案": "risk100", "熔断": {"连亏停手": 2, "回吐停手_美元": 200}}
SCHEME_NAMES = {"one": "只做 1 张", "risk100": "每笔风险 $100", "risk200": "每笔风险 $200", "risk300": "每笔风险 $300",
                "pyramid": "金字塔加仓", "scale": "分批出场", "roll": "广撒网滚仓"}
DIRW = {1: "做多（买 CALL）", -1: "做空（买 PUT）"}
_ROWS = {}


def settings(cfg=None):
    if cfg is None:
        try:
            cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    s = dict(DEFAULT)
    s.update(cfg.get("策略提醒") or {})
    return s


def names():
    return ({s["id"]: s["name"] for s in I.STRATS if s["id"] != "rand"}, {s["id"]: s["name"] for s in I.STOPS},
            {s["id"]: s["name"] for s in I.TARGETS})


# ================================================================ 前一天的日线数据（ATR、昨收、昨高低、波动率）

def prev_row(code, day):
    """day 之前的日线：atr_prev（ATR14）、pc、ph、pl、iv（20 日已实现波动率 × 1.1），和日内策略库同一算法。"""
    key = (code, str(day))
    if key in _ROWS:
        return _ROWS[key]
    start = str(pd.Timestamp(day) - pd.Timedelta(days=140))[:10]
    q = ("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE code=? AND ts >= ? AND ts < ? "
         "AND substr(ts,12,5) BETWEEN '09:31' AND '16:00'")
    parts = []
    for path in (I.DB_LONG, I.DB):
        if path.exists():
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                parts.append(pd.read_sql_query(q, con, params=(code, start, str(day))))
            finally:
                con.close()
    df = pd.concat(parts) if parts else pd.DataFrame()
    if df.empty:
        return None
    df = df.drop_duplicates("ts", keep="last").sort_values("ts")
    df.index = pd.to_datetime(df["ts"])
    df = df.drop(columns="ts").astype(float)
    g = df.groupby(df.index.date)
    d = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(), "n": g["close"].size()})
    d = d[d["n"] >= 300]
    if len(d) < 20:
        return None
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    rv = np.log(d["close"]).diff().rolling(20).std() * math.sqrt(252)
    row = pd.Series({"atr_prev": float(atr.iloc[-1]), "pc": float(d["close"].iloc[-1]), "ph": float(d["high"].iloc[-1]),
                     "pl": float(d["low"].iloc[-1]), "iv": float(np.clip((rv.iloc[-1] or 0.4) * 1.1, 0.12, 2.0)),
                     "last_day": str(d.index[-1])})
    _ROWS[key] = row
    return row


# ================================================================ 今天用哪组策略

def playbook_branches(day):
    """今日预案（playbook.json）里每只股票的分支：{(代码, 开盘, L/S): branch}。只认同一天的预案。"""
    try:
        pb = json.loads((HERE / "data" / "策略库" / "playbook.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    if str(pb.get("day")) != str(day):
        return {}
    out = {}
    for s in ((pb.get("intraday") or {}).get("stocks") or []):
        for b in s.get("branches") or []:
            if b.get("level") not in (None, "无") and b.get("dir"):
                out[(s["code"], b["gap"], b["dir"])] = b
    return out


def choices(item, gap_cls, day, pb=None):
    """观察名单的一项 → {方向 d: dict(入场, 止损, 止盈, 级别, 来源)}。手选的优先，没有就用预案；方向按名单。"""
    pdir = item.get("方向") or "双向"
    dirs = [1] if pdir == "多" else [-1] if pdir == "空" else [1, -1]
    pb = playbook_branches(day) if pb is None else pb
    st = item.get("策略") or {}
    sn, tn, gn = names()
    out = {}
    for d in dirs:
        b = pb.get((item["代码"], gap_cls, "L" if d == 1 else "S"))
        if st.get("入场") in sn and st.get("止损") in tn and st.get("止盈") in gn:
            same = b and (b["sid"], b["stop"], b["tgt"]) == (st["入场"], st["止损"], st["止盈"])
            out[d] = dict(sid=st["入场"], stop=st["止损"], tgt=st["止盈"], level=(b["level"] if same else "预案外"), src="手选")
        elif b:
            out[d] = dict(sid=b["sid"], stop=b["stop"], tgt=b["tgt"], level=b["level"], src="预案")
    return out


# ================================================================ 价位和期权

def levels(o, h, l, e, i, d, struct, atr, px, prem_x, p0, delta, fa, fl, ph, pl, stop, tgt):
    """入场价 px 时的初始止损价、止盈价（股价）。和 intraday.exits 同一套口径。"""
    if stop in ("atr10", "atr20"):
        sp = px - d * (0.1 if stop == "atr10" else 0.2) * atr
    elif stop == "prem":
        sp = px - d * prem_x * p0 / max(abs(delta), 0.05)              # 权利金跌 X 大约对应的股价
    else:
        sp = struct
    r1 = (px - sp) * d
    tp = None
    if tgt == "r2":
        tp = px + d * 2 * r1 if r1 > 0 else None
    elif tgt == "liq":
        if d == 1:
            c = [x for x in (h[:e].max(), ph) if x == x and x > px]
            tp = min(c) if c else None
        else:
            c = [x for x in (l[:e].min(), pl) if x == x and x < px]
            tp = max(c) if c else None
    elif tgt.startswith("fib"):
        k0 = next(t["level"] for t in I.TARGETS if t["id"] == tgt)
        for lev in [x for x in I.FIBS if x >= k0]:
            x = fa + d * lev * fl
            if fl > 0 and (x - px) * d > 0:
                tp = x
                break
    return sp, tp


def opt_at(leg, dS):
    """期权价随股价变化 dS 的估算（Delta + Gamma）。leg 是看涨或看跌那一条（delta 带符号）。"""
    return max(0.01, leg["price"] + leg["delta"] * dS + 0.5 * leg["gamma"] * dS * dS)


def model_leg(px, iv, d, tmin_e):
    """没有实时期权时，用模型算平值、约 3 天到期的期权价、Delta、Gamma。"""
    T = (I.DTE_TRADING * 390 + 16 * 60 - tmin_e + 1) / I.YEAR_MIN
    p, dl = I.bs(px, px, T, iv, d == 1)
    h = px * 0.001
    p_up, _ = I.bs(px + h, px, T, iv, d == 1)
    p_dn, _ = I.bs(px - h, px, T, iv, d == 1)
    g = float((p_up - 2 * p + p_dn) / (h * h))
    return dict(price=float(p), delta=float(dl), gamma=g, est=True, model=True)


def contracts(scheme, loss1, cap):
    """按仓位方案算第一次进几张。loss1 = 止损一次每张约亏多少美元。"""
    if scheme in ("risk100", "risk200", "risk300"):
        X = int(scheme[4:])
        if not loss1 or loss1 <= 0:
            return 1
        return max(0, min(cap, int(X // loss1)))
    return {"pyramid": min(2, cap), "scale": min(4, cap), "roll": 1}.get(scheme, 1)


# ================================================================ 核心：到现在为止

def evaluate(code, bars, qbars, row, item, day, pos_cfg=None, cap=4, opt=None, pb=None, now_t=None):
    """bars：当天到现在已经收盘的 1 分钟K（index = 结束时间）。返回 dict：
       trades：每个方向一笔（没信号就没有），各含 state（入场 / 持仓中 / 已出场）、价位、期权、张数、events（带时间的提醒）。"""
    pos_cfg = pos_cfg or DEFAULT_POS
    scheme = pos_cfg.get("方案") or "risk100"
    out = dict(code=code, trades=[], note="", choices={}, gap=None)
    if bars is None or len(bars) < 6 or row is None:
        out["note"] = "数据不足"
        return out
    o, h, l, c, v = (bars[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
    gap = (o[0] - row["pc"]) / row["atr_prev"]
    gcls = I._gap_class(gap)
    out["gap"] = dict(cls=gcls, name=dict(I.GAPS)[gcls], val=round(float(gap), 2))
    ch = choices(item, gcls, day, pb)
    sn, tn, gn = names()
    out["choices"] = {("L" if d == 1 else "S"): dict(x, name=sn.get(x["sid"]), stop_name=tn.get(x["stop"]), tgt_name=gn.get(x["tgt"]))
                      for d, x in ch.items()}
    if not ch:
        out["note"] = f"今天「{dict(I.GAPS)[gcls]}」，预案在这个方向没挑策略、也没手选：不提醒"
        return out
    # 补一根假的「下一根」，让刚收盘的那根也能算信号（假K成交量 0，不影响 VWAP，也不会自己出信号）
    nxt = bars.index[-1] + pd.Timedelta(minutes=1)
    fake = pd.DataFrame({"open": [c[-1]], "high": [c[-1]], "low": [c[-1]], "close": [c[-1]], "volume": [0.0]}, index=[nxt])
    bx = pd.concat([bars, fake])
    import c2
    c2s = c2.settings()
    c2s["最晚入场"] = "11:00"
    atr, iv = float(row["atr_prev"]), float(row["iv"])
    sigs = I.signals(bx, row, qbars, c2s)
    n = len(bars)
    vw = I._vwap(h, l, c, v)
    tmin = bars.index.hour.values * 60 + bars.index.minute.values
    for d, x in ch.items():
        mine = [s for s in sigs if s[0] == x["sid"] and s[2] == d and s[1] < n]
        if not mine:
            continue
        sid, i, _, struct, fa, fl = mine[0]
        t_sig = f"{bars.index[i]:%H:%M}"
        e = i + 1
        live_entry = e >= n                       # 信号K刚收盘，下一根还没走完
        px = c[i] if live_entry else o[e]
        tm_e = (tmin[i] + 1) if live_entry else tmin[e]
        leg = None
        if opt:
            leg = opt.get("call" if d == 1 else "put")
        mleg = model_leg(px, iv, d, tm_e)
        use = leg or mleg
        p_model = mleg["price"]
        prem_x = float(min(0.6, max(0.05, 0.5 / p_model))) if p_model > 0 else 0.2
        sp, tp = levels(o, h, l, e, i, d, struct, atr, px, prem_x, use["price"], use["delta"], fa, fl, row["ph"], row["pl"], x["stop"], x["tgt"])
        dS_stop, dS_tgt = sp - px, (tp - px) if tp is not None else None
        o_stop = opt_at(use, dS_stop) if x["stop"] != "prem" else use["price"] * (1 - prem_x)
        o_tgt = (opt_at(use, dS_tgt) if dS_tgt is not None else None) if x["tgt"] != "r2" or x["stop"] != "prem" else use["price"] * (1 + 2 * prem_x)
        spread = ((leg["ask"] - leg["bid"]) / 2) if leg and leg.get("ask") and leg.get("bid") else use["price"] * I.HALF_SPREAD
        loss1 = round((use["price"] - o_stop + spread) * 100 + 2, 0)
        lvl_cap = 1 if x["level"] in ("试仓级", "预案外") and scheme != "roll" else cap
        n0 = contracts(scheme, loss1, lvl_cap)
        tr = dict(dir=d, dir_name=DIRW[d], sid=sid, name=sn.get(sid), stop=x["stop"], stop_name=tn.get(x["stop"]), tgt=x["tgt"],
                  tgt_name=gn.get(x["tgt"]), level=x["level"], src=x["src"], t_sig=t_sig, entry=round(px, 2), stop_px=round(sp, 2),
                  tgt_px=round(tp, 2) if tp is not None else None, opt=dict(
                      src="实时" if leg else "模型估算", expiry=(opt or {}).get("expiry"), strike=(opt or {}).get("strike"),
                      price=round(use["price"], 2), delta=round(use["delta"], 2), stop=round(o_stop, 2),
                      tgt=round(o_tgt, 2) if o_tgt is not None else None), loss1=loss1, n0=n0, scheme=scheme,
                  scheme_name=SCHEME_NAMES.get(scheme, scheme), events=[], state="入场")
        ctr = f"{(opt or {}).get('expiry') or '约 3 天到期'} {((opt or {}).get('strike') or round(px)):g} {'CALL' if d == 1 else 'PUT'}"
        if n0 == 0:
            entry_txt = (f"{code} {DIRW[d]} · {tr['name']}：信号出现，但止损一次每张约亏 ${loss1:.0f}，超过{tr['scheme_name']}——按仓位方案这笔不做")
        else:
            entry_txt = (f"{code} {DIRW[d]} · {tr['name']}（{x['level']}，{x['src']}）\n"
                         f"正股约 {px:.2f}；止损 {sp:.2f}（{tr['stop_name']}）；止盈 {tp:.2f}（{tr['tgt_name']}）\n" if tp is not None else
                         f"{code} {DIRW[d]} · {tr['name']}（{x['level']}，{x['src']}）\n"
                         f"正股约 {px:.2f}；止损 {sp:.2f}（{tr['stop_name']}）；止盈：{tr['tgt_name']}\n")
            entry_txt += (f"合约 {ctr}，{tr['opt']['src']}约 ${use['price']:.2f}（Δ {use['delta']:+.2f}）→ 期权止损约 ${o_stop:.2f}"
                          + (f"、止盈约 ${o_tgt:.2f}" if o_tgt is not None else "") + f"\n{tr['scheme_name']}：进 {n0} 张（止损一次每张约亏 ${loss1:.0f}）")
        tr["events"].append(dict(t=t_sig, kind="入场" if n0 else "不做", text=entry_txt))
        if live_entry:
            out["trades"].append(tr)
            continue
        # ---------------- 已经进场：原出场规则（止损 / 止盈 / 时间 / VWAP）走到现在了没有
        r = I.exits(bars, vw, tmin, i, d, struct, atr, iv, prem_x, fa, fl, row["ph"], row["pl"], want_hit=True)
        hit = [z for z in (r[2] if r else []) if z[0] == x["stop"] and z[1] == x["tgt"]]
        xb, pb_, hit_ = (hit[0][2], hit[0][3], hit[0][5]) if hit else (n - 1, c[-1], False)
        if n0 > 0:
            tr["events"] += manage(scheme, bars, vw, tmin, e, xb if hit_ else n - 1, d, px, sp, iv, prem_x, x["stop"], n0, lvl_cap)
        stopped_early = [ev for ev in tr["events"] if ev.get("close_all")]
        if stopped_early:
            tr["state"] = "已出场"
            tr["exit_t"] = stopped_early[0]["t"]
        elif hit_:
            tr["state"] = "已出场"
            tr["exit_t"] = f"{bars.index[xb]:%H:%M}"
            why = _exit_reason(x, d, pb_, sp, tp, tmin[xb])
            tr["events"].append(dict(t=tr["exit_t"], kind="出场", close_all=True,
                                     text=f"{code} {DIRW[d]} · {tr['name']}：{why}，正股约 {pb_:.2f} → 全部平掉"))
        else:
            tr["state"] = "持仓中"
        tr["last"] = round(float(c[-1]), 2)
        tr["R_now"] = round(float((c[-1] - px) * d / max((px - sp) * d, 1e-9)), 2) if (px - sp) * d > 0 else None
        out["trades"].append(tr)
    return out


def _exit_reason(x, d, p, sp, tp, tm):
    if tp is not None and (p - tp) * d >= -1e-6:
        return f"到止盈（{x['tgt']}）"
    if x["stop"] == "time11" and tm >= 660:
        return "11:00 时间到"
    if x["stop"] == "vwap" and (p - sp) * d > 0:
        return "收盘回到 VWAP 另一侧"
    if x["stop"] == "prem":
        return "权利金到止损 / 止盈线"
    if x["stop"] == "trail5":
        return "移动止损到了"
    return "到止损"


def manage(scheme, bars, vw, tmin, e, k_end, d, px, sp, iv, prem_x, stop, n0=1, cap=4):
    """仓位方案的盘中提醒（和 sizing.py 同一套规则；R 用模型期权价算）。返回事件列表。"""
    o, h, l, c = (bars[x].values.astype(float) for x in ("open", "high", "low", "close"))
    ev = []
    if scheme not in ("pyramid", "scale", "roll") or k_end <= e or (scheme == "pyramid" and cap < 2) or (scheme == "scale" and n0 < 2):
        return ev
    T0 = (I.DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / I.YEAR_MIN
    Tx = T0 - (tmin[e:k_end + 1] - tmin[e] + 1) / I.YEAR_MIN
    call = d == 1
    p0 = float(I.bs(px, px, T0, iv, call)[0])
    V = np.asarray(I.bs(c[e:k_end + 1], px, np.maximum(Tx, 1e-6), iv, call)[0], float)
    R = prem_x * p0 if stop == "prem" else p0 - float(I.bs(sp, px, T0, iv, call)[0])
    if not (R > 0):
        return ev
    tt = lambda k: f"{bars.index[e + k]:%H:%M}"
    hit = lambda k, s: (l[e + k] <= s) if d == 1 else (h[e + k] >= s)
    m = k_end - e
    if scheme == "pyramid":
        adds, s_ov, s1 = 0, None, None
        for k in range(1, m + 1):
            if s_ov is not None and hit(k, s_ov):
                ev.append(dict(t=tt(k), kind="出场", close_all=True, text=f"整体止损 {s_ov:.2f} 到了：全部平掉"))
                break
            if adds == 0 and V[k] >= p0 + R:
                adds, s_ov, s1 = 1, px, c[e + k]
                ev.append(dict(t=tt(k), kind="加仓", text=f"到 +1R：加 1 张；整体止损挪到入场价 {px:.2f}"))
            elif adds == 1 and V[k] >= p0 + 2 * R:
                adds, s_ov = 2, s1
                ev.append(dict(t=tt(k), kind="加仓", text=f"到 +2R：再加 1 张；整体止损挪到上次加仓价 {s1:.2f}"))
    elif scheme == "scale":
        half, trail = False, None
        for k in range(1, m + 1):
            if half and trail is not None and hit(k, trail):
                ev.append(dict(t=tt(k), kind="出场", close_all=True, text=f"移动止损 {trail:.2f} 到了：剩下的平掉"))
                break
            if not half and V[k] >= p0 + R:
                half = True
                ev.append(dict(t=tt(k), kind="走一半", text=f"到 +1R：先走 {n0 // 2} 张，剩下的跟前 5 根K线的低点（做空是高点）移动止损"))
            if half:
                j = e + k
                t5 = l[max(0, j - 4):j + 1].min() if d == 1 else h[max(0, j - 4):j + 1].max()
                trail = t5 if trail is None else (max(trail, t5) if d == 1 else min(trail, t5))
    elif scheme == "roll":
        walked, added, s_ov, s1, vmax = False, False, None, None, p0
        for k in range(1, m + 1):
            if s_ov is not None and hit(k, s_ov):
                ev.append(dict(t=tt(k), kind="出场", close_all=True, text=f"止损 {s_ov:.2f} 到了：全部平掉"))
                break
            vmax = max(vmax, V[k])
            if k == 15 and not walked and vmax < p0 + 0.5 * R:
                ev.append(dict(t=tt(k), kind="砍", close_all=True, text="试仓 15 分钟还没到 +0.5R：砍掉"))
                break
            fav = (c[e + k] - vw[e + k]) * d > 0
            if not walked and V[k] >= p0 + R and fav:
                walked, s_ov, s1 = True, px, c[e + k]
                ev.append(dict(t=tt(k), kind="走出来", text=f"走出来了（+1R，在 VWAP 有利一侧）：止损挪到保本 {px:.2f}；回踩 VWAP 守住再加"))
            elif walked and not added and ((l[e + k] <= vw[e + k]) if d == 1 else (h[e + k] >= vw[e + k])) and fav:
                added = True
                ev.append(dict(t=tt(k), kind="加仓", text=f"回踩 VWAP {vw[e + k]:.2f} 守住：加 1 张"))
            if walked and V[k] >= p0 + 2 * R and s1 is not None and s_ov != s1:
                s_ov = s1
                ev.append(dict(t=tt(k), kind="挪止损", text=f"到 +2R：止损挪到 {s1:.2f}（+1R）"))
    return ev


def breaker(pos_cfg, snap):
    """熔断（按真实成交）：返回一句话或 None。snap = roll.snapshot()。"""
    b = (pos_cfg or {}).get("熔断") or {}
    if not snap or not snap.get("ok"):
        return None
    st = b.get("连亏停手")
    if st and (snap.get("streak") or 0) >= int(st):
        return f"今天已连亏 {snap['streak']} 笔，到熔断线（连亏 {st} 笔停）：今天不再开新仓"
    gb = b.get("回吐停手_美元")
    if gb:
        run = peak = 0.0
        for x in snap.get("closed") or []:
            run += x["pl"]
            peak = max(peak, run)
        if peak >= float(gb) and run <= peak * 0.5:
            return f"今天最多赚到 ${peak:,.0f}，现在回吐到 ${run:,.0f}（一半以下）：按熔断今天停手"
    return None


def describe(tr):
    """一笔的现状，一两句话（盯盘界面 / 状态文件用）。"""
    if tr["n0"] == 0:
        return tr["events"][0]["text"]
    s = (f"{tr['dir_name']} · {tr['name']}：{tr['t_sig']} 出信号，入场约 {tr['entry']:.2f}，止损 {tr['stop_px']:.2f}"
         + (f"，止盈 {tr['tgt_px']:.2f}" if tr.get("tgt_px") is not None else "，不设止盈") + f"；{tr['scheme_name']} {tr['n0']} 张")
    if tr["state"] == "持仓中" and tr.get("R_now") is not None:
        s += f"；现在 {tr['last']:.2f}（{tr['R_now']:+.1f}R）"
    if tr["state"] == "已出场":
        s += f"；{tr.get('exit_t')} 已出场"
    return s
