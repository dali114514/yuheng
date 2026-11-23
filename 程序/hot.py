# -*- coding: utf-8 -*-
"""
热数据：盘中每分钟把实时行情和程序状态写进「数据/热数据」，给 Claude 读

  数据/热数据/
      状态.md          观察名单 + QQQ / SPY：现价、涨跌、VWAP、今日高低、离关键价位多远、C1/C2 走到哪一步；纪律状态
      1分钟/代码.csv   当天常规时段到目前为止的 1 分钟K（含 VWAP 列），只放已经收盘的K线
      纪律.md          纪律程序写（guard.py）
  时间：美东工作日 7:00–9:30 每 5 分钟（盘前快照），9:30–16:00 每分钟。
  名单：今天纪律设置里的观察名单 + QQQ、SPY；还没定打法时用大盘股名单。只读行情，不下单。
"""
import datetime as dt
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
HOT = BASE / "数据" / "热数据"
PID_FILE = HERE / ".hot.pid"


def et_now():
    return dt.datetime.now(ET)


def cfg_all():
    try:
        return json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def codes_today():
    import today
    lst = today.watch_codes()
    if not lst:
        import history
        lst = sorted(history.big_list())
    for c in ("QQQ", "SPY"):
        if c not in lst:
            lst.append(c)
    return lst


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_bars(code, b):
    """写 1分钟/代码.csv（含 VWAP）；返回最后一根的摘要 dict。"""
    if b is None or not len(b):
        return None
    tp = (b["high"] + b["low"] + b["close"]) / 3
    vw = (tp * b["volume"]).cumsum() / b["volume"].cumsum().where(b["volume"].cumsum() > 0)
    d = b.copy()
    d["vwap"] = vw.round(4)
    d.index.name = "time_et"
    (HOT / "1分钟").mkdir(parents=True, exist_ok=True)
    out = d.reset_index()
    out["time_et"] = out["time_et"].dt.strftime("%Y-%m-%d %H:%M")
    tmp = HOT / "1分钟" / f"{code}.tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, HOT / "1分钟" / f"{code}.csv")
    last = d.iloc[-1]
    return dict(t=d.index[-1].strftime("%H:%M"), last=float(last["close"]), open=float(d["open"].iloc[0]),
                high=float(d["high"].max()), low=float(d["low"].min()), vwap=float(last["vwap"]) if last["vwap"] == last["vwap"] else None,
                n=len(d))


