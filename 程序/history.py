# -*- coding: utf-8 -*-
"""
历史 K 线库（SQLite，程序/data/history.db）

  bars_1m / bars_5m / bars_1d：code, ts（美东时间，K线结束时间，和 moomoo 一致）, open, high, low, close, volume
  每天收盘后追加最近几天的数据（含盘前盘后）；第一次运行或手动「回填历史」时补长期数据。

1 分钟K的免费来源只有最近几天，所以要从现在开始每天自己存。
研究范围：设置.json → 工作流 → 研究名单（留空 = moomoo 自选里全部美股 + 常用 ETF）。
"""
import datetime as dt
import sqlite3
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "history.db"
TABLES = {"1m": "bars_1m", "5m": "bars_5m", "1d": "bars_1d"}
ALWAYS = ["QQQ", "SPY", "SOXX", "XLK", "SMH"]
# 每天追加：往前取几天（周末 / 假期 / 某天没跑都能补上）
DAILY_DAYS = {"1m": 4, "5m": 7, "1d": 30}
# 回填：moomoo 能给多少给多少，最多这么久
BACKFILL_DAYS = {"1m": 30, "5m": 365, "1d": 3650}
BACKFILL_1M_BIG = 365        # 研究名单里的股票，1 分钟K补到一年（只取常规时段；冷数据和 C1/C2 打法用）


def big_list():
    import json
    try:
        d = json.loads((HERE.parent / "设置.json").read_text(encoding="utf-8"))
        lst = (d.get("盘感训练") or {}).get("大盘股名单")
    except Exception:
        lst = None
    return {str(c).strip().upper() for c in (lst or ["AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO",
                                                     "TSLA", "AMD", "NFLX", "MU", "ARM", "INTC", "KO", "QQQ", "SPY"])}


def connect(path=None):
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=20)
    for t in TABLES.values():
        con.execute(f"""CREATE TABLE IF NOT EXISTS {t} (code TEXT, ts TEXT, open REAL, high REAL, low REAL,
                        close REAL, volume REAL, PRIMARY KEY (code, ts))""")
    return con


