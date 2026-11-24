# -*- coding: utf-8 -*-
"""
盯盘界面（网页 http://127.0.0.1:8767）：左边今天盯的股票（最多 10 只），右边选中那只的 1 分钟K、
「现在像什么」（形态 + 历史概率 + 理由 + 纪律五条核对）、可选的「相似的历史日子」

  行情：盘中用 moomoo 订阅的实时 1 分钟K（只读）；不在交易时段时回放 history.db 里最近一个交易日，也可以指定日期和时间回放
  形态和概率都来自 patterns.py（和早盘监测同一份代码），相似日来自 engine.py
  盯的名单存在 数据/今日/<日期>/盯盘.json；没有时用今天纪律设置的观察名单
  只读，不下单。没有弹窗。
"""
import datetime as dt
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import c2        # noqa: E402
import patterns  # noqa: E402
import ladder    # noqa: E402

PORT = 8767
VERSION = int(Path(__file__).stat().st_mtime)
ET = ZoneInfo("America/New_York")
HOT = BASE / "数据" / "热数据"
REPLAY_DIR = HERE / "data" / "盯盘回放图"            # 回放时的相似日图放这里，不碰热数据
MAXN = 10

# 我们识别的形态 ≈ 交易者那张「92 个形态」图里的哪几个（只是对照名字，不代表那张图说的「高胜率」）
CHEAT = {
    "锤子线": "图里的 Pin Bar（拒绝）", "流星线": "图里的 Pin Bar（拒绝）",
    "看涨吞没": "图里的 Engulfing", "看跌吞没": "图里的 Engulfing",
    "回调后越过前高": "#89 Retest & Continuation / #82 BOS", "反弹后跌破前低": "#89 Retest & Continuation / #82 BOS",
    "大阳线": "#48 Displacement", "大阴线": "#48 Displacement",
    "内包线向上突破": "图里的 Inside Bar / #25 Inside Range", "内包线向下突破": "图里的 Inside Bar / #25 Inside Range",
    "三连阳": "#73 Stepping Highs", "三连阴": "#74 Stepping Lows",
    "十字星（涨后）": "Doji（图里没有单列）", "十字星（跌后）": "Doji（图里没有单列）",
    "双顶跌破颈线": "#59 Double Top", "双底升破颈线": "#60 Double Bottom",
    "震荡向上突破": "#19 Compression / #69 Rectangle", "震荡向下突破": "#19 Compression / #69 Rectangle",
    "向上假突破": "#13 Fakeout / #40 SFP / #39 Liquidity Sweep", "向下假突破": "#13 Fakeout / #40 SFP / #39 Liquidity Sweep",
}


def cfg_all():
    try:
        return json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def et_now():
    return dt.datetime.now(ET)


def market_open(n=None):
    n = n or et_now()
    m = n.hour * 60 + n.minute
    return n.weekday() < 5 and 570 <= m <= 961


# ================================================================ 名单
def watch_file(day):
    import today
    return today.folder(day) / "盯盘.json"


def get_watch(day):
    p = watch_file(day)
    try:
        lst = json.loads(p.read_text(encoding="utf-8")).get("代码") or []
    except Exception:
        lst = []
    if not lst:
        import today
        lst = today.watch_codes(day) or []
    return [c for c in lst if c][:MAXN]


def set_watch(day, codes):
    out = []
    for c in codes:
        c = str(c).strip().upper().lstrip("$")
        if c and len(c) <= 10 and all(ch.isalnum() or ch == "." for ch in c) and c not in out:
            out.append(c)
    out = out[:MAXN]
    p = watch_file(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"代码": out, "更新": et_now().strftime("%H:%M")}, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ================================================================ 行情
class Live:
    """盘中：moomoo 订阅 1 分钟K（只读）。每只 8 秒内重复请求用缓存。"""

    def __init__(self):
        self.feed, self.codes, self.cache, self.lock = None, set(), {}, threading.Lock()

    def bars(self, code):
        with self.lock:
            hit = self.cache.get(code)
            if hit and time.time() - hit[0] < 8:
                return hit[1]
            if self.feed is None or not self.feed.ok or code not in self.codes:
                import c2watch
                want = set(self.codes) | {code, "QQQ"}
                if self.feed is not None:
                    self.feed.close()
                self.feed = c2watch.Feed(cfg_all(), sorted(want))
                self.codes = want
            if not self.feed.ok:
                return None
            b = self.feed.bars(code, et_now())
            self.cache[code] = (time.time(), b)
            return b


LIVE = Live()


def _hist_day(code, day):
    import history
    b = history.load(code, "1m", start=f"{day} 09:30:00", end=f"{day} 16:00:00")
    return c2.session(b)


