# -*- coding: utf-8 -*-
"""
收盘复盘：K 线入库 → 冷数据 → 拉成交 → 合并交易并对照纪律设置 → 盘感引擎缓存和今天判断的结果 → 一段文字发到 #报告

  机器人每个交易日美东 16:20 自动跑（设置.json → Discord机器人 → 自动收盘复盘）
  手动：Discord 发「复盘」，或控制台 → 工具 →「收盘复盘」
  python review.py --check   只检查连接（OpenD、账户、成交、K线），不写数据
  python review.py --backfill 补长期 K 线，然后重建盘感引擎缓存
"""
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import history      # noqa: E402
import journal      # noqa: E402

TAGS = ("计划内", "方向相反", "计划外", "没定计划")


def et_today():
    from zoneinfo import ZoneInfo
    n = dt.datetime.now(ZoneInfo("America/New_York"))
    d = n.date()
    if n.hour < 16:                       # 收盘前运行：复盘上一个交易日
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def money(x):
    return "—" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def summary(day, path=None):
    con = journal.connect(path)
    L = [f"**收盘复盘 · {day}（美东）**"]
    try:
        p = con.execute("SELECT bias, max_trades, text FROM daily_plan WHERE date = ?", (str(day),)).fetchone()
        L.append(f"盘前计划：{p[2][:120]}" if p else "盘前计划：没定（在 Claude 里说「开始今天」定打法，锁定后自动写进来）")

        # 交易
        tr = con.execute("SELECT contract, direction, open_ts, close_ts, qty, pnl, tag, plan_note, your_reason, underlying "
                         "FROM trades WHERE date = ? ORDER BY open_ts", (str(day),)).fetchall()
        if tr:
            closed = [t for t in tr if t[3]]
            tot = sum(t[5] for t in closed if t[5] is not None)
            L.append(f"\n**交易**：{len(tr)} 笔（已平 {len(closed)}），已实现盈亏 {money(tot)}（按成交价，未扣费用）")
            for tag in TAGS:
                g = [t for t in closed if t[6] == tag]
                if g:
                    win = sum(1 for t in g if (t[5] or 0) > 0)
                    L.append(f"· {tag}：{len(g)} 笔，赚 {win} 笔，合计 {money(sum(t[5] or 0 for t in g))}")
            if p and p[1] and len(tr) > p[1]:
                L.append(f"⚠ 计划最多 {p[1]} 笔，实际做了 {len(tr)} 笔")
            need = [t for t in tr if t[6] != "计划内" and not t[8]]
            if need:
                L.append("\n**这几笔不在计划内，回一句当时为什么做**（发「原因 代码 你的原因」）：")
                for t in need[:8]:
                    L.append(f"· {t[2][11:16]} {t[0]}（{'做多' if t[1] == '多' else '做空'}，{t[4]:g} 张）"
                             f" {money(t[5]) if t[3] else '未平仓'}　{t[6]}" + (f"，{t[7]}" if t[7] else ""))
            opp = [t for t in tr if t[6] == "方向相反" or (t[7] and "相反" in t[7])]
            if opp:
                L.append(f"⚠ 有 {len(opp)} 笔和计划方向相反")
        else:
            L.append("\n**交易**：今天没有成交记录")
    finally:
        con.close()
    return "\n".join(L)


def run(cfg, log=print, day=None):
    day = day or et_today()
    parts = []
    try:
        parts.append(history.update(cfg, log=lambda *a, **k: None))
    except Exception as e:
        parts.append(f"✗ K 线入库出错：{e!r}"[:200])
    try:                                          # 冷数据文件夹（给 Claude 读）
        import cold
        parts.append(cold.export(log=None))
    except Exception as e:
        parts.append(f"✗ 冷数据更新出错：{e!r}"[:200])
    try:
        _, msg = journal.sync_fills(cfg, log=log)
        parts.append(msg)
        journal.build_trades()
    except Exception as e:
        parts.append(f"✗ 成交记录出错：{e!r}"[:200])
    sc = None
    try:                                          # 盘感引擎：用今天的 K 线更新缓存，再算今天每次判断之后真实怎么走
        import engine
        parts.append(engine.load_cache(log=None) and "盘感引擎缓存已更新")
        sc = engine.evaluate_calls(day, cfg)
    except Exception as e:
        parts.append(f"✗ 盘感引擎出错：{e!r}"[:200])
    try:                                          # 玉衡策略库：用今天的日线重算（单独进程，约 10 秒）
        import subprocess
        import sys as _sys
        r = subprocess.run([_sys.executable, str(Path(__file__).with_name("stratlib.py"))], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=900, stdin=subprocess.DEVNULL)
        last = [x for x in (r.stdout or "").splitlines() if x.startswith("策略库已更新")]
        parts.append(last[-1] if last and r.returncode == 0 else f"✗ 策略库出错：{(r.stdout + r.stderr)[-200:]}")
        ri = subprocess.run([_sys.executable, str(Path(__file__).with_name("intraday.py"))], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=1800, stdin=subprocess.DEVNULL)
        li = [x for x in (ri.stdout or "").splitlines() if x.startswith("日内策略库已更新")]
        parts.append(li[-1] if li and ri.returncode == 0 else f"✗ 日内策略库出错：{(ri.stdout + ri.stderr)[-200:]}")
        if ri.returncode == 0:                    # 仓位方案回测（在新的日内策略库上，约 2 分钟）
            rz = subprocess.run([_sys.executable, str(Path(__file__).with_name("sizing.py"))], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=1800, stdin=subprocess.DEVNULL)
            lz = [x for x in (rz.stdout or "").splitlines() if x.startswith("仓位方案回测已更新")]
            parts.append(lz[-1] if lz and rz.returncode == 0 else f"✗ 仓位方案回测出错：{(rz.stdout + rz.stderr)[-200:]}")
        if r.returncode == 0:
            import playbook                       # 下一个交易日的今日预案（按新的行情重挑）
            pb = playbook.build(log=None)
            go = [s["code"] + s["action"] for s in pb["stocks"] if s["level"] != "无"]
            parts.append(f"今日预案（{pb['day']}）：大盘「{pb['market']['regime']}」；" + ("、".join(go) if go else "没有挑中的策略，按不做准备"))
    except Exception as e:
        parts.append(f"✗ 策略库出错：{e!r}"[:200])
    try:
        import sync
        if sync.settings(cfg).get("启用"):
            parts.append(sync.run(cfg, full=True))
    except Exception as e:
        parts.append(f"✗ E 盘同步出错：{e!r}"[:200])
    text = summary(day) + today_extras(day) + (f"\n\n**{sc.splitlines()[0]}**" + ("\n" + "\n".join(sc.splitlines()[1:]) if "\n" in sc else "")
                                               if sc else "")
    try:                                          # 存一份到今日文件夹，和 Claude 复盘时用
        import today
        today.save_text("收盘数据.md", text.replace("**", "").replace("-# ", ""), day)
    except Exception as e:
        parts.append(f"✗ 存今日文件夹出错：{e!r}"[:200])
    return text + "\n\n" + "\n".join(f"-# {x}" for x in parts if x)


