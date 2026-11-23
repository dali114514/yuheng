# -*- coding: utf-8 -*-
"""
工作流开关与参数：设置.json →「工作流」

  单笔最大亏损_美元   用来倒推建议张数；留空（null）= 不推荐张数
  消息爆发_开盘观察分钟  开盘后前 N 分钟只观察、不分级（等第一根 15 分钟K收盘）
  消息爆发_最小止损ATR倍数  止损距离的下限，避免刚开盘时止损过窄把盈亏比算得虚高
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "设置.json"

DEFAULT = {
    "单笔最大亏损_美元": None,
    "消息爆发_开盘观察分钟": 15,
    "消息爆发_最小止损ATR倍数": 0.3,
    "说明": ("单笔最大亏损：填数字后提醒里会给出建议张数（张数 = 单笔最大亏损 ÷ 每张预计亏损），留空 null 就不给张数。"
             "消息爆发：开盘前和开盘后前 N 分钟只观察；之后按今日高低点定止损、算盈亏比，达标才分级。"),
}


def load():
    out = dict(DEFAULT)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        out.update(raw.get("工作流") or {})
    except Exception:
        pass
    return out


def num(w, key):
    """读一个数字设置；留空、0 或填错都返回 None。"""
    try:
        v = float(w.get(key))
        return v if v > 0 else None
    except Exception:
        return None