def last_hist_day():
    import sqlite3
    con = sqlite3.connect(f"file:{HERE / 'data' / 'history.db'}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT substr(ts,1,10) d FROM bars_1m WHERE code='QQQ' AND ts LIKE '% 15:59:00' ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        con.close()
    return r[0] if r else None


def mode_of(q):
    """返回 (day 字符串, 截止时间 'HH:MM' 或 None, 说明)。"""
    d, t = (q.get("date") or [""])[0], (q.get("time") or [""])[0]
    if d:
        return d, (t or None), f"回放 {d}" + (f" 到 {t}" if t else "")
    if market_open():
        return str(et_now().date()), None, "实时"
    ld = last_hist_day()
    import today
    return ld, (t or None), (f"现在不在交易时段，K 线是最近一个交易日 {ld}" + (f" 到 {t}" if t else "")
                             + f"；纪律和名单是今天（{today.session_day()}）的")


def get_bars(code, day, cut, live):
    b = LIVE.bars(code) if live else _hist_day(code, day)
    if b is None or not len(b):
        return None
    if cut:
        h, m = map(int, cut.split(":"))
        b = b[b.index <= pd.Timestamp(day) + pd.Timedelta(minutes=h * 60 + m)]
    return b


_REF = {}


def refs(code, day):
    """前一天 ATR、昨收、昨高、昨低（每天每只算一次）。"""
    k = (code, day)
    if k not in _REF:
        import history
        d1 = history.load(code, "1d", end=f"{day} 00:00:00")
        d1 = d1[d1.index.normalize() < pd.Timestamp(day)]
        last = d1.iloc[-1] if len(d1) else None
        _REF[k] = dict(atr=c2.daily_atr(code, day), pc=float(last.close) if last is not None else None,
                       ph=float(last.high) if last is not None else None, pl=float(last.low) if last is not None else None)
    return _REF[k]


def plan_day(q, day):
    """开盘前自动回放昨天时，纪律和名单用今天的（交易者要看今天的计划）；手动选了回放日期就用那天的。"""
    if not (q.get("date") or [""])[0] and not market_open():
        import today
        return str(today.session_day())
    return day


def plan_for(day):
    import today
    try:
        p = today.load_plan(dt.date.fromisoformat(day)) or {}
    except Exception:
        p = {}
    return p, {w["代码"]: w for w in p.get("观察名单") or []}


# ================================================================ 分析
def analyze(code, b, day, qb, plan, items, recent=15):
    """最近 recent 根K里出现的形态（每根都只用它自己之前的K线），加上现在的状态。"""
    r = refs(code, day)
    a = r["atr"]
    if b is None or not len(b) or not a:
        return dict(err="数据不足（没有 K 线或日线 ATR）")
    it = items.get(code)
    pdir = (it or {}).get("方向") if plan else "双向"
    st = c2.scan(b, a, cfg=cfg_all(), prev_close=r["pc"])
    c1 = (1 if st.get("dir") == "多" else -1) if st["phase"] in ("C1", "回踩中", "入场", "过时") else None
    events, box = [], None
    n = len(b)
    for j in range(max(6, n - recent), n + 1):
        res = patterns.live(code, b.iloc[:j], a, qb[qb.index <= b.index[j - 1]] if qb is not None else None,
                            it, pdir, c1, (r["ph"], r["pl"]), cfg_all())
        for e in res["events"]:
            e["ago"] = n - j
            e["cheat"] = CHEAT.get(e["name"], "")
            e["card"] = card(e["frame"], e["name"])
            if not plan:
                e["checks"] = e["checks"] + ["（今天的纪律设置还没定，没核对名单和方向）"]
            events.append(e)
        if j == n:
            box = res.get("box")
    events.reverse()                                  # 新的在前
    h, l, c, v = (b[x].values.astype(float) for x in ("high", "low", "close", "volume"))
    vw = patterns.vwap(h, l, c, v)
    px = float(c[-1])
    lines = []
    if box:
        lines.append(f"震荡中：{box[2]} 根挤在 {box[1]:.2f}–{box[0]:.2f}（高 ${box[0] - box[1]:.2f}）。区间里不做，等收盘站出去、下一根不收回。")
    lines.append(f"C1/C2：{st['phase']}{(' · ' + st['dir']) if st.get('dir') else ''}　{(st.get('reasons') or [''])[0]}")
    lines.append(f"现价 {px:.2f}，VWAP {vw[-1]:.2f}（{'上方' if px > vw[-1] else '下方'} {abs(px - vw[-1]) / a:.2f} 倍 ATR）")
    if it:
        lines.append(f"今天的计划：{it.get('方向')}，入场「{it.get('入场') or ''}」，认错价 {it.get('认错价')}")
    elif plan:
        lines.append("不在今天的观察名单里：按纪律只看不做")
    tmin = b.index[-1].hour * 60 + b.index[-1].minute
    lad = ladder.rows(b, a)
    summ = ""                                     # 由 detail() 填成结论卡的那句话
    return dict(c2_reason=(st.get("reasons") or [""])[0], ladder=lad, summary=summ, events=events, lines=lines, box=box, c2=dict(phase=st["phase"], dir=st.get("dir"), entry=st.get("entry"),
                                                             stop=st.get("stop"), target=st.get("target"), trig_t=st.get("trig_t"), trig_idx=st.get("trig_idx")),
                vwap=[round(float(x), 4) for x in vw], atr=a, refs=r, plan_item=it)


def card(frame, name):
    stt = patterns.load_stats()
    if not stt:
        return None
    F = stt["frames"].get(frame) or {}
    cd = (F.get("cards") or {}).get(name)
    if not cd:
        return None
    rows = []
    for cx in patterns.CTX:
        c = cd["ctx"].get(cx) or {}
        if c.get("n"):
            rows.append(dict(ctx=cx, n=c["n"], win=c["win"], base=c["base_win"], mean=c["mean"], base_mean=c["base_mean"],
                             R=(c.get("R") or {}).get("mean")))
    return dict(why=cd["why"], verdict=cd["verdict"], hz=F["hz"][1], rows=rows)


def levels(code, day, it, r, b):
    out = []
    if r.get("ph"):
        out.append(dict(p=r["ph"], name="昨高"))
    if r.get("pl"):
        out.append(dict(p=r["pl"], name="昨低"))
    if b is not None and len(b):
        out.append(dict(p=float(b.open.iloc[0]), name="开盘价"))
    for x in (it or {}).get("关键价位") or []:
        try:
            out.append(dict(p=float(x["价格"]), name=x.get("说明") or "关键价位"))
        except Exception:
            pass
    if it and it.get("认错价"):
        out.append(dict(p=float(it["认错价"]), name="认错价"))
    try:
        txt = (patterns.BASE / "数据" / "今日" / day / "期权墙.md").read_text(encoding="utf-8")
        for line in txt.splitlines():
            if line.startswith(f"| {code} |"):
                cells = [x.strip() for x in line.strip("|").split("|")]
                for k, nm in ((2, "看涨墙"), (3, "看跌墙")):
                    try:
                        out.append(dict(p=float(cells[k]), name=nm))
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def detail(code, q):
    day, cut, note = mode_of(q)
    live = note == "实时"
    b = get_bars(code, day, cut, live)
    qb = get_bars("QQQ", day, cut, live)
    plan, items = plan_for(plan_day(q, day))
    if b is None or not len(b):
        return dict(code=code, note=note, err=f"读不到 {code} {day} 的 1 分钟K" + ("（代码不对，或 OpenD 没连上）" if live else "（代码不对，或历史库里没有这一天）"))
    an = analyze(code, b, day, qb, plan, items)
    if an.get("err"):
        return dict(code=code, note=note, err=an["err"])
    px = float(b.close.iloc[-1])
    opt = options_of(code, px, ttl=120) if live else None          # 点开的这只 2 分钟刷新一次（今日成交量在变）
    flow = flow_of(code) if live else None
    guard = guard_state(day) if live else None
    strat = strat_eval(code, b, qb, day, plan, items, live)
    vd = verdict(code, b, an, qb, plan, items, live, opt, flow, guard, strat)
    an["summary"] = f"{vd['label']}：{vd['reason']}"            # 「合起来」和结论卡说同一句话
    lv = levels(code, day, an["plan_item"], an["refs"], b)
    if opt:
        lv = [x for x in lv if "墙" not in x["name"]] + [dict(p=opt[k], name=n) for k, n in (("call_wall", "看涨墙"), ("put_wall", "看跌墙")) if opt.get(k)]
    bars = [[f"{t:%H:%M}", round(float(r.open), 4), round(float(r.high), 4), round(float(r.low), 4), round(float(r.close), 4),
             float(r.volume)] for t, r in b.iterrows()]
    # 今天出现过的全部形态（K 线上标记用）：整段一次识别，每根都只用它之前的K线
    o, h, l, c = (b[x].values.astype(float) for x in ("open", "high", "low", "close"))
    marks = []
    for nm, arr in patterns.detect(o, h, l, c).items():
        for i in np.where(arr)[0]:
            if i >= 5:
                marks.append(dict(i=int(i), name=nm, dir=patterns.CARDS[nm][0]))
    for (j, nm, d, bh, bl, L) in patterns.range_events(h, l, c, an["atr"], patterns.settings(cfg_all())):
        marks.append(dict(i=int(j), name=nm, dir=d, box=[float(bh), float(bl), int(j - 1 - L), int(j - 1)]))
    return dict(code=code, day=day, note=note, t=bars[-1][0], bars=bars, vwap=an["vwap"], atr=an["atr"],
                levels=lv, marks=marks, box=an["box"],
                lines=an["lines"], events=an["events"], ladder=an["ladder"], summary=an["summary"], verdict=vd, opt=opt, flow=flow, guard=guard, c2=an["c2"], plan_locked=(plan or {}).get("状态"),
                strat=strat, strat_opts=strat_opts(code, day) if strat is not None else None,
                pos_scheme=((plan or {}).get("仓位") or {}).get("方案"), in_plan=code in items)


def overview(q):
    day, cut, note = mode_of(q)
    live = note == "实时"
    plan, items = plan_for(plan_day(q, day))
    import today
    codes = get_watch(today.session_day())
    qb = get_bars("QQQ", day, cut, live)
    rows = []
    for code in ["QQQ"] + [x for x in codes if x != "QQQ"]:
        try:
            b = get_bars(code, day, cut, live)
            r = refs(code, day)
            if b is None or not len(b):
                rows.append(dict(code=code, err="没有数据"))
                continue
            h, l, c, v = (b[x].values.astype(float) for x in ("high", "low", "close", "volume"))
            vw = patterns.vwap(h, l, c, v)
            px = float(c[-1])
            row = dict(code=code, last=px, chg=(px / r["pc"] - 1) * 100 if r.get("pc") else None,
                       side=1 if px > vw[-1] else -1, dist=(px - vw[-1]) / r["atr"] if r.get("atr") else None,
                       spark=[round(float(x), 3) for x in c[-60:]], plan=(items.get(code) or {}).get("方向"))
            if code != "QQQ" and r.get("atr"):
                res = patterns.live(code, b, r["atr"], qb, items.get(code), (items.get(code) or {}).get("方向") if plan else "双向",
                                    None, (r["ph"], r["pl"]), cfg_all())
                st = c2.scan(b, r["atr"], cfg=cfg_all(), prev_close=r["pc"])
                lite = dict(atr=r["atr"], refs=r, vwap=vw, box=res.get("box"), c2_reason=(st.get("reasons") or [""])[0],
                            c2=dict(phase=st["phase"], dir=st.get("dir"), entry=st.get("entry"), stop=st.get("stop"), target=st.get("target"),
                                    trig_t=st.get("trig_t"), trig_idx=st.get("trig_idx")))
                vd = verdict(code, b, lite, qb, plan, items, live, options_of(code, px, refresh=False) if live else None,
                             flow_of(code, fetch=False) if live else None, guard_state(day) if live else None)
                row["vd"] = dict(label=vd["label"], tone=vd["tone"], reason=vd["reason"])
            rows.append(row)
        except Exception as e:
            rows.append(dict(code=code, err=f"{type(e).__name__}"))
    return dict(day=day, note=note, t=f"{et_now():%H:%M}", rows=rows, watch=codes, max=MAXN,
                share=dict(on=share_on(), url=ts_url() if share_on() else None),
                plan=dict(状态=plan.get("状态"), 偏向=plan.get("偏向"), 一句话=plan.get("一句话")) if plan else None)


def similar(code, q):
    import engine
    day, cut, note = mode_of(q)
    if note == "实时":
        text, res = engine.judge(code, cfg=cfg_all())
    else:
        text, res = engine.judge(code, date=day, time_=cut or "16:00", cfg=cfg_all(), log_call=False, out_dir=REPLAY_DIR)
    if res is None:
        return dict(err=str(text))
    L, S = res["long"], res["short"]
    return dict(verdict=res["verdict"], n=res["N"], png=Path(res.get("png") or "").name, src="" if note == "实时" else "r",
                long=dict(mean=L["mean"], lo=L["lo"], hi=L["hi"], base=L["base"], win=L["win"], win_base=L["win_base"]),
                short=dict(mean=S["mean"], lo=S["lo"], hi=S["hi"], base=S["base"], win=S["win"], win_base=S["win_base"]))



# ================================================================ 实时期权（gamma / delta）和资金流向
OPT, FLOW, OLOCK = {}, {}, threading.Lock()


def _mm():
    """盘中用盯盘自己的 moomoo 连接（和 K 线共用）。"""
    if LIVE.feed is None or not LIVE.feed.ok:
        LIVE.bars("QQQ")
    return LIVE.feed.mm if LIVE.feed is not None and LIVE.feed.ok else None


def options_of(code, spot, refresh=True, ttl=300):
    """期权墙 + 净 gamma + delta 对比：期权链一天抓一次，快照每 5 分钟刷新（gamma、delta 是 moomoo 实时算的；未平仓量是昨天收盘的）。"""
    import flows
    day = str(et_now().date())
    with OLOCK:
        x = OPT.get(code) or {}
        if x.get("day") != day:
            x = dict(day=day)
        if not refresh or (x.get("t") and time.time() - x["t"] < ttl):
            return x.get("w")
        mm = _mm()
        if mm is None:
            return x.get("w")
        try:
            if not x.get("chain"):
                x["chain"] = flows.option_chain(mm, code, spot)
            if x.get("chain"):
                w = flows.walls_from(mm, x["chain"][0], spot)
                if w:
                    w["exps"], w["at"] = x["chain"][1], et_now().strftime("%H:%M")
                    x["w"] = w
        except Exception:
            pass
        x["t"] = time.time()
        OPT[code] = x
        return x.get("w")


def flow_of(code, fetch=True):
    """资金流向：先用热数据每分钟存的（data/.flow_status.json），没有才自己查（只查点开的那只，避免两边重复调用接口碰到频率上限）。"""
    import flows
    try:
        h = json.loads((HERE / "data" / ".flow_status.json").read_text(encoding="utf-8"))
        if time.time() - h.get("t", 0) < 150 and code in (h.get("codes") or {}):
            return h["codes"][code]
    except Exception:
        pass
    if not fetch:
        return None
    x = FLOW.get(code)
    if x and time.time() - x[0] < 60:
        return x[1]
    mm = _mm()
    v = flows.capital(mm, code) if mm is not None else None
    FLOW[code] = (time.time(), v)
    return v


def warm(codes_fn):
    """盘中后台每 5 分钟把名单里每只的期权数据刷新一遍（界面打开时就不用等）。"""
    while True:
        try:
            if market_open():
                for c in codes_fn():
                    b = LIVE.bars(c)
                    if b is not None and len(b):
                        options_of(c, float(b.close.iloc[-1]))
                    time.sleep(2)
        except Exception:
            pass
        time.sleep(60)


def guard_state(day):
    try:
        g = json.loads((HERE / "data" / ".guard_status.json").read_text(encoding="utf-8"))
        return g if g.get("day") == day else None
    except Exception:
        return None


def _money(x):
    if x is None:
        return "—"
    a = abs(x)
    t = f"{a / 1e8:.2f} 亿" if a >= 1e8 else f"{a / 1e4:.0f} 万"
    return ("+" if x >= 0 else "-") + "$" + t


# ================================================================ 一眼看懂的结论

# ================================================================ 选定策略（stratlive.py，和策略提醒程序同一套）
def strat_eval(code, b, qb, day, plan, items, live=False):
    it = items.get(code)
    if not it:
        return None
    try:
        import stratlive
        dd = dt.date.fromisoformat(str(day))
        lim = (plan or {}).get("限制") or {}
        mm = None
        if live:
            try:
                mm = _mm()
            except Exception:
                mm = None
        return stratlive.evaluate(code, b, qb, stratlive.prev_row(code, dd, mm), it, dd, (plan or {}).get("仓位"),
                                  int(lim.get("每笔最多张数") or 4))
    except Exception as e:
        return dict(err=f"{type(e).__name__}: {e}", trades=[], choices={})


def strat_opts(code, day):
    import stratlive
    sn, tn, gn = stratlive.names()
    pb = stratlive.playbook_branches(day)
    br = [dict(gap=g, dir=d, name=b["name"], stop=b["stop_name"], tgt=b["tgt_name"], level=b["level"], sid=b["sid"], stop_id=b["stop"], tgt_id=b["tgt"])
          for (c, g, d), b in pb.items() if c == code]
    return dict(entries=sn, stops=tn, targets=gn, schemes=stratlive.SCHEME_NAMES, playbook=br)


def strat_verdict(strat, tmin):
    """有选定策略时，结论卡按它说；返回 (label, tone, reason) 或 None（没选策略，走原来的判断）。"""
    if not strat or not strat.get("choices"):
        return None
    import stratlive
    cands = []
    for tr in strat.get("trades") or []:
        for ev in tr["events"]:
            h, m = map(int, ev["t"].split(":"))
            cands.append((h * 60 + m, ev, tr))
    cands.sort(key=lambda x: x[0])
    if cands:
        t, ev, tr = cands[-1]
        recent = tmin - t <= 2
        k = ev["kind"]
        first = ev["text"].splitlines()
        body = "；".join(first)
        if recent:
            lab = {"入场": f"现在进 · {tr['dir_name']}", "不做": "信号出现 · 按仓位方案不做", "出场": "该走了", "砍": "该砍了",
                   "加仓": "可以加仓", "走一半": "先走一半", "走出来": "挪止损到保本", "挪止损": "挪止损"}.get(k, k)
            tone = {"入场": "go", "加仓": "go", "走一半": "go", "出场": "stop", "砍": "stop", "不做": "no"}.get(k, "wait")
            return lab, tone, body
        live = [x for x in strat["trades"] if x["state"] == "持仓中"]
        if live:
            tr = live[-1]
            return f"持仓中 · {tr['dir_name']}", "wait", stratlive.describe(tr) + "。按选定的止损和止盈管，到了会提醒。"
    names = "；".join(f"{'做多' if d == 'L' else '做空'}：{c['name']} + {c['stop_name']} + {c['tgt_name']}（{c['src']}）"
                      for d, c in strat["choices"].items())
    if any(x["state"] == "已出场" for x in strat.get("trades") or []):
        done = [x for x in strat["trades"] if x["state"] == "已出场"]
        if tmin > 660 or len(done) == len(strat["choices"]):
            return "今天这笔结束了", "no", "；".join(stratlive.describe(x) for x in done)
    if tmin > 660:
        return "今天没出信号", "no", f"选定的策略（{names}）11:00 前没出信号：今天不做"
    return "等信号", "wait", f"选定：{names}。还没出信号；出了会弹窗。"


def set_strategy(d):
    """盯盘界面改选定策略 / 仓位方案（只限本机）：写今天的纪律设置；锁定后必须写理由，记进修改记录。"""
    import today
    import stratlive
    day = today.session_day()
    p = today.folder(day) / "纪律设置.json"
    if not p.exists():
        raise ValueError("今天还没有纪律设置（盘前报告会建一份，或在 Claude 里说「开始今天」）")
    raw = json.loads(p.read_text(encoding="utf-8"))
    locked = raw.get("状态") == "已锁定"
    why = str(d.get("理由") or "").strip()
    if locked and not why:
        raise ValueError("纪律已经锁定：改策略或仓位要写理由（会记进修改记录）")
    sn, tn, gn = stratlive.names()
    changes = []
    code = str(d.get("代码") or "").upper()
    if code:
        it = next((w for w in raw.get("观察名单") or [] if str(w.get("代码", "")).upper() == code), None)
        if it is None:
            raise ValueError(f"{code} 不在今天纪律设置的观察名单里")
        if d.get("用预案"):
            it.pop("策略", None)
            changes.append(f"{code} 策略改回按预案")
        else:
            e, s, t = d.get("入场"), d.get("止损"), d.get("止盈")
            if e not in sn or s not in tn or t not in gn:
                raise ValueError("入场 / 止损 / 止盈 没选全")
            it["策略"] = {"入场": e, "止损": s, "止盈": t}
            changes.append(f"{code} 策略 → {sn[e]} + {tn[s]} + {gn[t]}")
    if d.get("仓位方案"):
        if d["仓位方案"] not in stratlive.SCHEME_NAMES:
            raise ValueError("仓位方案不认识")
        raw.setdefault("仓位", json.loads(json.dumps(stratlive.DEFAULT_POS)))["方案"] = d["仓位方案"]
        changes.append(f"仓位方案 → {stratlive.SCHEME_NAMES[d['仓位方案']]}")
    if not changes:
        raise ValueError("没有要改的")
    raw.setdefault("修改记录", []).append({"时间": f"{et_now():%H:%M}", "改了什么": "；".join(changes) + "（盯盘界面）",
                                         "理由": why or "（草稿阶段，未写）"})
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(ok=True, changes=changes)


def verdict(code, b, an, qb, plan, items, live, opt, flow, guard, strat=None):
    """返回 dict(label, tone, reason, grade, checks, chips)。tone：go 可以进 / wait 等 / no 不做 / stop 停手或认错。"""
    s = patterns.settings(cfg_all())
    a, r = an["atr"], an["refs"]
    it = items.get(code)
    pdir = (it or {}).get("方向")
    px = float(b.close.iloc[-1])
    vw = an["vwap"][-1]
    tmin = b.index[-1].hour * 60 + b.index[-1].minute
    st = an["c2"]
    q_dir = 0
    if qb is not None and len(qb):
        qh, ql, qc, qv = (qb[x].values.astype(float) for x in ("high", "low", "close", "volume"))
        qvw = patterns.vwap(qh, ql, qc, qv)
        q_dir = 1 if (qc[-1] > qvw[-1] and qc[-1] > float(qb.open.iloc[0])) else -1 if (qc[-1] < qvw[-1] and qc[-1] < float(qb.open.iloc[0])) else 0
    chips = []
    if guard and not guard.get("note"):
        bad = (guard.get("max") and guard.get("opens", 0) >= int(guard["max"])) or \
              (guard.get("max_loss") and guard.get("pnl") is not None and guard["pnl"] <= -float(guard["max_loss"]))
        chips.append(dict(k="纪律", v=f"{guard.get('opens', 0)}/{guard.get('max') or '不限'} 笔 · " +
                          ("盈亏读不到" if guard.get("pnl") is None else f"${guard['pnl']:,.0f}"), tone="bad" if bad else ""))
    elif not live:
        chips.append(dict(k="纪律", v="回放时没有成交记录", tone=""))
    chips.append(dict(k="QQQ", v={1: "在开盘价和 VWAP 上方", -1: "在开盘价和 VWAP 下方", 0: "方向不明"}[q_dir],
                      tone="up" if q_dir > 0 else "down" if q_dir < 0 else ""))
    if flow and flow.get("d15") is not None:
        chips.append(dict(k="资金 15 分钟", v=_money(flow["d15"]), tone="up" if flow["d15"] > 0 else "down"))
    if opt:
        chips.append(dict(k="gamma", v="正 · 偏震荡" if opt["gex"] > 0 else "负 · 易单边", tone=""))
        if opt.get("vol_call_top") or opt.get("vol_put_top"):
            chips.append(dict(k="今日期权成交最多", v=f"看涨 {opt.get('vol_call_top') or '—':g} · 看跌 {opt.get('vol_put_top') or '—':g}"
                              if opt.get("vol_call_top") and opt.get("vol_put_top") else "—", tone=""))
        wl = []
        if opt.get("call_wall"):
            wl.append(f"上 {opt['call_wall']:g}（{(opt['call_wall'] / px - 1) * 100:+.1f}%）")
        if opt.get("put_wall"):
            wl.append(f"下 {opt['put_wall']:g}（{(opt['put_wall'] / px - 1) * 100:+.1f}%）")
        if wl:
            chips.append(dict(k="期权墙", v=" ".join(wl), tone=""))
    if an.get("box"):
        bx = an["box"]
        chips.append(dict(k="震荡", v=f"{bx[1]:.2f}–{bx[0]:.2f}", tone="box"))

    def out(label, tone, reason, grade=None, checks=None):
        return dict(label=label, tone=tone, reason=reason, grade=grade, checks=checks or [], chips=chips)

    # ① 纪律停手
    if guard and not guard.get("note"):
        if guard.get("max_loss") and guard.get("pnl") is not None and guard["pnl"] <= -float(guard["max_loss"]):
            return out("停手", "stop", f"今日盈亏 ${guard['pnl']:,.0f}，已到单日上限 ${float(guard['max_loss']):,.0f}：今天不再开仓")
        if guard.get("max") and guard.get("opens", 0) >= int(guard["max"]):
            return out("停手", "stop", f"今天已经开了 {guard['opens']} 笔，上限 {guard['max']} 笔：不再开新仓")
    # ② 认错价
    stop_px = (it or {}).get("认错价")
    if stop_px:
        if (pdir == "多" and float(b.low.min()) <= stop_px) or (pdir == "空" and float(b.high.max()) >= stop_px):
            return out("认错", "stop", f"今天已经碰到认错价 {stop_px}：计划的前提不成立，按计划认错，不再按原方向做")
    # ③ 名单
    if plan and not it:
        return out("不做", "no", "不在今天的观察名单里：只看不做")
    # ④ 时间
    if tmin < 576:
        return out("等", "wait", "开盘前 5 根不做：等第 6 根（9:35–9:36）收盘定 C1 方向")
    sv = strat_verdict(strat, tmin)                     # ④½ 选定了策略：按选定的策略说（和策略提醒同一套）
    if sv:
        return out(*sv)
    if tmin > 660:
        return out("不开新仓", "no", "11:00 以后不开新仓；手上有仓按原来的止损和目标管")
    ph, d = st.get("phase"), st.get("dir")
    dd = 1 if d == "多" else -1 if d == "空" else 0
    clash = pdir in ("多", "空") and d in ("多", "空") and pdir != d
    # ⑤ 个人策略 B（VWAP 回踩）入场：确认K收盘后 2 分钟内才算「可以进」；之后报这次信号的结果
    if ph == "入场" and not clash and st.get("trig_t"):
        th, tm = map(int, st["trig_t"].split(":"))
        ago = tmin - (th * 60 + tm)
        if ago > 2:
            res = c2.outcome(b, dict(st, dir=d)) if st.get("trig_idx") is not None else None
            hit = (res or {}).get("hit")
            done = hit in ("目标", "止损", "开盘价已过止损")
            what = {"目标": f"已经到目标（{(res or {}).get('R', 0):+.1f}R）", "止损": "已经止损（-1R）", "开盘价已过止损": "开盘价已过止损"}.get(hit, "还在进行中")
            return out("信号已过", "no", f"{st['trig_t']} 出过个人策略 B（VWAP 回踩）信号（{'做多' if dd > 0 else '做空'}，入场约 {st['entry']:.2f}，"
                                         f"止损 {st['stop']:.2f}，目标 {st['target']:.2f}），{what}。" +
                       ("今天的个人策略 B 结束了。" if done else "没上车就别追；在车上的按原止损和目标管。"))
    if ph == "入场" and not clash:
        stop_usd = abs((st.get("entry") or px) - (st.get("stop") or px))
        g, checks = patterns.grade(dd, pdir if plan else "双向", (px - vw) * dd > 0, dd, q_dir == dd, "VWAP", stop_usd, s, tmin)
        tone = "go" if g in ("A", "B") else "wait"
        return out(f"可以{'做多' if dd > 0 else '做空'} · {g} 级" if g in ("A", "B") else f"信号出现 · {g} 级", tone,
                   f"个人策略 B（VWAP 回踩）确认K出现：入场约 {st['entry']:.2f}，止损 {st['stop']:.2f}，目标 {st['target']:.2f}（2R）" +
                   ("" if g in ("A", "B") else "；条件差太多，按纪律不做"), g, checks)
    # ⑥ 个人策略 A：爆发（只做多）
    if pdir in ("多", "双向", None) and (pdir is not None or not plan):
        for k in range(3):
            bb = b.iloc[:len(b) - k] if k else b
            sig = c2.scan_b(bb, a, qqq=qb[qb.index <= bb.index[-1]] if qb is not None else None, prev_high=r.get("ph"))
            if sig:
                stop_usd = 1.0
                g, checks = patterns.grade(1, pdir if plan else "双向", True, dd, q_dir == 1, "VWAP", stop_usd, s, tmin)
                return out(f"个人策略 A 爆发 · 可以做多 · {g} 级" if g in ("A", "B") else f"个人策略 A 爆发信号 · {g} 级", "go" if g in ("A", "B") else "wait",
                           f"{'刚才' if k else '这一根'}大实体阳线站上 VWAP，目标前高 {sig['target']:.2f}（距离 ${sig['dist']:.2f}）；"
                           "止损按权利金亏 0.5。回测：有希望、没被证实", g, checks)
    if clash and ph in ("C1", "回踩中"):
        return out("不做", "no", f"C1 是{'向上' if dd > 0 else '向下'}，和今天计划方向「{pdir}」相反")
    # ⑦ 震荡（个人策略 B 还在进行中才说「等」）
    if an.get("box") and ph in ("C1", "回踩中", "等信号"):
        bx = an["box"]
        return out("等 · 震荡中", "wait", f"挤在 {bx[1]:.2f}–{bx[0]:.2f}（高 ${bx[0] - bx[1]:.2f}）：区间里不做，等收盘站出去、下一根不收回")
    if ph == "C1":
        return out("等回踩 VWAP", "wait", f"C1 {'向上' if dd > 0 else '向下'}，还没回到 VWAP {vw:.2f}（差 {abs(px - vw):.2f}）；碰到的那一根不进")
    if ph == "回踩中":
        return out("等确认 K", "wait", f"已经回踩到 VWAP：等{'收盘越过前一根高点或看涨吞没' if dd > 0 else '收盘跌破前一根低点或看跌吞没'}，"
                                     f"并收在 VWAP {'上方' if dd > 0 else '下方'}")
    if ph == "等信号":
        return out("等", "wait", "等第 6 根收盘定 C1 方向")
    why = {"不做": "个人策略 B 今天不成立（" + ((an.get("c2_reason") or "") or "条件不满足") + "）",
           "作废": "回调越过 C1 起点，这一波作废", "过时": "等太久没出确认，个人策略 B 今天放弃"}.get(ph, ph or "")
    if an.get("box"):
        why += "；另外现在在震荡区间里"
    return out("不做", "no", why)

# ================================================================ 共享（只对 Tailscale）
import ipaddress  # noqa: E402

TS_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]


