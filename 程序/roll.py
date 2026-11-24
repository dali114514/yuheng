# -*- coding: utf-8 -*-
"""
广撒网滚仓（原打法 C，仓位方案）的持仓监测 —— 盯盘界面「持仓」面板用

  只读 moomoo：持仓（position_list_query）、今天的成交（deal_list_query）。不下单、不撤单、不平仓。
  每个持仓给一个状态（按严重程度）：
    止损   现价 ≤ 止损价 → 立刻平仓
    接近   浮亏到 接近止损_R
    砍     试仓超过 试仓时限_分钟 还没走出来（< 0.5R）
    锁利   ≥ 锁利_R → 止损挪到 +1R
    走出来 ≥ 走出来_R 且正股在 VWAP 同侧 → 先挪保本，回踩守住再加（越加越少）
    挪保本 ≥ 走出来_R 但正股在 VWAP 另一边 → 只挪保本，不加
    试仓   其余
  止损价（每张权利金）= 成本 − min(止损_每张权利金, 成本 × 试仓止损比例)，两条取更严的。
  R = (现价 − 成本) ÷ (成本 − 止损价)。加仓后成本变了，R 跟着按新成本算。
  账户层面：今日盈亏（risk.compute 同一口径）、今天连亏几笔、同时试仓几只、名单外 / 方向相反、单日亏损上限。
  参数：设置.json → 滚仓。
"""
import datetime as dt
import json
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
ET = ZoneInfo("America/New_York")
DEFAULT = {"止损_每张权利金": 0.5, "试仓止损比例": 0.5, "试仓时限_分钟": 15, "走出来_R": 1.0, "锁利_R": 2.0,
           "接近止损_R": -0.7, "同时试仓最多": 3, "连亏停手笔数": 3}
_LOCK = threading.Lock()
_ST = {"ctx": None, "ft": None, "accs": None, "t": 0.0, "v": None, "err_t": 0.0}


def settings():
    try:
        d = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        d = {}
    s = dict(DEFAULT)
    s.update({k: v for k, v in (d.get("滚仓") or {}).items() if k in DEFAULT and v is not None})
    return s, d


def _num(x):
    try:
        v = float(x)
        return v if v == v else 0.0
    except Exception:
        return 0.0


def _ctx(cfg):
    if _ST["ctx"] is not None:
        return _ST["ft"], _ST["ctx"], _ST["accs"]
    if time.time() - _ST["err_t"] < 20:
        return None, None, "OpenD 刚才连不上，20 秒后再试"
    import journal
    con = journal.connect()
    try:
        ft, ctx, accs = journal.open_trade_ctx(cfg, lambda *a: None, con)
    finally:
        con.close()
    if ctx is None:
        _ST["err_t"] = time.time()
        return None, None, str(accs)
    _ST.update(ctx=ctx, ft=ft, accs=accs)
    return ft, ctx, accs


def _reset():
    try:
        if _ST["ctx"] is not None:
            _ST["ctx"].close()
    except Exception:
        pass
    _ST.update(ctx=None, ft=None, accs=None)


def fetch(cfg):
    """返回 (positions, deals) 或抛错。"""
    ft, ctx, accs = _ctx(cfg)
    if ctx is None:
        raise RuntimeError(accs)
    pos, deals = [], []
    for acc in accs:
        ret, d = ctx.position_list_query(trd_env=ft.TrdEnv.REAL, acc_id=int(acc), refresh_cache=True)
        if ret != ft.RET_OK:
            raise RuntimeError(f"读持仓失败：{d}")
        for _, r in d.iterrows():
            code = str(r.get("code", ""))
            if not code.startswith("US."):
                continue
            cost = _num(r.get("average_cost")) or _num(r.get("cost_price"))
            pos.append(dict(code=code, name=str(r.get("stock_name", "")), qty=_num(r.get("qty")), cost=cost,
                            now=_num(r.get("nominal_price")), pl=_num(r.get("pl_val")),
                            today_pl_val=_num(r.get("today_pl_val")), side=str(r.get("position_side", "LONG"))))
        ret, d = ctx.deal_list_query(trd_env=ft.TrdEnv.REAL, acc_id=int(acc), refresh_cache=True)
        if ret == ft.RET_OK:
            for _, r in d.iterrows():
                code = str(r.get("code", ""))
                if code.startswith("US.") and "CANCEL" not in str(r.get("status", "")).upper():
                    deals.append(dict(code=code, side=str(r["trd_side"]), qty=_num(r["qty"]), price=_num(r["price"]),
                                      order_id=str(r.get("order_id") or ""), t=str(r.get("create_time") or "")))
    return pos, deals


