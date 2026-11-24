# -*- coding: utf-8 -*-
"""
冷数据·信息面：把能抓到的新闻按类别每天归档，同时按股票汇总，给 Claude 直接读

  数据/冷数据/信息面/
      宏观/日期.md     华尔街见闻快讯里的宏观、政策、利率、地缘
      公司/日期.md     提到研究名单里某只股票的快讯 + Yahoo 个股新闻
      行业/日期.md     半导体、AI、存储、电动车、云……没点名具体公司的
      财报/日期.md     财报相关新闻；财报/财报日历.md = 研究名单的下一个财报日
      推特/日期.md     Grok 盘前雷达核实过的 X 帖子（谁、什么时候、观点、涉及股票和看涨看跌）
      Reddit/日期.md   ApeWisdom 热度快照（研究名单 + WSB 前 20）+ 当天触发提醒时 Grok 读帖的结论
      日历/日期.md     未来 72 小时美国中高影响数据
      按股票/代码.md   这只股票最近 30 天的全部条目，新的在前
  每条：时间（美东）、来源、涉及股票、标题 / 摘要、原文链接。情绪和重要程度不在这里判断，由读的人（Claude）判断。
  去重和重建靠 信息面/.index/年-月.jsonl（每条一行）。

  自动：机器人每 20 分钟跑一次（美东工作日 4:00–20:00），另外每天 22:00 补一次。
  手动：python3 news.py   （或 Discord 发「归档新闻」）
"""
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
ROOT = HERE.parent / "数据" / "冷数据" / "信息面"
INDEX = ROOT / ".index"
CATS = ["宏观", "公司", "行业", "财报", "推特", "Reddit", "日历"]
INDUSTRY = {
    "半导体": ["半导体", "芯片", "晶圆", "台积电", "光刻", "ASML", "semiconductor", "chip", "foundry", "TSMC"],
    "AI": ["人工智能", "大模型", "OpenAI", "Anthropic", "算力", "数据中心", "GPU", "AI 芯片", "生成式", "LLM", "AI"],
    "存储": ["存储", "内存", "DRAM", "NAND", "HBM", "闪存", "硬盘"],
    "云 / 软件": ["云计算", "云服务", "SaaS", "软件股", "cloud"],
    "电动车 / 自动驾驶": ["电动车", "新能源车", "自动驾驶", "Robotaxi", "EV "],
    "光通信 / 网络": ["光模块", "光通信", "光纤", "交换机", "以太网"],
    "能源": ["原油", "油价", "天然气", "OPEC"],
}
EARN = re.compile(r"财报|业绩|季报|营收|每股收益|EPS|指引|earnings|quarterly results|guidance|revenue", re.I)


def et_now():
    return dt.datetime.now(ET)


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


# ================================================================ 名单
def research(cfg=None):
    """研究名单：K 线库里的全部代码 + 名称（个股设置.xlsx 里有的）。"""
    codes = []
    try:
        import sqlite3
        con = sqlite3.connect(Path(HERE / 'data' / 'history.db').resolve().as_uri() + "?mode=ro", uri=True)
        codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM bars_1d ORDER BY 1")]
        con.close()
    except Exception:
        pass
    names = {}
    try:
        import stocks
        params, *_ = stocks.load_settings(lambda *a, **k: None)
        names = {c: (p.get("名称") or c) for c, p in params.items()}
    except Exception:
        pass
    return [dict(code=c, name=names.get(c, c), etf=c in ETF) for c in codes]


ETF = {"QQQ", "SPY", "SMH", "SOXX", "XLK", "XLC", "XLI", "XLP", "XLY", "IGV", "TLT", "IWM", "DIA"}


