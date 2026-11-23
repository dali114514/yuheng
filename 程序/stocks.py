# -*- coding: utf-8 -*-
"""
第二个平台：自选股。

数据：
  · 自选股列表、价格提醒、日K —— moomoo OpenAPI（需在本机运行 OpenD）
  · OpenD 不可用时，日K 改用 Yahoo，自选股用「个股设置.xlsx」里的列表
  · 公司简介、财报日、新闻标题 —— Yahoo（可选，拿不到就跳过）

用户自定义：项目文件夹/个股设置.xlsx
  关键位（自己画的线）/ 个股参数（板块ETF、放量倍数、RSI 阈值…）/ 公司档案
"""
import datetime as dt
import socket
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
XLSX_PATH = BASE / "个股设置.xlsx"

LINE_TYPES = ("压力", "支撑", "关键")
DEFAULT_PARAMS = {
    "板块": "", "对比ETF": "QQQ", "放量倍数": 1.5, "明显放量倍数": 2.0,
    "RSI超卖": 30, "RSI超买": 70, "临近距离%": 3.0,
}
SKIP_TYPES = {"DRVT", "WARRANT", "FUTURE", "BOND", "IDX", "PLATE", "PLATESET"}


# ================================================================ 设置文档
def _num(v, default):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return float(v)
    except Exception:
        return default


def _code(v):
    """统一成纯代码：'US.AAPL' / 'aapl ' → 'AAPL'。"""
    s = str(v or "").strip().upper()
    if "." in s and s.split(".")[0] in ("US", "HK", "SH", "SZ", "SG", "JP"):
        s = s.split(".", 1)[1]
    return s


def load_settings(log):
    """读取个股设置.xlsx，返回 (params{code:dict}, defaults, lines[...], profiles{code:dict}, order[])。"""
    params, lines, profiles, order = {}, [], {}, []
    defaults = dict(DEFAULT_PARAMS)
    if not XLSX_PATH.exists():
        log("  · 没有找到 个股设置.xlsx，使用默认参数")
        return params, defaults, lines, profiles, order
    try:
        from openpyxl import load_workbook
        wb = load_workbook(XLSX_PATH, data_only=True, read_only=True)
    except Exception as e:
        log(f"  ✗ 个股设置.xlsx 打不开（是不是正在编辑未保存？）：{e}")
        return params, defaults, lines, profiles, order

    def rows(sheet):
        if sheet not in wb.sheetnames:
            return []
        it = wb[sheet].iter_rows(values_only=True)
        try:
            head = [str(h or "").strip() for h in next(it)]
        except StopIteration:
            return []
        out = []
        for r in it:
            d = {head[i]: r[i] for i in range(min(len(head), len(r))) if head[i]}
            if any(v not in (None, "") for v in d.values()):
                out.append(d)
        return out

    for d in rows("个股参数"):
        code = _code(d.get("代码"))
        if not code:
            continue
        p = {
            "名称": str(d.get("名称") or "").strip(),
            "板块": str(d.get("板块") or "").strip(),
            "对比ETF": _code(d.get("对比ETF")),
            "放量倍数": _num(d.get("放量倍数"), None),
            "明显放量倍数": _num(d.get("明显放量倍数"), None),
            "RSI超卖": _num(d.get("RSI超卖"), None),
            "RSI超买": _num(d.get("RSI超买"), None),
            "临近距离%": _num(d.get("临近距离%"), None),
            "加入报告": str(d.get("加入报告") or "是").strip() not in ("否", "N", "n", "no", "No"),
        }
        if code in ("默认", "DEFAULT"):
            for k in DEFAULT_PARAMS:
                if p.get(k) not in (None, ""):
                    defaults[k] = p[k]
            continue
        params[code] = p
        order.append(code)

    for i, d in enumerate(rows("关键位")):
        code = _code(d.get("代码"))
        price = _num(d.get("价格"), None)
        if not code or price is None:
            continue
        if str(d.get("启用") or "是").strip() in ("否", "N", "n", "no", "No"):
            continue
        t = str(d.get("类型") or "关键").strip()
        if t not in LINE_TYPES:
            t = "关键"
        lines.append(dict(code=code, type=t, price=price,
                          note=str(d.get("备注") or "").strip(), src="手动"))

    for d in rows("公司档案"):
        code = _code(d.get("代码"))
        if code:
            profiles[code] = {k: str(v).strip() for k, v in d.items()
                              if k != "代码" and v not in (None, "")}
    log(f"  ✓ 个股设置.xlsx：{len(params)} 只股票参数，{len(lines)} 条自定义关键位")
    return params, defaults, lines, profiles, order


# ================================================================ moomoo OpenAPI
def _port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