def status_md(rows, prev_close, atr, note="", flow_md=""):
    import today
    plan = today.load_plan() or {}
    levels = {w["代码"]: w for w in plan.get("观察名单") or []}
    c2 = (_read_json(HERE / "data" / ".c2_status.json").get("codes") or {})
    n = et_now()
    L = [f"# 热数据 · 状态（美东 {n:%m-%d %H:%M} 更新）", ""]
    if note:
        L += [note, ""]
    L += ["| 代码 | 时间 | 现价 | 对昨收 % | 对开盘 % | VWAP | 离 VWAP（倍 ATR） | 今日高 / 低 | 今日振幅 / ATR | C1/C2 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for code, r in rows.items():
        if not r:
            L.append(f"| {code} | — | 读不到 | | | | | | | |")
            continue
        pc, a = prev_close.get(code), atr.get(code)
        ch = (r["last"] / pc - 1) * 100 if pc else None
        co = (r["last"] / r["open"] - 1) * 100 if r.get("open") else None
        dv = (r["last"] - r["vwap"]) / a if a and r.get("vwap") else None
        rg = (r["high"] - r["low"]) / a if a else None
        cs = c2.get(code) or {}
        f2 = lambda x, fmt: "" if x is None else format(x, fmt)
        cells = [code, r["t"], f"{r['last']:.2f}", f2(ch, "+.2f"), f2(co, "+.2f"), f2(r.get("vwap"), ".2f"), f2(dv, "+.2f"),
                 f"{r['high']:.2f} / {r['low']:.2f}", f2(rg, ".2f"), (cs.get("phase") or "") + (" " + cs["dir"] if cs.get("dir") else "")]
        L.append("| " + " | ".join(cells) + " |")
    lv_lines = []
    for code, w in levels.items():
        r = rows.get(code)
        if not r:
            continue
        for lv in w.get("关键价位") or []:
            p = lv.get("价格")
            if p:
                lv_lines.append(f"- {code} {lv.get('说明') or '关键价位'} {p}：现价 {r['last']:.2f}，差 {(r['last'] - p) / p * 100:+.2f}%")
        if w.get("认错价"):
            lv_lines.append(f"- {code} 认错价 {w['认错价']}：现价 {r['last']:.2f}，差 {(r['last'] - w['认错价']) / w['认错价'] * 100:+.2f}%")
    if lv_lines:
        L += ["", "## 离今天的关键价位", ""] + lv_lines
    c2r = [f"- {c}：{v.get('reason', '')}" + (f"　AI：{v['ai'].get('stance')} {v['ai'].get('reason')}" if v.get("ai") else "")
           for c, v in c2.items() if v.get("phase") in ("C1", "回踩中", "入场")]
    if c2r:
        L += ["", "## 早盘 C2 监测", ""] + c2r
    if flow_md:
        L += ["", flow_md.rstrip()]
    L += ["", "纪律状态见 `热数据/纪律.md`；形态和震荡见 `热数据/形态.md`；今天的计划见 `今日/日期/打法和纪律.md`；1 分钟K见 `热数据/1分钟/`。"]
    (HOT / "状态.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def premarket(cfg, codes):
    import today
    q, err = today.premarket_quotes(cfg)
    n = et_now()
    L = [f"# 热数据 · 盘前（美东 {n:%m-%d %H:%M} 更新）", "", "常规时段还没开盘，下面是盘前 / 夜盘价格。", "",
         "| 代码 | 昨收 | 盘前 | 盘前 % |", "| --- | --- | --- | --- |"]
    for c in codes:
        x = q.get(c) or {}
        L.append(f"| {c} | {x.get('prev', 0):.2f} | {x.get('盘前') or 0:.2f} | "
                 f"{'' if x.get('盘前%') is None else format(x['盘前%'], '+.2f')} |")
    if err:
        L += ["", err]
    HOT.mkdir(parents=True, exist_ok=True)
    (HOT / "状态.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: (PID_FILE.unlink(missing_ok=True), sys.exit(0)))
    print("热数据启动", flush=True)
    import c2
    import c2watch
    feed, day, last_pre = None, None, 0.0
    prev_close, atr, codes = {}, {}, []
    while True:
        try:
            cfg = cfg_all()
            n = et_now()
            m = n.hour * 60 + n.minute
            if n.weekday() >= 5:
                time.sleep(300)
                continue
            if n.date() != day:                                   # 新的一天：清掉昨天的 1 分钟文件
                day = n.date()
                for p in (HOT / "1分钟").glob("*.csv") if (HOT / "1分钟").exists() else []:
                    p.unlink(missing_ok=True)
                prev_close, atr, codes = {}, {}, []
            if 420 <= m < 570:
                if time.time() - last_pre >= 300:
                    last_pre = time.time()
                    premarket(cfg, codes_today())
                time.sleep(30)
                continue
            if 570 <= m <= 961:
                new = codes_today()
                if new != codes or feed is None or not feed.ok:
                    codes = new
                    if feed is not None:
                        feed.close()
                    feed = c2watch.Feed(cfg, codes)
                    if not feed.ok:
                        HOT.mkdir(parents=True, exist_ok=True)
                        (HOT / "状态.md").write_text(f"# 热数据\n\n美东 {n:%H:%M}：OpenD 没连上，没有实时行情。\n", encoding="utf-8")
                        feed = None
                        time.sleep(20)
                        continue
                for c in codes:
                    if c not in atr:
                        atr[c] = c2.daily_atr(c, str(n.date()))
                        prev_close[c] = c2.prev_close(c, str(n.date()))
                rows = {}
                for c in codes:
                    try:
                        rows[c] = write_bars(c, feed.bars(c, n))
                    except Exception:
                        rows[c] = None
                fmd = ""
                try:                                              # 资金流向（moomoo 口径，见 flows.py）
                    import flows
                    got = {}
                    fmd = flows.capital_md(feed.mm, codes, got)
                    (HERE / "data" / ".flow_status.json").write_text(json.dumps(dict(t=time.time(), codes=got), ensure_ascii=False), encoding="utf-8")
                except Exception:
                    fmd = ""
                status_md(rows, prev_close, atr, flow_md=fmd)
                time.sleep(max(1.0, 63 - et_now().second))       # 下一分钟第 3 秒
                continue
            if feed is not None:
                feed.close()
                feed = None
            time.sleep(60)
        except Exception:
            print("出错：" + traceback.format_exc().splitlines()[-1], flush=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