# ================================================================ 来源
def from_wscn(watch, hours):
    """华尔街见闻快讯 → 宏观 / 公司 / 行业 / 财报。"""
    import briefing as B
    out = []
    matchers = B.build_matchers(watch)
    for i in B._fetch_lives(hours):
        title = (i.get("title") or "").strip()
        body = (i.get("content_text") or "").strip()
        text = title or body
        if not text:
            continue
        full = text + " " + body
        t = dt.datetime.fromtimestamp(i["display_time"], dt.timezone.utc).astimezone(ET)
        who = [c for c, (_, rx) in matchers.items() if rx.search(full)]
        base = dict(t=t.isoformat(timespec="minutes"), src="华尔街见闻", url=i.get("uri") or "",
                    title=text[:160], summary=(body[:300] if body and body != text else ""), codes=who[:5])
        cats = []
        if who:
            cats.append("公司")
        else:
            inds = [k for k, ws in INDUSTRY.items() if any(w in full for w in ws)]
            if inds:
                cats.append("行业")
                base["tags"] = inds
            elif any(w in full for w in B.MACRO_WORDS) or (i.get("score") or 0) >= 2:
                cats.append("宏观")
        if EARN.search(full) and who:
            cats.append("财报")
        for c in cats:
            out.append(dict(base, cat=c, id=_id(c, base["url"] or text)))
    return out


def from_yahoo(watch, hours):
    """Yahoo 个股新闻 → 公司（财报类同时进财报）；顺便拿下一个财报日。"""
    import briefing as B
    from concurrent.futures import ThreadPoolExecutor
    out, earn = [], {}
    stocks_ = [w for w in watch if not w["etf"]]

    def one(w):
        return w["code"], B.yahoo_info(w["code"], hours)
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, stocks_))
    for code, info in res:
        if info.get("earnings"):
            earn[code] = str(info["earnings"])
        for n in info.get("news") or []:
            t = n.get("t")
            base = dict(t=t.astimezone(ET).isoformat(timespec="minutes") if t else et_now().isoformat(timespec="minutes"),
                        src=n.get("src") or "Yahoo", url=n.get("url") or "", title=n.get("text") or "", summary="",
                        codes=[code])
            out.append(dict(base, cat="公司", id=_id("公司", base["url"] or base["title"])))
            if EARN.search(base["title"]):
                out.append(dict(base, cat="财报", id=_id("财报", base["url"] or base["title"])))
    return out, earn


def from_calendar():
    import briefing as B
    out = []
    for e in B.calendar(hours=72):
        t = e["t"].astimezone(ET)
        title = f"{'★ ' if e['high'] else ''}{e['name']}" + (f"（预期 {e['forecast']}" + (f"，前值 {e['previous']}" if e["previous"] else "") + "）"
                                                            if e["forecast"] or e["previous"] else "")
        out.append(dict(t=t.isoformat(timespec="minutes"), src="Forex Factory", url="", title=title, summary="",
                        codes=[], cat="日历", id=_id("日历", t.isoformat(), e["name"])))
    return out


def from_grok():
    """Grok 盘前雷达最近一次的结果（它自己每天 8:40 跑，这里只归档，不额外花钱）。"""
    try:
        import grokscan as G
        r = (G.load_cache().get("latest") or {})
    except Exception:
        return []
    out = []
    for p in r.get("posts") or []:
        ticks = [f"{x['ticker']}{'↑' if x.get('stance') == '看涨' else '↓' if x.get('stance') == '看跌' else ''}" for x in p.get("tickers") or []]
        out.append(dict(t=str(p.get("t") or "")[:16], src=f"X @{p.get('handle')}（{p.get('cat')}）", url=p.get("url") or "",
                        title=p.get("summary") or "", summary=(p.get("core") or "") + (f"　涉及：{' '.join(ticks)}" if ticks else ""),
                        codes=[x["ticker"] for x in p.get("tickers") or []], cat="推特", id=_id("推特", p.get("url"))))
    syn = r.get("synth") or {}
    if syn:
        txt = json.dumps(syn, ensure_ascii=False)[:1500]
        out.append(dict(t=str(r.get("at") or "")[:16], src="Grok 综合（基于上面核实过的帖子）", url="", title="当天推特综合",
                        summary=txt, codes=[], cat="推特", id=_id("推特综合", r.get("day"))))
    return out


