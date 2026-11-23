# -*- coding: utf-8 -*-
"""
盘前报告：数据面 + 信息面 + 散户情绪合成一份（每天和 Claude 定打法只看这一份）

  完整版 → 数据/今日/<日期>/盘前资料.md（给 Claude 和你细看）
     一、大盘和日程　二、夜盘　三、全部自选股（盘前涨跌、位置、趋势、波幅、推特、Reddit、最新消息）
     四、Kill Zone 数据（每只股票各时段的历史统计 + 昨天）　五、C2 打法分股票回测　六、个股消息
     七、散户情绪（Reddit 热度 + 热度翻倍的读帖）　八、推特　九、宏观要闻
  精简版 → Discord #快讯，一屏以内（美东 8:45 自动；或发「简报」）
  一键生成（先抓新闻、补夜盘和 Grok，再写报告）：today.make_full，控制台「生成盘前报告」
  只放事实，不下结论；结论在和 Claude 讨论后写进「打法和纪律.md」。
"""
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
SYD = ZoneInfo("Australia/Sydney")
INFO = HERE.parent / "数据" / "冷数据" / "信息面"
US_WORDS = ["美联储", "鲍威尔", "FOMC", "降息", "加息", "美国", "美债", "美元", "关税", "特朗普", "白宫", "非农", "CPI", "PCE",
            "零售销售", "GDP", "纳指", "标普", "道指", "美股", "财政部", "收益率", "通胀", "就业", "初请", "原油", "沃勒", "联储"]



def _ver():
    """当前版本号（项目根目录 VERSION）。"""
    try:
        import version
        return version.get()
    except Exception:
        return "?"

def _pct(x):
    return "—" if x is None else f"{x:+.2f}%"


def _one_line(x):
    return " ".join(str(x or "").split())


def info_items(since_hours=18):
    """信息面索引里最近 N 小时的条目（标题压成一行）。"""
    now = dt.datetime.now(ET)
    cut = (now - dt.timedelta(hours=since_hours)).isoformat(timespec="minutes")
    out = []
    for m in sorted({now.strftime("%Y-%m"), (now - dt.timedelta(hours=since_hours)).strftime("%Y-%m")}):
        p = INFO / ".index" / f"{m}.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("t", "") >= cut:
                    o["title"] = _one_line(o.get("title"))
                    out.append(o)
    return out


def prev_day_levels(codes):
    """{code: (前一天高, 前一天低)}（K 线库里最近一根日线）。"""
    out = {}
    try:
        con = sqlite3.connect(f"file:{HERE / 'data' / 'history.db'}?mode=ro", uri=True)
        today = str(dt.datetime.now(ET).date())
        for c in codes:
            r = con.execute("SELECT high, low FROM bars_1d WHERE code = ? AND ts < ? ORDER BY ts DESC LIMIT 1", (c, today)).fetchone()
            if r:
                out[c] = r
        con.close()
    except Exception:
        pass
    return out


def grok_view():
    try:
        import grokscan as G
        r = G.load_cache().get("latest") or {}
        if r.get("day") != str(dt.datetime.now(ET).date()):
            return None, {}
        sides = G.sides(r.get("posts") or [])
        return r, {k: (len(v["看涨"]), len(v["看跌"])) for k, v in sides.items()}
    except Exception:
        return None, {}


def reddit_heat(items):
    """最近一次 ApeWisdom 快照里，提及次数是 24 小时前 2 倍以上的。"""
    snaps = [o for o in items if o.get("cat") == "Reddit" and o.get("src") == "ApeWisdom"]
    if not snaps:
        return {}
    last = max(snaps, key=lambda o: o["t"])
    out = {}
    for line in (last.get("summary") or "").splitlines():
        m = re.match(r"(\w+)：提及 (\d+)（24 小时前 (\d+)，([\d.]+) 倍）", line)
        if m:
            out[m.group(1)] = (int(m.group(2)), float(m.group(4)))
    return out


def watch_codes_all(cfg):
    try:
        import news
        return [w["code"] for w in news.research(cfg)]
    except Exception:
        return []


def overview():
    """冷数据/股票/总览.md 的表格 → {代码: {列名: 值}}。"""
    out = {}
    try:
        text = (HERE.parent / "数据" / "冷数据" / "股票" / "总览.md").read_text(encoding="utf-8")
    except Exception:
        return out
    head = None
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [x.strip() for x in line.strip("|").split("|")]
        if cells[0] == "代码":
            head = cells
        elif head and not set(cells[0]) <= set("-: "):
            code = re.sub(r"\[(\w+)\].*", r"\1", cells[0])
            out[code] = dict(zip(head, cells))
    return out


