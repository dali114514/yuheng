# -*- coding: utf-8 -*-
"""
开盘前快讯（机器人命令「快讯」）：按看盘顺序汇总过去 / 未来 24 小时要看的东西
  🎯 今日要点：市场环境结论、今晚重点数据、宏观主线、自选股涨跌、有消息的股票、临近财报
  📅 经济日历：未来 24 小时美国中高影响数据（Forex Factory）
  🌐 宏观要闻：过去 24 小时最重要的 5 条（华尔街见闻），每条带原文链接
  📈 自选股：只列有异动、有消息或临近财报的，每条新闻带原文链接
任何一部分拿不到数据都只跳过那一部分。
"""
import datetime as dt
import json
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
CAL_CACHE = HERE / ".calendar_cache.json"

CAL_CN = {
    "CPI": "CPI 通胀", "Core CPI": "核心 CPI", "PPI": "PPI", "Core PPI": "核心 PPI",
    "Non-Farm Employment Change": "非农就业", "Unemployment Rate": "失业率",
    "Average Hourly Earnings": "平均时薪", "ADP Non-Farm Employment Change": "ADP 就业",
    "Unemployment Claims": "初请失业金", "JOLTS Job Openings": "JOLTS 职位空缺",
    "Federal Funds Rate": "美联储利率决议", "FOMC Statement": "FOMC 声明", "FOMC Press Conference": "鲍威尔记者会",
    "FOMC Meeting Minutes": "FOMC 会议纪要", "Core PCE Price Index": "核心 PCE", "PCE Price Index": "PCE",
    "Retail Sales": "零售销售", "Core Retail Sales": "核心零售销售", "GDP": "GDP",
    "ISM Manufacturing PMI": "ISM 制造业 PMI", "ISM Services PMI": "ISM 服务业 PMI",
    "Flash Manufacturing PMI": "制造业 PMI 初值", "Flash Services PMI": "服务业 PMI 初值",
    "Prelim UoM Consumer Sentiment": "密歇根消费者信心初值", "CB Consumer Confidence": "谘商会消费者信心",
    "Crude Oil Inventories": "EIA 原油库存", "Durable Goods Orders": "耐用品订单",
    "Building Permits": "营建许可", "Housing Starts": "新屋开工", "New Home Sales": "新屋销售",
    "Existing Home Sales": "成屋销售", "Trade Balance": "贸易差额", "Empire State Manufacturing Index": "纽约联储制造业",
    "Philly Fed Manufacturing Index": "费城联储制造业", "Treasury Currency Report": "财政部汇率报告",
}


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo


def _fmt_time(t):
    """本地时间（你的 Mac 时区）+ 美东时间。"""
    loc = t.astimezone(_local_tz())
    e = t.astimezone(ET)
    return f"{loc:%m-%d %H:%M}（美东 {e:%H:%M}）"


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ================================================================ 一、经济日历
CAL_URL = "https://www.forexfactory.com/calendar"


