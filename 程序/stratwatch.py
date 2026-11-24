# -*- coding: utf-8 -*-
"""
玉衡5.0 · 策略提醒（常驻，控制台守护）：美东 9:31–16:00 每分钟，按今天纪律设置里每只股票选定的
「入场 + 止损 + 止盈」和「仓位方案」，提醒入场、出场、加仓、走一半、砍、熔断。规则在 stratlive.py（和回测同一套）。

  - 纪律设置「已锁定」时：入场 / 出场 / 加仓 / 走一半 / 砍 / 熔断 → 弹窗 + 提示音；走出来、挪止损 → 只发通知。
  - 「草稿」时只写状态文件，不弹窗（和纪律程序一致）。
  - 设置.json → 策略提醒 → 模式：提醒 / 影子（只记录）/ 关。
  - 每条提醒写进 数据/今日/<日期>/策略提醒.md（复盘用），状态写 data/.strat_status.json 和 数据/热数据/策略提醒.md。
只读行情和持仓，不下单。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import stratlive as S  # noqa: E402

DATA = HERE / "data"
STATUS = DATA / ".strat_status.json"
SENT = DATA / ".stratwatch_sent.json"
PID_FILE = HERE / ".stratwatch.pid"
HOT = BASE / "数据" / "热数据"
POPUP_KINDS = {"入场", "出场", "加仓", "走一半", "砍", "熔断", "不做"}
FRESH_MIN = 3                      # 只弹最近 3 分钟内发生的（中途重启时，老的只记下、不补弹）


def cfg_all():
    try:
        return json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def et_now():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York"))


def log(t):
    print(f"[{et_now():%H:%M:%S} 美东] {t}", flush=True)


def load_sent(day):
    try:
        d = json.loads(SENT.read_text(encoding="utf-8"))
        return set(d.get(str(day)) or [])
    except Exception:
        return set()


def save_sent(day, keys):
    try:
        SENT.write_text(json.dumps({str(day): sorted(keys)}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def notify(title, text, critical):
    import guard
    guard.popup(title, text, critical)


def journal_line(day, line):
    try:
        import today
        p = today.folder(day) / "策略提醒.md"
        if not p.exists():
            p.write_text(f"# 策略提醒记录 · {day}\n\n程序按选定策略发出的提醒（stratwatch.py）。复盘时对照：提醒了什么、你做了什么。\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


def write_status(day, res, note=""):
    try:
        STATUS.write_text(json.dumps(dict(date=str(day), at=f"{et_now():%H:%M}", note=note, stocks=res), ensure_ascii=False,
                                     default=lambda x: float(x) if hasattr(x, "__float__") else str(x)), encoding="utf-8")
        L = [f"# 策略提醒 · {day} {et_now():%H:%M} 美东", ""]
        if note:
            L += [note, ""]
        for code, r in res.items():
            L.append(f"## {code}" + (f"（{r['gap']['name']}，跳空 {r['gap']['val']:+.2f} 倍 ATR）" if r.get("gap") else ""))
            for dd, c in (r.get("choices") or {}).items():
                L.append(f"- 选定（{'做多' if dd == 'L' else '做空'}，{c['src']}，{c['level']}）：{c['name']} + {c['stop_name']} + {c['tgt_name']}")
            if r.get("note"):
                L.append(f"- {r['note']}")
            if r.get("warn"):
                L.append(f"- ⚠ {r['warn']}")
            for tr in r.get("trades") or []:
                L.append(f"- {tr['state']}：{S.describe(tr)}")
            L.append("")
        HOT.mkdir(parents=True, exist_ok=True)
        (HOT / "策略提醒.md").write_text("\n".join(L), encoding="utf-8")
    except Exception:
        log("写状态出错：" + traceback.format_exc().splitlines()[-1])


def scan_once(feed, day, now, sent, popups_on):
    import today
    plan = today.load_plan(day) or {}
    items = [w for w in plan.get("观察名单") or [] if w.get("代码")]
    locked = plan.get("状态") == "已锁定"
    pos_cfg = plan.get("仓位") or S.DEFAULT_POS
    cap = int((plan.get("限制") or {}).get("每笔最多张数") or 4)
    pb = S.playbook_branches(day)
    res = {}
    qb = feed.bars("QQQ", now)
    snap = None
    try:
        import roll
        snap = roll.snapshot(max_age=20)
    except Exception:
        snap = None
    brk = S.breaker(pos_cfg, snap)
    new_events = []
    if brk:
        new_events.append(("—", dict(t=f"{now:%H:%M}", kind="熔断", text=brk), "熔断|" + brk[:12]))
    for w in items:
        code = w["代码"]
        b = feed.bars(code, now) if code != "QQQ" else qb
        if b is None or not len(b):
            res[code] = dict(note="读不到行情")
            continue
        row = S.prev_row(code, day, getattr(feed, "mm", None))
        r = S.evaluate(code, b, qb, row, w, day, pos_cfg, cap, None, pb)
        # 刚出入场信号：读一次实时期权，按真实合约重算价位和张数
        if any(t["state"] == "入场" for t in r["trades"]):
            try:
                import playbook
                opt = playbook.live_options([code], log=lambda *a: None).get(code)
                if opt:
                    r = S.evaluate(code, b, qb, row, w, day, pos_cfg, cap, opt, pb)
            except Exception:
                pass
        res[code] = r
        for tr in r["trades"]:
            for ev in tr["events"]:
                key = f"{code}|{tr['dir']}|{ev['kind']}|{ev['t']}"
                new_events.append((code, ev, key))
    hm = now.hour * 60 + now.minute
    for code, ev, key in new_events:
        if key in sent:
            continue
        sent.add(key)
        h, m = map(int, ev["t"].split(":")) if ev["t"] != "—" else (now.hour, now.minute)
        fresh = hm - (h * 60 + m) <= FRESH_MIN
        text = ev["text"]
        if brk and ev["kind"] == "入场":
            text = "⚠ 已熔断，按纪律不做。\n" + text
        journal_line(day, f"- {ev['t']} [{ev['kind']}] {text.replace(chr(10), '；')}" + ("" if fresh else "（补记，没弹窗）"))
        log(f"{code} {ev['kind']}：{text.splitlines()[0]}")
        if fresh and popups_on and locked:
            notify(f"策略提醒 · {ev['kind']}" + (f" · {code}" if code != "—" else ""), text, ev["kind"] in POPUP_KINDS)
    save_sent(day, sent)
    note = "" if locked else "纪律设置还是草稿：只显示，不弹窗（锁定后才弹）"
    if brk:
        note = (note + "；" if note else "") + brk
    write_status(day, res, note)


def main():
    PID_FILE.write_text(str(os.getpid()))

    def bye(*_):
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    log("策略提醒启动")
    feed, feed_codes = None, None
    import c2watch
    import today
    while True:
        cfg = cfg_all()
        st = S.settings(cfg)
        n = et_now()
        m = n.hour * 60 + n.minute
        if st.get("模式") == "关":
            write_status(n.date(), {}, "已关闭（设置.json → 策略提醒 → 模式）")
            time.sleep(30)
            continue
        if n.weekday() >= 5 or not (571 <= m <= 961):          # 9:31–16:01
            if feed is not None:
                feed.close()
                feed = None
            write_status(today.session_day(n), {}, "周末休市" if n.weekday() >= 5 else ("等开盘（9:31 开始）" if m < 571 else "今天已收盘"))
            time.sleep(30)
            continue
        day = n.date()
        try:
            plan = today.load_plan(day) or {}
            codes = sorted({w["代码"] for w in plan.get("观察名单") or [] if w.get("代码")} | {"QQQ"})
            if feed is None or not feed.ok or codes != feed_codes:
                if feed is not None:
                    feed.close()
                feed = c2watch.Feed(cfg, codes)
                feed_codes = codes
                if not feed.ok:
                    write_status(day, {}, "OpenD 没连上")
                    feed = None
                    time.sleep(20)
                    continue
            sent = load_sent(day)
            scan_once(feed, day, n, sent, st.get("模式") == "提醒")
        except Exception:
            log("出错：" + traceback.format_exc().splitlines()[-1])
        time.sleep(max(1.0, 62 - et_now().second))              # 下一分钟的第 2 秒（等这一根确实收盘）


if __name__ == "__main__":
    if "--test" in sys.argv:                                    # 回放：stratwatch.py --test 2026-10-30 NVDA [时间]
        a = [x for x in sys.argv[1:] if not x.startswith("--")]
        day = dt.date.fromisoformat(a[0])
        import intraday as I
        code = a[1].upper()
        df, q = I.load_1m(code), I.load_1m("QQQ")
        b, qb = df[df.index.date == day], q[q.index.date == day]
        if len(a) > 2:
            b, qb = b[b.index <= f"{day} {a[2]}"], qb[qb.index <= f"{day} {a[2]}"]
        import today
        plan = today.load_plan(day) or {}
        item = next((w for w in plan.get("观察名单") or [] if w.get("代码") == code), {"代码": code, "方向": "双向"})
        r = S.evaluate(code, b, qb, S.prev_row(code, day), item, day, plan.get("仓位"), 4)
        print(json.dumps(r["choices"], ensure_ascii=False), r["note"])
        for tr in r["trades"]:
            print(tr["state"], S.describe(tr))
            for ev in tr["events"]:
                print("  ", ev["t"], ev["kind"], ev["text"].replace("\n", " | "))
        sys.exit(0)
    main()
