"""本地可视化控制台：python run.py dashboard → 浏览器打开 http://127.0.0.1:8765

只监听本机。页面能做的事：
  - 看：纪律锁状态、熔断、模拟盘/实盘持仓与挂单、交易日志、最近信号、回测结果
  - 做：扫描信号（只读）、跑一轮模拟盘、打开/关闭 STOP（停止开新仓）、更新策略库（只读研究）
  - 「行情与策略库」页：行情分类、34 条策略的回测与今日状态、止损实验、期权结构（数据来自 backtest/library.json）
页面里刻意不提供实盘下单、锁定/解锁——这些只能在终端里做。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import threading
import tomllib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .discipline import Discipline

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
_busy = threading.Lock()


def _cfg() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text("utf-8"))


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def snapshot() -> dict:
    cfg = _cfg()
    d = Discipline(ROOT, cfg)
    lk = d.lock_state()
    text = (ROOT / "config.toml").read_text("utf-8")
    envs = {}
    for env in ("simulate", "real"):
        s = _read_json(ROOT / f"state_{env}.json", {"positions": [], "pending": [], "closed_trades": []})
        trades = s.get("closed_trades", [])
        envs[env] = {"positions": s.get("positions", []), "pending": s.get("pending", []),
                     "closed_trades": trades, "realized": round(sum(t["pnl"] for t in trades), 2),
                     "breaker": s.get("breaker_tripped")}
    journal = []
    jf = ROOT / "journal.csv"
    if jf.exists():
        with jf.open(encoding="utf-8-sig") as f:
            journal = list(csv.DictReader(f))[-200:][::-1]
    bt = _read_json(ROOT / "backtest" / "results.json", None)
    if bt:   # 控制台不需要逐日明细之外的大块数据
        bt["base"]["trades"] = bt["base"]["trades"][-300:]
    return {
        "now": dt.datetime.now().isoformat(timespec="seconds"),
        "lock": {"locked": lk["locked"], "reason": lk["reason"],
                 "config_changed": bool(d.config_violation(text))},
        "stop": (ROOT / "STOP").exists(),
        "discipline": cfg.get("discipline", {}),
        "account": cfg["account"], "universe": cfg["universe"]["underlyings"],
        "unattended": not cfg["execution"]["require_confirm_live"],
        "envs": envs, "journal": journal,
        "signals": _read_json(ROOT / "signals.json", None),
        "backtest": bt,
    }


def do_scan() -> dict:
    from .cli import Cli, CliMarket
    from .strategy import make_signal, pick_spread
    cfg = _cfg()
    m = CliMarket(Cli(cfg))
    sel = cfg["selection"]
    out = []
    for und in cfg["universe"]["underlyings"]:
        closes = m.daily_closes(und)
        sig = make_signal(closes, cfg)
        item = {"underlying": und, "direction": sig.direction, "reasons": sig.reasons,
                "values": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in sig.values.items()},
                "closes": [round(c, 2) for c in closes[-130:]]}
        rows = m.option_rows(und, sel["min_dte"], sel["max_dte"], sel["strike_window_pct"])
        sp, note = pick_spread(sig.direction or "bull", und, rows, cfg)
        item["spread_note"] = note
        item["spread"] = sp.describe() if sp else None
        item["spread_is_demo"] = sig.direction is None
        out.append(item)
    res = {"time": dt.datetime.now().isoformat(timespec="seconds"), "items": out}
    (ROOT / "signals.json").write_text(json.dumps(res, ensure_ascii=False), "utf-8")
    return {"ok": True, "message": "扫描完成（只读，没有下单）"}


def do_paper_once() -> dict:
    from .cli import Cli, CliMarket, CliSim, Combined
    from .engine import Engine
    cfg = _cfg()
    cli = Cli(cfg)
    market = CliMarket(cli)
    logs: list[str] = []
    b = Combined(market, CliSim(cli, market, cfg["cli"]["sim_account"], log=logs.append))
    Engine(cfg, b, live=False, log=logs.append, confirm=lambda *_: "no").run_once()
    return {"ok": True, "message": "\n".join(logs) or "完成"}


def do_library() -> dict:
    from . import library
    return library.refresh(_cfg())


ACTIONS = {
    "scan": do_scan,
    "library": do_library,
    "paper_once": do_paper_once,
    "stop_on": lambda: ((ROOT / "STOP").write_text(""), {"ok": True, "message": "已创建 STOP：不再开新仓（止盈止损照常）"})[1],
    "stop_off": lambda: ((ROOT / "STOP").unlink(missing_ok=True), {"ok": True, "message": "已删除 STOP：恢复开新仓"})[1],
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400" if ctype == "image/png" else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            try:
                return self._json(snapshot())
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if self.path.startswith("/brand/"):   # 玉衡标志素材（只认 brand 目录下的 png）
            name = self.path.removeprefix("/brand/").split("?")[0]
            p = STATIC / "brand" / name
            if "/" not in name and "\\" not in name and name.endswith(".png") and p.is_file():
                return self._send(200, p.read_bytes(), "image/png")
            return self._send(404, b"not found", "text/plain")
        if self.path == "/api/library":
            p = ROOT / "backtest" / "library.json"
            if not p.exists():
                return self._json({"missing": True})
            return self._send(200, p.read_bytes(), "application/json; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # 只接受本页面发起的请求（防止其他网站借你的浏览器调用本机接口）
        if self.headers.get("X-Optbot") != "1":
            return self._json({"ok": False, "message": "forbidden"}, 403)
        name = self.path.removeprefix("/api/action/")
        fn = ACTIONS.get(name)
        if not fn:
            return self._json({"ok": False, "message": f"未知操作 {name}"}, 404)
        if not _busy.acquire(blocking=False):
            return self._json({"ok": False, "message": "上一个操作还在运行，请稍等"}, 409)
        try:
            return self._json(fn())
        except Exception as e:
            return self._json({"ok": False, "message": f"出错：{e}"}, 500)
        finally:
            _busy.release()


def serve(cfg: dict, port: int = 8765, open_browser: bool = True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"控制台已启动：{url}  （Ctrl+C 关闭）")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已关闭。")
