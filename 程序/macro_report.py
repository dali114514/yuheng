# -*- coding: utf-8 -*-
"""
玉衡5.0：自动拉取 VIX / VXN / QQQ / 布伦特 / US10Y / DXY，
按工作流规则分析，生成 Word 报告到项目文件夹的「报告」。

数据来源：Yahoo Finance（通过 yfinance），日线。
关键位、异动阈值都在项目文件夹的「设置.json」里改。
"""
import json
import os
import sys
import subprocess
import tempfile
import traceback
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import logging
import warnings

warnings.filterwarnings("ignore")                       # yfinance / pandas 的弃用提示
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # ETF 没有财报日时的 404 提示

BASE = Path(__file__).resolve().parent.parent          # 项目文件夹（桌面上的「玉衡5.0」）
REPORT_DIR = BASE / "报告"
CONFIG_PATH = BASE / "设置.json"
TV_DIR = BASE / "TradingView数据"

# risk = +1：上涨偏避险；risk = -1：上涨偏风险偏好
# tv：TradingView 导出文件名里用来识别品种的关键词
INDICATORS = [
    dict(key="VIX",   name="VIX 恐慌指数",     ticker="^VIX",     tv=["VIX"],                   tvsym="CBOE:VIX",  risk=+1, fmt="{:.2f}"),
    dict(key="VXN",   name="VXN 纳指波动率",   ticker="^VXN",     tv=["VXN"],                   tvsym="CBOE:VXN",  risk=+1, fmt="{:.2f}"),
    dict(key="QQQ",   name="QQQ 纳指100 ETF",  ticker="QQQ",      tv=["QQQ"],                   tvsym="NASDAQ:QQQ", risk=-1, fmt="{:.2f}"),
    dict(key="BRENT", name="布伦特原油",       ticker="BZ=F",     tv=["BRN1", "BRN", "UKOIL", "BRENT", "布伦特"], tvsym="ICE:BRN1!", risk=+1, fmt="{:.2f}"),
    dict(key="US10Y", name="美债10年期收益率", ticker="^TNX",     tv=["US10Y"],                 tvsym="TVC:US10Y", risk=+1, fmt="{:.3f}%"),
    dict(key="DXY",   name="美元指数",         ticker="DX-Y.NYB", tv=["DXY"],                   tvsym="TVC:DXY",   risk=+1, fmt="{:.2f}"),
]
IND = {d["key"]: d for d in INDICATORS}

DEFAULT_CONFIG = {
    "说明": "改完保存即可，下次运行生效。关键位填 0 表示不启用。",
    "临近预警距离_百分比": 5,
    "关键位": {
        "VIX_紧张线": 20,
        "VIX_恐慌线": 30,
        "VXN_紧张线": 25,
        "VXN_恐慌线": 35,
        "VXN减VIX_警戒差值": 8,
        "US10Y_上方关键位": 4.5,
        "US10Y_下方关键位": 0,
        "DXY_上方关键位": 105,
        "布伦特_上方关键位": 90,
        "布伦特_下方关键位": 0,
        "QQQ_下方支撑": 0
    },
    "单日异动": {
        "VIX_涨幅_百分比": 15,
        "QQQ_跌幅_百分比": 2,
        "US10Y_变动_bp": 10,
        "DXY_涨幅_百分比": 0.8,
        "布伦特_变动_百分比": 4
    },
    "共振_同向指标数量": 4,
    "TradingView_CSV_有效天数": 3,
    "自选股": {
        "启用": True,
        "说明": "个股关键位和参数在「个股设置.xlsx」里改"
    },
    "moomoo": {
        "启用": True,
        "OpenD地址": "127.0.0.1",
        "OpenD端口": 11111,
        "自选分组": "全部",
        "读取价格提醒作为关键位": True
    },
    "生成后自动打开报告": True,
    "网站分享": {
        "启用": False,
        "说明": "填好 Cloudflare 的三项后把「启用」改成 true。API令牌只放在你自己电脑上，不要发给别人。",
        "Cloudflare账户ID": "",
        "Cloudflare_API令牌": "",
        "网站项目名": "macro-fengxiangbiao",
        "保留报告份数": 60,
        "群推送": {
            "类型": "",
            "Webhook地址": "",
            "钉钉加签密钥": "",
            "推送报告摘要": True,
            "推送盘中提醒": True,
            "说明": "类型填：企业微信 / 飞书 / 钉钉 / Slack / Discord 之一；不需要推送就留空。"
        }
    },
    "Discord机器人": {
        "启用": False,
        "说明": "机器人令牌只放在你自己电脑上，不要发给别人。允许的频道ID 留空 = 所有频道都响应；允许的身份组 留空 = 所有人都能用。",
        "机器人令牌": "",
        "允许的频道ID": [],
        "允许的身份组": [],
        "自动快讯": {
            "启用": True,
            "时间": "01:00",
            "时区": "Australia/Sydney",
            "只在美股交易日": True,
            "说明": "每天到点自动把快讯发到「允许的频道ID」里的第一个频道；机器人窗口要开着、电脑不能睡眠。"
        }
    }
}


# ---------------------------------------------------------------- 配置

def _ver():
    """当前版本号（项目根目录 VERSION）。"""
    try:
        import version
        return version.get()
    except Exception:
        return "?"

