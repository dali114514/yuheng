# -*- coding: utf-8 -*-
"""
玉衡5.0 · 今日预案：按《交易的本质》的三个问题，从策略库里挑「现在这种行情下」该用的策略，写成「如果……就……」。

  ① 凭什么有优势（识别机会）
     每只股票先看它现在属于哪种行情（强势上涨 / 温和上涨 / 窄幅震荡 / 宽幅震荡 / 温和下跌 / 强势下跌 / 恐慌急跌），
     再从策略库 34 条里找「在这种行情里真的赚过钱」的策略。要同时满足：
       a. 这只股票上，这种行情里：样本内（2022 年以前）和样本外（2022 年以后）都跑赢同期持有，而且两段都赚钱；
       b. 两段在这种行情里的天数都够（默认各 ≥ 40 天），在场时间够（默认 ≥ 15%）；
       c. 跨股票也成立：这条策略在这种行情里，在至少 30% 的股票上满足 a（防止只是这一只股票碰巧）。
     一条都没有 → 结论是「没有经过检验的优势，这只不做」。挑不出来是正常的，不硬挑。
  ② 值不值得冒险（风险定价）
     给出挑中策略的平均每笔、胜率、最差一笔、平均持有天数，以及同类入场在止损实验里两段都最好的止损方式。
     当天的风险上限按大盘（QQQ）的行情决定：设置.json → 预案 → 行情风险（只会比基本限制更严，不会放宽）。
  ③ 怎么兑现（执行）
     把上面的结论写成「如果 X 就 Y」：如果 NVDA 还是「恐慌急跌」→ 按某策略今天的状态做多 / 不做；
     如果大盘是「宽幅震荡」→ 每笔最多 3 张、单日亏 $450 停手……；以及执行纪律（到止损就走、不改规则）。

输出：数据/今日/<日期>/预案.md 和 预案.json；程序/data/策略库/playbook.json（策略库页读）。
只读策略库结果和设置，不下单。日线行情是按前一个交易日收盘分的，一天之内不变。

用法：arch -arm64 程序/.venv/bin/python 程序/playbook.py        （先跑过 stratlib.py）
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
LIB = HERE / "data" / "策略库"
sys.path.insert(0, str(HERE))

# 设置.json 里没有「预案」时用这些（第一次运行会写进 设置.json，之后改那边）
DEFAULTS = {
    "说明": "今日预案（playbook.py）的参数。挑策略的门槛越低，挑出来的越可能是运气；行情风险只能比纪律设置的基本限制更严。改了要在下面「修改记录」里写理由。",
    "大盘看哪只": "QQQ",
    "挑策略": {"每段最少天数": 40, "最少在场比例": 0.15, "跨股票最少通过比例": 0.3, "最少笔数": 10},
    "行情风险": {
        "强势上涨": {"每笔最多张数": 4, "最多笔数": 3, "单日最大亏损_美元": 600},
        "温和上涨": {"每笔最多张数": 4, "最多笔数": 3, "单日最大亏损_美元": 600},
        "窄幅震荡": {"每笔最多张数": 4, "最多笔数": 3, "单日最大亏损_美元": 600},
        "宽幅震荡": {"每笔最多张数": 3, "最多笔数": 3, "单日最大亏损_美元": 450},
        "温和下跌": {"每笔最多张数": 3, "最多笔数": 3, "单日最大亏损_美元": 450},
        "强势下跌": {"每笔最多张数": 3, "最多笔数": 2, "单日最大亏损_美元": 450},
        "恐慌急跌": {"每笔最多张数": 2, "最多笔数": 2, "单日最大亏损_美元": 300},
    },
    "行情风险_说明": "按波动大小缩仓：波动越大、行情越乱，张数和亏损上限越小。这是控制亏损的风险偏好，没有回测过，不是提高胜率。",
    "修改记录": [{"时间": "2026-11-23", "改了什么": "建立", "理由": "交易者要求纪律按行情变化（如果……就……），不写死"}],
}


def settings():
    p = BASE / "设置.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if "预案" not in cfg:                       # 第一次：把默认值写进 设置.json（用 json 读写，不碰其他字段）
        cfg["预案"] = DEFAULTS
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    s = cfg["预案"]
    for k, v in DEFAULTS.items():
        s.setdefault(k, v)
    return s


def _load():
    idx = json.loads((LIB / "index.json").read_text(encoding="utf-8"))
    full = {}
    for c in idx["symbols"]:
        f = LIB / f"{c}.json"
        if f.exists():
            full[c] = json.loads(f.read_text(encoding="utf-8"))
    return idx, full


def _pass(y, sid, r, need):
    """这只股票、这条策略、这种行情：两段都跑赢持有并且都赚钱。None = 数据不够判断。"""
    e, bh = y["strategies"][sid], y["strategies"]["buyhold"]
    if not e.get("regimes_in") or not e.get("regimes_out"):
        return None
    a, b, ba, bb = e["regimes_in"][r], e["regimes_out"][r], bh["regimes_in"][r], bh["regimes_out"][r]
    if a["days"] < need["每段最少天数"] or b["days"] < need["每段最少天数"] or a["ann"] is None or b["ann"] is None:
        return None
    if (a["in"] or 0) < need["最少在场比例"] or (b["in"] or 0) < need["最少在场比例"]:
        return False
    return a["ann"] > ba["ann"] and b["ann"] > bb["ann"] and a["ann"] > 0 and b["ann"] > 0


def _direction(lv):
    """策略今天的状态 → 今天的方向。pending 是今天收盘触发、下个交易日开盘执行的动作。"""
    if lv["pending"] in (1, -1):
        return "多" if lv["pending"] == 1 else "空", "今天收盘触发" + ("做多" if lv["pending"] == 1 else "做空")
    if lv["pending"] == 0 and lv["position"]:
        return None, "今天收盘触发平仓"
    if lv["position"] == 1:
        return "多", f"持有多头 {lv.get('days') or 0} 天"
    if lv["position"] == -1:
        return "空", f"持有空头 {lv.get('days') or 0} 天"
    return None, "空仓"


def _best_stop(y, family, stops_meta):
    kind = "dip" if family == "meanrev" else "breakout"
    rows = []
    for s in stops_meta:
        e = y["stops"].get(f"{kind}:{s['id']}")
        if e and e["cagr_in"] is not None and e["cagr_out"] is not None:
            rows.append((min(e["cagr_in"], e["cagr_out"]), s, e))
    if not rows:
        return None
    rows.sort(key=lambda x: -x[0])
    _, s, e = rows[0]
    base = y["stops"].get(f"{kind}:none")
    return {"kind": "低吸" if kind == "dip" else "趋势突破", "id": s["id"], "name": s["name"], "how": s["how"],
            "cagr_in": e["cagr_in"], "cagr_out": e["cagr_out"], "worst": e["worst"],
            "none_worst": base["worst"] if base else None}


def build(day=None, log=print, live=False):
    """live=True：盘前读 moomoo 实时跳空和期权 Delta（OpenD 要开着）。"""
    st = settings()
    need = st["挑策略"]
    idx, full = _load()
    regs = {r["id"]: r["name"] for r in idx["regimes"]}
    smeta = {s["id"]: s for s in idx["strategies"]}
    sids = [s for s in smeta if s != "buyhold"]

    # ---- 跨股票：每种行情里，每条策略在多少只股票上成立
    pooled = {}
    for r in regs:
        for sid in sids:
            v = [x for x in (_pass(y, sid, r, need) for y in full.values()) if x is not None]
            pooled[(r, sid)] = (sum(v), len(v))

    def pooled_ok(r, sid):
        k, n = pooled[(r, sid)]
        return n >= 5 and k / n >= need["跨股票最少通过比例"]

    # ---- 每只股票
    stocks = []
    for c, y in sorted(full.items()):
        r = y["now"]["r"]
        cands = []
        for sid in sids:
            if not _pass(y, sid, r, need):
                continue
            e, bh = y["strategies"][sid], y["strategies"]["buyhold"]
            if not e["avg"] or e["avg"] <= 0 or e["n"] < need.get("最少笔数", 10):
                continue                          # ② 风险定价：这只股票上全期平均每笔（扣成本）要为正、笔数够
            ex_in = e["regimes_in"][r]["ann"] - bh["regimes_in"][r]["ann"]
            ex_out = e["regimes_out"][r]["ann"] - bh["regimes_out"][r]["ann"]
            d, why = _direction(e["live"])
            k, n = pooled[(r, sid)]
            cands.append({"sid": sid, "name": smeta[sid]["name"], "pooled_ok": pooled_ok(r, sid), "family": smeta[sid]["family"], "dir_rule": smeta[sid]["dir"],
                          "ann_in": e["regimes_in"][r]["ann"], "ann_out": e["regimes_out"][r]["ann"],
                          "bh_in": bh["regimes_in"][r]["ann"], "bh_out": bh["regimes_out"][r]["ann"],
                          "days_in": e["regimes_in"][r]["days"], "days_out": e["regimes_out"][r]["days"],
                          "score": min(ex_in, ex_out), "pooled": f"{k}/{n}",
                          "win": e["win"], "avg": e["avg"], "worst": e["worst"], "avg_days": e["avg_days"], "n": e["n"],
                          "rand": e["rand"], "direction": d, "state": why,
                          "stop": _best_stop(y, smeta[sid]["family"], idx["stops"]), "entry": smeta[sid]["entry"],
                          "exit": smeta[sid]["exit"]})
        cands.sort(key=lambda x: (not x["pooled_ok"], -x["score"]))
        best = cands[0] if cands else None
        # 三档：跨股票也成立 → 按行情风险的正常仓位；只在这只股票上成立 → 试仓级（最多 1 张，可能是运气）；都没有 → 不做
        if best is None:
            level, action, reason = "无", "不做", "这种行情下没有一条策略在这只股票上两段都成立：没有优势就不出手"
        elif best["direction"] is None:
            level, action, reason = "无", "不做", f"最好的「{best['name']}」今天{best['state']}，日线不给方向"
        elif best["pooled_ok"]:
            level, action, reason = "检验通过", "做" + best["direction"], f"「{best['name']}」{best['state']}"
        else:
            level, action, reason = "试仓级", "试仓" + best["direction"], (f"「{best['name']}」{best['state']}；只在这只股票上成立，"
                                                                      f"跨股票只有 {best['pooled']}，可能是运气：最多 1 张")
        stocks.append({"code": c, "regime": r, "regime_name": regs[r], "since": y["now_since"], "rsi": y["now"]["rsi"],
                       "vs200": y["now"]["vs200"], "tags": y["now"]["tags"], "last_close": y["last_close"],
                       "last_date": y["last_date"], "level": level, "action": action, "reason": reason, "best": best, "others": cands[1:4]})

    # ---- 大盘行情 → 今天的风险上限
    mk = st["大盘看哪只"]
    mrow = next((s for s in stocks if s["code"] == mk), None)
    mreg = mrow["regime_name"] if mrow else None
    risk = st["行情风险"].get(mreg) if mreg else None

    # ---- 每种行情的首选（跨股票）：给「如果……就……」总表用
    table = []
    for r, name in regs.items():
        ranked = sorted(((pooled[(r, sid)][0] / pooled[(r, sid)][1], pooled[(r, sid)], sid) for sid in sids if pooled[(r, sid)][1] >= 5),
                        reverse=True)
        top = [(sid, f"{k}/{n}", p >= need["跨股票最少通过比例"]) for p, (k, n), sid in ranked if k > 0][:3]
        base = sum(p for p, _, _ in ranked) / len(ranked) if ranked else None
        table.append({"regime": r, "name": name, "base": base,
                      "top": [{"sid": s, "name": smeta[s]["name"], "pooled": f, "ok": ok} for s, f, ok in top],
                      "risk": st["行情风险"].get(name), "n_now": sum(1 for s in stocks if s["regime"] == r)})

    if day is None:
        import today
        day = today.session_day()
    res = {"day": str(day), "generated": dt.datetime.now().isoformat(timespec="seconds"), "data_date": max(s["last_date"] for s in stocks),
           "market": {"code": mk, "regime": mreg, "since": mrow["since"] if mrow else None, "risk": risk},
           "need": need, "table": table, "stocks": stocks, "risk_note": st.get("行情风险_说明", "")}
    try:
        res["intraday"] = build_intraday(st, live=live, log=log or (lambda *a: None))
    except Exception as e:
        res["intraday"] = None
        if log:
            log(f"（日内预案没生成：{type(e).__name__}: {e}）")
    (LIB / "playbook.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    try:
        import today
        today.save_text("预案.md", to_md(res), day)
        (today.folder(day) / "预案.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log(f"（写今日文件夹失败：{e}）")
    go = [s for s in stocks if s["level"] == "检验通过"]
    tr = [s for s in stocks if s["level"] == "试仓级"]
    msg = (f"今日预案（{day}）：大盘 {mk}「{mreg}」；{len(stocks)} 只里检验通过 {len(go)} 只"
           + (f"（{'、'.join(s['code'] + s['action'][1:] for s in go)}）" if go else "")
           + f"，试仓级 {len(tr)} 只" + (f"（{'、'.join(s['code'] + s['action'][2:] for s in tr)}）" if tr else "")
           + f"，写进 数据/今日/{day}/预案.md")
    if log:
        log(msg)
    return res


# ================================================================ 日内预案（intraday.py 的结果）：按开盘类型「如果……就……」

ILIB = HERE / "data" / "日内策略库"
GAP_NAMES = {"big_down": "大幅低开", "down": "低开", "flat": "平开", "up": "高开", "big_up": "大幅高开"}
GAP_ORDER = ["big_down", "down", "flat", "up", "big_up"]


def _gap_class(g):
    return "big_down" if g <= -0.75 else "down" if g <= -0.25 else "big_up" if g >= 0.75 else "up" if g >= 0.25 else "flat"


def _cell_ok(c, nin, nout):
    return bool(c and c.get("in") and c.get("out") and c["in"]["n"] >= nin and c["out"]["n"] >= nout
                and c["in"]["avg"] > 0 and c["out"]["avg"] > 0)


def live_options(codes, log=print):
    """moomoo 实时：每只股票最接近 3 天到期的平值看涨 / 看跌的价格、Delta、Gamma。OpenD 没开就返回 {}。只读。"""
    import time as _t
    try:
        import flows
        cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        mm = flows._mm(cfg)
    except Exception as e:
        log(f"（读不到实时期权：{e}）")
        return {}
    if not mm.ok:
        return {}
    out = {}
    try:
        ft, ctx = mm.ft, mm.ctx
        ret, sn = ctx.get_market_snapshot(["US." + c for c in codes])
        spots = {}
        if ret == ft.RET_OK:
            for _, r in sn.iterrows():
                c = str(r["code"]).replace("US.", "")
                px = [float(r.get(k) or 0) for k in ("pre_price", "last_price") if r.get(k) == r.get(k)]
                spots[c] = next((p for p in px if p), 0.0)
        today_ = dt.date.today()
        for c in codes:
            spot = spots.get(c)
            if not spot:
                continue
            try:
                ret, ex = ctx.get_option_expiration_date("US." + c)
                if ret != ft.RET_OK or not len(ex):
                    continue
                exps = ex["strike_time"].tolist()
                exp = min(exps, key=lambda e: (abs((dt.date.fromisoformat(e) - today_).days - 3), e))
                _t.sleep(0.5)
                ret, ch = ctx.get_option_chain("US." + c, start=exp, end=exp)
                if ret != ft.RET_OK or not len(ch):
                    continue
                k = min(ch["strike_price"].unique(), key=lambda x: abs(x - spot))
                legs = ch[ch["strike_price"] == k]
                ret, q = ctx.get_market_snapshot(legs["code"].tolist())
                if ret != ft.RET_OK:
                    continue
                o = {"spot": round(spot, 2), "expiry": exp, "strike": float(k)}
                for _, r in q.iterrows():
                    side = "call" if str(r.get("option_type")).upper().startswith("C") else "put"
                    bid, ask, last = (float(r.get(x) or 0) for x in ("bid_price", "ask_price", "last_price"))
                    mid = (bid + ask) / 2 if bid and ask else last
                    o[side] = {"price": round(mid, 2), "bid": bid, "ask": ask, "delta": float(r.get("option_delta") or 0),
                               "gamma": float(r.get("option_gamma") or 0), "iv": float(r.get("option_implied_volatility") or 0)}
                out[c] = o
                _t.sleep(2.5)                         # 期权链接口有频率限制
            except Exception:
                continue
    finally:
        mm.close()
    return out


def per_contract_loss(opt_leg, dist):
    """股价逆向 dist 美元时，一张期权大约亏多少（Delta + Gamma 近似，再加一半买卖价差）。"""
    if not opt_leg or not dist:
        return None
    d, g = abs(opt_leg["delta"]), abs(opt_leg["gamma"])
    move = max(0.0, d * dist - 0.5 * g * dist * dist)
    spread = max(0.0, (opt_leg["ask"] - opt_leg["bid"]) / 2) if opt_leg.get("ask") and opt_leg.get("bid") else 0.0
    return round((min(move, opt_leg["price"]) + spread) * 100, 0)


def build_intraday(st, live=True, log=print):
    if not (ILIB / "index.json").exists():
        return None
    need = dict({"样本内最少笔数": 12, "样本外最少笔数": 6, "跨股票最少只数": 8, "跨股票检验通过比例": 0.3, "跨股票试仓比例": 0.15},
                **(st.get("日内挑策略") or {}))
    idx = json.loads((ILIB / "index.json").read_text(encoding="utf-8"))
    pooled = {(x["sid"], x["stop"]): x["ctx"] for x in idx["combos"]}
    smeta = {s["id"]: s for s in idx["strats"]}
    stmeta = {s["id"]: s for s in idx["stops"]}
    syms = {}
    for c in idx["symbols"]:
        f = ILIB / f"{c}.json"
        if f.exists():
            syms[c] = json.loads(f.read_text(encoding="utf-8"))

    def rate(key, g):
        p = pooled.get(key, {}).get(f"gap:{g}")
        if not p or p["of"] < need["跨股票最少只数"]:
            return None, p
        return p["pass"] / p["of"], p

    # 对照组（随机入场）在每种开盘里的通过率：噪音基准
    noise = {}
    for g in GAP_ORDER:
        v = [rate(("rand", s["id"]), g)[0] for s in idx["stops"]]
        v = [x for x in v if x is not None]
        noise[g] = max(v) if v else None

    def branch(y, g, borrowed=False):
        best = None
        for key, c in y["combos"].items():
            sid, stp = key.split("|")
            if sid == "rand":
                continue
            r, p = rate((sid, stp), g)
            floor = max(need["跨股票试仓比例"], (noise.get(g) or 0) + 0.05)   # 要明显高于随机入场的通过率
            if r is None or r < floor:
                continue
            cell = c["gap"].get(g)
            if not borrowed and not _cell_ok(cell, need["样本内最少笔数"], need["样本外最少笔数"]):
                continue
            if borrowed and not (cell and cell.get("all") and cell["all"]["n"] >= 5 and cell["all"]["avg"] > 0):
                continue                               # 新股：自己的样本不够分两段，只要求自己这段不亏
            score = (r, min(cell["in"]["avg"], cell["out"]["avg"]) if not borrowed else cell["all"]["avg"])
            if best is None or score > best[0]:
                best = (score, sid, stp, cell, p, c)
        if best is None:
            return {"gap": g, "level": "无"}
        (r, _), sid, stp, cell, p, c = best
        lvl = "试仓级" if borrowed else ("检验通过" if r >= max(need["跨股票检验通过比例"], (noise.get(g) or 0) + 0.15) else "试仓级")
        return {"gap": g, "level": lvl, "sid": sid, "stop": stp, "name": smeta[sid]["name"], "stop_name": stmeta[stp]["name"],
                "stop_how": stmeta[stp]["how"], "entry": smeta[sid]["entry"], "dir_rule": smeta[sid]["dir"], "struct": smeta[sid]["struct"],
                "in": cell.get("in"), "out": cell.get("out"), "all": cell.get("all"), "pooled": f"{p['pass']}/{p['of']}",
                "pooled_rate": round(r, 3), "noise": noise.get(g), "delta_model": c.get("delta"), "struct_atr": c.get("struct_atr"),
                "bars": c.get("bars"), "borrowed": borrowed}

    stocks = []
    for code, y in sorted(syms.items()):
        borrowed = not y.get("has_in")
        br = [branch(y, g, borrowed) for g in GAP_ORDER]
        stocks.append({"code": code, "atr": y["atr_now"], "close": y["close_now"], "premium_model": y["premium_now"],
                       "first": y["first"], "days": y["days"], "borrowed": borrowed, "branches": br})

    # 盘前：实时跳空 + 实时期权（只查至少有一个可做分支的股票）
    want = [s["code"] for s in stocks if any(b["level"] != "无" for b in s["branches"])]
    gaps, opts = {}, {}
    if live and want:
        try:
            import today
            cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
            q, _ = today.premarket_quotes(cfg)
            for s in stocks:
                x = q.get(s["code"])
                if x and x.get("prev") and s["atr"]:
                    px = x.get("盘前") or x.get("夜盘") or None
                    if px:
                        gaps[s["code"]] = round((px - x["prev"]) / s["atr"], 3)
        except Exception as e:
            log(f"（盘前跳空没读到：{e}）")
        opts = live_options(want, log)
    for s in stocks:
        g = gaps.get(s["code"])
        s["gap_now"] = g
        s["gap_now_class"] = _gap_class(g) if g is not None else None
        s["option"] = opts.get(s["code"])
        for b in s["branches"]:
            if b["level"] == "无":
                continue
            dist = None
            if b["stop"] in ("atr10", "atr20"):
                dist = (0.1 if b["stop"] == "atr10" else 0.2) * s["atr"]
            elif b["stop"] != "prem" and b.get("struct_atr"):
                dist = b["struct_atr"] * s["atr"]
            b["stop_dist"] = round(dist, 2) if dist else None
            o = s["option"]
            legs = [o.get("call"), o.get("put")] if o else []
            if o and dist:
                b["loss_call"] = per_contract_loss(o.get("call"), dist)
                b["loss_put"] = per_contract_loss(o.get("put"), dist)
            if o and b["stop"] == "prem":
                prem = [x["price"] for x in legs if x]
                b["loss_call"] = b["loss_put"] = round(0.5 * 100, 0) if prem else None
            if b.get("all"):
                p = (o["call"]["price"] if o and o.get("call") else s["premium_model"])
                b["usd_per_contract"] = round(b["all"]["avg"] * p * 100, 0)
    return {"generated": idx["generated"], "split": idx["split"], "need": need, "noise": noise, "stocks": stocks,
            "live": bool(opts), "gaps_live": bool(gaps)}


def intraday_md(it, risk):
    if not it:
        return ["（日内策略库还没生成：运行 程序/intraday.py）", ""]
    L = []
    n_ok = sum(1 for s in it["stocks"] for b in s["branches"] if b["level"] == "检验通过")
    n_tr = sum(1 for s in it["stocks"] for b in s["branches"] if b["level"] == "试仓级")
    L += [f"日内策略库（1 分钟K，2019 起；样本外从 {it['split']} 起）。9:36–11:00 开仓，最晚收盘平。"
          f"按开盘类型分支：{len(it['stocks'])} 只股票 × 5 种开盘里，检验通过 {n_ok} 个、试仓级 {n_tr} 个，其余不做。", ""]
    cap = (risk or {}).get("每笔最多张数")
    for s in it["stocks"]:
        act = [b for b in s["branches"] if b["level"] != "无"]
        if not act:
            continue
        now = ""
        if s["gap_now_class"]:
            now = f"（盘前看是「{GAP_NAMES[s['gap_now_class']]}」，跳空 {s['gap_now']:+.2f} 倍 ATR）"
        opt = s.get("option")
        live = ""
        if opt and opt.get("call") and opt.get("put"):
            live = (f"；实时 {opt['expiry']} 到期 {opt['strike']:g} 平值：看涨 ${opt['call']['price']:.2f}（Δ {opt['call']['delta']:+.2f}），"
                    f"看跌 ${opt['put']['price']:.2f}（Δ {opt['put']['delta']:+.2f}）")
        L.append(f"- **{s['code']}**{now}{' · 新股，借用其他股票的检验' if s['borrowed'] else ''}{live}")
        for b in s["branches"]:
            if b["level"] == "无":
                continue
            mark = "→ 当前分支" if s["gap_now_class"] == b["gap"] else ""
            size = f"按大盘行情最多 {cap} 张" if b["level"] == "检验通过" and cap else "最多 1 张"
            loss = ""
            if b.get("loss_call") is not None or b.get("loss_put") is not None:
                loss = f"；按实时 Delta，止损一次每张约亏 看涨 ${b.get('loss_call') or '—'} / 看跌 ${b.get('loss_put') or '—'}"
            stop = (f"止损：{b['stop_name']}" + (f"（股价约 ${b['stop_dist']:.2f}）" if b.get("stop_dist") else ""))
            usd = f"，按现价期权折算每张平均 {'+' if (b.get('usd_per_contract') or 0) >= 0 else '−'}${abs(b.get('usd_per_contract') or 0):.0f}" if b.get("usd_per_contract") is not None else ""
            stat = (f"样本内 {b['in']['n']} 笔平均 {_p(b['in']['avg'])}、样本外 {b['out']['n']} 笔平均 {_p(b['out']['avg'])}（权利金收益率）"
                    if not b["borrowed"] else f"自己 {b['all']['n']} 笔平均 {_p(b['all']['avg'])}")
            L.append(f"  - 如果「{GAP_NAMES[b['gap']]}」{mark} → **{b['name']}**，{stop}，{size}（{b['level']}）。"
                     f"{stat}{usd}；跨股票 {b['pooled']} 只成立（随机入场最多 {round((b['noise'] or 0) * 100)}%）{loss}。")
        L.append("  - 其他开盘 → 不做。")
    L.append("")
    return L


def _p(v, d=1):
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def to_md(res):
    m = res["market"]
    r = m["risk"] or {}
    L = [f"# 今日预案 · {res['day']}", "",
         f"按《交易的本质》的三个问题，从玉衡策略库里按「现在的行情」挑策略。日线数据截至 {res['data_date']} 收盘，生成于 {res['generated'].replace('T', ' ')}。",
         "主线是日内策略库（你的打法：1 分钟K、9:36–11:00 开仓、期权）：按今天的开盘类型走哪个分支；日线只当背景和风险上限。", ""]
    go = [s for s in res["stocks"] if s["level"] == "检验通过"]
    tr = [s for s in res["stocks"] if s["level"] == "试仓级"]
    no = [s for s in res["stocks"] if s["level"] == "无"]

    def why(s):
        b = s["best"]
        return (f"这种行情里样本内年化 {_p(b['ann_in'])}（持有 {_p(b['bh_in'])}）、样本外 {_p(b['ann_out'])}（持有 {_p(b['bh_out'])}），"
                f"跨股票 {b['pooled']} 只成立" + (f"；止损参考「{b['stop']['name']}」" if b.get("stop") else ""))

    # ③ 先给结论：如果……就……
    L += ["## 今天的「如果……就……」", ""]
    if m["regime"]:
        L.append(f"- **如果大盘（{m['code']}）是「{m['regime']}」**（从 {m['since']} 起）→ 每笔最多 **{r.get('每笔最多张数', '—')} 张**、"
                 f"最多 **{r.get('最多笔数', '—')} 笔**、单日亏 **${r.get('单日最大亏损_美元', '—')}** 停手。")
    L += ["", "### 日内：按开盘类型（主线）", ""] + intraday_md(res.get("intraday"), r)
    L += ["### 日线背景（拿几天到几周的规则，只当方向参考）", ""]
    for s in go:
        L.append(f"- **如果 {s['code']} 还是「{s['regime_name']}」** → {s['action']}，按上面的仓位（{s['reason']}）。{why(s)}。")
    for s in tr:
        L.append(f"- **如果 {s['code']} 还是「{s['regime_name']}」** → {s['action']}，**最多 1 张**（{s['reason']}）。{why(s)}。")
    if no:
        L.append(f"- **其余 {len(no)} 只** → 不做（{'、'.join(s['code'] for s in no)}）：这种行情下没有在它身上两段都成立的策略，或者成立的策略今天空仓。")
    L += ["", "### 任何时候", "",
          "- 到这条策略定的止损就走（止损跟着策略，没有单笔美元上限；入场就定好，之后不往外挪）；不在盘中改规则；亏损平仓后先停一停，不翻本。",
          "- **如果明天行情变了** → 收盘复盘后预案自动按新的行情重挑。", ""]
    if not go:
        L += ["> 今天没有一只股票有「跨股票也成立」的策略。按《交易的本质》第一个问题：信号不等于优势，没找到优势时，不出手本身就是正确的决定。", ""]

    # ① 凭什么有优势
    L += ["## ① 凭什么有优势：每只股票现在的行情和挑中的策略", "",
          f"在这只股票、这种行情里：样本内和样本外都跑赢持有并且都赚钱（两段各至少 {res['need']['每段最少天数']} 天、在场 ≥ "
          f"{res['need']['最少在场比例'] * 100:.0f}%）才算候选。候选里，这种行情下在至少 {res['need']['跨股票最少通过比例'] * 100:.0f}% "
          "的股票上也成立的 = 检验通过；只在这一只上成立的 = 试仓级。另外这条策略在这只股票上全期平均每笔（扣成本）要为正、"
          f"至少 {res['need'].get('最少笔数', 10)} 笔。", "",
          "| 股票 | 现在的行情 | 结论 | 最好的策略 | 这种行情里 样本内 / 样本外（持有） | 跨股票 | 今天状态 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for s in res["stocks"]:
        b = s["best"]
        if b:
            L.append(f"| {s['code']} | {s['regime_name']}（{s['since'][5:]} 起） | {s['action']} | {b['name']} | "
                     f"{_p(b['ann_in'], 0)} / {_p(b['ann_out'], 0)}（{_p(b['bh_in'], 0)} / {_p(b['bh_out'], 0)}） | {b['pooled']} | {b['state']} |")
        else:
            L.append(f"| {s['code']} | {s['regime_name']}（{s['since'][5:]} 起） | 不做 | — | — | — | — |")

    # ② 风险定价
    L += ["", "## ② 值不值得冒险：挑中策略的成本和代价", ""]
    for s in go + tr:
        b = s["best"]
        st = b["stop"]
        win = "—" if b["win"] is None else f"{b['win'] * 100:.0f}%"
        rnd = "—" if b["rand"] is None else f"{b['rand'] * 100:.0f}%"
        L.append(f"- **{s['code']} · {b['name']}**（{s['level']}）：全期 {b['n']} 笔，胜率 {win}，平均每笔 {_p(b['avg'])}，"
                 f"最差一笔 {_p(b['worst'])}，平均持有 {b['avg_days']} 天，择时分位 {rnd}。入场：{b['entry']} 出场：{b['exit']}"
                 + (f" 止损参考（同类入场的止损实验里两段都最好）：{st['name']}——{st['how']}" if st else ""))
    if not (go or tr):
        L.append("- 今天没有挑中的策略，不需要定价：最好的风险管理就是不出手。")
    L += ["", f"风险上限按大盘行情（第一条）。{res['risk_note']}", "",
          "## 每种行情里，跨股票最常成立的策略", "",
          "「平均」是 33 条策略的平均通过率，可以当成噪音的基准：最好的那条如果只比平均高一点，就说明这种行情下没有稳定的策略。", "",
          "| 行情 | 现在有几只 | 最常成立的（通过的股票数） | 平均 | 风险上限（张 / 笔 / 单日亏损） |", "| --- | --- | --- | --- | --- |"]
    for t in res["table"]:
        rr = t["risk"] or {}
        tops = "、".join(x["name"] + "（" + x["pooled"] + ("，过门槛" if x["ok"] else "") + "）" for x in t["top"]) or "没有"
        base = "—" if t["base"] is None else f"{t['base'] * 100:.0f}%"
        L.append(f"| {t['name']} | {t['n_now']} | {tops} | {base} | {rr.get('每笔最多张数', '—')} / {rr.get('最多笔数', '—')} / ${rr.get('单日最大亏损_美元', '—')} |")
    L += ["", "## 如实说", "",
          "- 34 条策略 × 7 种行情 × 30 多只股票，组合很多；就算全是噪音，也会有一些碰巧两段都好。试仓级就是这种情况的高发区。",
          "- 这些是日线规则（收盘出信号、次日开盘成交），你日内 1 分钟做期权：日线只决定方向和风险，不决定入场点。",
          "- 行情分类描述的是刚刚发生了什么，不是预测。",
          "- 要判断这套预案有没有用，记 40 个交易日以上：按预案做的单和不按预案做的单分开算。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    build()
