# -*- coding: utf-8 -*-
"""
Grok 盘前雷达（每天一次，美东 9:00）：为开盘爆发准备
  · 按分类分组搜索你关注的 X 账号（每组 ≤ 20 个，分组越细，大号越不容易挤掉小号）
  · 每条帖子：发帖时间（由帖子编号计算）、账号、中文摘要、核心观点、涉及股票及看涨 / 看跌 / 中性
  · 防编造：帖子链接必须出现在 Grok 本次搜索结果里，否则丢掉
  · 事实（谁发了什么、谁看涨谁看跌）由程序从已核实的帖子汇总；分析（核心观点、影响）再交给 Grok 写，不联网、不加新事实
  · 输出：简讯（#快讯）+ 报告（#报告，附 Word，存进「报告」文件夹）
  · 每日费用上限：估算费用超过上限就停止后面的搜索
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
CACHE = HERE / ".grok_cache.json"
API = "https://api.x.ai/v1/responses"

GROUPS = {
    "芯片设计官方/CEO": ["nvidia", "JensenHuang", "Arm", "Broadcom", "AMD", "LisaSu", "intel", "LipBuTan1"],
    "存储官方": ["SKhynix", "wd_corporation", "SanDisk", "MicronTech"],
    "AI基建/光通信官方": ["nebiusai", "Lumentum", "nokia", "justinhotard"],
    "互联网/软件/云/电商官方/CEO": ["Google", "sundarpichai", "Meta", "netflix", "Microsoft", "satyanadella",
                               "Apple", "tim_cook", "amazon", "ajassy"],
    "电动车/航天": ["Tesla", "elonmusk", "SpaceX"],
    "其他公司": ["ROKAutomation", "CocaCola"],
    "政策/特朗普相关": ["realDonaldTrump", "WhiteHouse", "POTUS", "SenateCommerce", "FTC", "SECGov"],
    "快讯/准确消息": ["WSJ", "CNBC", "Bloomberg", "Reuters", "DeItaone", "FirstSquawk", "LiveSquawk",
                  "Unusual_Whales", "OptionsHawk", "SpotGamma", "SemiAnalysis_", "techmeme"],
    "极端/野路子": ["zerohedge", "WallStreetBets"],
    "预测/分析": ["jimcramer", "Polymarket", "TradingView"],
}
# 搜索时把小分类合并，减少调用次数；每组 ≤ 20 个账号
SEARCH_BATCHES = [
    ["芯片设计官方/CEO"],
    ["存储官方", "AI基建/光通信官方"],
    ["互联网/软件/云/电商官方/CEO"],
    ["电动车/航天", "其他公司"],
    ["政策/特朗普相关"],
    ["快讯/准确消息"],
    ["极端/野路子", "预测/分析"],
]
OFFICIAL = ("官方", "CEO", "电动车/航天", "其他公司")

DEFAULT = {
    "启用": False,
    "说明": "每天美东 9:00 扫一次你关注的 X 账号，出简讯（#快讯）+ 报告（#报告，附 Word）。API 密钥只放在你自己电脑上。",
    "API密钥": "",
    "模型": "grok-4.7",
    "报告时间_美东": "09:00",
    "每日费用上限美元": 5,
    "账号分组": GROUPS,
    "搜索批次": SEARCH_BATCHES,
    "识别图片": True,
}
PRICE = {"in": 2.0 / 1e6, "out": 6.0 / 1e6, "post": 5.0 / 1000}   # 第三方整理价格，以 docs.x.ai 为准


def settings(cfg):
    s = dict(DEFAULT)
    s.update(cfg.get("Grok盘前") or {})
    return s


# ================================================================ 工具
def post_id(url):
    m = re.search(r"/status(?:es)?/(\d{10,25})", url or "")
    return m.group(1) if m else None


def post_time(url):
    pid = post_id(url)
    if not pid:
        return None
    return dt.datetime.fromtimestamp(((int(pid) >> 22) + 1288834657657) / 1000, dt.timezone.utc)


def handle_of(url):
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status", url or "")
    return m.group(1) if m else ""


def last_close(now=None):
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    d = now.date()
    while True:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            return dt.datetime.combine(d, dt.time(16, 0), ET)


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


def _local(t):
    return t.astimezone(dt.datetime.now().astimezone().tzinfo)


# ================================================================ Grok 调用
import threading as _th
_GATE = _th.BoundedSemaphore(3)          # 整个程序同一时间最多 3 个 Grok 请求


def _post(s, body, timeout=360, tries=4):
    """所有 Grok 请求都走这里：先排队（最多同时 3 个），遇到 429 或超时就等一会儿再试。"""
    with _GATE:
        return _post_raw(s, body, timeout, tries)


def _post_raw(s, body, timeout, tries):
    import urllib.error
    last = None
    for i in range(tries):
        req = urllib.request.Request(API, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {s['API密钥'].strip()}",
                                              "User-Agent": "ZhuanQianXiaoZhuShou/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                raise
            wait = int(e.headers.get("Retry-After") or 0) or 30 * (i + 1)
        except (TimeoutError, OSError) as e:
            last = e
            wait = 10
        if i < tries - 1:
            time.sleep(wait)
    raise last


def call_search(s, handles, cats, watch, since):
    names = "、".join(f"{w['name']}({w['code']})" for w in watch)
    prompt = f"""用 X Search 查看这些账号（分类：{'、'.join(cats)}）自 {since:%Y-%m-%d %H:%M} 美东时间以来发的帖子，