class Moomoo:
    """对 moomoo / futu OpenAPI 的薄封装；任何一步失败都返回 None，由调用方退回 Yahoo。"""

    def __init__(self, cfg, log):
        self.log, self.ctx, self.ft = log, None, None
        host = cfg.get("OpenD地址", "127.0.0.1")
        port = int(cfg.get("OpenD端口", 11111))
        if not cfg.get("启用", True):
            log("  · moomoo OpenAPI 已在设置中关闭")
            return
        if not _port_open(host, port):
            log(f"  · 没有检测到 OpenD（{host}:{port}），自选股改用 个股设置.xlsx 列表、行情改用 Yahoo")
            return
        for mod in ("moomoo", "futu"):
            try:
                self.ft = __import__(mod)
                break
            except Exception:
                continue
        if self.ft is None:
            log("  · 没有安装 moomoo-api，跳过 OpenAPI")
            return
        try:
            self.ft.SysConfig.set_all_thread_daemon(True)
        except Exception:
            pass
        try:
            self.ctx = self.ft.OpenQuoteContext(host=host, port=port)
            log(f"  ✓ 已连接 OpenD（{host}:{port}）")
        except Exception as e:
            log(f"  ✗ 连接 OpenD 失败：{e}")
            self.ctx = None

    @property
    def ok(self):
        return self.ctx is not None

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass

    def watchlist(self, group):
        if not self.ok:
            return None
        try:
            ret, data = self.ctx.get_user_security(group)
            if ret != self.ft.RET_OK:
                # 分组名随 OpenD 界面语言变化：英文是 All，中文是 全部
                for alt in ("All", "全部", "美股", "US"):
                    if alt != group:
                        r2, d2 = self.ctx.get_user_security(alt)
                        if r2 == self.ft.RET_OK:
                            self.log(f"  · 自选分组「{group}」不存在，改用「{alt}」")
                            ret, data, group = r2, d2, alt
                            break
            if ret != self.ft.RET_OK:
                self.log(f"  ✗ 读取自选分组「{group}」失败：{data}")
                try:
                    r2, g = self.ctx.get_user_security_group()
                    if r2 == self.ft.RET_OK:
                        self.log("    可用分组：" + "、".join(map(str, g["group_name"].tolist())))
                except Exception:
                    pass
                return None
            out = []
            for _, r in data.iterrows():
                code = str(r.get("code", ""))
                if not code.startswith("US."):
                    continue                       # 目前只处理美股
                if str(r.get("stock_type", "")).upper() in SKIP_TYPES:
                    continue                       # 跳过期权等衍生品
                out.append(dict(code=_code(code), name=str(r.get("name", "")),
                                etf=str(r.get("stock_type", "")).upper() == "ETF"))
            self.log(f"  ✓ moomoo 自选「{group}」：{len(out)} 只美股/ETF（已跳过期权）")
            return out
        except Exception as e:
            self.log(f"  ✗ 读取自选股出错：{e}")
            return None

    def reminders(self):
        """价格提醒 → 关键位。涨到 = 压力，跌到 = 支撑。"""
        if not self.ok:
            return []
        try:
            ret, data = self.ctx.get_price_reminder(code=None, market=self.ft.Market.US)
            if ret != self.ft.RET_OK:
                if "empty" not in str(data):
                    self.log(f"  · 读取价格提醒失败：{data}")
                else:
                    self.log("  ✓ moomoo 价格提醒：0 条")
                return []
            out = []
            for _, r in data.iterrows():
                t = str(r.get("reminder_type", "")).upper()
                if "PRICE_UP" in t:
                    typ = "压力"
                elif "PRICE_DOWN" in t:
                    typ = "支撑"
                else:
                    continue                       # 涨跌幅类提醒不是价位
                enable = r.get("enable", True)
                if enable is False or str(enable).lower() == "false":
                    continue
                out.append(dict(code=_code(r.get("code")), type=typ, price=float(r.get("value")),
                                note=str(r.get("note") or "").strip(), src="moomoo提醒"))
            self.log(f"  ✓ moomoo 价格提醒：{len(out)} 条价位提醒")
            return out
        except Exception as e:
            self.log(f"  · 读取价格提醒出错：{e}")
            return []

    def kline(self, code, days=400):
        if not self.ok:
            return None
        try:
            end = dt.date.today()
            start = end - dt.timedelta(days=days)
            frames, page = [], None
            while True:
                ret, data, page = self.ctx.request_history_kline(
                    "US." + code, start=str(start), end=str(end),
                    ktype=self.ft.KLType.K_DAY, autype=self.ft.AuType.QFQ,
                    max_count=1000, page_req_key=page)
                if ret != self.ft.RET_OK:
                    return None
                frames.append(data)
                if page is None:
                    break
            df = pd.concat(frames)
            df.index = pd.to_datetime(df["time_key"]).dt.normalize()
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            return df[~df.index.duplicated(keep="last")].sort_index()
        except Exception:
            return None


