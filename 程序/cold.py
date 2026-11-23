# -*- coding: utf-8 -*-
"""
冷数据·数据面：把 K 线库导出成「每只股票一个文件夹」，给 Claude 直接读

  数据/冷数据/股票/<代码>/
      摘要.md          现价、趋势、波幅、关键价位、近 20 天每天的走势、C2 打法在它身上的历史表现
      日线.csv         全部日线
      5分钟/年-月.csv  含盘前盘后（session 列标明）
      1分钟/年-月.csv  常规时段
  数据/冷数据/股票/总览.md   所有股票一张表
  数据/说明.md               文件夹怎么用

  只用 Python 自带的库（sqlite3、csv），哪里都能跑。
  每天收盘复盘后自动更新：日线和摘要重写，分钟线只重写最近两个月的文件（full=True 全部重写）。
  用法：python3 cold.py [--full] [代码 …]
"""
import csv
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
DB = HERE / "data" / "history.db"
ROOT = BASE / "数据"
COLD = ROOT / "冷数据" / "股票"
C2_BY_CODE = HERE / "data" / ".c2_by_code.json"
TABLES = {"1d": "bars_1d", "5m": "bars_5m", "1m": "bars_1m"}
HEAD = ["time_et", "open", "high", "low", "close", "volume", "session"]


def _con():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    return con


def codes_all(con):
    out = set()
    for t in TABLES.values():
        out |= {r[0] for r in con.execute(f"SELECT DISTINCT code FROM {t}")}
    return sorted(out)


def _session(ts):
    hm = int(ts[11:13]) * 60 + int(ts[14:16])
    if hm <= 570:
        return "盘前"
    if hm <= 960:
        return "盘中"
    return "盘后"


def _r(x, nd=4):
    return round(float(x), nd)


def _write_csv(path, rows, head=HEAD):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)
    os.replace(tmp, path)


# ================================================================ 导出 K 线
def export_daily(con, code):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1d WHERE code = ? ORDER BY ts", (code,)).fetchall()
    out, prev = [], None
    for ts, o, h, l, c, v in rows:
        chg = (c / prev - 1) * 100 if prev else ""
        out.append([ts[:10], _r(o), _r(h), _r(l), _r(c), int(v or 0), _r(chg, 2) if chg != "" else ""])
        prev = c
    _write_csv(COLD / code / "日线.csv", out, ["date", "open", "high", "low", "close", "volume", "chg_pct"])
    return rows


def export_intraday(con, code, freq, months=None):
    """months=None：全部；否则只重写这些月份（'2026-11' …）。"""
    t = TABLES[freq]
    folder = COLD / code / ("5分钟" if freq == "5m" else "1分钟")
    have = [r[0] for r in con.execute(f"SELECT DISTINCT substr(ts, 1, 7) FROM {t} WHERE code = ? ORDER BY 1", (code,))]
    n = 0
    for m in have:
        if months is not None and m not in months:
            continue
        rows = con.execute(f"SELECT ts, open, high, low, close, volume FROM {t} WHERE code = ? AND ts >= ? AND ts < ? ORDER BY ts",
                           (code, f"{m}-01", f"{m}-32")).fetchall()
        if freq == "1m":
            rows = [r for r in rows if _session(r[0]) == "盘中"]
        if not rows:
            continue
        _write_csv(folder / f"{m}.csv", [[ts[:16], _r(o), _r(h), _r(l), _r(c), int(v or 0), _session(ts)] for ts, o, h, l, c, v in rows])
        n += len(rows)
    return n, have


