"""小账户回测：$50 起步、全仓、按 +70% 止盈 / −30% 止损，资金滚动，亏到买不起就「出局」。

对比两种结构 × 三种入场信号，并从 2010–2024 每个月初各起步一次、每次跑 2 年，看结果分布。

近似说明（和 backtest.py 一样）：
  - 期权价格用 VIX 当隐含波动率按 Black-Scholes 估算，没有波动率偏斜。
    实际中指数的虚值 call 隐含波动率低于 VIX，所以单买深度虚值 call 的真实价格会更便宜，
    但真实到价概率也更低——两者方向相反，整体影响不确定。
  - 只在每天收盘检查止盈止损：真实的止损会在盘中触发，但跳空时同样会滑点，
    所以这里的「−30% 止损」实际成交常常比 −30% 更差，这一点是真实存在的风险。
  - 手续费：每张合约每条腿 $1（开、平各算一次）。
"""
from __future__ import annotations

import datetime as dt
import math
import statistics as st

from . import backtest as bt
from .strategy import make_signal, pick_spread, rsi

FEE = 1.0
TP, SL = 0.70, 0.30


# ---------------------------------------------------------------- 预计算信号

def precompute(cfg, prices, vix):
    vix_map = dict(vix)
    unds = [u for u in ("US.SPY", "US.QQQ") if u in prices]
    series = {u: dict(prices[u]) for u in unds}
    dates = sorted(set.intersection(*(set(series[u]) for u in unds)) & set(vix_map))
    closes = {u: [series[u][d] for d in dates] for u in unds}
    vixs = [vix_map[d] for d in dates]
    sigs = {"always": {}, "rsi35": {}, "current_bull": {}}
    for u in unds:
        c = closes[u]
        a, r35, cur = [], [], []
        for i in range(len(c)):
            w = c[max(0, i - 399):i + 1]
            a.append(i >= 250)
            r35.append(i >= 250 and rsi(w[-200:], 14) <= cfg["signal"]["rsi_oversold"])
            cur.append(i >= 250 and make_signal(w, cfg).direction == "bull")
        sigs["always"][u], sigs["rsi35"][u], sigs["current_bull"][u] = a, r35, cur
    return {"dates": dates, "closes": closes, "vix": vixs, "sigs": sigs, "unds": unds}


SIGNAL_NAMES = {"always": "空仓就买", "rsi35": "RSI14 ≤ 35", "current_bull": "现有规则（只做多）"}
STRUCT_NAMES = {"single": "单买一张 Call", "spread": "$1 宽牛市价差"}


# ---------------------------------------------------------------- 入场选合约

def _iv(u, v):
    return v / 100 * bt.IV_MULT.get(u, 1.0)


