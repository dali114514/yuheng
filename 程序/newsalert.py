# -*- coding: utf-8 -*-
"""
玉衡5.0 · 重大新闻马上推送：每 90 秒看一次华尔街见闻快讯，判断为「重大」的马上发到 Discord #快讯，并顺手归档。

  什么算「重大」（规则，不用 AI，不花钱）：
    ① 点名自选股（ETF 不算）+ 华尔街见闻标成「重要」（红色快讯）；
    ② 点名自选股 + 出现重大事件词（CEO 去留、收购合并、财报 / 指引、调查起诉、禁令、停牌、暴涨暴跌……）；
    ③ 宏观大事：华尔街见闻标「重要」+ 宏观重大词（美联储利率决议、加息降息、CPI / 非农公布、关税、出口管制、股指期货大涨大跌……）。
  附带：涉及股票现在的价格和涨跌（读 moomoo 快照，盘前用盘前价；OpenD 没开就不附）。
  不重复：推过的记在 程序/data/.news_alert_seen.json；只推最近 15 分钟内发生的（重启不会把旧新闻刷一遍）。
  不刷屏：每 10 分钟最多推「每10分钟最多条数」条，同一轮同一只股票的几条合成一条。
  设置：设置.json → 重大新闻推送（第一次运行会写入默认值）。只读，不下单。

  手动看今天哪些会被推（不发送）：arch -arm64 程序/.venv/bin/python 程序/newsalert.py --test [小时数]
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
SEEN = HERE / "data" / ".news_alert_seen.json"

DEFAULTS = {
    "启用": True,
    "说明": "重大新闻马上推到 Discord（newsalert.py）。规则判断，不用 AI。点名自选股 + 华尔街见闻「重要」或重大事件词，或宏观大事，就推。",
    "间隔_秒": 90,
    "时段_美东": ["04:00", "20:00"],
    "只在工作日": True,
    "频道": "快讯",
    "最近多少分钟内的才推": 15,
    "每10分钟最多条数": 8,
    "附带股价": True,
    "强事件词": ["CEO", "首席执行官", "辞职", "离职", "罢免", "复出", "回归", "重返", "恢复", "任命", "收购", "并购", "合并", "要约",
               "分拆", "拆分", "财报", "业绩", "营收", "指引", "预警", "上调指引", "下调指引", "调查", "起诉", "诉讼", "罚款", "禁令",
               "制裁", "出口管制", "停牌", "暂停交易", "破产", "退市", "回购", "增发", "暴涨", "暴跌", "大涨", "大跌", "闪崩", "熔断",
               "涨超", "跌超", "涨近", "跌近", "涨逾", "跌逾", "盘前涨", "盘前跌", "盘后涨", "盘后跌"],
    "宏观重大词": ["利率决议", "降息", "加息", "FOMC", "鲍威尔", "非农", "CPI", "PCE", "GDP", "关税", "出口管制", "制裁",
                "股指期货", "纳指期货", "标普期货", "熔断", "停摆", "违约", "开战", "袭击", "紧急"],
    "涨跌词最少幅度_百分比": 5,
    "排除词": ["早餐", "晚报", "早报", "午报", "要闻汇总", "你需要了解", "日内请重点关注", "提醒：", "商品期货", "主力合约", "期货收盘",
             "A股", "沪指", "深成指", "创业板", "两融", "港股通"],
    "宏观要和美国有关": True,
    "同一只股票冷却_分钟": 5,
}
US_WORDS = ["美国", "美联储", "鲍威尔", "FOMC", "美股", "美债", "美元", "特朗普", "白宫", "纳指", "标普", "道指", "非农", "CPI", "PCE",
            "股指期货", "财政部", "华尔街"]


def settings(cfg=None):
    p = BASE / "设置.json"
    if cfg is None:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    if "重大新闻推送" not in cfg:                    # 第一次：写入默认值（json 读写，不碰其他字段）
        full = json.loads(p.read_text(encoding="utf-8"))
        full["重大新闻推送"] = DEFAULTS
        p.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg = full
    s = dict(DEFAULTS)
    s.update(cfg.get("重大新闻推送") or {})
    return s


def _hm(x):
    h, m = str(x).split(":")
    return int(h) * 60 + int(m)


def in_window(s, now=None):
    now = now or dt.datetime.now(ET)
    if s.get("只在工作日", True) and now.weekday() >= 5:
        return False
    a, b = (s.get("时段_美东") or ["04:00", "20:00"])
    return _hm(a) <= now.hour * 60 + now.minute <= _hm(b)


PCT = re.compile(r"(涨|跌)(?:超|近|逾|幅达|幅超|约)?\s*(\d+(?:\.\d+)?)\s*%")


def judge(item, matchers, etfs, s):
    """一条华尔街见闻快讯 → (是否重大, 原因, 涉及股票)。"""
    title = (item.get("title") or "").strip()
    body = (item.get("content_text") or "").strip()
    full = f"{title} {body}"
    if any(w in full for w in (s.get("排除词") or [])):
        return False, "", []
    head = title or body[:80]                         # 股票要在标题（没有标题就是正文开头）里被点名，正文顺带提到的不算
    codes = [c for c, (_, rx) in matchers.items() if rx.search(head)]
    stocks = [c for c in codes if c not in etfs]
    important = (item.get("score") or 0) >= 2 and item.get("type") != "article"
    strong = [w for w in s["强事件词"] if w in full]
    if strong and all(w in ("涨超", "跌超", "涨近", "跌近", "涨逾", "跌逾", "盘前涨", "盘前跌", "盘后涨", "盘后跌", "大涨", "大跌") for w in strong):
        # 只有涨跌类的词：幅度要够大才算（「涨超 1%」不算）
        pcts = [float(m.group(2)) for m in PCT.finditer(full)]
        if not pcts or max(pcts) < float(s.get("涨跌词最少幅度_百分比") or 5):
            strong = []
    macro = [w for w in s["宏观重大词"] if w in full]
    if macro and s.get("宏观要和美国有关", True) and not any(w in full for w in US_WORDS):
        macro = []
    if stocks and important:
        return True, "点名自选股 · 华尔街见闻标为重要", stocks
    if stocks and strong:
        return True, f"点名自选股 · {strong[0]}", stocks
    if important and macro:
        return True, f"宏观大事 · {macro[0]}", codes
    return False, "", stocks


def _load_seen():
    try:
        d = json.loads(SEEN.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    cut = time.time() - 3 * 86400                     # 只留 3 天
    return {k: v for k, v in d.items() if isinstance(v, (int, float)) and v >= cut}


def _save_seen(d):
    try:
        SEEN.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def quotes(codes):
    """涉及股票现在的价格和涨跌：开盘前用盘前价、收盘后用盘后价。读不到返回 {}。"""
    if not codes:
        return {}
    try:
        import stocks as S
        cfg = json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
        mm = S.Moomoo(cfg.get("moomoo") or {}, lambda *a, **k: None)
    except Exception:
        return {}
    if not mm.ok:
        return {}
    out = {}
    try:
        ret, df = mm.ctx.get_market_snapshot(["US." + c for c in codes])
        if ret != mm.ft.RET_OK:
            return {}
        now = dt.datetime.now(ET)
        m = now.hour * 60 + now.minute
        before, after = m < 570, m >= 960
        for _, r in df.iterrows():
            c = str(r["code"]).replace("US.", "")
            g = lambda k: float(r.get(k) or 0) if k in r and r.get(k) == r.get(k) else 0.0
            last, prev = g("last_price"), g("prev_close_price")
            if before:                                # 开盘前：last_price 才是最近一个收盘（见 CLAUDE.md 已知的坑）
                px, ref, tag = g("pre_price") or g("overnight_price"), last, "盘前"
            elif after:
                px, ref, tag = g("after_price") or last, last, "盘后"
            else:
                px, ref, tag = last, prev, "现价"
            if px and ref:
                out[c] = (tag, px, (px / ref - 1) * 100)
    except Exception:
        return {}
    finally:
        mm.close()
    return out


def fmt(group, q):
    """同一只股票（或同一条宏观）的几条 → 一条 Discord 消息。"""
    head_codes = group["codes"][:3]
    head = "、".join(head_codes) if head_codes else "宏观"
    L = [f"🔴 **重大消息 · {head}**　{group['reason']}"]
    for it in group["items"][:4]:
        t = dt.datetime.fromtimestamp(it["display_time"], dt.timezone.utc).astimezone(ET)
        title = re.sub(r"\s+", " ", (it.get("title") or "")).strip()
        body = re.sub(r"\s+", " ", (it.get("content_text") or "")).strip()
        text = title if title else body
        extra = body if (title and body and body != title) else ""
        line = f"> **美东 {t:%H:%M}** {text[:160]}"
        if extra:
            line += f"\n> {extra[:200]}"
        url = it.get("uri") or ""
        if url:
            line += f"\n> <{url}>"
        L.append(line)
    px = [f"{c} {q[c][0]} ${q[c][1]:.2f}（{q[c][2]:+.1f}%）" for c in head_codes if c in q]
    if px:
        L.append("　" + "；".join(px))
    L.append("-# 规则判断的重大快讯（华尔街见闻），不是买卖建议；按今天的预案和纪律来。")
    return "\n".join(L)


def scan(cfg=None, log=print, send=True):
    """看一轮：返回要发的消息列表（已经去重、合并、限流），并把它们记为已推。"""
    import briefing as B
    import news
    s = settings(cfg)
    watch = news.research(cfg)
    matchers = B.build_matchers(watch)
    etfs = {w["code"] for w in watch if w.get("etf")} | news.ETF
    items = B._fetch_lives(1)
    seen = _load_seen()
    now = time.time()
    fresh_cut = now - float(s.get("最近多少分钟内的才推") or 15) * 60
    cool = float(s.get("同一只股票冷却_分钟") or 5) * 60
    picked = []
    for it in sorted(items, key=lambda i: i.get("display_time", 0)):
        key = str(it.get("id"))
        if key in seen:
            continue
        if it.get("display_time", 0) < fresh_cut:
            seen[key] = now                           # 太旧的：记下，不推
            continue
        ok, why, codes = judge(it, matchers, etfs, s)
        if not ok:
            seen[key] = now
            continue
        k0 = codes[0] if codes else "宏观"
        if now - seen.get(f"code:{k0}", 0) < cool:
            continue                                  # 这只刚推过：先攒着，冷却结束后和后面的合成一条
        seen[key] = now
        picked.append((it, why, codes))
    # 限流：最近 10 分钟已经推了几条
    sent_times = [v for k, v in seen.items() if k.startswith("sent:") and v >= now - 600]
    room = max(0, int(s.get("每10分钟最多条数") or 8) - len(sent_times))
    groups = {}
    for it, why, codes in picked:
        g = groups.setdefault(codes[0] if codes else "宏观", {"codes": [], "items": [], "reason": why})
        g["items"].append(it)
        g["codes"] += [c for c in codes if c not in g["codes"]]
    msgs = []
    if groups:
        q = quotes(sorted({c for g in groups.values() for c in g["codes"][:3]})) if s.get("附带股价", True) else {}
        for k, g in list(groups.items())[:room]:
            msgs.append(fmt(g, q))
            seen[f"sent:{k}:{now}"] = now
            seen[f"code:{k}"] = now
        if len(groups) > room and log:
            log(f"重大新闻：这 10 分钟推得太多，{len(groups) - room} 组没推（已归档）")
    if send:
        _save_seen(seen)
    return msgs


if __name__ == "__main__":
    if "--test" in sys.argv:                          # 拿最近 N 小时的快讯看看哪些会被推（不发送、不记录）
        import briefing as B
        import news
        hrs = int(next((a for a in sys.argv[1:] if a.isdigit()), "24"))
        s = settings()
        watch = news.research()
        matchers = B.build_matchers(watch)
        etfs = {w["code"] for w in watch if w.get("etf")} | news.ETF
        its = B._fetch_lives(hrs)
        hit = [(i, *judge(i, matchers, etfs, s)) for i in its]
        hit = [h for h in hit if h[1]]
        print(f"最近 {hrs} 小时华尔街见闻 {len(its)} 条，其中会推送 {len(hit)} 条：")
        for i, ok, why, codes in sorted(hit, key=lambda h: h[0]["display_time"]):
            t = dt.datetime.fromtimestamp(i["display_time"], dt.timezone.utc).astimezone(ET)
            print(f"  美东 {t:%m-%d %H:%M}  [{why}] {'、'.join(codes) or '宏观'}｜{(i.get('title') or i.get('content_text') or '')[:70]}")
    else:
        for m in scan(send=False):
            print(m, "\n")