def _is_buy(side):
    return str(side).upper() in ("BUY", "BUY_BACK")


def rounds(deals):
    """今天的成交 → (每只现在这一轮的开仓时间 {code: 'HH:MM'}, 今天平掉的每一笔 [(平仓时间, code, 盈亏)])。
    昨天就持有的（今天先卖）算不出盈亏，跳过。"""
    import journal
    by = {}
    for d in sorted(deals, key=lambda x: x["t"]):
        by.setdefault(d["code"], []).append(d)
    start, closed = {}, []
    for code, ds in by.items():
        mult = journal.parse(code)["mult"]
        q, cash, st, broken = 0.0, 0.0, None, False
        for d in ds:
            sgn = 1 if _is_buy(d["side"]) else -1
            if q == 0:
                st, cash, broken = d["t"], 0.0, sgn < 0
            q += sgn * d["qty"]
            cash -= sgn * d["qty"] * d["price"] * mult
            if abs(q) < 1e-9:
                if not broken:
                    closed.append((d["t"], code, cash))
                q, st = 0.0, None
        if st and q > 0 and not broken:
            start[code] = st
    closed.sort()
    return start, closed


def _hm(ts):
    s = str(ts)
    return s[11:16] if len(s) >= 16 else ""


def _minutes_since(ts, now):
    try:
        t = dt.datetime.fromisoformat(str(ts)[:19]).replace(tzinfo=ET)
        return (now - t).total_seconds() / 60
    except Exception:
        return None


def vwap_side(bars, call):
    """正股相对 VWAP：1 = 在有利一侧，-1 = 不利，0 = 不知道。"""
    try:
        if bars is None or not len(bars):
            return 0, None, None
        tp = (bars.high + bars.low + bars.close) / 3
        v = bars.volume.astype(float)
        vw = float((tp * v).sum() / max(v.sum(), 1))
        px = float(bars.close.iloc[-1])
        if px == vw:
            return 0, px, vw
        return (1 if (px > vw) == call else -1), px, vw
    except Exception:
        return 0, None, None


def judge(p, s, now, start, bars_fn, plan_dirs):
    import journal
    info = journal.parse(p["code"])
    und = info["underlying"]
    is_call = info["opt_type"] != "PUT"
    direction = "多" if (info["kind"] == "期权" and is_call) or (info["kind"] == "正股" and p["qty"] > 0) else "空"
    cost, px = p["cost"], p["now"]
    qty = abs(p["qty"])
    if info["kind"] == "期权":
        risk = min(s["止损_每张权利金"], cost * s["试仓止损比例"]) if cost > 0 else 0
        stop = cost - risk
        R = (px - cost) / risk if risk > 0 else 0.0
    else:                                             # 正股：按每股 $1（≈ 平值期权 0.5）算
        risk = 1.0
        stop = cost - risk if p["qty"] > 0 else cost + risk
        R = ((px - cost) if p["qty"] > 0 else (cost - px)) / risk
    st_ts = start.get(p["code"])
    held = _minutes_since(st_ts, now) if st_ts else None
    if held is not None and held < 0:
        held = None
    vs, upx, uvw = vwap_side(bars_fn(und) if bars_fn else None, is_call if info["kind"] == "期权" else p["qty"] > 0)
    flags = []
    pd_ = plan_dirs.get(und)
    if plan_dirs:
        if pd_ is None:
            flags.append("名单外")
        elif pd_ not in ("双向", direction):
            flags.append("方向和计划相反")
    if R <= -1 + 1e-9 or (info["kind"] == "期权" and px <= stop + 1e-9):
        state, lvl, act = "止损", "stop", f"立刻平仓！权利金 {px:.2f} 已到止损 {stop:.2f}"
    elif R <= s["接近止损_R"]:
        state, lvl, act = "接近止损", "warn", f"离止损 {stop:.2f} 只差 {abs(px - stop):.2f}，到了就走，不许挪止损"
    elif held is not None and held >= s["试仓时限_分钟"] and R < 0.5:
        state, lvl, act = "砍", "warn", f"拿了 {held:.0f} 分钟还没走出来（{R:+.1f}R）：砍掉，把子弹留给走出来的"
    elif R >= s["锁利_R"]:
        lock = cost + risk
        state, lvl, act = "锁利", "good", f"{R:+.1f}R：止损挪到 +1R（权利金 {lock:.2f}），剩下的跟着走"
    elif R >= s["走出来_R"] and vs >= 0:
        state, lvl, act = "走出来", "good", f"{R:+.1f}R 且正股在 VWAP 有利一侧：先把止损挪到保本 {cost:.2f}；回踩 VWAP 守住再加，加的张数比上次少"
    elif R >= s["走出来_R"]:
        state, lvl, act = "挪保本", "info", f"{R:+.1f}R 但正股在 VWAP 另一边：止损挪到保本 {cost:.2f}，先不加"
    else:
        need = s["走出来_R"] - R
        tl = f"，还有 {s['试仓时限_分钟'] - held:.0f} 分钟" if held is not None else ""
        state, lvl, act = "试仓", "info", f"{R:+.1f}R，离「走出来」还差 {need:.1f}R{tl}；止损 {stop:.2f} 不动"
    return dict(code=p["code"], und=und, kind=info["kind"], opt=info["opt_type"], strike=info["strike"], expiry=info["expiry"],
                dir=direction, qty=qty, cost=round(cost, 3), now=round(px, 3), stop=round(stop, 3), R=round(R, 2), pl=round(p["pl"], 2),
                held=None if held is None else round(held), since=_hm(st_ts), state=state, lvl=lvl, act=act, flags=flags,
                und_px=upx, und_vwap=None if uvw is None else round(uvw, 2), vwap_ok=vs)


