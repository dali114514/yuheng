# -*- coding: utf-8 -*-
"""
Reddit 散户监测（盘前，像盘中监测一样开着盯，有异动才提醒，推送到 #散户）

热度：ApeWisdom（免费，统计 r/wallstreetbets、r/options、r/stocks 每只股票的提及次数、点赞、排名，并给出 24 小时前的数值）
方向：只有触发提醒的股票，才让 Grok 用网页搜索（限定 reddit.com）去读帖子，判断散户押 call 还是 put、情绪是否极端

提醒类型（同一只股票同一类每天只发一次，升级时再发）：
  🔥 热度飙升：自选股提及次数是 24 小时前的 3 / 5 / 10 倍（且达到最少提及数），或排名冲进前 10
  🐎 新黑马：自选股以外的股票冲进 r/wallstreetbets 前 20，提及次数暴涨
标签（结合 moomoo 盘前涨跌）：共振 / 过热 / 逼空风险 / 观察
"""
import datetime as dt
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
CACHE = HERE / ".reddit_cache.json"
STATE = HERE / ".reddit_on"
APE = "https://apewisdom.io/api/v1.0/filter/{f}/page/1"
UA = "Mozilla/5.0 (Macintosh) ZhuanQianXiaoZhuShou/1.0"

DEFAULT = {
    "启用": True, "扫描间隔_分钟": 5, "运行时段_美东": ["04:00", "09:30"],
    "版块": ["wallstreetbets", "options", "stocks"],
    "自选股_最少提及": 20, "自选股_飙升倍数": [3, 5, 10], "排名跃升_进入前": 10,
    "黑马_进入前": 20, "黑马_最少提及": 50, "黑马_飙升倍数": 5,
    "读帖判断方向": True, "每日费用上限美元": 2,
}
SUB_CN = {"wallstreetbets": "WSB", "options": "r/options", "stocks": "r/stocks"}


def settings(cfg):
    s = dict(DEFAULT)
    s.update(cfg.get("Reddit散户") or {})
    return s


def is_on():
    return STATE.exists()


def set_on(on):
    if on:
        STATE.write_text(dt.datetime.now().isoformat())
    else:
        STATE.unlink(missing_ok=True)


