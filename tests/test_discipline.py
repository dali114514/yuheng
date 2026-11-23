"""纪律锁测试：锁定、冷静期、熔断、连亏暂停、手动干预、STOP 不影响平仓。"""
import copy
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from optbot.discipline import Discipline  # noqa: E402
from optbot.engine import Engine  # noqa: E402
from test_bot import CFG, FakeBroker  # noqa: E402

T0 = dt.datetime(2026, 11, 23, 12, 0)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def disc(tmp_path, clock, **over):
    cfg = copy.deepcopy(CFG)
    cfg["discipline"].update(over)
    return Discipline(tmp_path, cfg, now=clock), cfg


def test_lock_blocks_config_change_and_unlock_needs_cooldown(tmp_path):
    c = Clock(T0)
    d, _ = disc(tmp_path, c)
    assert "已锁定" in d.lock("a = 1", days=30)
    assert d.config_violation("a = 1") is None
    assert d.config_violation("a = 2")                        # 改了配置 → 拒绝
    assert "已经锁定" in d.lock("a = 2")                      # 不能重新锁来覆盖

    d.request_unlock()
    c.t = T0 + dt.timedelta(hours=71)
    assert d.lock_state()["locked"] and d.config_violation("a = 2")
    c.t = T0 + dt.timedelta(hours=72, minutes=1)
    assert not d.lock_state()["locked"] and d.config_violation("a = 2") is None


def test_lock_expires(tmp_path):
    c = Clock(T0)
    d, _ = disc(tmp_path, c)
    d.lock("x", days=1)
    c.t = T0 + dt.timedelta(days=1, minutes=1)
    assert not d.lock_state()["locked"]


def test_unattended_live_requires_lock(tmp_path):
    c = Clock(T0)
    d, cfg = disc(tmp_path, c)
    cfg["execution"]["require_confirm_live"] = False
    assert d.unattended_violation(live=True)
    assert d.unattended_violation(live=False) is None
    d.lock("x")
    assert d.unattended_violation(live=True) is None


def trade(pnl, t):
    return {"pnl": pnl, "closed_at": t.isoformat(), "underlying": "US.SPY"}


def test_loss_breaker_trips_and_stays(tmp_path):
    c = Clock(T0)
    d, _ = disc(tmp_path, c, max_total_loss_usd=28, max_consecutive_losses=0)
    st = {"positions": [], "pending": [], "closed_trades": [trade(-15, T0), trade(5, T0), trade(-20, T0)]}
    assert "熔断" in d.entry_block(st, set())
    st["closed_trades"].append(trade(100, T0))                # 之后再赚回来也不自动解除
    assert "熔断" in d.entry_block(st, set())
    d.lock("x")
    assert "❌" in d.reset_breaker(st)                         # 锁定期内不能复位


def test_consecutive_losses_pause(tmp_path):
    c = Clock(T0)
    d, _ = disc(tmp_path, c, max_total_loss_usd=1000, max_consecutive_losses=2, pause_days_after_streak=5)
    st = {"positions": [], "pending": [], "closed_trades": [trade(-3, T0), trade(-4, T0)]}
    assert "暂停" in d.entry_block(st, set())
    c.t = T0 + dt.timedelta(days=5, minutes=1)
    assert d.entry_block(st, set()) is None


def test_manual_trade_detected(tmp_path):
    d, _ = disc(tmp_path, Clock(T0), max_total_loss_usd=1000)
    st = {"positions": [{"long_code": "US.A", "short_code": "US.B"}], "pending": [], "closed_trades": []}
    assert d.entry_block(st, {"US.A", "US.B"}) is None
    assert "对不上" in d.entry_block(st, {"US.A", "US.B", "US.SPY261224C744000"})
    assert "对不上" in d.entry_block(st, {"US.A"})
    assert d.entry_block(st, None) is None                    # 读不到持仓时跳过


# ---------------------------------------------------------------- 接入引擎

def engine(tmp_path, **over):
    cfg = copy.deepcopy(CFG)
    cfg["discipline"].update(over)
    return Engine(cfg, FakeBroker(), live=False, log=lambda *_: None, root=tmp_path)


def test_stop_file_still_runs_exits(tmp_path):
    e = engine(tmp_path, max_total_loss_usd=1000)
    e.run_once(force_window=True)
    e.run_once(force_window=True)                             # 开仓成交
    assert len(e.state["positions"]) == 1
    (tmp_path / "STOP").write_text("")
    e.b.value = 0.01                                          # 跌到止损线以下
    n_before = e.b.n
    e.run_once(force_window=True)
    assert e.b.n == n_before + 1                              # 平仓单照样发出
    e.run_once(force_window=True)
    assert e.state["positions"] == [] and e.b.n == n_before + 1   # 平掉后不开新仓


def test_engine_records_pnl_and_trips_breaker(tmp_path):
    e = engine(tmp_path, max_total_loss_usd=20, max_consecutive_losses=0)
    e.run_once(force_window=True)
    e.run_once(force_window=True)
    e.b.value = 0.01
    e.run_once(force_window=True)                             # 发平仓
    e.run_once(force_window=True)                             # 平仓成交 → 记亏损 → 熔断
    t = e.state["closed_trades"][-1]
    assert t["pnl"] < -20
    assert e.state.get("breaker_tripped")
    n = e.b.n
    e.run_once(force_window=True)
    assert e.b.n == n                                         # 熔断后不再开仓


def test_engine_refuses_when_config_changed(tmp_path):
    (tmp_path / "config.toml").write_text("v = 1", "utf-8")
    Discipline(tmp_path, CFG).lock("v = 1")
    (tmp_path / "config.toml").write_text("v = 2", "utf-8")
    e = engine(tmp_path)
    e.run_once(force_window=True)
    assert e.b.n == 0