def from_reddit(cfg, watch):
    """ApeWisdom 热度快照（免费）+ 散户监测当天读帖的结论。"""
    out = []
    try:
        import reddit_watch as RW
        s = RW.settings(cfg or {})
        H = RW.heat(s)
    except Exception:
        return out
    now = et_now()
    codes = {w["code"] for w in watch}
    lines = []
    for t, d in sorted(H.items(), key=lambda x: -x[1]["mentions"]):
        if t in codes or (d.get("wsb_rank") or 999) <= 20:
            lines.append(f"{t}：提及 {d['mentions']}（24 小时前 {d['m24']}，{d['ratio']:.1f} 倍），"
                         f"WSB 排名 {d.get('wsb_rank') or '—'}（前一天 {d.get('wsb_r24') or '—'}）{'　★研究名单' if t in codes else ''}")
    if lines:
        out.append(dict(t=now.isoformat(timespec="minutes"), src="ApeWisdom", url="https://apewisdom.io/",
                        title=f"Reddit 热度快照 {now:%H:%M}", summary="\n".join(lines[:40]), codes=[],
                        cat="Reddit", id=_id("Reddit", now.strftime("%Y-%m-%d %H"))))
    try:
        st = (RW.load_cache().get(str(now.date())) or {})
        for a in st.get("log") or []:
            out.append(dict(t=a["t"][:16], src="Grok 读 Reddit 帖子", url=(a.get("posts") or [{}])[0].get("url", ""),
                            title=f"{a['ticker']}：{a.get('tone', '')}，{a.get('calls_puts', '')}，{a.get('mood', '')}",
                            summary=a.get("summary") or "", codes=[a["ticker"]], cat="Reddit", id=_id("Reddit读帖", a["t"], a["ticker"])))
    except Exception:
        pass
    return out


# ================================================================ 存 / 写
def _load_month(m):
    p = INDEX / f"{m}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def save(items):
    """去重后追加进索引；返回新增条数和涉及的日期。"""
    INDEX.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for it in items:
        by_month.setdefault(it["t"][:7], []).append(it)
    added, days = 0, set()
    for m, its in by_month.items():
        old = _load_month(m)
        seen = {o["id"] for o in old}
        snap = {it["id"] for it in its if it["cat"] == "Reddit" and it["src"] == "ApeWisdom"}
        new = [it for it in its if it["id"] not in seen]
        rep = [it for it in its if it["id"] in seen and it["id"] in snap]          # 同一小时的热度快照：用最新的
        if rep:
            rid = {r["id"] for r in rep}
            old = [o for o in old if o["id"] not in rid] + rep
            (INDEX / f"{m}.jsonl").write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in old), encoding="utf-8")
            days |= {r["t"][:10] for r in rep}
        if new:
            with (INDEX / f"{m}.jsonl").open("a", encoding="utf-8") as f:
                for it in new:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            added += len(new)
            days |= {it["t"][:10] for it in new}
    return added, days


def _item_md(it, show_codes=True):
    head = f"- **{it['t'][11:16]}** [{it['src']}] {it['title']}"
    if show_codes and it.get("codes"):
        head += f"　〔{' '.join(it['codes'])}〕"
    if it.get("tags"):
        head += f"　〔{'、'.join(it['tags'])}〕"
    if it.get("url"):
        head += f"　[原文]({it['url']})"
    body = it.get("summary") or ""
    if body:
        head += "\n" + "\n".join("  " + x for x in body.splitlines() if x.strip())
    return head


def render_day(day):
    items = [o for o in _load_month(day[:7]) if o["t"][:10] == day]
    for cat in CATS:
        its = sorted([o for o in items if o["cat"] == cat], key=lambda o: o["t"], reverse=True)
        if not its:
            continue
        p = ROOT / cat / f"{day}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {cat} · {day}（美东）\n\n{len(its)} 条，新的在前。\n\n" + "\n".join(_item_md(o) for o in its) + "\n",
                     encoding="utf-8")


