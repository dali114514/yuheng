# -*- coding: utf-8 -*-
"""
夜盘复盘（机器人命令「夜盘」，每天夜盘结束后自动发到 #快讯）

数据：moomoo 美股夜盘 1 分钟K（美东 20:00 – 次日 04:00，周日到周四晚）
  · 1 分钟K 用来定精确的高低点、VWAP、午夜开盘价和「哪一分钟出了异动」
  · 夜盘成交稀薄，很多分钟只有零星成交，所以判断走势（单边 / 震荡）时先合成 5 分钟K，避免被个别成交带偏

每只股票算：
  夜盘涨跌（对比前一个常规时段收盘）、区间（占日线 ATR 的比例）、成交量占日均的比例、
  VWAP 和收盘在 VWAP 上方还是下方、收在区间什么位置、走势、
  1 分钟异动（时间 + 幅度 + 量，并去快讯里找同时间的消息）、是否扫过前一天的高点 / 低点、
  开盘前要盯的位置：夜盘高 / 低、VWAP、午夜开盘价
"""
import datetime as dt
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")


# ================================================================ 时段
def session_window(now=None):
    """最近一个夜盘（正在进行的或刚结束的）：返回 (开始, 结束, 是否还在进行)，均为美东时间。
    夜盘：周日–周四 20:00 开始，次日 04:00 结束。"""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    d = now.date()
    for back in range(0, 8):
        day = d - dt.timedelta(days=back)
        if day.weekday() not in (6, 0, 1, 2, 3):           # 周日到周四晚开盘
            continue
        start = dt.datetime.combine(day, dt.time(20, 0), ET)
        if start > now:
            continue
        end = start + dt.timedelta(hours=8)
        return start, end, now < end
    return None, None, False


def _local(t):
    return t.astimezone(dt.datetime.now().astimezone().tzinfo)


# ================================================================ 取数据（带频率控制）
class Fetcher:
    """moomoo 历史K线接口限制约 60 次 / 30 秒，这里按 50 次 / 30 秒节流。"""

    def __init__(self, mm, log):
        self.mm, self.ft, self.log, self.calls = mm, mm.ft, log, []
        self.partial = False

    def _wait(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 30]
        if len(self.calls) >= 50:
            time.sleep(30 - (now - self.calls[0]) + 0.5)
        self.calls.append(time.time())

    def kline(self, code, start, end, ktype, session=None):
        frames, page = [], None
        while True:
            self._wait()
            kw = dict(start=start, end=end, ktype=ktype, max_count=1000, page_req_key=page)
            if session is not None:
                kw["session"] = session
            ret, data, page = self.mm.ctx.request_history_kline("US." + code, **kw)
            if ret != self.ft.RET_OK:
                raise RuntimeError(str(data))
            frames.append(data)
            if page is None:
                break
        df = pd.concat(frames) if frames else pd.DataFrame()
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["time_key"]).dt.tz_localize(ET)
        return df.sort_values("ts").drop_duplicates("ts").set_index("ts")

    def overnight_1m(self, code, start, end, diag=None):
        """夜盘 1 分钟K。moomoo 的「历史K线」接口不支持夜盘时段，只能走「实时K线」：
        先订阅 1 分钟K（全时段 / 仅夜盘），再取最近 1000 根，自己截出 20:00–04:00。
        1000 根全时段 1 分钟K 大约覆盖 16 小时，所以在美东下午 2 点前运行都能拿到完整的上一个夜盘。"""
        best = pd.DataFrame()
        for name in ("ALL", "OVERNIGHT"):
            sess = getattr(self.ft.Session, name, None)
            if sess is None:
                continue
            ret, err = self.mm.ctx.subscribe(["US." + code], [self.ft.SubType.K_1M], is_first_push=False,
                                             subscribe_push=False, session=sess)
            if ret != self.ft.RET_OK:
                if diag is not None:
                    diag.append(f"{code} 订阅{name}: 失败 {str(err)[:120]}")
                continue
            ret, df = self.mm.ctx.get_cur_kline("US." + code, 1000, self.ft.KLType.K_1M)
            if ret != self.ft.RET_OK or df is None or len(df) == 0:
                if diag is not None:
                    diag.append(f"{code} {name}: 取K线失败或 0 根 {'' if ret == self.ft.RET_OK else str(df)[:100]}")
                continue
            df = df.copy()
            df["ts"] = pd.to_datetime(df["time_key"]).dt.tz_localize(ET)
            df = df.sort_values("ts").drop_duplicates("ts").set_index("ts")
            cut = df[(df.index >= start) & (df.index < end)]
            if diag is not None:
                diag.append(f"{code} {name}: 共 {len(df)} 根（{df.index[0]:%m-%d %H:%M} → {df.index[-1]:%m-%d %H:%M} 美东），"
                            f"落在夜盘时段 {len(cut)} 根")
            if len(cut) > len(best):
                best = cut
            if not cut.empty and cut.index[0] <= start + dt.timedelta(minutes=30):
                break                                     # 已经覆盖到夜盘开头
        if not best.empty:
            self.partial = self.partial or best.index[0] > start + dt.timedelta(minutes=30)
        return best

    def daily(self, code, upto):
        df = self.kline(code, str(upto - dt.timedelta(days=45)), str(upto), self.ft.KLType.K_DAY)
        if df.empty:
            return df
        return df[df.index.date <= upto]


