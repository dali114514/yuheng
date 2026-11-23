"""信号改进评估：几种候选信号 vs 现有规则 vs「永远做多」。

防过拟合的做法：
  - 样本内 2010–2017 用来比较，样本外 2018 起用来验证；两段都要好才算数。
  - 候选信号都是公开、常见的规则，参数取常用默认值，不在本数据上调参。
  - 两种检验：① 信号出现后标的未来 30 个交易日涨跌（不涉及期权，纯看择时）；
              ② 完整的期权价差回测（三种成本设定）。
"""
from __future__ import annotations

import copy
import statistics as st

from . import backtest as bt
from .strategy import Signal, make_signal, rsi, sma

SPLIT = "2018-01-01"
HORIZON = 30


def _bull(why: str) -> Signal:
    return Signal("bull", [why])


NONE = Signal(None, [])

# 每个候选：(名称, 说明, 函数(closes, cfg, vix_hist) -> Signal)
VARIANTS = [
    ("current", "现有规则（MA120 + RSI14，多空都做）",
     lambda c, cfg, v: make_signal(c, cfg)),
    ("current_bull", "现有规则，只做多",
     lambda c, cfg, v: s if (s := make_signal(c, cfg)).direction == "bull" else NONE),
    ("always_bull", "对照：永远做多",
     lambda c, cfg, v: _bull("always")),
    ("ma200", "趋势过滤：收盘 > MA200 就做多",
     lambda c, cfg, v: _bull("ma200") if len(c) > 200 and c[-1] > sma(c, 200) else NONE),
    ("ma50_200", "金叉状态：MA50 > MA200 就做多",
     lambda c, cfg, v: _bull("ma50>200") if len(c) > 200 and sma(c, 50) > sma(c, 200) else NONE),
    ("ma200_rsi2", "趋势中短线超卖：收盘 > MA200 且 RSI(2) < 10",
     lambda c, cfg, v: _bull("rsi2") if len(c) > 200 and c[-1] > sma(c, 200) and rsi(c[-60:], 2) < 10 else NONE),
    ("ma200_dip", "趋势中回调：收盘 > MA200 且收盘 < MA10",
     lambda c, cfg, v: _bull("dip") if len(c) > 200 and c[-1] > sma(c, 200) and c[-1] < sma(c, 10) else NONE),
    ("vix_fear", "恐慌入场：VIX ≥ 25",
     lambda c, cfg, v: _bull("vix") if v and v[-1] is not None and v[-1] >= 25 else NONE),
    ("rsi14_oversold", "只看 RSI14 ≤ 35（不管趋势）",
     lambda c, cfg, v: _bull("rsi14") if rsi(c[-200:], 14) <= cfg["signal"]["rsi_oversold"] else NONE),
]


# ---------------------------------------------------------------- 检验 ①：未来 30 日涨跌

def forward_test(cfg, prices, vix) -> list[dict]:
    vix_map = dict(vix)
    out = []
    for key, desc, fn in VARIANTS:
        rec = {"key": key, "desc": desc}
        for period, lo, hi in (("in", "2010-01-01", SPLIT), ("out", SPLIT, "9999")):
            rets, base = [], []
            for u, series in prices.items():
                closes = [p for _, p in series]
                dates = [d for d, _ in series]
                vh: list[float] = []
                last = None
                for i, d in enumerate(dates):
                    last = vix_map.get(d, last)
                    vh.append(last)
                    if not (lo <= d < hi) or i + HORIZON >= len(closes) or i < 250:
                        continue
                    r = closes[i + HORIZON] / closes[i] - 1
                    base.append(r)
                    sig = fn(closes[max(0, i - 399):i + 1], cfg, vh[-60:])
                    if sig.direction == "bull":
                        rets.append(r)
                    elif sig.direction == "bear":
                        rets.append(-r)          # 做空方向：跌了算赚
            rec[period] = {
                "days": len(rets),
                "coverage": round(len(rets) / len(base), 3) if base else 0,
                "avg": round(st.mean(rets) * 100, 2) if rets else None,
                "hit": round(sum(r > 0 for r in rets) / len(rets), 3) if rets else None,
                "base_avg": round(st.mean(base) * 100, 2) if base else None,
                "edge": round((st.mean(rets) - st.mean(base)) * 100, 2) if rets and base else None,
            }
        out.append(rec)
    return out


# ---------------------------------------------------------------- 检验 ②：期权回测

SETTINGS = [
    ("pure", "纯信号：无手续费、无买卖价差、无预算限制", {"fee": 0, "spread": False, "budget": 1000}),
    ("costs", "含手续费 + 买卖价差，无预算限制", {"fee": None, "spread": True, "budget": 1000}),
    ("yours", "你的实际情况：含成本，预算按 config", {"fee": None, "spread": True, "budget": None}),
]


def _split_stats(trades: list[dict]) -> dict:
    res = {}
    for period, ts in (("in", [t for t in trades if t["opened"] < SPLIT]),
                       ("out", [t for t in trades if t["opened"] >= SPLIT])):
        n = len(ts)
        pnl = sum(t["pnl"] for t in ts)
        res[period] = {"trades": n, "pnl": round(pnl, 0),
                       "per_trade": round(pnl / n, 2) if n else None,
                       "win": round(sum(t["pnl"] > 0 for t in ts) / n, 3) if n else None}
    return res


def option_test(cfg, prices, vix, log=print) -> list[dict]:
    orig_hs = bt.half_spread
    out = []
    for key, desc, fn in VARIANTS:
        rec = {"key": key, "desc": desc}
        for skey, _, s in SETTINGS:
            c = copy.deepcopy(cfg)
            if s["fee"] is not None:
                c["account"]["fee_per_contract"] = s["fee"]
            if s["budget"]:
                c["account"]["budget_usd"] = s["budget"]
            bt.half_spread = orig_hs if s["spread"] else (lambda m: 0.0)
            try:
                r = bt.run(c, prices, vix, signal_fn=fn)
            finally:
                bt.half_spread = orig_hs
            rec[skey] = _split_stats(r["trades"])
        log(f"  完成：{desc}")
        out.append(rec)
    return out