def calendar(hours=24):
    """未来 24 小时美国中高影响数据 → [dict(t, high, name, forecast, previous)]"""
    data = None
    try:
        if CAL_CACHE.exists() and time.time() - CAL_CACHE.stat().st_mtime < 3600:
            data = json.loads(CAL_CACHE.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if data is None:
        data = _get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")   # 官方要求不要频繁请求：缓存 1 小时
        CAL_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for e in data:
        if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
            continue
        try:
            t = dt.datetime.fromisoformat(e["date"])
        except Exception:
            continue
        if not (now - dt.timedelta(minutes=30) <= t <= now + dt.timedelta(hours=hours)):
            continue
        base = re.sub(r" (m/m|y/y|q/q)$", "", e["title"]).strip()
        out.append(dict(t=t, high=e["impact"] == "High", name=CAL_CN.get(base, e["title"]),
                        forecast=e.get("forecast") or "", previous=e.get("previous") or ""))
    return sorted(out, key=lambda x: x["t"])


# ================================================================ 二、隔夜快讯
# 公司别名：用来在中文快讯和英文新闻标题里认出是哪只股票（没列到的股票用代码和中文名匹配）
ALIASES = {
    "AAPL": ["苹果公司", "苹果", "iPhone", "库克", "Apple"],
    "MSFT": ["微软", "Azure", "纳德拉", "Microsoft"],
    "GOOGL": ["谷歌", "Alphabet", "Gemini", "YouTube", "Waymo", "Google"],
    "GOOG": ["谷歌", "Alphabet", "Google"],
    "META": ["Meta", "脸书", "Facebook", "扎克伯格", "Instagram", "WhatsApp"],
    "AMZN": ["亚马逊", "AWS", "贝索斯", "贾西", "Amazon"],
    "NFLX": ["奈飞", "网飞", "Netflix"],
    "NVDA": ["英伟达", "黄仁勋", "Nvidia", "NVIDIA", "Blackwell", "Rubin"],
    "AMD": ["AMD", "超威", "超微半导体", "苏姿丰"],
    "AVGO": ["博通", "Broadcom"],
    "INTC": ["英特尔", "Intel"],
    "ARM": ["Arm Holdings", "ARM公司", "安谋"],
    "MU": ["美光", "Micron"],
    "SNDK": ["闪迪", "SanDisk", "Sandisk"],
    "WDC": ["西部数据", "Western Digital"],
    "SKHY": ["SK海力士", "海力士", "SK Hynix", "Hynix"],
    "LITE": ["Lumentum"],
    "NBIS": ["Nebius"],
    "NOK": ["诺基亚", "Nokia"],
    "TSLA": ["特斯拉", "马斯克", "Tesla", "Cybertruck", "Cybercab", "Robotaxi", "FSD", "Optimus", "擎天柱",
             "Model 3", "Model Y", "Model S", "Model X", "Model Q", "Megapack", "Semi"],
    "SPCX": ["SpaceX", "太空探索", "星链", "Starlink", "星舰", "Starship"],
    "KO": ["可口可乐", "Coca-Cola", "Coca Cola"],
    "ROK": ["罗克韦尔", "Rockwell"],
    "PLTR": ["Palantir"], "FUTU": ["富途"], "COIN": ["Coinbase"],
}
MACRO_WORDS = ["美联储", "鲍威尔", "FOMC", "降息", "加息", "利率", "非农", "CPI", "PCE", "通胀", "就业", "失业",
               "关税", "特朗普", "白宫", "财政部", "美债", "国债收益率", "GDP", "衰退", "停摆", "制裁", "出口管制",
               "地缘", "原油", "OPEC", "美元指数", "日本央行", "欧洲央行"]


def _is_etf(w):
    return w.get("etf") or "ETF" in (w.get("name") or "").upper() or w["code"] in ("SPY", "QQQ", "SOXX", "TLT")


def build_matchers(watch):
    """每只（非 ETF）自选股 → 一个正则，同时认中文名、英文名和代码。"""
    out = {}
    for w in watch:
        if _is_etf(w):
            continue
        code = w["code"]
        words = list(dict.fromkeys(ALIASES.get(code, []) + [w.get("name") or ""]))
        parts = [(r"(?<![A-Za-z])" + re.escape(x) + r"(?![A-Za-z])") if x.isascii() else re.escape(x)
                 for x in words if x and len(x) >= 2]
        if len(code) >= 3 or code in ALIASES:     # 2 字母代码（KO、MU）只在列了别名时才单独匹配代码
            parts.append(r"(?<![A-Za-z])" + re.escape(code) + r"(?![A-Za-z])")
        if parts:
            out[code] = (w.get("name") or code, re.compile("|".join(parts), re.I if code not in ("ARM", "KO", "MU") else 0))
    return out


def _fetch_lives(hours):
    cutoff = time.time() - hours * 3600
    items, seen = [], set()
    for ch in ("global-channel", "us-stock-channel"):
        cursor = None
        for _ in range(8):                                   # 每个频道最多翻 8 页，每页 100 条
            url = f"https://api-one-wscn.awtmt.com/apiv1/content/lives?channel={ch}&limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            d = (_get_json(url).get("data") or {})
            page = d.get("items") or []
            for i in page:
                if i.get("id") not in seen and i.get("display_time", 0) >= cutoff:
                    seen.add(i.get("id"))
                    items.append(i)
            cursor = d.get("next_cursor")
            if not page or not cursor or page[-1].get("display_time", 0) < cutoff:
                break
    items.sort(key=lambda i: -i.get("display_time", 0))
    return items


def flash(watch, hours=24, per_stock=3, macro_cap=5):
    """返回 (按股票分组的快讯 {code: [item]}, 宏观要闻 [item])；item = dict(t, text, url)。"""
    matchers = build_matchers(watch)
    by_stock, macro = {}, []
    for i in _fetch_lives(hours):
        text = (i.get("title") or i.get("content_text") or "").strip()
        if not text:
            continue
        full = text + " " + (i.get("content_text") or "")
        item = dict(t=dt.datetime.fromtimestamp(i["display_time"], dt.timezone.utc).astimezone(_local_tz()),
                    text=text, url=i.get("uri") or "", src="华尔街见闻")
        who = [c for c, (_, rx) in matchers.items() if rx.search(full)]
        if who:
            for c in who[:3]:
                if len(by_stock.setdefault(c, [])) < per_stock:
                    by_stock[c].append(item)
        elif (i.get("score") or 0) >= 2:
            macro.append((sum(w in full for w in MACRO_WORDS), i["display_time"], item))
    macro.sort(key=lambda x: (-x[0], -x[1]))
    return by_stock, [m[2] for m in sorted(macro[:macro_cap], key=lambda x: -x[1])]


# ================================================================ 三、自选股
def _session():
    """美东时间判断当前时段。夜盘 = 周日到周四 20:00 至次日 4:00（moomoo 美股夜盘）。"""
    n = dt.datetime.now(ET)
    m, wd = n.hour * 60 + n.minute, n.weekday()          # 周一 = 0 … 周日 = 6
    if (m >= 20 * 60 and wd in (6, 0, 1, 2, 3)) or (m < 4 * 60 and wd in (0, 1, 2, 3, 4)):
        return "夜盘"
    if wd >= 5:
        return "休市"
    if 4 * 60 <= m < 9 * 60 + 30:
        return "盘前"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "盘中"
    if 16 * 60 <= m < 20 * 60:
        return "盘后"
    return "休市"


def _num(v):
    try:
        f = float(v)
        return None if f != f or f == 0 else f
    except Exception:
        return None


def yahoo_info(code, hours=24):
    """财报日 + 近 24 小时新闻（带链接）。拿不到就返回空。"""
    out = {"earnings": None, "news": []}
    try:
        import yfinance as yf
        tk = yf.Ticker(code)
    except Exception:
        return out
    try:
        cal = tk.calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            ed = ed[0] if isinstance(ed, (list, tuple)) else ed
            out["earnings"] = dt.date.fromisoformat(str(ed)[:10])
    except Exception:
        pass
    try:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        for n in (tk.news or [])[:20]:
            c = n.get("content", n)
            title = c.get("title")
            when = c.get("pubDate") or c.get("providerPublishTime")
            if isinstance(when, (int, float)):
                ts = dt.datetime.fromtimestamp(when, dt.timezone.utc)
            elif when:
                ts = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            else:
                ts = None
            url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                   or c.get("link") or "")
            if title and url and (ts is None or ts >= cutoff):
                out["news"].append(dict(t=ts.astimezone(_local_tz()) if ts else None, text=title, url=url,
                                        src=(c.get("provider") or {}).get("displayName") or c.get("publisher") or "Yahoo"))
    except Exception:
        pass
    return out


