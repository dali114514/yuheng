# -*- coding: utf-8 -*-
"""
当日风控：今天亏了多少、做了几笔，用来执行「单日最大亏损」和「单日最多交易笔数」

  今日盈亏（美元，按成交价，未扣费用）=
    还持有的仓位：moomoo 持仓里的「今日盈亏」
    今天已经平掉的仓位：今天卖出金额 − 今天买入金额 − 昨天就持有那部分的昨收价值
  今日笔数 = 今天开仓买入的订单数（同一张合约分几次成交、同一个订单算一笔）

  设置.json → 工作流
    单日最大亏损_美元   亏到这个数，当天剩下的信号只记录、不弹窗（留空 = 不限制）
    单日最多交易笔数    做到这个数弹窗提醒「已达今日计划笔数」（留空 = 用盘前计划里写的「最多 N 笔」）
  只读：不下单、不撤单，也不会替你平仓。
"""
import datetime as dt
import json
import time

_CACHE = {"t": 0.0, "v": None}
SCHEMA = """CREATE TABLE IF NOT EXISTS risk_events (
    date TEXT, ts TEXT, kind TEXT, value REAL, limit_ REAL, note TEXT, PRIMARY KEY (date, kind))"""


def _num(x):
    try:
        v = float(x)
        return v if v == v else 0.0
    except Exception:
        return 0.0


def compute(positions, deals, prev_close):
    """纯计算，方便测试。
    positions：[{code, qty, today_pl_val}]；deals：今天的成交 [{code, side, qty, price, order_id}]；
    prev_close：{code: 昨收价}（只需要今天已平仓、昨天就持有的合约）。返回 dict。"""
    import journal
    held = {p["code"]: p for p in positions}
    by = {}
    for d in deals:
        by.setdefault(d["code"], []).append(d)
    open_pl = sum(_num(p.get("today_pl_val")) for p in positions if _num(p.get("qty")) != 0)
    closed_pl, detail, missing = 0.0, [], []
    for code, ds in by.items():
        if code in held and _num(held[code].get("qty")) != 0:
            continue                                  # 还持有：已经算在持仓的今日盈亏里
        if code in held:                              # 有的券商当天平掉的仓位还留在持仓里（数量 0）：直接用它的今日盈亏
            closed_pl += _num(held[code].get("today_pl_val"))
            detail.append((code, _num(held[code].get("today_pl_val"))))
            continue
        mult = journal.parse(code)["mult"]
        buy_q = sum(d["qty"] for d in ds if str(d["side"]).upper() in ("BUY", "BUY_BACK"))
        sell_q = sum(d["qty"] for d in ds if str(d["side"]).upper() not in ("BUY", "BUY_BACK"))
        buy_v = sum(d["qty"] * d["price"] for d in ds if str(d["side"]).upper() in ("BUY", "BUY_BACK")) * mult
        sell_v = sum(d["qty"] * d["price"] for d in ds if str(d["side"]).upper() not in ("BUY", "BUY_BACK")) * mult
        overnight = sell_q - buy_q                    # 卖得比今天买得多 = 昨天就持有的部分
        pl = sell_v - buy_v
        if overnight > 0:
            pc = prev_close.get(code)
            if pc is None:
                missing.append(code)
            else:
                pl -= overnight * pc * mult
        closed_pl += pl
        detail.append((code, pl))
    opens = {d.get("order_id") or f"{d['code']}|{i}" for i, d in enumerate(deals)
             if str(d["side"]).upper() in ("BUY",)}
    return dict(total=open_pl + closed_pl, open_pl=open_pl, closed_pl=closed_pl, n_trades=len(opens),
                detail=detail, missing=missing)