挑出和美股市场、宏观政策、科技行业、或以下自选股有关的帖子（营销、生活、与市场无关的不要），最多 15 条，按重要性优先：
自选股：{names}

只输出一个 JSON 对象，不要其他文字：
{{"posts":[{{"url":"帖子原链接 https://x.com/用户名/status/数字",
"summary":"这条帖子说了什么（中文，不超过60字）",
"core":"核心观点（中文，不超过40字）",
"status":"已证实|传闻|观点|公告",
"importance":1到3,
"macro":true或false（是否影响大盘/宏观）,
"tickers":[{{"ticker":"代码","stance":"看涨|看跌|中性","strength":"强|中|弱"}}]}}],
"silent":["这段时间没有发相关帖子的账号用户名"]}}

要求：url 必须是这次搜索结果里真实存在的帖子；status：公司/政府官方账号发布的算「公告」，
有权威出处的新闻算「已证实」，单一来源爆料或"据悉/sources say"算「传闻」，评论分析算「观点」；
stance 是这条帖子对该股票的倾向（公告类按事件本身利好/利空判断）。"""
    tools = [{"type": "x_search", "allowed_x_handles": handles[:20],
              "from_date": since.astimezone(dt.timezone.utc).strftime("%Y-%m-%d"),
              "enable_image_understanding": bool(s.get("识别图片", False))}]
    models = [m for m in (s.get("搜索模型"), s["模型"]) if m]
    last = None
    for m in dict.fromkeys(models):                 # 快模型不支持 / 出错时，自动退回主模型
        try:
            return _post(s, {"model": m, "input": [{"role": "user", "content": prompt}], "tools": tools})
        except Exception as e:
            last = e
            if "429" in str(e):
                raise
    raise last


def call_synth(s, posts, watch, pm):
    """不联网，只基于已核实帖子写分析。"""
    slim = [dict(t=p["t"][11:16], acct="@" + p["handle"], cat=p["cat"], status=p["status"], summary=p["summary"],
                 core=p["core"], tickers=p["tickers"], macro=p["macro"]) for p in posts]
    wl = "、".join(f"{w['name']}({w['code']})" for w in watch)
    pmt = "、".join(f"{k} {v:+.2f}%" for k, v in (pm or {}).items() if v is not None) or "未知"
    prompt = f"""下面是今天美股盘前（前一天收盘后到现在）从 X 上收集并核实过的帖子（JSON）。只根据这些帖子分析，不要加入帖子以外的事实。