def watch_quotes(cfg, log=lambda *a: None):
    import stocks
    params, defaults, lines, profiles, order = stocks.load_settings(log)
    mm_cfg = cfg.get("moomoo", {})
    mm = stocks.Moomoo(mm_cfg, log)
    watch = mm.watchlist(mm_cfg.get("自选分组", "全部")) if mm.ok else None
    src = "moomoo"
    if not watch:
        watch = [dict(code=c, name=params[c]["名称"], etf=False) for c in order]
        src = "Yahoo"
    for w in watch:
        w["sector"] = (params.get(w["code"]) or {}).get("板块") or ""
    sess = _session()
    quotes = {}
    if mm.ok and watch:
        try:
            ret, df = mm.ctx.get_market_snapshot(["US." + w["code"] for w in watch])
            if ret == mm.ft.RET_OK:
                for _, r in df.iterrows():
                    code = str(r["code"]).replace("US.", "")
                    prev = _num(r.get("prev_close_price"))
                    last = _num(r.get("last_price"))
                    q = dict(last=last, chg=(last / prev - 1) * 100 if last and prev else None)
                    for key, cn in (("pre", "盘前"), ("after", "盘后"), ("overnight", "夜盘")):
                        px = _num(r.get(f"{key}_price"))
                        if px:
                            q[cn] = (px, _num(r.get(f"{key}_change_rate")))
                    quotes[code] = q
        except Exception as e:
            log(f"快照失败：{e}")
    mm.close()

    def yahoo_q(code):
        try:
            import yfinance as yf
            info = yf.Ticker(code).info
            q = dict(last=_num(info.get("regularMarketPrice")), chg=_num(info.get("regularMarketChangePercent")))
            if _num(info.get("preMarketPrice")):
                q["盘前"] = (_num(info.get("preMarketPrice")), _num(info.get("preMarketChangePercent")))
            if _num(info.get("postMarketPrice")):
                q["盘后"] = (_num(info.get("postMarketPrice")), _num(info.get("postMarketChangePercent")))
            return code, q
        except Exception:
            return code, {}

    codes = [w["code"] for w in watch]
    with ThreadPoolExecutor(8) as ex:
        for code, q in ex.map(yahoo_q, [c for c in codes if c not in quotes]):
            quotes[code] = q
        extras = dict(zip(codes, ex.map(yahoo_info, codes)))
    return watch, quotes, extras, src, sess