def render_stocks(watch, days=30):
    today = et_now().date()
    months = sorted({(today - dt.timedelta(days=d)).strftime("%Y-%m") for d in range(0, days + 1, 10)} | {today.strftime("%Y-%m")})
    allit = [o for m in months for o in _load_month(m) if o["t"][:10] >= str(today - dt.timedelta(days=days))]
    for w in watch:
        its = sorted([o for o in allit if w["code"] in (o.get("codes") or [])], key=lambda o: o["t"], reverse=True)
        lines = [f"# {w['code']}（{w['name']}）· 信息面", "", f"最近 {days} 天 {len(its)} 条，新的在前。时间为美东。", ""]
        cur = None
        for o in its:
            if o["t"][:10] != cur:
                cur = o["t"][:10]
                lines += ["", f"## {cur}", ""]
            lines.append(f"{_item_md(o, show_codes=False)}　（{o['cat']}）")
        p = ROOT / "按股票" / f"{w['code']}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_earnings(earn, watch):
    if not earn:
        return
    p = ROOT / "财报" / "财报日历.md"
    old = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\| (\w+) \| [^|]* \| (\d{4}-\d{2}-\d{2}) \|", line)
            if m:
                old[m.group(1)] = m.group(2)
    old.update(earn)
    names = {w["code"]: w["name"] for w in watch}
    today = str(et_now().date())
    rows = sorted(old.items(), key=lambda x: x[1])
    L = ["# 研究名单财报日历", "", f"更新于 {today}（来源 Yahoo，日期可能是预估）。已经过去的排在最后。", "",
         "| 代码 | 名称 | 财报日 |", "| --- | --- | --- |"]
    L += [f"| {c} | {names.get(c, c)} | {d} |" for c, d in rows if d >= today]
    L += [f"| {c} | {names.get(c, c)} | {d} |" for c, d in rows if d < today]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L) + "\n", encoding="utf-8")


README = """# 信息面说明

- 每个类别一个文件夹，每天一个文件（美东日期），新的在前。
- `按股票/代码.md`：这只股票最近 30 天在所有类别里的条目，读一只股票先看这个。
- `财报/财报日历.md`：研究名单的下一个财报日。
- 每条的格式：**时间（美东）** [来源] 标题或摘要 〔涉及股票〕 [原文链接]。
- 情绪、真假、重要程度没有在抓取时判断，读的时候自己判断；推特帖子的链接都经过核实（Grok 盘前雷达只保留搜索结果里真实存在的帖子）。
- 历史从 2026-11-17 开始积累，之前没有存档。
"""


def collect(cfg=None, hours=24, log=print, sources=("wscn", "yahoo", "calendar", "grok", "reddit")):
    """抓一轮、归档、重写当天文件和按股票文件。返回说明文字。"""
    watch = research(cfg)
    items, errs, earn = [], [], {}
    for name in sources:
        try:
            if name == "wscn":
                items += from_wscn(watch, hours)
            elif name == "yahoo":
                its, earn = from_yahoo(watch, hours)
                items += its
            elif name == "calendar":
                items += from_calendar()
            elif name == "grok":
                items += from_grok()
            elif name == "reddit":
                items += from_reddit(cfg, watch)
        except Exception as e:
            errs.append(f"{name}：{type(e).__name__} {str(e)[:60]}")
    added, days = save(items)
    days |= {str(et_now().date())}
    for d in sorted(days):
        render_day(d)
    render_stocks(watch)
    render_earnings(earn, watch)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "说明.md").write_text(README, encoding="utf-8")
    msg = f"信息面归档：抓到 {len(items)} 条，新增 {added} 条" + (f"；{len(errs)} 个来源失败（{errs[0]}）" if errs else "")
    if log:
        log(msg)
    return msg


if __name__ == "__main__":
    try:
        import macro_report as mr
        cfg = mr.load_config()
    except Exception:
        cfg = {}
    print(collect(cfg, hours=int(sys.argv[1]) if len(sys.argv) > 1 else 24))