def in_window(s, now=None):
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:
        return False
    a, b = [dt.time(*map(int, x.split(":"))) for x in s["运行时段_美东"]]
    return a <= now.time() < b


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c):
    try:
        CACHE.write_text(json.dumps(c, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


# ================================================================ 热度
def fetch_sub(f):
    req = urllib.request.Request(APE.format(f=f), headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_ctx()) as r:
        return json.loads(r.read().decode("utf-8")).get("results") or []


def heat(s):
    """{ticker: {"name", "subs": {sub: {rank, mentions, m24, r24, upvotes}}, "mentions", "m24", "ratio", "wsb_rank", "wsb_r24"}}"""
    out = {}
    for f in s["版块"]:
        for r in fetch_sub(f):
            t = str(r.get("ticker") or "").upper()
            if not t:
                continue
            d = out.setdefault(t, dict(name=str(r.get("name") or t).replace("&amp;", "&"), subs={}))
            d["subs"][f] = dict(rank=int(r.get("rank") or 0), mentions=int(r.get("mentions") or 0),
                                m24=int(r.get("mentions_24h_ago") or 0), r24=int(r.get("rank_24h_ago") or 0),
                                upvotes=int(r.get("upvotes") or 0))
    for t, d in out.items():
        d["mentions"] = sum(v["mentions"] for v in d["subs"].values())
        d["m24"] = sum(v["m24"] for v in d["subs"].values())
        d["ratio"] = d["mentions"] / max(d["m24"], 1)
        w = d["subs"].get("wallstreetbets") or {}
        d["wsb_rank"], d["wsb_r24"] = w.get("rank"), w.get("r24")
    return out


def watchlist():
    try:
        import stocks
        params, _, _, _, order = stocks.load_settings(lambda *a: None)
        return {c: params[c]["名称"] for c in order}
    except Exception:
        return {}


# ================================================================ 触发
def triggers(H, watch, s):
    """返回 [dict(kind, ticker, level, why)]"""
    out = []
    tiers = sorted(float(x) for x in s["自选股_飙升倍数"])
    for t in watch:
        d = H.get(t)
        if not d:
            continue
        lvl = 0
        if d["mentions"] >= int(s["自选股_最少提及"]):
            lvl = sum(1 for x in tiers if d["ratio"] >= x)
        jump = (d["wsb_rank"] and d["wsb_rank"] <= int(s["排名跃升_进入前"]) and (not d["wsb_r24"] or d["wsb_r24"] > int(s["排名跃升_进入前"])))
        if lvl or jump:
            why = []
            if lvl:
                why.append(f"提及 {d['mentions']} 次，是 24 小时前（{d['m24']}）的 {d['ratio']:.1f} 倍")
            if jump:
                why.append(f"WSB 排名 {d['wsb_r24'] or '榜外'} → {d['wsb_rank']}")
            out.append(dict(kind="热度飙升", ticker=t, level=max(lvl, 1 if jump else 0), why="；".join(why)))
    for t, d in H.items():
        if t in watch or not d["wsb_rank"]:
            continue
        if d["wsb_rank"] <= int(s["黑马_进入前"]) and d["mentions"] >= int(s["黑马_最少提及"]) and d["ratio"] >= float(s["黑马_飙升倍数"]):
            out.append(dict(kind="新黑马", ticker=t, level=1,
                            why=f"WSB 第 {d['wsb_rank']}（24 小时前 {d['wsb_r24'] or '榜外'}），提及 {d['mentions']} 次，{d['ratio']:.1f} 倍"))
    return out


# ================================================================ Grok 读帖
def read_posts(cfg, ticker, name, log=lambda *a: None):
    """返回 dict(tone, calls_puts, mood, summary, posts[{url,title}], cost) 或 None。"""
    g = cfg.get("Grok盘前") or {}
    key = str(g.get("API密钥") or "").strip()
    if not key:
        return None
    since = (dt.datetime.now(ET) - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    prompt = f"""在 Reddit（r/wallstreetbets、r/options、r/stocks）上找自 {since} 美东时间以来讨论 {ticker}（{name}）的帖子和评论，
重点看 WSB 的 Daily Discussion、"What Are Your Moves Tomorrow" 和热门帖。判断散户情绪，只输出一个 JSON：
{{"tone":"偏多|偏空|分歧","calls_puts":"以 call 为主|以 put 为主|多空都有|看不出",
"mood":"正常|亢奋|恐慌","yolo":"有没有人晒大额仓位/收益截图，一句话",
"summary":"散户在讨论什么、为什么看多或看空（中文，不超过80字）",
"posts":[{{"url":"reddit 帖子链接","title":"帖子标题中文意思（不超过30字）"}}]}}
posts 最多 3 条，必须是这次搜索结果里真实存在的 reddit.com 链接；讨论太少就把 tone 写成 "分歧"、summary 写明样本不足。"""
    import grokscan as G
    tools = [{"type": "web_search", "filters": {"allowed_domains": ["reddit.com"]}}]
    data, last = None, None
    for m in dict.fromkeys([x for x in (g.get("搜索模型"), g.get("模型") or "grok-4.7") if x]):
        try:
            data = G._post({"API密钥": key}, {"model": m, "input": [{"role": "user", "content": prompt}], "tools": tools},
                           timeout=240)
            break
        except Exception as e:
            last = e
            if "429" in str(e):
                break
    if data is None:
        raise last
    text, _, usage = G.parse(data)
    cites = set()
    for o in data.get("output") or []:
        for c in (o.get("content") or []) if o.get("type") == "message" else []:
            for a in c.get("annotations") or []:
                if a.get("url"):
                    cites.add(a["url"].split("?")[0].rstrip("/"))
    obj = G.extract_json(text)
    posts = []
    for p in obj.get("posts") or []:
        u = str(p.get("url") or "").split("?")[0].rstrip("/")
        if "reddit.com" in u and (not cites or u in cites):
            posts.append(dict(url=u, title=str(p.get("title") or "")[:40]))
    calls = sum(1 for o in data.get("output") or [] if "search" in str(o.get("type", "")))
    obj["posts"] = posts
    obj["cost"] = G.est_cost(usage) + max(calls, 1) * 0.005
    return obj


# ================================================================ 盘前涨跌
def premarket(cfg, tickers):
    try:
        import stocks
        mm = stocks.Moomoo(cfg.get("moomoo", {}), lambda *a: None)
        if not mm.ok:
            return {}
        try:
            ret, df = mm.ctx.get_market_snapshot(["US." + t for t in tickers])
            if ret != mm.ft.RET_OK:
                return {}
            out = {}
            for _, r in df.iterrows():
                try:
                    out[str(r["code"]).replace("US.", "")] = float(r.get("pre_change_rate"))
                except Exception:
                    pass
            return out
        finally:
            mm.close()
    except Exception:
        return {}


def news_side(ticker):
    """读 Grok 盘前雷达的缓存（不花钱）：这只股票的看涨 / 看跌 / 公告数。"""
    try:
        import grokscan as G
        r = G.latest()
        if not r:
            return None
        sd = G.sides(r["posts"]).get(ticker)
        if not sd:
            return None
        up = len(sd["看涨"]) + sum(1 for e in sd["公告"] if e["stance"] == "看涨")
        dn = len(sd["看跌"]) + sum(1 for e in sd["公告"] if e["stance"] == "看跌")
        return dict(up=up, dn=dn)
    except Exception:
        return None


def direction(ticker, read, pm):
    """永远给一个大方向：盘前价格优先（开盘时价格才是真相），散户情绪和消息面用来定信心。"""
    sgn = lambda x: (x > 0) - (x < 0)
    p = sgn(pm) if pm is not None and abs(pm) >= 1 else 0
    tone = (read or {}).get("tone")
    r = {"偏多": 1, "偏空": -1}.get(tone, 0)
    ns = news_side(ticker)
    n = sgn(ns["up"] - ns["dn"]) if ns else 0
    why = []
    if pm is not None:
        why.append(f"盘前 {pm:+.2f}%" + ("" if p else "（幅度小，不定方向）"))
    why.append(f"散户{tone}" if tone else "散户情绪未读到")
    if ns:
        why.append(f"Grok 消息面 看涨 {ns['up']} / 看跌 {ns['dn']}")
    if p:
        final = p
        agree = [x for x in (r, n) if x]
        conf = "高" if agree and all(x == p for x in agree) and len(agree) == 2 else \
               "中" if all(x == p for x in agree) else "低"
        if r and r != p:
            why.append("散户逆着价格（" + ("抄底" if p < 0 else "摸顶/做空") + "），留意开盘后的拉锯")
    elif r and n and r != n:
        final, conf = 0, "低"
    else:
        final = r or n
        conf = "中" if (r and n) else "低"
    word = {1: "🟢 偏多", -1: "🔴 偏空", 0: "🟡 不明"}[final]
    return word, conf, "；".join(why)


def label(trig, d, read, pm):
    tone = (read or {}).get("tone")
    mood = (read or {}).get("mood")
    if tone is None:
        if pm is not None and abs(pm) >= 2 and d.get("ratio", 0) >= 3:
            return ("过热" if pm > 0 else "观察"), (
                f"散户讨论暴增 {d['ratio']:.0f} 倍、盘前{'大涨' if pm > 0 else '大跌'} {pm:+.1f}%：大家都在盯它，"
                + ("追高要等开盘确认，防高开低走" if pm > 0 else "开盘容易先惯性下杀，抄底要等止跌确认") + "（散户方向未读到）")
        return "观察", "散户方向未读到，先看价格"
    if pm is None:
        return "观察", "没有盘前价格"
    if tone == "偏多" and mood == "亢奋" and d["ratio"] >= 5 and pm >= 2:
        return "过热", "散户一片亢奋、热度暴涨，股价已大幅高开：容易高开低走，追多要等确认"
    if tone == "偏多" and pm >= 1:
        return "共振", "散户看多且盘前已经上涨：方向一致，等开盘第一根 5 分钟K确认"
    if tone == "偏空" and pm <= -1:
        return "共振", "散户看空且盘前已经下跌：方向一致"
    if tone == "偏空" and pm >= 0:
        return "逼空风险", "散户押 put 为主，股价却跌不下去：提防逼空"
    if tone == "偏多" and pm <= -1:
        return "观察", "散户在逆势抄底：开盘先看下杀能否止住，止跌确认前不急着接"
    if tone == "偏空" and pm < 1 and pm > -1:
        return "观察", "散户看空但价格没动：等价格表态"
    return "观察", "情绪和盘前走势还没形成明确组合"


# ================================================================ 一轮扫描
def scan(cfg, log=lambda *a: None, force_read=None):
    """返回 [提醒 dict]（已去重）。force_read：手动查询时直接读帖的代码。"""
    s = settings(cfg)
    now = dt.datetime.now(ET)
    day = str(now.date())
    cache = load_cache()
    st = cache.get(day) or dict(alerted={}, spent=0.0, snaps=[])
    H = heat(s)
    watch = watchlist()
    st["snaps"] = (st.get("snaps") or [])[-60:] + [dict(t=now.isoformat(), top=[(t, d["mentions"]) for t, d in
                                                                              sorted(H.items(), key=lambda x: -x[1]["mentions"])[:30]])]
    trig = triggers(H, watch, s)
    new = []
    for tr in trig:
        k = f"{tr['kind']}|{tr['ticker']}"
        if st["alerted"].get(k, 0) >= tr["level"]:
            continue
        st["alerted"][k] = tr["level"]
        new.append(tr)
    if new:
        pm = premarket(cfg, [t["ticker"] for t in new])
        for tr in new:
            d = H[tr["ticker"]]
            tr["heat"] = d
            tr["name"] = watch.get(tr["ticker"]) or d["name"]
            tr["pm"] = pm.get(tr["ticker"])
            tr["read"] = None
            if s.get("读帖判断方向") and st["spent"] < float(s["每日费用上限美元"]):
                try:
                    tr["read"] = read_posts(cfg, tr["ticker"], tr["name"], log)
                    if tr["read"]:
                        st["spent"] += tr["read"].get("cost", 0)
                except Exception as e:
                    tr["read_err"] = str(e)[:100]
            elif s.get("读帖判断方向"):
                tr["read_err"] = f"已达每日费用上限 ${float(s['每日费用上限美元']):.2f}"
            tr["label"], tr["label_why"] = label(tr, d, tr["read"], tr["pm"])
            if tr["read"]:                                  # 记下来给信息面归档（news.py）
                r = tr["read"]
                st.setdefault("log", []).append(dict(t=now.isoformat(timespec="minutes"), ticker=tr["ticker"],
                                                     tone=r.get("tone"), calls_puts=r.get("calls_puts"), mood=r.get("mood"),
                                                     summary=r.get("summary"), posts=r.get("posts") or []))
    cache = {k: v for k, v in cache.items() if k >= str(now.date() - dt.timedelta(days=7))}
    cache[day] = st
    cache["last_heat"] = dict(t=now.isoformat(), H=H)
    save_cache(cache)
    return new


def fmt_alert(tr):
    d = tr["heat"]
    icon = {"热度飙升": "🔥", "新黑马": "🐎"}[tr["kind"]]
    tag = {"共振": "🟢 共振", "过热": "🟠 过热", "逼空风险": "🟣 逼空风险", "观察": "⚪ 观察"}[tr["label"]]
    lvl = "↑" * tr["level"] if tr["kind"] == "热度飙升" else ""
    subs = "　".join(f"{SUB_CN.get(k, k)} #{v['rank']}（{v['mentions']} 次）" for k, v in d["subs"].items())
    L = [f"{icon} **{tr['kind']}{lvl}｜{tr['name']} {tr['ticker']}**　{tag}",
         f"· {tr['why']}", f"· {subs}"]
    if tr.get("pm") is not None:
        L.append(f"· 盘前 {tr['pm']:+.2f}%")
    r = tr.get("read")
    if r:
        L.append(f"· 🦍 散户：**{r.get('tone', '—')}**，{r.get('calls_puts', '—')}，情绪{r.get('mood', '—')}"
                 + (f"；{r['yolo']}" if r.get("yolo") else ""))
        if r.get("summary"):
            L.append(f"　{r['summary']}")
        for p in (r.get("posts") or [])[:3]:
            L.append(f"　└ [{p['title'] or '帖子'}](<{p['url']}>)")
    elif tr.get("read_err"):
        L.append(f"· 没有读帖：{tr['read_err']}")
    w, conf, why = direction(tr["ticker"], tr.get("read"), tr.get("pm"))
    L.insert(1, f"🧭 **大方向：{w}**（信心 {conf}）— {why}")
    L.append(f"· 🏷 {tr['label_why']}")
    return "\n".join(L)


def snapshot_text(cfg, ticker=None, log=lambda *a: None):
    """手动查看：「散户」= 热度榜 + 自选股；「散户 TSLA」= 单只股票读帖。"""
    s = settings(cfg)
    H = heat(s)
    watch = watchlist()
    now = dt.datetime.now(ET)
    if ticker:
        t = ticker.upper()
        if not t.isascii():                       # 写的是中文名：按自选股名称找代码（「博通」→ AVGO）
            hit = [c for c, n in watch.items() if ticker in n or n in ticker]
            if not hit:
                return [f"没认出「{ticker}」：请写股票代码（比如 散户 AVGO），或者自选股里的名称。"]
            t = hit[0]
        d = H.get(t)
        name = watch.get(t) or (d or {}).get("name") or t
        tr = dict(kind="热度飙升", ticker=t, level=0, name=name,
                  why=(f"提及 {d['mentions']} 次，24 小时前 {d['m24']} 次（{d['ratio']:.1f} 倍）" if d else "三个版块的榜单里都没有它（讨论很少）"),
                  heat=d or dict(subs={}, mentions=0, m24=0, ratio=0), pm=premarket(cfg, [t]).get(t))
        try:
            tr["read"] = read_posts(cfg, t, name, log)
        except Exception as e:
            tr["read"], tr["read_err"] = None, str(e)[:100]
        tr["label"], tr["label_why"] = label(tr, tr["heat"], tr["read"], tr["pm"])
        return [fmt_alert(tr).replace("🔥 **热度飙升｜", "🔎 **散户情绪｜")]
    L = [f"**🦍 Reddit 散户热度**　{now:%m-%d %H:%M} 美东（数据：ApeWisdom，三个版块合计）", "", "**🔝 热度前 10**"]
    for t, d in sorted(H.items(), key=lambda x: -x[1]["mentions"])[:10]:
        mark = "⭐" if t in watch else ""
        L.append(f"· {mark}**{t}** {d['mentions']} 次（{d['ratio']:.1f}×）　WSB #{d['wsb_rank'] or '—'}")
    L += ["", "**⭐ 你的自选股**"]
    rows = [(t, H[t]) for t in watch if t in H]
    for t, d in sorted(rows, key=lambda x: -x[1]["ratio"])[:15]:
        L.append(f"· {watch[t]} **{t}**：{d['mentions']} 次，{d['ratio']:.1f}×，WSB #{d['wsb_rank'] or '—'}")
    miss = [t for t in watch if t not in H]
    if miss:
        L.append(f"· 榜单外（讨论很少）：{'、'.join(miss)}")
    L.append(f"\n监测状态：{'🟢 开启' if is_on() else '⚪ 关闭'}（盘前 {'–'.join(s['运行时段_美东'])} 美东，每 {s['扫描间隔_分钟']} 分钟）")
    import briefing
    return briefing._split(L)