def today_extras(day):
    """纪律触发记录 + 早盘 C2 信号结果（合进收盘复盘）。"""
    L = []
    con = journal.connect()
    try:
        try:
            ev = con.execute("SELECT ts, kind, detail FROM discipline_events WHERE date = ? ORDER BY ts", (str(day),)).fetchall()
        except Exception:
            ev = []
        try:
            c2 = con.execute("SELECT code, dir, entry, r_result, hit, ai_stance FROM c2_live WHERE date = ? AND phase = '入场'",
                             (str(day),)).fetchall()
        except Exception:
            c2 = []
    finally:
        con.close()
    try:
        import today
        p = today.load_plan(day)
    except Exception:
        p = None
    if p:
        L.append(f"\n**今天的打法**（{p.get('状态')}）：{today.plan_text(p)[:200]}")
    if ev:
        L.append(f"\n**纪律**：触发 {len(ev)} 次")
        L += [f"· {t[11:16]} {k}：{d[:80]}" for t, k, d in ev[:10]]
    elif p:
        L.append("\n**纪律**：今天没有触发")
    if c2:
        rs = [r for *_, r, h, a in c2 if r is not None]
        L.append(f"\n**早盘 C2（程序按你的打法）**：{len(c2)} 个入场信号" + (f"，平均 {sum(rs) / len(rs):+.2f}R" if rs else ""))
        L += [f"· {c} {'多' if d == '多' else '空'} 入场约 {e:.2f}，" + (f"{h} {r:+.2f}R" if r is not None else "收盘后算结果")
              + (f"，AI {a}" if a else "") for c, d, e, r, h, a in c2]
    return "\n".join(L)


def check(cfg):
    import stocks
    print("① OpenD 行情连接 …")
    mm = stocks.Moomoo(cfg.get("moomoo") or {}, print)
    if mm.ok:
        df, err = history.fetch(mm, "QQQ", "1m", dt.date.today() - dt.timedelta(days=3), dt.date.today() + dt.timedelta(days=1))
        print(f"   QQQ 最近 1 分钟K：{len(df) if df is not None else 0} 根" + (f"（{err}）" if err else
              f"，{df.index[0]} → {df.index[-1]}"))
        df, err = history.fetch(mm, "QQQ", "5m", dt.date.today() - dt.timedelta(days=400), dt.date.today() + dt.timedelta(days=1))
        print(f"   QQQ 5 分钟K 最早能拿到：{df.index[0] if df is not None and len(df) else err}")
        mm.close()
    print("② 交易账户（只读）…")
    con = journal.connect()
    try:
        ft, ctx, accs = journal.open_trade_ctx(cfg, print, con)
        if ctx is None:
            print("   ✗", accs)
            return
        print(f"   ✓ 券商主体 {journal._meta(con, 'security_firm')}，真实账户 {len(accs)} 个")
        end = dt.date.today() + dt.timedelta(days=1)
        for acc in accs:
            ret, d = ctx.history_deal_list_query(start=f"{end - dt.timedelta(days=8)} 00:00:00", end=f"{end} 23:59:59",
                                                 trd_env=ft.TrdEnv.REAL, acc_id=int(acc))
            if ret == ft.RET_OK:
                us = d[d["code"].astype(str).str.startswith("US.")] if len(d) else d
                print(f"   账户 …{str(acc)[-4:]}：最近 7 天美股成交 {len(us)} 笔"
                      + (f"，例：{us.iloc[0]['create_time']} {us.iloc[0]['code']} {us.iloc[0]['trd_side']}" if len(us) else ""))
            else:
                print(f"   账户 …{str(acc)[-4:]}：读取失败 {str(d)[:80]}")
        ctx.close()
    finally:
        con.close()
    print("检查完成，没有写入任何数据。")


if __name__ == "__main__":
    import macro_report as mr
    cfg = mr.load_config()
    if "--check" in sys.argv:
        check(cfg)
    elif "--backfill" in sys.argv:
        print("开始回填历史 K 线（日线 10 年、5 分钟最多 1 年、1 分钟最多 30 天；第一次可能要几十分钟）……")
        print(history.update(cfg, log=print, backfill=True))
        print("接着重建盘感引擎缓存……")
        import engine
        print(engine.build_cache(log=None))
    else:
        print(run(cfg).replace("**", "").replace("-# ", ""))