def today(cfg, max_age=120):
    """读 moomoo（只读）算今天的盈亏和笔数，缓存 2 分钟。失败返回 dict(ok=False, err=…)。"""
    if _CACHE["v"] is not None and time.time() - _CACHE["t"] < max_age:
        return _CACHE["v"]
    import journal
    out = dict(ok=False, err="", total=0.0, n_trades=0, at=dt.datetime.now().strftime("%H:%M"))
    con = journal.connect()
    try:
        ft, ctx, accs = journal.open_trade_ctx(cfg, lambda *a: None, con)
    finally:
        con.close()
    if ctx is None:
        out["err"] = str(accs)
        _CACHE.update(t=time.time(), v=out)
        return out
    try:
        positions, deals = [], []
        for acc in accs:
            ret, d = ctx.position_list_query(trd_env=ft.TrdEnv.REAL, acc_id=int(acc), refresh_cache=True)
            if ret == ft.RET_OK:
                for _, r in d.iterrows():
                    if str(r.get("code", "")).startswith("US."):
                        positions.append(dict(code=str(r["code"]), qty=_num(r.get("qty")), today_pl_val=_num(r.get("today_pl_val"))))
            ret, d = ctx.deal_list_query(trd_env=ft.TrdEnv.REAL, acc_id=int(acc), refresh_cache=True)
            if ret == ft.RET_OK:
                for _, r in d.iterrows():
                    if str(r.get("code", "")).startswith("US.") and "CANCEL" not in str(r.get("status", "")).upper():
                        deals.append(dict(code=str(r["code"]), side=str(r["trd_side"]), qty=_num(r["qty"]),
                                          price=_num(r["price"]), order_id=str(r.get("order_id") or "")))
    finally:
        ctx.close()
    prev = {}
    res = compute(positions, deals, prev)
    if res["missing"]:                                # 今天平掉的隔夜仓：查昨收价补上
        try:
            import stocks
            mm = stocks.Moomoo(cfg.get("moomoo") or {}, lambda *a, **k: None)
            if mm.ok:
                try:
                    ret, d = mm.ctx.get_market_snapshot(res["missing"])
                    if ret == mm.ft.RET_OK:
                        prev = {str(r["code"]): _num(r.get("prev_close_price")) for _, r in d.iterrows()}
                finally:
                    mm.close()
        except Exception:
            pass
        res = compute(positions, deals, prev)
    out.update(res, ok=True, err="" if not res["missing"] else f"{len(res['missing'])} 张隔夜仓查不到昨收价，没算进去")
    _CACHE.update(t=time.time(), v=out)
    return out


def limits(day=None):
    """返回 (单日最大亏损, 单日最多笔数)，没设的为 None。以当天「纪律设置.json」的限制为准。"""
    try:
        import today
        p = today.load_plan(day)
        if p:
            lim = p.get("限制") or {}
            ml = lim.get("单日最大亏损_美元")
            mt = lim.get("最多笔数")
            return (float(ml) if ml else None), (int(mt) if mt else None)
    except Exception:
        pass
    return None, None


def text(r, day=None):
    """一行文字：今日盈亏、剩余亏损额度、笔数。"""
    if not r or not r.get("ok"):
        return "今日盈亏：读不到（" + ((r or {}).get("err") or "OpenD 没连上") + "）"
    ml, mt = limits(day)
    s = f"今日盈亏 {'+' if r['total'] >= 0 else '-'}${abs(r['total']):,.0f}（{r['at']}，未扣费用）"
    if ml:
        left = ml + r["total"]
        s += f"，单日上限 ${ml:,.0f}，" + (f"还剩 ${left:,.0f}" if left > 0 else "**已到上限**")
    s += f"；今天 {r['n_trades']} 笔" + (f" / 上限 {mt} 笔" if mt else "")
    return s


def record(day, kind, value, limit_, note=""):
    """记一次「到上限」事件（每天每种只记第一次），周报统计用。"""
    try:
        import store
        con = store.connect()
        try:
            con.execute(SCHEMA)
            with con:
                con.execute("INSERT OR IGNORE INTO risk_events VALUES (?,?,?,?,?,?)",
                            (str(day), dt.datetime.now().isoformat(timespec="seconds"), kind, value, limit_, note))
        finally:
            con.close()
    except Exception:
        pass