def c2_block(codes):
    try:
        d = json.loads((HERE / "data" / ".c2_by_code.json").read_text(encoding="utf-8"))
        tot = json.loads((HERE / "data" / ".c2_backtest.json").read_text(encoding="utf-8")).get("text")
    except Exception:
        return ["- 还没有回测结果（控制台 → 工具 → C2 回测）"]
    L = [f"- 全部：{tot}" if tot else "", "",
         "| 代码 | 笔数 | 到目标 % | 平均 R | 做多 笔数 / 平均 R | 做空 笔数 / 平均 R |", "| --- | --- | --- | --- | --- | --- |"]
    for c in codes:
        x = d.get(c)
        if x:
            L.append(f"| {c} | {x['n']} | {x['hit']}% | {x['avg']:+.2f} | {x['n_long']} / {x['avg_long']:+.2f} | {x['n_short']} / {x['avg_short']:+.2f} |")
    L += ["", "每只只有几十笔，单只的平均 R 波动很大（几十笔的 95% 区间大约 ±0.4R），只当参考；没有 1 分钟数据的股票不在表里。"]
    return L[2:] if not tot else L


def reddit_block(cfg, codes, items, max_read=3):
    """自选股的 Reddit 热度（ApeWisdom 实时）+ 热度飙升的最多 max_read 只读帖判断情绪（Grok，按次收费，很便宜）。"""
    try:
        import reddit_watch as RW
        s = RW.settings(cfg)
        H = RW.heat(s)
    except Exception as e:
        h = reddit_heat(items)
        if not h:
            return [f"- 读不到 Reddit 热度（{type(e).__name__}）"]
        return [f"- {c}：提及 {v[0]}，{v[1]:.1f} 倍" for c, v in h.items() if c in codes] or ["- 自选股都不在热度榜上"]
    rows = [(c, H[c]) for c in codes if c in H]
    L = ["| 代码 | 提及 | 24 小时前 | 倍数 | WSB 排名（24 小时前） |", "| --- | --- | --- | --- | --- |"]
    for c, d in sorted(rows, key=lambda x: -x[1]["mentions"]):
        L.append(f"| {c} | {d['mentions']} | {d['m24']} | {d['ratio']:.1f} | {d['wsb_rank'] or '—'}（{d['wsb_r24'] or '—'}） |")
    miss = [c for c in codes if c not in H]
    if miss:
        L.append(f"\n榜单外（讨论很少）：{'、'.join(miss)}")
    hot = [(c, d) for c, d in rows if d["ratio"] >= 2 and d["mentions"] >= 20]
    hot.sort(key=lambda x: -x[1]["ratio"])
    if hot and s.get("读帖判断方向", True):
        L.append("\n**热度翻倍的股票，读帖看情绪**：")
        for c, d in hot[:max_read]:
            try:
                r = RW.read_posts(cfg, c, c, lambda *a: None)
            except Exception as e:
                r = None
            if r:
                L.append(f"- {c}：{r.get('tone', '')}；{r.get('mood', '')}；看涨期权 / 看跌期权：{r.get('calls_puts', '—')}。{str(r.get('summary') or '')[:150]}")
            else:
                L.append(f"- {c}：没读到（设置里关了读帖，或者 Grok 不可用）")
    top10 = sorted(H.items(), key=lambda x: -x[1]["mentions"])[:10]
    L.append("\n全市场热度前 10：" + "、".join(f"{t} {d['mentions']}" for t, d in top10))
    return L


