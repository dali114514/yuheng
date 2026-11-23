# -*- coding: utf-8 -*-
"""
板块：显示顺序、图标、代表 ETF，以及「板块集体异动」判断。
股票属于哪个板块写在「个股设置.xlsx → 个股参数 → 板块」；这里只管排序和显示。
新加的板块名不在下面列表里也能用，会排在最后、用 ▪️ 图标。
"""
SECTORS = [
    ("芯片设计", "🔲", "SOXX"),
    ("存储", "💾", "SOXX"),
    ("AI基建/光通信", "🔌", "XLK"),
    ("互联网/流媒体", "🌐", "XLC"),
    ("软件/云", "☁️", "IGV"),
    ("电商/消费科技", "🛒", "XLY"),
    ("电动车/自动驾驶", "🚗", "XLY"),
    ("航天/工业", "🚀", "XLI"),
    ("必需消费", "🥤", "XLP"),
    ("指数/债券ETF", "📊", "SPY"),
]
_ORDER = {n: i for i, (n, _, _) in enumerate(SECTORS)}
_EMOJI = {n: e for n, e, _ in SECTORS}
_ETF = {n: x for n, _, x in SECTORS}

DEFAULT_TH = {"板块异动_平均涨跌幅%": 2.5, "板块异动_同向比例": 0.75}


def order(name):
    return (_ORDER.get(name or "未分类", 98 if name else 99), name or "")


def emoji(name):
    return _EMOJI.get(name, "▪️")


def label(name):
    return f"{emoji(name)} {name or '未分类'}"


def etf(name):
    return _ETF.get(name)


def is_index(name):
    return (name or "").endswith("ETF")


def group(items):
    """items: [(板块, 涨跌幅或 None, 任意对象)] → [dict(name, avg, ups, downs, n, members)]，按板块顺序。"""
    g = {}
    for sec, mv, obj in items:
        g.setdefault(sec or "未分类", []).append((mv, obj))
    out = []
    for name, ms in g.items():
        mvs = [m for m, _ in ms if m is not None]
        out.append(dict(name=name, n=len(ms), members=[o for _, o in ms],
                        avg=sum(mvs) / len(mvs) if mvs else None,
                        ups=sum(m > 0 for m in mvs), downs=sum(m < 0 for m in mvs), priced=len(mvs)))
    return sorted(out, key=lambda s: order(s["name"]))


def anomalies(groups, th=None):
    """板块集体异动：同一板块至少 2 只有报价、同向比例 ≥ 阈值、平均涨跌幅绝对值 ≥ 阈值。指数/债券 ETF 不算。"""
    th = dict(DEFAULT_TH, **(th or {}))
    lim, ratio = float(th["板块异动_平均涨跌幅%"]), float(th["板块异动_同向比例"])
    out = []
    for s in groups:
        if is_index(s["name"]) or s["priced"] < 2 or s["avg"] is None or abs(s["avg"]) < lim:
            continue
        same = s["ups"] if s["avg"] > 0 else s["downs"]
        if same / s["priced"] >= ratio:
            out.append(dict(name=s["name"], dir="涨" if s["avg"] > 0 else "跌", avg=s["avg"],
                            same=same, n=s["priced"]))
    return out


def anomaly_text(a):
    return (f"{label(a['name'])} 集体{'大涨' if a['dir'] == '涨' else '大跌'} 平均 {a['avg']:+.1f}%"
            f"（{a['same']}/{a['n']} 只同向）")