# ================================================================ Yahoo
def yahoo_kline(code):
    try:
        import yfinance as yf
        h = yf.Ticker(code).history(period="2y", interval="1d", auto_adjust=False)
        h = h.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
        return h[~h.index.duplicated(keep="last")]
    except Exception:
        return None


def yahoo_extra(code):
    """财报日 + 近 3 天新闻标题（拿不到就返回空）。"""
    out = {"earnings": None, "news": [], "sector": "", "industry": ""}
    try:
        import yfinance as yf
        t = yf.Ticker(code)
    except Exception:
        return out
    try:
        cal = t.calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            ed = ed[0] if isinstance(ed, (list, tuple)) else ed
            out["earnings"] = pd.Timestamp(ed).date()
    except Exception:
        pass
    try:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
        for n in (t.news or [])[:15]:
            c = n.get("content", n)
            title = c.get("title")
            when = c.get("pubDate") or c.get("providerPublishTime")
            ts = pd.Timestamp(when, unit="s", tz="UTC") if isinstance(when, (int, float)) \
                else pd.Timestamp(when) if when else None
            if ts is not None and ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if title and (ts is None or ts >= cutoff):
                prov = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
                url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                       or c.get("link") or "")
                out["news"].append(dict(title=title, src=prov, url=url,
                                        date=ts.tz_convert("America/New_York").strftime("%m-%d") if ts is not None else ""))
            if len(out["news"]) >= 5:
                break
    except Exception:
        pass
    return out


# ================================================================ 指标
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def macd(close, fast=12, slow=26, sig=9):
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    return dif, dea, 2 * (dif - dea)          # 柱状图采用 moomoo 的 ×2 口径


def last_cross(dif, dea, lookback=10):
    """最近 lookback 根K线内的最后一次金叉/死叉：(几天前, '金叉'/'死叉', 零轴之上/之下)。"""
    s = np.sign(dif - dea)
    for i in range(1, min(lookback, len(s) - 1) + 1):
        a, b = s.iloc[-i - 1], s.iloc[-i]
        if a != b and b != 0 and a != 0:
            kind = "金叉" if b > 0 else "死叉"
            pos = "零轴之上" if dif.iloc[-i] > 0 else "零轴之下"
            return i - 1, kind, pos
    return None


MACD_MEANING = {
    ("金叉", "零轴之上"): ("强", "上升趋势中回调结束、重新走强"),
    ("死叉", "零轴之上"): ("中", "上升趋势中的回调，未必转势"),
    ("金叉", "零轴之下"): ("弱", "下降趋势中的反弹，需确认"),
    ("死叉", "零轴之下"): ("弱", "下降趋势延续"),
}


def pct(a, b):
    return (a / b - 1) * 100 if b else np.nan


