"""不连 moomoo 的离线测试：策略规则 + 用假券商跑完整开仓/平仓流程。"""
import datetime as dt
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optbot.engine import ET, Engine  # noqa: E402
from optbot.strategy import exit_decision, make_signal, pick_spread, rsi, sma  # noqa: E402

CFG = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))
CFG["account"]["budget_usd"] = 35.0   # 测试用固定预算，不受你改 config.toml 影响


# ---------------------------------------------------------------- 指标

def test_sma():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1], 2) is None


def test_rsi_extremes():
    assert rsi(list(range(1, 30))) == 100.0
    assert rsi(list(range(30, 1, -1))) == pytest.approx(0.0)
    up_down = [100 + (1 if i % 2 else -1) for i in range(40)]
    assert 40 < rsi(up_down) < 60


# ---------------------------------------------------------------- 信号

def uptrend_then_dip():
    closes = [100 + i * 0.5 for i in range(200)]     # 稳定上涨，站上 MA120
    closes += [closes[-1] - 1.5 * i for i in range(1, 9)]  # 最近连跌 8 天 → RSI 超卖
    return closes


def test_signal_bull_on_oversold_dip():
    s = make_signal(uptrend_then_dip(), CFG)
    assert s.direction == "bull"
    assert s.values["rsi"] <= CFG["signal"]["rsi_oversold"]


def test_signal_bear_on_overbought_rally_in_downtrend():
    closes = [300 - i * 0.5 for i in range(200)]
    closes += [closes[-1] + 1.5 * i for i in range(1, 9)]
    assert make_signal(closes, CFG).direction == "bear"


def test_signal_needs_enough_history():
    assert make_signal([100.0] * 50, CFG).direction is None


# ---------------------------------------------------------------- 选合约

def chain(spot=500.0, dte=44, expiry="2027-01-08"):
    """造一条简化期权链：行权价每 1 美元一档，Delta 随虚值程度线性下降。"""
    # 相邻两档 call 的价差 ≈ 到期价内概率 ≈ Delta，所以用 Delta 累加出价格（接近真实 SPY 45 天期权）
    strikes = list(range(int(spot) - 40, int(spot) + 41))
    cd = {k: max(0.01, min(0.99, 0.5 - (k - spot) * 0.012)) for k in strikes}
    call_mid = {k: max(0.05, sum(cd[j] for j in strikes if j >= k)) for k in strikes}
    put_mid = {k: max(0.05, sum(1 - cd[j] for j in strikes if j < k)) for k in strikes}
    rows = []
    for k in strikes:
        for typ, d, mid in (("CALL", cd[k], call_mid[k]), ("PUT", cd[k] - 1, put_mid[k])):
            rows.append({"code": f"US.SPY{typ[0]}{k}", "option_type": typ, "strike": float(k),
                         "expiry": expiry, "dte": dte, "bid": round(mid - 0.01, 2),
                         "ask": round(mid + 0.01, 2), "delta": round(d, 3), "oi": 5000, "iv": 0.18})
    return rows


def test_pick_bull_spread_within_budget():
    sp, note = pick_spread("bull", "US.SPY", chain(), CFG)
    assert sp is not None, note
    budget = CFG["account"]["budget_usd"] - 2 * CFG["account"]["fee_per_contract"]
    assert sp.worst_debit * 100 <= budget      # 按对手价也在预算内
    assert sp.short_strike > sp.long_strike
    assert CFG["selection"]["long_delta_min"] <= abs(sp.long_delta) <= CFG["selection"]["long_delta_max"]
    assert sp.reward_risk >= CFG["selection"]["min_reward_risk"]


def test_pick_bear_spread_uses_puts_below():
    sp, note = pick_spread("bear", "US.SPY", chain(), CFG)
    assert sp is not None, note
    assert sp.short_strike < sp.long_strike
    assert "P" in sp.long_code


def test_pick_rejects_illiquid_and_wrong_dte():
    rows = chain(dte=10)
    assert pick_spread("bull", "US.SPY", rows, CFG)[0] is None
    rows = chain()
    for r in rows:
        r["oi"] = 0
    assert pick_spread("bull", "US.SPY", rows, CFG)[0] is None


def test_pick_none_when_budget_too_small():
    cfg = {**CFG, "account": {**CFG["account"], "budget_usd": 3.0}}
    assert pick_spread("bull", "US.SPY", chain(), cfg)[0] is None


# ---------------------------------------------------------------- 平仓规则

POS = {"entry_debit": 0.30, "width": 1.0, "direction": "bull"}


