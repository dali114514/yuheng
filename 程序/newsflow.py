# -*- coding: utf-8 -*-
"""
消息面：把新闻和交易机会联系起来

  1. 给每条新闻定性：类型（财报 / 指引 / 评级 / 并购 / 监管 / 产品 / 订单 / 管理层 / 回购 / 供应链）+ 利好 / 利空
  2. 汇总每只股票的消息面：利好 / 利空 / 多空交织 / 无消息
  3. 和交易方向对照：
       · 消息和方向一致 → 标「✓ 消息面配合」
       · 消息和方向相反 → 标「✗ 消息面逆向」并降一级（逆着新消息做期权买方，胜率明显变低）
       · 财报在 3 天内   → 标「⚠ 临近财报」，波段 / 隔夜单降一级（财报后隐含波动率骤降，方向对也可能亏）
       · 财报刚发布      → 写明财报消息偏利好还是利空，供判断跳空是否会延续

定性用关键词规则，能认出「超预期 / 下调指引 / 评级下调」这类明确说法，但读不出「是否已被市场计价」，仅作辅助。
新闻来源：Yahoo（报告运行时抓）+ 华尔街见闻快讯（每 10 分钟最多抓一次）。
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".news_cache.json"

# ---------------------------------------------------------------- 类型
TYPES = [
    ("财报", r"财报|业绩|季报|营收|收入|净利|每股收益|EPS|earnings|quarterly results|revenue|profit|\bQ[1-4]\b"),
    ("指引", r"指引|展望|guidance|outlook|forecast"),
    ("评级", r"评级|目标价|上调至|下调至|分析师|upgrade|downgrade|price target|rating|analyst|overweight|underweight"),
    ("并购", r"收购|并购|合并|拆分|分拆|acquire|acquisition|merger|takeover|buyout|spin-?off"),
    ("监管", r"监管|调查|诉讼|起诉|罚款|反垄断|召回|SEC|DOJ|FTC|probe|investigation|lawsuit|sue[sd]?\b|fine[sd]?\b|antitrust|recall"),
    ("供应链", r"出口管制|禁令|关税|芯片限制|供应|产能|export control|ban\b|tariff|supply|shortage|capacity"),
    ("产品", r"发布|推出|新车|新品|上市|量产|交付|launch|unveil|release|deliver(y|ies)|rollout|debut"),
    ("订单", r"订单|合同|合作|签约|中标|partnership|deal\b|contract|order[s]?\b|agreement|collaborat"),
    ("管理层", r"CEO|CFO|首席执行官|辞职|离职|任命|接任|resign|steps? down|appoint|succession"),
    ("回购", r"回购|分红|股息|buy ?back|repurchase|dividend"),
    ("定价", r"涨价|提价|降价|合约价|price hike|price increase|price cut"),
]

# ---------------------------------------------------------------- 利好 / 利空
POS = r"""超预期|好于预期|高于预期|超出预期|创新高|创纪录|大涨|飙升|暴涨|涨超|上调|上修|提高|增长强劲|强劲|获批|批准|中标|大单|
回购|增持|买入评级|buy ?back|repurchase|上调评级|看涨|突破|扭亏|盈利改善|需求旺盛|供不应求|涨价|
\bbeats?\b|\btops?\b|surpass|exceed|record|surge|soar|jump|rall(y|ies)|raise[sd]?\b|boost|upgrade|outperform|
\bbuy rating\b|approv|\bwins?\b|strong demand|bullish|higher guidance|price hike"""
NEG = r"""不及预期|低于预期|逊于预期|差于预期|大跌|暴跌|跳水|跌超|下调|下修|削减|下滑|下降|亏损|疲软|放缓|
调查|诉讼|起诉|罚款|召回|禁令|限制|延迟|推迟|暂停|裁员|减持|下调评级|卖出评级|看跌|警告|预警|
\bmiss(es|ed)?\b|\bfall[s]?\b|\bfell\b|plunge|slump|tumble|sink|\bcut[s]?\b|lower(s|ed)? guidance|downgrade|underperform|
\bsell rating\b|probe|lawsuit|recall|\bban\b|delay|halt|layoff|warn|weak|bearish|slowdown|decline"""
_POS = re.compile(POS.replace("\n", ""), re.I)
_NEG = re.compile(NEG.replace("\n", ""), re.I)
_TYPES = [(n, re.compile(p, re.I)) for n, p in TYPES]


def classify(text):
    """→ dict(type, s)；s = +1 利好 / -1 利空 / 0 中性或无法判断。"""
    t = text or ""
    typ = next((n for n, rx in _TYPES if rx.search(t)), "其他")
    p, n = len(_POS.findall(t)), len(_NEG.findall(t))
    # 「下调目标价但维持买入」这类混合说法：两边都有就看哪边多，持平算中性
    s = 1 if p > n else -1 if n > p else 0
    # 「上调…评级」「目标价上调」明确利好；「下调评级/目标价」明确利空
    if re.search(r"(上调|upgrade).{0,8}(评级|目标价|rating|target)|(评级|目标价|target).{0,6}(上调|raised)", t, re.I):
        s = 1
    if re.search(r"(下调|downgrade).{0,8}(评级|目标价|rating|target)|(评级|目标价|target).{0,6}(下调|cut|lowered)", t, re.I):
        s = -1
    return dict(type=typ, s=s)


# ---------------------------------------------------------------- 缓存
def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(c):
    try:
        CACHE.write_text(json.dumps(c, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


def _flash(watch, cache):
    """华尔街见闻里提到自选股的快讯（10 分钟内复用上次结果）。"""
    fc = cache.get("flash") or {}
    if fc and time.time() - fc.get("ts", 0) < 600 and set(fc.get("codes", [])) >= {w["code"] for w in watch}:
        return fc.get("by", {})
    try:
        import briefing
        by_stock, _ = briefing.flash(watch, hours=24, per_stock=5)
        by = {c: [dict(text=i["text"], url=i.get("url", ""), src="华尔街见闻",
                       date=i["t"].strftime("%m-%d %H:%M")) for i in items] for c, items in by_stock.items()}
    except Exception:
        by = fc.get("by", {})
    cache["flash"] = dict(ts=time.time(), codes=[w["code"] for w in watch], by=by)
    return by


def prepare(R, log=lambda *a: None):
    """在交易机会计算前调用一次：返回 {代码: 消息面}。
    报告运行时 r["extra"] 里有 Yahoo 新闻和财报日；盘中监测（light）没有，就用缓存里最近一次报告抓到的。"""
    cache = _load()
    yc = cache.setdefault("yahoo", {})
    for r in R:
        ex = r.get("extra") or {}
        if ex.get("news") or ex.get("earnings"):
            yc[r["code"]] = dict(ts=time.time(), earnings=str(ex.get("earnings") or ""),
                                 news=[dict(text=n.get("title", ""), src=n.get("src", ""), date=n.get("date", ""),
                                            url=n.get("url", "")) for n in ex.get("news", [])])
    watch = [dict(code=r["code"], name=r["name"], etf=str(r.get("p", {}).get("板块", "")).endswith("ETF"))
             for r in R]
    flash = _flash(watch, cache)
    _save(cache)

    out = {}
    for r in R:
        code = r["code"]
        y = yc.get(code) or {}
        fresh = time.time() - y.get("ts", 0) < 36 * 3600          # 超过 36 小时的旧新闻不再用
        items = list(flash.get(code, [])) + (y.get("news", []) if fresh else [])
        seen, rows = set(), []
        for it in items:
            key = re.sub(r"\W", "", it["text"])[:30]
            if not it["text"] or key in seen:
                continue
            seen.add(key)
            rows.append(dict(it, **classify(it["text"])))
        score = sum(x["s"] for x in rows)
        pos, neg = sum(x["s"] > 0 for x in rows), sum(x["s"] < 0 for x in rows)
        if not rows:
            label = "无消息"
        elif pos and neg and abs(score) <= 1:
            label = "多空交织"
        else:
            label = "利好" if score > 0 else "利空" if score < 0 else "中性"
        ed = None
        try:
            ed = dt.date.fromisoformat((y.get("earnings") or "")[:10]) if y.get("earnings") else None
        except Exception:
            ed = None
        types = []
        for x in rows:
            if x["type"] != "其他" and x["type"] not in types:
                types.append(x["type"])
        out[code] = dict(score=score, label=label, items=rows, types=types, earnings=ed)
    return out


# ---------------------------------------------------------------- 和交易方向对照
def _down(g):
    return {"A": "B", "B": "C", "C": "C"}.get(g, g)


def summary(v, n=2):
    """一句话：利好（财报、评级）：标题1；标题2"""
    if not v or not v["items"]:
        return ""
    heads = [x for x in v["items"] if x["s"] != 0][:n] or v["items"][:n]
    tag = {1: "↑", -1: "↓", 0: "·"}
    return (f"{v['label']}" + (f"（{'、'.join(v['types'][:3])}）" if v["types"] else "") + "："
            + "；".join(f"{tag[x['s']]}{x['text'][:40]}" for x in heads))


def apply(c, v, session):
    """修改交易机会 c 的级别和说明。返回新级别。c 需要已有 grade / direction / kind / play。"""
    g = c.get("grade")
    notes = []
    c["news_view"] = v
    if v:
        sgn = 1 if c["direction"] == "多" else -1
        if v["label"] in ("利好", "利空"):
            if v["score"] * sgn > 0:
                notes.append(f"✓ 消息面配合：{summary(v)}")
                c["news_agree"] = True
            else:
                notes.append(f"✗ 消息面逆向：{summary(v)}")
                c["news_agree"] = False
                if g:
                    g = _down(g)
                    notes[-1] += "（降一级）"
        elif v["label"] == "多空交织":
            notes.append(f"· 消息面多空交织：{summary(v)}")

        ed = v.get("earnings")
        if ed:
            days = (ed - session).days
            if 0 <= days <= 3:
                msg = (f"⚠ 临近财报 {ed:%m-%d}（{days} 天后）：期权里含财报溢价，财报后隐含波动率骤降，"
                       "方向对了也可能亏")
                if c["kind"] != "日内" and g:
                    g = _down(g)
                    msg += "；隔夜 / 波段单降一级"
                else:
                    msg += "；日内单务必收盘前平仓"
                notes.append(msg)
            elif -2 <= days < 0:
                er = [x for x in v["items"] if x["type"] in ("财报", "指引")]
                tone = sum(x["s"] for x in er)
                t = "偏利好" if tone > 0 else "偏利空" if tone < 0 else "好坏参半 / 未识别"
                notes.append(f"· 财报刚发布（{ed:%m-%d}）：新闻定性{t}" +
                             ("，与交易方向一致" if tone * (1 if c["direction"] == "多" else -1) > 0
                              else "，与交易方向相反，留意跳空回补" if tone else ""))
    c["news_notes"] = notes
    if notes:
        c.setdefault("layer_notes", []).extend(notes)
    c["grade"] = g
    return g
