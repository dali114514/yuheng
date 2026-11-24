# -*- coding: utf-8 -*-
"""
玉衡5.0 · 盘前分析（控制台 /pre 页面的数据）：每只股票一块（热力图），点开看
  ① 今天会走成哪种「形态」的概率（饼图）：同一只股票、同样开盘类型的历史天数里，各形态出现的比例（天数少时往全部股票的比例收缩）；
  ② 每种形态里，回测（事后看）哪组「入场 + 止损 + 止盈」、哪种仓位方案最好，和随机入场比；
  ③ 今天按开盘类型挑的建议（今日预案，挑的时候不知道今天会走成哪种形态）。
如实说：形态要收盘后才知道；开盘类型主要改变「窄幅震荡」的比例，几乎不改变涨还是跌。概率是历史比例，不是预测。只读，不下单。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
ILIB = HERE / "data" / "日内策略库"
GAP_NAMES = {"big_down": "大幅低开", "down": "低开", "flat": "平开", "up": "高开", "big_up": "大幅高开"}
_CACHE = {"t": 0.0, "v": None}


def _gap_class(g):
    return "big_down" if g <= -0.75 else "down" if g <= -0.25 else "big_up" if g >= 0.75 else "up" if g >= 0.25 else "flat"


def _load(name):
    try:
        return json.loads((ILIB / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def _dollar_volume(codes):
    """20 日平均成交额（块的大小用）。读 K 线库日线。"""
    out = {}
    try:
        import history
        start = str(dt.date.today() - dt.timedelta(days=45))
        for c in codes:
            k = history.load(c, "1d", start=start)
            if len(k):
                k = k.tail(20)
                out[c] = float((k["close"] * k["volume"]).mean())
    except Exception:
        pass
    return out


def _market_state():
    import today
    from zoneinfo import ZoneInfo
    n = dt.datetime.now(ZoneInfo("America/New_York"))
    m = n.hour * 60 + n.minute
    if n.weekday() >= 5 or today.session_day(n) > n.date():
        return "盘后" if n.weekday() < 5 and m >= 960 else "休市"
    return "盘前" if m < 570 else ("盘中" if m < 960 else "盘后")


def build(live=True, max_age=60):
    if _CACHE["v"] is not None and time.time() - _CACHE["t"] < max_age:
        return _CACHE["v"]
    import today
    pm = _load("premarket.json")
    if not pm:
        return {"missing": True, "msg": "还没有形态统计：控制台工具「日内策略库重算」跑一次（约 2 分钟）"}
    sz = _load("sizing.json") or {}
    cfg = {}
    try:
        cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    day = today.session_day()
    pb = {}
    try:
        pj = json.loads((HERE / "data" / "策略库" / "playbook.json").read_text(encoding="utf-8"))
        if str(pj.get("day")) == str(day):
            pb = {s["code"]: s for s in (pj.get("intraday") or {}).get("stocks") or []}
    except Exception:
        pass
    plan = today.load_plan(day) or {}
    items = {w["代码"]: w for w in plan.get("观察名单") or [] if w.get("代码")}
    names = {}
    try:
        import news
        names = {w["code"]: w.get("name") or "" for w in news.research(cfg)}
    except Exception:
        pass
    quotes, qnote = {}, ""
    state = _market_state()
    if live:
        try:
            quotes, qnote = today.premarket_quotes(cfg)
        except Exception as e:
            qnote = f"读不到行情：{type(e).__name__}"
    codes = sorted(pm["stocks"])
    dv = _dollar_volume(codes)
    sch_names = dict(sz.get("schemes") or [])
    by_dt = sz.get("by_daytype") or {}
    # 仓位：没看过的数据上回撤最小的方案（按开盘挑组合的留出检验）
    best_pos = None
    try:
        R = sz["sets"]["holdout"]["res"]
        rows = [(sc, R.get(f"out|{sc}|None|None")) for sc in sch_names]
        rows = [(sc, m) for sc, m in rows if m]
        best_pos = max(rows, key=lambda x: x[1]["max_dd"])[0] if rows else None
    except Exception:
        pass
    dts = pm["daytypes"]
    tiles = []
    for c in codes:
        s = pm["stocks"][c]
        q = quotes.get(c) or {}
        prev = q.get("prev")
        if state == "盘前" or state == "休市":
            px = q.get("盘前") or q.get("夜盘") or None
        else:
            px = q.get("last") or None
        chg = (px / prev - 1) * 100 if (px and prev) else None
        atr = s.get("atr") or None
        gap = ((px - prev) / atr) if (px and prev and atr) else None
        gcls = _gap_class(gap) if gap is not None else ((pb.get(c) or {}).get("gap_now_class") or "flat")
        probs = (s["probs"].get(gcls) or {}).get("p") or {}
        n_days = (s["probs"].get(gcls) or {}).get("n")
        per_dt = []
        for d in dts:
            k = d["id"]
            pool = (pm["top_pool"].get(k) or {})
            mine = (s.get("top") or {}).get(k) or {}
            sch = (by_dt.get(k) or {}).get("schemes") or {}
            sbest = max(((sc, v) for sc, v in sch.items() if v.get("per_trade") is not None), key=lambda x: x[1]["per_trade"], default=None)
            per_dt.append(dict(id=k, name=d["name"], p=probs.get(k), long=(pool.get("L") or [None])[0], short=(pool.get("S") or [None])[0],
                               long_mine=(mine.get("L") or [None])[0], short_mine=(mine.get("S") or [None])[0],
                               rand=(pm.get("rand_pool") or {}).get(k), sizing=dict(id=sbest[0], name=sch_names.get(sbest[0], sbest[0]),
                                                                                    per_trade=sbest[1]["per_trade"], n=sbest[1]["n"]) if sbest else None,
                               sizing_all={sch_names.get(sc, sc): v.get("per_trade") for sc, v in sch.items()}))
        rec = []
        for b in (pb.get(c) or {}).get("branches") or []:
            if b.get("level") not in (None, "无") and b.get("gap") == gcls:
                rec.append(dict(dir=b["dir"], dir_name=b.get("dir_name"), name=b["name"], stop=b["stop_name"], tgt=b["tgt_name"],
                                level=b["level"], loss=b.get("loss"), out=(b.get("out") or {}).get("avg"), inn=(b.get("in") or {}).get("avg")))
        it = items.get(c)
        tiles.append(dict(code=c, name=names.get(c, ""), px=px, prev=prev, chg=None if chg is None else round(chg, 2),
                          gap=None if gap is None else round(gap, 2), gap_cls=gcls, gap_name=GAP_NAMES[gcls], size=dv.get(c) or 1.0,
                          probs=per_dt, n_days=n_days, rec=rec, in_plan=bool(it), strategy=(it or {}).get("策略")))
    res = dict(day=str(day), state=state, at=dt.datetime.now().strftime("%H:%M:%S"), qnote=qnote, tiles=tiles,
               daytypes=dts, how=pm.get("how"), best_pos=best_pos, best_pos_name=sch_names.get(best_pos, best_pos) if best_pos else None,
               lib_generated=pm.get("generated"), pool_freq=pm.get("pool_freq"))
    _CACHE.update(t=time.time(), v=res)
    return res


if __name__ == "__main__":
    r = build(live="--offline" not in sys.argv)
    print(r.get("msg") or f"{len(r['tiles'])} 只，{r['state']}，{r['qnote']}")
    for t in r["tiles"][:3]:
        print(t["code"], t["chg"], t["gap_name"], [(x["name"], x["p"]) for x in t["probs"]], t["rec"][:1])
