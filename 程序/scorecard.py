# -*- coding: utf-8 -*-
"""
打分记录卡（2026-11-26，v5.14.0）：先按固定格式记，40 天以后再用真实结果定每一项值几分。

两部分：
  1. 盘前消息面标记（盘前报告时自动跑，写 数据/今日/<日期>/消息面标记.json，并附进盘前资料）：
     每只自选股，从上一个交易日 16:00 到今天 9:30 的个股新闻（公司 / 行业 / 财报 / 推特，不含 Reddit 热度），
     按关键词粗分「利好 / 利空 / 有好有坏 / 有消息·看不出方向 / 没有个股消息」，附盘前跳空（倍日线 ATR）。
     这是规则标的，不是 AI，也不保证对；它的用处是**在看到走势之前**留一个带时间的记录，事后不能改（9:30 以后不再覆盖）。
  2. 进场前打分卡（盯盘界面「进场前打分卡」，只限本机）：进场前点一下，存进 数据/今日/<日期>/打分卡.jsonl。
     你自己填：方向、消息面（利好 / 利空 / 没有 / 不清楚）、数据面（利好 / 利空 / 中性 / 不清楚）、一句话理由；
     程序自动填：自动消息面标记、今天跳空、现价在 VWAP 哪边、QQQ 在开盘价和 VWAP 哪边、开盘后几分钟、名单和方向、第几张卡。
  3. 统计（`scorecard.py --stats`，控制台工具「打分卡统计」）：把卡和 trading.db 里的真实成交对上
     （同一天、同一只、同方向、卡之后 30 分钟内开的第一笔），按每一项看胜率和平均盈亏，并按「顺着方向的项数」看。
     天数不够 40 天时报告开头会写明「只看不信」。

只读成交和行情；不下单、不改纪律设置。分数现在不影响任何提醒和仓位。
"""
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
INDEX = BASE / "数据" / "冷数据" / "信息面" / ".index"
TRADING_DB = HERE / "data" / "trading.db"
MIN_DAYS = 40

DEFAULTS = {
    "利好词": ["上调评级", "上调目标价", "买入评级", "增持评级", "超预期", "好于预期", "高于预期", "强于预期", "创新高", "历史新高", "获批", "批准",
             "合作", "签署", "大单", "订单", "中标", "回购", "上调指引", "提高指引", "上调预期", "上调全年", "大涨", "涨超", "盘前涨", "利好",
             "放宽", "豁免", "解除", "扩产", "加大投资", "强劲", "upgrade", "beats", "beat estimates", "raises", "record high", "approval",
             "partnership", "buyback", "surges", "jumps"],
    "利空词": ["下调评级", "下调目标价", "卖出评级", "减持评级", "不及预期", "低于预期", "逊于预期", "弱于预期", "疲软", "下滑", "亏损", "调查",
             "起诉", "诉讼", "罚款", "禁令", "制裁", "出口管制", "召回", "裁员", "下调指引", "下调预期", "警告", "预警", "暴跌", "大跌", "跌超",
             "盘前跌", "利空", "推迟", "延迟", "停产", "违约", "做空", "downgrade", "misses", "cuts", "lawsuit", "probe", "recall", "ban",
             "plunges", "slumps", "falls"],
    "新闻类别": ["公司", "行业", "财报", "推特"],
    "排除词": ["你需要了解", "要闻", "早报", "晚报", "午报", "汇总", "日内请重点关注"],
}
MANUAL_NEWS = ["利好", "利空", "没有", "不清楚"]
MANUAL_DATA = ["利好", "利空", "中性", "不清楚"]


def settings(cfg=None):
    if cfg is None:
        try:
            cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (cfg.get("打分卡") or {}).items() if k in DEFAULTS and v})
    return s


def et_now():
    return dt.datetime.now(ET)


def folder(day, make=True):
    p = BASE / "数据" / "今日" / str(day)
    if make:
        p.mkdir(parents=True, exist_ok=True)
    return p


def prev_weekday(d):
    d = d - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# ================================================================ 1. 盘前消息面标记