def share_on():
    return bool((cfg_all().get("盯盘界面") or {}).get("共享给Tailscale"))


def allowed(ip):
    """本机永远可以；开了共享时，Tailscale 地址也可以。局域网其他设备、公网一律不行。"""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except Exception:
        return False
    if a.is_loopback:
        return True
    return share_on() and any(a in n for n in TS_NETS)


_TS = {}


def ts_url():
    if time.time() - _TS.get("t", 0) < 300:
        return _TS.get("url")
    url = None
    try:                                   # 直接看网卡上有没有 Tailscale 的 100.x 地址（命令行工具有时读不到 App 的状态）
        import re
        import subprocess
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        ips = [x for x in re.findall(r"inet (100\.\d+\.\d+\.\d+)", r.stdout) if ipaddress.ip_address(x) in TS_NETS[0]]
        url = f"http://{ips[0]}:{PORT}/" if ips else None
    except Exception:
        url = None
    _TS.update(t=time.time(), url=url)
    return url


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        if allowed(self.client_address[0]):
            return True
        self._send(403, {"error": "只对本机和 Tailscale 共享的设备开放"})
        return False

    def _local(self):
        try:
            return ipaddress.ip_address(self.client_address[0].split("%")[0]).is_loopback
        except Exception:
            return False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=lambda x: None if x != x else str(x)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._ok():
            return
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                import version
                self._send(200, version.page(HERE / "dash.html"), "text/html; charset=utf-8")
            elif u.path in ("/cyber.css", "/cyber.js", "/yuheng.css"):
                self._send(200, (HERE / u.path[1:]).read_bytes(),
                           "text/css; charset=utf-8" if u.path.endswith(".css") else "application/javascript; charset=utf-8")
            elif u.path.startswith("/brand/"):          # 玉衡标志素材（只认 brand 目录下的文件名）
                name = u.path.removeprefix("/brand/")
                f = HERE / "brand" / name
                if "/" in name or ".." in name or not f.is_file():
                    return self._send(404, {"error": "not found"})
                self._send(200, f.read_bytes(), "image/png" if name.endswith(".png") else "image/x-icon")
            elif u.path == "/api/version":
                self._send(200, dict(version=VERSION))
            elif u.path == "/api/list":
                self._send(200, overview(q))
            elif u.path == "/api/detail":
                res = detail((q.get("code") or ["QQQ"])[0].upper(), q)
                res["can_edit"] = self._local()
                self._send(200, res)
            elif u.path == "/api/pos":
                import roll
                self._send(200, roll.snapshot(LIVE.bars if market_open() else None))
            elif u.path == "/api/similar":
                self._send(200, similar((q.get("code") or ["QQQ"])[0].upper(), q))
            elif u.path == "/png":
                f = Path((q.get("f") or [""])[0]).name
                p = (REPLAY_DIR if (q.get("src") or [""])[0] == "r" else HOT) / f
                if f.endswith(".png") and p.exists():
                    self._send(200, p.read_bytes(), "image/png")
                else:
                    self._send(404, {"error": "not found"})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            print(traceback.format_exc(), flush=True)
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._ok():
            return
        o = self.headers.get("Origin")
        if o is not None and o != f"http://{self.headers.get('Host')}":       # 只接受本页面自己发的修改
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/strategy":                 # 改选定策略：只限本机（共享给同事的设备不能改纪律）
                if not self._local():
                    return self._send(403, {"error": "只能在本机改策略"})
                try:
                    return self._send(200, set_strategy(d))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/watch":
                import today
                return self._send(200, {"watch": set_watch(today.session_day(), d.get("codes") or [])})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main(open_browser=False):
    import today
    threading.Thread(target=warm, args=(lambda: get_watch(today.session_day()),), daemon=True).start()
    host = "0.0.0.0" if share_on() else "127.0.0.1"         # 开了共享才对外监听；每个请求还会再查一次来源
    srv = ThreadingHTTPServer((host, PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"盯盘界面：{url}", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main("--open" in sys.argv)
