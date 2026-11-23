# -*- coding: utf-8 -*-
"""
纪律程序：按「今日/<日期>/纪律设置.json」盯你的成交和价格，违反纪律或到了认错价就弹窗

  盘中（美东 9:30–16:00）每分钟一次：
    ① 读 moomoo 今天的成交（只读）：新开的仓
         · 不在观察名单（只做名单内）
         · 方向和名单不一致（CALL / 买正股 = 多，PUT / 卖空 = 空）
         · 超过最晚开仓时间
         · 超过最多笔数
         · 同一个合约今天买进的张数超过「每笔最多张数」（按今日预案的大盘行情定）
       → 强提醒弹窗（必须点掉），记进数据库 discipline_events
    ② 今日盈亏到单日最大亏损 → 强提醒
    ③ 价格：观察名单的认错价 → 强提醒；关键价位 → 通知；「提醒」里的条件 → 强提醒（每条每天一次）
  每分钟把状态写进 数据/热数据/纪律.md 和 data/.guard_status.json（控制台读）。
  纪律设置是「草稿」时只显示、不弹窗；「已锁定」才弹窗。只读：不下单、不撤单、不平仓。
"""
import datetime as dt
import json
import re
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import today   # noqa: E402

ET = ZoneInfo("America/New_York")
HOT = BASE / "数据" / "热数据"
STATUS = HERE / "data" / ".guard_status.json"
PID_FILE = HERE / ".guard.pid"
LOG = BASE / "报告" / "纪律日志.txt"
SCHEMA = """CREATE TABLE IF NOT EXISTS discipline_events (
    date TEXT, ts TEXT, kind TEXT, code TEXT, detail TEXT, PRIMARY KEY (date, kind, code, detail))"""


def et_now():
    return dt.datetime.now(ET)


def log(t):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {t}\n")
    print(t, flush=True)


