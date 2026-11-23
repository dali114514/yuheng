"""一次「检查 → 管理持仓 → 找新机会 → 下单」的完整循环。

broker 只要实现 Moomoo 同名方法即可（测试里用假券商代替）。
状态存在 state_simulate.json / state_real.json，交易日志追加到 journal.csv。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .discipline import Discipline
from .strategy import exit_decision, make_signal, pick_spread

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent


class Engine:
    def __init__(self, cfg: dict, broker, live: bool, confirm=input, log=print, root: Path = ROOT):
        self.cfg, self.b, self.live, self.confirm, self.log = cfg, broker, live, confirm, log
        self.root = root
        self.state_file = root / f"state_{'real' if live else 'simulate'}.json"
        self.journal_file = root / "journal.csv"
        self.state = json.loads(self.state_file.read_text("utf-8")) if self.state_file.exists() \
            else {"positions": [], "pending": []}
        self.state.setdefault("closed_trades", [])
        self.disc = Discipline(root, cfg)
        cfg_file = root / "config.toml"
        self.config_text = cfg_file.read_text("utf-8") if cfg_file.exists() else None

    # ------------------------------------------------------------ 工具

    @property
    def env_name(self):
        return "实盘" if self.live else "模拟盘"

    def save(self):
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")

    def journal(self, **row):
        row = {"time": dt.datetime.now().isoformat(timespec="seconds"), "env": self.env_name, **row}
        new = not self.journal_file.exists()
        with self.journal_file.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["time", "env", "action", "underlying", "direction",
                                              "long_code", "short_code", "qty", "price", "order_id",
                                              "reason"], extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def in_trade_window(self, now_et: dt.datetime | None = None) -> bool:
        now_et = now_et or dt.datetime.now(ET)
        if now_et.weekday() >= 5:
            return False
        ex = self.cfg["execution"]
        start = dt.time.fromisoformat(ex["trade_start_et"])
        end = dt.time.fromisoformat(ex["trade_end_et"])
        return start <= now_et.time() <= end

    def stopped(self) -> bool:
        return (self.root / "STOP").exists()

    def ok_to_send(self, text: str) -> bool:
        """实盘下单前的人工确认。模拟盘直接放行。"""
        if not self.live or not self.cfg["execution"]["require_confirm_live"]:
            return True
        self.log("\n" + "=" * 60 + f"\n【实盘下单确认】\n{text}\n" + "=" * 60)
        ans = self.confirm("确认用真钱下这笔单？输入大写 YES 确认，其它任意键取消：").strip()
        return ans == "YES"

    # ------------------------------------------------------------ 主流程

    def run_once(self, force_window: bool = False) -> None:
        for why in (self.config_text is not None and self.disc.config_violation(self.config_text),
                    self.disc.unattended_violation(self.live)):
            if why:
                self.log(why)
                return
        self.log(f"===== {self.env_name} | 美东 {dt.datetime.now(ET):%Y-%m-%d %H:%M} | "
                 f"{self.disc.lock_state()['reason']} =====")
        self.check_pending()
        self.manage_positions(force_window)          # 止盈止损永远执行，不受 STOP / 熔断影响
        if self.stopped():
            self.log("发现 STOP 文件：不开新仓（已有持仓的止盈止损照常执行）。删除 STOP 文件后恢复。")
        else:
            block = self.disc.entry_block(self.state, self._broker_option_codes())
            if block:
                self.log(block)
            else:
                self.look_for_entry(force_window)
        self.save()

    def _broker_option_codes(self) -> set[str] | None:
        """券商端当前的期权持仓代码，用于发现手动交易。读不到就返回 None（跳过这项检查）。"""
        if not hasattr(self.b, "positions"):
            return None
        try:
            out = set()
            for p in self.b.positions():
                code = str(p.get("code") or p.get("symbol") or "")
                qty = float(p.get("qty") or 0)
                if qty and len(code.split(".", 1)[-1]) > 12:      # 期权代码很长，如 SPY261224C744000
                    out.add(code)
            return out
        except Exception as e:
            self.log(f"（读取券商持仓失败，跳过手动交易检查：{e}）")
            return None

    def check_pending(self):
        """处理还没成交的挂单：成交了转为持仓，超时没成交就撤单。"""
        keep = []
        for p in self.state["pending"]:
            info = self.b.order_info(p["order_id"])
            status = str(info.get("order_status", "")) if info else "UNKNOWN"
            filled = info and float(info.get("dealt_qty") or 0) >= p["qty"]
            age_min = (dt.datetime.now() - dt.datetime.fromisoformat(p["placed_at"])).total_seconds() / 60
            if filled:
                px = float(info.get("dealt_avg_price") or 0) or p["price"]
                if p["kind"] == "open":
                    pos = dict(p["spread"], entry_debit=px, qty=p["qty"], opened_at=p["placed_at"])
                    self.state["positions"].append(pos)
                    self.log(f"✅ 开仓成交：{pos['underlying']} 净支出 ${px:.2f}/股")
                else:
                    self.state["positions"] = [x for x in self.state["positions"]
                                               if x["long_code"] != p["spread"]["long_code"]]
                    entry = p["spread"].get("entry_debit", 0.0)
                    fees = self.cfg["account"]["fee_per_contract"] * 4 * p["qty"]   # 开平各两条腿
                    pnl = round((px - entry) * 100 * p["qty"] - fees, 2)
                    self.state["closed_trades"].append({"underlying": p["spread"]["underlying"],
                                                        "entry": entry, "exit": px, "qty": p["qty"],
                                                        "pnl": pnl, "closed_at": dt.datetime.now().isoformat()})
                    self.log(f"✅ 平仓成交：{p['spread']['underlying']} 卖出 ${px:.2f}/股，"
                             f"这笔盈亏 ${pnl:+.2f}（含手续费估算）")
                self.journal(action=f"{p['kind']}_filled", price=px, order_id=p["order_id"],
                             qty=p["qty"], **_legs(p["spread"]))
            elif "CANCEL" in status or "FAIL" in status or "DELETE" in status:
                self.log(f"挂单 {p['order_id']} 状态 {status}，移除")
                self.journal(action=f"{p['kind']}_dead", order_id=p["order_id"], reason=status, **_legs(p["spread"]))
            elif age_min >= self.cfg["execution"]["cancel_after_min"]:
                self.log(f"挂单 {p['order_id']} 已挂 {age_min:.0f} 分钟未成交，撤单")
                try:
                    self.b.cancel(p["order_id"])
                except Exception as e:  # 撤单失败也记录，下轮再看
                    self.log(f"撤单失败：{e}")
                    keep.append(p)
                    continue
                self.journal(action=f"{p['kind']}_cancelled", order_id=p["order_id"],
                             reason="超时未成交", **_legs(p["spread"]))
            else:
                self.log(f"挂单 {p['order_id']} 等待成交（{status}，已 {age_min:.0f} 分钟）")
                keep.append(p)
        self.state["pending"] = keep

    def manage_positions(self, force_window: bool):
        pending_legs = {p["spread"]["long_code"] for p in self.state["pending"]}
        for pos in self.state["positions"]:
            if pos["long_code"] in pending_legs:
                continue
            closes = self.b.daily_closes(pos["underlying"])
            sig = make_signal(closes, self.cfg)
            value, dte = self.b.spread_value(pos["long_code"], pos["short_code"])
            do_exit, why = exit_decision(pos, value, dte, sig.direction, self.cfg)
            pnl = (value - pos["entry_debit"]) * 100 * pos["qty"]
            self.log(f"[持仓] {pos['underlying']} {pos['direction']} 浮动盈亏约 ${pnl:+.0f} | {why}")
            if not do_exit:
                continue
            if not (force_window or self.in_trade_window()):
                self.log("  不在交易时间窗口，下轮再平仓")
                continue
            price = max(round(value, 2), 0.01)
            if not self.ok_to_send(f"平仓 {pos['underlying']} 价差，限价 ${price:.2f}/股 × {pos['qty']} 组\n原因：{why}"):
                self.log("  已取消")
                continue
            oid = self.b.place_spread(pos["long_code"], pos["short_code"], open_=False, price=price, qty=pos["qty"])
            self.state["pending"].append({"kind": "close", "order_id": oid, "qty": pos["qty"], "price": price,
                                          "placed_at": dt.datetime.now().isoformat(), "spread": _legs(pos)
                                          | {"underlying": pos["underlying"], "direction": pos["direction"],
                                             "entry_debit": pos["entry_debit"]}})
            self.journal(action="close_sent", price=price, order_id=oid, qty=pos["qty"], reason=why, **_legs(pos))

    def look_for_entry(self, force_window: bool):
        cap = self.cfg["account"]["max_open_positions"]
        busy = len(self.state["positions"]) + sum(1 for p in self.state["pending"] if p["kind"] == "open")
        if busy >= cap:
            self.log(f"[开仓] 已有 {busy} 组持仓/挂单，达到上限 {cap}，不再开新仓")
            return
        held = {p["underlying"] for p in self.state["positions"]} | \
               {p["spread"]["underlying"] for p in self.state["pending"]}

        sel = self.cfg["selection"]
        for und in self.cfg["universe"]["underlyings"]:
            if und in held:
                continue
            sig = make_signal(self.b.daily_closes(und), self.cfg)
            self.log(f"[信号] {und}: {sig.direction or '无'} | " + "；".join(sig.reasons))
            if not sig.direction:
                continue
            rows = self.b.option_rows(und, sel["min_dte"], sel["max_dte"], sel["strike_window_pct"])
            sp, note = pick_spread(sig.direction, und, rows, self.cfg)
            self.log("  " + note)
            if not sp:
                continue
            self.log("  " + sp.describe().replace("\n", "\n  "))
            if not (force_window or self.in_trade_window()):
                self.log("  不在交易时间窗口（美东 "
                         f"{self.cfg['execution']['trade_start_et']}-{self.cfg['execution']['trade_end_et']}），只展示不下单")
                return
            if not self.ok_to_send(sp.describe() + "\n理由：" + "；".join(sig.reasons)):
                self.log("  已取消")
                return
            oid = self.b.place_spread(sp.long_code, sp.short_code, open_=True, price=sp.debit, qty=1)
            spread = {"underlying": und, "direction": sp.direction, "expiry": sp.expiry,
                      "long_code": sp.long_code, "short_code": sp.short_code,
                      "long_strike": sp.long_strike, "short_strike": sp.short_strike, "width": sp.width}
            self.state["pending"].append({"kind": "open", "order_id": oid, "qty": 1, "price": sp.debit,
                                          "placed_at": dt.datetime.now().isoformat(), "spread": spread})
            self.journal(action="open_sent", underlying=und, direction=sp.direction, long_code=sp.long_code,
                         short_code=sp.short_code, qty=1, price=sp.debit, order_id=oid,
                         reason="；".join(sig.reasons))
            self.log(f"  📨 已发送限价单 {oid}（${sp.debit:.2f}/股），等待成交")
            return   # 一轮最多开一笔


def _legs(d: dict) -> dict:
    return {k: d[k] for k in ("underlying", "direction", "long_code", "short_code") if k in d}
