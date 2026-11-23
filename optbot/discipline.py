"""纪律锁：把「事先定好的规则」变成程序强制执行、本人也不能随手改的东西。

1. 规则锁定：lock 之后记下 config.toml 的指纹；锁定期内配置被改动 → 机器人拒绝运行。
2. 解锁冷静期：提前解锁要先申请，满 N 小时（默认 72）才生效。
3. 亏损熔断：累计已实现亏损达到上限 → 永久停止开新仓（平仓照常）。只能在未锁定时手动复位。
4. 连亏暂停：连续亏损 N 笔 → 暂停开新仓 M 天。
5. 手动干预检测：券商端的期权持仓和机器人记录对不上 → 停止开新仓。
6. 无人值守实盘（不需要每笔输入 YES）只允许在锁定状态下开启。

注意：任何程序都拦不住你直接关掉它、或者在 app 里手动下单。这层锁的作用是让「冲动」需要额外的步骤和等待时间。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


class Discipline:
    def __init__(self, root: Path, cfg: dict, now=dt.datetime.now):
        self.root, self.cfg, self.now = root, cfg, now
        self.d = cfg.get("discipline", {})
        self.lock_file = root / "lock.json"

    # ------------------------------------------------------------ 锁定

    def _read(self) -> dict | None:
        return json.loads(self.lock_file.read_text("utf-8")) if self.lock_file.exists() else None

    def _write(self, data: dict):
        self.lock_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def lock_state(self) -> dict:
        """返回 {"locked": bool, "reason": str, ...}，并清理已经到期 / 解锁生效的锁。"""
        lk = self._read()
        if not lk:
            return {"locked": False, "reason": "未锁定"}
        now = self.now()
        until = dt.datetime.fromisoformat(lk["until"])
        if now >= until:
            self.lock_file.unlink()
            return {"locked": False, "reason": f"锁定期已于 {until:%Y-%m-%d %H:%M} 结束"}
        req = lk.get("unlock_requested_at")
        if req:
            eff = dt.datetime.fromisoformat(req) + dt.timedelta(hours=self.d.get("unlock_cooldown_hours", 72))
            if now >= eff:
                self.lock_file.unlink()
                return {"locked": False, "reason": f"提前解锁已于 {eff:%Y-%m-%d %H:%M} 生效"}
            return {"locked": True, "until": until, "unlock_effective": eff, **lk,
                    "reason": f"锁定中，解锁申请将在 {eff:%Y-%m-%d %H:%M} 生效"}
        return {"locked": True, "until": until, **lk, "reason": f"锁定中，到 {until:%Y-%m-%d %H:%M}"}

    def lock(self, config_text: str, days: int | None = None) -> str:
        st = self.lock_state()
        if st["locked"]:
            return f"已经锁定：{st['reason']}"
        days = days or self.d.get("lock_days", 30)
        until = self.now() + dt.timedelta(days=days)
        self._write({"locked_at": self.now().isoformat(timespec="seconds"),
                     "until": until.isoformat(timespec="seconds"),
                     "config_hash": fingerprint(config_text), "unlock_requested_at": None})
        return f"🔒 规则已锁定 {days} 天，到 {until:%Y-%m-%d %H:%M}。期间修改 config.toml 机器人会拒绝运行。"

    def request_unlock(self) -> str:
        lk = self._read()
        st = self.lock_state()
        if not st["locked"]:
            return f"当前没有锁定（{st['reason']}）"
        if lk.get("unlock_requested_at"):
            return st["reason"]
        lk["unlock_requested_at"] = self.now().isoformat(timespec="seconds")
        self._write(lk)
        h = self.d.get("unlock_cooldown_hours", 72)
        return (f"已申请提前解锁，冷静期 {h} 小时，"
                f"{self.now() + dt.timedelta(hours=h):%Y-%m-%d %H:%M} 生效。期间规则仍然锁定。")

    def config_violation(self, config_text: str) -> str | None:
        st = self.lock_state()
        if st["locked"] and fingerprint(config_text) != st["config_hash"]:
            return ("❌ 规则锁定期间 config.toml 被改动了，机器人拒绝运行。\n"
                    "   要么把 config.toml 改回锁定时的样子，要么运行 `run.py unlock` 申请解锁并等冷静期结束。")
        return None

    def unattended_violation(self, live: bool) -> str | None:
        if live and not self.cfg["execution"]["require_confirm_live"] and not self.lock_state()["locked"]:
            return ("❌ 无人值守实盘（require_confirm_live = false）必须先锁定规则：运行 `run.py lock`。\n"
                    "   否则你随时可以改规则，就失去了交给程序执行的意义。")
        return None

    # ------------------------------------------------------------ 风控闸门（只拦开新仓，平仓永远放行）

    def entry_block(self, state: dict, broker_option_codes: set[str] | None) -> str | None:
        if state.get("breaker_tripped"):
            return f"⛔ 亏损熔断已触发（{state['breaker_tripped']}），不再开新仓。平仓照常执行。"

        trades = state.get("closed_trades", [])
        loss = -sum(t["pnl"] for t in trades)
        cap = self.d.get("max_total_loss_usd")
        if cap is not None and loss >= cap:
            state["breaker_tripped"] = f"{self.now():%Y-%m-%d %H:%M} 累计亏损 ${loss:.2f} ≥ 上限 ${cap:.2f}"
            return f"⛔ 亏损熔断：累计亏损 ${loss:.2f} 已达上限 ${cap:.2f}，从现在起不再开新仓。"

        n = self.d.get("max_consecutive_losses", 2)
        streak = 0
        for t in reversed(trades):
            if t["pnl"] < 0:
                streak += 1
            else:
                break
        if n and streak >= n:
            last = dt.datetime.fromisoformat(trades[-1]["closed_at"])
            resume = last + dt.timedelta(days=self.d.get("pause_days_after_streak", 5))
            if self.now() < resume:
                return f"⏸ 已连续亏损 {streak} 笔，暂停开新仓到 {resume:%Y-%m-%d %H:%M}。"

        if self.d.get("check_manual_trades", True) and broker_option_codes is not None:
            mine = {c for p in state.get("positions", []) for c in (p["long_code"], p["short_code"])}
            mine |= {c for p in state.get("pending", []) for c in (p["spread"]["long_code"], p["spread"]["short_code"])}
            extra, missing = broker_option_codes - mine, {c for p in state.get("positions", [])
                                                         for c in (p["long_code"], p["short_code"])} - broker_option_codes
            if extra or missing:
                return ("⛔ 券商端期权持仓和机器人记录对不上（可能有手动交易），停止开新仓。"
                        f" 多出：{sorted(extra) or '无'}；缺少：{sorted(missing) or '无'}。")
        return None

    def reset_breaker(self, state: dict) -> str:
        if self.lock_state()["locked"]:
            return "❌ 锁定期间不能复位熔断。先 `run.py unlock`，等冷静期结束。"
        state.pop("breaker_tripped", None)
        state["closed_trades_archived"] = state.get("closed_trades_archived", []) + state.get("closed_trades", [])
        state["closed_trades"] = []
        return "熔断已复位，亏损累计从 0 开始（历史交易已归档）。"
