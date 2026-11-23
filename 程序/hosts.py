# -*- coding: utf-8 -*-
"""
找主机：先试局域网地址，连不上再试外网地址（Tailscale），结果缓存 1 分钟

  设置.json → 存储
    主机地址   局域网 IP（在家时最快），例如 <主机的局域网地址>
    外网地址   主机的 Tailscale 地址（100. 开头）或 Tailscale 机器名；不在家时自动走这个
"""
import socket
import time

_CACHE = {}          # (候选地址, 端口) → (时间, 地址, 线路)
TTL = 60


def _open(host, port, timeout):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def pick(lan, wan, port):
    """返回 (地址, 线路说明)。线路：局域网 / 外网（Tailscale）/ 连不上。都没填返回 (None, "没填")。"""
    lan, wan = str(lan or "").strip(), str(wan or "").strip()
    if not lan and not wan:
        return None, "没填"
    key = (lan, wan, int(port))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1], hit[2]
    if lan and _open(lan, port, 0.8):
        res = (lan, "局域网")
    elif wan and _open(wan, port, 3.0):
        res = (wan, "外网（Tailscale）")
    else:
        res = (lan or wan, "连不上")          # 照常返回一个地址，让调用方报出具体错误
    _CACHE[key] = (time.time(), res[0], res[1])
    return res


def route(lan, wan, port):
    """只看缓存里上次走的哪条线路（不去连），给状态灯用。"""
    hit = _CACHE.get((str(lan or "").strip(), str(wan or "").strip(), int(port)))
    return hit[2] if hit else None
