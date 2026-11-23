"""moomoo OpenAPI 的薄封装。所有和券商打交道的代码都在这里。

需要本机先运行 OpenD 并登录 moomoo 账号（见 README）。
接口文档：https://openapi.moomoo.com/moomoo-api-doc/en/
"""
from __future__ import annotations

import datetime as dt
import time

from moomoo import (RET_OK, ComboLeg, Currency, KLType, ModifyOrderOp, OpenQuoteContext,
                    OpenSecTradeContext, OptionType, OrderType, SecurityFirm, TrdEnv,
                    TrdMarket, TrdSide)


class BrokerError(RuntimeError):
    pass


def _ok(ret, data, what: str):
    if ret != RET_OK:
        raise BrokerError(f"{what} 失败：{data}")
    return data


class Moomoo:
    def __init__(self, cfg: dict, live: bool):
        c = cfg["connection"]
        self.env = TrdEnv.REAL if live else TrdEnv.SIMULATE
        self.quote = OpenQuoteContext(host=c["host"], port=c["port"])
        self.trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=c["host"], port=c["port"],
                                         security_firm=getattr(SecurityFirm, c["security_firm"]))

    def close(self):
        self.quote.close()
        self.trade.close()

    # ------------------------------------------------------------ 行情

    def daily_closes(self, code: str, bars: int = 260) -> list[float]:
        end = dt.date.today()
        start = end - dt.timedelta(days=int(bars * 1.6))
        closes, key = [], None
        while True:
            ret, data, key = self.quote.request_history_kline(
                code, start=start.isoformat(), end=end.isoformat(),
                ktype=KLType.K_DAY, max_count=1000, page_req_key=key)
            data = _ok(ret, data, f"{code} 日K线")
            closes += [float(x) for x in data["close"].tolist()]
            if key is None:
                break
        return closes

    def snapshot(self, codes: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(codes), 400):          # 接口单次最多 400 个
            ret, data = self.quote.get_market_snapshot(codes[i:i + 400])
            out += _ok(ret, data, "行情快照").to_dict("records")
            if i + 400 < len(codes):
                time.sleep(1)
        return out

    def spot(self, code: str) -> float:
        return float(self.snapshot([code])[0]["last_price"])

    def option_rows(self, underlying: str, min_dte: int, max_dte: int, window_pct: float) -> list[dict]:
        """返回到期日在 [min_dte, max_dte] 内、行权价在现价 ±window_pct 内的所有期权（含 Greeks、买卖价）。"""
        spot = self.spot(underlying)
        exps = _ok(*self.quote.get_option_expiration_date(underlying), f"{underlying} 到期日")
        exps = exps[(exps["option_expiry_date_distance"] >= min_dte) &
                    (exps["option_expiry_date_distance"] <= max_dte)]
        codes = []
        for exp in exps["strike_time"].tolist():
            time.sleep(3)                             # 期权链接口有频率限制
            chain = _ok(*self.quote.get_option_chain(underlying, start=exp, end=exp,
                                                     option_type=OptionType.ALL), f"{underlying} {exp} 期权链")
            chain = chain[(chain["strike_price"] >= spot * (1 - window_pct)) &
                          (chain["strike_price"] <= spot * (1 + window_pct))]
            codes += chain["code"].tolist()
        if not codes:
            return []
        rows = []
        for s in self.snapshot(codes):
            if not s.get("option_valid"):
                continue
            rows.append({
                "code": s["code"],
                "option_type": str(s["option_type"]).upper(),
                "strike": float(s["option_strike_price"]),
                "expiry": str(s.get("strike_time", ""))[:10],
                "dte": int(s["option_expiry_date_distance"]),
                "bid": float(s["bid_price"] or 0),
                "ask": float(s["ask_price"] or 0),
                "delta": float(s["option_delta"] or 0),
                "iv": float(s["option_implied_volatility"] or 0),
                "oi": int(s["option_open_interest"] or 0),
            })
        return rows

    def spread_value(self, long_code: str, short_code: str) -> tuple[float, int]:
        """价差当前每股中间价价值、剩余天数。"""
        snap = {s["code"]: s for s in self.snapshot([long_code, short_code])}
        mid = lambda s: (float(s["bid_price"] or 0) + float(s["ask_price"] or 0)) / 2
        return round(mid(snap[long_code]) - mid(snap[short_code]), 2), int(snap[long_code]["option_expiry_date_distance"])

    # ------------------------------------------------------------ 账户

    def account(self) -> dict:
        data = _ok(*self.trade.accinfo_query(trd_env=self.env, currency=Currency.USD), "账户资金")
        return data.to_dict("records")[0]

    def positions(self) -> list[dict]:
        return _ok(*self.trade.position_list_query(trd_env=self.env), "持仓").to_dict("records")

    # ------------------------------------------------------------ 下单

    def place_spread(self, long_code: str, short_code: str, open_: bool, price: float, qty: int) -> str:
        """开仓：买 long 卖 short；平仓：卖 long 买 short。price 为每股净价（限价单）。"""
        l1, l2 = ComboLeg(), ComboLeg()
        l1.code, l1.qty_ratio = long_code, 1
        l2.code, l2.qty_ratio = short_code, 1
        l1.trd_side = TrdSide.BUY if open_ else TrdSide.SELL
        l2.trd_side = TrdSide.SELL if open_ else TrdSide.BUY
        data = _ok(*self.trade.place_combo_order([l1, l2], price=price, qty=qty,
                                                 order_type=OrderType.NORMAL, trd_env=self.env,
                                                 remark="optbot"), "组合下单")
        return str(data["order_id"].iloc[0])

    def order_info(self, order_id: str) -> dict | None:
        ret, data = self.trade.order_list_query(order_id=order_id, trd_env=self.env)
        if ret != RET_OK or len(data) == 0:
            return None
        return data.to_dict("records")[0]

    def cancel(self, order_id: str):
        _ok(*self.trade.modify_order(ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=self.env), "撤单")
