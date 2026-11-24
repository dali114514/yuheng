# -*- coding: utf-8 -*-
"""
长期历史 K 线（研究专用，单独一个库 程序/data/history_long.db，不影响 history.db 和常驻程序）

  1 分钟K：只取常规时段，从 2019-01-02 补到 history.db 里 1 分钟K开始的那天
  日线：同一次重新抓（前复权口径和 1 分钟一致），2018-06 起
  价格是 moomoo 前复权价：老数据的美元价位会被拆股缩小（2019 年 NVDA 约 $3），研究时用倍日线 ATR，不要用美元阈值

  用法：arch -arm64 程序/.venv/bin/python 程序/longhist.py [代码 …]      （不写代码 = 研究名单里 1 分钟K满一年的全部）
        … longhist.py --check                                              （看覆盖和缺口）
  可以随时中断，重跑会从断点继续（按年份分段，已补完的年份跳过）。
  美东 9:20–11:10、16:10–16:50 自动暂停，给盘中监测和收盘复盘让出接口额度。只读行情，不下单。
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history   # noqa: E402

LONG = HERE / "data" / "history_long.db"
START = dt.date(2019, 1, 2)
ET = ZoneInfo("America/New_York")
QUIET = [(560, 670), (970, 1010)]           # 美东分钟：9:20–11:10、16:10–16:50 暂停
FIRST = ["QQQ", "SPY", "NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "META", "GOOGL", "AMD", "AVGO"]


def log(t):
    print(f"[{dt.datetime.now(ET):%m-%d %H:%M:%S} 美东] {t}", flush=True)


def wait_quiet():
    while True:
        n = dt.datetime.now(ET)
        m = n.hour * 60 + n.minute
        if n.weekday() >= 5 or not any(a <= m < b for a, b in QUIET):
            return
        log("盘中 / 收盘复盘时段，暂停 5 分钟")
        time.sleep(300)


def _done(con, code, y):
    """这一年是否已经补过（有 ≥ 200 个交易日）。"""
    r = con.execute("SELECT COUNT(DISTINCT substr(ts,1,10)) FROM bars_1m WHERE code = ? AND ts >= ? AND ts < ?",
                    (code, f"{y}-01-01", f"{y + 1}-01-01")).fetchone()
    return (r[0] or 0) >= 200


def codes_default():
    con = history.connect()
    try:
        have = [r[0] for r in con.execute("SELECT code FROM bars_1m GROUP BY code HAVING COUNT(*) > 50000")]
    finally:
        con.close()
    return [c for c in FIRST if c in have] + sorted(c for c in have if c not in FIRST)


def first_short(code):
    con = history.connect()
    try:
        r = con.execute("SELECT MIN(ts) FROM bars_1m WHERE code = ? AND ts LIKE '% 09:31:00'", (code,)).fetchone()
    finally:
        con.close()
    return dt.date.fromisoformat(r[0][:10]) if r and r[0] else dt.date.today()


def run(codes):
    import stocks
    cfg = json.loads((HERE.parent / "设置.json").read_text(encoding="utf-8"))
    mm = None
    con = history.connect(LONG)
    try:
        for ci, code in enumerate(codes):
            stop_at = first_short(code)
            for y in range(START.year, stop_at.year + 1):
                a = max(START, dt.date(y, 1, 1))
                b = min(dt.date(y, 12, 31), stop_at - dt.timedelta(days=1))
                if a > b or (y < stop_at.year and _done(con, code, y)):
                    continue
                wait_quiet()
                if mm is None or not mm.ok:
                    mm = stocks.Moomoo(cfg.get("moomoo") or {}, lambda *x, **k: None)
                    if not mm.ok:
                        log("OpenD 没连上，1 分钟后重试")
                        mm = None
                        time.sleep(60)
                        continue
                t0 = time.time()
                df, err = history.fetch(mm, code, "1m", a, b + dt.timedelta(days=1), pause=0.7, ext=False)
                if err:
                    log(f"{code} {y} 1 分钟：{err[:80]}")
                    continue
                df = df[df.index.date <= b]
                n = history.save(con, "1m", code, df)
                log(f"{ci + 1}/{len(codes)} {code} {y}：{n:,} 根，{df.index.normalize().nunique()} 天，{time.time() - t0:.0f} 秒")
            wait_quiet()
            if mm is None or not mm.ok:
                mm = stocks.Moomoo(cfg.get("moomoo") or {}, lambda *x, **k: None)
            df, err = history.fetch(mm, code, "1d", dt.date(2018, 6, 1), dt.date.today() + dt.timedelta(days=1))
            if not err:
                history.save(con, "1d", code, df)
    finally:
        con.close()
        if mm is not None:
            mm.close()
    log("完成")


def check():
    import sqlite3
    if not LONG.exists():
        print("还没有长期库")
        return
    c = sqlite3.connect(Path(LONG).resolve().as_uri() + "?mode=ro", uri=True)
    q = c.execute("SELECT DISTINCT substr(ts,1,10) d FROM bars_1m WHERE code='QQQ'").fetchall()
    qdays = {r[0] for r in q}
    print(f"QQQ 交易日 {len(qdays)}")
    for code, n, a, b, days in c.execute("SELECT code, COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT substr(ts,1,10)) "
                                         "FROM bars_1m GROUP BY code ORDER BY code"):
        short = c.execute("SELECT COUNT(*) FROM (SELECT substr(ts,1,10) d, COUNT(*) k FROM bars_1m WHERE code=? "
                          "GROUP BY d HAVING k < 380)", (code,)).fetchone()[0]
        print(f"{code:6} {n:>9,} 根  {a[:10]} → {b[:10]}  {days} 天（不满 380 根的 {short} 天）")


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if "--check" in sys.argv:
        check()
    else:
        run([x.upper() for x in args] or codes_default())