def build(cfg):
    """返回 (完整 markdown, 精简 Discord 文字列表)。"""
    import today
    day = today.session_day()
    now = dt.datetime.now(ET)
    q, qerr = {}, ""
    try:
        q, qerr = today.premarket_quotes(cfg)
    except Exception as e:
        qerr = f"盘前行情读取失败：{type(e).__name__}"
    items = info_items(18)
    gres, gsides = grok_view()
    heat = reddit_heat(items)
    lv = prev_day_levels(list(q))
    # 日历
    events = []
    try:
        import briefing as B
        events = B.calendar(hours=24)
    except Exception:
        pass
    # 财报
    soon = []
    try:
        cal = (INFO / "财报" / "财报日历.md").read_text(encoding="utf-8")
        soon = [(c, d) for c, _, d in re.findall(r"\| (\w+) \| ([^|]*) \| (\d{4}-\d{2}-\d{2}) \|", cal)
                if str(day) <= d <= str(day + dt.timedelta(days=3))]
    except Exception:
        pass

    def pre(c):
        x = q.get(c) or {}
        return x.get("盘前%") if x.get("盘前%") is not None else x.get("夜盘%")

    def where(c):
        x, l_ = q.get(c) or {}, lv.get(c)
        px = x.get("盘前") or x.get("夜盘")
        if not px or not l_:
            return ""
        return "高于昨天高点" if px > l_[0] else "低于昨天低点" if px < l_[1] else "在昨天区间内"

    def top_news(c):
        its = [o for o in items if c in (o.get("codes") or []) and o.get("cat") in ("公司", "财报")]
        its.sort(key=lambda o: o["t"], reverse=True)
        return its[0] if its else None

    movers = sorted([c for c in q if pre(c) is not None and c not in ("QQQ", "SPY")], key=lambda c: -abs(pre(c)))[:8]
    macro = [o for o in items if o.get("cat") == "宏观" and any(w in (o.get("title", "") + o.get("summary", "")) for w in US_WORDS)]
    macro.sort(key=lambda o: o["t"], reverse=True)
    syn = ((gres or {}).get("synth") or {}).get("overall") or {}

    # ---------------- 精简版（Discord）
    D = [f"**📋 今日简报 · {day}**（美东 {now:%H:%M} / 悉尼 {now.astimezone(SYD):%H:%M}）"]
    D.append("**大盘**：" + "，".join(f"{c} 夜盘 {_pct((q.get(c) or {}).get('夜盘%'))} / 盘前 {_pct((q.get(c) or {}).get('盘前%'))}"
                                     for c in ("QQQ", "SPY")) if q else f"**大盘**：{qerr or '没有行情'}")
    if events:
        D.append("**今天的数据**：" + "；".join(f"{e['t'].astimezone(ET):%H:%M}（悉尼 {e['t'].astimezone(SYD):%H:%M}）"
                                          f"{'★' if e['high'] else ''}{e['name']}" for e in events[:6]))
    if soon:
        D.append("**3 天内财报**：" + "、".join(f"{c} {d[5:]}" for c, d in soon))
    if movers:
        D.append("**盘前异动**")
        for c in movers:
            n = top_news(c)
            g = gsides.get(c)
            h = heat.get(c)
            D.append(f"· **{c}** {_pct(pre(c))}" + (f"，{where(c)}" if where(c) else "")
                     + (f"；推特 ↑{g[0]} ↓{g[1]}" if g and (g[0] or g[1]) else "")
                     + (f"；Reddit 热度 {h[1]:.1f} 倍" if h and h[1] >= 2 else "")
                     + (f"\n　{n['title'][:70]}" if n else ""))
    if macro:
        D.append("**宏观**")
        D += [f"· {o['t'][11:16]} {o['title'][:80]}" for o in macro[:5]]
    if syn:
        D.append(f"**推特综合**：{syn.get('bias', '')}" + "".join(f"\n· {x}" for x in (syn.get("drivers") or [])[:3]))
    D.append(f"-# 完整资料：数据/今日/{day}/盘前资料.md　·　在 Claude 里说「开始今天」一起定打法")

    # ---------------- 完整版（文件）
    codes = [c for c in q] or watch_codes_all(cfg)
    ov = overview()
    rd = reddit_block(cfg, codes, items)
    L = [f"# 盘前资料 · {day}（美东）", "", f"玉衡5.0 v{_ver()} 生成", "",
         f"程序整理于美东 {now:%H:%M}（悉尼 {now.astimezone(SYD):%H:%M}）。只放事实，不下结论；结论在和 Claude 讨论后写进「打法和纪律.md」。", "",
         "目录：一、大盘和日程　二、夜盘　三、全部自选股　四、Kill Zone 数据　五、C2 打法分股票回测　六、个股消息"
         "　七、散户情绪　八、推特　九、宏观要闻", ""]
    L += ["## 一、大盘和日程", ""]
    if q:
        L += [f"- {c}：昨收 {(q.get(c) or {}).get('prev', 0):.2f}，夜盘 {_pct((q.get(c) or {}).get('夜盘%'))}，盘前 {_pct((q.get(c) or {}).get('盘前%'))}"
              for c in ("QQQ", "SPY")]
    else:
        L.append(f"- {qerr or '没有行情'}")
    L += [f"- {e['t'].astimezone(ET):%H:%M}（悉尼 {e['t'].astimezone(SYD):%H:%M}）{'★ ' if e['high'] else ''}{e['name']}"
          + (f"（预期 {e['forecast']}，前值 {e['previous']}）" if e["forecast"] or e["previous"] else "") for e in events] or ["- 今天没有中高影响数据"]
    L += [f"- 财报：{c} {d}" for c, d in soon]
    L += ["", "## 二、夜盘", ""]
    try:
        import today as T
        nt = (T.folder(day) / "夜盘.md").read_text(encoding="utf-8")
        nt = re.sub(r"^# .*\n+", "", nt).replace("**", "").replace("-# ", "")
        L.append(nt.strip() or "- 没有内容")
    except Exception:
        L.append("- 还没有今天的夜盘复盘（美东 4:10 自动；一键生成时会补跑）")
    L += ["", "## 三、全部自选股（盘前，按盘前涨跌幅排序）", "",
          "| 代码 | 昨收 | 夜盘 % | 盘前 % | 位置 | 趋势 | 5 日 % | 20 日 % | 日均波幅 % | 离 52 周高 % | 推特 ↑ / ↓ | Reddit 提及（倍） | 最新消息 |",
          "| " + " | ".join("---" for _ in range(13)) + " |"]
    for c in sorted(codes, key=lambda c: -abs(pre(c) or 0)):
        x, n, g, h, o = q.get(c) or {}, top_news(c), gsides.get(c), heat.get(c), ov.get(c) or {}
        news_cell = (('[' + n['title'][:40].replace('|', '/') + '](' + n['url'] + ')') if n and n.get('url') else (n['title'][:40] if n else ''))
        L.append(f"| {c} | {(format(x['prev'], '.2f') if x.get('prev') else o.get('收盘', ''))} | {_pct(x.get('夜盘%'))} | {_pct(x.get('盘前%'))} | {where(c)} | "
                 f"{o.get('趋势', '')} | {o.get('5 日 %', '')} | {o.get('20 日 %', '')} | {o.get('日均波幅 %', '')} | {o.get('离 52 周高点 %', '')} | "
                 f"{f'{g[0]} / {g[1]}' if g else ''} | {f'{h[0]}（{h[1]:.1f}）' if h else ''} | {news_cell} |")
    L += ["", "趋势、5 / 20 日、波幅、离 52 周高来自 `冷数据/股票/总览.md`（截至上一个收盘）；每只的关键价位看 `冷数据/股票/代码/摘要.md`。"]
    L += ["", "## 四、Kill Zone 数据", ""]
    try:
        import engine
        L.append(engine.kz_markdown(codes, 60))
    except Exception as e:
        L.append(f"- 算不出（{type(e).__name__}: {str(e)[:80]}）")
    L += ["", "## 五、C2 打法分股票回测（1 分钟，一年，未扣成本）", ""]
    L += c2_block(codes)
    L += ["", "## 六、个股消息（最近 18 小时，每只最多 3 条，新的在前）", ""]
    any_news = False
    for c in codes:
        its, seen = [], set()
        for o in sorted([o for o in items if c in (o.get("codes") or []) and o.get("cat") in ("公司", "财报", "行业", "推特")],
                        key=lambda o: o["t"], reverse=True):
            if o["title"][:30] not in seen:
                seen.add(o["title"][:30])
                its.append(o)
        its = its[:3]
        if its:
            any_news = True
            L.append(f"- **{c}**：" + "；".join(f"{o['t'][11:16]} {o['title'][:70]}" for o in its))
    if not any_news:
        L.append("- 没有")
    L += ["", "## 七、散户情绪（Reddit：WSB、r/options、r/stocks 合计）", ""] + rd
    L += ["", "## 八、推特（Grok，基于核实过的帖子）", ""]
    if syn:
        L.append(f"- 倾向：{syn.get('bias', '')}（信心 {syn.get('confidence', '—')}）")
        L += [f"- {x}" for x in syn.get("drivers") or []]
        if syn.get("conflict"):
            L.append(f"- 分歧：{syn['conflict']}")
        L += [f"- {c}：看涨 {v[0]}，看跌 {v[1]}" for c, v in sorted(gsides.items(), key=lambda x: -(x[1][0] + x[1][1])) if v[0] or v[1]][:15]
    else:
        L.append("- 今天的 Grok 雷达还没跑（美东 8:40 自动；一键生成时会补跑）")
    L += ["", "## 九、宏观要闻（和美股有关的，新的在前）", ""]
    L += [f"- {o['t'][11:16]} {o['title'][:120]}" + (f" [原文]({o['url']})" if o.get("url") else "") for o in macro[:15]] or ["- 没有"]
    L += ["", "## 更多", "",
          f"- 全部新闻：`冷数据/信息面/` 各类别的 {day}.md；单只股票 `按股票/代码.md`",
          "- 盘中：在 Claude 里问「NVDA 现在怎么样」，Claude 会跑盘感引擎（`engine.py`），结合热数据和这份资料回答"]
    import briefing
    return "\n".join(L) + "\n", briefing._split(D)
