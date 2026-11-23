# -*- coding: utf-8 -*-
"""
调用 AI：只用云端 Grok（设置.json → Grok盘前 里的密钥和模型）。

  2026-11-17 起去掉了本地模型（主机上的 Ollama / 千问），环境卡只用 Grok 生成。
  每次调用都记进数据库 ai_calls（输入、输出、用时），复盘时能查 AI 当时为什么这么判断。
  输出一律要求是 JSON；解析失败重试一次，还不行就报错，由调用方决定怎么办。
"""
import datetime as dt
import json
import re
import time

import store

DEFAULT = {"超时_秒": 240}
SCHEMA = """CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, purpose TEXT, backend TEXT, model TEXT, ok INTEGER,
    latency_ms INTEGER, prompt TEXT, output TEXT, error TEXT)"""


def settings(cfg):
    return dict(DEFAULT)


def _log(purpose, backend, model, ok, ms, prompt, output, error=None):
    try:
        con = store.connect()
        try:
            con.execute(SCHEMA)
            with con:
                con.execute("INSERT INTO ai_calls (ts, purpose, backend, model, ok, latency_ms, prompt, output, error) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (dt.datetime.now().isoformat(timespec="seconds"), purpose, backend, model, int(ok), ms,
                             prompt, output, error))
        finally:
            con.close()
    except Exception:
        pass


def extract_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI 没有返回 JSON")
    return json.loads(m.group(0))


def _cloud(cfg, system, user, timeout):
    import grokscan
    g = grokscan.settings(cfg) if hasattr(grokscan, "settings") else dict(cfg.get("Grok盘前") or {})
    if not str(g.get("API密钥") or "").strip():
        raise RuntimeError("没有 Grok API 密钥（设置.json → Grok盘前）")
    data = grokscan._post(g, {"model": g["模型"], "input": [{"role": "system", "content": system},
                                                          {"role": "user", "content": user}]}, timeout=timeout)
    text, _, usage = grokscan.parse(data)
    return text, "grok:" + str(g["模型"])


def backend_for(cfg, prefer=None):
    """只剩云端。返回 ("云端"/None, 说明)。"""
    g = cfg.get("Grok盘前") or {}
    if not str(g.get("API密钥") or "").strip():
        return None, "没有 Grok API 密钥（设置.json → Grok盘前）"
    return "云端", "Grok"


def chat_json(cfg, purpose, system, user, schema=None, backend="云端", tries=2):
    """返回 (dict, 模型名)。失败抛异常。backend 参数只为兼容旧调用，一律走云端。"""
    timeout = int(settings(cfg).get("超时_秒") or 240)
    err = None
    for i in range(tries):
        t0 = time.time()
        text, model = "", "grok"
        try:
            sys_ = system + ("\n只输出一个 JSON 对象，符合这个 JSON Schema：" + json.dumps(schema, ensure_ascii=False)
                             if schema else "")
            text, model = _cloud(cfg, sys_, user, timeout)
            out = extract_json(text)
            _log(purpose, "云端", model, True, int((time.time() - t0) * 1000), system + "\n\n" + user, text)
            return out, model
        except Exception as e:
            err = e
            _log(purpose, "云端", model, False, int((time.time() - t0) * 1000), system + "\n\n" + user, text,
                 f"{type(e).__name__}: {e}"[:300])
    raise err
