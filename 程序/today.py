# -*- coding: utf-8 -*-
"""
「今日」文件夹：每个交易日一个，放当天的盘前资料、讨论定下的打法和纪律、纪律设置、复盘

  数据/今日/<日期>/
      盘前资料.md      程序自动整理（美东 8:30，或 Discord「今日资料」）：盘前涨跌、夜盘、今天的数据和财报、信息面入口
      夜盘.md          夜盘复盘（夜盘结束后机器人自动写）
      盘前分析.md      Claude 写
      打法和纪律.md    和 Claude 讨论后定稿（给人看）
      纪律设置.json    同一份内容给程序读：盘中监测和纪律程序（guard.py）按它运行
      复盘.md          收盘后和 Claude 一起写

  日期 = 美东交易日（16:00 以后、周末 = 下一个交易日）。
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
ROOT = HERE.parent / "数据" / "今日"
INFO = HERE.parent / "数据" / "冷数据" / "信息面"

TEMPLATE = {
    "日期": "",
    "状态": "草稿",                 # 草稿 / 已锁定
    "偏向": "中性",                 # 偏多 / 偏空 / 中性 / 不做
    "打法": "标准",                 # 激进 / 标准 / 保守
    "一句话": "",
    "观察名单": [],                 # 例：{"代码": "NVDA", "方向": "多", "入场": "C2 回踩 VWAP 确认", "关键价位": [{"价格": 182.5, "说明": "昨天高点"}], "认错价": 176, "备注": ""}
    "限制": {
        "最多笔数": 3,
        "单笔最大亏损_美元": 300,
        "单日最大亏损_美元": 600,
        "最晚开仓": "11:00",
        "只做名单内": True,
        "只做名单方向": True,
    },
    "提醒": [],                     # 例：{"代码": "QQQ", "条件": "跌破", "价格": 600, "说明": "认错：重新评估今天的偏向"}
    "仓位": {"方案": "risk100", "熔断": {"连亏停手": 2, "回吐停手_美元": 200}},   # 见 sizing.py；策略提醒按这个算张数和熔断
    "修改记录": [],
}
SCHEMA_NOTE = """纪律设置.json 字段：
- 状态：草稿 / 已锁定。只有「已锁定」时纪律程序才会弹窗卡纪律（草稿时只显示，不弹窗）。
- 偏向：偏多 / 偏空 / 中性 / 不做；打法：激进 / 标准 / 保守；一句话：今天的核心判断。
- 观察名单：每只 {代码, 方向（多 / 空 / 双向）, 入场（文字）, 关键价位 [{价格, 说明}], 认错价, 备注}。
  例：{"代码": "NVDA", "方向": "多", "入场": "C2 回踩 VWAP 确认", "关键价位": [{"价格": 182.5, "说明": "昨天高点"}], "认错价": 176, "备注": ""}
  早盘 C2 监测只盯这些（加上 QQQ）；碰到关键价位发通知，碰到认错价弹窗。
- 限制：最多笔数、单笔 / 单日最大亏损（美元）、每笔最多张数、最晚开仓（美东 HH:MM）、只做名单内、只做名单方向。
  新建时会按「今日预案」里大盘行情的风险上限收紧（设置.json → 预案 → 行情风险）。
- 提醒：[{代码, 条件（跌破 / 突破）, 价格, 说明}]，触发一次弹一次窗。
- 观察名单每只可以加「策略」：{"入场": 入场id, "止损": 止损id, "止盈": 止盈id}（id 见 程序/intraday.py 的 STRATS / STOPS / TARGETS，
  例如 {"入场": "B", "止损": "time11", "止盈": "close"}）。不写就用今日预案里这只股票、今天这种开盘、这个方向挑的那组；预案也没有就不提醒。
  策略提醒程序（stratwatch.py）按它盯入场和出场，弹窗 + 提示音；盯盘界面也能改（锁定后改要写理由）。
- 仓位：{"方案": one / risk100 / risk200 / risk300 / pyramid / scale / roll, "熔断": {"连亏停手": 笔数或 null, "回吐停手_美元": 金额或 null}}。
  默认 risk100（每笔按止损亏 $100 算张数；2026-11-24 仓位方案回测里回撤最小）+ 连亏 2 笔停、赚到 $200 后回吐一半停。
