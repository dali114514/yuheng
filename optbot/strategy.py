"""纯策略逻辑：指标、开仓信号、价差选择、平仓规则。

这里不碰任何网络 / 券商接口，所以可以单独测试。
规则出处（期权坤哥系列）：
  - 只看 MA + RSI；MA120 是牛熊分界线
  - RSI 超卖做牛市价差（Bull Call Spread），超买做熊市价差（Bear Put Spread）
  - 均线多头排列 = 趋势行情，顺势做牛市价差；空头排列反之
  - 买方别买两周内到期的；价差选 45-60 DTE
  - 赚到 50% 止盈；亏 50% 止损；剩 14 天平仓
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------- 指标

def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder RSI。"""
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


# ---------------------------------------------------------------- 信号

@dataclass
class Signal:
    direction: str | None          # "bull" / "bear" / None
    reasons: list[str] = field(default_factory=list)
    values: dict = field(default_factory=dict)


def make_signal(closes: list[float], cfg: dict) -> Signal:
    s = cfg["signal"]
    trend_n = s["trend_ma"]
    if len(closes) < trend_n + 1:
        return Signal(None, [f"历史K线不足 {trend_n + 1} 根，无法计算 MA{trend_n}"])

    close = closes[-1]
    ma5, ma10, ma20, ma60 = (sma(closes, n) for n in (5, 10, 20, 60))
    ma_trend = sma(closes, trend_n)
    r = rsi(closes, s["rsi_period"])
    v = {"close": close, "ma5": ma5, "ma10": ma10, "ma20": ma20,
         "ma60": ma60, f"ma{trend_n}": ma_trend, "rsi": r}

    above = close > ma_trend
    bull_stack = ma5 > ma10 > ma20 > ma60
    bear_stack = ma5 < ma10 < ma20 < ma60

    if above and r <= s["rsi_oversold"]:
        return Signal("bull", [f"站上 MA{trend_n}（大趋势向上）",
                               f"RSI {r:.1f} ≤ {s['rsi_oversold']}（短期超卖，回调买点）"], v)
    if above and bull_stack and r < s["rsi_overbought"]:
        return Signal("bull", [f"站上 MA{trend_n}",
                               "MA5>MA10>MA20>MA60 多头排列（趋势行情）",
                               f"RSI {r:.1f} 未超买"], v)
    if (not above) and r >= s["rsi_overbought"]:
        return Signal("bear", [f"跌破 MA{trend_n}（大趋势向下）",
                               f"RSI {r:.1f} ≥ {s['rsi_overbought']}（短期超买，反弹卖点）"], v)
    if (not above) and bear_stack and r > s["rsi_oversold"]:
        return Signal("bear", [f"跌破 MA{trend_n}",
                               "MA5<MA10<MA20<MA60 空头排列（下跌趋势）",
                               f"RSI {r:.1f} 未超卖"], v)

    return Signal(None, [f"无信号：{'站上' if above else '跌破'} MA{trend_n}，"
                         f"RSI {r:.1f}，均线{'多头' if bull_stack else '空头' if bear_stack else '缠绕'}"], v)


# ---------------------------------------------------------------- 选合约

@dataclass
class Spread:
    direction: str            # bull / bear
    underlying: str
    expiry: str
    dte: int
    long_code: str
    short_code: str
    long_strike: float
    short_strike: float
    long_delta: float
    short_delta: float
    debit: float              # 每股净支出（中间价）
    worst_debit: float        # 每股净支出（按对手价成交）
    width: float

    @property
    def max_loss(self) -> float:        # 每组（100股）
        return self.debit * 100

    @property
    def max_profit(self) -> float:
        return (self.width - self.debit) * 100

    @property
    def reward_risk(self) -> float:
        return self.max_profit / self.max_loss if self.max_loss > 0 else 0.0

    def describe(self) -> str:
        kind = "牛市看涨价差 Bull Call Spread" if self.direction == "bull" else "熊市看跌价差 Bear Put Spread"
        cp = "Call" if self.direction == "bull" else "Put"
        return (f"{kind} | {self.underlying} 到期 {self.expiry}（{self.dte} 天）\n"
                f"  买入 {self.long_strike:g} {cp}（Delta {self.long_delta:+.2f}）  {self.long_code}\n"
                f"  卖出 {self.short_strike:g} {cp}（Delta {self.short_delta:+.2f}）  {self.short_code}\n"
                f"  净支出 ${self.debit:.2f}/股 = ${self.max_loss:.0f}/组（对手价 ${self.worst_debit * 100:.0f}）\n"
                f"  最大盈利 ${self.max_profit:.0f}  最大亏损 ${self.max_loss:.0f}  盈亏比 {self.reward_risk:.2f}")


def _mid(row: dict) -> float:
    return (row["bid"] + row["ask"]) / 2