def load_config():
    def merge(default, user):
        out = dict(default)
        for k, v in (user or {}).items():
            if isinstance(v, dict) and isinstance(default.get(k), dict):
                out[k] = merge(default[k], v)
            else:
                out[k] = v
        return out

    user, broken = {}, False
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            broken = True
            print("⚠ 设置.json 格式有误，本次使用默认设置（文件未改动）。")
    cfg = merge(DEFAULT_CONFIG, user)
    # 文件不存在，或旧版本缺少新设置项时，写回补全后的设置（格式错误时不覆盖）
    if not broken and (not CONFIG_PATH.exists() or cfg != user):
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- 数据：Yahoo
def fetch_yahoo():
    try:
        import yfinance as yf
    except Exception as e:
        print(f"  ✗ yfinance 不可用：{e}")
        return {d["key"]: None for d in INDICATORS}
    data = {}
    for d in INDICATORS:
        try:
            hist = yf.Ticker(d["ticker"]).history(period="1y", interval="1d", auto_adjust=False)
            s = hist["Close"].dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            s = s[~s.index.duplicated(keep="last")]
            if d["key"] == "US10Y" and len(s) and s.median() > 20:   # 兼容 ×10 报价
                s = s / 10
            if len(s) < 25:
                raise ValueError("数据太少")
            data[d["key"]] = s
        except Exception as e:
            print(f"  · Yahoo {d['name']} 获取失败：{str(e)[:80]}")
            data[d["key"]] = None
    return data


# ---------------------------------------------------------------- 数据：TradingView CSV
TV_README = """TradingView 数据文件夹

把 TradingView 导出的 CSV 放在这里，程序会优先使用它们（比 Yahoo 更贴近你看盘的数值）。
没有放、或者文件超过「设置.json」里的有效天数（默认 3 天），程序会自动改用 Yahoo 数据。

【导出方法】
1. 在 TradingView 打开品种，周期选「1 天」（日线）。
2. 点图表顶部布局名称旁的箭头（或右上角菜单）→「导出图表数据…」。
3. 时间格式选 UNIX 或 ISO 都可以，点「导出」。
4. 把下载的 CSV 拖进这个文件夹。

需要的 6 个品种：
  CBOE:VIX   CBOE:VXN   NASDAQ:QQQ   ICE:BRN1!   TVC:US10Y   TVC:DXY

文件名保持 TradingView 默认的即可（里面带品种代码，程序靠它识别）。
如果自己改名，文件名里包含 VIX / VXN / QQQ / BRN1 / US10Y / DXY 即可。
同一品种有多个文件时，用最新导出的那个；旧文件可以留着，不影响。

建议导出时图表上显示至少 1 年的K线，这样「1年分位」才准确。
「导出图表数据」需要 TradingView 付费账户。
"""


def _tokens(name):
    import re
    return set(re.split(r"[^0-9A-Z一-鿿]+", name.upper()))


def _match_tv(path):
    """根据文件名判断是哪个品种。"""
    toks = _tokens(path.stem)
    for d in INDICATORS:
        for kw in d["tv"]:
            if kw.upper() in toks or (len(kw) > 3 and any(t.startswith(kw.upper()) for t in toks)):
                return d["key"]
    return None


def read_tv_csv(path):
    df = pd.read_csv(path)
    cols = {c.strip().lower(): c for c in df.columns}
    tcol = cols.get("time") or cols.get("date") or list(df.columns)[0]
    ccol = cols.get("close")
    if ccol is None:
        raise ValueError("找不到 close 列")
    t = df[tcol]
    if pd.api.types.is_numeric_dtype(t):
        unit = "ms" if t.abs().median() > 1e11 else "s"   # TradingView 为秒，兼容毫秒
        ts = pd.to_datetime(t, unit=unit, utc=True)
    else:
        ts = pd.to_datetime(t, utc=True, errors="coerce")
    s = pd.Series(pd.to_numeric(df[ccol], errors="coerce").values, index=ts).dropna()
    s = s[s.index.notna()].sort_index()
    if len(s) < 2:
        raise ValueError("数据太少")
    step = pd.Series(s.index).diff().median()
    if step >= pd.Timedelta(hours=20):
        # 日线：K线时间戳是交易时段开始（UTC），+6 小时后取日期可统一对齐到交易日
        idx = (s.index + pd.Timedelta(hours=6)).tz_localize(None).normalize()
        s = pd.Series(s.values, index=idx)
    else:
        # 盘中周期：按纽约时间合成日线收盘
        idx = s.index.tz_convert("America/New_York").tz_localize(None).normalize()
        s = pd.Series(s.values, index=idx)
    s = s[~s.index.duplicated(keep="last")]
    return s


def load_tradingview(cfg):
    TV_DIR.mkdir(parents=True, exist_ok=True)
    readme = TV_DIR / "导出方法.txt"
    if not readme.exists():
        readme.write_text(TV_README, encoding="utf-8")
    max_age = float(cfg.get("TradingView_CSV_有效天数", 3))
    now = dt.datetime.now().timestamp()
    best = {}   # key -> (mtime, path)
    for p in TV_DIR.glob("*.csv"):
        key = _match_tv(p)
        if key is None:
            continue
        m = p.stat().st_mtime
        if key not in best or m > best[key][0]:
            best[key] = (m, p)
    out = {}
    for key, (m, p) in best.items():
        age = (now - m) / 86400
        try:
            s = read_tv_csv(p)
            if key == "US10Y" and s.median() > 20:
                s = s / 10
            out[key] = dict(series=s, path=p, age=age, fresh=age <= max_age,
                            exported=dt.datetime.fromtimestamp(m))
        except Exception as e:
            print(f"  · TradingView 文件 {p.name} 读取失败：{e}")
    return out