# ================================================================ 分析
def analyze_stock(code, k, p, lines, etf_k):
    c, v = k["close"], k["volume"]
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    r = rsi(c)
    dif, dea, hist = macd(c)
    avg20 = float(v.iloc[-21:-1].mean()) if len(v) > 21 else float(v.mean())
    vr = float(v.iloc[-1]) / avg20 if avg20 else np.nan
    res = dict(
        code=code, name=p.get("名称") or code, date=k.index[-1].date(),
        last=last, c1=pct(last, prev),
        c5=pct(last, float(c.iloc[-6])) if len(c) > 6 else np.nan,
        c20=pct(last, float(c.iloc[-21])) if len(c) > 21 else np.nan,
        rsi=float(r.iloc[-1]), rsi_prev=float(r.iloc[-2]),
        dif=float(dif.iloc[-1]), dea=float(dea.iloc[-1]),
        hist=float(hist.iloc[-1]), hist_prev=float(hist.iloc[-2]),
        cross=last_cross(dif, dea), vr=vr, vol=float(v.iloc[-1]), avg20=avg20,
        ma20=float(c.tail(20).mean()), ma50=float(c.tail(50).mean()),
        ma200=float(c.tail(200).mean()) if len(c) >= 200 else np.nan,
        signals=[], levels=[],
    )
    sig = res["signals"]

    def add(level, text):
        sig.append((level, text))

    # --- RSI ---
    lo, hi = p["RSI超卖"], p["RSI超买"]
    rv, rp = res["rsi"], res["rsi_prev"]
    if rv < lo:
        add("中", f"RSI {rv:.1f} 低于 {lo:.0f}，超卖" + ("（今日进入）" if rp >= lo else ""))
    elif rp < lo <= rv:
        add("中", f"RSI 回升到 {rv:.1f}，重新站上 {lo:.0f}（超卖回穿，较可靠的反弹信号）")
    if rv > hi:
        add("中", f"RSI {rv:.1f} 高于 {hi:.0f}，超买" + ("（今日进入）" if rp <= hi else ""))
    elif rp > hi >= rv:
        add("中", f"RSI 回落到 {rv:.1f}，跌回 {hi:.0f} 以下（超买回落，警惕转弱）")

    # --- RSI / MACD 背离（近 20 日对比前 20–60 日）---
    if len(c) > 60:
        rec, old = slice(-20, None), slice(-60, -20)
        if c.iloc[rec].max() > c.iloc[old].max() and r.iloc[rec].max() < r.iloc[old].max() - 2 \
                and c.iloc[-5:].max() >= c.iloc[rec].max() * 0.98:
            add("中", "顶背离：股价创近期新高，RSI 没有跟随创新高，上涨动能减弱")
        if c.iloc[rec].min() < c.iloc[old].min() and r.iloc[rec].min() > r.iloc[old].min() + 2 \
                and c.iloc[-5:].min() <= c.iloc[rec].min() * 1.02:
            add("中", "底背离：股价创近期新低，RSI 没有跟随创新低，下跌动能减弱")

    # --- MACD ---
    if res["cross"]:
        ago, kind, pos = res["cross"]
        strength, mean = MACD_MEANING[(kind, pos)]
        when = "今日" if ago == 0 else f"{ago} 天前"
        lvl = "高" if ago == 0 and strength == "强" or (ago == 0 and kind == "死叉" and pos == "零轴之下") else \
              ("中" if ago <= 2 else "提示")
        add(lvl, f"MACD {when}{pos}{kind}：{mean}")
    if res["dif"] * float(dif.iloc[-2]) < 0:
        add("中", "MACD 的 DIF 今日" + ("上穿零轴，趋势转多" if res["dif"] > 0 else "下穿零轴，趋势转空"))
    if not res["cross"] and abs(res["hist"]) < abs(res["hist_prev"]) and len(hist) > 3 \
            and abs(hist.iloc[-2]) < abs(hist.iloc[-3]):
        add("提示", "MACD 柱连续缩短，" + ("多头动能减弱，可能接近死叉" if res["hist"] > 0 else "空头动能减弱，可能接近金叉"))

    # --- 成交量 ---
    if vr >= p["明显放量倍数"]:
        dirn = "上涨" if res["c1"] > 0 else "下跌"
        add("高" if dirn == "下跌" else "中", f"明显放量{dirn}：成交量是 20 日均量的 {vr:.1f} 倍")
    elif vr >= p["放量倍数"]:
        dirn = "上涨" if res["c1"] > 0 else "下跌"
        add("提示", f"放量{dirn}：成交量是 20 日均量的 {vr:.1f} 倍")

    # --- 关键位 ---
    near = p["临近距离%"]
    events = []
    for ln in lines:
        lv = ln["price"]
        dist = pct(last, lv)
        crossed_up = prev < lv <= last
        crossed_dn = prev > lv >= last
        state, level = None, None
        if ln["type"] == "压力":
            if crossed_up:
                state, level = "今日突破", "高"
            elif crossed_dn:
                state, level = "今日跌回下方", "中"          # 突破失败 / 压力重新压制
            elif last > lv:
                state = "已在上方"
            elif -near <= dist < 0:
                state, level = "临近", "提示"
        elif ln["type"] == "支撑":
            if crossed_dn:
                state, level = "今日跌破", "高"
            elif crossed_up:
                state, level = "今日重新站上", "中"          # 收复失守的支撑
            elif last < lv:
                state = "已在下方"
            elif 0 < dist <= near:
                state, level = "临近", "提示"
        else:
            if crossed_up:
                state, level = "今日上穿", "高"
            elif crossed_dn:
                state, level = "今日下穿", "高"
            elif abs(dist) <= near:
                state, level = "临近", "提示"
        if state and (crossed_up or crossed_dn):
            if vr >= p["放量倍数"]:
                state += f"（放量 {vr:.1f} 倍，有效性较高）"
            else:
                state += "（未放量，需确认）"
        res["levels"].append(dict(ln, dist=dist, state=state or ""))
        if level:
            events.append(dict(level=level, ln=ln, state=state, dist=dist,
                               way="up" if crossed_up else "dn" if crossed_dn else "near"))

    # 同一方向穿过多条线时合并成一条；“临近”只报上方、下方最近的各一条，避免线密集时刷屏
    def short_note(ln):
        s = ln["note"].split("；")[0].replace("枢轴｜", "枢轴 ")
        return f"（{s if len(s) <= 28 else s[:27] + '…'}）" if s else ""

    rank = {"高": 0, "中": 1, "提示": 2}
    for way in ("up", "dn"):
        ev = [e for e in events if e["way"] == way]
        if not ev:
            continue
        e = min(ev, key=lambda e: abs(e["dist"]))
        lvl = min((x["level"] for x in ev), key=rank.get)
        more = f"；今日共{'上穿' if way == 'up' else '下穿'} {len(ev)} 条线：" + \
               "、".join(f"{x['ln']['price']:.2f}" for x in sorted(ev, key=lambda x: -x["ln"]["price"])) if len(ev) > 1 else ""
        add(lvl, f"{e['ln']['type']}位 {e['ln']['price']:.2f}{short_note(e['ln'])} {e['state']}，"
                 f"现 {last:.2f}，距离 {e['dist']:+.1f}%{more}")
    near_ev = [e for e in events if e["way"] == "near"]
    for side in (lambda e: e["dist"] < 0, lambda e: e["dist"] >= 0):
        ev = [e for e in near_ev if side(e)]
        if ev:
            e = min(ev, key=lambda e: abs(e["dist"]))
            add("提示", f"{e['ln']['type']}位 {e['ln']['price']:.2f}{short_note(e['ln'])} 临近，"
                       f"现 {last:.2f}，距离 {e['dist']:+.1f}%")

    # --- 板块对比 ---
    res["etf"] = p["对比ETF"]
    res["sector"] = p["板块"]
    if etf_k is not None and len(etf_k) > 30 and p["对比ETF"] != code:
        e = etf_k["close"]
        j = pd.concat([c.pct_change(), e.pct_change()], axis=1, join="inner").dropna()
        j.columns = ["s", "e"]
        j60 = j.tail(60)
        res["corr"] = float(j60["s"].corr(j60["e"])) if len(j60) > 20 else np.nan
        res["beta"] = float(j60["s"].cov(j60["e"]) / j60["e"].var()) if len(j60) > 20 and j60["e"].var() else np.nan
        j20 = j.tail(20)
        res["same_dir"] = float((np.sign(j20["s"]) == np.sign(j20["e"])).mean() * 100) if len(j20) else np.nan
        ec = e.reindex(c.index).ffill()
        res["e1"] = pct(float(ec.iloc[-1]), float(ec.iloc[-2]))
        res["rs5"] = res["c5"] - pct(float(ec.iloc[-1]), float(ec.iloc[-6]))
        res["rs20"] = res["c20"] - pct(float(ec.iloc[-1]), float(ec.iloc[-21]))
        rs1 = res["c1"] - res["e1"]
        if abs(res["e1"]) >= 1 and np.sign(res["c1"]) != np.sign(res["e1"]) and abs(rs1) >= 2:
            if res["c1"] < 0:
                add("中", f"板块（{res['etf']} {res['e1']:+.1f}%）上涨但个股下跌 {res['c1']:.1f}%：个股自身可能有利空，查新闻")
            else:
                add("提示", f"板块（{res['etf']} {res['e1']:+.1f}%）下跌但个股逆势上涨 {res['c1']:+.1f}%：有独立逻辑，相对强势")
    else:
        res.update(corr=np.nan, beta=np.nan, same_dir=np.nan, e1=np.nan, rs5=np.nan, rs20=np.nan)

    order = {"高": 0, "中": 1, "提示": 2}
    sig.sort(key=lambda s: order[s[0]])
    return res


