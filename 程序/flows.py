# -*- coding: utf-8 -*-
"""
期权墙（盘前）和资金流向（盘中），都从 moomoo 读，只读不下单

  期权墙：最近 7 天内到期的期权，现价上下 10% 的行权价
    · 看涨墙 = 现价上方未平仓量最多的看涨行权价；看跌墙 = 现价下方未平仓量最多的看跌行权价
    · 净 gamma（GEX）：看涨 +、看跌 −，gamma × 未平仓量 × 100 × 现价² × 1%，单位「美元 / 每涨 1%」
      这是常见的估算口径，假设做市商持有看涨的空头对冲方向和看跌相反；真实持仓谁也看不到
    · 未平仓量是前一个交易日收盘后的数字，盘中不更新
  资金流向：moomoo 自己按成交单大小分的「流入 / 流出」，不是真的知道谁在买；没有历史数据可以回测，只能看个大概
"""
import datetime as dt
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent


def _mm(cfg):
    import stocks
    return stocks.Moomoo((cfg or {}).get("moomoo") or {}, lambda *a, **k: None)


def option_chain(mm, code, spot, days=7, band=0.10):
    """最近 days 天到期、现价上下 band 的期权代码（一天抓一次就够：期权链接口限制最严）。返回 (代码列表, 到期日列表) 或 None。"""
    ft, ctx = mm.ft, mm.ctx
    ret, ex = ctx.get_option_expiration_date("US." + code)
    if ret != ft.RET_OK or not len(ex):
        return None
    today = dt.date.today()
    exps = [e for e in ex["strike_time"].tolist() if (dt.date.fromisoformat(e) - today).days <= days] or ex["strike_time"].tolist()[:1]
    time.sleep(0.6)
    ret, ch = ctx.get_option_chain("US." + code, start=exps[0], end=exps[-1])
    if ret != ft.RET_OK or not len(ch):
        return None
    ch = ch[(ch.strike_price >= spot * (1 - band)) & (ch.strike_price <= spot * (1 + band))]
    return ch["code"].tolist(), exps


def walls_from(mm, codes, spot):
    """用期权快照（moomoo 实时算的 gamma、delta；未平仓量是昨天收盘的）算墙、净 gamma 和 delta 对比。"""
    import pandas as pd
    ft, ctx = mm.ft, mm.ctx
    rows = []
    for i in range(0, len(codes), 300):
        ret, sn = ctx.get_market_snapshot(codes[i:i + 300])
        if ret == ft.RET_OK:
            rows.append(sn)
        time.sleep(0.4)
    if not rows:
        return None
    sn = pd.concat(rows)
    if "volume" not in sn.columns:
        sn["volume"] = 0.0
    sn = sn[["code", "option_type", "option_strike_price", "option_open_interest", "option_gamma", "option_delta", "volume"]]
    sn = sn.dropna(subset=["option_strike_price", "option_open_interest", "option_gamma", "option_delta"])
    sn["vol"] = sn["volume"].fillna(0).astype(float)
    sn["oi"] = sn["option_open_interest"].astype(float)
    sn["k"] = sn["option_strike_price"].astype(float)
    sn["g"] = sn["option_gamma"].astype(float)
    sn["dl"] = sn["option_delta"].astype(float)
    sn["call"] = sn["option_type"].astype(str).str.upper().str.contains("CALL")
    sn["gex"] = sn["g"] * sn["oi"] * 100 * spot * spot * 0.01 * sn["call"].map({True: 1, False: -1})
    calls, puts = sn[sn.call], sn[~sn.call]
    c_by = calls.groupby("k").oi.sum()
    p_by = puts.groupby("k").oi.sum()
    top_c = c_by[c_by.index >= spot].sort_values(ascending=False).head(3)
    top_p = p_by[p_by.index <= spot].sort_values(ascending=False).head(3)
    g_by = sn.groupby("k").gex.sum()
    cd = float((calls.dl.abs() * calls.oi).sum() * 100 * spot)
    pd_ = float((puts.dl.abs() * puts.oi).sum() * 100 * spot)
    prof = []                                          # 每个行权价：看涨 / 看跌的未平仓量、今日成交量、净 gamma（画图用）
    for k, g in sn.groupby("k"):
        c_, p_ = g[g.call], g[~g.call]
        prof.append([float(k), int(c_.oi.sum()), int(p_.oi.sum()), int(c_.vol.sum()), int(p_.vol.sum()), round(float(g.gex.sum()), 0)])
    vc, vp = calls.groupby("k").vol.sum(), puts.groupby("k").vol.sum()
    return dict(profile=prof, vol_call_top=float(vc.idxmax()) if len(vc) and vc.max() > 0 else None,
                vol_put_top=float(vp.idxmax()) if len(vp) and vp.max() > 0 else None,
                spot=spot, call_wall=float(top_c.index[0]) if len(top_c) else None,
                put_wall=float(top_p.index[0]) if len(top_p) else None,
                calls=[(float(k), int(v)) for k, v in top_c.items()], puts=[(float(k), int(v)) for k, v in top_p.items()],
                gex=float(sn.gex.sum()), pcr=float(puts.oi.sum() / max(calls.oi.sum(), 1)),
                call_delta=cd, put_delta=pd_, delta_ratio=cd / pd_ if pd_ > 0 else None,
                gex_top=[(float(k), float(v)) for k, v in g_by.reindex(g_by.abs().sort_values(ascending=False).index).head(3).items()])


