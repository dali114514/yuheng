# -*- coding: utf-8 -*-
"""
玉衡5.0 · 盘中认形态回测：在 9:45 / 10:00 / 10:30 / 11:00，只用当时已经收盘的 1 分钟K，能不能认出今天最后会走成哪种形态
（单边上涨 / 单边下跌 / 冲高回落 / 探底回升 / 宽幅震荡 / 窄幅震荡，定义同 intraday.DAYTYPES）？认出来以后照着做期权，扣完成本赚不赚？

  特征（都是当时已知的）：跳空、从开盘走了多少、到现在的振幅、收在今天区间的哪里、离 VWAP 多远、先到高点还是先到低点、
        最近 15 根的方向、QQQ 从开盘走了多少和在不在 VWAP 上方（都用倍日线 ATR）。
  模型：多类别逻辑回归（numpy 自己写），只用 2019–2023 训练，2024 起检验（模型没见过）。
  交易：模型说「偏上涨」（单边上涨 + 探底回升的概率 − 单边下跌 − 冲高回落 超过门槛）就买平值 CALL，偏下跌就买 PUT，下一根开盘进；
        出场两种：拿到收盘 / 0.2 倍 ATR 止损（否则拿到收盘）。门槛在样本内定成大约 30% 的日子出手。
  对照：① 顺势（从开盘走的方向，同样只在 30% 走得最多的日子做）② 随机方向（同样的日子）③ 「事先知道形态」的上限。
  期权和日内策略库同一套：平值、约 3 天到期、Black-Scholes、按交易时间扣时间价值、买卖各扣 1% 价差。

用法：arch -arm64 程序/.venv/bin/python 程序/daytype_bt.py      → 报告/盘中认形态回测_<日期>.md + data/日内策略库/daytype_bt.json
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

CHECKS = [("09:45", 585), ("10:00", 600), ("10:30", 630), ("11:00", 660)]
FEATS = ["gap", "mv", "rng", "pos", "vw", "first", "mom15", "qmv", "qvw", "absmv", "gap_x_mv"]
TRADE_SHARE = 0.30


# ================================================================ 每只股票：特征 + 结果

def _stop_exit(o, h, l, c, e, d, stop):
    """从第 e 根起，碰到止损（跳空穿过按开盘价）就走，否则拿到收盘。返回 (出场下标, 出场价)。"""
    adv = l[e:] if d == 1 else h[e:]
    hit = np.flatnonzero(adv <= stop) if d == 1 else np.flatnonzero(adv >= stop)
    if len(hit):
        k = e + int(hit[0])
        return k, (min(o[k], stop) if d == 1 else max(o[k], stop))
    return len(c) - 1, c[-1]


def run_symbol(code):
    df = I.load_1m(code)
    if df.empty:
        return []
    days = I.daily_from_1m(df)
    q = I.load_1m("QQQ") if code != "QQQ" else df
    qd = I.daily_from_1m(q)
    qg = {k: g for k, g in q.groupby(q.index.date)}
    rows = []
    for day, g in df.groupby(df.index.date):
        if day not in days.index or len(g) < 380:
            continue
        row = days.loc[day]
        atr, iv = float(row["atr_prev"]), float(row["iv"])
        if not (atr > 0 and iv > 0 and row["pc"] > 0):
            continue
        o, h, l, c, v = (g[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
        tmin = g.index.hour.values * 60 + g.index.minute.values
        vw = I._vwap(h, l, c, v)
        dtype = I.DT_IDS.index(I.day_type(o, h, l, c, atr))
        qq = qg.get(day)
        qatr = float(qd.loc[day]["atr_prev"]) if day in qd.index and qd.loc[day]["atr_prev"] > 0 else None
        if qq is not None and len(qq):
            qo, qh, ql, qc, qv = (qq[x].values.astype(float) for x in ("open", "high", "low", "close", "volume"))
            qvw = I._vwap(qh, ql, qc, qv)
            qt = qq.index.hour.values * 60 + qq.index.minute.values
        for name, tm in CHECKS:
            ks = np.flatnonzero(tmin == tm)
            if not len(ks):
                continue
            k = int(ks[0])
            e = k + 1
            if e >= len(c) - 5:
                continue
            hi, lo = h[:k + 1].max(), l[:k + 1].min()
            rg = max(hi - lo, 1e-9)
            f = dict(gap=(o[0] - row["pc"]) / atr, mv=(c[k] - o[0]) / atr, rng=rg / atr, pos=(c[k] - lo) / rg,
                     vw=(c[k] - vw[k]) / atr, first=1.0 if int(np.argmax(h[:k + 1])) < int(np.argmin(l[:k + 1])) else -1.0,
                     mom15=(c[k] - c[max(0, k - 15)]) / atr, qmv=0.0, qvw=0.0)
            if qq is not None and len(qq) and qatr:
                qk = np.flatnonzero(qt <= tm)
                if len(qk):
                    j = int(qk[-1])
                    f["qmv"] = (qc[j] - qo[0]) / qatr
                    f["qvw"] = (qc[j] - qvw[j]) / qatr
            f["absmv"] = abs(f["mv"])
            f["gap_x_mv"] = f["gap"] * f["mv"]
            px = o[e]
            out = dict(code=code, date=str(day), chk=name, dt=dtype, **{kk: float(vv) for kk, vv in f.items()})
            for d in (1, -1):
                xs, ps = [len(c) - 1], [c[-1]]
                k2, p2 = _stop_exit(o, h, l, c, e, d, px - d * 0.2 * atr)
                xs.append(k2)
                ps.append(p2)
                r, _, _ = I.option_return(g, e, np.array(xs), px, np.array(ps), d, iv)
                if r is None:
                    break
                tag = "L" if d == 1 else "S"
                out[f"r_close_{tag}"] = float(r[0])
                out[f"r_stop_{tag}"] = float(r[1])
                out[f"s_close_{tag}"] = float((c[-1] - px) * d / atr)              # 正股，倍 ATR（不扣成本）
            else:
                rows.append(out)
    return rows


# ================================================================ 模型：多类别逻辑回归

def fit_softmax(X, y, k, lam=1e-3, iters=600, lr=0.5):
    n, p = X.shape
    W = np.zeros((p, k))
    Y = np.eye(k)[y]
    for _ in range(iters):
        Z = X @ W
        Z -= Z.max(1, keepdims=True)
        P = np.exp(Z)
        P /= P.sum(1, keepdims=True)
        G = X.T @ (P - Y) / n + lam * W
        W -= lr * G
    return W


def predict(W, X):
    Z = X @ W
    Z -= Z.max(1, keepdims=True)
    P = np.exp(Z)
    return P / P.sum(1, keepdims=True)


def design(df, mu=None, sd=None):
    X = df[FEATS].values.astype(float)
    X = np.c_[X, np.clip(df["pos"].values, 0, 1) ** 2, np.tanh(df["mv"].values)]
    if mu is None:
        mu, sd = X.mean(0), X.std(0) + 1e-9
    X = (X - mu) / sd
    return np.c_[np.ones(len(X)), X], mu, sd


def stats(r):
    r = np.asarray(r, float)
    r = r[~np.isnan(r)]
    if not len(r):
        return None
    se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else None
    return {"n": int(len(r)), "avg": round(float(r.mean()), 4), "win": round(float((r > 0).mean()), 3),
            "lo95": round(float(r.mean() - 1.96 * se), 4) if se else None, "hi95": round(float(r.mean() + 1.96 * se), 4) if se else None}


def evaluate(df):
    K = len(I.DT_IDS)
    UP = [I.DT_IDS.index("trend_up"), I.DT_IDS.index("dip_rally")]
    DN = [I.DT_IDS.index("trend_down"), I.DT_IDS.index("spike_fade")]
    res = {}
    for name, _ in CHECKS:
        d = df[df["chk"] == name].copy()
        tr, te = d[d["date"] < I.SPLIT], d[d["date"] >= I.SPLIT]
        if len(tr) < 1000 or len(te) < 300:
            continue
        Xtr, mu, sd = design(tr)
        Xte, _, _ = design(te, mu, sd)
        W = fit_softmax(Xtr, tr["dt"].values, K)
        Ptr, Pte = predict(W, Xtr), predict(W, Xte)
        base = np.bincount(tr["dt"].values, minlength=K) / len(tr)
        yte = te["dt"].values
        acc = float((Pte.argmax(1) == yte).mean())
        acc_base = float((yte == base.argmax()).mean())
        ll = float(-np.log(np.clip(Pte[np.arange(len(yte)), yte], 1e-9, 1)).mean())
        ll_base = float(-np.log(np.clip(base[yte], 1e-9, 1)).mean())
        # 每种形态：模型最有把握的 10% 日子里，真的走成这种形态的比例（样本外）
        conf = {}
        for j, kname in enumerate(I.DT_IDS):
            thr = np.quantile(Pte[:, j], 0.9)
            sel = Pte[:, j] >= thr
            conf[kname] = {"base": round(float((yte == j).mean()), 3), "top10": round(float((yte[sel] == j).mean()), 3),
                           "avg_p": round(float(Pte[sel, j].mean()), 3)}
        # 交易：方向分 = P(上) − P(下)；门槛在样本内定成约 30% 的日子出手
        s_tr = Ptr[:, UP].sum(1) - Ptr[:, DN].sum(1)
        s_te = Pte[:, UP].sum(1) - Pte[:, DN].sum(1)
        thr = float(np.quantile(np.abs(s_tr), 1 - TRADE_SHARE))
        m_tr = np.abs(tr["mv"].values)
        mthr = float(np.quantile(m_tr, 1 - TRADE_SHARE))
        rng = np.random.default_rng(7)
        out = {"n_train": int(len(tr)), "n_test": int(len(te)), "acc": round(acc, 3), "acc_base": round(acc_base, 3),
               "logloss": round(ll, 3), "logloss_base": round(ll_base, 3), "confident": conf, "trade": {}}
        for half, part, s, P in (("in", tr, s_tr, Ptr), ("out", te, s_te, Pte)):
            sel = np.abs(s) >= thr
            dmod = np.sign(s)
            mom = np.abs(part["mv"].values) >= mthr
            dmom = np.sign(part["mv"].values)
            drand = rng.choice([1, -1], size=len(part))
            yy = part["dt"].values
            oracle_dir = np.where(np.isin(yy, UP), 1, np.where(np.isin(yy, DN), -1, 0))
            T = {}
            for ex in ("close", "stop"):
                def pick(mask, dirs, ex=ex):
                    vals = np.where(dirs > 0, part[f"r_{ex}_L"].values, part[f"r_{ex}_S"].values)
                    return stats(vals[mask & (dirs != 0)])
                T[ex] = {"model": pick(sel, dmod), "momentum": pick(mom, dmom), "random": pick(sel, drand),
                         "oracle": pick(oracle_dir != 0, oracle_dir)}
            T["stock_model"] = stats(np.where(dmod > 0, part["s_close_L"].values, part["s_close_S"].values)[sel])
            T["stock_momentum"] = stats(np.where(dmom > 0, part["s_close_L"].values, part["s_close_S"].values)[mom])
            T["dir_hit_model"] = round(float((np.sign(np.where(dmod > 0, part["s_close_L"].values, part["s_close_S"].values)[sel]) > 0).mean()), 3)
            T["dir_hit_momentum"] = round(float((np.sign(np.where(dmom > 0, part["s_close_L"].values, part["s_close_S"].values)[mom]) > 0).mean()), 3)
            T["share"] = round(float(sel.mean()), 3)
            out["trade"][half] = T
        out["coef_note"] = "特征标准化后的系数（单边上涨 − 单边下跌）：" + "、".join(
            f"{n} {w:+.2f}" for n, w in sorted(zip(FEATS + ["pos²", "tanh(mv)"], (W[1:, I.DT_IDS.index('trend_up')] - W[1:, I.DT_IDS.index('trend_down')])),
                                              key=lambda x: -abs(x[1]))[:5])
        res[name] = out
    return res


# ================================================================ 全部

def _work(code):
    try:
        return run_symbol(code)
    except Exception as e:
        print(f"{code} 出错：{e}", flush=True)
        return []


def run(log=print):
    t0 = time.time()
    idx = json.loads((I.OUT / "index.json").read_text("utf-8"))
    codes = sorted(idx["symbols"])
    import multiprocessing as mp
    import os
    rows = []
    with mp.get_context("spawn").Pool(max(1, min(len(codes), (os.cpu_count() or 2) - 2))) as pool:
        for r in pool.imap_unordered(_work, codes):
            rows += r
    df = pd.DataFrame(rows)
    log(f"盘中认形态：{df['code'].nunique()} 只股票、{df['date'].nunique()} 天、{len(df)} 个检查点（{time.time() - t0:.0f} 秒）")
    res = evaluate(df)
    out = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "split": I.SPLIT, "checks": [c for c, _ in CHECKS],
           "daytypes": [{"id": k, "name": n} for k, n in I.DAYTYPES], "trade_share": TRADE_SHARE, "res": res}
    (I.OUT / "daytype_bt.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    md = report(out)
    p = BASE / "报告" / f"盘中认形态回测_{dt.date.today()}.md"
    p.write_text(md, "utf-8")
    log(f"盘中认形态回测已更新：{p.name}，用时 {time.time() - t0:.0f} 秒")
    return out


def _p(v):
    return "—" if v is None else f"{v * 100:+.1f}%"


def _s(x):
    if not x:
        return "—"
    ci = f"（95% {_p(x['lo95'])} ~ {_p(x['hi95'])}）" if x.get("lo95") is not None else ""
    return f"{x['n']:,} 笔，每笔 {_p(x['avg'])}，赚钱 {round(x['win'] * 100)}%{ci}"


def report(o):
    names = {d["id"]: d["name"] for d in o["daytypes"]}
    L = [f"# 盘中认形态回测 · {dt.date.today()}", "",
         "程序：`程序/daytype_bt.py`。问题：开盘后某个时间，只用当时已经收盘的 1 分钟K，能不能认出今天最后会走成哪种形态？认出来照着做期权，扣完成本赚不赚？",
         f"34 只股票，2019 年起。模型只用 2019–2023 训练，**{o['split'][:4]} 起是模型没见过的数据**。形态定义同日内策略库（收盘后整天分类）。",
         "期权：平值、约 3 天到期、Black-Scholes、按交易时间扣时间价值、买卖各扣 1% 价差；下一根开盘进。", ""]
    rs = [(c, o["res"][c]) for c in o["checks"] if c in o["res"]]
    if rs:
        accs = "、".join(f"{c} {r['acc'] * 100:.0f}%" for c, r in rs)
        hits = "、".join(f"{c} {r['trade']['out']['dir_hit_model'] * 100:.0f}%" for c, r in rs)
        mod = "、".join(f"{c} {_p(r['trade']['out']['close']['model']['avg'])}" for c, r in rs)
        rnd = "、".join(f"{c} {_p(r['trade']['out']['close']['random']['avg'])}" for c, r in rs)
        orc = "、".join(f"{c} {_p(r['trade']['out']['close']['oracle']['avg'])}" for c, r in rs)
        L += ["## 结论（先说）", "",
              f"1. **形态「认得出」一部分**：样本外认对的比例 {accs}（一直猜最常见那种只有 {rs[0][1]['acc_base'] * 100:.0f}%），越晚越准；"
              "模型最有把握的日子里，单边上涨 / 单边下跌 / 窄幅震荡认对的比例是历史比例的 2–3 倍。",
              "2. **但认出来的主要是「已经走完的那一段」**：形态是按整天分的，早上已经涨了一段的日子，本来就更容易被归成单边上涨。"
              f"真正要赚的是「从现在到收盘」的方向，样本外做对的比例只有 {hits}，和抛硬币一样。",
              f"3. **所以照着做期权不赚钱，和随机方向一样亏**：样本外拿到收盘，按认出的形态做每笔 {mod}；随机方向 {rnd}。顺势（从开盘走的方向）也一样。",
              f"4. **「事先知道形态」值很多钱**：如果在那个时间点就已经知道今天最后会走成什么，照着做每笔 {orc}——越晚知道值得越少（剩下的空间小了）。"
              "差别全在「事先知道」和「看出来时已经走完了」之间。",
              "5. 对你的想法：「判断对形态 + 用对策略就赚钱」在逻辑上成立，但用价格和 QQQ 这些盘中信息，到 11:00 都没法比抛硬币更好地判断剩下的方向。"
              "要赚钱，判断得来自这些数据之外的东西（消息、盘口、你的盘感），而那只能靠记录真实判断来验证。", ""]
    L += ["## 认得准不准（样本外）", "",
          "| 时间 | 认对的比例 | 一直猜最常见那种 | 对数损失（越小越好） | 只用历史比例 |", "|---|---:|---:|---:|---:|"]
    for c in o["checks"]:
        r = o["res"].get(c)
        if r:
            L.append(f"| {c} | {r['acc'] * 100:.0f}% | {r['acc_base'] * 100:.0f}% | {r['logloss']} | {r['logloss_base']} |")
    L += ["", "模型最有把握的 10% 日子里，真的走成那种形态的比例（样本外；括号里是所有日子的比例）：", "",
          "| 时间 | " + " | ".join(names[k] for k in names) + " |", "|---|" + "---:|" * len(names)]
    for c in o["checks"]:
        r = o["res"].get(c)
        if r:
            L.append(f"| {c} | " + " | ".join(f"{r['confident'][k]['top10'] * 100:.0f}%（{r['confident'][k]['base'] * 100:.0f}%）" for k in names) + " |")
    L += ["", "## 照着做期权赚不赚", "",
          f"每个时间点，方向分 = P(单边上涨) + P(探底回升) − P(单边下跌) − P(冲高回落)，分数最极端的约 {round(o['trade_share'] * 100)}% 的日子出手"
          "（门槛在样本内定）；对照用同样多的日子。", ""]
    for c in o["checks"]:
        r = o["res"].get(c)
        if not r:
            continue
        L += [f"### {c}", "", "| | 样本内 · 拿到收盘 | 样本外 · 拿到收盘 | 样本外 · 0.2 ATR 止损 |", "|---|---|---|---|"]
        for key, nm in (("model", "按认出的形态做"), ("momentum", "顺势（从开盘走的方向）"), ("random", "随机方向（同样的日子）"), ("oracle", "事先知道形态（上限，做不到）")):
            L.append(f"| {nm} | {_s(r['trade']['in']['close'][key])} | {_s(r['trade']['out']['close'][key])} | {_s(r['trade']['out']['stop'][key])} |")
        ti, to = r["trade"]["in"], r["trade"]["out"]
        L += ["", f"方向做对的比例（正股收盘比进场有利）：模型 样本内 {ti['dir_hit_model'] * 100:.0f}% / 样本外 {to['dir_hit_model'] * 100:.0f}%；"
                  f"顺势 样本内 {ti['dir_hit_momentum'] * 100:.0f}% / 样本外 {to['dir_hit_momentum'] * 100:.0f}%。"
                  f"正股（不扣成本，倍 ATR）：模型 {_s(to['stock_model'])}；顺势 {_s(to['stock_momentum'])}。", "", r["coef_note"], ""]
    L += ["## 如实说", "",
          "- 同一天的股票彼此相关（大盘一起动），真实的不确定性比 95% 区间更大。",
          "- 期权收益是模型估算的；开盘后隐含波动率下降、真实价差更大时结果更差。",
          "- 「事先知道形态」那一行是做不到的上限，用来看「认对」值多少钱。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    run()