@pytest.mark.parametrize("value,dte,sig,exp_exit,word", [
    (0.65, 40, None, True, "止盈"),     # 0.30 + 0.5*0.70 = 0.65
    (0.15, 40, None, True, "止损"),
    (0.35, 14, None, True, "时间"),
    (0.35, 40, "bear", True, "反转"),
    (0.35, 40, "bull", False, "继续"),
])
def test_exit_rules(value, dte, sig, exp_exit, word):
    do, why = exit_decision(POS, value, dte, sig, CFG)
    assert do is exp_exit and word in why


# ---------------------------------------------------------------- 引擎（假券商）

class FakeBroker:
    def __init__(self):
        self.orders, self.value = {}, 0.30
        self.n = 0

    def daily_closes(self, code, bars=260):
        return uptrend_then_dip()

    def option_rows(self, und, a, b, c):
        return chain()

    def place_spread(self, long_code, short_code, open_, price, qty):
        self.n += 1
        oid = f"OID{self.n}"
        self.orders[oid] = {"order_status": "FILLED_ALL", "dealt_qty": qty, "dealt_avg_price": price,
                            "open": open_}
        return oid

    def order_info(self, oid):
        return self.orders.get(oid)

    def cancel(self, oid):
        self.orders[oid]["order_status"] = "CANCELLED_ALL"

    def spread_value(self, l, s):
        return self.value, 40


def make_engine(tmp_path, live=False, answer="YES"):
    logs = []
    e = Engine(CFG, FakeBroker(), live=live, confirm=lambda _: answer, log=logs.append, root=tmp_path)
    return e, logs


def test_full_cycle_paper(tmp_path):
    e, logs = make_engine(tmp_path)
    e.run_once(force_window=True)                    # 1) 有信号 → 发开仓单
    assert len(e.state["pending"]) == 1 and e.state["pending"][0]["kind"] == "open"

    e.run_once(force_window=True)                    # 2) 成交 → 变持仓；已满仓不再开新仓
    assert len(e.state["positions"]) == 1 and not any(p["kind"] == "open" for p in e.state["pending"])
    entry = e.state["positions"][0]["entry_debit"]
    width = e.state["positions"][0]["width"]

    e.b.value = round(entry + 0.6 * (width - entry), 2)  # 3) 价差涨到止盈线以上 → 发平仓单
    e.run_once(force_window=True)
    assert any(p["kind"] == "close" for p in e.state["pending"])

    e.run_once(force_window=True)                    # 4) 平仓成交 → 持仓清空（又会找新机会）
    assert e.state["positions"] == []
    text = (tmp_path / "journal.csv").read_text("utf-8-sig")
    for a in ("open_sent", "open_filled", "close_sent", "close_filled"):
        assert a in text
    assert (tmp_path / "state_simulate.json").exists()


def test_live_requires_yes(tmp_path):
    e, logs = make_engine(tmp_path, live=True, answer="no")
    e.run_once(force_window=True)
    assert e.state["pending"] == [] and e.b.n == 0
    assert any("已取消" in x for x in logs)

    (tmp_path / "x").mkdir()
    e2, _ = make_engine(tmp_path / "x", live=True, answer="YES")
    e2.run_once(force_window=True)
    assert e2.b.n == 1 and e2.state_file.name == "state_real.json"


def test_stop_file_blocks_everything(tmp_path):
    (tmp_path / "STOP").write_text("")
    e, logs = make_engine(tmp_path)
    e.run_once(force_window=True)
    assert e.b.n == 0 and any("STOP" in x for x in logs)


def test_outside_window_never_orders(tmp_path):
    e, logs = make_engine(tmp_path)
    e.in_trade_window = lambda now_et=None: False
    e.run_once()
    assert e.b.n == 0


def test_trade_window_times():
    e = Engine(CFG, FakeBroker(), live=False, log=lambda *_: None, root=Path("."))
    mon = dt.datetime(2026, 11, 23, 10, 0, tzinfo=ET)
    assert e.in_trade_window(mon)
    assert not e.in_trade_window(mon.replace(hour=9, minute=35))
    assert not e.in_trade_window(dt.datetime(2026, 11, 22, 11, 0, tzinfo=ET))  # 周日


def test_unfilled_order_cancelled_after_timeout(tmp_path):
    e, logs = make_engine(tmp_path)
    e.run_once(force_window=True)
    p = e.state["pending"][0]
    e.b.orders[p["order_id"]].update(order_status="SUBMITTED", dealt_qty=0)
    p["placed_at"] = (dt.datetime.now() - dt.timedelta(minutes=45)).isoformat()
    e.check_pending()
    assert e.state["pending"] == [] and e.b.orders[p["order_id"]]["order_status"] == "CANCELLED_ALL"