def trend_txt(r):
    if r["last"] > r["ma20"] > r["ma50"]:
        t = "上升趋势"
    elif r["last"] < r["ma20"] < r["ma50"]:
        t = "下降趋势"
    else:
        t = "震荡"
    if not np.isnan(r["ma200"]):
        t += "，200日线" + ("上方" if r["last"] > r["ma200"] else "下方")
    return t


def macd_state(r):
    pos = "零轴上" if r["dif"] > 0 else "零轴下"
    if r["cross"]:
        ago, kind, p = r["cross"]
        return f"{pos}，{'今日' if ago == 0 else f'{ago}天前'}{kind}"
    return f"{pos}，{'多头' if r['dif'] > r['dea'] else '空头'}排列"


def link_txt(r):
    cr = r.get("corr", np.nan)
    if np.isnan(cr):
        return "—"
    if cr >= 0.7:
        return f"高度同步（相关 {cr:.2f}）"
    if cr >= 0.4:
        return f"较同步（相关 {cr:.2f}）"
    return f"走势较独立（相关 {cr:.2f}）"


# ================================================================ 主流程
def run(cfg, log=print, fake=False, light=False):
    mm_cfg = cfg.get("moomoo", {})
    params, defaults, lines, profiles, order = load_settings(log)

    if fake:
        mm = None
        watch = [dict(code=c, name=params[c]["名称"], etf=False) for c in order]
        reminders = []
    else:
        mm = Moomoo(mm_cfg, log)
        watch = mm.watchlist(mm_cfg.get("自选分组", "全部")) if mm.ok else None
        reminders = mm.reminders() if (mm.ok and mm_cfg.get("读取价格提醒作为关键位", True)) else []
    watch_src = "moomoo 自选" if watch else "个股设置.xlsx"
    if not watch:
        watch = [dict(code=c, name=params[c]["名称"], etf=False) for c in order]
    # 设置文档里有、moomoo 自选里没有的股票也加进来；「加入报告=否」的去掉
    have = {w["code"] for w in watch}
    for c in order:
        if c not in have:
            watch.append(dict(code=c, name=params[c]["名称"], etf=False))
    watch = [w for w in watch if params.get(w["code"], {}).get("加入报告", True)]

    def P(code, name):
        p = dict(defaults)
        for k, v in params.get(code, {}).items():
            if v not in (None, ""):
                p[k] = v
        p["名称"] = p.get("名称") or name or code
        if not p.get("对比ETF"):
            p["对比ETF"] = defaults["对比ETF"]
        return p

    kcache, ksrc = {}, {}

    def get_k(code):
        if code in kcache:
            return kcache[code]
        k, s = None, "缺失"
        if fake:
            k, s = fake_kline(code), "模拟"
        else:
            if mm is not None and mm.ok:
                k = mm.kline(code)
                s = "moomoo" if k is not None else s
            if k is None:
                k = yahoo_kline(code)
                s = "Yahoo" if k is not None else s
        if k is not None and len(k) < 35:
            k, s = None, "数据太少"
        kcache[code], ksrc[code] = k, s
        return k

    results, missing = [], []
    for w in watch:
        code = w["code"]
        p = P(code, w["name"])
        k = get_k(code)
        if k is None:
            missing.append(f"{p['名称']}（{code}）")
            continue
        etf_k = get_k(p["对比ETF"]) if p["对比ETF"] and p["对比ETF"] != code else None
        my_lines = [l for l in lines + reminders if l["code"] == code]
        # 同价位同类型去重（设置文档和 moomoo 提醒重复时只留一条）
        seen, uniq = set(), []
        for l in my_lines:
            key = (l["type"], round(l["price"], 2))
            if key not in seen:
                seen.add(key)
                uniq.append(l)
        r = analyze_stock(code, k, p, sorted(uniq, key=lambda l: -l["price"]), etf_k)
        r["src"] = ksrc[code]
        r["k"] = k
        r["p"] = p
        r["all_lines"] = uniq
        r["profile"] = profiles.get(code, {})
        r["extra"] = {} if (fake or light) else yahoo_extra(code)   # 盘中监测不取新闻/财报日（太慢）
        if fake:
            r["extra"] = {"earnings": dt.date.today() + dt.timedelta(days=5 + len(results) * 9),
                          "news": [dict(title="(模拟) Sample headline for testing", src="Test", date="10-31")]}
        results.append(r)
        log(f"  ✓ {r['name']:<10} [{r['src']}] {r['last']:.2f}  RSI {r['rsi']:.0f}  量比 {r['vr']:.1f}  信号 {len(r['signals'])}")
    for m in missing:
        log(f"  ✗ {m} 没有拿到日K")

    # 板块汇总
    import sectors as SEC
    sectors = {}
    for r in results:
        sectors.setdefault(r["sector"] or "未分类", []).append(r)
    sector_rows = []
    for name, rs in sectors.items():
        etfs = [r["etf"] for r in rs if r["etf"] and r["etf"] != r["code"]]
        etf = SEC.etf(name) or (max(set(etfs), key=etfs.count) if etfs else "")
        ek = kcache.get(etf) if etf else None
        if ek is None and etf and not fake:
            try:
                ek = get_k(etf)
            except Exception:
                ek = None
        e1 = e5 = np.nan
        if ek is not None and len(ek) > 6:
            e = ek["close"]
            e1, e5 = pct(float(e.iloc[-1]), float(e.iloc[-2])), pct(float(e.iloc[-1]), float(e.iloc[-6]))
        s1 = float(np.nanmean([r["c1"] for r in rs]))
        ups = sum(1 for r in rs if r["c1"] > 0)
        corr = np.nanmean([r.get("corr", np.nan) for r in rs]) if any(not np.isnan(r.get("corr", np.nan)) for r in rs) else np.nan
        sector_rows.append(dict(name=name, etf=etf, e1=e1, e5=e5, s1=s1, n=len(rs), ups=ups, corr=corr,
                                members=[r for r in rs]))
    sector_rows.sort(key=lambda s: SEC.order(s["name"]))

    # OpenD 连接留给第三板块（期权）继续用，由调用方关闭
    return dict(results=results, missing=missing, sectors=sector_rows, watch_src=watch_src,
                n_lines=len(lines), n_reminders=len(reminders), ksrc=ksrc,
                kcache=kcache, get_k=get_k, mm=mm, fake=fake)


