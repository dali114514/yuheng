"""通过 moomoo CLI（OAuth，不需要 OpenD）拿行情、在模拟账户下单。

为什么不用 OpenD：在这台电脑上，OpenD 的 API 没有美股实时行情权限（订阅会被拒），
而且看不到模拟账户；moomoo CLI 两样都有。
CLI 文档：~/.agents/skills/moomoo-quote 与 moomoo-sim-trading 下的 references/cli-parameters.md
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path


class CliError(RuntimeError):
    pass


def _g(d: dict, *keys, default=None):
    """同时兼容 snake_case / camelCase 字段名。"""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


class Cli:
    def __init__(self, cfg: dict):
        c = cfg.get("cli", {})
        self.exe = os.path.expandvars(c.get("path", r"%LOCALAPPDATA%\MoomooCLI\bin\moomoo.exe"))
        if not Path(self.exe).exists():
            raise CliError(f"找不到 moomoo CLI：{self.exe}（见 README 第 3 步安装）")
        self.env = {**os.environ, "GODEBUG": "netdns=go"}   # 绕开 Windows DNS 接口的偶发错误

    def __call__(self, *args: str, retries: int = 2):
        cmd = [self.exe, *map(str, args), "--format", "json"]
        last = ""
        for i in range(retries + 1):
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=60)
            if p.returncode == 0:
                out = p.stdout.strip()
                return json.loads(out) if out else {}
            last = (p.stderr or p.stdout).strip().splitlines()[0] if (p.stderr or p.stdout).strip() else "未知错误"
            if "not logged in" in last or "unauthorized" in last.lower():
                raise CliError("moomoo CLI 未登录或授权过期：运行 moomoo auth login")
            time.sleep(1.5 * (i + 1))
        raise CliError(f"moomoo {' '.join(args[:3])} 失败：{last}")


# ---------------------------------------------------------------- 行情

class CliMarket:
    def __init__(self, cli: Cli):
        self.cli = cli

    def daily_closes(self, code: str, bars: int = 260) -> list[float]:
        start = (dt.date.today() - dt.timedelta(days=int(bars * 1.6))).isoformat()
        d = self.cli("history-kline", code, "--start", start, "--num", 370)
        rows = _g(d.get("data", d), "kline_list", default=[])
        rows.sort(key=lambda r: r.get("date") or r.get("time_key") or 0)
        return [float(_g(r, "close", "close_price")) for r in rows]

    def snapshot(self, codes: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(codes), 150):
            d = self.cli("snapshot", *codes[i:i + 150])
            out += d.get("snapshot_list", d if isinstance(d, list) else [])
        return out

    def spot(self, code: str) -> float:
        return float(self.snapshot([code])[0]["last_price"])

    def option_rows(self, underlying: str, min_dte: int, max_dte: int, window_pct: float) -> list[dict]:
        spot = self.spot(underlying)
        today = dt.date.today()
        d = self.cli("option", "chain", underlying,
                     "--start", (today + dt.timedelta(days=min_dte)).isoformat(),
                     "--end", (today + dt.timedelta(days=max_dte)).isoformat())
        chain = d.get("option_chain", [])
        codes = [c["code"] for c in chain
                 if spot * (1 - window_pct) <= float(c["strike_price"]) <= spot * (1 + window_pct)
                 and c.get("option_standard_type", "STANDARD") == "STANDARD"]
        rows = []
        for s in self.snapshot(codes):
            if not s.get("option_valid"):
                continue
            rows.append({
                "code": s["code"],
                "option_type": str(s.get("option_type", "")).upper(),
                "strike": float(s["option_strike_price"]),
                "expiry": _expiry_from_code(s["code"]),
                "dte": int(s["option_expiry_date_distance"]),
                "bid": float(s.get("bid_price") or 0),
                "ask": float(s.get("ask_price") or 0),
                "delta": float(s.get("delta") or 0),
                "iv": float(s.get("option_implied_volatility") or 0),
                "oi": int(s.get("option_open_interest") or 0),
            })
        return rows

    def legs_quote(self, long_code: str, short_code: str) -> dict:
        snap = {s["code"]: s for s in self.snapshot([long_code, short_code])}
        return {c: {"bid": float(snap[c].get("bid_price") or 0), "ask": float(snap[c].get("ask_price") or 0),
                    "dte": int(snap[c]["option_expiry_date_distance"])} for c in (long_code, short_code)}

    def spread_value(self, long_code: str, short_code: str) -> tuple[float, int]:
        q = self.legs_quote(long_code, short_code)
        mid = lambda c: (q[c]["bid"] + q[c]["ask"]) / 2
        return round(mid(long_code) - mid(short_code), 2), q[long_code]["dte"]


def _expiry_from_code(code: str) -> str:
    """US.SPY261224C744000 → 2026-12-24"""
    body = code.split(".", 1)[1]
    i = next(i for i, ch in enumerate(body) if ch.isdigit())
    y, m, dd = body[i:i + 2], body[i + 2:i + 4], body[i + 4:i + 6]
    return f"20{y}-{m}-{dd}"


# ---------------------------------------------------------------- 模拟账户交易

class CliSim:
    """moomoo 模拟账户。CLI 不支持组合单，所以价差拆成两条腿：
    开仓先买保护腿、成交后再卖另一条腿；平仓先买回卖出腿、再卖出保护腿。
    两条腿都用「可立即成交」的限价（买按卖一价，卖按买一价），尽量避免只成交一条腿。
    """
    MARKET = "US_OPTION"

    def __init__(self, cli: Cli, market: CliMarket, account: str, leg_wait_s: int = 30, log=print):
        self.cli, self.mkt, self.acc, self.wait, self.log = cli, market, str(account), leg_wait_s, log

    def _order(self, side: str, code: str, qty: int, price: float) -> str:
        d = self.cli("sim", "order", "place", qty, "--symbol", code, "--side", side,
                     "--account", self.acc, "--market", self.MARKET, "--type", "limit",
                     "--price", f"{price:.2f}", "--text", "optbot")
        oid = _g(d, "order_id", "orderId")
        if not oid:
            raise CliError(f"下单没有返回订单号：{d}")
        return str(oid)

    def _find(self, oid: str) -> dict | None:
        for args in (("sim", "order", "list"), ("sim", "order", "history", "--page-size", 100)):
            d = self.cli(*args, "--account", self.acc, "--market", self.MARKET)
            for o in d.get("orders", []):
                if str(_g(o, "order_id", "orderId")) == oid:
                    return o
        return None

    def _filled(self, o: dict | None) -> tuple[float, float]:
        if not o:
            return 0.0, 0.0
        return float(_g(o, "cum_qty", "cumQty", "dealt_qty", default=0)), \
            float(_g(o, "avg_fill_price", "avgFillPrice", "dealt_avg_price", default=0))

    def _wait_fill(self, oid: str, qty: int) -> bool:
        end = time.time() + self.wait
        while time.time() < end:
            time.sleep(3)
            if self._filled(self._find(oid))[0] >= qty:
                return True
        return False

    def _cancel(self, oid: str):
        try:
            self.cli("sim", "order", "cancel", oid, "--account", self.acc, "--market", self.MARKET)
        except CliError as e:
            self.log(f"撤单 {oid} 失败：{e}")

    def place_spread(self, long_code: str, short_code: str, open_: bool, price: float, qty: int) -> str:
        q = self.mkt.legs_quote(long_code, short_code)
        if open_:
            first = ("BUY", long_code, q[long_code]["ask"])
            second = ("SELL", short_code, q[short_code]["bid"])
        else:
            first = ("BUY", short_code, q[short_code]["ask"])
            second = ("SELL", long_code, q[long_code]["bid"])
        o1 = self._order(first[0], first[1], qty, first[2])
        if not self._wait_fill(o1, qty):
            self._cancel(o1)
            raise CliError(f"第一条腿 {first[0]} {first[1]} 在 {self.wait} 秒内没成交，已撤单，本次不下第二条腿")
        o2 = self._order(second[0], second[1], qty, max(second[2], 0.01))
        return f"{o1}|{o2}|{'open' if open_ else 'close'}"

    def order_info(self, oid: str) -> dict | None:
        o1, o2, kind = oid.split("|")
        (q1, p1), (q2, p2) = self._filled(self._find(o1)), self._filled(self._find(o2))
        filled = min(q1, q2)
        if kind == "open":
            px = p1 - p2           # 买入保护腿 - 卖出腿
        else:
            px = p2 - p1           # 卖出保护腿 - 买回卖出腿
        return {"order_status": "FILLED_ALL" if filled > 0 and q1 == q2 else "SUBMITTED",
                "dealt_qty": filled, "dealt_avg_price": round(px, 2) if filled else 0}

    def cancel(self, oid: str):
        o1, o2, kind = oid.split("|")
        self._cancel(o2)
        self.log("⚠️ 第二条腿超时未成交已撤单；第一条腿已成交，请用 status 查看并在 app 里处理单腿持仓")

    def account(self) -> dict:
        return self.cli("sim", "account", "funds", "--account", self.acc)

    def positions(self) -> list[dict]:
        d = self.cli("sim", "position", "--account", self.acc, "--market", self.MARKET)
        return d.get("positions", [])


class Combined:
    """行情走 CLI；交易走模拟账户（CLI）或实盘（OpenD）。"""

    def __init__(self, market: CliMarket, trader):
        self.market, self.trader = market, trader

    def __getattr__(self, name):
        for obj in (self.trader, self.market):
            if hasattr(obj, name):
                return getattr(obj, name)
        raise AttributeError(name)

    # 行情方法固定走 CLI（即使 trader 是 OpenD 也有同名方法）
    def daily_closes(self, *a, **k): return self.market.daily_closes(*a, **k)
    def option_rows(self, *a, **k): return self.market.option_rows(*a, **k)
    def spread_value(self, *a, **k): return self.market.spread_value(*a, **k)
    def spot(self, *a, **k): return self.market.spot(*a, **k)

    def close(self):
        if hasattr(self.trader, "close"):
            self.trader.close()