def _move(q, sess):
    """当前时段最有意义的涨跌：夜盘 / 盘前 / 盘后价优先，否则常规时段。"""
    order = {"夜盘": ("夜盘", "盘后"), "盘前": ("盘前", "夜盘"), "盘后": ("盘后",)}.get(sess, ())
    for k in order:
        ext = q.get(k)
        if ext and ext[1] is not None:
            return ext[1], k, ext[0]
    if q.get("chg") is not None and q.get("last"):
        return q["chg"], "现价" if sess == "盘中" else "收盘", q["last"]
    return None, "", None


def _link(item, n=60):
    """Discord 带链接的标题；<> 防止每条链接都展开大预览图。"""
    txt = _clip(item["text"], n).replace("[", "【").replace("]", "】")
    return f"[{txt}](<{item['url']}>)" if item.get("url") else txt


def macro_view(cfg, log):
    """用玉衡5.0自己的六指标框架给一句市场环境结论。"""
    import macro_report as mr
    data, _ = mr.fetch_data(cfg)
    A = mr.analyze(data, cfg)
    top = [a[1] for a in A["alerts"] if a[0] == "高"][:2] or [a[1] for a in A["alerts"] if a[0] == "中"][:1]
    return f"{A['verdict']}（综合分 {A['score']:+d}）" + (f"；{'；'.join(top)}" if top else ""), A