自选股：{wl}
盘前实际涨跌：{pmt}

帖子：
{json.dumps(slim, ensure_ascii=False)}

只输出一个 JSON 对象：
{{"overall":{{"bias":"偏多|偏空|分歧|不明","confidence":"高|中|低","drivers":["最关键的驱动，最多3条，每条≤40字"],
"conflict":"消息面与盘前实际涨跌是否冲突，≤40字"}},
"stocks":{{"代码":{{"what":"这只股票盘前发生了什么（≤80字）","core":"核心观点（≤50字）",
"impact":{{"direction":"利好|利空|中性|分歧","strength":"强|中|弱","horizon":"今日开盘|短期|长期","why":"≤60字"}}}}}},
"categories":{{"政策/特朗普相关":"≤80字","公司官方/CEO":"≤80字","快讯/准确消息":"≤80字","极端/野路子":"≤80字","预测/分析":"≤80字"}},
"burst":[{{"ticker":"代码","direction":"多|空","why":"≤40字"}}]}}
stocks 只写帖子里涉及到的股票；没有相关帖子的分类写"无"。"""
    return _post(s, {"model": s["模型"], "input": [{"role": "user", "content": prompt}]}, timeout=180)


def parse(data):
    texts, cites = [], set()
    for o in data.get("output") or []:
        if o.get("type") == "message":
            for c in o.get("content") or []:
                if c.get("type") in ("output_text", "text"):
                    texts.append(c.get("text") or "")
                    for a in c.get("annotations") or []:
                        if a.get("url"):
                            cites.add(a["url"])
    for u in data.get("citations") or []:
        cites.add(u if isinstance(u, str) else u.get("url", ""))
    if not texts and data.get("output_text"):
        texts.append(data["output_text"])
    return "\n".join(texts), {post_id(u) for u in cites if post_id(u)}, data.get("usage") or {}


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Grok 没有返回 JSON")
    return json.loads(m.group(0))


def est_cost(usage, n_posts=0):
    i = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    o = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return i * PRICE["in"] + o * PRICE["out"] + n_posts * PRICE["post"]


def premarket(cfg, log=lambda *a: None):
    try:
        import stocks
        mm = stocks.Moomoo(cfg.get("moomoo", {}), log)
        if not mm.ok:
            return None
        try:
            ret, df = mm.ctx.get_market_snapshot(["US.QQQ", "US.SPY"])
            if ret != mm.ft.RET_OK:
                return None
            out = {}
            for _, r in df.iterrows():
                v = r.get("pre_change_rate")
                try:
                    out[str(r["code"]).replace("US.", "")] = float(v)
                except Exception:
                    out[str(r["code"]).replace("US.", "")] = None
            return out
        finally:
            mm.close()
    except Exception:
        return None


# ================================================================ 一天一次的扫描
def run(cfg, log=lambda *a: None, now=None):
    s = settings(cfg)
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if not s.get("启用") or not str(s.get("API密钥", "")).strip():
        return dict(ok=False, msg="Grok 盘前雷达还没开启：在 设置.json → Grok盘前 填好 API密钥，并把「启用」改成 true。")
    since = last_close(now)
    groups = s.get("账号分组") or GROUPS
    batches = s.get("搜索批次") or SEARCH_BATCHES
    cap = float(s.get("每日费用上限美元") or 5)
    day = str(now.date())
    cache = load_cache()
    spent = float((cache.get("spent") or {}).get(day, 0))

    try:
        import stocks
        params, _, _, _, order = stocks.load_settings(lambda *a: None)
        watch = [dict(code=c, name=params[c]["名称"]) for c in order]
    except Exception:
        watch = []

    cat_of = {h.lower(): c for c, hs in groups.items() for h in hs}
    posts, seen, silent, notes, dropped = [], set(), set(), [], 0
    out_of_range = 0
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()
    budget = {"spent": spent}

    def one(b):
        handles = [h.lstrip("@") for c in b for h in groups.get(c, [])][:20]
        if not handles:
            return b, None, None, None
        with lock:
            if budget["spent"] >= cap:
                return b, None, None, f"已达每日费用上限 ${cap:.2f}，跳过：{'、'.join(b)}"
        try:
            t0 = time.time()
            data = call_search(s, handles, b, watch, since)
            text, cited, usage = parse(data)
            obj = extract_json(text)
            with lock:
                budget["spent"] += est_cost(usage, len(cited))
            log(f"[Grok] {'、'.join(b)}：{len(obj.get('posts') or [])} 条，{time.time() - t0:.0f} 秒")
            return b, obj, cited, None
        except Exception as ex:
            return b, None, None, f"{'、'.join(b)} 搜索失败：{str(ex)[:100]}"

    t_all = time.time()
    with ThreadPoolExecutor(max_workers=int(s.get("同时搜索组数") or 3)) as ex:
        results = list(ex.map(one, batches))
    spent = budget["spent"]
    log(f"[Grok] 全部 {len(batches)} 组搜索完成，用时 {time.time() - t_all:.0f} 秒")
    for b, obj, cited, err in results:
        if err:
            notes.append(err)
        if obj is None:
            continue
        silent |= {str(x).lstrip("@").lower() for x in obj.get("silent") or []}
        for p in obj.get("posts") or []:
            url = (p.get("url") or "").strip()
            pid, t = post_id(url), post_time(url)
            if not pid or not t or (cited and pid not in cited):
                dropped += 1
                continue
            if t < since.astimezone(dt.timezone.utc) or t > now.astimezone(dt.timezone.utc) + dt.timedelta(minutes=5):
                out_of_range += 1
                continue
            if pid in seen:
                continue
            seen.add(pid)
            h = handle_of(url)
            tick = [dict(ticker=str(x.get("ticker", "")).upper().lstrip("$"), stance=x.get("stance", "中性"),
                         strength=x.get("strength", "弱")) for x in p.get("tickers") or [] if x.get("ticker")]
            posts.append(dict(url=url, id=pid, t=t.astimezone(ET).isoformat(), handle=h,
                              cat=cat_of.get(h.lower(), "其他"), summary=str(p.get("summary") or "")[:90],
                              core=str(p.get("core") or "")[:60], status=p.get("status") or "观点",
                              importance=int(p.get("importance") or 1), macro=bool(p.get("macro")), tickers=tick[:6]))
    posts.sort(key=lambda p: p["t"])
    if out_of_range:
        notes.append(f"{out_of_range} 条帖子发在前一天收盘之前（或时间异常），已排除")
    pm = premarket(cfg, log)

    synth = {}
    if posts and spent < cap:
        try:
            data = call_synth(s, posts, watch, pm)
            text, _, usage = parse(data)
            synth = extract_json(text)
            spent += est_cost(usage)
        except Exception as e:
            notes.append(f"分析步骤失败：{str(e)[:100]}（下面只有事实部分）")
    all_handles = {h.lower() for hs in groups.values() for h in hs}
    posted = {p["handle"].lower() for p in posts}
    res = dict(ok=True, day=day, since=since.isoformat(), at=now.isoformat(), posts=posts, synth=synth, pm=pm,
               notes=notes, dropped=dropped, cost=round(spent - float((cache.get("spent") or {}).get(day, 0)), 3),
               spent=round(spent, 3), cap=cap, no_posts=sorted(all_handles - posted),
               watch=[w["code"] for w in watch])
    cache.setdefault("spent", {})[day] = round(spent, 4)
    cache["spent"] = {k: v for k, v in cache["spent"].items() if k >= str(now.date() - dt.timedelta(days=10))}
    cache["latest"] = res
    save_cache(cache)
    return res


def latest():
    r = load_cache().get("latest")
    if r and r.get("since") == last_close().isoformat():
        return r
    return None


# ================================================================ 谁看涨谁看跌（程序汇总，不靠模型）
def sides(posts):
    """{代码: {"看涨": [...], "看跌": [...], "中性": [...], "公告": [...]}}"""
    out = {}
    for p in posts:
        official = any(k in p["cat"] for k in OFFICIAL) and p["status"] == "公告"
        for t in p["tickers"]:
            d = out.setdefault(t["ticker"], {"看涨": [], "看跌": [], "中性": [], "公告": []})
            entry = dict(handle=p["handle"], cat=p["cat"], core=p["core"] or p["summary"], url=p["url"],
                         stance=t["stance"], strength=t["strength"], status=p["status"], t=p["t"])
            if official:
                d["公告"].append(entry)
            else:
                d[t["stance"] if t["stance"] in ("看涨", "看跌") else "中性"].append(entry)
    return out


# ================================================================ 简讯（#快讯）
def brief(res):
    o = (res.get("synth") or {}).get("overall") or {}
    bias = o.get("bias", "不明")
    icon = {"偏多": "🟢", "偏空": "🔴", "分歧": "🟡"}.get(bias, "⚪")
    now = dt.datetime.fromisoformat(res["at"])
    posts = res["posts"]
    L = [f"**🛰 Grok 盘前简讯**　{_local(now):%m-%d %H:%M}（美东 {now:%H:%M}）· {len(posts)} 条帖子，"
         f"{len({p['handle'] for p in posts})} 个账号有动静",
         f"**🧭 今日倾向：{icon} {bias}**（信心 {o.get('confidence', '—')}）"]
    L += [f"· {d}" for d in (o.get("drivers") or [])[:3]]
    pm = res.get("pm") or {}
    if pm.get("QQQ") is not None:
        L.append(f"· 盘前 QQQ {pm['QQQ']:+.2f}%" + (f"、SPY {pm['SPY']:+.2f}%" if pm.get("SPY") is not None else "")
                 + (f"　{o['conflict']}" if o.get("conflict") else ""))
    burst = (res.get("synth") or {}).get("burst") or []
    if burst:
        L.append("**🎯 开盘爆发关注**：" + "；".join(f"{'🔺' if b.get('direction') == '多' else '🔻'}{b.get('ticker')} {b.get('why', '')}"
                                             for b in burst[:5]))
    top = sorted(posts, key=lambda p: (-p["importance"], p["t"]))[:5]
    if top:
        L.append("**📌 最重要的帖子**")
        for p in top:
            st = {"已证实": "✅", "传闻": "⚠️", "观点": "💬", "公告": "📢"}.get(p["status"], "")
            tk = "/".join(t["ticker"] for t in p["tickers"][:3])
            L.append(f"· `{_local(dt.datetime.fromisoformat(p['t'])):%H:%M}` {st} @{p['handle']}"
                     + (f" **[{tk}]**" if tk else "") + f" {p['summary']} [原帖](<{p['url']}>)")
    L.append("_完整报告（每只股票发生了什么、核心观点、影响、谁看涨谁看跌）见 #报告。传闻未经证实，仅供参考。_")
    import briefing
    return briefing._split(L)


def brief_section():
    """凌晨 1 点快讯用（读缓存，不花钱）。"""
    r = latest()
    if not r:
        return []
    o = (r.get("synth") or {}).get("overall") or {}
    if not o:
        return []
    icon = {"偏多": "🟢", "偏空": "🔴", "分歧": "🟡"}.get(o.get("bias"), "⚪")
    out = [f"**🛰 Grok 盘前倾向：{icon} {o.get('bias', '不明')}**（信心 {o.get('confidence', '—')}，{len(r['posts'])} 条帖子）"]
    out += [f"· {d}" for d in (o.get("drivers") or [])[:3]]
    return out


# ================================================================ 报告（#报告 摘要 + Word）
def report_summary(res, path=None):
    sy = res.get("synth") or {}
    o = sy.get("overall") or {}
    sd = sides(res["posts"])
    L = [f"**📑 Grok 盘前报告**　{res['day']}　· 帖子 {len(res['posts'])} 条 · 今日倾向 {o.get('bias', '不明')}（信心 {o.get('confidence', '—')}）"]
    for code in sorted(sd, key=lambda c: -sum(len(v) for v in sd[c].values()))[:10]:
        st = (sy.get("stocks") or {}).get(code) or {}
        im = st.get("impact") or {}
        d = sd[code]
        L.append(f"· **{code}**：{im.get('direction', '—')}（{im.get('strength', '—')}，{im.get('horizon', '—')}）"
                 f"　看涨 {len(d['看涨'])} / 看跌 {len(d['看跌'])} / 公告 {len(d['公告'])}"
                 + (f"　{st.get('core', '')}" if st.get("core") else ""))
    L.append("_完整内容见附件 Word。_" if path else "_Word 生成失败，只有摘要。_")
    import briefing
    return briefing._split(L)


def _hyperlink(par, url, text):
    from docx.oxml.shared import OxmlElement, qn
    part = par.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), r_id)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    c = OxmlElement("w:color")
    c.set(qn("w:val"), "1F5FBF")
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(c)
    rpr.append(u)
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    r.append(t)
    h.append(r)
    par._p.append(h)


def word_report(res, out_dir):
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.oxml.ns import qn
    FONT = "PingFang SC"
    RED, GREEN, GREY, ORANGE = RGBColor(0xC0, 0, 0), RGBColor(0, 0x80, 0x3C), RGBColor(0x66, 0x66, 0x66), RGBColor(0xB4, 0x5F, 0x06)
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = FONT, Pt(10.5)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    def para(text="", bold=False, color=None, size=None):
        p = doc.add_paragraph()
        if text:
            r = p.add_run(text)
            r.bold = bold
            if color is not None:
                r.font.color.rgb = color
            if size:
                r.font.size = Pt(size)
        return p

    def table(headers, rows):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(headers):
            t.rows[0].cells[i].text = h
        for row in rows:
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = str(v)
        return t

    sy = res.get("synth") or {}
    o = sy.get("overall") or {}
    since = dt.datetime.fromisoformat(res["since"])
    at = dt.datetime.fromisoformat(res["at"])
    posts = res["posts"]
    sd = sides(posts)
    doc.add_heading("玉衡5.0 · Grok 盘前报告", 0)
    para(f"覆盖时间：{since:%m-%d %H:%M} → {at:%m-%d %H:%M}（美东）　·　帖子 {len(posts)} 条，来自 {len({p['handle'] for p in posts})} 个账号",
         color=GREY, size=9)
    para("事实部分（谁发了什么、时间、链接、谁看涨谁看跌）由程序从已核实的帖子汇总；分析部分由 Grok 只根据这些帖子撰写。"
         "传闻未经证实；仅供参考，不构成投资建议。", color=GREY, size=9)

    doc.add_heading("一、结论速览", 1)
    p = para()
    p.add_run("今日倾向：").bold = True
    r = p.add_run(f"{o.get('bias', '不明')}（信心 {o.get('confidence', '—')}）")
    r.bold = True
    r.font.color.rgb = GREEN if o.get("bias") == "偏多" else RED if o.get("bias") == "偏空" else ORANGE
    for d in o.get("drivers") or []:
        doc.add_paragraph(d, style="List Bullet")
    pm = res.get("pm") or {}
    if pm:
        para("盘前实际：" + "、".join(f"{k} {v:+.2f}%" for k, v in pm.items() if v is not None)
             + (f"　{o['conflict']}" if o.get("conflict") else ""))
    if sy.get("burst"):
        para("开盘爆发关注：", bold=True)
        for b in sy["burst"]:
            doc.add_paragraph(f"{b.get('ticker')} {'做多' if b.get('direction') == '多' else '做空'}：{b.get('why', '')}", style="List Bullet")

    doc.add_heading("二、逐只股票", 1)
    order = sorted(sd, key=lambda c: (c not in res.get("watch", []), -sum(len(v) for v in sd[c].values())))
    if not order:
        para("盘前没有涉及个股的帖子。")
    for code in order:
        info = (sy.get("stocks") or {}).get(code) or {}
        im = info.get("impact") or {}
        doc.add_heading(f"{code}" + ("（自选股）" if code in res.get("watch", []) else ""), 2)
        if info.get("what"):
            para(f"发生了什么：{info['what']}")
        if info.get("core"):
            para(f"核心观点：{info['core']}", bold=True)
        if im:
            c = GREEN if im.get("direction") == "利好" else RED if im.get("direction") == "利空" else ORANGE
            p = para()
            p.add_run("对股价的影响：").bold = True
            r = p.add_run(f"{im.get('direction', '—')}，{im.get('strength', '—')}，{im.get('horizon', '—')}")
            r.font.color.rgb = c
            if im.get("why"):
                p.add_run(f"　{im['why']}")
        d = sd[code]
        rows = []
        for side in ("看涨", "看跌", "公告", "中性"):
            for e in d[side]:
                rows.append([side, f"@{e['handle']}（{e['cat']}）", f"{dt.datetime.fromisoformat(e['t']):%m-%d %H:%M}",
                             e["status"], e["core"]])
        if rows:
            table(["立场", "账号", "时间(美东)", "性质", "观点 / 内容"], rows)
        para("相关帖子：", color=GREY, size=9)
        for e in sorted([e for v in d.values() for e in v], key=lambda e: e["t"]):
            p = para(f"{dt.datetime.fromisoformat(e['t']):%H:%M} @{e['handle']}　", size=9)
            _hyperlink(p, e["url"], "原帖")

    doc.add_heading("三、按账号类别", 1)
    cats = sy.get("categories") or {}
    by_cat = {}
    for p_ in posts:
        key = "公司官方/CEO" if any(k in p_["cat"] for k in OFFICIAL) else p_["cat"]
        by_cat.setdefault(key, []).append(p_)
    for cat in ("政策/特朗普相关", "公司官方/CEO", "快讯/准确消息", "极端/野路子", "预测/分析"):
        doc.add_heading(cat, 2)
        if cats.get(cat):
            para(cats[cat], bold=True)
        for p_ in by_cat.get(cat, []):
            pp = para(f"{dt.datetime.fromisoformat(p_['t']):%m-%d %H:%M} @{p_['handle']}［{p_['status']}］{p_['summary']}　", size=9.5)
            _hyperlink(pp, p_["url"], "原帖")
        if not by_cat.get(cat):
            para("没有相关帖子。", color=GREY, size=9)

    doc.add_heading("四、全部帖子", 1)
    table(["时间(美东)", "账号", "分类", "性质", "内容", "涉及"],
          [[f"{dt.datetime.fromisoformat(p_['t']):%m-%d %H:%M}", "@" + p_["handle"], p_["cat"], p_["status"], p_["summary"],
            "、".join(f"{t['ticker']}{t['stance']}" for t in p_["tickers"])] for p_ in posts])

    doc.add_heading("附：运行情况", 1)
    para(f"本次费用约 ${res.get('cost', 0):.2f}（今日累计 ${res.get('spent', 0):.2f}，上限 ${res.get('cap', 0):.2f}）；"
         f"丢弃无法核对链接的内容 {res.get('dropped', 0)} 条。", size=9)
    if res.get("no_posts"):
        para("这段时间没有抓到相关帖子的账号（可能是没发帖、发的与市场无关，或用户名有误，请核对）：" + "、".join("@" + h for h in res["no_posts"]),
             color=GREY, size=9)
    for n in res.get("notes") or []:
        para("⚠ " + n, color=ORANGE, size=9)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"Grok盘前报告_{res['day']}.docx"
    doc.save(path)
    return path
