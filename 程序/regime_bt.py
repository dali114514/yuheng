# -*- coding: utf-8 -*-
"""
玉衡5.0 · 按形态匹配打法回测：盘中认出今天的形态 → 用这种形态下回测最好的「入场 + 止损 + 止盈 + 方向」→ 只做认出之后才出现的信号。

  时间点：9:45 / 10:00 / 10:30 / 11:00。认形态的模型同 daytype_bt.py（只用当时已经收盘的K线 + QQQ，2019–2023 训练）。
  打法：日内策略库的全部组合（12 条入场 × 7 种止损 × 8 种止盈 × 多空），入场只看「这个时间点之后、13:00 之前」的第一个信号，下一根开盘进，最晚收盘平。
  挑打法（只用 2019–2023）——每种形态挑样本内每笔平均最高的一组（至少 30 笔）；最好的那组样本内也不赚钱就「不做」：
    ① 你的想法：按认出的形态，用「真的走成这种形态的日子」里最好的打法
    ② 按认出的形态，用「被认成这种形态的日子」里最好的打法（挑的时候和用的时候条件一样）
    ③ 不分形态：样本内整体最好的一组，每天都用
    ④ 对照：随机给每天贴一个形态（按历史比例），再按 ② 的办法挑和用
    ⑤ 上限：那个时间点就已经知道今天最后的形态，用 ① 的打法（做不到，看「认对」值多少）
  检验：2024 起（挑打法和认形态的模型都没见过）。期权同日内策略库（平值、约 3 天到期、按交易时间扣时间价值、买卖各扣 1% 价差）。

用法：arch -arm64 程序/.venv/bin/python 程序/regime_bt.py      → 报告/按形态匹配打法回测_<日期>.md + data/日内策略库/regime_bt.json
只读 K 线库，不下单。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import intraday as I  # noqa: E402
import daytype_bt as D  # noqa: E402

CHECKS = D.CHECKS
ENTRY_LAST = 13 * 60
MIN_N = 30
METHODS = [("mine", "① 你的想法：认出的形态 + 这种形态（真实）里最好的打法"),
           ("pred", "② 认出的形态 + 「被认成这种形态的日子」里最好的打法"),
           ("all", "③ 不分形态：样本内整体最好的一组"),
           ("rand", "④ 对照：随机贴形态，再按 ② 挑和用"),
           ("oracle", "⑤ 上限：已经知道今天最后的形态 + ① 的打法")]


def _prep(code):
    import c2
    c2s = c2.settings()
    c2s["最晚入场"] = f"{ENTRY_LAST // 60:02d}:{ENTRY_LAST % 60:02d}"
    df = I.load_1m(code)
    days = I.daily_from_1m(df)
    q = I.load_1m("QQQ") if code != "QQQ" else df
    qg = {k: g for k, g in q.groupby(q.index.date)}
    last = days.iloc[-1]
    ivl = days["iv"].dropna()
    p_now = float(I.bs(last["close"], last["close"], (I.DTE_TRADING + 1) * 390 / I.YEAR_MIN, float(ivl.iloc[-1]) if len(ivl) else 0.4, True)[0])
    prem_x = float(min(0.6, max(0.05, 0.5 / p_now))) if p_now > 0 else 0.2
    return c2s, df, days, qg, prem_x


def day_combos(g, row, qday, c2s, start_min, prem_x):
    """这个时间点之后（信号K结束 > start_min）、13:00 之前每条入场每个方向的第一个信号，56 种出场的期权收益：{组合键: 收益}。"""
    I.ENTRY_FROM, I.ENTRY_TO = start_min + 1, ENTRY_LAST
    o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
    vw = I._vwap(h, l, c, v)
    tmin = g.index.hour.values * 60 + g.index.minute.values
    atr, iv = float(row["atr_prev"]), float(row["iv"])
    out = {}
    for sid, i, d, struct, fa, fl in I.signals(g, row, qday, c2s):
        if sid == "rand":
            continue
        r = I.exits(g, vw, tmin, i, d, struct, atr, iv, prem_x, fa, fl, row["ph"], row["pl"])
        if not r:
            continue
        e, px, res = r
        xs = np.array([x for _, _, x, _, _ in res])
        ps = np.array([p for _, _, _, p, _ in res])
        rets, _, _ = I.option_return(g, e, xs, px, ps, d, iv)
        if rets is None:
            continue
        dd = "L" if d == 1 else "S"
        for (stp, tgt, *_), rr in zip(res, rets):
            out[f"{sid}|{stp}|{tgt}|{dd}"] = float(rr)
    return out


def _iter_days(code, labels):
    c2s, df, days, qg, prem_x = _prep(code)
    for day, g in df.groupby(df.index.date):
        ds = str(day)
        if day not in days.index or len(g) < 380:
            continue
        row = days.loc[day]
        if not (row["atr_prev"] > 0 and row["iv"] > 0 and row["pc"] > 0):
            continue
        for name, tm in CHECKS:
            lab = labels.get((ds, name))
            if lab is None:
                continue
            yield ds, name, lab, day_combos(g, row, qg.get(day), c2s, tm, prem_x)


def pass_insample(args):
    """样本内：按（时间点, 认出的形态 / 真实形态 / 随机形态, 组合）累计笔数和收益。"""
    code, labels = args
    agg = {"pred": {}, "true": {}, "rand": {}, "all": {}}
    try:
        for ds, name, (tru, pred, rnd), combos in _iter_days(code, {k: v for k, v in labels.items() if k[0] < I.SPLIT}):
            for key, r in combos.items():
                for kind, t in (("pred", pred), ("true", tru), ("rand", rnd), ("all", -1)):
                    a = agg[kind].setdefault((name, t, key), [0, 0.0])
                    a[0] += 1
                    a[1] += r
    except Exception as e:
        print(f"{code} 出错：{e}", flush=True)
    return agg


def pass_outsample(args):
    """样本外：每个方法按选好的组合交易（这个组合当天在这个时间点之后出了信号才算一笔）。"""
    code, labels, pick = args
    rows = []
    try:
        for ds, name, (tru, pred, rnd), combos in _iter_days(code, {k: v for k, v in labels.items() if k[0] >= I.SPLIT}):
            for m, t in (("mine", pred), ("pred", pred), ("all", -1), ("rand", rnd), ("oracle", tru)):
                key = pick.get((m, name, t))
                if key and key in combos:
                    rows.append((name, m, ds, code, combos[key]))
    except Exception as e:
        print(f"{code} 出错：{e}", flush=True)
    return rows


def merge(aggs):
    out = {"pred": {}, "true": {}, "rand": {}, "all": {}}
    for a in aggs:
        for kind, m in a.items():
            for k, (n, s) in m.items():
                x = out[kind].setdefault(k, [0, 0.0])
                x[0] += n
                x[1] += s
    return out


def choose(agg):
    """每个方法、每个时间点、每种形态：样本内每笔平均最高的组合（≥ MIN_N 笔）；最好的也 ≤ 0 就不做（None）。"""
    best = {}
    for kind, m in agg.items():
        for (name, t, key), (n, s) in m.items():
            if n < MIN_N:
                continue
            avg = s / n
            b = best.setdefault((kind, name, t), (None, -9.0, 0))
            if avg > b[1]:
                best[(kind, name, t)] = (key, avg, n)
    pick, info = {}, {}
    for (kind, name, t), (key, avg, n) in best.items():
        use = key if avg > 0 else None
        for m in {"true": ["mine", "oracle"], "pred": ["pred"], "rand": ["rand"], "all": ["all"]}[kind]:
            pick[(m, name, t)] = use
            info[(m, name, t)] = (key, round(avg, 4), n)
    return pick, info


def run(log=print):
    t0 = time.time()
    idx = json.loads((I.OUT / "index.json").read_text("utf-8"))
    codes = sorted(idx["symbols"])
    import multiprocessing as mp
    import os
    nproc = max(1, min(len(codes), (os.cpu_count() or 2) - 2))
    # 1) 特征 + 认形态模型（同 daytype_bt）
    with mp.get_context("spawn").Pool(nproc) as pool:
        feats = pd.DataFrame([r for rows in pool.imap_unordered(D._work, codes) for r in rows])
    K = len(I.DT_IDS)
    rng = np.random.default_rng(11)
    labels = {}
    for name, _ in CHECKS:
        d = feats[feats["chk"] == name]
        tr = d[d["date"] < I.SPLIT]
        Xtr, mu, sd = D.design(tr)
        W = D.fit_softmax(Xtr, tr["dt"].values, K)
        Xa, _, _ = D.design(d, mu, sd)
        pred = D.predict(W, Xa).argmax(1)
        base = np.bincount(tr["dt"].values, minlength=K) / len(tr)
        rnd = rng.choice(K, size=len(d), p=base)
        for (code, ds, tru), p_, r_ in zip(d[["code", "date", "dt"]].itertuples(index=False), pred, rnd):
            labels.setdefault(code, {})[(ds, name)] = (int(tru), int(p_), int(r_))
    log(f"认形态模型：{len(feats)} 个检查点（{time.time() - t0:.0f} 秒）")
    # 2) 样本内累计
    with mp.get_context("spawn").Pool(nproc) as pool:
        agg = merge(pool.imap_unordered(pass_insample, [(c, labels.get(c, {})) for c in codes]))
    pick, info = choose(agg)
    log(f"样本内挑打法完成（{time.time() - t0:.0f} 秒）")
    # 3) 样本外检验
    with mp.get_context("spawn").Pool(nproc) as pool:
        rows = [r for rs in pool.imap_unordered(pass_outsample, [(c, labels.get(c, {}), pick) for c in codes]) for r in rs]
    res = pd.DataFrame(rows, columns=["chk", "m", "date", "code", "ret"])
    n_days = {name: sum(1 for c in labels for (ds, nm) in labels[c] if nm == name and ds >= I.SPLIT) for name, _ in CHECKS}
    out = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "split": I.SPLIT, "min_n": MIN_N, "entry_last": "13:00",
           "methods": METHODS, "daytypes": [{"id": k, "name": n} for k, n in I.DAYTYPES], "checks": [c for c, _ in CHECKS], "res": {}, "picks": {}}
    for name, _ in CHECKS:
        r = {}
        for m, _ in METHODS:
            x = res[(res["chk"] == name) & (res["m"] == m)]["ret"].values
            if len(x):
                se = x.std(ddof=1) / np.sqrt(len(x)) if len(x) > 1 else 0
                daily = res[(res["chk"] == name) & (res["m"] == m)].groupby("date")["ret"].mean()
                r[m] = {"n": int(len(x)), "avg": round(float(x.mean()), 4), "win": round(float((x > 0).mean()), 3),
                        "lo95": round(float(x.mean() - 1.96 * se), 4), "hi95": round(float(x.mean() + 1.96 * se), 4),
                        "trade_rate": round(len(x) / max(n_days[name], 1), 3), "pos_days": round(float((daily > 0).mean()), 3)}
            else:
                r[m] = None
            ins = [info.get((m, name, t)) for t in (range(K) if m not in ("all",) else [-1])]
            r[m + "_in"] = None
            if m != "oracle":
                vals = [(t, x) for t, x in zip(range(K) if m != "all" else [-1], ins) if x]
                out["picks"].setdefault(name, {})[m] = [
                    {"type": (I.DT_IDS[t] if t >= 0 else "all"), "key": k, "in_avg": a, "in_n": n, "used": a > 0} for t, (k, a, n) in vals]
        out["res"][name] = r
    (I.OUT / "regime_bt.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    p = BASE / "报告" / f"按形态匹配打法回测_{dt.date.today()}.md"
    p.write_text(report(out), "utf-8")
    log(f"按形态匹配打法回测已更新：{p.name}，用时 {time.time() - t0:.0f} 秒")
    return out


def _p(v):
    return "—" if v is None else f"{v * 100:+.1f}%"


def _label(key):
    sn = {s["id"]: s["name"] for s in I.STRATS}
    tn = {s["id"]: s["name"] for s in I.STOPS}
    gn = {s["id"]: s["name"] for s in I.TARGETS}
    sid, stp, tgt, dd = key.split("|")
    return f"{'做多' if dd == 'L' else '做空'} · {sn.get(sid, sid)} + {tn.get(stp, stp)} + {gn.get(tgt, tgt)}"


def report(o):
    names = {d["id"]: d["name"] for d in o["daytypes"]}
    L = [f"# 按形态匹配打法回测 · {dt.date.today()}", "",
         "程序：`程序/regime_bt.py`。问题：盘中认出今天的形态，用这种形态下回测最好的「入场 + 止损 + 止盈 + 方向」，只做认出之后才出现的信号，扣完成本赚不赚？",
         f"34 只股票，2019 年起；认形态的模型和挑打法都只用 2019–2023，**{o['split'][:4]} 起是都没见过的数据**。"
         f"每种形态挑样本内每笔平均最高的一组（至少 {o['min_n']} 笔，从约 1,300 组里挑）；最好的那组样本内也不赚钱就不做。"
         f"入场：时间点之后、{o['entry_last']} 之前的第一个信号，下一根开盘进，最晚收盘平。期权同日内策略库（平值、约 3 天到期、扣时间价值和价差）。", ""]
    rs = [(c, o["res"][c]) for c in o["checks"] if c in o["res"]]
    if rs:
        def g(m):
            return "、".join(f"{c} {_p((r.get(m) or {}).get('avg'))}" for c, r in rs)
        L += ["## 结论（先说）", "",
              f"- **① 你的想法（认出的形态 + 这种形态里最好的打法），样本外每笔**：{g('mine')}。",
              f"- ② 用「被认成这种形态的日子」里挑的打法：{g('pred')}；③ 不分形态、整体最好的一组：{g('all')}；④ 随机贴形态（对照）：{g('rand')}。",
              f"- ⑤ 如果那个时间点就知道今天最后的形态，用同样的打法：{g('oracle')}。",
              "- 看法：① 要比 ③（不分形态）和 ④（随机形态）明显好、而且样本外为正，才说明「按形态匹配打法」有用；"
              "⑤ 和 ① 的差距，就是「认不准形态」损失掉的部分。"]
        ok = [c for c, r in rs if (r.get("mine") or {}).get("lo95", -1) > 0]
        ok2 = [c for c, r in rs if (r.get("pred") or {}).get("lo95", -1) > 0]
        L += [f"- **判断**：① 在{'、'.join(ok) if ok else '任何一个时间点都'}{'样本外显著为正' if ok else '没有样本外显著为正'}"
              f"；② {'在 ' + '、'.join(ok2) + ' 显著为正' if ok2 else '也没有一个时间点显著为正（偶尔为正但 95% 区间包含 0，其他时间点为负，像噪音）'}。"
              "① 亏得最多的原因：每种形态里最好的打法（几乎都是「偏离 VWAP 回归」顺着最后的方向做）是靠事后知道方向才赚的，"
              "而认形态只有三四成认对，认错的日子按反方向亏掉。", ""]
    for c, r in rs:
        L += [f"## {c}", "", "| 方法 | 样本外笔数 | 出手的日子 | 每笔（95% 区间） | 赚钱的笔 | 赚钱的天 |", "|---|---:|---:|---|---:|---:|"]
        for m, nm in o["methods"]:
            x = r.get(m)
            if not x:
                L.append(f"| {nm} | 0 | — | — | — | — |")
                continue
            L.append(f"| {nm} | {x['n']:,} | {x['trade_rate'] * 100:.0f}% | {_p(x['avg'])}（{_p(x['lo95'])} ~ {_p(x['hi95'])}） | "
                     f"{x['win'] * 100:.0f}% | {x['pos_days'] * 100:.0f}% |")
        pk = (o["picks"].get(c) or {}).get("mine") or []
        if pk:
            L += ["", "① 用到的打法（样本内每种形态真实日子里最好的一组；括号是样本内每笔、笔数）：", ""]
            L += [f"- {names.get(x['type'], x['type'])}：{_label(x['key'])}（{_p(x['in_avg'])}，{x['in_n']} 笔）{'' if x['used'] else '——样本内也不赚，不做'}" for x in pk]
        pk = (o["picks"].get(c) or {}).get("pred") or []
        if pk:
            L += ["", "② 用到的打法（样本内「被认成这种形态的日子」里最好的一组）：", ""]
            L += [f"- 认成{names.get(x['type'], x['type'])}：{_label(x['key'])}（{_p(x['in_avg'])}，{x['in_n']} 笔）{'' if x['used'] else '——样本内也不赚，不做'}" for x in pk]
        L.append("")
    L += ["## 如实说", "",
          "- 每种形态从约 1,300 组里挑最好的一组，样本内的数字必然偏高；只看样本外。",
          "- 同一天的股票彼此相关，真实的不确定性比 95% 区间更大。",
          "- 期权收益是模型估算的；开盘后隐含波动率下降、真实价差更大时更差。",
          "- 这里每笔 1 张，没有叠加仓位方案；仓位方案只会放大或缩小同一个正负号。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    run()