# ================================================================ 汇总
def build(cfg, log=lambda *a: None):
    """返回若干段文字（每段 ≤1900 字），可直接发到 Discord。"""
    now = dt.datetime.now().astimezone()
    sess = _session()
    with ThreadPoolExecutor(3) as ex:
        f_cal = ex.submit(calendar)
        f_watch = ex.submit(watch_quotes, cfg, log)
        f_macro = ex.submit(macro_view, cfg, log)
        try:
            cal = f_cal.result()
        except Exception as e:
            cal = None
            log(f"日历失败：{e!r}")
        try:
            watch, quotes, extras, src, sess = f_watch.result()
        except Exception as e:
            log(f"自选股失败：{e!r}")
            watch, quotes, extras, src = [], {}, {}, "—"
        try:
            env, A = f_macro.result()
        except Exception as e:
            env, A = None, None
            log(f"宏观框架失败：{e!r}")
    try:
        by_stock, macro = flash(watch)
        flash_err = None
    except Exception as e:
        by_stock, macro, flash_err = {}, [], type(e).__name__

    names = {w["code"]: w["name"] for w in watch}
    today = dt.date.today()

    # ---- 每只股票：涨跌 + 消息 + 财报
    rows = []
    for w in watch:
        c = w["code"]
        mv, label, px = _move(quotes.get(c, {}), sess)
        news = list(by_stock.get(c, []))
        rx = build_matchers([w]).get(c, (None, None))[1]
        y = [n for n in (extras.get(c) or {}).get("news", []) if rx is None or rx.search(n["text"])]
        news += y[: max(0, 2 - len(news))]
        ed = (extras.get(c) or {}).get("earnings")
        er = ed if ed and 0 <= (ed - today).days <= 7 else None
        try:
            import newsflow
            for n in news:
                n["s"] = newsflow.classify(n["text"])["s"]
        except Exception:
            pass
        tone = sum(n.get("s", 0) for n in news)
        rows.append(dict(code=c, name=w["name"], mv=mv, label=label, px=px, news=news[:2], er=er, etf=_is_etf(w),
                         tone=tone, sector=w.get("sector") or ""))
    import sectors as SEC
    groups = SEC.group([(r["sector"], r["mv"], r) for r in rows])
    th = {k: v for k, v in (cfg.get("盘中监测") or {}).items() if str(k).startswith("板块异动")}
    anos = {a["name"]: a for a in SEC.anomalies(groups, th)}
    moved = [r for r in rows if r["mv"] is not None]
    up = sum(r["mv"] > 0 for r in moved)
    dn = sum(r["mv"] < 0 for r in moved)

    # ---- 🎯 今日要点（结论）
    pts = []
    if env:
        pts.append(f"**市场环境**：{env}")
    hi = [e for e in (cal or []) if e["high"]]
    if hi:
        pts.append("**今晚重点数据**：" + "，".join(
            f"{e['t'].astimezone(_local_tz()):%H:%M} {e['name']}" + (f"（预期 {e['forecast']}）" if e["forecast"] else "")
            for e in hi[:3]))
    elif cal is not None:
        pts.append("**今晚重点数据**：没有高影响数据" + (f"，中等影响 {len(cal)} 项" if cal else ""))
    if macro:
        key = max(macro, key=lambda m: sum(w in m["text"] for w in MACRO_WORDS))
        pts.append(f"**宏观主线**：{_link(key, 50)}")
    if moved:
        mlabel = moved[0]["label"] if moved else ""
        srt = sorted(moved, key=lambda r: r["mv"])
        lead = f"领涨 {srt[-1]['name']} {srt[-1]['mv']:+.1f}%" if srt[-1]["mv"] > 0 else ""
        lag = f"领跌 {srt[0]['name']} {srt[0]['mv']:+.1f}%" if srt[0]["mv"] < 0 else ""
        pts.append(f"**自选股{mlabel}**：{up} 涨 / {dn} 跌" + (f"；{'，'.join(x for x in (lead, lag) if x)}" if lead or lag else ""))
    ranked = [g for g in groups if g["avg"] is not None and not SEC.is_index(g["name"]) and g["name"] != "未分类"]
    if len(ranked) >= 2:
        hi_g, lo_g = max(ranked, key=lambda g: g["avg"]), min(ranked, key=lambda g: g["avg"])
        pts.append(f"**板块**：最强 {SEC.label(hi_g['name'])} {hi_g['avg']:+.1f}%，最弱 {SEC.label(lo_g['name'])} {lo_g['avg']:+.1f}%")
    if anos:
        pts.append("**板块异动**：" + "；".join(SEC.anomaly_text(a) for a in anos.values()))
    hot = [r for r in rows if r["news"] and not r["etf"]]
    if hot:
        arrow = lambda r: "↑" if r["tone"] > 0 else "↓" if r["tone"] < 0 else ""
        pts.append("**有消息**：" + "、".join(r["name"] + arrow(r) for r in sorted(hot, key=lambda r: -abs(r["mv"] or 0))[:6])
                   + "（↑利好 ↓利空）")
    ers = [r for r in rows if r["er"]]
    if ers:
        pts.append("**财报临近**：" + "、".join(f"{r['name']} {r['er']:%m-%d}" for r in sorted(ers, key=lambda r: r["er"])))

    parts = [f"**📰 开盘前快讯**　{now:%m-%d %H:%M}（美东 {now.astimezone(ET):%H:%M}，{sess}）", "",
             "**🎯 今日要点**"] + [f"· {p}" for p in pts] + [""]
    try:
        import grokscan
        g = grokscan.brief_section()
        if g:
            parts += g + [""]
    except Exception:
        pass

    # ---- 📅 经济日历
    parts.append(f"**📅 经济日历**（未来 24 小时，你的本地时间）　[完整日历](<{CAL_URL}>)")
    if cal is None:
        parts.append("⚠ 暂时拿不到")
    elif not cal:
        parts.append("无中高影响的美国数据")
    for e in cal or []:
        fp = " / ".join(x for x in (f"预期 {e['forecast']}" if e["forecast"] else "",
                                    f"前值 {e['previous']}" if e["previous"] else "") if x)
        parts.append(f"{'🔴' if e['high'] else '🟠'} `{e['t'].astimezone(_local_tz()):%m-%d %H:%M}` {e['name']}" + (f"　{fp}" if fp else ""))
    parts.append("")

    # ---- 🌐 宏观要闻
    parts.append("**🌐 宏观要闻**（过去 24 小时）")
    if flash_err:
        parts.append(f"⚠ 暂时拿不到（{flash_err}）")
    for m in macro:
        parts.append(f"· `{m['t']:%H:%M}` {_link(m, 70)}")
    if not macro and not flash_err:
        parts.append("没有特别重要的宏观快讯")
    parts.append("")

    # ---- 📈 自选股：按板块分组；每组一行汇总，只展开有异动 / 有消息 / 临近财报的个股
    parts.append(f"**📈 自选股**（按板块，行情：{src}）")
    for g in groups:
        show = [r for r in g["members"] if r["news"] or r["er"] or (r["mv"] is not None and abs(r["mv"]) >= 2)]
        show.sort(key=lambda r: (-(abs(r["mv"]) if r["mv"] is not None else 0)))
        avg = f"平均 {g['avg']:+.1f}%（{g['ups']}↑ {g['downs']}↓）" if g["avg"] is not None else "无报价"
        tag = ("　⚡集体" + ("大涨" if anos[g["name"]]["dir"] == "涨" else "大跌")) if g["name"] in anos else ""
        quiet = "" if show else "　· 平稳、无新消息"
        parts.append(f"**{SEC.label(g['name'])}**　{avg}{tag}{quiet}")
        _stock_lines(parts, show)
        if show and len(show) < g["n"]:
            parts.append(f"　　其余 {g['n'] - len(show)} 只平稳")
    parts += ["", "_来源：Forex Factory · 华尔街见闻 · moomoo / Yahoo。仅供参考，不构成投资建议。_"]
    return _split(parts)