def fetch_data(cfg):
    """TradingView CSV 优先（未过期且够长），否则 Yahoo；Yahoo 失败时退回过期 CSV。"""
    tv = load_tradingview(cfg)
    yahoo = fetch_yahoo()
    data, src = {}, {}
    for d in INDICATORS:
        k = d["key"]
        t, y = tv.get(k), yahoo.get(k)
        tv_ok = t is not None and len(t["series"]) >= 25
        if tv_ok and t["fresh"]:
            data[k] = t["series"]
            src[k] = dict(kind="TradingView", label=f"TradingView {d['tvsym']}",
                          note=f"{t['path'].name}，导出于 {t['exported']:%m-%d %H:%M}")
        elif y is not None:
            data[k] = y
            if tv_ok:
                note = f"TradingView 文件已过期（{t['age']:.0f} 天前导出），改用 Yahoo"
            elif t is not None:
                note = "TradingView 文件K线太少（少于25根），改用 Yahoo"
            else:
                note = "未提供 TradingView 文件"
            src[k] = dict(kind="Yahoo", label=f"Yahoo {d['ticker']}", note=note)
        elif tv_ok:
            data[k] = t["series"]
            src[k] = dict(kind="TradingView", label=f"TradingView {d['tvsym']}",
                          note=f"Yahoo 获取失败，使用 {t['age']:.0f} 天前导出的旧文件")
        else:
            data[k] = None
            src[k] = dict(kind="缺失", label="缺失", note="两个来源都没有拿到数据")
        s = data[k]
        mark = "✓" if s is not None else "✗"
        last = f"最新 {s.iloc[-1]:.2f}（{s.index[-1].date()}）" if s is not None else ""
        print(f"  {mark} {d['name']:<14} [{src[k]['kind']}] {last}")
    return data, src


def fetch_fake():
    """测试用模拟数据（环境变量 MACRO_FAKE=1）。"""
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=252)
    base = dict(VIX=17, VXN=22, QQQ=480, BRENT=76, US10Y=4.2, DXY=101)
    vol = dict(VIX=0.06, VXN=0.055, QQQ=0.012, BRENT=0.018, US10Y=0.012, DXY=0.004)
    data = {}
    for k, b in base.items():
        r = rng.normal(0, vol[k], len(idx))
        data[k] = pd.Series(b * np.exp(np.cumsum(r) - np.cumsum(r).mean() * 0), index=idx)
    # 制造一个避险日：VIX 跳涨、QQQ 大跌、利率美元上行
    data["VIX"].iloc[-1] = data["VIX"].iloc[-2] * 1.22
    data["VXN"].iloc[-1] = data["VXN"].iloc[-2] * 1.25
    data["QQQ"].iloc[-1] = data["QQQ"].iloc[-2] * 0.973
    data["US10Y"].iloc[-1] = data["US10Y"].iloc[-2] + 0.12
    data["DXY"].iloc[-1] = data["DXY"].iloc[-2] * 1.009
    return data


# ---------------------------------------------------------------- 计算
def stats(key, s):
    bp = key == "US10Y"

    def chg(n):
        if len(s) <= n:
            return np.nan
        a, b = s.iloc[-1], s.iloc[-1 - n]
        return (a - b) * 100 if bp else (a / b - 1) * 100

    last = float(s.iloc[-1])
    ma20 = float(s.tail(20).mean())
    ma50 = float(s.tail(50).mean())
    return dict(
        last=last, prev=float(s.iloc[-2]), date=s.index[-1].date(),
        c1=chg(1), c5=chg(5), c20=chg(20),
        ma20=ma20, ma50=ma50,
        vs_ma20=(last / ma20 - 1) * 100,
        pctile=float((s <= last).mean() * 100),
        hi=float(s.max()), lo=float(s.min()),
        unit="bp" if bp else "%",
    )