def fake_kline(code):
    seed = sum(ord(ch) for ch in code)
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=300)
    base = 50 + seed % 400
    c = base * np.exp(np.cumsum(rng.normal(0.0004, 0.02, len(idx))))
    v = rng.lognormal(16, 0.3, len(idx))
    if seed % 3 == 0:
        v[-1] *= 2.6
        c[-1] = c[-2] * 0.95
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": v}, index=idx)


# ================================================================ 写入报告
def add_to_doc(S, h):
    """h：报告的排版工具（para / table / heading / colors）。"""
    para, table, heading = h["para"], h["table"], h["heading"]
    RED, GREEN, GREY, ORANGE = h["RED"], h["GREEN"], h["GREY"], h["ORANGE"]
    R = S["results"]

    def f1(x, unit="%"):
        return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.1f}{unit}"

    def col(x):
        return None if x is None or np.isnan(x) else (GREEN if x < 0 else RED if x > 0 else None)   # 红涨绿跌，与 moomoo 一致

    heading("七、自选股总览", 1)
    para(f"共 {len(R)} 只（列表来源：{S['watch_src']}；关键位：自定义 {S['n_lines']} 条 + moomoo 价格提醒 {S['n_reminders']} 条）。"
         "按板块分组（▌开头的是板块汇总行：成员平均涨跌和上涨家数），组内按今日信号数量排序。", color=GREY, size=9)
    if S["missing"]:
        para("没有拿到数据：" + "、".join(S["missing"]), color=RED, size=9)
    import sectors as SEC
    R = sorted(R, key=lambda r: (SEC.order(r["sector"]), -sum(1 for s in r["signals"] if s[0] == "高"), -len(r["signals"])))
    rows, cols = [], []
    cur = object()
    for r in R:
        if r["sector"] != cur:
            cur = r["sector"]
            mem = [x for x in R if x["sector"] == cur]
            a1 = float(np.nanmean([x["c1"] for x in mem]))
            a5 = float(np.nanmean([x["c5"] for x in mem]))
            rows.append([f"▌{SEC.label(cur)}", "", f1(a1), f1(a5), "", "", "",
                         f"{sum(1 for x in mem if x['c1'] > 0)}/{len(mem)} 只上涨", ""])
            cols.append([ORANGE, None, col(a1), col(a5), None, None, None, None, None])
        rsi_c = RED if r["rsi"] < 30 or r["rsi"] > 70 else None
        vr_c = ORANGE if r["vr"] >= 1.5 else None
        lv = [l for l in r["levels"] if l["state"] and not l["state"].startswith("已在")]
        lv_txt = "；".join(f"{l['type']}{l['price']:.2f} {l['state'].split('（')[0]}" for l in lv) or "—"
        rows.append([f"{r['name']}\n{r['code']}", f"{r['last']:.2f}", f1(r["c1"]), f1(r["c5"]),
                     f"{r['rsi']:.0f}", macd_state(r), f"{r['vr']:.1f}x", lv_txt, f1(r.get("rs5"))])
        cols.append([None, None, col(r["c1"]), col(r["c5"]), rsi_c,
                     RED if "金叉" in macd_state(r) else GREEN if "死叉" in macd_state(r) else None,
                     vr_c, ORANGE if lv else None, col(r.get("rs5"))])
    table(["股票", "收盘", "1日", "5日", "RSI14", "MACD", "量比", "关键位", "5日相对板块"],
          rows, widths=[2.4, 1.5, 1.4, 1.4, 1.2, 2.6, 1.2, 3.6, 1.7], colors=cols, size=8.5)
    para("量比 = 当日成交量 ÷ 过去 20 日平均成交量。5日相对板块 = 个股5日涨跌幅 − 对比ETF 5日涨跌幅，正数表示跑赢板块。"
         "颜色与 moomoo 一致：红涨绿跌。", color=GREY, size=8.5)

    heading("八、个股信号", 1)
    any_sig = False
    cmap = {"高": RED, "中": ORANGE, "提示": GREY}
    srows, scols = [], []
    for r in R:
        for lvl, txt in r["signals"]:
            srows.append([lvl, f"{r['name']} {r['code']}", txt])
            scols.append([cmap[lvl], None, None])
            any_sig = True
    if any_sig:
        order = {"高": 0, "中": 1, "提示": 2}
        z = sorted(zip(srows, scols), key=lambda t: order[t[0][0]])
        table(["级别", "股票", "信号"], [a for a, _ in z], widths=[1.4, 3.4, 12.2], colors=[b for _, b in z], size=9)
    else:
        para("今日自选股没有触发 RSI、MACD、放量或关键位信号。")

    heading("九、板块联动", 1)
    para("每个板块用对比ETF代表板块方向。「同涨同跌」看近60日个股与ETF日涨跌的相关系数："
         "0.7 以上高度同步（主要受板块和大环境驱动），0.4 以下走势独立（主要看个股自身消息）。", color=GREY, size=9)
    rows, cols = [], []
    for s in S["sectors"]:
        rows.append([s["name"], s["etf"], f1(s["e1"]), f1(s["e5"]), f1(s["s1"]),
                     f"{s['ups']}/{s['n']}", "—" if np.isnan(s["corr"]) else f"{s['corr']:.2f}",
                     "、".join(m["code"] for m in s["members"])])
        cols.append([None, None, col(s["e1"]), col(s["e5"]), col(s["s1"]), None, None, None])
    table(["板块", "ETF", "ETF 1日", "ETF 5日", "成员平均1日", "上涨家数", "平均相关", "成员"],
          rows, widths=[2.6, 1.3, 1.5, 1.5, 1.9, 1.5, 1.5, 5.2], colors=cols, size=9)
    lines_ = []
    for s in S["sectors"]:
        if np.isnan(s["e1"]):
            continue
        same = all(np.sign(m["c1"]) == np.sign(s["e1"]) for m in s["members"])
        diverge = [m for m in s["members"] if np.sign(m["c1"]) != np.sign(s["e1"]) and abs(m["c1"] - s["e1"]) >= 1.5]
        if same and s["n"] > 1:
            lines_.append(f"{s['name']}：成员与 {s['etf']} 今日同向（{'同涨' if s['e1'] > 0 else '同跌'}），板块效应明显。")
        elif diverge:
            lines_.append(f"{s['name']}：{'、'.join(m['name'] for m in diverge)} 与板块方向相反，留意个股消息。")
    groups = SEC.group([(r["sector"], r["c1"], r) for r in S["results"]])
    for a in SEC.anomalies(groups):
        lines_.insert(0, "⚡ 板块集体异动：" + SEC.anomaly_text(a))
    for t in lines_:
        para(t)

    heading("十、个股档案", 1)
    para("业务、优势、催化剂和利好/利空来自你在「个股设置.xlsx → 公司档案」里写的内容；财报日和新闻标题为自动获取（英文原标题，未做利好利空判断）。",
         color=GREY, size=9)
    for r in R:
        heading(f"{r['name']}（{r['code']}）　{r['last']:.2f}　{f1(r['c1'])}", 2)
        pr, ex = r["profile"], r["extra"]
        p = para(f"板块：{r['sector'] or '未分类'}（对比 {r['etf']}，{link_txt(r)}）　趋势：{trend_txt(r)}", size=9.5, space_after=2)
        for label in ("基本业务", "核心产品/优势", "未来动作/催化剂", "利好", "利空", "我的观点"):
            if pr.get(label):
                para(f"{label}：{pr[label]}", size=9.5, space_after=2)
        ed = ex.get("earnings")
        if ed:
            days = (ed - dt.date.today()).days
            warn = days >= 0 and days <= 14
            para(f"下次财报：{ed}（{'还有 ' + str(days) + ' 天' if days >= 0 else '已过'}）" +
                 ("　⚠ 两周内发财报，注意波动和指引" if warn else ""),
                 color=ORANGE if warn else None, size=9.5, space_after=2)
        if r["levels"]:
            para("关键位：" + "；".join(
                f"{l['type']} {l['price']:.2f}（{l['src']}{'，' + l['note'] if l['note'] else ''}）距 {l['dist']:+.1f}%"
                for l in r["levels"]), size=9.5, space_after=2)
        for n in ex.get("news", []):
            para(f"· {n['date']} {n['title']}" + (f"（{n['src']}）" if n["src"] else ""), color=GREY, size=9, space_after=2)
        if not any(pr.get(k) for k in ("基本业务", "核心产品/优势", "未来动作/催化剂", "利好", "利空")):
            para("（公司档案还没填写）", color=GREY, size=9, space_after=2)


def source_rows(S):
    from collections import Counter
    cnt = Counter(S["ksrc"].values())
    return "，".join(f"{k} {v} 个" for k, v in cnt.items())
