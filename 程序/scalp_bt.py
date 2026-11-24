# -*- coding: utf-8 -*-
"""
玉衡5.0 · 散户剥头皮回测（2026-11-24）。只读 K 线库，不下单。

问题：今天预案里挑中的日内策略（开盘区间突破、缺口回补、10 点趋势延续、VWAP 收复、假突破收回、个人策略 A / B、偏离 VWAP 回归），
      如果不拿到 2R / 收盘，而是「剥头皮」——赚一点点就走、几分钟没走出来就砍——扣掉散户真实成本后还赚不赚？

散户能做到的设定：
  - 入场：信号K收盘后，下一根开盘成交（程序自动下单也差不多是这个速度；手动只会更慢）。
  - 出场：目标价挂限价单，价格要「穿过」目标才算成交（只是碰到不算）；止损按止损市价单，跳空穿过按开盘价成交；
          到了最长持有时间还没出，按那根收盘平。
  - 同一根 1 分钟K里目标和止损都碰到：1 分钟K看不出先后，保守口径算止损、乐观口径算目标。真实结果在两者之间。
  - 出场网格：目标 0.03 / 0.05 / 0.10 倍日线 ATR × 止损 = 目标的 1 倍或 0.5 倍 × 最长持有 3 / 5 / 10 / 15 分钟。
  - 对照：每只股票每天在 9:36–11:00 随机挑 5 个时点、随机方向，同样的出场。策略要明显好过它才算有东西。

成本（可在下面改）：
  - 正股：每股每边 $0.02（半个价差 + 滑点 + 佣金和平台费），按今天的股价折成比例，所以老数据（前复权价）也按比例扣。
  - 期权：平值、3 天到期，Black-Scholes 逐根重算（和日内策略库同一套）；价差每边 1%（乐观）和 3%（剥头皮常用市价单，更现实），
          另加每张每边 $1 的佣金和平台费，按今天的平值期权价折成比例。

检验：样本内 2019–2023，样本外 2024 起。结果写 报告/散户剥头皮回测_<日期>.md 和 程序/data/scalp_bt.json。

用法：arch -arm64 程序/.venv/bin/python 程序/scalp_bt.py [代码 …]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import intraday as I  # noqa: E402

TARGETS = [0.03, 0.05, 0.10]          # 倍日线 ATR
STOP_RATIOS = [1.0, 0.5]              # 止损 = 目标 × 这个
HOLDS = [3, 5, 10, 15]                # 最长持有（根，1 分钟）
COMBOS = [(t, s, h) for t in TARGETS for s in STOP_RATIOS for h in HOLDS]
N_RANDOM = 5
STOCK_COST_SIDE = 0.02                # 美元 / 股 / 边，按今天的股价
OPT_SPREADS = [0.01, 0.03]            # 权利金的半价差，每边
OPT_FEE_SIDE = 1.0                    # 美元 / 张 / 边
SID_NAMES = {s["id"]: s["name"] for s in I.STRATS}
SID_NAMES["rand"] = "对照：随机入场（每天 5 次）"


def _key_combo(c):
    t, s, h = c
    return f"目标 {t:.2f} ATR / 止损 {t * s:.3f} ATR / 最多 {h} 分钟"


def scalp_exits(o, h, l, c, e, d, atr):
    """入场 e 根开盘价，方向 d。返回每个组合的 (出场下标 保守, 出场价 保守, 出场价 乐观, 是否同根两碰)。"""
    n = len(c)
    px = o[e]
    hmax = max(HOLDS)
    end = min(n, e + hmax)
    H, L, C, O = h[e:end], l[e:end], c[e:end], o[e:end]
    fav = (H - px) * d if d == 1 else (px - L)
    adv = (px - L) if d == 1 else (H - px)
    out = []
    for t, sr, hold in COMBOS:
        m = min(hold, len(C))
        tg, sp = t * atr, t * sr * atr
        th = np.flatnonzero(fav[:m] > tg)              # 穿过目标
        sh = np.flatnonzero(adv[:m] >= sp)
        ti = int(th[0]) if len(th) else None
        si = int(sh[0]) if len(sh) else None
        if si is not None:
            # 止损成交价：开盘就跳过止损按开盘价
            gap_through = ((O[si] - px) * d) <= -sp
            s_px = O[si] if gap_through else px - d * sp
        if ti is not None:
            gap_t = ((O[ti] - px) * d) > tg
            t_px = O[ti] if gap_t else px + d * tg
        if si is None and ti is None:
            k = m - 1
            out.append((e + k, C[k], C[k], False))
        elif ti is None or (si is not None and si < ti):
            out.append((e + si, s_px, s_px, False))
        elif si is None or ti < si:
            out.append((e + ti, t_px, t_px, False))
        else:                                           # 同一根两个都碰到
            out.append((e + si, s_px, t_px, True))
    return out


def opt_returns(tmin, e, xs, px_in, px_outs, d, iv, spread, fee_frac):
    T0 = (I.DTE_TRADING * 390 + 16 * 60 - tmin[e] + 1) / I.YEAR_MIN
    T1 = T0 - (tmin[xs] - tmin[e] + 1) / I.YEAR_MIN
    p0, _ = I.bs(px_in, px_in, T0, iv, d == 1)
    p1, _ = I.bs(np.asarray(px_outs), px_in, T1, iv, d == 1)
    p0 = float(p0)
    if p0 <= 0:
        return None
    buy = p0 * (1 + spread + fee_frac)
    sell = p1 * (1 - spread) - p0 * fee_frac
    return sell / buy - 1


def run_symbol(code, qqq_reg):
    import c2
    c2s = c2.settings()
    c2s["最晚入场"] = "11:00"
    df = I.load_1m(code)
    if df.empty:
        return None
    days = I.daily_from_1m(df)
    if len(days) < I.MIN_DAYS:
        return None
    qqq = I.load_1m("QQQ") if code != "QQQ" else df
    qg = {k: g for k, g in qqq.groupby(qqq.index.date)} if len(qqq) else {}
    last = days.iloc[-1]
    p_now_stock = float(last["close"])
    iv_last = days["iv"].dropna()
    p_now_opt = float(I.bs(p_now_stock, p_now_stock, (I.DTE_TRADING + 1) * 390 / I.YEAR_MIN,
                           float(iv_last.iloc[-1]) if len(iv_last) else 0.4, True)[0])
    fee_frac = OPT_FEE_SIDE / max(p_now_opt * 100, 1.0)
    cost_frac = STOCK_COST_SIDE / p_now_stock       # 每边
    # 累加：key = (sid, combo_idx, 期间, 开盘类型, QQQ恐慌) → [n, 正股毛(保守), 正股毛(乐观), 正股净, 正股净平方, 正股净>0, 期权1%, 期权3%, 期权3%平方, 期权3%>0, 同根两碰]
    acc = defaultdict(lambda: np.zeros(11))
    for day, g in df.groupby(df.index.date):
        if day not in days.index:
            continue
        row = days.loc[day]
        if not (row["atr_prev"] > 0) or not (row["iv"] > 0) or not (row["pc"] > 0) or len(g) < 300:
            continue
        o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        tmin = g.index.hour.values * 60 + g.index.minute.values
        atr, iv = float(row["atr_prev"]), float(row["iv"])
        gap = (o[0] - row["pc"]) / atr
        gcls = "big_down" if gap <= -0.75 else "down" if gap <= -0.25 else "big_up" if gap >= 0.75 else "up" if gap >= 0.25 else "flat"
        ds = str(day)
        period = "is" if ds < I.SPLIT else "oos"
        panic = 1 if qqq_reg.get(ds) == "panic" else 0
        sigs = [(sid, i, d) for sid, i, d, _ in I.signals(g, row, qg.get(day), c2s) if sid != "rand"]
        ok = np.flatnonzero((tmin >= I.ENTRY_FROM) & (np.r_[tmin[1:], 10 ** 6] <= I.ENTRY_TO))
        if len(ok):
            rng = np.random.default_rng(zlib.crc32((code + ds).encode()))
            for i in rng.choice(ok, size=min(N_RANDOM, len(ok)), replace=False):
                sigs.append(("rand", int(i), int(rng.choice([1, -1]))))
        for sid, i, d in sigs:
            e = i + 1
            if e >= len(c) - 1:
                continue
            px = o[e]
            ex = scalp_exits(o, h, l, c, e, d, atr)
            xs = np.array([x[0] for x in ex])
            pc_ = np.array([x[1] for x in ex])
            po_ = np.array([x[2] for x in ex])
            amb = np.array([x[3] for x in ex], float)
            g_cons = (pc_ - px) * d / atr
            g_opt = (po_ - px) * d / atr
            net = g_cons - 2 * cost_frac * px / atr
            o1 = opt_returns(tmin, e, xs, px, pc_, d, iv, OPT_SPREADS[0], fee_frac)
            o3 = opt_returns(tmin, e, xs, px, pc_, d, iv, OPT_SPREADS[1], fee_frac)
            if o1 is None:
                continue
            for k in range(len(COMBOS)):
                vals = np.array([1, g_cons[k], g_opt[k], net[k], net[k] ** 2, net[k] > 0, o1[k], o3[k], o3[k] ** 2, o3[k] > 0, amb[k]])
                for gk in (gcls, "all"):
                    acc[(sid, k, period, gk, panic)] += vals
    return {"code": code, "price": p_now_stock, "atr": float(last["atr_prev"]), "opt_price": p_now_opt, "cost_frac": cost_frac, "fee_frac": fee_frac,
            "acc": {"|".join(map(str, k)): v.tolist() for k, v in acc.items()}}


def _work(args):
    code, qreg = args
    try:
        return code, run_symbol(code, qreg), None
    except Exception as e:
        import traceback
        return code, None, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"


def main(codes=None):
    t0 = time.time()
    codes = [c.upper() for c in codes] if codes else I.all_codes()
    print(f"剥头皮回测：{len(codes)} 只 × {len(COMBOS)} 种出场", flush=True)
    qreg = I.daily_regimes().get("QQQ", {})
    import multiprocessing as mp
    import os
    res = {}
    with mp.get_context("spawn").Pool(max(1, min(len(codes), (os.cpu_count() or 2) - 2))) as pool:
        for k, (code, r, err) in enumerate(pool.imap_unordered(_work, [(c, qreg) for c in codes]), 1):
            if err:
                print(f"[{k}/{len(codes)}] {code} 出错：{err}", flush=True)
            elif r is None:
                print(f"[{k}/{len(codes)}] {code} 数据不够，跳过", flush=True)
            else:
                res[code] = r
                print(f"[{k}/{len(codes)}] {code} 完成", flush=True)
    out = HERE / "data" / "scalp_bt.json"
    out.write_text(json.dumps({"made": dt.datetime.now().isoformat(timespec="seconds"), "combos": COMBOS, "symbols": res},
                              ensure_ascii=False), "utf-8")
    print(f"写入 {out}，用时 {time.time() - t0:.0f} 秒", flush=True)


# ================================================================ 报告

LIQUID_MAX_COST = 0.006               # 「适合剥头皮的大票」：往返正股成本 ≤ 0.6% 日线 ATR
TODAY_SIDS = ["orb5", "orb15", "gap_fill", "trend10", "vwap_reclaim", "fakeout", "vwap_fade", "A", "B"]


def _agg(d, codes, sid, ci, gap="all", panic=None):
    """合计：返回 (样本内, 样本外) 两个 11 维数组。"""
    out = [np.zeros(11), np.zeros(11)]
    for code in codes:
        acc = d["symbols"][code]["acc"]
        for j, per in enumerate(("is", "oos")):
            for p in ((0, 1) if panic is None else (panic,)):
                v = acc.get(f"{sid}|{ci}|{per}|{gap}|{p}")
                if v:
                    out[j] += np.array(v)
    return out


def _st(a, b):
    t = a + b
    n = t[0]
    if n == 0:
        return None
    m = t[3] / n
    se = math.sqrt(max(t[4] / n - m * m, 0) / n)
    return dict(n=int(n), n_is=int(a[0]), n_oos=int(b[0]), gc=t[1] / n, go=t[2] / n, net=m, ci=1.96 * se, win=t[5] / n,
                o1=t[6] / n, o3=t[7] / n, win3=t[9] / n, amb=t[10] / n,
                net_is=a[3] / a[0] if a[0] else float("nan"), net_oos=b[3] / b[0] if b[0] else float("nan"))


def _consistent(d, codes, sid, ci):
    ok = tot = 0
    for code in codes:
        a, b = _agg(d, [code], sid, ci)
        if a[0] < 20 or b[0] < 20:
            continue
        tot += 1
        ok += a[3] > 0 and b[3] > 0
    return ok, tot


def report(path=None):
    d = json.loads((HERE / "data" / "scalp_bt.json").read_text("utf-8"))
    combos = [tuple(c) for c in d["combos"]]
    syms = d["symbols"]
    liquid = sorted(c for c, s in syms.items() if s.get("atr") and 2 * STOCK_COST_SIDE / s["atr"] <= LIQUID_MAX_COST)
    pct = lambda x: f"{x * 100:+.2f}%"
    today = dt.date.today().isoformat()
    L = [f"# 散户剥头皮回测 · {today}", "",
         f"程序：`程序/scalp_bt.py`（只读 K 线库，不下单）。数据：1 分钟K，2019-01 起；样本内 2019–2023，样本外 2024 起。生成于 {d['made']}。", "",
         "## 结论", "",
         "**扣掉散户成本后，今天预案里的日内策略改成剥头皮，没有一个组合稳定赚钱；做期权剥头皮亏得更多。**", "",
         "- 不扣成本时，策略入场和随机入场几乎一样：每笔毛利在 ±0.5% 日线 ATR 以内。剥头皮的目标只有 3%–10% ATR，这点毛利不够付成本。",
         "- 正股：散户往返成本（价差 + 滑点 + 费用，约每股 $0.04）在大票上约 0.1%–0.5% ATR，就已经比最好的毛利大或差不多。",
         "- 期权：每笔光价差和手续费就要吃掉权利金的 2%（价差每边 1%）到 6%（每边 3%）以上，而几分钟的股价波动换成期权只有几个百分点，所以全部为负。",
         "- 今天这种行情（QQQ「恐慌急跌」）里，正股剥头皮有几个组合点估计为正。再碰上大幅低开时，「缺口回补」和「15 分钟开盘区间突破」的区间甚至不跨 0。"
         "但这是从 9 条策略 × 24 种出场 × 几种行情里挑出来的；QQQ 恐慌的日子 2019 年以来只有约 240 天，集中在 2020 年 3–6 月、2022 年、2025 年 4 月这几段，同一天的股票一起跌，真实样本远比笔数少。"
         "**换成期权（价差每边 3%）全部为负。** 只能当「值得记录观察」，不能当优势。", "",
         "## 设定（散户能做到的）", "",
         "- 入场：今天预案用到的日内策略（开盘区间突破 5 / 15 / 30 分钟、缺口回补、10 点趋势延续、VWAP 收复、假突破收回、偏离 VWAP 回归、个人策略 B（VWAP 回踩）、个人策略 A（爆发）），每天每只取第一个信号，信号K收盘后**下一根开盘**成交。",
         "- 对照：每只股票每天 9:36–11:00 随机 5 个时点、随机方向，同样出场。",
         f"- 出场网格（{len(combos)} 种）：目标 0.03 / 0.05 / 0.10 倍日线 ATR；止损 = 目标的 1 倍或 0.5 倍；最长持有 3 / 5 / 10 / 15 分钟，到时按收盘平。目标价要**穿过**才算成交；止损跳空按开盘价成交。",
         "- 1 分钟K看不出同一根里先碰目标还是先碰止损：表里的「净」用保守口径（算止损），「毛·乐观」是算目标的上限。",
         f"- 成本：正股每股每边 ${STOCK_COST_SIDE:.2f}（按今天股价折成比例）；期权平值 3 天到期、Black-Scholes 逐根重算，价差每边 1% 或 3%，另加每张每边 ${OPT_FEE_SIDE:.0f}。手续费是假设，以你 moomoo 账户实际费率为准。",
         f"- 「大票池」= 往返正股成本 ≤ {LIQUID_MAX_COST * 100:.1f}% 日线 ATR 的股票（今天的价格和 ATR），共 {len(liquid)} 只：{', '.join(liquid)}。成本占 ATR 比例高的股票（KO、NOK、TLT 一类）剥头皮更没戏，不放进主表。", "",
         "单位：「毛 / 净」是每笔占日线 ATR 的百分比（正股）；「期权」是每笔权利金收益率。", "",
         "## 一、大票池、全部日子：每条策略最好的出场", "",
         "这里每条策略都从 24 种出场里挑最好的一种，所以已经偏乐观（挑的次数越多越容易挑到运气）。",
         "「两段都正」= 有多少只股票样本内外的正股净收益都为正（每段至少 20 笔）。", "",
         "| 策略 | 最好的出场 | 笔数 | 毛·保守 | 毛·乐观 | 净（95% 区间） | 样本内 / 外 净 | 胜率 | 期权 1% | 期权 3% | 两段都正 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

    def best(sid, gap="all", panic=None, codes=liquid):
        rows = []
        for ci in range(len(combos)):
            r = _st(*_agg(d, codes, sid, ci, gap, panic))
            if r and r["n"] >= 30:
                rows.append((ci, r))
        return max(rows, key=lambda x: x[1]["net"]) if rows else (None, None)

    for sid in TODAY_SIDS + ["rand"]:
        ci, r = best(sid)
        if r is None:
            continue
        ok, tot = _consistent(d, liquid, sid, ci)
        L.append(f"| {SID_NAMES[sid]} | {_key_combo(combos[ci])} | {r['n']} | {pct(r['gc'])} | {pct(r['go'])} | {pct(r['net'])}（±{r['ci'] * 100:.2f}） | "
                 f"{pct(r['net_is'])} / {pct(r['net_oos'])} | {r['win']:.0%} | {r['o1'] * 100:+.1f}% | {r['o3'] * 100:+.1f}% | {ok}/{tot} |")
    L += ["", "## 二、随机入场在每种出场下的结果（检查模型偏不偏）", "",
          "随机入场不扣成本应该在 0 附近：保守口径略负、乐观口径略正，真实值在中间，说明模型本身不偏。扣掉成本后全部为负，说明剥头皮的「底」就是成本。", "",
          "| 出场 | 毛·保守 | 毛·乐观 | 同根两碰 | 净 | 期权 1% | 期权 3% |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for ci, cmb in enumerate(combos):
        r = _st(*_agg(d, liquid, "rand", ci))
        L.append(f"| {_key_combo(cmb)} | {pct(r['gc'])} | {pct(r['go'])} | {r['amb']:.0%} | {pct(r['net'])} | {r['o1'] * 100:+.1f}% | {r['o3'] * 100:+.1f}% |")
    for title, gap, panic in (("三、今天这种行情：QQQ「恐慌急跌」的日子（大票池）", "all", 1),
                              ("四、QQQ「恐慌急跌」+ 这只股票大幅低开（预案里检验通过的分支就在这种开盘）", "big_down", 1)):
        L += ["", f"## {title}", "",
              "| 策略 | 最好的出场 | 笔数（内 / 外） | 净（95% 区间） | 样本内 / 外 净 | 期权 1% | 期权 3% | 随机同出场：净 / 期权 3% |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for sid in TODAY_SIDS:
            ci, r = best(sid, gap, panic)
            if r is None:
                continue
            rr = _st(*_agg(d, liquid, "rand", ci, gap, panic))
            L.append(f"| {SID_NAMES[sid]} | {_key_combo(combos[ci])} | {r['n']}（{r['n_is']} / {r['n_oos']}） | {pct(r['net'])}（±{r['ci'] * 100:.2f}） | "
                     f"{pct(r['net_is'])} / {pct(r['net_oos'])} | {r['o1'] * 100:+.1f}% | {r['o3'] * 100:+.1f}% | {pct(rr['net'])} / {rr['o3'] * 100:+.1f}% |")
    L += ["", "## 五、成本和目标比一比（今天的价格）", "",
          "| 股票 | 现价 | 日线 ATR | 往返成本（$0.04/股）占 ATR | 0.05 ATR 目标 = | 平值 3 天期权 | 期权每张往返成本（价差 3% + $2） |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for code in [c for c in ("QQQ", "SPY", "NVDA", "TSLA", "AAPL", "AMZN", "META", "AMD", "KO", "NOK") if c in syms]:
        s = syms[code]
        L.append(f"| {code} | ${s['price']:.2f} | ${s['atr']:.2f} | {2 * STOCK_COST_SIDE / s['atr'] * 100:.2f}% | ${0.05 * s['atr']:.2f} | ${s['opt_price']:.2f} | "
                 f"${s['opt_price'] * 100 * 2 * OPT_SPREADS[1] + 2 * OPT_FEE_SIDE:.0f} |")
    L += ["", "## 如实说", "",
          "- 这是用 1 分钟K模拟的：看不到盘口，不知道限价单排在队伍哪里。真实剥头皮里，目标价「刚好碰到」往往成交不了，止损却一定成交，现实通常比保守口径还差。",
          "- 期权价格是 Black-Scholes 估算（没有历史期权报价），隐含波动率用已实现波动率 × 1.1。真实期权价差在开盘头几分钟、急跌行情里往往比 3% 更宽。",
          "- 每条策略都从 24 种出场里挑了最好的，挑得越多越容易挑到运气；就算这样，扣成本后的点估计也几乎全部为负。",
          "- 同一天不同股票的结果彼此相关，真实的不确定性比表里的 95% 区间更大。",
          "- 要让剥头皮可能成立，需要的不是更快的服务器，而是比随机明显更好的入场（毛利至少要超过往返成本），这在 1 分钟K上没有找到。下一步如果还想试，需要逐笔成交和买一卖一报价数据，用真实价差重做。", ""]
    out = Path(path) if path else BASE / "报告" / f"散户剥头皮回测_{today}.md"
    out.write_text("\n".join(L), "utf-8")
    print(f"报告写入 {out}")
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["--report"]:
        report()
    else:
        main(sys.argv[1:] or None)
        report()