# ================================================================ 摘要
def _ma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _atr(d, n=14):
    trs = []
    for i in range(1, len(d)):
        _, _, h, l, _, _ = d[i]
        pc = d[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n if len(trs) >= n else None


def _f(x, nd=2):
    return "—" if x is None else f"{x:,.{nd}f}"


def _pct(a, b):
    return None if not a or not b else (a / b - 1) * 100


def summary(con, code, daily, c2stats=None, have5=(), have1=()):
    d = [(r[0][:10], r[1], r[2], r[3], r[4], r[5] or 0) for r in daily]
    if len(d) < 30:
        return f"# {code}\n\n日线不足 30 根，暂无摘要。\n"
    closes = [x[4] for x in d]
    last = d[-1]
    c = last[4]
    ma20, ma50, ma200 = _ma(closes, 20), _ma(closes, 50), _ma(closes, 200)
    atr = _atr(d)
    y = d[-252:]
    hi52, lo52 = max(x[2] for x in y), min(x[3] for x in y)
    # 上一周、上一月（按日历）
    ld = dt.date.fromisoformat(last[0])
    wk0 = ld - dt.timedelta(days=ld.weekday() + 7)
    wk = [x for x in d if wk0 <= dt.date.fromisoformat(x[0]) < wk0 + dt.timedelta(days=7)]
    m_first = (ld.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    mo = [x for x in d if m_first <= dt.date.fromisoformat(x[0]) < ld.replace(day=1)]
    trend = ("上升（价格 > MA20 > MA50）" if ma50 and c > ma20 > ma50 else
             "下降（价格 < MA20 < MA50）" if ma50 and c < ma20 < ma50 else "震荡")
    vol20 = sum(x[5] for x in d[-21:-1]) / 20 if len(d) > 21 else None
    L = [f"# {code} 摘要", "",
         f"更新到 {last[0]}（美东）。价格单位：美元。日均波幅 = 14 日 ATR。", "",
         "## 现在", "",
         f"- 收盘 {_f(c)}，最近一天 {_f(_pct(c, d[-2][4]))}%；5 日 {_f(_pct(c, d[-6][4]))}%；20 日 {_f(_pct(c, d[-21][4]))}%；60 日 {_f(_pct(c, d[-61][4]) if len(d) > 61 else None)}%",
         f"- 趋势：{trend}。MA20 {_f(ma20)}，MA50 {_f(ma50)}，MA200 {_f(ma200)}（价格在 MA200 {'上方' if ma200 and c > ma200 else '下方' if ma200 else '—'}）",
         f"- 日均波幅 {_f(atr)}（{_f(atr / c * 100 if atr else None)}% 的价格）；最近一天振幅 {_f(last[2] - last[3])}，是日均波幅的 {_f((last[2] - last[3]) / atr if atr else None)} 倍",
         f"- 最近一天成交量是 20 日均量的 {_f(last[5] / vol20 if vol20 else None)} 倍", "",
         "## 关键价位", "",
         "| 价位 | 价格 | 离现价 |", "| --- | --- | --- |"]
    lv = [("最近一天高点", last[2]), ("最近一天低点", last[3]), ("最近一天收盘", c)]
    if wk:
        lv += [("上周高点", max(x[2] for x in wk)), ("上周低点", min(x[3] for x in wk))]
    if mo:
        lv += [("上月高点", max(x[2] for x in mo)), ("上月低点", min(x[3] for x in mo))]
    lv += [("20 日高点", max(x[2] for x in d[-20:])), ("20 日低点", min(x[3] for x in d[-20:])),
           ("52 周高点", hi52), ("52 周低点", lo52)]
    for name, p in sorted(lv, key=lambda x: -x[1]):
        dist = (p - c) / atr if atr else None
        L.append(f"| {name} | {_f(p)} | {_f(_pct(p, c))}%（{_f(dist)} 倍日均波幅） |")
    L += ["", "## 最近 20 个交易日", "",
          "| 日期 | 收盘 | 涨跌 % | 振幅 / 日均波幅 | 收在当天区间的位置 | 量 / 20 日均量 |",
          "| --- | --- | --- | --- | --- | --- |"]
    for i in range(max(1, len(d) - 20), len(d)):
        x, p = d[i], d[i - 1]
        rng = x[2] - x[3]
        v20 = sum(z[5] for z in d[max(0, i - 20):i]) / max(1, min(20, i))
        L.append(f"| {x[0]} | {_f(x[4])} | {_f(_pct(x[4], p[4]))} | {_f(rng / atr if atr else None)} | "
                 f"{_f((x[4] - x[3]) / rng * 100 if rng else None, 0)}% | {_f(x[5] / v20 if v20 else None)} |")
    L += ["", "## 数据文件", "",
          f"- 日线.csv：{len(d)} 天（{d[0][0]} 起）",
          f"- 5分钟/：{len(have5)} 个月（{have5[0] if have5 else '—'} 起），含盘前盘后",
          f"- 1分钟/：{len(have1)} 个月（{have1[0] if have1 else '—'} 起），常规时段"]
    if c2stats:
        s = c2stats
        L += ["", "## C2 打法在它身上的历史表现（1 分钟图，正股、未扣成本）", "",
              f"- {s.get('n', 0)} 笔，到目标 {s.get('hit', 0)}%，平均每笔 {s.get('avg', 0):+.2f}R；做多 {s.get('n_long', 0)} 笔 {s.get('avg_long', 0):+.2f}R，做空 {s.get('n_short', 0)} 笔 {s.get('avg_short', 0):+.2f}R",
              "- 样本少于 30 笔时只当参考"]
    return "\n".join(L) + "\n"


def overview(rows):
    L = ["# 股票总览", "", f"更新于 {dt.datetime.now():%Y-%m-%d %H:%M}（Mac 时间）。按 20 日涨跌排序。", "",
         "| 代码 | 收盘 | 最近一天 % | 5 日 % | 20 日 % | 趋势 | 日均波幅 % | 离 52 周高点 % |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in sorted(rows, key=lambda r: -(r["r20"] or -999)):
        L.append(f"| [{r['code']}]({r['code']}/摘要.md) | {_f(r['c'])} | {_f(r['r1'])} | {_f(r['r5'])} | {_f(r['r20'])} | {r['trend']} | {_f(r['atrp'])} | {_f(r['from_hi'])} |")
    return "\n".join(L) + "\n"


README = """# 数据文件夹说明

给 Claude 和你自己看：每个文件夹放什么、怎么用。

## 约定
- 时间一律是**美东时间**。分钟K线的时间是这根K线的**结束时间**（09:31 = 9:30–9:31 那根）。
- 价格是前复权后的美元价格，成交量是股数。
- 日均波幅 = 14 日 ATR，用来把不同价位的股票放在一起比较。

## 冷数据（每天收盘复盘后更新）
- `冷数据/股票/总览.md`：所有股票一张表，先看这个。
- `冷数据/股票/<代码>/摘要.md`：现价、趋势、关键价位、最近 20 天、C2 打法历史表现。
- `冷数据/股票/<代码>/日线.csv`：date, open, high, low, close, volume, chg_pct
- `冷数据/股票/<代码>/5分钟/年-月.csv`、`1分钟/年-月.csv`：time_et, open, high, low, close, volume, session（盘前 / 盘中 / 盘后）
- `冷数据/信息面/`：新闻按类别和日期归档（第二步建好后才有）。

## 今日（每天开盘前生成）
- `今日/<日期>/`：盘前分析、讨论定下的打法和纪律、纪律设置、复盘。

## 热数据（盘中每分钟更新）
- `热数据/`：当天实时行情和程序状态（第四步建好后才有）。

## 原始数据
冷数据是从 `程序/data/history.db`（SQLite）导出的；有疑问时以数据库为准。
"""


def export(codes=None, full=False, log=print):
    """导出冷数据。full=False 时分钟线只重写最近两个月。返回说明文字。"""
    if not DB.exists():
        return "✗ 找不到 K 线库"
    con = _con()
    try:
        allc = codes_all(con)
        codes = [c for c in (codes or allc) if c in allc]
        today = dt.date.today()
        recent = None if full else {today.strftime("%Y-%m"), (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")}
        try:
            c2s = json.loads(C2_BY_CODE.read_text(encoding="utf-8"))
        except Exception:
            c2s = {}
        rows, n5, n1 = [], 0, 0
        for code in codes:
            daily = export_daily(con, code)
            a, have5 = export_intraday(con, code, "5m", recent)
            b, have1 = export_intraday(con, code, "1m", recent)
            n5 += a
            n1 += b
            (COLD / code).mkdir(parents=True, exist_ok=True)
            (COLD / code / "摘要.md").write_text(summary(con, code, daily, c2s.get(code), have5, have1), encoding="utf-8")
            if len(daily) >= 61:
                cl = [r[4] for r in daily]
                d4 = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in daily]
                atr = _atr(d4)
                ma20, ma50 = _ma(cl, 20), _ma(cl, 50)
                rows.append(dict(code=code, c=cl[-1], r1=_pct(cl[-1], cl[-2]), r5=_pct(cl[-1], cl[-6]), r20=_pct(cl[-1], cl[-21]),
                                 trend="上升" if cl[-1] > ma20 > ma50 else "下降" if cl[-1] < ma20 < ma50 else "震荡",
                                 atrp=atr / cl[-1] * 100 if atr else None,
                                 from_hi=_pct(cl[-1], max(r[2] for r in daily[-252:]))))
            if log:
                log(f"  · 冷数据 {code}")
        COLD.mkdir(parents=True, exist_ok=True)
        (COLD / "总览.md").write_text(overview(rows), encoding="utf-8")
        (ROOT / "说明.md").write_text(README, encoding="utf-8")
        for sub in ("冷数据/信息面", "今日", "热数据"):
            (ROOT / sub).mkdir(parents=True, exist_ok=True)
    finally:
        con.close()
    return f"冷数据已更新：{len(codes)} 只股票，{'全部重写' if full else '最近两个月的分钟线'}（5 分钟 {n5:,} 根、1 分钟 {n1:,} 根）"


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print(export([a.upper() for a in args] or None, full="--full" in sys.argv))