def sgn(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


def vix_regime(v):
    if v < 15:
        return "平静", "市场平静，偏乐观"
    if v < 20:
        return "正常", "情绪正常"
    if v < 30:
        return "紧张", "情绪紧张，资金开始避险"
    return "恐慌", "恐慌区间，常伴随集中抛售"


def zone_up(x, lvl, near):
    if not lvl:
        return 0
    if x >= lvl:
        return 2
    return 1 if x >= lvl * (1 - near / 100) else 0


def zone_dn(x, lvl, near):
    if not lvl:
        return 0
    if x <= lvl:
        return 2
    return 1 if x <= lvl * (1 + near / 100) else 0


def analyze(data, cfg):
    near = float(cfg["临近预警距离_百分比"])
    L = cfg["关键位"]
    J = cfg["单日异动"]
    st = {k: stats(k, s) for k, s in data.items() if s is not None}
    alerts = []   # (级别, 文本)

    def add(level, text):
        alerts.append((level, text))

    # --- 关键位 ---
    level_rules = [
        ("VIX", "恐慌线", L["VIX_恐慌线"], "up"),
        ("VIX", "紧张线", L["VIX_紧张线"], "up"),
        ("VXN", "恐慌线", L["VXN_恐慌线"], "up"),
        ("VXN", "紧张线", L["VXN_紧张线"], "up"),
        ("US10Y", "上方关键位", L["US10Y_上方关键位"], "up"),
        ("US10Y", "下方关键位", L["US10Y_下方关键位"], "dn"),
        ("DXY", "上方关键位", L["DXY_上方关键位"], "up"),
        ("BRENT", "上方关键位", L["布伦特_上方关键位"], "up"),
        ("BRENT", "下方关键位", L["布伦特_下方关键位"], "dn"),
        ("QQQ", "支撑", L["QQQ_下方支撑"], "dn"),
    ]
    done = set()
    level_status = {}
    for key, label, lvl, way in level_rules:
        # 同一指标同一方向只报最严重的一条（如 VIX 恐慌线优先于紧张线）；上下方向各自独立
        if key not in st or not lvl or (key, way) in done:
            continue
        s = st[key]
        z = zone_up(s["last"], lvl, near) if way == "up" else zone_dn(s["last"], lvl, near)
        zp = zone_up(s["prev"], lvl, near) if way == "up" else zone_dn(s["prev"], lvl, near)
        if z == 0:
            continue
        name = IND[key]["name"]
        lv = IND[key]["fmt"].format(lvl)
        cur = IND[key]["fmt"].format(s["last"])
        verb = "突破" if way == "up" else "跌破"
        if z == 2:
            new = f"（今日新{verb}）" if zp < 2 else ""
            sev = "高" if label in ("恐慌线", "支撑") else "中"
            add(sev, f"{name} 已{verb}{label} {lv}，现 {cur}{new}")
            txt = f"已{verb}{label}"
        else:
            dist = (f"{abs(s['last'] - lvl) * 100:.0f}bp" if key == "US10Y"
                    else f"{abs(s['last'] / lvl - 1) * 100:.1f}%")
            add("提示", f"{name} 临近{label} {lv}，现 {cur}，距离 {dist}")
            txt = f"临近{label}"
        level_status[key] = level_status[key] + "；" + txt if key in level_status else txt
        done.add((key, way))

    # --- 单日异动 ---
    def jump(key, cond, text, sev="中"):
        if key in st and cond(st[key]):
            add(sev, text.format(**st[key]))

    jump("VIX", lambda s: s["c1"] >= J["VIX_涨幅_百分比"], "VIX 单日跳涨 {c1:.1f}%", "高")
    jump("QQQ", lambda s: s["c1"] <= -J["QQQ_跌幅_百分比"], "QQQ 单日下跌 {c1:.2f}%", "高")
    jump("US10Y", lambda s: abs(s["c1"]) >= J["US10Y_变动_bp"], "US10Y 单日变动 {c1:+.1f}bp")
    jump("DXY", lambda s: s["c1"] >= J["DXY_涨幅_百分比"], "美元指数单日上涨 {c1:.2f}%")
    jump("BRENT", lambda s: abs(s["c1"]) >= J["布伦特_变动_百分比"], "布伦特单日变动 {c1:+.2f}%")

    # --- VXN − VIX 差值 ---
    spread = None
    if "VIX" in st and "VXN" in st:
        sp = (data["VXN"] - data["VIX"]).dropna()
        spread = dict(last=float(sp.iloc[-1]), mean60=float(sp.tail(60).mean()))
        if spread["last"] >= L["VXN减VIX_警戒差值"]:
            add("中", f"VXN − VIX 差值 {spread['last']:.2f}（60日均值 {spread['mean60']:.2f}），恐慌集中在科技股")

    # --- 共振（5 个指标：VIX、US10Y、DXY、布伦特、QQQ）---
    reso_keys = [k for k in ("VIX", "US10Y", "DXY", "BRENT", "QQQ") if k in st]

    def count(field):
        off = sum(1 for k in reso_keys if sgn(st[k][field]) * IND[k]["risk"] > 0)
        on = sum(1 for k in reso_keys if sgn(st[k][field]) * IND[k]["risk"] < 0)
        return off, on

    off1, on1 = count("c1")
    off5, on5 = count("c5")
    need = int(cfg["共振_同向指标数量"])
    if off1 >= need:
        add("高", f"避险共振：{off1}/{len(reso_keys)} 个指标今日同时指向避险")
    if on1 >= need:
        add("提示", f"风险偏好共振：{on1}/{len(reso_keys)} 个指标今日同时指向风险偏好")

    # --- 传导链 ---
    links = [
        ("BRENT", "US10Y", +1, "油价 → 利率", "通胀预期推动利率"),
        ("US10Y", "DXY",   +1, "利率 → 美元", "利差吸引资金流入美元"),
        ("US10Y", "QQQ",   -1, "利率 → 科技股", "利率上行压制估值"),
        ("DXY",   "QQQ",   -1, "美元 → 科技股", "美元走强收紧流动性"),
        ("QQQ",   "VIX",   -1, "价格 → 大盘情绪", "下跌推升恐慌"),
        ("QQQ",   "VXN",   -1, "价格 → 科技情绪", "下跌推升科技股恐慌"),
    ]
    chain = []
    for a, b, rel, title, why in links:
        if a not in st or b not in st:
            continue
        row = dict(title=title, why=why)
        for f in ("c1", "c5"):
            sa, sb = sgn(st[a][f]), sgn(st[b][f])
            row[f] = None if sa == 0 or sb == 0 else (sa * sb == rel)
        chain.append(row)

    # --- 综合判断 ---
    regime, regime_desc = vix_regime(st["VIX"]["last"]) if "VIX" in st else ("未知", "")
    regime_pts = {"平静": -1, "正常": 0, "紧张": 1, "恐慌": 2}.get(regime, 0)
    score = (off1 - on1) + (off5 - on5) + regime_pts
    if score >= 6:
        verdict = "明显避险"
    elif score >= 3:
        verdict = "偏避险"
    elif score > -3:
        verdict = "分歧 / 中性"
    elif score > -6:
        verdict = "偏风险偏好"
    else:
        verdict = "风险偏好强"

    order = {"高": 0, "中": 1, "提示": 2}
    alerts.sort(key=lambda a: order[a[0]])

    return dict(st=st, alerts=alerts, level_status=level_status, spread=spread,
                off1=off1, on1=on1, off5=off5, on5=on5, n=len(reso_keys),
                chain=chain, regime=regime, regime_desc=regime_desc,
                score=score, verdict=verdict, cfg=cfg)


# ---------------------------------------------------------------- 文字分析
def f(key, v):
    return IND[key]["fmt"].format(v)


def trend_word(s):
    if s["last"] > s["ma20"] and s["ma20"] > s["ma50"]:
        return "处于上升趋势（高于20日均线，且20日线在50日线之上）"
    if s["last"] < s["ma20"] and s["ma20"] < s["ma50"]:
        return "处于下降趋势（低于20日均线，且20日线在50日线之下）"
    return "趋势不明确（均线交织）"


def chg_txt(s, field="c1"):
    v = s[field]
    if np.isnan(v):
        return "—"
    return f"{v:+.1f}bp" if s["unit"] == "bp" else f"{v:+.2f}%"


def range_text(key, x, lo, hi):
    """描述当前值相对上下关键位的位置，返回以“，”结尾的短句（无关键位时为空）。"""
    lo, hi = (lo or 0), (hi or 0)

    def fv(v):
        return f(key, v)

    def dist(a, b):
        return f"{abs(a - b) * 100:.0f}bp" if key == "US10Y" else f"{abs(a / b - 1) * 100:.1f}%"

    if lo and hi:
        if x > hi:
            return f"已在上方关键位 {fv(hi)} 之上（高出 {dist(x, hi)}），"
        if x < lo:
            return f"已跌破下方关键位 {fv(lo)}（低于 {dist(x, lo)}），"
        pos = (x - lo) / (hi - lo) * 100 if hi > lo else 50
        return (f"处于关键区间 {fv(lo)}–{fv(hi)} 内（区间 {pos:.0f}% 位置，"
                f"距上沿 {dist(x, hi)}、距下沿 {dist(x, lo)}），")
    if hi:
        return f"位于上方关键位 {fv(hi)} {'之上' if x >= hi else '之下'}，"
    if lo:
        return f"位于下方关键位 {fv(lo)} {'之上' if x >= lo else '之下'}，"
    return ""


def narrative(A):
    st = A["st"]
    L = A["cfg"]["关键位"]
    out = {}

    # 第 1 步：情绪
    p = []
    if "VIX" in st:
        v = st["VIX"]
        p.append(f"VIX 最新 {f('VIX', v['last'])}，单日 {chg_txt(v)}，5日 {chg_txt(v, 'c5')}，"
                 f"处于「{A['regime']}」区间：{A['regime_desc']}。在过去一年中处于 {v['pctile']:.0f}% 分位。")
        if v["c1"] >= 10:
            p.append("单日涨幅较大，情绪变化速度比绝对水平更值得警惕。")
    if "VXN" in st:
        x = st["VXN"]
        p.append(f"VXN 最新 {f('VXN', x['last'])}，单日 {chg_txt(x)}。")
    if A["spread"]:
        sp = A["spread"]
        diff = sp["last"] - sp["mean60"]
        if diff > 1.5:
            p.append(f"VXN 比 VIX 高 {sp['last']:.2f}，明显高于60日均值 {sp['mean60']:.2f}：恐慌更集中在科技股。")
        elif diff < -1.5:
            p.append(f"VXN 与 VIX 差值 {sp['last']:.2f}，低于60日均值 {sp['mean60']:.2f}：科技股情绪相对大盘更稳。")
        else:
            p.append(f"VXN 与 VIX 差值 {sp['last']:.2f}，接近60日均值，科技股与大盘情绪同步。")
    out["情绪"] = p

    # 第 2 步：利率与资金
    p = []
    if "US10Y" in st:
        y = st["US10Y"]
        rng = range_text("US10Y", y["last"], L.get("US10Y_下方关键位"), L.get("US10Y_上方关键位"))
        p.append(f"US10Y 最新 {f('US10Y', y['last'])}，单日 {chg_txt(y)}，5日 {chg_txt(y, 'c5')}，"
                 f"{rng}{trend_word(y)}。")
        if y["c5"] > 0:
            p.append("近5日收益率上行，对高估值科技股构成估值压力。")
        elif y["c5"] < 0:
            p.append("近5日收益率下行，估值压力缓解；但若同时股市下跌，可能是衰退担忧带来的避险买债。")
    if "DXY" in st:
        d = st["DXY"]
        p.append(f"美元指数最新 {f('DXY', d['last'])}，单日 {chg_txt(d)}，5日 {chg_txt(d, 'c5')}，{trend_word(d)}。")
        if d["c5"] > 0 and "US10Y" in st and st["US10Y"]["c5"] > 0:
            p.append("利率与美元同步上行，资金面在收紧。")
        elif d["c5"] < 0 and "US10Y" in st and st["US10Y"]["c5"] < 0:
            p.append("利率与美元同步回落，流动性环境偏宽松。")
    out["利率与资金"] = p

    # 第 3 步：通胀
    p = []
    if "BRENT" in st:
        b = st["BRENT"]
        rng = range_text("BRENT", b["last"], L.get("布伦特_下方关键位"), L.get("布伦特_上方关键位"))
        p.append(f"布伦特最新 {f('BRENT', b['last'])}，单日 {chg_txt(b)}，5日 {chg_txt(b, 'c5')}，"
                 f"20日 {chg_txt(b, 'c20')}，{rng}{trend_word(b)}。")
        if "US10Y" in st:
            y5, b5 = sgn(st["US10Y"]["c5"]), sgn(b["c5"])
            if b5 > 0 and y5 > 0:
                p.append("油价与利率同涨：利率上行有通胀预期在推动。")
            elif b5 > 0 and y5 < 0:
                p.append("油价涨但利率跌：利率变化不是通胀驱动，更可能是避险或增长担忧。")
            elif b5 < 0 and y5 > 0:
                p.append("油价跌但利率涨：利率上行来自通胀以外的因素（如美联储表态、供给），需另外关注。")
            elif b5 < 0 and y5 < 0:
                p.append("油价与利率同跌：通胀压力缓解，有利于降息预期。")
        p.append("注意区分：需求旺盛带来的油价上涨与供给冲击（减产、地缘）带来的上涨，对股市影响不同，需结合新闻判断。")
    out["通胀"] = p

    # 第 4 步：价格确认
    p = []
    if "QQQ" in st:
        q = st["QQQ"]
        rng_pos = (q["last"] - q["lo"]) / (q["hi"] - q["lo"]) * 100 if q["hi"] > q["lo"] else 50
        p.append(f"QQQ 最新 {f('QQQ', q['last'])}，单日 {chg_txt(q)}，5日 {chg_txt(q, 'c5')}，20日 {chg_txt(q, 'c20')}，"
                 f"{trend_word(q)}；处于一年高低区间的 {rng_pos:.0f}% 位置。")
        macro_off = A["off5"] - A["on5"]
        if macro_off >= 2 and q["c5"] >= 0:
            p.append("宏观指标偏避险，但 QQQ 价格尚未下跌：价格还没确认，可能存在滞后或分歧。")
        elif macro_off <= -2 and q["c5"] <= 0:
            p.append("宏观指标偏友好，但 QQQ 未跟涨：价格尚未确认，需观察是否有个股/行业层面的拖累。")
        elif macro_off >= 2:
            p.append("宏观偏避险，QQQ 同步走弱：价格已确认避险方向。")
        elif macro_off <= -2:
            p.append("宏观偏友好，QQQ 同步走强：价格已确认风险偏好方向。")
        else:
            p.append("宏观信号不集中，QQQ 走势主要看自身驱动。")
    out["价格确认"] = p
    return out


def summary_points(A):
    pts = []
    st = A["st"]
    pts.append(f"今日 {A['n']} 个传导指标中，{A['off1']} 个指向避险、{A['on1']} 个指向风险偏好；"
               f"近5日为 {A['off5']} 比 {A['on5']}。")
    if "VIX" in st:
        pts.append(f"VIX {f('VIX', st['VIX']['last'])}（{A['regime']}），"
                   f"US10Y {f('US10Y', st['US10Y']['last']) if 'US10Y' in st else '—'}，"
                   f"DXY {f('DXY', st['DXY']['last']) if 'DXY' in st else '—'}。")
    ok = [c for c in A["chain"] if c["c5"] is True]
    pts.append(f"传导链（5日）{len(ok)}/{len(A['chain'])} 个环节符合典型方向"
               + ("，链条通畅，信号可信度较高。" if len(ok) >= 4 else "，链条不完整，信号存在分歧。"))
    high = [a for a in A["alerts"] if a[0] == "高"]
    pts.append(f"触发 {len(A['alerts'])} 条警报，其中高级别 {len(high)} 条。" if A["alerts"] else "今日无警报触发。")
    if A["verdict"] == "分歧 / 中性":
        pts.append("按你的工作流规则：指标方向矛盾时，适合降低仓位、等待方向明确。")
    return pts


# ---------------------------------------------------------------- 图表
def make_chart(data, cfg, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    L = cfg["关键位"]
    levels = {
        "VIX": [L["VIX_紧张线"], L["VIX_恐慌线"]],
        "VXN": [L["VXN_紧张线"], L["VXN_恐慌线"]],
        "QQQ": [L["QQQ_下方支撑"]],
        "BRENT": [L["布伦特_上方关键位"], L.get("布伦特_下方关键位")],
        "US10Y": [L["US10Y_上方关键位"], L.get("US10Y_下方关键位")],
        "DXY": [L["DXY_上方关键位"]],
    }
    titles = dict(VIX="VIX", VXN="VXN", QQQ="QQQ", BRENT="Brent", US10Y="US10Y (%)", DXY="DXY")
    fig, axes = plt.subplots(3, 2, figsize=(10, 9))
    for ax, d in zip(axes.flat, INDICATORS):
        k = d["key"]
        s = data.get(k)
        ax.set_title(titles[k], fontsize=11, loc="left")
        if s is None:
            ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
            continue
        s = s.tail(126)
        ax.plot(s.index, s.values, color="#1f4e79", lw=1.4)
        ax.plot(s.index, s.rolling(20).mean(), color="#e69138", lw=1, alpha=0.9, label="MA20")
        lo, hi = s.min(), s.max()
        for lv in levels[k]:
            if lv and lo * 0.8 <= lv <= hi * 1.2:
                ax.axhline(lv, color="#c00000", ls="--", lw=0.9, alpha=0.7)
        ax.grid(alpha=0.25)
        ax.tick_params(labelsize=8)
        for lab in ax.get_xticklabels():
            lab.set_rotation(30)
    axes.flat[0].legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- Word
def build_doc(A, data, src, out_path, extra=None, stock_src="", top_lines=None):
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn

    FONT = "PingFang SC"
    RED, GREEN, GREY, ORANGE = RGBColor(0xC0, 0, 0), RGBColor(0, 0x80, 0x3C), RGBColor(0x66, 0x66, 0x66), RGBColor(0xB4, 0x5F, 0x06)

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Cm(2))
    sec.top_margin = sec.bottom_margin = Cm(2)

    def set_font(style_name, size=None, bold=None, color=None):
        stl = doc.styles[style_name]
        stl.font.name = FONT
        stl.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        if size:
            stl.font.size = Pt(size)
        if bold is not None:
            stl.font.bold = bold
        if color is not None:
            stl.font.color.rgb = color

    set_font("Normal", 10.5)
    for h, sz in (("Title", 22), ("Heading 1", 15), ("Heading 2", 12.5)):
        set_font(h, sz, True, RGBColor(0x1F, 0x3A, 0x5F))

    def run_font(r):
        r.font.name = FONT
        r._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), FONT)
        return r

    def para(text="", bold=False, color=None, size=None, style=None, space_after=None):
        p = doc.add_paragraph(style=style)
        if space_after is not None:
            p.paragraph_format.space_after = Pt(space_after)
        if text:
            r = run_font(p.add_run(text))
            r.bold = bold
            if color is not None:
                r.font.color.rgb = color
            if size:
                r.font.size = Pt(size)
        return p

    def table(headers, rows, widths=None, colors=None, size=9.5):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Light Grid Accent 1"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, h in enumerate(headers):
            c = t.rows[0].cells[i]
            c.text = ""
            hr = run_font(c.paragraphs[0].add_run(h))
            hr.bold = True
            if size < 9.5:
                hr.font.size = Pt(size)
        for ri, row in enumerate(rows):
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = ""
                r = run_font(cells[i].paragraphs[0].add_run(str(v)))
                r.font.size = Pt(size)
                if colors and colors[ri] and colors[ri][i] is not None:
                    r.font.color.rgb = colors[ri][i]
        if widths:
            t.autofit = False
            for i, w in enumerate(widths):
                t.columns[i].width = Cm(w)
            for row in t.rows:
                for i, w in enumerate(widths):
                    row.cells[i].width = Cm(w)
        return t

    st = A["st"]
    last_date = max(s["date"] for s in st.values())
    now = dt.datetime.now()

    # 标题
    tp = doc.add_paragraph(style="Title")
    run_font(tp.add_run(f"玉衡5.0 日报 · v{_ver()}"))
    para(f"数据截至 {last_date}（最近交易日收盘 / 盘中最新）　·　生成于 {now:%Y-%m-%d %H:%M}", color=GREY, size=9)
    if now.date() > last_date and now.weekday() >= 5:
        para("今天是周末，数据为上一个交易日。", color=GREY, size=9)
    kinds = [src[d["key"]]["kind"] for d in INDICATORS]
    parts = [f"{k} {kinds.count(k)} 个" for k in ("TradingView", "Yahoo", "模拟", "缺失") if kinds.count(k)]
    para("数据来源：" + "，".join(parts) + "（明细见文末「数据来源」）", color=GREY, size=9)

    # 1. 结论
    doc.add_heading("一、结论速览", level=1)
    vcolor = RED if "避险" in A["verdict"] else (GREEN if "风险偏好" in A["verdict"] else ORANGE)
    p = para()
    run_font(p.add_run("市场环境：")).bold = True
    r = run_font(p.add_run(A["verdict"]))
    r.bold, r.font.color.rgb, r.font.size = True, vcolor, Pt(14)
    run_font(p.add_run(f"　（综合分 {A['score']:+d}，正数偏避险，负数偏风险偏好）")).font.color.rgb = GREY
    for t_ in summary_points(A) + list(top_lines or []):
        para(t_, style="List Bullet")

    # 2. 警报
    doc.add_heading("二、警报", level=1)
    if A["alerts"]:
        cmap = {"高": RED, "中": ORANGE, "提示": GREY}
        table(["级别", "内容"], [[a[0], a[1]] for a in A["alerts"]], widths=[2, 15],
              colors=[[cmap[a[0]], None] for a in A["alerts"]])
    else:
        para("今日没有指标临近或突破关键位，也没有单日异动和共振。")

    # 3. 概览表
    doc.add_heading("三、六指标概览", level=1)
    rows, cols = [], []
    for d in INDICATORS:
        k = d["key"]
        if k not in st:
            rows.append([d["name"], "数据缺失", "", "", "", "", "", ""])
            cols.append(None)
            continue
        s = st[k]
        state = A["level_status"].get(k, "正常")
        rows.append([d["name"], f(k, s["last"]), chg_txt(s), chg_txt(s, "c5"), chg_txt(s, "c20"),
                     f"{s['vs_ma20']:+.1f}%", f"{s['pctile']:.0f}%", state])
        day_risk = sgn(s["c1"]) * d["risk"]
        c_day = RED if day_risk > 0 else (GREEN if day_risk < 0 else None)
        c_state = RED if "突破" in state else (ORANGE if "临近" in state else None)
        cols.append([None, None, c_day, None, None, None, None, c_state])
    table(["指标", "最新", "1日", "5日", "20日", "相对MA20", "1年分位", "状态"], rows,
          widths=[3.6, 1.9, 1.7, 1.7, 1.7, 1.9, 1.6, 2.6], colors=cols)
    para("「1日」颜色：红 = 当日变动指向避险，绿 = 指向风险偏好（例如 VIX 上涨记红，QQQ 上涨记绿）。",
         color=GREY, size=8.5)

    # 4. 看盘顺序
    doc.add_heading("四、按看盘顺序分析", level=1)
    nar = narrative(A)
    titles = [("情绪", "第 1 步　情绪：VIX / VXN"),
              ("利率与资金", "第 2 步　原因：US10Y / 美元指数"),
              ("通胀", "第 3 步　通胀：布伦特原油"),
              ("价格确认", "第 4 步　结果：QQQ 是否确认")]
    for key, title in titles:
        doc.add_heading(title, level=2)
        for line in nar.get(key) or ["数据缺失，本步跳过。"]:
            para(line)

    # 5. 传导链
    doc.add_heading("五、传导链检查", level=1)
    para("典型避险链：油价↑ → 通胀预期↑ → US10Y↑ → 美元↑ → QQQ↓ → VIX/VXN↑（风险偏好时方向全部相反）。"
         "下表检查每个环节今天和近5日是否按典型方向联动。")

    def mark(v):
        return "—" if v is None else ("✓ 符合" if v else "✗ 背离")

    def mcol(v):
        return None if v is None else (GREEN if v else RED)

    table(["环节", "逻辑", "1日", "5日"],
          [[c["title"], c["why"], mark(c["c1"]), mark(c["c5"])] for c in A["chain"]],
          widths=[3.5, 6.5, 3.5, 3.5],
          colors=[[None, None, mcol(c["c1"]), mcol(c["c5"])] for c in A["chain"]])
    ok5 = sum(1 for c in A["chain"] if c["c5"] is True)
    para(f"近5日 {ok5}/{len(A['chain'])} 个环节符合典型方向。符合越多，说明当前行情由宏观主线驱动，信号越可信；"
         "背离多则说明有其他因素（财报、个股、政策事件）在主导。")

    # 6. 图表
    doc.add_heading("六、近半年走势", level=1)
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "chart.png")
        make_chart(data, A["cfg"], png)
        doc.add_picture(png, width=Cm(17))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    para("蓝线为收盘价，橙线为20日均线，红色虚线为你设置的关键位。", color=GREY, size=8.5)

    # 7–10. 第二个平台：自选股
    if extra is not None:
        try:
            extra(dict(para=para, table=table, heading=lambda t, lv: doc.add_heading(t, level=lv),
                       page_break=lambda: doc.add_page_break(),
                       RED=RED, GREEN=GREEN, GREY=GREY, ORANGE=ORANGE))
        except Exception:
            doc.add_heading("自选股", level=1)
            para("自选股部分生成失败，详情见「报告/错误日志.txt」。", color=RED)
            (REPORT_DIR / "错误日志.txt").write_text(traceback.format_exc(), encoding="utf-8")

    # 数据来源
    doc.add_heading("数据来源", level=1)
    kcol = {"TradingView": GREEN, "Yahoo": ORANGE, "缺失": RED}
    table(["指标", "来源", "数据截至", "备注"],
          [[d["name"], src[d["key"]]["label"],
            str(st[d["key"]]["date"]) if d["key"] in st else "—", src[d["key"]]["note"]]
           for d in INDICATORS],
          widths=[3.4, 3.8, 2.4, 7.4],
          colors=[[None, kcol.get(src[d["key"]]["kind"]), None, None] for d in INDICATORS])
    para("程序优先使用「TradingView数据」文件夹中未过期的 CSV（与你看盘的数值一致）；没有或过期时改用 Yahoo Finance。"
         "Yahoo 的布伦特（BZ=F）、美元指数（DX-Y.NYB）、美债（^TNX）与 TradingView 的 ICE:BRN1!、TVC:DXY、TVC:US10Y "
         "因合约与收盘时间不同会有小幅差异，但不影响方向和趋势判断。", color=GREY, size=9)
    if stock_src:
        para("自选股日K来源：" + stock_src + "。moomoo 来源需要本机运行 OpenD；未运行时自动改用 Yahoo。", color=GREY, size=9)

    # 说明
    doc.add_heading("说明", level=1)
    for t_ in [
        "宏观关键位与阈值在项目文件夹的「设置.json」中修改。VIX/VXN 区间为经验值；US10Y、DXY、布伦特、QQQ 的关键位请按自己的判断设置。",
        "个股关键位、板块ETF、放量倍数、RSI 阈值和公司档案在项目文件夹的「个股设置.xlsx」中修改。",
        "本报告由程序按固定规则生成，仅用于辅助复盘，不构成投资建议。",
    ]:
        para(t_, color=GREY, size=9)

    doc.save(out_path)