def save(con, freq, code, df):
    if df is None or not len(df):
        return 0
    rows = [(code, str(ts)[:19], float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
            for ts, r in df.iterrows()]
    with con:
        con.executemany(f"INSERT OR REPLACE INTO {TABLES[freq]} VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def load(code, freq, start=None, end=None, path=None):
    """读出 K 线：index = 时间（美东，K线结束时间），列 open/high/low/close/volume。"""
    con = connect(path)
    try:
        q, a = f"SELECT ts, open, high, low, close, volume FROM {TABLES[freq]} WHERE code = ?", [code]
        if start:
            q += " AND ts >= ?"
            a.append(str(start)[:19])
        if end:
            q += " AND ts <= ?"
            a.append(str(end)[:19])
        df = pd.read_sql_query(q + " ORDER BY ts", con, params=a)
    finally:
        con.close()
    df.index = pd.to_datetime(df.pop("ts"))
    return df


def coverage(path=None):
    con = connect(path)
    try:
        out = {}
        for f, t in TABLES.items():
            out[f] = con.execute(f"SELECT COUNT(DISTINCT code), COUNT(*), MIN(ts), MAX(ts) FROM {t}").fetchone()
        return out
    finally:
        con.close()


def _first_ts(con, freq, code):
    r = con.execute(f"SELECT MIN(ts) FROM {TABLES[freq]} WHERE code = ?", (code,)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- 从 moomoo 拉
def fetch(mm, code, freq, start, end, pause=0.55, ext=True):
    kt = {"1m": mm.ft.KLType.K_1M, "5m": mm.ft.KLType.K_5M, "1d": mm.ft.KLType.K_DAY}[freq]
    frames, page, err = [], None, None
    kw = dict(ktype=kt, autype=mm.ft.AuType.QFQ, max_count=1000)
    if freq != "1d":
        kw["extended_time"] = bool(ext)
    for _ in range(400):
        for attempt in range(3):
            try:
                ret, data, page2 = mm.ctx.request_history_kline("US." + code, start=str(start), end=str(end),
                                                                page_req_key=page, **kw)
            except Exception as e:
                ret, data, page2 = None, str(e), None
            if ret == mm.ft.RET_OK:
                break
            err = str(data)
            if any(t in err for t in ("频率", "频繁", "frequen", "Frequen", "too many")):
                time.sleep(31)
                continue
            return None, err
        else:
            return None, err
        frames.append(data)
        page = page2
        if page is None:
            break
        time.sleep(pause)                          # 历史K线接口每 30 秒最多 60 次；盘中回填放慢，给盘中监测留额度
    if not frames:
        return None, "无数据"
    df = pd.concat(frames)
    df.index = pd.to_datetime(df["time_key"])
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    return df[~df.index.duplicated(keep="last")].sort_index(), None


def research_list(cfg, mm, log):
    try:
        import workflow
        lst = [str(x).strip().upper() for x in (workflow.load().get("研究名单") or []) if str(x).strip()]
    except Exception:
        lst = []
    if not lst:
        w = mm.watchlist((cfg.get("moomoo") or {}).get("自选分组", "全部")) if mm and mm.ok else None
        lst = [x["code"] for x in (w or [])]
    try:
        import stocks
        params, defaults, *_ = stocks.load_settings(lambda *a, **k: None)
        etfs = [str(v.get("对比ETF") or "").upper() for v in params.values()] + [str(defaults.get("对比ETF") or "").upper()]
    except Exception:
        etfs = []
    out = []
    for c in lst + ALWAYS + [e for e in etfs if e]:
        if c not in out:
            out.append(c)
    return out


def update(cfg, log=print, codes=None, backfill=False, freqs=("1d", "5m", "1m"), mm=None, slow=False):
    """收盘后调用。backfill=True：对还没有长期数据的标的补到 BACKFILL_DAYS。返回摘要文字。"""
    import stocks
    own = mm is None
    if own:
        mm = stocks.Moomoo(cfg.get("moomoo") or {}, log)
    if not mm.ok:
        return "✗ OpenD 未连接，K 线没有入库"
    con = connect()
    added, errs = {f: 0 for f in freqs}, []
    try:
        codes = codes or research_list(cfg, mm, log)
        today = dt.date.today() + dt.timedelta(days=1)
        pause = 1.6 if slow else 0.55
        big = big_list()
        jobs = [(f, code) for f in freqs for code in codes] if backfill else [(f, code) for code in codes for f in freqs]
        for j, (f, code) in enumerate(jobs):          # 回填时先把所有股票的日线补完，再补 5 分钟、1 分钟
            if True:
                days = DAILY_DAYS[f]
                start, end, ext = today - dt.timedelta(days=days), today, True
                if backfill:
                    first = _first_ts(con, f, code)
                    span = BACKFILL_1M_BIG if f == "1m" else BACKFILL_DAYS[f]   # 1 分钟K：研究名单全部补一年
                    want = today - dt.timedelta(days=span)
                    if first is None or first[:10] > str(want + dt.timedelta(days=7)):
                        start = want
                        if f == "1m" and first is not None:   # 已有最近一段：只补更早的部分，常规时段
                            end, ext = dt.date.fromisoformat(first[:10]), False
                df, err = fetch(mm, code, f, start, end, pause=pause, ext=ext)
                if err:
                    errs.append(f"{code} {f}：{err[:60]}")
                    continue
                added[f] += save(con, f, code, df)
            if backfill and log:
                log(f"  · 回填 {j + 1}/{len(jobs)} {code} {f}")
    finally:
        con.close()
        if own:
            mm.close()
    msg = (f"K 线入库：{len(codes)} 只，新增/更新 日线 {added.get('1d', 0):,} 根、5 分钟 {added.get('5m', 0):,} 根、"
           f"1 分钟 {added.get('1m', 0):,} 根")
    if errs:
        msg += f"；{len(errs)} 项失败（如 {errs[0]}）"
    return msg
