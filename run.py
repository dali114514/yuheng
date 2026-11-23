"""期权价差机器人 —— 命令行入口

  python run.py check            检查 OpenD 连接、账户、行情权限（不下单）
  python run.py scan             算信号 + 找合约，只展示，绝不下单
  python run.py once             跑一轮（模拟盘）：管理持仓 + 找机会 + 下模拟单
  python run.py auto             每隔 N 分钟跑一轮（模拟盘），Ctrl+C 停止
  python run.py status           看持仓、挂单、账户

  加 --live 才会用真钱（每笔都要你在终端输入 YES 确认）。
  在本目录新建一个名为 STOP 的空文件 = 紧急停止，机器人不再下任何单。
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_cfg() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="期权价差机器人")
    ap.add_argument("cmd", choices=["check", "scan", "once", "auto", "status",
                                    "lock", "unlock", "discipline", "reset-breaker", "backtest", "dashboard",
                                    "research", "small", "pa", "h2", "box", "library"])
    ap.add_argument("--live", action="store_true", help="使用实盘（真钱）。不加就是模拟盘")
    ap.add_argument("--anytime", action="store_true", help="忽略交易时间窗口（仅模拟盘可用，用于测试）")
    ap.add_argument("--days", type=int, help="lock 的天数（默认用 config.toml 里的 lock_days）")
    a = ap.parse_args()
    if a.anytime and a.live:
        sys.exit("--anytime 只能在模拟盘用。")

    cfg = load_cfg()
    if a.cmd in ("lock", "unlock", "discipline", "reset-breaker"):
        return discipline_cmd(a, cfg)
    if a.cmd == "backtest":
        return backtest_cmd(cfg)
    if a.cmd == "research":
        return research_cmd(cfg)
    if a.cmd == "small":
        return small_cmd(cfg)
    if a.cmd == "pa":
        return pa_cmd(cfg)
    if a.cmd == "h2":
        return h2_cmd(cfg)
    if a.cmd == "box":
        return box_cmd(cfg)
    if a.cmd == "library":
        from optbot import library
        print(library.refresh(cfg)["message"])
        return
    if a.cmd == "dashboard":
        from optbot.dashboard import serve
        return serve(cfg)
    from optbot.cli import Cli, CliError, CliMarket, CliSim, Combined
    from optbot.engine import Engine
    from optbot.strategy import make_signal, pick_spread

    try:
        cli = Cli(cfg)
    except CliError as e:
        sys.exit(f"❌ {e}")
    market = CliMarket(cli)

    if a.live:
        # 实盘下单走 OpenD（行情仍走 CLI）
        c = cfg["connection"]
        try:
            socket.create_connection((c["host"], c["port"]), timeout=3).close()
        except OSError:
            sys.exit(f"❌ 实盘需要 OpenD，但连不上 {c['host']}:{c['port']}。请打开 moomoo 并登录。")
        from optbot.broker import Moomoo
        print("⚠️  实盘模式：会用真钱。请确认已在 OpenD / moomoo 里「解锁交易」。")
        trader = Moomoo(cfg, live=True)
    else:
        trader = CliSim(cli, market, cfg["cli"]["sim_account"])
    b = Combined(market, trader)
    try:
        if a.cmd == "check":
            check(b, cfg, a.live)
        elif a.cmd == "scan":
            sel = cfg["selection"]
            for und in cfg["universe"]["underlyings"]:
                sig = make_signal(b.daily_closes(und), cfg)
                print(f"\n[{und}] 信号：{sig.direction or '无'}")
                for r in sig.reasons:
                    print("   -", r)
                v = sig.values
                if v:
                    print("   数值：" + "  ".join(f"{k}={x:.2f}" for k, x in v.items() if x is not None))
                # 无信号时也演示一下：如果是看涨，会选哪组价差
                d = sig.direction or "bull"
                rows = b.option_rows(und, sel["min_dte"], sel["max_dte"], sel["strike_window_pct"])
                sp, note = pick_spread(d, und, rows, cfg)
                print(f"   {'选中的价差' if sig.direction else '（演示）如果做牛市价差会选'}：{note}")
                if sp:
                    print("   " + sp.describe().replace("\n", "\n   "))
            print("\nscan 只展示，没有下任何单。")
        elif a.cmd == "once":
            Engine(cfg, b, live=a.live).run_once(force_window=a.anytime)
        elif a.cmd == "auto":
            every = cfg["execution"]["loop_interval_min"]
            print(f"自动模式：每 {every} 分钟一轮。Ctrl+C 停止；新建 STOP 文件可紧急停止下单。")
            while True:
                try:
                    Engine(cfg, b, live=a.live).run_once(force_window=a.anytime)
                except Exception as e:      # 网络抖动等，记下来继续
                    print(f"本轮出错：{e}")
                time.sleep(every * 60)
        elif a.cmd == "status":
            status(b, a.live)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        b.close()


def check(b, cfg, live):
    print(f"1) 交易通道：{'实盘（OpenD）' if live else '模拟账户 ' + cfg['cli']['sim_account'] + '（moomoo CLI）'}")
    try:
        acc = b.account()
        keys = [k for k in ("total_asset", "total_assets", "balance", "cash", "us_cash", "power",
                            "max_power_long", "currency") if k in acc]
        print(f"2) {'实盘' if live else '模拟盘'}账户（美元）：" + "  ".join(f"{k}={acc[k]}" for k in keys))
    except Exception as e:
        print(f"2) ❌ {'实盘' if live else '模拟盘'}账户读取失败：{e}")
    und = cfg["universe"]["underlyings"][0]
    print(f"3) {und} 现价：{b.spot(und)}")
    closes = b.daily_closes(und)
    print(f"4) 日K线 {len(closes)} 根（需要 ≥ {cfg['signal']['trend_ma'] + 1}）"
          + (" OK" if len(closes) > cfg["signal"]["trend_ma"] else " ❌ 不够"))
    sel = cfg["selection"]
    rows = b.option_rows(und, sel["min_dte"], sel["max_dte"], 0.02)
    quoted = [r for r in rows if r["bid"] > 0 and r["delta"] != 0]
    print(f"5) 期权行情：取到 {len(rows)} 个合约，其中 {len(quoted)} 个有买卖价和 Delta")
    if rows and not quoted:
        print("   ❌ 没有期权实时报价/Greeks：多半是没有开通美股期权行情权限（OPRA），见 README 第 4 步")
    elif quoted:
        r = quoted[len(quoted) // 2]
        print(f"   例：{r['code']} bid {r['bid']} ask {r['ask']} delta {r['delta']:+.2f} OI {r['oi']}")
    print("检查完成。")


def backtest_cmd(cfg):
    import json
    from optbot import backtest as bt
    from optbot.cli import Cli
    cli = Cli(cfg)
    print("下载/更新历史日线（首次较慢，之后用缓存）……")
    prices = {u: bt.fetch_daily(cli, u) for u in cfg["universe"]["underlyings"]}
    vix = bt.fetch_daily(cli, "US..VIX")
    for u, p in prices.items():
        print(f"  {u}: {len(p)} 根，{p[0][0]} → {p[-1][0]}")
    print(f"  VIX: {len(vix)} 根")
    print("回测当前规则（2010 起）……")
    base = bt.run(cfg, prices, vix)
    s = base["stats"]
    print(f"  交易 {s['trades']} 笔  胜率 {s['win_rate']:.0%}  累计 ${s['total_pnl']:+.0f}  "
          f"盈亏因子 {s['profit_factor']}  最大回撤 ${s['max_drawdown']:.0f}  最长连亏 {s['max_losing_streak']} 笔")
    print(f"  平均每笔赚 ${s['avg_win']:.0f} / 亏 ${s['avg_loss']:.0f}，平均持有 {s['avg_days_held']} 天")
    print(f"  有信号但预算内找不到价差：{base['skipped']['no_spread']} 天")
    print(f"  按 $28 熔断线，第一次触发熔断：{s['breaker_would_trip_on'] or '从未触发'}")
    print("参数敏感性（预算 × RSI 阈值）……")
    g = bt.grid(cfg, prices, vix)
    for r in g:
        print(f"  预算 ${r['budget']:>3}  RSI {r['rsi']}  交易 {r['trades']:>3}  胜率 {r['win_rate']:.0%}  "
              f"累计 ${r['total_pnl']:+7.0f}  回撤 ${r['max_drawdown']:6.0f}  连亏 {r['max_losing_streak']}")
    print("亏损拆解 + 对照组……")
    diag = bt.diagnostics(cfg, prices, vix)
    for r in diag:
        print(f"  {r['label']:<24} 交易 {r['trades']:>3}  胜率 {r['win_rate']:.0%}  累计 ${r['total_pnl']:+8.0f}  "
              f"平均每笔 ${r['per_trade']:+.1f}")
    out = ROOT / "backtest" / "results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                               "base": base, "grid": g, "diagnostics": diag}, ensure_ascii=False), "utf-8")
    print(f"结果已写入 {out}（控制台会读取它）")


def research_cmd(cfg):
    import json
    from optbot import backtest as bt
    from optbot import research as rs
    from optbot.cli import Cli
    cli = Cli(cfg)
    prices = {u: bt.fetch_daily(cli, u) for u in cfg["universe"]["underlyings"]}
    vix = bt.fetch_daily(cli, "US..VIX")
    print(f"检验①：信号出现后 {rs.HORIZON} 个交易日的涨跌（SPY+QQQ，样本内 <{rs.SPLIT} / 样本外 ≥{rs.SPLIT}）")
    fwd = rs.forward_test(cfg, prices, vix)
    f = lambda x: "—" if x is None else f"{x:+.2f}%"
    for r in fwd:
        i, o = r["in"], r["out"]
        print(f"  {r['desc']:<30} 内：覆盖 {i['coverage']:.0%} 均值 {f(i['avg'])} 超额 {f(i['edge'])} | "
              f"外：覆盖 {o['coverage']:.0%} 均值 {f(o['avg'])} 超额 {f(o['edge'])}")
    print("检验②：期权价差回测（较慢）……")
    opt = rs.option_test(cfg, prices, vix)
    for skey, sdesc, _ in rs.SETTINGS:
        print(f"  [{sdesc}] 平均每笔（样本内 / 样本外）")
        for r in opt:
            i, o = r[skey]["in"], r[skey]["out"]
            g = lambda x: "—" if x["per_trade"] is None else f"${x['per_trade']:+.1f}（{x['trades']}笔）"
            print(f"    {r['desc']:<30} {g(i):>16} / {g(o):>16}")
    out = ROOT / "backtest" / "research.json"
    out.write_text(json.dumps({"generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                               "split": rs.SPLIT, "horizon": rs.HORIZON, "forward": fwd, "options": opt,
                               "settings": [[k, d] for k, d, _ in rs.SETTINGS]}, ensure_ascii=False), "utf-8")
    print(f"结果已写入 {out}")


def small_cmd(cfg):
    import json
    from optbot import backtest as bt
    from optbot import smallacct as sa
    from optbot.cli import Cli
    cli = Cli(cfg)
    prices = {u: bt.fetch_daily(cli, u) for u in ("US.SPY", "US.QQQ")}
    vix = bt.fetch_daily(cli, "US..VIX")
    print("小账户回测：$50 起步、全仓、+70% 止盈 / −30% 止损；2010–2024 每月起步一次、每次 2 年……")
    res = sa.study(cfg, prices, vix)
    print(f"\n{'方案':<26}{'出局率':>8}{'赚钱率':>8}{'翻倍率':>8}{'中位数':>9}{'25%-75%':>16}{'胜率':>7}{'止损实际':>9}")
    for r in res["results"]:
        print(f"{r['label']:<26}{r['ruined_pct']:>8.0%}{r['pct_up']:>8.0%}{r['pct_double']:>8.0%}"
              f"{'$' + format(r['median_final'], '.0f'):>9}{'$' + format(r['p25'], '.0f') + '-$' + format(r['p75'], '.0f'):>16}"
              f"{(r['win_rate'] or 0):>7.0%}{(r['avg_stop_ret'] or 0):>9.0%}")
    out = ROOT / "backtest" / "small_account.json"
    out.write_text(json.dumps({"generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"), **res},
                              ensure_ascii=False), "utf-8")
    print(f"\n结果已写入 {out}")


def pa_cmd(cfg):
    import json
    import statistics as st
    from optbot import backtest as bt
    from optbot import pa_backtest as pa
    from optbot.cli import Cli
    cli = Cli(cfg)
    bars = {u: pa.Bars(bt.fetch_ohlcv(cli, u)) for u in ("US.SPY", "US.QQQ")}
    vix = dict(bt.fetch_daily(cli, "US..VIX"))
    out = []
    print(f"杰明价格行为打法 · SPY+QQQ 日线 · 止损=结构点±0.5ATR · 目标 {pa.TARGET_R}R · 最多 {pa.MAX_DAYS} 天 · 次日开盘入场")
    hdr = f"{'打法':<20}{'区间':<6}{'笔数':>5}{'胜率':>7}{'平均R':>8}{'累计R':>8}{'最大回撤R':>10}"
    print(hdr)
    for key, (name, fn) in pa.SIGNALS.items():
        ts = []
        for u, b in bars.items():
            for t in pa.simulate(b, fn):
                t["u"] = u
                ts.append(t)
        rec = {"key": key, "name": name}
        for label, sub in (("全部", ts), ("样本内", [t for t in ts if t["entry_date"] < pa.SPLIT]),
                           ("样本外", [t for t in ts if t["entry_date"] >= pa.SPLIT])):
            s = pa.r_stats(sub)
            rec[label] = s
            if s["n"]:
                print(f"{name if label == '全部' else '':<20}{label:<6}{s['n']:>5}{s['win']:>7.0%}{s['avg_r']:>+8.2f}"
                      f"{s['total_r']:>+8.1f}{s['max_dd_r']:>10.1f}")
        for side in ("long", "short"):
            s = pa.r_stats([t for t in ts if t["side"] == side])
            rec[side] = s
            if s["n"] and key != "baseline_long":
                print(f"{'':<20}{'做多' if side == 'long' else '做空':<6}{s['n']:>5}{s['win']:>7.0%}{s['avg_r']:>+8.2f}{s['total_r']:>+8.1f}")
        # 换算成买期权
        for lbl, delta in (("ATM", None), ("Δ0.3", 0.30)):
            rets = []
            for u, b in bars.items():
                rets += pa.option_returns(b, [t for t in ts if t["u"] == u], vix, bt.IV_MULT[u], target_delta=delta)
            if rets:
                r = [x["ret"] for x in rets]
                rec[f"opt_{lbl}"] = {"n": len(r), "win": round(sum(x > 0 for x in r) / len(r), 3),
                                     "avg": round(st.mean(r), 3), "median": round(st.median(r), 3)}
                print(f"{'':<20}{'期权' + lbl:<8}{len(r):>3}笔  胜率 {rec[f'opt_{lbl}']['win']:.0%}  "
                      f"平均 {st.mean(r):+.0%}  中位数 {st.median(r):+.0%}")
        out.append(rec)
        print()
    (ROOT / "backtest" / "price_action.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    print("结果已写入 backtest/price_action.json")


def h2_cmd(cfg):
    import json
    from collections import Counter
    from optbot import backtest as bt
    from optbot import h2_backtest as h2
    from optbot.cli import Cli
    cli = Cli(cfg)
    scen = [
        ("日线（2010–2026）", {u: h2.Series(bt.fetch_ohlcv(cli, u)) for u in ("US.SPY", "US.QQQ")},
         False, "2010-01-01", "2018-01-01", 16.9),
        ("30分钟·可过夜（2016–2026）", {u: h2.Series(bt.fetch_intraday(cli, u, 8), "time") for u in ("US.SPY", "US.QQQ")},
         False, "2016-06-01", "2021-07-01", 10.4),
        ("30分钟·当天平仓", None, True, "2016-06-01", "2021-07-01", 10.4),
    ]
    scen[2] = (scen[2][0], scen[1][1], True, scen[2][3], scen[2][4], scen[2][5])
    out = []
    for name, series, eod, start, split, years in scen:
        print(f"\n===== {name} =====")
        for label, fn in (("高二/低二 · 多空", lambda s: h2.run_h2(s, start, eod)),
                          ("高二 · 只做多", lambda s: h2.run_h2(s, start, eod, sides=("long",))),
                          ("对照：多头排列就进", lambda s: h2.run_aligned_any(s, start, eod))):
            ts = [t for s in series.values() for t in fn(s)]
            ts.sort(key=lambda t: t["t"])
            a = h2.stats(ts, years * 2)
            ins = h2.stats([t for t in ts if t["t"] < split], 1)
            oos = h2.stats([t for t in ts if t["t"] >= split], 1)
            why = Counter(t["why"] for t in ts)
            print(f"  {label:<14} 笔数 {a['n']:>4}（每标的每年 {a['per_year']}）胜率 {a['win']:.0%}  "
                  f"平均 {a['avg_r']:+.3f}R（赢 {a['avg_win_r']:+.2f} / 亏 {a['avg_loss_r']:+.2f}）  "
                  f"累计 {a['total_r']:+.1f}R  最大回撤 {a['max_dd_r']:.1f}R")
            print(f"  {'':<14} 样本内 {ins['avg_r']:+.3f}R ({ins['n']}笔) / 样本外 {oos['avg_r']:+.3f}R ({oos['n']}笔)  "
                  f"每笔冒险1%复利 ×{a['eq_mult_1pct']}（最大回撤 {a['eq_mdd_1pct']:.0%}）  平仓原因 {dict(why)}")
            if label.startswith("高二/低二"):
                for side in ("long", "short"):
                    sd = h2.stats([t for t in ts if t["side"] == side], years * 2)
                    if sd["n"]:
                        print(f"  {'':<14} {'做多' if side == 'long' else '做空'} {sd['n']}笔 胜率 {sd['win']:.0%} 平均 {sd['avg_r']:+.3f}R")
            out.append({"scenario": name, "rule": label, "all": a, "in": ins, "out": oos, "exit": dict(why)})
        bh = {u: s.c[-1] / s.c[next(i for i, t in enumerate(s.t) if t >= start)] for u, s in series.items()}
        print(f"  买入持有：" + "  ".join(f"{u} ×{m:.2f}" for u, m in bh.items()) + f"（同期，{years:.0f} 年）")
    (ROOT / "backtest" / "h2_ema20.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    print("\n结果已写入 backtest/h2_ema20.json")


def box_cmd(cfg):
    import json
    from optbot import backtest as bt
    from optbot import box_backtest as bx
    from optbot.cli import Cli
    cli = Cli(cfg)
    data = {u: bt.fetch_ohlcv(cli, u) for u in ("US.SPY", "US.QQQ")}
    out = {"main": {}, "periods": {}, "grid": {}, "worst": {}, "years": {}}

    def line(tag, s, extra=""):
        print(f"  {tag:<16} {s['n']:>3}笔（每年 {s['per_year']}） 胜率 {s['win']:.0%}  平均 {s['avg']:+.2%}  "
              f"在场 {s['exposure']:.0%}  总倍数 ×{s['mult']}（年化 {s['cagr']:+.1%}，回撤 {s['mdd']:.0%}）  "
              f"| 持有 ×{s['bh_mult']}（年化 {s['bh_cagr']:+.1%}，回撤 {s['bh_mdd']:.0%}）{extra}")

    print("===== 箱体底部低吸（20日线 ±3%，不止损），2010-01 → 2026-11 =====")
    for u, rows in data.items():
        ts, (i0, i1) = bx.run(rows)
        s = bx.summarize(rows, ts, i0, i1)
        pr = bx.random_pctile(rows, ts, i0, i1)
        s["rand_pctile"], s["rand_median"] = round(pr[0], 3), round(pr[1], 3)
        line(u, s, f"\n  {'':<16} 随机同持仓天数：策略排在第 {pr[0]:.0%} 分位（随机中位数 ×{pr[1]:.2f}）"
                   f"  单笔最差 {s['worst']:+.1%}  持仓中最深浮亏 {s['worst_mae']:+.1%}  最长持有 {s['max_days']} 天")
        out["main"][u] = s
        out["worst"][u] = sorted(ts, key=lambda t: t["ret"])[:5]
        out["years"][u] = {}
        for y in range(2010, 2027):
            ty = [t for t in ts if t["in"][:4] == str(y)]
            out["years"][u][y] = round(bx.equity(ty)[0] - 1, 4)

    print("\n===== 样本内 / 样本外（2018 切分） =====")
    for u, rows in data.items():
        for tag, a, b in (("2010–2017", "2010-01-01", "2018-01-01"), ("2018–2026", "2018-01-01", "9999")):
            ts, (i0, i1) = bx.run(rows, a, b)
            s = bx.summarize(rows, ts, i0, i1)
            line(f"{u} {tag}", s)
            out["periods"][f"{u} {tag}"] = s

    print("\n===== 参数敏感性（只看稳不稳，不用来挑参数） =====")
    for u, rows in data.items():
        for n in (10, 20, 50):
            row = []
            for k in (0.02, 0.03, 0.04, 0.05):
                ts, (i0, i1) = bx.run(rows, k=k, n=n)
                s = bx.summarize(rows, ts, i0, i1)
                out["grid"][f"{u} n{n} k{k}"] = s
                row.append(f"±{k:.0%}: ×{s['mult']:<5} {s['n']:>3}笔 在场{s['exposure']:.0%}")
            print(f"  {u} {n}日线  " + "  |  ".join(row))
    print("\n===== 加止损（收盘跌破买入价 X% → 次日开盘卖；止损后需先收回下轨上方才重新买） =====")
    out["stops"] = {}
    for u, rows in data.items():
        for stop in (None, 0.03, 0.05, 0.08, 0.10, 0.15):
            for rearm in ((True, False) if stop else (True,)):
                ts, (i0, i1) = bx.run(rows, stop=stop, rearm=rearm)
                s = bx.summarize(rows, ts, i0, i1)
                s["stops"] = sum(1 for t in ts if t.get("stopped"))
                ins = bx.summarize(rows, *bx.run(rows, "2010-01-01", "2018-01-01", stop=stop, rearm=rearm)[0:1],
                                   *bx.run(rows, "2010-01-01", "2018-01-01")[1])
                oos = bx.summarize(rows, *bx.run(rows, "2018-01-01", stop=stop, rearm=rearm)[0:1],
                                   *bx.run(rows, "2018-01-01")[1])
                s["in_cagr"], s["out_cagr"] = ins["cagr"], oos["cagr"]
                tag = "不止损" if not stop else f"止损{stop:.0%}" + ("" if rearm else " 立即可再买")
                out["stops"][f"{u} {tag}"] = s
                line(f"{u} {tag}", s, f"\n  {'':<16} 止损 {s['stops']} 次  单笔最差 {s['worst']:+.1%}  "
                                      f"样本内年化 {ins['cagr']:+.1%} / 样本外 {oos['cagr']:+.1%}")
    (ROOT / "backtest" / "box_bottom.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    print("\n结果已写入 backtest/box_bottom.json")


def discipline_cmd(a, cfg):
    import json
    from optbot.discipline import Discipline
    d = Discipline(ROOT, cfg)
    text = (ROOT / "config.toml").read_text("utf-8")
    if a.cmd == "lock":
        print(d.lock(text, a.days))
    elif a.cmd == "unlock":
        print(d.request_unlock())
    elif a.cmd == "discipline":
        st = d.lock_state()
        print("规则锁：", st["reason"])
        v = d.config_violation(text)
        print("配置：", v or "与锁定时一致" if st["locked"] else "未锁定，可以修改")
        for env in ("simulate", "real"):
            f = ROOT / f"state_{env}.json"
            if not f.exists():
                continue
            s = json.loads(f.read_text("utf-8"))
            trades = s.get("closed_trades", [])
            pnl = sum(t["pnl"] for t in trades)
            name = "实盘" if env == "real" else "模拟盘"
            print(f"[{name}] 已平仓 {len(trades)} 笔，累计 ${pnl:+.2f}"
                  f"（熔断线 -${cfg['discipline']['max_total_loss_usd']:.2f}）"
                  + (f"  ⛔ 熔断：{s['breaker_tripped']}" if s.get("breaker_tripped") else ""))
    elif a.cmd == "reset-breaker":
        f = ROOT / f"state_{'real' if a.live else 'simulate'}.json"
        if not f.exists():
            return print("没有状态文件，无需复位。")
        s = json.loads(f.read_text("utf-8"))
        msg = d.reset_breaker(s)
        if not msg.startswith("❌"):
            f.write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")
        print(msg)


def status(b, live):
    from optbot.engine import Engine
    e = Engine(load_cfg(), b, live=live)
    print(f"[{e.env_name}] 机器人记录的持仓：{e.state['positions'] or '无'}")
    print(f"[{e.env_name}] 机器人记录的挂单：{e.state['pending'] or '无'}")
    print(f"[{e.env_name}] 券商端持仓：")
    for p in b.positions():
        print("   ", {k: v for k, v in p.items() if k in ("code", "symbol", "stock_name", "stockName", "qty",
                                                         "cost_price", "costPrice", "pl_val", "plVal")})


if __name__ == "__main__":
    main()