def plan_dirs(day=None):
    try:
        import today
        p = today.load_plan(day) or {}
        return {str(x.get("代码", "")).upper(): str(x.get("方向", "")) for x in p.get("观察名单") or [] if x.get("代码")}, p
    except Exception:
        return {}, {}


def snapshot(bars_fn=None, max_age=5):
    """给网页用：持仓状态 + 账户提醒。缓存 max_age 秒。"""
    with _LOCK:
        if _ST["v"] is not None and time.time() - _ST["t"] < max_age:
            return _ST["v"]
        s, cfg = settings()
        now = dt.datetime.now(ET)
        out = dict(ok=False, at=now.strftime("%H:%M:%S"), err="", total=None, positions=[], alerts=[], streak=0, closed=[])
        try:
            pos, deals = fetch(cfg)
        except Exception as e:
            _reset()
            out["err"] = str(e)[:200]
            _ST.update(t=time.time(), v=out)
            return out
        import risk
        res = risk.compute([dict(code=p["code"], qty=p["qty"], today_pl_val=p["today_pl_val"]) for p in pos], deals, {})
        start, closed = rounds(deals)
        streak = 0
        for _, _, pl in reversed(closed):
            if pl < 0:
                streak += 1
            else:
                break
        dirs, plan = plan_dirs()
        held = [p for p in pos if abs(p["qty"]) > 0]
        rows = [judge(p, s, now, start, bars_fn, dirs) for p in held]
        order = {"stop": 0, "warn": 1, "good": 2, "info": 3}
        rows.sort(key=lambda r: (order.get(r["lvl"], 9), r["R"]))
        al = []
        for r in rows:
            if r["lvl"] == "stop":
                al.append(dict(lvl="stop", text=f"{r['und']} {r['opt'] or ''} 到止损：{r['act']}"))
            for f in r["flags"]:
                al.append(dict(lvl="stop", text=f"{r['und']} {f}：这笔不在今天的计划里，按纪律应该平掉"))
        lim = (plan.get("限制") or {}) if plan else {}
        ml = lim.get("单日最大亏损_美元")
        if ml and res["total"] <= -float(ml):
            al.append(dict(lvl="stop", text=f"今天已亏 ${-res['total']:,.0f}，到单日上限 ${float(ml):,.0f}：停手，不再开新仓"))
        if streak >= s["连亏停手笔数"]:
            al.append(dict(lvl="stop", text=f"今天已连亏 {streak} 笔：按纪律停手，下一笔之前先在这里复盘"))
        scouts = [r for r in rows if r["R"] < s["走出来_R"]]
        if len(scouts) > s["同时试仓最多"]:
            al.append(dict(lvl="warn", text=f"同时试仓 {len(scouts)} 只，超过 {s['同时试仓最多']} 只：先砍掉最弱的"))
        for r in rows:
            if r["lvl"] == "warn":
                al.append(dict(lvl="warn", text=f"{r['und']}：{r['act']}"))
        out.update(ok=True, total=round(res["total"], 2), open_pl=round(res["open_pl"], 2), closed_pl=round(res["closed_pl"], 2),
                   n_trades=res["n_trades"], positions=rows, alerts=al, streak=streak, limit=ml,
                   closed=[dict(t=_hm(t), code=c.replace("US.", ""), pl=round(pl, 2)) for t, c, pl in closed[-8:]],
                   rules=s)
        _ST.update(t=time.time(), v=out)
        return out


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2, default=str))
