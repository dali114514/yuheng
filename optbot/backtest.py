"""历史回测：用真实日线 + VIX 推算期权价格，逐日复用机器人自己的规则。

⚠️ 这是「近似」回测，不是真实成交记录：
  - 没有历史期权报价，期权价格用 Black-Scholes 估算；隐含波动率 = VIX（QQQ 用 VIX×1.2），
    没有考虑波动率微笑/偏斜（实际上虚值 put 更贵、虚值 call 更便宜）。
  - 买卖价差按经验值设定（每条腿 ±$0.01~0.02 再加中间价的 0.5%）。
  - 入场、出场都按当天收盘价、按「对手价」成交（和模拟盘下单方式一致）。
信号、选合约、止盈止损、仓位上限都直接调用 strategy.py，和机器人完全一致。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
from pathlib import Path

from .strategy import exit_decision, make_signal, pick_spread

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "backtest" / "data"
R = 0.02            # 无风险利率（固定近似）
IV_MULT = {"US.SPY": 1.0, "US.QQQ": 1.2}


# ---------------------------------------------------------------- 数据

def fetch_daily(cli, code: str, start: str = "2009-01-01") -> list[tuple[str, float]]:
    """从 moomoo CLI 分段拉日线，缓存到 backtest/data/。返回 [(YYYY-MM-DD, close)]。"""
    DATA.mkdir(parents=True, exist_ok=True)
    cache = DATA / f"{code.replace('.', '_')}.json"
    rows: dict[str, float] = {}
    if cache.exists():
        rows = dict(json.loads(cache.read_text("utf-8")))
        start = max(rows) if rows else start
    today = dt.date.today().isoformat()
    cur = start
    while cur <= today:
        d = cli("history-kline", code, "--start", cur, "--end", today, "--num", 370)
        got = d.get("data", d).get("kline_list", [])
        if not got:
            break
        before = len(rows)
        for k in got:
            s = str(k["date"])
            rows[f"{s[:4]}-{s[4:6]}-{s[6:8]}"] = float(k["close"])
        last = max(rows)
        nxt = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
        if nxt <= cur or len(rows) == before:
            break
        cur = nxt
    out = sorted(rows.items())
    cache.write_text(json.dumps(out), "utf-8")
    return out


def fetch_ohlcv(cli, code: str, start: str = "2009-01-01") -> list[dict]:
    """带开高低收和成交量的日线，缓存到 backtest/data/<code>_ohlcv.json。"""
    DATA.mkdir(parents=True, exist_ok=True)
    cache = DATA / f"{code.replace('.', '_')}_ohlcv.json"
    rows: dict[str, dict] = {}
    if cache.exists():
        rows = {r["date"]: r for r in json.loads(cache.read_text("utf-8"))}
        start = max(rows) if rows else start
    today = dt.date.today().isoformat()
    cur = start
    while cur <= today:
        d = cli("history-kline", code, "--start", cur, "--end", today, "--num", 370)
        got = d.get("data", d).get("kline_list", [])
        if not got:
            break
        before = len(rows)
        for k in got:
            s = str(k["date"])
            day = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            rows[day] = {"date": day, "open": float(k["open"]), "high": float(k["high"]),
                         "low": float(k["low"]), "close": float(k["close"]), "volume": float(k.get("volume") or 0)}
        last = max(rows)
        nxt = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
        if nxt <= cur or len(rows) == before:
            break
        cur = nxt
    out = [rows[k] for k in sorted(rows)]
    cache.write_text(json.dumps(out), "utf-8")
    return out


def fetch_intraday(cli, code: str, ktype: int = 8, start: str = "2016-01-01") -> list[dict]:
    """分钟级 K 线（ktype 8 = 30 分钟），缓存到 backtest/data/<code>_k<ktype>.json。time 为美东时间字符串。"""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    DATA.mkdir(parents=True, exist_ok=True)
    cache = DATA / f"{code.replace('.', '_')}_k{ktype}.json"
    rows: dict[str, dict] = {}
    if cache.exists():
        rows = {r["time"]: r for r in json.loads(cache.read_text("utf-8"))}
        start = max(rows)[:10] if rows else start
    today = dt.date.today().isoformat()
    cur = start
    while cur <= today:
        d = cli("history-kline", code, "--ktype", ktype, "--start", cur, "--end", today, "--num", 370)
        got = d.get("data", d).get("kline_list", [])
        if not got:
            break
        before = len(rows)
        for k in got:
            t = dt.datetime.fromtimestamp(int(k["time_key"]) / 1000, et).strftime("%Y-%m-%d %H:%M")
            rows[t] = {"time": t, "open": float(k["open"]), "high": float(k["high"]), "low": float(k["low"]),
                       "close": float(k["close"]), "volume": float(k.get("volume") or 0)}
        last_day = max(rows)[:10]
        if len(rows) == before:
            nxt = (dt.date.fromisoformat(cur) + dt.timedelta(days=30)).isoformat()   # 跳过没有数据的空档
        else:
            nxt = (dt.date.fromisoformat(last_day) + dt.timedelta(days=1)).isoformat()
        if nxt <= cur:
            break
        cur = nxt
    out = [rows[k] for k in sorted(rows)]
    cache.write_text(json.dumps(out), "utf-8")
    return out


# ---------------------------------------------------------------- 期权定价

def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(spot: float, k: float, t_years: float, iv: float, call: bool) -> tuple[float, float]:
    """返回 (价格, delta)。"""
    if t_years <= 0 or iv <= 0:
        intrinsic = max(spot - k, 0) if call else max(k - spot, 0)
        return intrinsic, (1.0 if call and spot > k else -1.0 if (not call and spot < k) else 0.0)
    v = iv * math.sqrt(t_years)
    d1 = (math.log(spot / k) + (R + iv * iv / 2) * t_years) / v
    d2 = d1 - v
    disc = math.exp(-R * t_years)
    if call:
        return spot * _ncdf(d1) - k * disc * _ncdf(d2), _ncdf(d1)
    return k * disc * _ncdf(-d2) - spot * _ncdf(-d1), _ncdf(d1) - 1


def half_spread(mid: float) -> float:
    return 0.01 + 0.005 * mid if mid < 5 else 0.02 + 0.005 * mid


def synth_chain(und: str, spot: float, iv: float, dte: int, expiry: str, window: float) -> list[dict]:
    rows = []
    lo, hi = math.ceil(spot * (1 - window)), math.floor(spot * (1 + window))
    for k in range(lo, hi + 1):
        for call in (True, False):
            mid, delta = bs(spot, k, dte / 365, iv, call)
            if mid < 0.02:
                continue
            h = half_spread(mid)
            rows.append({"code": f"{und}{expiry}{'C' if call else 'P'}{k}", "option_type": "CALL" if call else "PUT",
                         "strike": float(k), "expiry": expiry, "dte": dte, "bid": round(max(mid - h, 0.01), 2),
                         "ask": round(mid + h, 2), "delta": delta, "oi": 10000, "iv": iv})
    return rows


def leg_quote(spot, strike, dte, iv, call) -> tuple[float, float]:
    mid, _ = bs(spot, strike, max(dte, 0) / 365, iv, call)
    h = half_spread(mid)
    return max(mid - h, 0.0), mid + h


# ---------------------------------------------------------------- 回测主循环

def run(cfg: dict, prices: dict[str, list[tuple[str, float]]], vix: list[tuple[str, float]],
        start: str = "2010-01-01", signal_fn=None) -> dict:
    """signal_fn(closes, cfg, vix_hist) -> Signal；不传就用机器人当前的 make_signal。"""
    sig_of = signal_fn or (lambda closes, c, vh: make_signal(closes, c))
    sel, acc = cfg["selection"], cfg["account"]
    fee = acc["fee_per_contract"]
    vix_map = dict(vix)
    unds = [u for u in cfg["universe"]["underlyings"] if u in prices]
    series = {u: dict(prices[u]) for u in unds}
    hist = {u: [] for u in unds}
    dates = sorted(set.intersection(*(set(series[u]) for u in unds)) & set(vix_map))

    pos = None
    trades, skipped, equity = [], {"no_spread": 0}, []
    cum = 0.0
    last_vix = None
    vix_hist: list[float] = []
    for day in dates:
        for u in unds:
            hist[u].append(series[u][day])
        last_vix = vix_map.get(day, last_vix)
        vix_hist.append(last_vix)
        if day < start:
            continue

        # --- 管理持仓
        if pos:
            u = pos["underlying"]
            spot, iv = hist[u][-1], last_vix / 100 * IV_MULT[u]
            dte = (dt.date.fromisoformat(pos["expiry"]) - dt.date.fromisoformat(day)).days
            call = pos["direction"] == "bull"
            lb, la = leg_quote(spot, pos["long_strike"], dte, iv, call)
            sb, sa = leg_quote(spot, pos["short_strike"], dte, iv, call)
            value_mid = round((lb + la) / 2 - (sb + sa) / 2, 2)
            sig = sig_of(hist[u][-400:], cfg, vix_hist[-60:])
            do_exit, why = exit_decision(pos, value_mid, dte, sig.direction, cfg)
            if dte <= 0:
                do_exit, why = True, "到期"
            if do_exit:
                exit_px = max(lb - sa, 0.0) if dte > 0 else value_mid
                pnl = round((exit_px - pos["entry_debit"]) * 100 - fee * 4, 2)
                cum += pnl
                trades.append({**{k: pos[k] for k in ("underlying", "direction", "opened", "expiry",
                                                     "long_strike", "short_strike", "entry_debit", "spot_in")},
                               "closed": day, "exit": round(exit_px, 2), "spot_out": round(spot, 2),
                               "pnl": pnl, "reason": why.split("：")[0], "days": (dt.date.fromisoformat(day)
                                                                                 - dt.date.fromisoformat(pos["opened"])).days})
                pos = None

        # --- 找新仓（仓位上限 1，与机器人默认一致）
        if not pos:
            for u in unds:
                sig = sig_of(hist[u][-400:], cfg, vix_hist[-60:])
                if not sig.direction:
                    continue
                spot, iv = hist[u][-1], last_vix / 100 * IV_MULT[u]
                expiry = (dt.date.fromisoformat(day) + dt.timedelta(days=sel["target_dte"])).isoformat()
                rows = synth_chain(u, spot, iv, sel["target_dte"], expiry, sel["strike_window_pct"])
                sp, _ = pick_spread(sig.direction, u, rows, cfg)
                if not sp:
                    skipped["no_spread"] += 1
                    continue
                pos = {"underlying": u, "direction": sp.direction, "opened": day, "expiry": expiry,
                       "long_strike": sp.long_strike, "short_strike": sp.short_strike, "width": sp.width,
                       "entry_debit": sp.worst_debit, "spot_in": round(spot, 2)}
                break
        equity.append((day, round(cum, 2)))

    return {"trades": trades, "equity": equity, "skipped": skipped, "stats": stats(trades, equity, cfg)}


def stats(trades: list[dict], equity: list[tuple[str, float]], cfg: dict) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    peak, mdd = 0.0, 0.0
    for _, v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    by_year: dict[str, dict] = {}
    for t in trades:
        y = by_year.setdefault(t["closed"][:4], {"n": 0, "pnl": 0.0, "wins": 0})
        y["n"] += 1
        y["pnl"] = round(y["pnl"] + t["pnl"], 2)
        y["wins"] += t["pnl"] > 0
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    gross_w, gross_l = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    cap = cfg.get("discipline", {}).get("max_total_loss_usd")
    breaker_day, c = None, 0.0
    for t in trades:
        c += t["pnl"]
        if cap is not None and -c >= cap and not breaker_day:
            breaker_day = t["closed"]
    streak = best = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        best = max(best, streak)
    return {
        "trades": n, "win_rate": round(len(wins) / n, 3) if n else 0,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "avg_win": round(gross_w / len(wins), 2) if wins else 0,
        "avg_loss": round(-gross_l / len(losses), 2) if losses else 0,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "max_drawdown": round(mdd, 2), "max_losing_streak": best,
        "avg_days_held": round(sum(t["days"] for t in trades) / n, 1) if n else 0,
        "by_year": by_year, "exit_reasons": reasons,
        "breaker_would_trip_on": breaker_day,
        "first_day": equity[0][0] if equity else None, "last_day": equity[-1][0] if equity else None,
    }


def diagnostics(cfg: dict, prices, vix, start="2010-01-01") -> list[dict]:
    """拆解：亏损来自交易成本还是信号本身？再和「永远做多」对照。"""
    from . import strategy
    global half_spread, make_signal
    orig_hs, orig_sig = half_spread, make_signal
    rows = []

    def one(label, fee=None, spread=True, budget=None, always_bull=False):
        global half_spread, make_signal
        c = copy.deepcopy(cfg)
        if fee is not None:
            c["account"]["fee_per_contract"] = fee
        if budget:
            c["account"]["budget_usd"] = budget
        half_spread = orig_hs if spread else (lambda m: 0.0)
        make_signal = (lambda closes, cfg: strategy.Signal("bull", ["always"], {})) if always_bull else orig_sig
        try:
            s = run(c, prices, vix, start)["stats"]
        finally:
            half_spread, make_signal = orig_hs, orig_sig
        rows.append({"label": label, "trades": s["trades"], "win_rate": s["win_rate"], "total_pnl": s["total_pnl"],
                     "profit_factor": s["profit_factor"],
                     "per_trade": round(s["total_pnl"] / s["trades"], 2) if s["trades"] else 0})

    one("当前规则（含手续费 + 买卖价差）")
    one("去掉手续费", fee=0)
    one("去掉手续费和买卖价差（只看信号）", fee=0, spread=False)
    one("只看信号，且不受 $28 预算限制", fee=0, spread=False, budget=1000)
    one("对照组：不看信号、永远做多", fee=0, spread=False, budget=1000, always_bull=True)
    return rows


def grid(cfg: dict, prices, vix, start="2010-01-01") -> list[dict]:
    """参数敏感性：预算 × RSI 阈值。"""
    out = []
    for budget in (28, 50, 100):
        for lo, hi in ((30, 70), (35, 65), (40, 60)):
            c = copy.deepcopy(cfg)
            c["account"]["budget_usd"] = budget
            c["signal"]["rsi_oversold"], c["signal"]["rsi_overbought"] = lo, hi
            s = run(c, prices, vix, start)["stats"]
            out.append({"budget": budget, "rsi": f"{lo}/{hi}", **{k: s[k] for k in (
                "trades", "win_rate", "total_pnl", "profit_factor", "max_drawdown", "max_losing_streak")}})
    return out
