# -*- coding: utf-8 -*-
"""
今日环境卡：盘前把全部资料交给 AI，输出结构化的「今天市场在交易什么」，和规则结论并排；收盘后给两边打分

  ① 收集资料（daily_inputs）：宏观六指标与规则结论、传导链、经济日历、板块与个股、期权墙、夜盘、Grok 雷达、
     Reddit 热度、新闻快讯。每条资料一个编号，AI 的每个结论都要引用编号。
  ② 分三步问 AI（14B 本地模型一次读不完全部资料，分步更稳）：宏观判断 → 板块与个股 → 汇总成环境卡
  ③ 防编造：引用了不存在编号的结论删掉；结论里出现的数字和资料对不上，整句删掉；删了什么都记下来
  ④ 存进 regime_cards；收盘复盘时按 QQQ 和板块 ETF 的实际涨跌给「规则」和「AI」各打一次分（verdict_scores）
只展示，不影响信号和弹窗（按方案第四阶段）。
"""
import datetime as dt
import json
import re
from pathlib import Path

import llm
import store

HERE = Path(__file__).resolve().parent
NIGHTLY_CACHE = HERE / ".nightly_cache.json"
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS daily_inputs (
        date TEXT, id INTEGER, source TEXT, label TEXT, text TEXT, PRIMARY KEY (date, id))""",
    """CREATE TABLE IF NOT EXISTS regime_cards (
        date TEXT, version INTEGER, created_at TEXT, backend TEXT, model TEXT, trigger TEXT, rule_verdict TEXT,
        card TEXT, removed TEXT, PRIMARY KEY (date, version))""",
    """CREATE TABLE IF NOT EXISTS verdict_scores (
        date TEXT, item TEXT, source TEXT, pred TEXT, actual TEXT, ret REAL, score REAL, PRIMARY KEY (date, item, source))""",
]
BIAS = ["偏多", "偏空", "中性"]


def connect(path=None):
    con = store.connect(path)
    for s in SCHEMA:
        con.execute(s)
    return con


def et_now():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York"))


def session_day(n=None):
    """环境卡是给哪个交易日的：美东 16:00 前 = 今天，之后 / 周末 = 下一个交易日。"""
    n = n or et_now()
    d = n.date() + dt.timedelta(days=1 if n.hour >= 16 else 0)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def f2(x, nd=2):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return "—"


# ================================================================ ① 收集资料
def save_nightly(msgs):
    """机器人发完夜盘复盘后存一份，环境卡直接用，不用重算。"""
    try:
        NIGHTLY_CACHE.write_text(json.dumps(dict(day=str(session_day()), at=et_now().isoformat(), msgs=msgs),
                                            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _read(name):
    try:
        return json.loads((HERE / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def collect(cfg, log=print, day=None, A=None, S=None):
    """返回 (items, rule_verdict)。items: [dict(id, source, label, text)]。A / S 可以传进来（测试用）。"""
    day = day or session_day()
    items = []

    def add(source, label, text):
        text = re.sub(r"\s+", " ", str(text)).strip()
        if text:
            items.append(dict(id=len(items) + 1, source=source, label=label, text=text[:300]))

    # 宏观六指标 + 规则结论
    import macro_report as mr
    if A is None:
        data, _ = mr.fetch_data(cfg)
        A = mr.analyze(data, cfg)
    names = {"VIX": "VIX", "VXN": "VXN（纳指波动率）", "QQQ": "QQQ", "BRENT": "布伦特原油", "US10Y": "美债10年期收益率",
             "DXY": "美元指数"}
    for k, st in (A.get("st") or {}).items():
        if not st:
            continue
        add("宏观", names.get(k, k), f"{names.get(k, k)} 最新 {f2(st.get('last'), 3 if k == 'US10Y' else 2)}，"
            f"1日 {f2(st.get('c1'))}%，5日 {f2(st.get('c5'))}%，20日 {f2(st.get('c20'))}%，"
            f"相对20日均线 {f2(st.get('vs_ma20'))}%，1年分位 {f2(st.get('pctile'), 0)}%"
            + (f"，{A['level_status'][k]}" if (A.get("level_status") or {}).get(k) else ""))
    rule = A.get("verdict") or ""
    add("规则结论", "规则判断", f"规则判断：{rule}（综合分 {A.get('score')}，今日 {A.get('off1')} 个指标指向避险、"
        f"{A.get('on1')} 个指向风险偏好；波动环境：{A.get('regime')}，{A.get('regime_desc')}）")
    for lv, t in (A.get("alerts") or [])[:8]:
        add("宏观警报", f"警报（{lv}）", t)
    for c in (A.get("chain") or [])[:6]:
        add("传导链", c.get("title", ""), f"{c.get('title')}：{c.get('why')}；今日{'联动' if c.get('c1') else '未联动'}，"
            f"近5日{'联动' if c.get('c5') else '未联动'}")

    # 经济日历（今天，美国，中高影响）
    for e in (_read(".calendar_cache.json") or []):
        if str(e.get("date", ""))[:10] == str(day) and e.get("country") == "USD" and e.get("impact") in ("High", "Medium"):
            add("经济日历", e.get("title", ""), f"{str(e['date'])[11:16]} 美东 {e.get('title')}（{'高' if e['impact'] == 'High' else '中'}影响）"
                + (f"，预期 {e['forecast']}" if e.get("forecast") else "") + (f"，前值 {e['previous']}" if e.get("previous") else ""))

    # 板块与个股
    if S is None:
        try:
            import stocks
            S = stocks.run(cfg, log=lambda *a, **k: None, light=True)
            if S.get("mm") is not None:
                S["mm"].close()
        except Exception as e:
            log(f"  · 板块数据读取失败：{e}")
            S = {"sectors": [], "results": []}
    for s in S.get("sectors") or []:
        add("板块", s["name"], f"{s['name']}（{s.get('etf') or '无ETF'}）：ETF 1日 {f2(s.get('e1'))}%，5日 {f2(s.get('e5'))}%；"
            f"成员平均 {f2(s.get('s1'))}%，上涨 {s.get('ups')}/{s.get('n')} 只")
    for r in S.get("results") or []:
        sig = [t for lv, t in (r.get("signals") or []) if lv in ("高", "中")][:2]
        if abs(r.get("c1") or 0) >= 2 or sig:
            add("个股", r["code"], f"{r['code']} {r.get('name', '')} 收 {f2(r.get('last'))}，1日 {f2(r.get('c1'))}%，"
                f"5日 {f2(r.get('c5'))}%，RSI {f2(r.get('rsi'), 0)}" + (f"；{'；'.join(sig)}" if sig else ""))

    # 期权墙
    for code, w in (_read(".walls_cache.json") or {}).items():
        ws = w.get("walls") or []
        if ws:
            add("期权墙", code, f"{code} 期权墙：" + "，".join(
                f"{x.get('type', x.get('side', ''))} {x.get('strike', x.get('price', ''))}" for x in ws[:4]))

    # 夜盘
    nc = _read(".nightly_cache.json")
    if nc and nc.get("day") == str(day):
        for m in nc.get("msgs") or []:
            for line in str(m).splitlines():
                line = re.sub(r"[*_`>#]", "", line).strip(" -·")
                if len(line) > 8:
                    add("夜盘", "夜盘复盘", line)
                if sum(1 for i in items if i["source"] == "夜盘") >= 20:
                    break

    # Grok 雷达（X 帖子）
    g = (_read(".grok_cache.json") or {}).get("latest") or {}
    if g.get("day") == str(day) and g.get("synth"):
        o = g["synth"].get("overall") or {}
        add("Grok雷达", "整体倾向", f"X 帖子整体：{o.get('bias')}（{o.get('confidence')}）；" + "；".join(o.get("drivers") or [])
            + (f"；{o['conflict']}" if o.get("conflict") else ""))
        for code, v in list((g["synth"].get("stocks") or {}).items())[:12]:
            im = v.get("impact") or {}
            add("Grok雷达", code, f"{code}：{v.get('what')}（{im.get('direction')}，{im.get('strength')}，{im.get('why')}）")
        for p in sorted(g.get("posts") or [], key=lambda p: -(p.get("importance") or 0))[:8]:
            add("X帖子", "@" + str(p.get("handle")), f"@{p.get('handle')}（{p.get('cat')}）：{p.get('summary')}")

    # Reddit 热度
    rc = _read(".reddit_cache.json") or {}
    H = (rc.get("last_heat") or {}).get("H") or {}
    if H and str((rc.get("last_heat") or {}).get("t", ""))[:10] >= str(day - dt.timedelta(days=1)):
        top = sorted(H.items(), key=lambda x: -x[1].get("mentions", 0))[:8]
        add("Reddit", "散户热度", "Reddit 提及最多：" + "，".join(
            f"{t} {d.get('mentions')} 次（24小时前 {d.get('m24')}）" for t, d in top))

    # 新闻快讯
    nw = (_read(".news_cache.json") or {}).get("flash") or {}
    n = 0
    for code, lst in (nw.get("by") or {}).items():
        for x in lst[:2]:
            add("新闻", code, f"{code} 相关快讯：{x.get('text') or x.get('title')}（{x.get('src', '')} {x.get('date', '')}）")
            n += 1
        if n >= 16:
            break

    con = connect()
    try:
        with con:
            con.execute("DELETE FROM daily_inputs WHERE date = ?", (str(day),))
            con.executemany("INSERT INTO daily_inputs VALUES (?,?,?,?,?)",
                            [(str(day), i["id"], i["source"], i["label"], i["text"]) for i in items])
    finally:
        con.close()
    return items, rule


# ================================================================ ② 问 AI
SYSTEM = ("你是美股期权交易者的盘前分析员。只能根据给你的编号资料下结论，资料里没有的不要猜，也不要引用资料以外的数字。"
          "每个结论都必须在 evidence 里写出依据的资料编号。资料之间互相矛盾时写进 conflicts，不要强行得出结论。"
          "Reddit 散户情绪只能用来判断是否过热，不能单独作为方向依据。用简体中文，句子短。")

EV = {"type": "array", "items": {"type": "integer"}}
S_MACRO = {"type": "object", "required": ["market_bias", "confidence", "reasons", "vs_rule"], "properties": {
    "market_bias": {"type": "string", "enum": BIAS}, "confidence": {"type": "string", "enum": ["高", "中", "低"]},
    "reasons": {"type": "array", "items": {"type": "object", "required": ["text", "evidence"],
                "properties": {"text": {"type": "string"}, "evidence": EV}}},
    "vs_rule": {"type": "string"}}}
S_SECT = {"type": "object", "required": ["sectors", "stocks"], "properties": {
    "sectors": {"type": "array", "items": {"type": "object", "required": ["name", "bias", "reason", "evidence"],
                "properties": {"name": {"type": "string"}, "bias": {"type": "string", "enum": BIAS},
                               "reason": {"type": "string"}, "evidence": EV}}},
    "stocks": {"type": "array", "items": {"type": "object", "required": ["code", "bias", "special", "evidence"],
               "properties": {"code": {"type": "string"}, "bias": {"type": "string", "enum": BIAS},
                              "special": {"type": "string"}, "evidence": EV}}}}}
S_CARD = {"type": "object", "required": ["main_theme", "market_bias", "confidence", "events", "conflicts", "avoid"],
          "properties": {
              "main_theme": {"type": "string"}, "main_theme_evidence": EV,
              "market_bias": {"type": "string", "enum": BIAS}, "confidence": {"type": "string", "enum": ["高", "中", "低"]},
              "events": {"type": "array", "items": {"type": "object", "required": ["time_et", "what", "impact"],
                         "properties": {"time_et": {"type": "string"}, "what": {"type": "string"},
                                        "impact": {"type": "string", "enum": ["高", "中", "低"]}, "evidence": EV}}},
              "conflicts": {"type": "array", "items": {"type": "object", "required": ["text", "evidence"],
                            "properties": {"text": {"type": "string"}, "evidence": EV}}},
              "avoid": {"type": "array", "items": {"type": "object", "required": ["text", "evidence"],
                        "properties": {"text": {"type": "string"}, "evidence": EV}}}}}


def _fmt(items):
    return "\n".join(f"[{i['id']}] （{i['source']}）{i['text']}" for i in items)


def ask(cfg, items, backend, log=print):
    pick = lambda *src: [i for i in items if i["source"] in src]
    macro_items = pick("宏观", "规则结论", "宏观警报", "传导链", "经济日历")
    m, model = llm.chat_json(cfg, "环境卡-宏观", SYSTEM,
                             "资料：\n" + _fmt(macro_items) + "\n\n任务：判断今天美股大盘（以 QQQ 为准）偏多、偏空还是中性，"
                             "给 2–4 条理由（每条 ≤40 字，写出依据编号），并用一句话说明和「规则判断」一致还是不一致、为什么。",
                             S_MACRO, backend)
    log(f"  · 宏观判断：{m.get('market_bias')}")
    sect_items = pick("板块", "个股", "期权墙", "Grok雷达", "新闻")
    s, _ = llm.chat_json(cfg, "环境卡-板块", SYSTEM,
                         "资料：\n" + _fmt(sect_items) + "\n\n任务：逐个板块判断今天偏多、偏空还是中性（理由 ≤40 字，写依据编号）；"
                         "再挑出今天需要特别对待的个股（最多 6 只：有重大消息、临近财报、异常波动），写明特殊在哪（≤40 字）。",
                         S_SECT, backend)
    log(f"  · 板块：{len(s.get('sectors') or [])} 个，个股：{len(s.get('stocks') or [])} 只")
    rest = pick("夜盘", "X帖子", "Reddit", "经济日历", "Grok雷达", "规则结论")
    c, _ = llm.chat_json(cfg, "环境卡-汇总", SYSTEM,
                         "资料：\n" + _fmt(rest) + "\n\n已经得出的宏观判断：" + json.dumps(m, ensure_ascii=False)
                         + "\n已经得出的板块判断：" + json.dumps(s.get("sectors") or [], ensure_ascii=False)
                         + "\n\n任务：汇总成今日环境卡。main_theme 用一句话写今天市场在交易什么主线（≤30 字，写依据编号）；"
                         "market_bias 原则上沿用宏观判断，除非夜盘和消息面明显相反（相反时写进 conflicts）；"
                         "events 列今天有时间点的风险事件；avoid 写今天要避免的做法（最多 3 条）。",
                         S_CARD, backend)
    card = dict(c)
    card["macro_reasons"], card["macro_vs_rule"] = m.get("reasons") or [], m.get("vs_rule", "")
    card["macro_bias"] = m.get("market_bias")
    card["sectors"], card["stocks"] = s.get("sectors") or [], s.get("stocks") or []
    return card, model


# ================================================================ ③ 防编造
NUM = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")


def _allowed_numbers(items):
    vals = set()
    for i in items:
        for x in NUM.findall(i["text"]):
            try:
                vals.add(abs(float(x)))
            except Exception:
                pass
    return vals


def _num_ok(text, allowed):
    """句子里每个数字都要能在资料里找到（允许四舍五入差异）。小于 10 的整数（「3 条」「2 只」）不查。"""
    for x in NUM.findall(text or ""):
        v = abs(float(x))
        if v == int(v) and v < 10:
            continue
        if not any(abs(v - a) <= max(0.051, abs(a) * 0.005) for a in allowed):
            return False, x
    return True, None


def verify(card, items):
    ids = {i["id"] for i in items}
    allowed = _allowed_numbers(items)
    removed = []

    def keep(obj, field, where):
        ev = [e for e in (obj.get("evidence") or []) if e in ids]
        obj["evidence"] = ev
        if not ev:
            removed.append(f"{where}：「{obj.get(field)}」没有有效的资料编号")
            return False
        ok, bad = _num_ok(obj.get(field), allowed)
        if not ok:
            removed.append(f"{where}：「{obj.get(field)}」里的数字 {bad} 在资料里找不到")
            return False
        return True

    for key, field, where in (("macro_reasons", "text", "宏观理由"), ("sectors", "reason", "板块"),
                              ("stocks", "special", "个股"), ("conflicts", "text", "矛盾点"), ("avoid", "text", "避免")):
        card[key] = [o for o in (card.get(key) or []) if keep(o, field, where)]
    card["events"] = [e for e in (card.get("events") or []) if _num_ok(e.get("what"), allowed)[0]]
    ok, bad = _num_ok(card.get("main_theme"), allowed)
    if not ok or not [e for e in (card.get("main_theme_evidence") or []) if e in ids]:
        removed.append(f"主线：「{card.get('main_theme')}」" + (f"里的数字 {bad} 在资料里找不到" if not ok else "没有有效的资料编号"))
        card["main_theme"] = "（主线判断没有通过核对，已删除）"
    return card, removed


# ================================================================ 生成 / 读取 / 显示
def generate(cfg, log=print, trigger="盘前", day=None, A=None, S=None):
    """生成并保存一版环境卡。返回 (card, 说明)；失败返回 (None, 原因)。"""
    day = day or session_day()
    backend, why = llm.backend_for(cfg)
    if backend is None:
        return None, f"✗ 环境卡没有生成：{why}"
    log(f"  · 环境卡用{backend}模型（{why}）")
    items, rule = collect(cfg, log, day, A=A, S=S)
    try:
        card, model = ask(cfg, items, backend, log)
    except Exception as e:
        return None, f"✗ 环境卡生成失败（{backend}）：{type(e).__name__}: {str(e)[:120]}"
    card, removed = verify(card, items)
    con = connect()
    try:
        v = (con.execute("SELECT MAX(version) FROM regime_cards WHERE date = ?", (str(day),)).fetchone()[0] or 0) + 1
        with con:
            con.execute("INSERT INTO regime_cards VALUES (?,?,?,?,?,?,?,?,?)",
                        (str(day), v, dt.datetime.now().isoformat(timespec="seconds"), backend, model, trigger, rule,
                         json.dumps(card, ensure_ascii=False), json.dumps(removed, ensure_ascii=False)))
    finally:
        con.close()
    return card, f"{backend} · {model} · 第 {v} 版 · 资料 {len(items)} 条" + (f" · 删掉 {len(removed)} 句" if removed else "")


def latest(day=None, before=None, path=None):
    """读某天最新一版（before：只看这个时间之前生成的）。返回 dict(card, backend, model, rule, items, removed, version) 或 None。"""
    day = day or session_day()
    con = connect(path)
    try:
        q, a = "SELECT version, backend, model, rule_verdict, card, removed, created_at FROM regime_cards WHERE date = ?", [str(day)]
        if before:
            q += " AND created_at <= ?"
            a.append(before)
        r = con.execute(q + " ORDER BY version DESC LIMIT 1", a).fetchone()
        if not r:
            return None
        labels = {i: (s, l) for i, s, l in con.execute("SELECT id, source, label FROM daily_inputs WHERE date = ?", (str(day),))}
    finally:
        con.close()
    return dict(version=r[0], backend=r[1], model=r[2], rule=r[3], card=json.loads(r[4]), removed=json.loads(r[5] or "[]"),
                created_at=r[6], labels=labels, day=str(day))


def rule_bias(v):
    v = str(v or "")
    return "偏空" if "避险" in v else "偏多" if "风险偏好" in v else "中性"


def fmt(L):
    """Discord 文字。"""
    if not L:
        return "还没有今天的环境卡。"
    c, lab = L["card"], L["labels"]
    src = lambda ev: "、".join(f"{lab.get(e, ('?', ''))[0]}·{lab.get(e, ('', '?'))[1]}" for e in (ev or [])[:3])
    agree = "一致" if rule_bias(L["rule"]) == c.get("market_bias") else "不一致"
    out = [f"**今日环境卡 · {L['day']}（{'本地 ' if L['backend'] == '本地' else '云端 '}{L['model']}）**",
           f"主线：{c.get('main_theme')}",
           f"大盘：AI **{c.get('market_bias')}**（信心{c.get('confidence')}）｜规则 {L['rule']} → {agree}"]
    if c.get("macro_vs_rule"):
        out.append(f"-# {c['macro_vs_rule']}")
    for r in c.get("macro_reasons") or []:
        out.append(f"· {r['text']}　〔{src(r['evidence'])}〕")
    if c.get("sectors"):
        out.append("\n**板块**")
        for s in c["sectors"]:
            out.append(f"· {s['name']} {s['bias']}：{s['reason']}")
    if c.get("stocks"):
        out.append("\n**特别关注**")
        for s in c["stocks"]:
            out.append(f"· {s['code']}（{s['bias']}）：{s['special']}")
    if c.get("events"):
        out.append("\n**今日事件**")
        for e in c["events"]:
            out.append(f"· {e.get('time_et')} {e.get('what')}（{e.get('impact')}）")
    if c.get("conflicts"):
        out.append("\n**矛盾点**")
        out += [f"· {x['text']}" for x in c["conflicts"]]
    if c.get("avoid"):
        out.append("\n**今天避免**")
        out += [f"· {x['text']}" for x in c["avoid"]]
    if L["removed"]:
        out.append(f"\n-# 核对时删掉 {len(L['removed'])} 句（没有来源或数字对不上），记录在数据库 regime_cards")
    out.append("-# 环境卡只做参考，不影响信号和弹窗；收盘后会按实际走势给 AI 和规则各打一次分")
    return "\n".join(out)


# ================================================================ ④ 收盘打分
def _actual(ret, band):
    return "偏多" if ret > band else "偏空" if ret < -band else "中性"


def _score(pred, actual):
    if pred == actual:
        return 1.0
    if "中性" in (pred, actual):
        return 0.5
    return 0.0


def score_day(day, path=None, hist_path=None):
    """用开盘前最后一版环境卡（和当时的规则判断）对照当天收盘涨跌。返回 [行]。"""
    import history
    L = latest(day, before=f"{day}T09:30:00", path=path) or latest(day, path=path)
    if not L:
        return []
    rows = []

    def ret_of(code):
        k = history.load(code, "1d", path=hist_path)
        k = k[k.index.date <= day] if len(k) else k
        if len(k) < 2 or k.index[-1].date() != day:
            return None
        return float(k["close"].iloc[-1] / k["close"].iloc[-2] - 1) * 100

    q = ret_of("QQQ")
    if q is not None:
        act = _actual(q, 0.5)
        for src, pred in (("AI", L["card"].get("market_bias")), ("规则", rule_bias(L["rule"]))):
            rows.append((str(day), "大盘", src, pred, act, q, _score(pred, act)))
    import sectors as SEC
    for s in L["card"].get("sectors") or []:
        etf = SEC.etf(s["name"]) if hasattr(SEC, "etf") else None
        r = ret_of(etf) if etf else None
        if r is not None:
            act = _actual(r, 0.7)
            rows.append((str(day), "板块:" + s["name"], "AI", s["bias"], act, r, _score(s["bias"], act)))
    con = connect(path)
    try:
        with con:
            con.executemany("INSERT OR REPLACE INTO verdict_scores VALUES (?,?,?,?,?,?,?)", rows)
    finally:
        con.close()
    return rows


def score_text(day, path=None):
    con = connect(path)
    try:
        today = con.execute("SELECT source, pred, actual, ret, score FROM verdict_scores WHERE date = ? AND item = '大盘'",
                            (str(day),)).fetchall()
        tot = con.execute("SELECT source, COUNT(*), AVG(score) FROM verdict_scores WHERE item = '大盘' GROUP BY source").fetchall()
        sec = con.execute("SELECT COUNT(*), AVG(score) FROM verdict_scores WHERE item LIKE '板块:%' AND date = ?",
                          (str(day),)).fetchone()
    finally:
        con.close()
    if not today:
        return None
    d = {s: (p, a, r, sc) for s, p, a, r, sc in today}
    any_ = next(iter(d.values()))
    mark = lambda sc: "✓" if sc == 1 else "△" if sc == 0.5 else "✗"
    line = f"环境判断：QQQ {any_[2]:+.2f}%（{any_[1]}）｜" + "，".join(
        f"{s} {d[s][0]} {mark(d[s][3])}" for s in ("AI", "规则") if s in d)
    if sec and sec[0]:
        line += f"｜AI 板块 {sec[0]} 个，得分 {sec[1] * 100:.0f}%"
    acc = {s: (n, a) for s, n, a in tot}
    if acc:
        line += "\n累计：" + "，".join(f"{s} {acc[s][1] * 100:.0f}%（{acc[s][0]} 天）" for s in ("AI", "规则") if s in acc)
        if min(n for n, _ in acc.values()) < 20:
            line += "（不到 20 个交易日，先别下结论）"
    return line
