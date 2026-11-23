# -*- coding: utf-8 -*-
"""
交易日志：从 moomoo 只读拉成交 → 开平仓合并成一笔笔交易 → 和当天的纪律设置对照

  fills       moomoo 的每一笔成交（只读接口，不需要交易密码，程序不会下单）
  trades      一笔完整交易：同一张合约从开仓到仓位归零；没平完的 close_ts 为空
  daily_plan  你的盘前计划（Discord 发「计划 …」）
  meta        记住自动识别出的券商主体和账户

对照规则（标签 tag，按「今日/日期/纪律设置.json」）：
  计划内        标的在观察名单里，方向和名单一致（名单写「双向」也算）
  方向相反      标的在名单里，但方向和名单相反
  计划外        标的不在名单里
  没定计划      那天没有纪律设置
  另外 plan_note 记：过了最晚开仓时间、和当天偏向相反、纪律设置还是草稿
盈亏按成交价计算，未扣佣金和费用。
"""
import datetime as dt
import json
import re

import store

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS fills (
        deal_id TEXT PRIMARY KEY, acc_id TEXT, ts TEXT, code TEXT, name TEXT, side TEXT, qty REAL, price REAL,
        order_id TEXT, status TEXT)""",
    """CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY, date TEXT, contract TEXT, underlying TEXT, kind TEXT, opt_type TEXT, strike REAL,
        expiry TEXT, direction TEXT, open_ts TEXT, close_ts TEXT, qty REAL, avg_open REAL, avg_close REAL,
        pnl REAL, hold_min REAL, signal_id TEXT, tag TEXT, plan_note TEXT, your_reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS daily_plan (
        date TEXT PRIMARY KEY, ts TEXT, bias TEXT, max_trades INTEGER, text TEXT)""",
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
]
FIRMS = ["NONE", "FUTUAU", "FUTUINC", "FUTUSG", "FUTUSECURITIES", "FUTUCA", "FUTUMY", "FUTUJP"]
OPT = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d+)$")


def connect(path=None):
    con = store.connect(path)
    for s in SCHEMA:
        con.execute(s)
    return con


def parse(code):
    """'US.NVDA261113C200000' → dict(underlying, kind=期权/正股, opt_type, strike, expiry)。"""
    c = str(code).replace("US.", "")
    m = OPT.match(c)
    if m:
        u, ymd, cp, k = m.groups()
        return dict(underlying=u, kind="期权", opt_type="CALL" if cp == "C" else "PUT", strike=int(k) / 1000,
                    expiry=f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}", mult=100)
    return dict(underlying=c, kind="正股", opt_type=None, strike=None, expiry=None, mult=1)


# ---------------------------------------------------------------- 拉成交
def _meta(con, k, v=None):
    if v is None:
        r = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return r[0] if r else None
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))


def open_trade_ctx(cfg, log, con):
    """自动识别券商主体：依次试，直到找到有美股权限的真实账户。返回 (ft, ctx, [acc_id…]) 或 (None, None, 原因)。"""
    import stocks
    mcfg = cfg.get("moomoo") or {}
    host, port = mcfg.get("OpenD地址", "127.0.0.1"), int(mcfg.get("OpenD端口", 11111))
    if not stocks._port_open(host, port):
        return None, None, "OpenD 未连接"
    try:
        import moomoo as ft
    except Exception:
        import futu as ft
    try:
        ft.SysConfig.set_all_thread_daemon(True)
    except Exception:
        pass
    known = _meta(con, "security_firm")
    tried = []
    for firm in ([known] if known else []) + [f for f in FIRMS if f != known]:
        ctx = None
        try:
            ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.US, host=host, port=port,
                                         security_firm=getattr(ft.SecurityFirm, firm))
            ret, accs = ctx.get_acc_list()
            if ret == ft.RET_OK and len(accs):
                real = accs[accs["trd_env"].astype(str).str.upper().str.contains("REAL")]
                if len(real):
                    _meta(con, "security_firm", firm)
                    return ft, ctx, [str(x) for x in real["acc_id"].tolist()]
            tried.append(f"{firm}:{'无真实账户' if ret == ft.RET_OK else str(accs)[:40]}")
        except Exception as e:
            tried.append(f"{firm}:{str(e)[:40]}")
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass
    return None, None, "找不到有美股权限的真实账户（" + "；".join(tried[:3]) + "）"


def sync_fills(cfg, log=print, days=7):
    """把最近 days 天的成交存进 fills（数据库里还没有成交时拉 90 天）。返回 (新增笔数, 说明)。"""
    con = connect()
    try:
        if con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0:
            days = 90
        ft, ctx, accs = open_trade_ctx(cfg, log, con)
        if ctx is None:
            return 0, f"✗ 读取成交失败：{accs}"
        rows = []
        try:
            end = dt.date.today() + dt.timedelta(days=1)
            start = end - dt.timedelta(days=days + 1)
            for acc in accs:
                for how in ("history", "today"):
                    try:
                        if how == "history":
                            ret, d = ctx.history_deal_list_query(start=f"{start} 00:00:00", end=f"{end} 23:59:59",
                                                                 trd_env=ft.TrdEnv.REAL, acc_id=int(acc))
                        else:
                            ret, d = ctx.deal_list_query(trd_env=ft.TrdEnv.REAL, acc_id=int(acc), refresh_cache=True)
                    except Exception as e:
                        ret, d = None, str(e)
                    if ret != ft.RET_OK or not hasattr(d, "iterrows"):
                        if how == "history":
                            log(f"  · 账户 {acc} 历史成交读取失败：{str(d)[:80]}")
                        continue
                    for _, r in d.iterrows():
                        if not str(r.get("code", "")).startswith("US."):
                            continue
                        rows.append((str(r["deal_id"]), acc, str(r["create_time"])[:19], str(r["code"]),
                                     str(r.get("stock_name", "")), str(r["trd_side"]), float(r["qty"]), float(r["price"]),
                                     str(r.get("order_id", "")), str(r.get("status", ""))))
        finally:
            ctx.close()
        before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        with con:
            con.executemany("INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        n = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] - before
        return n, f"成交记录：读到 {len(rows)} 笔，新增 {n} 笔"
    finally:
        con.close()


# ---------------------------------------------------------------- 合并成交易
def build_trades(path=None):
    """用全部成交重建 trades 表（你写的「原因」会保留）。"""
    con = connect(path)
    try:
        reasons = dict(con.execute("SELECT trade_id, your_reason FROM trades WHERE your_reason IS NOT NULL").fetchall())
        fills = con.execute("SELECT deal_id, ts, code, side, qty, price FROM fills "
                            "WHERE status NOT LIKE '%CANCEL%' AND status NOT LIKE '%FAIL%' ORDER BY ts, deal_id").fetchall()
        open_, done, orphan = {}, [], 0
        for deal_id, ts, code, side, qty, price in fills:
            sgn = 1 if str(side).upper() in ("BUY", "BUY_BACK") else -1
            t = open_.get(code)
            if t is None and sgn < 0 and parse(code)["kind"] == "期权":
                orphan += 1          # 只做期权买方：没有对应买入的卖出 = 开仓在记录开始之前，跳过
                continue
            if t is None:
                t = open_[code] = dict(code=code, id=f"{code}|{deal_id}", open_ts=ts, pos=0.0, sgn=sgn,
                                       oq=0.0, ov=0.0, cq=0.0, cv=0.0, close_ts=None)
            if sgn == t["sgn"]:
                t["oq"] += qty
                t["ov"] += qty * price
            else:
                t["cq"] += qty
                t["cv"] += qty * price
            t["pos"] += sgn * qty
            if t["sgn"] > 0 and t["pos"] < -1e-9 and parse(code)["kind"] == "期权":
                # 卖出比记录到的买入多：多出的部分是记录开始前开的仓，只算记录到的那部分
                t["cq"] -= -t["pos"]
                t["cv"] -= -t["pos"] * price
                t["pos"] = 0.0
            if abs(t["pos"]) < 1e-9:
                t["close_ts"] = ts
                done.append(open_.pop(code))
        done += list(open_.values())
        rows = []
        for t in done:
            p = parse(t["code"])
            ao = t["ov"] / t["oq"] if t["oq"] else None
            ac = t["cv"] / t["cq"] if t["cq"] else None
            pnl = (ac - ao) * t["cq"] * p["mult"] * t["sgn"] if (ao is not None and ac is not None) else None
            long_ = t["sgn"] > 0
            if p["kind"] == "期权":
                direction = "多" if (p["opt_type"] == "CALL") == long_ else "空"
            else:
                direction = "多" if long_ else "空"
            hold = None
            if t["close_ts"]:
                hold = (dt.datetime.fromisoformat(t["close_ts"]) - dt.datetime.fromisoformat(t["open_ts"])).total_seconds() / 60
            rows.append(dict(trade_id=t["id"], date=t["open_ts"][:10], contract=t["code"].replace("US.", ""),
                             underlying=p["underlying"], kind=p["kind"], opt_type=p["opt_type"], strike=p["strike"],
                             expiry=p["expiry"], direction=direction, open_ts=t["open_ts"], close_ts=t["close_ts"],
                             qty=t["oq"], avg_open=ao, avg_close=ac, pnl=pnl, hold_min=hold,
                             your_reason=reasons.get(t["id"])))
        for r in rows:
            r.update(match(con, r))
        with con:
            con.execute("DELETE FROM trades")
            cols = ["trade_id", "date", "contract", "underlying", "kind", "opt_type", "strike", "expiry", "direction",
                    "open_ts", "close_ts", "qty", "avg_open", "avg_close", "pnl", "hold_min", "signal_id", "tag",
                    "plan_note", "your_reason"]
            con.executemany(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [[r.get(c) for c in cols] for r in rows])
        with con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('orphan_closes', ?)", (str(orphan),))
        return len(rows)
    finally:
        con.close()


def match(con, t):
    """对照当天的纪律设置 → tag / plan_note。"""
    try:
        import today
        p = today.load_plan(dt.date.fromisoformat(t["date"]))
    except Exception:
        p = None
    if not p:
        return dict(signal_id=None, tag="没定计划", plan_note=None)
    w = next((x for x in p["观察名单"] if x["代码"] == t["underlying"]), None)
    if w is None:
        tag = "计划外"
    elif w.get("方向") in ("多", "空") and w["方向"] != t["direction"]:
        tag = "方向相反"
    else:
        tag = "计划内"
    notes = []
    late = (p.get("限制") or {}).get("最晚开仓")
    if late and t.get("open_ts") and t["open_ts"][11:16] > late:
        notes.append(f"过了最晚开仓 {late}")
    if p.get("偏向") in ("偏多", "偏空") and p["偏向"][1] != t["direction"]:
        notes.append(f"和当天偏向（{p['偏向']}）相反")
    if p.get("状态") != "已锁定":
        notes.append("纪律设置还是草稿")
    return dict(signal_id=None, tag=tag, plan_note="；".join(notes) or None)


# ---------------------------------------------------------------- 盘前计划 / 原因
def save_plan(day, text, path=None):
    bias = "偏多" if re.search(r"偏多|看多|做多", text) else "偏空" if re.search(r"偏空|看空|做空", text) else \
        "中性" if re.search(r"中性|震荡|观望", text) else None
    m = re.search(r"(?:最多|不超过|上限)\s*(\d+)\s*(?:笔|单|次)", text)
    mx = int(m.group(1)) if m else None
    con = connect(path)
    try:
        with con:
            con.execute("INSERT OR REPLACE INTO daily_plan VALUES (?, ?, ?, ?, ?)",
                        (str(day), dt.datetime.now().isoformat(timespec="seconds"), bias, mx, text.strip()))
    finally:
        con.close()
    return bias, mx


def get_plan(day, path=None):
    con = connect(path)
    try:
        r = con.execute("SELECT bias, max_trades, text, ts FROM daily_plan WHERE date = ?", (str(day),)).fetchone()
        return dict(bias=r[0], max_trades=r[1], text=r[2], ts=r[3]) if r else None
    finally:
        con.close()


def set_reason(text, path=None):
    """「原因 NVDA 看到突破忍不住追了」→ 写到最近一笔该标的、还没写原因的交易上。返回说明。"""
    m = re.match(r"\s*([A-Za-z]+\d*[A-Za-z0-9]*)\s+(.+)", text.strip(), re.S)
    if not m:
        return "格式：原因 代码 你的原因（例如：原因 NVDA 看到突破忍不住追了）"
    key, why = m.group(1).upper(), m.group(2).strip()
    con = connect(path)
    try:
        r = con.execute("SELECT trade_id, contract, date FROM trades WHERE (underlying = ? OR contract = ?) "
                        "ORDER BY (your_reason IS NULL) DESC, open_ts DESC LIMIT 1", (key, key)).fetchone()
        if not r:
            return f"没找到 {key} 的交易记录（收盘复盘后才会有今天的成交）"
        with con:
            con.execute("UPDATE trades SET your_reason = ? WHERE trade_id = ?", (why, r[0]))
        return f"已记到 {r[2]} 的 {r[1]} 这笔交易上"
    finally:
        con.close()