def _liquid(row: dict, sel: dict) -> bool:
    if row["bid"] <= 0 or row["ask"] <= 0 or row["ask"] < row["bid"]:
        return False
    if row.get("oi", 0) < sel["min_open_interest"]:
        return False
    mid = _mid(row)
    return mid > 0 and (row["ask"] - row["bid"]) / mid <= sel["max_leg_spread_pct"]


def pick_spread(direction: str, underlying: str, rows: list[dict], cfg: dict) -> tuple[Spread | None, str]:
    """rows: 每个期权一行 dict，字段 code/option_type/strike/expiry/dte/bid/ask/delta/oi。

    返回 (最优价差, 说明)。
    """
    sel, acc = cfg["selection"], cfg["account"]
    want = "CALL" if direction == "bull" else "PUT"
    fees = acc["fee_per_contract"] * 2           # 开仓两条腿
    max_debit_total = acc["budget_usd"] - fees

    legs = [r for r in rows if r["option_type"] == want
            and sel["min_dte"] <= r["dte"] <= sel["max_dte"] and _liquid(r, sel)]
    if not legs:
        return None, f"{underlying}: {sel['min_dte']}-{sel['max_dte']} 天内没有流动性合格的 {want}"

    by_exp: dict[str, list[dict]] = {}
    for r in legs:
        by_exp.setdefault(r["expiry"], []).append(r)

    cands: list[Spread] = []
    for exp, rs in by_exp.items():
        rs.sort(key=lambda r: r["strike"])
        for a in rs:
            ad = abs(a["delta"])
            if not (sel["long_delta_min"] <= ad <= sel["long_delta_max"]):
                continue
            for b in rs:
                # bull call: 卖更高行权价；bear put: 卖更低行权价
                width = (b["strike"] - a["strike"]) if direction == "bull" else (a["strike"] - b["strike"])
                if width <= 0 or width > sel["max_width"]:
                    continue
                debit = round(_mid(a) - _mid(b), 2)
                worst = round(a["ask"] - b["bid"], 2)
                if debit <= 0.02 or debit >= width or worst >= width:
                    continue
                # 按「对手价」算预算：模拟盘两条腿是按可立即成交的价格下单的，实际会付对手价
                cost = worst if sel.get("budget_on_worst", True) else debit
                if cost * 100 > max_debit_total:
                    continue
                sp = Spread(direction, underlying, exp, a["dte"], a["code"], b["code"],
                            a["strike"], b["strike"], a["delta"], b["delta"],
                            debit, round(a["ask"] - b["bid"], 2), width)
                if sp.reward_risk < sel["min_reward_risk"]:
                    continue
                cands.append(sp)

    if not cands:
        return None, (f"{underlying}: 找不到预算内（≤${max_debit_total:.0f}/组，已扣手续费）"
                      f"且盈亏比 ≥ {sel['min_reward_risk']} 的价差")

    tgt = sel["target_dte"]
    # 先选最接近目标天数的到期日；同一到期日里，买入腿 Delta 越高（越接近平值，胜率越高）越好，
    # 再看盈亏比。
    cands.sort(key=lambda s: (abs(s.dte - tgt), -abs(s.long_delta), -s.reward_risk))
    return cands[0], f"{underlying}: 共 {len(cands)} 个候选价差，选出最优一个"


# ---------------------------------------------------------------- 平仓规则

def exit_decision(pos: dict, value_now: float, dte_now: int, signal_dir: str | None, cfg: dict) -> tuple[bool, str]:
    """pos: state 里的持仓（entry_debit/width/direction）。value_now: 价差当前每股价值（中间价）。"""
    ex = cfg["exit"]
    entry, width = pos["entry_debit"], pos["width"]
    tp = entry + ex["take_profit_frac"] * (width - entry)
    sl = entry * (1 - ex["stop_loss_frac"])
    if value_now >= tp:
        return True, f"止盈：价差价值 ${value_now:.2f} ≥ 目标 ${tp:.2f}（最大盈利的 {ex['take_profit_frac']:.0%}）"
    if value_now <= sl:
        return True, f"止损：价差价值 ${value_now:.2f} ≤ ${sl:.2f}（亏损 {ex['stop_loss_frac']:.0%}）"
    if dte_now <= ex["exit_dte"]:
        return True, f"时间止损：只剩 {dte_now} 天到期（≤ {ex['exit_dte']} 天，Theta 加速期）"
    opposite = "bear" if pos["direction"] == "bull" else "bull"
    if signal_dir == opposite:
        return True, "信号反转：出现反方向信号"
    return False, (f"继续持有：价值 ${value_now:.2f}（止盈 ${tp:.2f} / 止损 ${sl:.2f}），剩 {dte_now} 天")
