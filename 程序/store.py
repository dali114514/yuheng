# -*- coding: utf-8 -*-
"""
本地数据库（SQLite，程序/data/trading.db）

  signals        每天每个机会一行：第一次出现时的价位、级别、合约，之后更新最新状态
  signal_events  每次提醒 / 状态变化一行（包括机会 A/B/C）
  alert_state    当天已经提醒过什么（盘中监测重启后不重复弹窗）

这里出错不能影响盯盘：调用方都包在 try 里，本模块的函数失败时只返回空结果。
"""
import datetime as dt
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_DIR = HERE / "data"
DB_PATH = DB_DIR / "trading.db"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY, date TEXT, source TEXT, code TEXT, name TEXT, kind TEXT, play TEXT,
        direction TEXT, first_ts TEXT, last_ts TEXT, grade_first TEXT, grade_best TEXT, grade_last TEXT,
        status_first TEXT, status_last TEXT, spot_first REAL, entry REAL, stop REAL, target1 REAL, target2 REAL,
        rr REAL, align INTEGER, contract TEXT, qty INTEGER, macro TEXT, alerted_ts TEXT, alert_count INTEGER DEFAULT 0,
        detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS signal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, date TEXT, signal_id TEXT, event TEXT, payload TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_events_sig ON signal_events(signal_id)",
    "CREATE INDEX IF NOT EXISTS ix_signals_date ON signals(date)",
    """CREATE TABLE IF NOT EXISTS alert_state (
        date TEXT, kind TEXT, key TEXT, value TEXT, PRIMARY KEY (date, kind, key))""",
]
ORDER = {"A": 0, "B": 1, "C": 2}


def _default(o):
    try:
        return float(o)
    except Exception:
        return str(o)


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=_default)


def _tup(x):
    return tuple(_tup(i) for i in x) if isinstance(x, list) else x


def _f(x):
    try:
        x = float(x)
        return None if x != x else x
    except Exception:
        return None


def connect(path=None):
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    for s in SCHEMA:
        con.execute(s)
    return con


# ---------------------------------------------------------------- 提醒去重状态
def load_state(day, path=None):
    """返回 (alerted, sector_alerted, abc_seen)；读不到就返回三个空字典。"""
    out = {"rule": {}, "sector": {}, "abc": {}}
    try:
        con = connect(path)
        try:
            for kind, k, v in con.execute("SELECT kind, key, value FROM alert_state WHERE date = ?", (str(day),)):
                if kind in out:
                    out[kind][_tup(json.loads(k))] = _tup(json.loads(v))
        finally:
            con.close()
    except Exception:
        return {}, {}, {}
    return out["rule"], out["sector"], out["abc"]


def save_state(day, alerted, sector_alerted, abc_seen, path=None):
    rows = []
    for kind, d in (("rule", alerted), ("sector", sector_alerted), ("abc", abc_seen)):
        for k, v in (d or {}).items():
            rows.append((str(day), kind, _js(k), _js(v)))
    con = connect(path)
    try:
        with con:
            con.execute("DELETE FROM alert_state WHERE date = ?", (str(day),))
            con.executemany("INSERT OR REPLACE INTO alert_state VALUES (?, ?, ?, ?)", rows)
    finally:
        con.close()


# ---------------------------------------------------------------- 信号记录
def log_opps(day, now, opps, alerts=(), macro="", path=None):
    """opps：本轮全部机会；alerts：本轮弹窗提醒的那几个（带 _why）。now：美东时间。"""
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    alert_ids = {id(c): c for c in alerts}
    con = connect(path)
    try:
        with con:
            for c in opps:
                sid = f"{day}|{c['code']}|{c['play']}|{c['direction']}"
                ct = (c.get("contracts") or [None])[0]
                tg = list(c.get("targets") or []) + [None, None]
                g = c.get("grade")
                row = con.execute("SELECT grade_best FROM signals WHERE signal_id = ?", (sid,)).fetchone()
                if row is None:
                    detail = dict(title=c.get("title"), trigger=c.get("trigger"), layer_notes=c.get("layer_notes"),
                                  news_notes=c.get("news_notes"), opt_note=c.get("opt_note"), atr=_f(c.get("atr")))
                    con.execute(
                        "INSERT INTO signals (signal_id, date, source, code, name, kind, play, direction, first_ts, last_ts,"
                        " grade_first, grade_best, grade_last, status_first, status_last, spot_first, entry, stop, target1,"
                        " target2, rr, align, contract, qty, macro, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, str(day), "规则", c["code"], c.get("name"), c.get("kind"), c["play"], c["direction"], ts, ts,
                         g, g, g, c.get("status"), c.get("status"), _f(c.get("spot")), _f(c.get("entry")), _f(c.get("stop")),
                         _f(tg[0]), _f(tg[1]), _f(c.get("rr")), c.get("align"), _js(ct) if ct else None, c.get("qty"),
                         macro, _js(detail)))
                else:
                    best = min([x for x in (row[0], g) if x in ORDER], key=ORDER.get, default=g)
                    con.execute("UPDATE signals SET last_ts = ?, grade_last = ?, grade_best = ?, status_last = ? WHERE signal_id = ?",
                                (ts, g, best, c.get("status"), sid))
                if id(c) in alert_ids:
                    con.execute("UPDATE signals SET alerted_ts = COALESCE(alerted_ts, ?), alert_count = alert_count + 1,"
                                " contract = COALESCE(?, contract), qty = COALESCE(?, qty) WHERE signal_id = ?",
                                (ts, _js(ct) if ct else None, c.get("qty"), sid))
                    con.execute("INSERT INTO signal_events (ts, date, signal_id, event, payload) VALUES (?,?,?,?,?)",
                                (ts, str(day), sid, "提醒：" + str(c.get("_why", "")),
                                 _js(dict(grade=g, status=c.get("status"), spot=_f(c.get("spot")), stop=_f(c.get("stop")),
                                          targets=c.get("targets"), rr=_f(c.get("rr")), contract=ct, qty=c.get("qty")))))
    finally:
        con.close()


def log_abc(day, now, r, path=None):
    """机会 A/B/C 的一次状态变化。"""
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    sid = f"{day}|ABC|{r.get('key')}"
    z = r.get("zone") or {}
    con = connect(path)
    try:
        with con:
            if con.execute("SELECT 1 FROM signals WHERE signal_id = ?", (sid,)).fetchone() is None:
                con.execute(
                    "INSERT INTO signals (signal_id, date, source, code, name, kind, play, direction, first_ts, last_ts,"
                    " status_first, status_last, entry, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, str(day), "ABC", r.get("code"), r.get("name"), "日内", "机会" + str(r.get("path")),
                     r.get("direction"), ts, ts, r.get("state"), r.get("state"), _f(z.get("price")),
                     _js(dict(stage=r.get("stage"), zone=z, evidence=r.get("evidence"), missing=r.get("missing")))))
            else:
                con.execute("UPDATE signals SET last_ts = ?, status_last = ? WHERE signal_id = ?", (ts, r.get("state"), sid))
            con.execute("UPDATE signals SET alerted_ts = COALESCE(alerted_ts, ?), alert_count = alert_count + 1 WHERE signal_id = ?",
                        (ts, sid))
            con.execute("INSERT INTO signal_events (ts, date, signal_id, event, payload) VALUES (?,?,?,?,?)",
                        (ts, str(day), sid, "ABC：" + str(r.get("state")),
                         _js(dict(stage=r.get("stage"), sig=r.get("sig"), evidence=r.get("evidence"),
                                  contracts=r.get("contracts")))))
    finally:
        con.close()