def _items(start, end):
    """信息面索引里 [start, end) 的条目（美东时间）。"""
    out = []
    months = sorted({start.strftime("%Y-%m"), end.strftime("%Y-%m")})
    for m in months:
        p = INDEX / f"{m}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                it = json.loads(line)
                t = dt.datetime.fromisoformat(it["t"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=ET)
            except Exception:
                continue
            if start <= t < end:
                it["_t"] = t
                out.append(it)
    return out


def classify(text, s):
    low = text.lower()
    pos = [w for w in s["利好词"] if w.lower() in low]
    neg = [w for w in s["利空词"] if w.lower() in low]
    return pos, neg


def label_of(pos_n, neg_n, n):
    if n == 0:
        return "没有个股消息"
    if pos_n and neg_n:
        return "有好有坏" if abs(pos_n - neg_n) <= 1 else ("偏利好" if pos_n > neg_n else "偏利空")
    if pos_n:
        return "利好"
    if neg_n:
        return "利空"
    return "有消息·看不出方向"


def news_lean(label):
    """自动标记 → +1 利好 / -1 利空 / 0。"""
    return 1 if label in ("利好", "偏利好") else -1 if label in ("利空", "偏利空") else 0


def tag_news(day, codes, cfg=None, now=None):
    s = settings(cfg)
    day = dt.date.fromisoformat(str(day))
    start = dt.datetime.combine(prev_weekday(day), dt.time(16, 0), ET)
    now = now or et_now()
    end = min(dt.datetime.combine(day, dt.time(9, 30), ET), now)
    items = [it for it in _items(start, end) if it.get("cat") in s["新闻类别"]
             and not any(w in (it.get("title") or "") for w in s["排除词"])]
    out = {}
    for c in codes:
        mine = [it for it in items if c in (it.get("codes") or [])]
        heads, P, N = [], 0, 0
        for it in sorted(mine, key=lambda x: x["_t"], reverse=True):
            pos, neg = classify(f"{it.get('title', '')} {it.get('summary', '')}", s)
            P += bool(pos)
            N += bool(neg)
            heads.append(dict(t=it["_t"].strftime("%m-%d %H:%M"), src=it.get("src", ""), title=(it.get("title") or "").split("\n")[0][:90],
                              pos=pos[:3], neg=neg[:3]))
        out[c] = dict(label=label_of(P, N, len(mine)), n=len(mine), pos=P, neg=N, heads=heads[:6])
    macro = []
    cal_end = dt.datetime.combine(day, dt.time(16, 0), ET)
    for it in _items(dt.datetime.combine(day, dt.time(0, 0), ET), cal_end):
        if it.get("cat") == "日历":
            macro.append(f"{it['_t']:%H:%M} {it.get('title', '')}")
    return out, macro


def _atr_pc(code, day):
    """前一个交易日收盘和 14 日 ATR（只用 day 之前的日线）。"""
    try:
        import history
        d1 = history.load(code, "1d", end=f"{day} 00:00:00")
        d1 = d1[d1.index.normalize() < dt.datetime.combine(dt.date.fromisoformat(str(day)), dt.time())]
        if len(d1) < 15:
            return None, None
        pc = d1.close.shift(1)
        tr = (d1.high - d1.low).combine((d1.high - pc).abs(), max).combine((d1.low - pc).abs(), max)
        return float(d1.close.iloc[-1]), float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    except Exception:
        return None, None


def write_tags(cfg, day, codes, quotes=None, log=print):
    """盘前报告调用。9:30 以后不覆盖已有的标记（保证是开盘前留下的）。返回给盘前资料用的 markdown。"""
    p = folder(day) / "消息面标记.json"
    now = et_now()
    after_open = now >= dt.datetime.combine(dt.date.fromisoformat(str(day)), dt.time(9, 30), ET)
    if p.exists() and after_open:
        data = json.loads(p.read_text(encoding="utf-8"))
        return to_md(data)
    tags, macro = tag_news(day, codes, cfg, now)
    quotes = quotes or {}
    for c, t in tags.items():
        q = quotes.get(c) or {}
        px = q.get("盘前") or q.get("夜盘")
        pc, atr = _atr_pc(c, day)
        prev = q.get("prev") or pc
        if px and prev:
            t["盘前涨跌%"] = round((px / prev - 1) * 100, 2)
            if atr:
                t["盘前跳空_ATR"] = round((px - prev) / atr, 2)
    data = dict(日期=str(day), 标记时间=now.strftime("%Y-%m-%d %H:%M"), 说明="规则按关键词粗标，不是 AI；开盘前留的记录，9:30 后不再覆盖",
                宏观日程=macro, 个股=tags)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if log:
        log(f"消息面标记：{len(tags)} 只，{sum(1 for t in tags.values() if t['n'])} 只有个股消息")
    return to_md(data)


def load_tags(day):
    p = folder(day, False) / "消息面标记.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def to_md(data):
    L = [f"## 消息面标记（自动，规则粗标 · {data.get('标记时间', '')} 留的记录）", "",
         "从上一个交易日 16:00 到今天开盘前的个股新闻，按关键词粗分；**只是给打分卡留的开盘前记录，不代表方向判断**。", "",
         "| 股票 | 标记 | 条数（利好词 / 利空词） | 盘前涨跌 | 盘前跳空（倍 ATR） | 最近一条 |", "|---|---|---|---|---|---|"]
    rows = sorted((data.get("个股") or {}).items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    for c, t in rows:
        h = t["heads"][0]["title"] if t.get("heads") else ""
        L.append(f"| {c} | {t['label']} | {t['n']}（{t['pos']} / {t['neg']}） | "
                 f"{'—' if t.get('盘前涨跌%') is None else format(t['盘前涨跌%'], '+.2f') + '%'} | "
                 f"{'—' if t.get('盘前跳空_ATR') is None else format(t['盘前跳空_ATR'], '+.2f')} | {h} |")
    if data.get("宏观日程"):
        L += ["", "今天的经济日程：" + "；".join(data["宏观日程"][:8])]
    return "\n".join(L) + "\n"


# ================================================================ 2. 进场前打分卡

def cards_file(day, make=True):
    return folder(day, make) / "打分卡.jsonl"


def load_cards(day):
    p = cards_file(day, False)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def add_card(day, code, manual, auto):
    """manual = {方向, 消息面, 数据面, 理由}；auto = 程序算好的客观项。返回存下的那条。"""
    d = manual.get("方向")
    if d not in ("多", "空"):
        raise ValueError("方向要选「多」或「空」")
    if manual.get("消息面") not in MANUAL_NEWS:
        raise ValueError("消息面没选")
    if manual.get("数据面") not in MANUAL_DATA:
        raise ValueError("数据面没选")
    now = et_now()
    rec = dict(时间=now.strftime("%Y-%m-%d %H:%M:%S"), 日期=str(day), 代码=code.upper(), 方向=d,
               消息面=manual["消息面"], 数据面=manual["数据面"], 理由=str(manual.get("理由") or "").strip()[:200],
               第几张=len(load_cards(day)) + 1, 自动=auto)
    with cards_file(day).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def auto_items(code, day, bars, qqq, plan, items, atr, pc):
    """打分卡里程序自动填的客观项（按点「记一张」那一刻能看到的K线）。"""
    import numpy as np
    a = {}
    tags = load_tags(day)
    t = ((tags or {}).get("个股") or {}).get(code)
    a["消息面自动"] = t["label"] if t else ("没跑盘前标记" if tags is None else "不在自选股")
    if t and t.get("盘前跳空_ATR") is not None:
        a["盘前跳空_ATR"] = t["盘前跳空_ATR"]
    if bars is not None and len(bars):
        h, l, c, v = (bars[x].values.astype(float) for x in ("high", "low", "close", "volume"))
        tp = (h + l + c) / 3
        vw = float((tp * v).sum() / v.sum()) if v.sum() > 0 else float(c[-1])
        px = float(c[-1])
        a["现价"] = round(px, 4)
        a["VWAP"] = round(vw, 4)
        if atr:
            a["离VWAP_ATR"] = round((px - vw) / atr, 3)
            if pc:
                a["跳空_ATR"] = round((float(bars.open.iloc[0]) - pc) / atr, 3)
        t0 = bars.index[-1]
        a["开盘后分钟"] = int(t0.hour * 60 + t0.minute - 570)
    if qqq is not None and len(qqq):
        qc = qqq.close.values.astype(float)
        qtp = (qqq.high.values + qqq.low.values + qc) / 3
        qv = qqq.volume.values.astype(float)
        qvw = float((qtp * qv).sum() / qv.sum()) if qv.sum() > 0 else qc[-1]
        a["QQQ_开盘价上方"] = bool(qc[-1] > float(qqq.open.iloc[0]))
        a["QQQ_VWAP上方"] = bool(qc[-1] > qvw)
    it = (items or {}).get(code)
    if not plan:
        a["名单"] = "没有纪律设置"
    elif it is None:
        a["名单"] = "名单外"
    else:
        a["名单"] = "名单内"
        a["计划方向"] = it.get("方向")
    a["纪律状态"] = (plan or {}).get("状态")
    return a


# ================================================================ 3. 统计

def all_cards():
    root = BASE / "数据" / "今日"
    out = []
    for p in sorted(root.glob("*/打分卡.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def trades():
    import pandas as pd
    if not TRADING_DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(TRADING_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return pd.read_sql_query("SELECT trade_id, date, underlying, direction, open_ts, qty, pnl, tag FROM trades", con)
    finally:
        con.close()


def match(cards, tr):
    """卡 → 成交：同一天、同一只、同方向，卡之后 30 分钟内开的第一笔（卡前 5 分钟内的也算，先下单后补卡）。每笔成交只配一张卡。"""
    import pandas as pd
    used = set()
    out = []
    if len(tr):
        tr = tr.assign(_t=pd.to_datetime(tr.open_ts, errors="coerce"))
    for c in sorted(cards, key=lambda x: x["时间"]):
        ct = pd.Timestamp(c["时间"])
        hit = None
        if len(tr):
            g = tr[(tr.date == c["日期"]) & (tr.underlying == c["代码"]) & (tr.direction == c["方向"])
                   & (tr._t >= ct - pd.Timedelta(minutes=5)) & (tr._t <= ct + pd.Timedelta(minutes=30))
                   & (~tr.trade_id.isin(used))].sort_values("_t")
            if len(g):
                hit = g.iloc[0]
                used.add(hit.trade_id)
        out.append(dict(c, 成交=None if hit is None else dict(trade_id=hit.trade_id, pnl=float(hit.pnl), qty=float(hit.qty),
                                                               tag=hit.tag, open_ts=str(hit.open_ts))))
    return out, used


def features(m):
    """一张卡 → 统计用的各项（都换成「和方向顺不顺」）。"""
    sgn = 1 if m["方向"] == "多" else -1
    a = m.get("自动") or {}
    f = {}
    f["你判断的消息面"] = {"利好": "顺" if sgn > 0 else "逆", "利空": "逆" if sgn > 0 else "顺"}.get(m["消息面"], m["消息面"])
    f["你判断的数据面"] = {"利好": "顺" if sgn > 0 else "逆", "利空": "逆" if sgn > 0 else "顺"}.get(m["数据面"], m["数据面"])
    ln = news_lean(a.get("消息面自动", ""))
    f["自动消息面"] = "顺" if ln * sgn > 0 else "逆" if ln * sgn < 0 else (a.get("消息面自动") or "—")
    if a.get("离VWAP_ATR") is not None:
        f["VWAP"] = "同侧" if a["离VWAP_ATR"] * sgn > 0 else "反侧"
    if a.get("QQQ_开盘价上方") is not None:
        k = (a["QQQ_开盘价上方"] + a["QQQ_VWAP上方"]) * 2 - 2         # -2 / 0 / +2
        f["QQQ"] = "同向" if k * sgn > 0 else "反向" if k * sgn < 0 else "混合"
    if a.get("跳空_ATR") is not None:
        g = a["跳空_ATR"] * sgn
        f["跳空"] = "顺着跳 >0.3 ATR" if g > 0.3 else "逆着跳 >0.3 ATR" if g < -0.3 else "小跳空"
    if a.get("开盘后分钟") is not None:
        mnt = a["开盘后分钟"]
        f["时间"] = "开盘 10 分钟内" if mnt < 10 else "10–30 分钟" if mnt < 30 else "30–90 分钟" if mnt < 90 else "90 分钟以后"
    nm = a.get("名单")
    if nm == "名单内":
        pdir = a.get("计划方向")
        f["名单"] = "名单内·同向" if pdir in (m["方向"], "双向") else "名单内·反向"
    else:
        f["名单"] = nm or "—"
    score = sum(1 for k in ("你判断的消息面", "你判断的数据面", "自动消息面") if f.get(k) == "顺") \
        + (f.get("VWAP") == "同侧") + (f.get("QQQ") == "同向") + (f.get("名单") == "名单内·同向")
    f["顺着方向的项数（满分 6）"] = str(score)
    return f


def stats(log=print, path=None):
    import pandas as pd
    cards = all_cards()
    tr = trades()
    ms, _ = match(cards, tr)
    days = sorted({c["日期"] for c in cards})
    today = dt.date.today().isoformat()
    path = path or (BASE / "报告" / f"打分卡统计_{today}.md")
    L = [f"# 打分卡统计 · {today}", "",
         f"打分卡 {len(cards)} 张，{len(days)} 天（{days[0] if days else '—'} ~ {days[-1] if days else '—'}）；对上真实成交 {sum(1 for m in ms if m['成交'])} 张。",
         "对法：同一天、同一只、同方向，卡之后 30 分钟内（或卡前 5 分钟内）开的第一笔。盈亏是这笔的美元盈亏（含手续费口径以成交同步为准）。", ""]
    if len(days) < MIN_DAYS:
        L += [f"> **只看不信**：只有 {len(days)} 天，不到 {MIN_DAYS} 天。下面的数字随便一两笔就会变，不能拿来定分值。", ""]
    if len(tr) and days:
        t2 = tr[tr.date.isin(days)]
        matched = {m["成交"]["trade_id"] for m in ms if m["成交"]}
        miss = t2[~t2.trade_id.isin(matched)]
        L += [f"这些天里有 {len(t2)} 笔真实成交，{len(miss)} 笔没有对应的打分卡（进场前没点）。", ""]
    rows = [dict(features(m), pnl=m["成交"]["pnl"]) for m in ms if m["成交"]]
    if not rows:
        L += ["还没有对上成交的打分卡。", ""]
    else:
        df = pd.DataFrame(rows)
        L += [f"全部：{len(df)} 笔，胜率 {(df.pnl > 0).mean() * 100:.0f}%，平均每笔 ${df.pnl.mean():+.0f}，合计 ${df.pnl.sum():+.0f}", ""]
        for col in [c for c in df.columns if c != "pnl"]:
            g = df.groupby(col).pnl.agg(["size", "mean", "sum", lambda s: (s > 0).mean()])
            g.columns = ["笔数", "平均", "合计", "胜率"]
            L += [f"### {col}", "", "| 取值 | 笔数 | 胜率 | 平均每笔 | 合计 |", "|---|---|---|---|---|"]
            for k, r in g.sort_index().iterrows():
                L.append(f"| {k} | {int(r['笔数'])} | {r['胜率'] * 100:.0f}% | ${r['平均']:+.0f} | ${r['合计']:+.0f} |")
            L.append("")
    nomatch = [m for m in ms if not m["成交"]]
    if nomatch:
        L += [f"### 点了卡但没下单（或没对上成交）：{len(nomatch)} 张", "",
              "这些也有用：以后可以看「打了分没做」的那些，之后的走势和做了的比怎么样。", ""]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    if log:
        log(f"打分卡 {len(cards)} 张、{len(days)} 天，对上成交 {len(rows)} 笔 → {path}")
    return path


if __name__ == "__main__":
    if "--stats" in sys.argv:
        stats()
    elif "--tag" in sys.argv:
        import today
        import news
        cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        day = today.session_day()
        codes = [w["code"] for w in news.research(cfg)]
        tags, macro = tag_news(day, codes, cfg)
        print(to_md(dict(标记时间=f"{et_now():%Y-%m-%d %H:%M}（预览，没保存）", 个股=tags, 宏观日程=macro)))
    else:
        print(__doc__)