def _stock_lines(parts, show):
    for r in show:
        icon = "🔺" if (r["mv"] or 0) > 0 else "🔻" if (r["mv"] or 0) < 0 else "▫️"
        mv = f"{r['label']} {r['mv']:+.2f}%" if r["mv"] is not None else "无报价"
        tags = (" ⚡" if r["mv"] is not None and abs(r["mv"]) >= 3 else "") + (f"　📅财报 {r['er']:%m-%d}" if r["er"] else "")
        tone = {1: "　📰利好", -1: "　📰利空"}.get((r["tone"] > 0) - (r["tone"] < 0), "") if r["news"] else ""
        clash = " ⚠️与走势相反" if r["news"] and r["mv"] is not None and r["tone"] * r["mv"] < 0 and abs(r["mv"]) >= 1 else ""
        parts.append(f"{icon} **{r['name']}** {r['code']}　{mv}{tags}{tone}{clash}")
        for n in r["news"]:
            mark = {1: "↑", -1: "↓"}.get(n.get("s", 0), "·")
            parts.append(f"　└ {mark} {_link(n, 60)}")


def _split(parts):
    msgs, cur = [], ""
    for line in parts:
        if len(cur) + len(line) + 1 > 1900:
            msgs.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        msgs.append(cur)
    return msgs


if __name__ == "__main__":
    import macro_report as mr
    for m in build(mr.load_config(), log=print):
        print(m)
        print("-" * 40)