def option_walls(mm, code, spot, days=7, band=0.10):
    """返回 dict 或 None（没有期权 / 读不到）。"""
    ch = option_chain(mm, code, spot, days, band)
    if not ch:
        return None
    w = walls_from(mm, ch[0], spot)
    if w:
        w.update(code=code, exps=ch[1])
    return w


def _money(x):
    a = abs(x)
    s = f"{a / 1e8:.2f} 亿" if a >= 1e8 else f"{a / 1e4:.0f} 万"
    return ("-" if x < 0 else "+") + "$" + s


def walls_md(cfg, codes, spots, log=None):
    """期权墙这一节（markdown）。spots：{代码: 最近价格}。"""
    mm = _mm(cfg)
    if not mm.ok:
        return "## 期权墙\n\nOpenD 没连上，读不到期权数据。\n"
    L = ["## 期权墙（最近 7 天到期、现价上下 10%）", "",
         "未平仓量是上一个交易日收盘后的数字。墙 = 未平仓量最多的行权价，价格靠近时常常有反应（停一停、来回），但**不是一定挡得住**；"
         "净 gamma 为正时做市商对冲倾向「高抛低吸」，行情偏震荡；为负时倾向「追涨杀跌」，行情容易走单边。"
         "这些是常见的估算口径，没有用你的数据回测过，只当盘前看位置用。", "",
         "| 代码 | 现价 | 看涨墙（上方） | 看跌墙（下方） | 看跌/看涨 未平仓比 | 净 gamma（每 1%） | 倾向 | 到期日 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    detail = []
    try:
        for c in codes:
            sp = spots.get(c)
            if not sp:
                continue
            try:
                w = option_walls(mm, c, sp)
            except Exception as e:
                w = None
                if log:
                    log(f"  · {c} 期权墙出错：{type(e).__name__}")
            if not w:
                L.append(f"| {c} | {sp:.2f} | 读不到 | | | | | |")
                continue
            lean = "偏震荡（正 gamma）" if w["gex"] > 0 else "易走单边（负 gamma）"
            L.append(f"| {c} | {sp:.2f} | {w['call_wall'] or '—'} | {w['put_wall'] or '—'} | {w['pcr']:.2f} | "
                     f"{_money(w['gex'])} | {lean} | {w['exps'][0][5:]}" + (f"–{w['exps'][-1][5:]}" if len(w['exps']) > 1 else "") + " |")
            detail.append(f"- {c}：看涨未平仓最多 " + "、".join(f"{k:g}（{v:,}）" for k, v in w["calls"])
                          + "；看跌最多 " + "、".join(f"{k:g}（{v:,}）" for k, v in w["puts"]))
            time.sleep(0.5)
    finally:
        try:
            mm.close()
        except Exception:
            pass
    return "\n".join(L + [""] + detail) + "\n"


def write_walls(cfg, day, codes, log=None):
    """盘前：写 今日/<日期>/期权墙.md，返回 markdown。现价用盘前 / 最近收盘。"""
    import today
    q, _ = today.premarket_quotes(cfg)
    spots = {}
    for c in codes:
        x = q.get(c) or {}
        spots[c] = x.get("盘前") or x.get("夜盘") or x.get("last") or x.get("prev")
    if not any(spots.values()):
        import c2
        spots = {c: c2.prev_close(c, str(day)) for c in codes}
    md = walls_md(cfg, codes, spots, log)
    today.save_text("期权墙.md", f"# 期权墙（{day}，美东 {dt.datetime.now(today.ET):%H:%M} 抓取）\n\n" + md, day)
    return md


# ================================================================ 资金流向（盘中）
def capital(mm, code):
    """{net: 今天累计净流入, d15: 最近 15 分钟净流入变化, big: 特大单 + 大单净流入} 或 None。"""
    ft, ctx = mm.ft, mm.ctx
    out = {}
    try:
        ret, d = ctx.get_capital_flow("US." + code)
        if ret == ft.RET_OK and len(d):
            d = d[d["capital_flow_item_time"].astype(str).str[:10] == str(d["capital_flow_item_time"].iloc[-1])[:10]]
            f = d["in_flow"].astype(float).values
            out["net"] = float(f[-1])
            out["d15"] = float(f[-1] - f[-16]) if len(f) > 15 else None
            out["t"] = str(d["capital_flow_item_time"].iloc[-1])[11:16]
    except Exception:
        pass
    try:
        ret, d = ctx.get_capital_distribution("US." + code)
        if ret == ft.RET_OK and len(d):
            r = d.iloc[-1]
            g = lambda k: float(r.get(k) or 0)
            out["big"] = g("capital_in_super") + g("capital_in_big") - g("capital_out_super") - g("capital_out_big")
            out["small"] = g("capital_in_small") + g("capital_in_mid") - g("capital_out_small") - g("capital_out_mid")
    except Exception:
        pass
    return out or None


def capital_md(mm, codes, out=None):
    L = ["## 资金流向（moomoo 口径）", "",
         "moomoo 按成交单大小分的流入 / 流出，不是真的知道谁在买；没有历史可以回测，只当参考，**不单独作为进场理由**。", "",
         "| 代码 | 今日净流入 | 最近 15 分钟 | 大单净额 | 中小单净额 |", "| --- | --- | --- | --- | --- |"]
    for c in codes:
        x = capital(mm, c)
        if out is not None:
            out[c] = x
        if not x:
            L.append(f"| {c} | 读不到 | | | |")
            continue
        f = lambda k: _money(x[k]) if x.get(k) is not None else "—"
        L.append(f"| {c} | {f('net')} | {f('d15')} | {f('big')} | {f('small')} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, str(HERE))
    cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    codes = [x.upper() for x in sys.argv[1:]] or ["QQQ", "NVDA"]
    import today
    q, _ = today.premarket_quotes(cfg)                 # 只显示，不覆盖今天的 期权墙.md
    spots = {c: (q.get(c) or {}).get("盘前") or (q.get(c) or {}).get("夜盘") or (q.get(c) or {}).get("last") for c in codes}
    if not all(spots.values()):
        mm = _mm(cfg)
        ret, sn = mm.ctx.get_market_snapshot(["US." + c for c in codes])
        if ret == mm.ft.RET_OK:
            for _, r in sn.iterrows():
                spots[str(r["code"]).replace("US.", "")] = float(r["last_price"])
        mm.close()
    print(walls_md(cfg, codes, spots, log=print))
    mm = _mm(cfg)
    print(capital_md(mm, codes))
    mm.close()