def pick_single(u, spot, iv, capital):
    """在 7–45 天里，选「买得起」的 Delta 最高的 call。返回 (strike, dte, ask, qty) 或 None。"""
    best = None
    for dte in (7, 14, 21, 30, 45):
        k = math.ceil(spot)
        while k < spot * 1.25:
            mid, delta = bt.bs(spot, k, dte / 365, iv, True)
            ask = mid + bt.half_spread(mid)
            per = ask * 100 + FEE
            if per <= capital and delta >= 0.02:
                if not best or delta > best[4] or (delta == best[4] and dte > best[1]):
                    best = (k, dte, round(ask, 2), int(capital // per), delta)
                break   # 同一到期日里，第一个买得起的就是 Delta 最高的
            k += 1
    return best


def pick_spread_small(u, spot, iv, capital, cfg, day):
    c = {**cfg, "account": {**cfg["account"], "budget_usd": capital, "fee_per_contract": FEE},
         "selection": {**cfg["selection"], "long_delta_min": 0.15, "min_reward_risk": 1.0}}
    exp = (dt.date.fromisoformat(day) + dt.timedelta(days=45)).isoformat()
    rows = bt.synth_chain(u, spot, iv, 45, exp, 0.08)
    sp, _ = pick_spread("bull", u, rows, c)
    if not sp:
        return None
    per = sp.worst_debit * 100 + 2 * FEE
    return sp, int(capital // per)


# ---------------------------------------------------------------- 单次模拟

def simulate(P, cfg, structure, signal, i0, i1, capital=50.0, risk_frac=None):
    """risk_frac=None：全仓；否则每笔最多投入「当前本金 × risk_frac」（至少 1 张）。"""
    dates, closes, vixs, sig = P["dates"], P["closes"], P["vix"], P["sigs"][signal]
    pos, trades, curve = None, [], []
    ruined_on = None
    for i in range(i0, min(i1, len(dates))):
        day = dates[i]
        if pos:
            u = pos["u"]
            spot, iv = closes[u][i], _iv(u, vixs[i])
            dte = pos["dte0"] - (dt.date.fromisoformat(day) - dt.date.fromisoformat(pos["day"])).days
            if structure == "single":
                bid, _ = bt.leg_quote(spot, pos["k"], dte, iv, True)
                val = bid
                time_exit = dte <= 1
            else:
                lb, _ = bt.leg_quote(spot, pos["k"], dte, iv, True)
                _, sa = bt.leg_quote(spot, pos["k2"], dte, iv, True)
                val = max(lb - sa, 0.0)
                time_exit = dte <= 14
            ret = val / pos["entry"] - 1
            reason = "止盈" if ret >= TP else "止损" if ret <= -SL else "到期前平仓" if time_exit or dte <= 0 else None
            if reason:
                legs = 1 if structure == "single" else 2
                proceeds = val * 100 * pos["q"] - FEE * legs * pos["q"]
                capital += proceeds
                pnl = proceeds - pos["cost"]
                trades.append({"day": pos["day"], "exit_day": day, "ret": round(ret, 3), "pnl": round(pnl, 2),
                               "reason": reason, "capital": round(capital, 2)})
                pos = None
        if not pos and ruined_on is None:
            for u in P["unds"]:
                if not sig[u][i]:
                    continue
                spot, iv = closes[u][i], _iv(u, vixs[i])
                budget = capital if risk_frac is None else capital * risk_frac
                if structure == "single":
                    p = pick_single(u, spot, iv, budget)
                    if p:
                        k, dte0, ask, q, _ = p
                        cost = ask * 100 * q + FEE * q
                        capital -= cost
                        pos = {"u": u, "k": k, "dte0": dte0, "day": day, "entry": ask, "q": q, "cost": cost}
                else:
                    p = pick_spread_small(u, spot, iv, budget, cfg, day)
                    if p and p[1] >= 1:
                        sp, q = p
                        cost = sp.worst_debit * 100 * q + 2 * FEE * q
                        capital -= cost
                        pos = {"u": u, "k": sp.long_strike, "k2": sp.short_strike, "dte0": 45, "day": day,
                               "entry": sp.worst_debit, "q": q, "cost": cost}
                if pos:
                    break
            if not pos and _cant_afford(P, structure, capital, i, cfg):
                ruined_on = day
        curve.append((day, round(capital + (pos["cost"] if pos else 0), 2)))   # 持仓按成本计
    final = capital + (pos["cost"] if pos else 0)
    return {"final": round(final, 2), "trades": trades, "ruined_on": ruined_on, "curve": curve}


def _cant_afford(P, structure, capital, i, cfg):
    u = "US.SPY"
    spot, iv = P["closes"][u][i], _iv(u, P["vix"][i])
    if structure == "single":
        return pick_single(u, spot, iv, capital) is None
    p = pick_spread_small(u, spot, iv, capital, cfg, P["dates"][i])
    return not p or p[1] < 1


# ---------------------------------------------------------------- 多起点分布

def study(cfg, prices, vix, years=2, log=print):
    P = precompute(cfg, prices, vix)
    dates = P["dates"]
    starts = [i for i in range(1, len(dates)) if dates[i] >= "2010-01-01" and dates[i][:7] != dates[i - 1][:7]
              and dt.date.fromisoformat(dates[i]) + dt.timedelta(days=365 * years) <= dt.date.fromisoformat(dates[-1])]
    horizon = int(252 * years)
    out = []
    for structure in ("single", "spread"):
        for signal in ("always", "rsi35", "current_bull"):
            finals, ruined, ntr, wins, losses_r = [], 0, [], 0, []
            for i0 in starts:
                r = simulate(P, cfg, structure, signal, i0, i0 + horizon)
                finals.append(r["final"])
                ruined += r["ruined_on"] is not None
                ntr.append(len(r["trades"]))
                wins += sum(t["pnl"] > 0 for t in r["trades"])
                losses_r += [t["ret"] for t in r["trades"] if t["reason"] == "止损"]
            tot = sum(ntr)
            q = st.quantiles(finals, n=4)
            full = simulate(P, cfg, structure, signal, starts[0], len(dates))
            out.append({
                "structure": structure, "signal": signal,
                "label": f"{STRUCT_NAMES[structure]} · {SIGNAL_NAMES[signal]}",
                "runs": len(starts), "ruined_pct": round(ruined / len(starts), 3),
                "median_final": round(st.median(finals), 2), "p25": round(q[0], 2), "p75": round(q[2], 2),
                "pct_up": round(sum(f > 50 for f in finals) / len(finals), 3),
                "pct_double": round(sum(f >= 100 for f in finals) / len(finals), 3),
                "avg_trades": round(tot / len(starts), 1),
                "win_rate": round(wins / tot, 3) if tot else None,
                "avg_stop_ret": round(st.mean(losses_r), 3) if losses_r else None,
                "full_path": {"final": full["final"], "ruined_on": full["ruined_on"], "trades": len(full["trades"]),
                              "curve": full["curve"][::5]},
            })
            log(f"  完成：{out[-1]['label']}")
    return {"years": years, "start_capital": 50, "tp": TP, "sl": SL, "results": out}