# ================================================================ 分析一只
def analyze(code, name, m1, d1):
    """m1：夜盘 1 分钟K；d1：日K（最后一根 = 夜盘前那个常规时段）。"""
    if m1 is None or m1.empty or d1 is None or len(d1) < 15:
        return None
    m1 = m1[m1["volume"] > 0] if (m1["volume"] > 0).sum() >= 5 else m1
    prev = d1.iloc[-1]
    pc, pdh, pdl = float(prev["close"]), float(prev["high"]), float(prev["low"])
    tr = pd.concat([d1["high"] - d1["low"], (d1["high"] - d1["close"].shift()).abs(),
                    (d1["low"] - d1["close"].shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())
    avg_vol = float(d1["volume"].tail(20).mean())

    o, last = float(m1["open"].iloc[0]), float(m1["close"].iloc[-1])
    hi, lo = float(m1["high"].max()), float(m1["low"].min())
    t_hi, t_lo = m1["high"].idxmax(), m1["low"].idxmin()
    vol = float(m1["volume"].sum())
    typ = (m1["high"] + m1["low"] + m1["close"]) / 3
    vwap = float((typ * m1["volume"]).sum() / vol) if vol > 0 else float(typ.mean())
    rng = hi - lo
    pos = (last - lo) / rng if rng > 0 else 0.5
    chg = (last / pc - 1) * 100
    mid = m1[m1.index.hour == 0]
    midnight = float(mid["open"].iloc[0]) if not mid.empty else None

    # 走势：合成 5 分钟K，看收在区间哪里 + 夜盘内涨跌
    m5 = m1.resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    on_chg = (last / o - 1) * 100
    if pos >= 0.7 and on_chg > 0.2:
        trend = "单边走高、收在高位"
    elif pos <= 0.3 and on_chg < -0.2:
        trend = "单边走低、收在低位"
    elif len(m5) >= 6 and rng / atr >= 0.4 and 0.35 <= pos <= 0.65:
        trend = "冲高回落 / 探底回升，收在区间中部"
    else:
        trend = "窄幅震荡"

    # 1 分钟异动：涨跌幅和成交量都明显超过这只股票夜盘的常态
    r = m1["close"].pct_change().abs() * 100
    med_r, med_v = float(r.median() or 0), float(m1["volume"].median() or 0)
    spikes = []
    cand = m1[(r >= max(0.4, 5 * med_r)) & (m1["volume"] >= max(1, 5 * med_v))]
    prev_close = m1["close"].shift()
    for ts in r.loc[cand.index].sort_values(ascending=False).head(2).index:
        row = m1.loc[ts]
        spikes.append(dict(t=ts, move=(float(row["close"]) / float(prev_close.loc[ts]) - 1) * 100,
                           vx=float(row["volume"]) / med_v if med_v else None, price=float(row["close"])))
    spikes.sort(key=lambda s: s["t"])

    sweeps = []
    if hi > pdh:
        sweeps.append(f"扫过前日高 {pdh:.2f} 后收回" if last < pdh else f"站上前日高 {pdh:.2f}")
    if lo < pdl:
        sweeps.append(f"扫过前日低 {pdl:.2f} 后收回" if last > pdl else f"跌破前日低 {pdl:.2f}")

    return dict(code=code, name=name, pc=pc, open=o, last=last, hi=hi, lo=lo, t_hi=t_hi, t_lo=t_lo,
                chg=chg, rng_atr=rng / atr if atr else None, vol=vol,
                vol_share=vol / avg_vol * 100 if avg_vol else None, vwap=vwap, above_vwap=last >= vwap,
                pos=pos, trend=trend, midnight=midnight, spikes=spikes, sweeps=sweeps, bars=len(m1))


def notable(a):
    return (abs(a["chg"]) >= 1 or (a["rng_atr"] or 0) >= 0.5 or (a["vol_share"] or 0) >= 3
            or a["spikes"] or a["sweeps"])


# ================================================================ 汇总
def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _news_near(spike, items, minutes=20):
    for it in items or []:
        t = it.get("t")
        if t is not None and abs((t - spike["t"]).total_seconds()) <= minutes * 60:
            return it
    return None


def build(cfg, log=lambda *a: None):
    import stocks
    import sectors as SEC

    start, end, live = session_window()
    if start is None:
        return ["没有找到最近的夜盘时段。"]
    params, defaults, lines, profiles, order = stocks.load_settings(log)
    mm_cfg = cfg.get("moomoo", {})
    mm = stocks.Moomoo(mm_cfg, log)
    if not mm.ok:
        return ["🌙 夜盘复盘需要 moomoo OpenD：请在电脑上打开 OpenD 并登录后再试。"]
    log(f"[夜盘] 时段 {start:%m-%d %H:%M} → {end:%m-%d %H:%M} 美东")
    try:
        watch = mm.watchlist(mm_cfg.get("自选分组", "全部")) or \
            [dict(code=c, name=params[c]["名称"], etf=False) for c in order]
        for w in watch:
            w["sector"] = (params.get(w["code"]) or {}).get("板块") or ""
        f = Fetcher(mm, log)
        res, errs, diag = [], [], []
        for i, w in enumerate(watch):
            try:
                m1 = f.overnight_1m(w["code"], start, end, diag if i < 3 else None)   # 前 3 只记诊断
                d1 = f.daily(w["code"], start.date())
                a = analyze(w["code"], w["name"], m1, d1)
                if a:
                    a["sector"] = w["sector"]
                    res.append(a)
            except Exception as e:
                errs.append(f"{w['code']}: {_clip(str(e), 60)}")
    finally:
        mm.close()

    partial = getattr(f, "partial", False)
    head = (f"**🌙 夜盘复盘**　{start:%m-%d} 美东 20:00 → {end:%m-%d} 04:00"
            f"（你的时间 {_local(start):%m-%d %H:%M} – {_local(end):%H:%M}）" + ("　⏳ 夜盘还在进行中" if live else "")
            + ("\n_⚠ 现在离夜盘结束较久，moomoo 只给最近 1000 根 1 分钟K，夜盘前段的数据不全_" if partial else ""))
    for d in diag:
        log("[夜盘诊断] " + d)
    if not res:
        import briefing as B
        msg = [head, "", "❌ 没有拿到夜盘 1 分钟数据。"]
        if errs:
            msg.append("接口报错：" + "；".join(errs[:3]))
        if diag:
            msg.append("moomoo 实际返回（前 3 只）：")
            msg += [f"· {_clip(d, 150)}" for d in diag[:6]]
        msg.append("常见原因：账号没有美股夜盘行情权限、订阅额度用完，或 OpenD 版本太旧。把这条发给 Claude 看看。")
        return B._split(msg)

    # 消息：华尔街见闻快讯里提到这些股票的
    try:
        import briefing
        by_stock, _ = briefing.flash([dict(code=a["code"], name=a["name"], etf=False) for a in res],
                                     hours=max(10, (dt.datetime.now(ET) - start).total_seconds() / 3600 + 1),
                                     per_stock=6)
    except Exception:
        by_stock = {}

    idx = {a["code"]: a for a in res}
    pts = []
    for c in ("QQQ", "SPY"):
        if c in idx:
            a = idx[c]
            pts.append(f"**大盘**：{a['name']} 夜盘 {a['chg']:+.2f}%，{a['trend']}，"
                       f"收于 VWAP {'上方' if a['above_vwap'] else '下方'}")
            break
    stocks_only = [a for a in res if not SEC.is_index(a["sector"])]
    if stocks_only:
        ups = sum(a["chg"] > 0 for a in stocks_only)
        top = max(stocks_only, key=lambda a: a["chg"])
        bot = min(stocks_only, key=lambda a: a["chg"])
        pts.append(f"**自选股**：{ups} 涨 / {len(stocks_only) - ups} 跌；最强 {top['name']} {top['chg']:+.1f}%，"
                   f"最弱 {bot['name']} {bot['chg']:+.1f}%")
        act = sorted([a for a in stocks_only if a["vol_share"]], key=lambda a: -a["vol_share"])[:3]
        if act:
            pts.append("**最活跃**：" + "、".join(f"{a['name']}（成交占日均 {a['vol_share']:.1f}%）" for a in act))
    sw = [f"{a['name']} {s}" for a in res for s in a["sweeps"] if "收回" in s]
    if sw:
        pts.append("**扫流动性后收回**：" + "；".join(sw[:4]))
    groups = SEC.group([(a["sector"], a["chg"], a) for a in res])
    anos = SEC.anomalies(groups, {"板块异动_平均涨跌幅%": 1.5, "板块异动_同向比例": 0.75})   # 夜盘波动小，阈值放低
    if anos:
        pts.append("**板块**：" + "；".join(SEC.anomaly_text(x) for x in anos))

    parts = [head, "", "**🎯 要点**"] + [f"· {p}" for p in pts] + [""]
    parts.append("**📈 个股**（只列有异动的；🧲 = 开盘前要盯的位置）")
    shown = 0
    for g in groups:
        mem = sorted([a for a in g["members"] if notable(a)], key=lambda a: -abs(a["chg"]))
        if not mem:
            continue
        parts.append(f"**{SEC.label(g['name'])}**　夜盘平均 {g['avg']:+.2f}%")
        for a in mem:
            shown += 1
            icon = "🔺" if a["chg"] > 0 else "🔻" if a["chg"] < 0 else "▫️"
            parts.append(f"{icon} **{a['name']}** {a['code']}　{a['chg']:+.2f}%（{a['last']:.2f}）　{a['trend']}")
            info = [f"区间 {a['lo']:.2f}–{a['hi']:.2f}" + (f"（{a['rng_atr']:.1f} 倍 ATR）" if a["rng_atr"] else "")]
            if a["vol_share"] is not None:
                info.append(f"成交占日均 {a['vol_share']:.1f}%")
            info.append(f"VWAP {a['vwap']:.2f}（收在{'上方' if a['above_vwap'] else '下方'}）")
            parts.append("　" + "　·　".join(info))
            for s in a["spikes"]:
                news = _news_near(s, by_stock.get(a["code"]))
                txt = (f"　⚡ {_local(s['t']):%H:%M}（美东 {s['t']:%H:%M}）1 分钟 {s['move']:+.2f}%"
                       + (f"，量是平时 {s['vx']:.0f} 倍" if s.get("vx") else ""))
                if news:
                    txt += f" → [{_clip(news['text'], 40)}](<{news['url']}>)" if news.get("url") else f" → {_clip(news['text'], 40)}"
                parts.append(txt)
            for s in a["sweeps"]:
                parts.append(f"　🧲 {s}")
            lv = [f"夜盘高 {a['hi']:.2f}", f"夜盘低 {a['lo']:.2f}", f"VWAP {a['vwap']:.2f}"]
            if a["midnight"]:
                lv.append(f"午夜开盘 {a['midnight']:.2f}")
            parts.append("　🧲 " + " / ".join(lv))
    rest = len(res) - shown
    if rest > 0:
        parts.append(f"其余 {rest} 只夜盘平淡（涨跌 <1%、区间 <0.5 ATR、没有异动）。")
    if errs:
        parts.append(f"_{len(errs)} 只没拿到数据：{'；'.join(errs[:3])}_")
    parts += ["", "_夜盘成交稀薄，单笔大单就能拉出影线；夜盘高低点和 VWAP 更适合当开盘后的参考位，不宜单独据此下单。仅供参考，不构成投资建议。_"]

    import briefing as B
    return B._split(parts)


if __name__ == "__main__":
    import macro_report as mr
    for m in build(mr.load_config(), log=print):
        print(m)
        print("-" * 40)