# ---------------------------------------------------------------- 主程序
def main():
    print("=" * 46)
    print(f"  玉衡5.0 v{_ver()}：正在拉取数据……")
    print("=" * 46)
    cfg = load_config()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if os.environ.get("MACRO_FAKE") == "1":
        data = fetch_fake()
        src = {k: dict(kind="模拟", label="模拟数据", note="测试用") for k in data}
    else:
        data, src = fetch_data(cfg)
    if sum(1 for s in data.values() if s is not None) < 3:
        raise RuntimeError("超过一半的指标没有拿到数据。请检查网络，或把 TradingView 导出的 CSV 放进「TradingView数据」文件夹后重试。")
    A = analyze(data, cfg)

    # 第二个平台：自选股（出错不影响宏观部分）
    S, extra, stock_src = None, None, ""
    if cfg.get("自选股", {}).get("启用", True):
        print()
        print("  自选股：")
        try:
            import stocks
            S = stocks.run(cfg, log=print, fake=os.environ.get("MACRO_FAKE") == "1")
            if S["results"]:
                stock_src = stocks.source_rows(S)

                def extra(h, S=S):
                    stocks.add_to_doc(S, h)
            if S.get("mm") is not None:
                S["mm"].close()
        except Exception:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (REPORT_DIR / "错误日志.txt").write_text(traceback.format_exc(), encoding="utf-8")
            print("  ✗ 自选股部分出错，已跳过（详情见 报告/错误日志.txt）")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"玉衡5.0_{dt.datetime.now():%Y-%m-%d_%H%M}.docx"
    top = []
    build_doc(A, data, src, out, extra=extra, stock_src=stock_src, top_lines=top)
    print()
    print(f"  结论：{A['verdict']}（综合分 {A['score']:+d}）")
    for lvl, txt in A["alerts"]:
        print(f"  [{lvl}] {txt}")
    if S:
        hi = [(r["name"], t) for r in S["results"] for l, t in r["signals"] if l == "高"]
        for n, t in hi:
            print(f"  [高] {n}：{t}")
    print()
    print(f"  ✓ 报告已生成：{out}")
    try:
        import share
        share.after_report(cfg, out, A, top_lines=top)
    except Exception:
        with (REPORT_DIR / "错误日志.txt").open("a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
        print("  ✗ 网站分享出错，已跳过（详情见 报告/错误日志.txt）")
    if cfg.get("生成后自动打开报告") and sys.platform == "darwin" and not os.environ.get("NO_OPEN_REPORT"):
        subprocess.run(["open", str(out)])
    return out


if __name__ == "__main__":
    try:
        main()
    except Exception:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        log = REPORT_DIR / "错误日志.txt"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        print("\n  ✗ 运行出错，详情见：", log)
        print(traceback.format_exc())
        sys.exit(1)