- 修改记录：盘中改动都要在这里加一条 {时间, 改了什么, 理由}。
"""


def session_day(n=None):
    n = n or dt.datetime.now(ET)
    d = n.date() + dt.timedelta(days=1 if n.hour >= 16 else 0)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def folder(day=None):
    p = ROOT / str(day or session_day())
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_text(name, text, day=None):
    p = folder(day) / name
    p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return p


def load_plan(day=None):
    """读当天的纪律设置；没有或格式坏了返回 None。"""
    p = ROOT / str(day or session_day()) / "纪律设置.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    d.setdefault("限制", {})
    lim = dict(TEMPLATE["限制"])
    lim.update(d["限制"] or {})
    d["限制"] = lim
    d.setdefault("观察名单", [])
    d.setdefault("提醒", [])
    d.setdefault("仓位", json.loads(json.dumps(TEMPLATE["仓位"])))
    for w in d["观察名单"]:
        w["代码"] = str(w.get("代码") or "").upper().strip()
    return d


def watch_codes(day=None):
    p = load_plan(day)
    return [w["代码"] for w in (p or {}).get("观察名单") or [] if w.get("代码")]


def plan_text(p):
    """给人看的一段话（也写进旧的盘前计划，让原来的「没写计划不弹窗」等逻辑照常工作）。"""
    lim = p["限制"]
    names = "、".join(f"{w['代码']}{w.get('方向', '')}" for w in p["观察名单"]) or "无"
    mx, ml = lim.get('最多笔数'), lim.get('单日最大亏损_美元')
    return (f"{p.get('偏向', '')}，{p.get('打法', '')}打法；只做 {names}；"
            f"{f'最多 {mx} 笔' if mx else '笔数不限'}；{f'单日最多亏 ${ml}' if ml else '单日亏损不设上限'}；"
            f"{lim.get('最晚开仓')} 后不开新仓。{p.get('一句话', '')}")


def sync_old_plan(p, day=None):
    """锁定后同步到原来的「盘前计划」（Discord「计划」、复盘对照都用它）。"""
    try:
        import journal
        journal.save_plan(day or session_day(), plan_text(p))
    except Exception:
        pass


# ================================================================ 盘前资料（程序整理，给 Claude 和你看）
def _pct(x):
    return "—" if x is None else f"{x:+.2f}%"


def premarket_quotes(cfg):
    import stocks
    import news
    codes = [w["code"] for w in news.research(cfg)]
    mm = stocks.Moomoo(cfg.get("moomoo") or {}, lambda *a, **k: None)
    out = {}
    n = dt.datetime.now(ET)
    before_open = session_day(n) > n.date() or n.weekday() >= 5 or n.hour * 60 + n.minute < 570
    if not mm.ok:
        return out, "OpenD 没连上，没有盘前行情"
    try:
        ret, df = mm.ctx.get_market_snapshot(["US." + c for c in codes])
        if ret == mm.ft.RET_OK:
            for _, r in df.iterrows():
                c = str(r["code"]).replace("US.", "")
                g = lambda k: float(r.get(k) or 0) if k in r and r.get(k) == r.get(k) else 0.0
                prev, last = g("prev_close_price"), g("last_price")
                # 开盘前（含夜盘、周末）：moomoo 的 prev_close 是「上上个」收盘，last_price 才是最近一个常规时段收盘
                if before_open and last:
                    prev = last
                q = dict(prev=prev, last=last, vol=g("volume"))
                for key, cn in (("pre", "盘前"), ("overnight", "夜盘"), ("after", "盘后")):
                    px = g(f"{key}_price")
                    if px:
                        q[cn] = px
                        q[cn + "%"] = (px / prev - 1) * 100 if prev else None
                out[c] = q
    finally:
        mm.close()
    return out, ""


def make_pack(cfg, log=print, want_brief=False):
    """写 盘前资料.md（数据面 + 信息面合在一起，见 brief.py）。want_brief=True 时同时返回给 Discord 的精简版。"""
    import brief
    day = session_day()
    full, short = brief.build(cfg)
    pw = folder(day) / "期权墙.md"                     # make_full 盘前抓的期权墙（见 flows.py）
    if pw.exists():
        full = full.rstrip() + "\n\n" + pw.read_text(encoding="utf-8").split("\n", 2)[-1]
    pb = None
    try:                                               # 玉衡5.0 今日预案：按行情从策略库挑策略（playbook.py）
        import playbook
        pb = playbook.build(day, log=None, live=True)
        md = playbook.to_md(pb)
        full = full.rstrip() + "\n\n" + md.split("\n## ①")[0].replace("# 今日预案", "## 今日预案（按行情挑策略）", 1) \
            + f"\n完整预案：数据/今日/{day}/预案.md\n"
    except Exception as e:
        full = full.rstrip() + f"\n\n（今日预案没生成：{type(e).__name__}: {e}）\n"
    save_text("盘前资料.md", full, day)
    p = folder(day) / "纪律设置.json"
    if not p.exists():
        t = dict(TEMPLATE, 日期=str(day))
        risk = ((pb or {}).get("market") or {}).get("risk")
        if risk:                                       # 「如果大盘是 X 就……」：按预案收紧（只会比模板更严）
            t["限制"] = dict(t["限制"])
            for k in ("最多笔数", "单日最大亏损_美元"):
                if risk.get(k) is not None and (t["限制"].get(k) is None or risk[k] < t["限制"][k]):
                    t["限制"][k] = risk[k]
            if risk.get("每笔最多张数"):
                t["限制"]["每笔最多张数"] = risk["每笔最多张数"]
            t["一句话"] = f"大盘 {pb['market']['code']}「{pb['market']['regime']}」：按今日预案的风险上限（草稿，和 Claude 讨论后锁定）"
        p.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder(day) / "纪律设置_说明.md").write_text(SCHEMA_NOTE, encoding="utf-8")
    msg = f"今日资料已整理：数据/今日/{day}/盘前资料.md"
    if log:
        log(msg)
    return (msg, short) if want_brief else msg


def make_full(cfg, log=print, run_grok=True, want_brief=False):
    """一键生成盘前报告：抓新闻 → 补夜盘复盘 → 补 Grok 推特（今天还没跑过才跑，按用量收费）→ 更新盘感引擎缓存 → 写盘前资料.md。"""
    day = session_day()
    steps = []
    out_log = log
    log = log or (lambda *a, **k: None)
    try:
        import news
        steps.append("新闻：" + str(news.collect(cfg, 24, log)).split("\n")[0][:80])
    except Exception as e:
        steps.append(f"新闻出错：{type(e).__name__}")
    if not (folder(day) / "夜盘.md").exists():
        try:
            import nightly
            msgs = [m for m in nightly.build(cfg, log=log) if m and m.strip()]
            save_text("夜盘.md", "# 夜盘复盘\n\n" + "\n\n".join(msgs), day)
            steps.append("夜盘复盘：已补跑")
        except Exception as e:
            steps.append(f"夜盘复盘出错：{type(e).__name__}")
    g = cfg.get("Grok盘前") or {}
    if run_grok and g.get("启用") and str(g.get("API密钥") or "").strip():
        try:
            import grokscan
            if (grokscan.load_cache().get("latest") or {}).get("day") != str(dt.datetime.now(ET).date()):
                r = grokscan.run(cfg, log)
                steps.append("Grok 推特：" + (r.get("msg") or f"{len(r.get('posts') or [])} 条，约 ${r.get('cost', 0):.2f}"))
                import news
                news.collect(cfg, 24, log, ("grok",))
            else:
                steps.append("Grok 推特：今天已经跑过")
        except Exception as e:
            steps.append(f"Grok 出错：{type(e).__name__}")
    try:
        import engine
        engine.load_cache(log=None)
    except Exception as e:
        steps.append(f"盘感引擎缓存出错：{type(e).__name__}")
    try:                                               # 期权墙：名单里的 + 自选股前 15 只 + QQQ、SPY
        import flows
        import news
        codes = watch_codes(day) or [w["code"] for w in news.research(cfg)][:15]
        codes = list(dict.fromkeys(["QQQ", "SPY"] + codes))
        flows.write_walls(cfg, day, codes, log=log)
        steps.append(f"期权墙：{len(codes)} 只")
    except Exception as e:
        steps.append(f"期权墙出错：{type(e).__name__}")
    res = make_pack(cfg, None, want_brief)
    msg = (res[0] if want_brief else res) + "\n" + "\n".join(f"· {x}" for x in steps)
    if out_log:
        out_log(msg)
    return (msg, res[1]) if want_brief else msg


if __name__ == "__main__":
    import macro_report as mr
    print(make_full(mr.load_config(), log=None) if "--full" in sys.argv else make_pack(mr.load_config()))