def cfg_all():
    try:
        return json.loads((BASE / "设置.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def popup(title, msg, critical=True):
    if sys.platform != "darwin":
        print(f"[弹窗] {title}：{msg}")
        return
    if critical:
        script = ['tell application "System Events"', "activate",
                  'display alert (item 1 of argv) message (item 2 of argv) as critical buttons {"知道了"} default button "知道了"',
                  "end tell"]
        args = ["osascript", "-e", "on run argv"] + sum([["-e", x] for x in script], []) + ["-e", "end run", title, msg]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        snd = HERE / "sounds" / "alert.mp3"                 # 交易者选的提示音；没有就用系统声音
        subprocess.Popen(["afplay", str(snd) if snd.exists() else "/System/Library/Sounds/Sosumi.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t = msg.replace('"', "'")[:200]
    subprocess.Popen(["osascript", "-e", f'display notification "{t}" with title "{title}"'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def record(day, kind, code, detail):
    """记一次事件；同一天同一条只记一次。返回 True = 第一次（要提醒）。"""
    try:
        import store
        con = store.connect()
        try:
            con.execute(SCHEMA)
            with con:
                cur = con.execute("INSERT OR IGNORE INTO discipline_events VALUES (?,?,?,?,?)",
                                  (str(day), dt.datetime.now().isoformat(timespec="seconds"), kind, code, detail))
                return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return True


# ================================================================ 成交 / 行情
class Broker:
    def __init__(self, cfg):
        self.cfg, self.ft, self.ctx, self.accs = cfg, None, None, []

    def deals(self):
        """今天的成交 [dict(code, side, qty, price, order_id, time)]；读不到返回 None。"""
        import journal
        if self.ctx is None:
            con = journal.connect()
            try:
                self.ft, self.ctx, accs = journal.open_trade_ctx(self.cfg, lambda *a: None, con)
            finally:
                con.close()
            if self.ctx is None:
                return None
            self.accs = accs
        out = []
        try:
            for acc in self.accs:
                ret, d = self.ctx.deal_list_query(trd_env=self.ft.TrdEnv.REAL, acc_id=int(acc), refresh_cache=True)
                if ret != self.ft.RET_OK:
                    continue
                for _, r in d.iterrows():
                    if str(r.get("code", "")).startswith("US.") and "CANCEL" not in str(r.get("status", "")).upper():
                        out.append(dict(code=str(r["code"]), side=str(r["trd_side"]).upper(), qty=float(r["qty"]),
                                        price=float(r["price"]), order_id=str(r.get("order_id") or ""),
                                        time=str(r.get("create_time") or "")))
        except Exception:
            self.close()
            return None
        return out

    def close(self):
        try:
            if self.ctx is not None:
                self.ctx.close()
        except Exception:
            pass
        self.ctx = None


def quotes(cfg, codes):
    if not codes:
        return {}
    import stocks
    mm = stocks.Moomoo(cfg.get("moomoo") or {}, lambda *a, **k: None)
    out = {}
    if not mm.ok:
        return out
    try:
        ret, df = mm.ctx.get_market_snapshot(["US." + c for c in codes])
        if ret == mm.ft.RET_OK:
            for _, r in df.iterrows():
                out[str(r["code"]).replace("US.", "")] = dict(last=float(r.get("last_price") or 0),
                                                               high=float(r.get("high_price") or 0),
                                                               low=float(r.get("low_price") or 0))
    finally:
        mm.close()
    return out


# ================================================================ 检查
def direction(code, side):
    import journal
    p = journal.parse(code)
    if p["kind"] == "期权":
        return p["underlying"], ("多" if p["opt_type"] == "CALL" else "空"), True
    if side in ("BUY",):
        return p["underlying"], "多", True
    if side in ("SELL_SHORT",):
        return p["underlying"], "空", True
    return p["underlying"], None, False          # 卖出平仓 / 买回平仓


def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def check(plan, deals, pnl, q, day, now):
    """纯判断（方便测试）：返回 (要提醒的 [(kind, code, detail, critical)], 状态 dict)。"""
    lim = plan["限制"]
    names = {w["代码"]: w for w in plan["观察名单"]}
    alerts, opens = [], {}
    for d in deals or []:
        und, dirn, is_open = direction(d["code"], d["side"])
        if not is_open:
            continue
        if d["side"] == "SELL" or (d["side"] not in ("BUY", "SELL_SHORT")):
            continue
        key = d["order_id"] or f"{d['code']}|{d['time']}"
        o = opens.setdefault(key, dict(und=und, dirn=dirn, time=d["time"], code=d["code"], qty=0.0))
        o["qty"] += float(d.get("qty") or 0)
    mq = lim.get("每笔最多张数")
    if mq:                                        # 同一个合约同时拿着的最多张数（加仓算在一起，平掉的减掉）
        net, peak = {}, {}
        for d in sorted(deals or [], key=lambda x: x["time"]):
            sgn = 1 if d["side"] in ("BUY", "SELL_SHORT") else -1 if d["side"] in ("SELL", "BUY_BACK") else 0
            net[d["code"]] = max(0.0, net.get(d["code"], 0.0) + sgn * float(d.get("qty") or 0))
            peak[d["code"]] = max(peak.get(d["code"], 0.0), net[d["code"]])
        for code, q in peak.items():
            if q > float(mq):
                alerts.append(("超过张数", code, f"{code} 同时拿了 {q:g} 张，今天每笔最多 {mq} 张（按大盘行情定的）", True))
    for k, o in sorted(opens.items(), key=lambda x: x[1]["time"]):
        w = names.get(o["und"])
        if lim.get("只做名单内") and w is None:
            alerts.append(("名单外", o["und"], f"{o['und']} 不在今天的观察名单里（名单：{'、'.join(names) or '无'}）", True))
        elif w is not None and lim.get("只做名单方向") and w.get("方向") in ("多", "空") and o["dirn"] != w["方向"]:
            alerts.append(("方向相反", o["und"], f"{o['und']} 今天只做{w['方向']}，你开的是{o['dirn']}", True))
        t = o["time"][11:16] if len(o["time"]) >= 16 else ""
        if t and lim.get("最晚开仓") and _hm(t) > _hm(lim["最晚开仓"]):
            alerts.append(("过了开仓时间", o["und"], f"{t} 开了 {o['und']}，今天 {lim['最晚开仓']} 以后不开新仓", True))
    n = len(opens)
    mx = lim.get("最多笔数")
    if mx and n > int(mx):
        alerts.append(("超过笔数", "", f"今天已经开了 {n} 笔，上限 {mx} 笔", True))
    ml = lim.get("单日最大亏损_美元")
    if ml and pnl is not None and pnl <= -float(ml):
        alerts.append(("到亏损上限", "", f"今日盈亏 ${pnl:,.0f}，已到单日上限 ${float(ml):,.0f}：今天停手", True))
    for w in plan["观察名单"]:
        x = q.get(w["代码"])
        if not x or not x["last"]:
            continue
        stop = w.get("认错价")
        if stop:
            bad = (w.get("方向") == "多" and x["low"] <= stop) or (w.get("方向") == "空" and x["high"] >= stop)
            if bad:
                alerts.append(("到认错价", w["代码"], f"{w['代码']} 碰到认错价 {stop}（现价 {x['last']:.2f}）：按计划认错", True))
        for lv in w.get("关键价位") or []:
            p = lv.get("价格")
            if p and x["low"] <= p <= x["high"]:
                alerts.append(("到关键价位", w["代码"], f"{w['代码']} 到了 {p}（{lv.get('说明') or '关键价位'}），现价 {x['last']:.2f}", False))
    for r in plan.get("提醒") or []:
        x = q.get(str(r.get("代码") or "").upper())
        p = r.get("价格")
        if not x or not p:
            continue
        hit = (r.get("条件") == "跌破" and x["low"] < p) or (r.get("条件") == "突破" and x["high"] > p)
        if hit:
            alerts.append(("提醒", r["代码"], f"{r['代码']} {r['条件']} {p}：{r.get('说明') or ''}", True))
    st = dict(day=str(day), at=f"{now:%H:%M}", locked=plan.get("状态") == "已锁定", opens=n, max=mx, pnl=pnl, max_loss=ml,
              bias=plan.get("偏向"), style=plan.get("打法"), names=[f"{w['代码']}{w.get('方向', '')}" for w in plan["观察名单"]])
    return alerts, st


def write_status(st, events):
    try:
        STATUS.write_text(json.dumps(dict(st, events=events[-20:]), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    try:
        HOT.mkdir(parents=True, exist_ok=True)
        L = [f"# 纪律状态 · {st.get('day')}（美东 {st.get('at')} 更新）", ""]
        if st.get("note"):
            L.append(st["note"])
        else:
            L += [f"- 纪律设置：{'已锁定' if st.get('locked') else '草稿（只显示，不弹窗）'}；偏向 {st.get('bias')}，{st.get('style')}打法",
                  f"- 观察名单：{'、'.join(st.get('names') or []) or '无'}",
                  f"- 已开仓 {st.get('opens')} 笔 / 上限 {st.get('max') or '不限'}",
                  "- 今日盈亏 " + ("读不到" if st.get("pnl") is None else f"${st['pnl']:,.0f}") + f" / 单日上限 {st.get('max_loss') or '不限'}"]
        L += ["", "## 今天触发过的", ""] + ([f"- {e}" for e in events] or ["- 没有"])
        try:   # 基本纪律：每天都适用，原样附在后面（标题降一级，去掉依据和修改记录）
            basic = (BASE / "数据" / "基本纪律.md").read_text(encoding="utf-8").split("依据：")[0].strip()
            if basic:
                L += [""] + [("#" + x if x.startswith("# ") else x) for x in basic.splitlines()]
        except Exception:
            pass
        (HOT / "纪律.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    except Exception:
        pass


def main():
    PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: (PID_FILE.unlink(missing_ok=True), sys.exit(0)))
    log("纪律程序启动")
    broker, events, cur_day = None, [], None
    while True:
        try:
            cfg = cfg_all()
            n = et_now()
            m = n.hour * 60 + n.minute
            day = today.session_day(n) if m < 960 else n.date()
            if day != cur_day:
                events, cur_day = [], day
            plan = today.load_plan(day)
            if plan is None:
                write_status(dict(day=str(day), at=f"{n:%H:%M}", note="今天还没有纪律设置（和 Claude 讨论定下后才有）"), events)
                time.sleep(60)
                continue
            live = n.weekday() < 5 and 570 <= m < 960
            deals, pnl, q = [], None, {}
            if live:
                broker = broker or Broker(cfg)
                deals = broker.deals()
                try:
                    import risk
                    r = risk.today(cfg)
                    pnl = r["total"] if r.get("ok") else None
                except Exception:
                    pnl = None
                codes = sorted({w["代码"] for w in plan["观察名单"]} | {str(r.get("代码") or "").upper() for r in plan.get("提醒") or []} - {""})
                q = quotes(cfg, codes)
            alerts, st = check(plan, deals, pnl, q, day, n)
            for kind, code, detail, critical in alerts:
                key = re.sub(r"[，（]?现价 [\d.,]+）?", "", detail)        # 同一个价位每天只提醒一次（不含现价，否则每分钟都算新的）
                if record(day, kind, code, key if kind in ("到关键价位", "提醒", "到认错价") else kind + code + detail[:20]):
                    events.append(f"{n:%H:%M} {kind}：{detail}")
                    log(f"{kind}：{detail}")
                    if st["locked"]:
                        popup(f"纪律 · {kind}", detail + ("\n\n按今天定下的打法来。要改纪律，先跟 Claude 说明理由。" if critical else ""), critical)
            if deals is None:
                st["note_deals"] = "读不到成交（OpenD 没连上？）"
            write_status(st, events)
        except Exception:
            log("出错：" + traceback.format_exc().splitlines()[-1])
        time.sleep(60)


if __name__ == "__main__":
    main()
