# -*- coding: utf-8 -*-
"""
周报：每周五收盘后（美东 17:00，悉尼约周六早上）自动发到 #报告，并存一份到「报告/周报_日期.md」

  ① 交易：笔数、胜率、盈亏；按「计划内 / 方向相反 / 计划外 / 没定计划」分开；计划外交易你补的原因
  ② 纪律：超出计划笔数的天数、到单日亏损上限的天数、没写计划的天数、和计划方向相反的交易
  ③ 盘感引擎：本周和累计问了几次、给「可以追 / 等回踩」的那些之后真实怎么走
  ④ 早盘 C2 监测（影子）：程序按你的打法找到的入场信号和结果
  ⑤ 练习：盘感训练做题数和准确率
Discord 发「周报」随时看本周（周末看刚结束的一周）。
"""
import datetime as dt
import json

import store
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORTS = HERE.parent / "报告"


def week_range(today=None):
    """本周一到周五（周末运行 = 刚结束的那周）。"""
    import regime
    d = today or regime.et_now().date()
    mon = d - dt.timedelta(days=d.weekday())
    return mon, mon + dt.timedelta(days=4)


def _money(x):
    return "—" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _rate(w, l_):
    return f"{w / (w + l_) * 100:.0f}%" if w + l_ else "—"


def _q(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except Exception:
        return []


def build(today=None, path=None, ai=True, cfg=None):
    """返回 (Discord 文字, 给 AI 的数字摘要)。"""
    mon, fri = week_range(today)
    a, b = str(mon), str(fri)
    con = store.connect(path)
    try:
        for mod in ("journal", "risk", "engine"):
            try:
                m = __import__(mod)
                for s in (getattr(m, "SCHEMA", None) if isinstance(getattr(m, "SCHEMA", None), str) else None,):
                    if s:
                        con.execute(s)
                for s in (getattr(m, "SCHEMA", None) if isinstance(getattr(m, "SCHEMA", None), list) else []):
                    con.execute(s)
            except Exception:
                pass
        L = [f"**周报 · {mon:%m-%d} – {fri:%m-%d}（美东）**"]
        F = []                                   # 给 AI 的事实

        # ① 交易
        tr = _q(con, "SELECT tag, pnl, close_ts, your_reason, contract, plan_note, date FROM trades WHERE date BETWEEN ? AND ?", (a, b))
        closed = [t for t in tr if t[2]]
        if tr:
            tot = sum(t[1] or 0 for t in closed)
            win = sum(1 for t in closed if (t[1] or 0) > 0)
            L.append(f"\n**交易**：{len(tr)} 笔（已平 {len(closed)}），赚 {win} 笔，已实现 {_money(tot)}（未扣费用）")
            F.append(f"本周交易 {len(tr)} 笔，已平 {len(closed)} 笔，赚 {win} 笔，已实现盈亏 {tot:.0f} 美元")
            for tag in ("计划内", "方向相反", "计划外", "没定计划"):
                g = [t for t in closed if t[0] == tag]
                if g:
                    w = sum(1 for t in g if (t[1] or 0) > 0)
                    p = sum(t[1] or 0 for t in g)
                    L.append(f"· {tag}：{len(g)} 笔，赚 {w} 笔，合计 {_money(p)}")
                    F.append(f"{tag} {len(g)} 笔，赚 {w} 笔，合计 {p:.0f} 美元")
            why = [f"{t[6][5:]} {t[4]}：{t[3]}" for t in tr if t[0] != "计划内" and t[3]]
            if why:
                L.append("计划外交易当时的原因：\n" + "\n".join(f"　- {x}" for x in why[:8]))
            nowhy = sum(1 for t in tr if t[0] != "计划内" and not t[3])
            if nowhy:
                L.append(f"-# 还有 {nowhy} 笔计划外交易没写原因")
        else:
            L.append("\n**交易**：本周没有成交记录（收盘复盘后才会有）")
            F.append("本周没有交易")

        # ② 纪律
        days = [mon + dt.timedelta(days=i) for i in range(5)]
        plans = {r[0]: r[1] for r in _q(con, "SELECT date, max_trades FROM daily_plan WHERE date BETWEEN ? AND ?", (a, b))}
        per_day = {}
        for t in tr:
            per_day[t[6]] = per_day.get(t[6], 0) + 1
        traded = [str(d) for d in days if per_day.get(str(d))]
        no_plan = [d for d in traded if d not in plans]
        over = [d for d in traded if plans.get(d) and per_day[d] > plans[d]]
        hit = _q(con, "SELECT date FROM risk_events WHERE kind = '亏损' AND date BETWEEN ? AND ?", (a, b))
        opp = sum(1 for t in tr if t[0] == "方向相反" or (t[5] and "相反" in t[5]))
        L.append(f"\n**纪律**：交易日 {len(traded)} 天里，没写计划 {len(no_plan)} 天，超出计划笔数 {len(over)} 天，"
                 f"到单日亏损上限 {len(hit)} 天；和盘前计划方向相反的交易 {opp} 笔")
        F.append(f"有交易的 {len(traded)} 天里，没写计划 {len(no_plan)} 天，超笔数 {len(over)} 天，到亏损上限 {len(hit)} 天，"
                 f"和计划方向相反 {opp} 笔")

        # ③ 盘感引擎：给了「可以追 / 等回踩」之后真实怎么走（按衡量尺子：止损 0.2 倍 ATR、2R、扣成本）
        try:
            import engine
            cost = float(engine.settings(cfg)["成本_R"])
            def eng(where, args):
                rows = _q(con, "SELECT verdict, r_long, r_short FROM engine_calls WHERE evaluated = 1 " + where, args)
                act = [(rl if "多" in v else rs) - cost for v, rl, rs in rows
                       if (v.startswith("可以追") or v.startswith("等回")) and (rl if "多" in v else rs) is not None]
                return len(rows), act
            n1, a1 = eng("AND date BETWEEN ? AND ?", (a, b))
            n2, a2 = eng("", ())
            if n2:
                f = lambda n, x: f"问了 {n} 次，给出进场类结论 {len(x)} 次" + (f"，之后平均 {sum(x) / len(x):+.2f}R" if x else "")
                L.append(f"\n**盘感引擎**：本周{f(n1, a1)}；累计{f(n2, a2)}")
                F.append(f"盘感引擎本周{f(n1, a1)}；累计{f(n2, a2)}")
                if len(a2) < 30:
                    L.append("-# 进场类结论累计不到 30 次，先别下结论")
        except Exception:
            pass

        # ④ 早盘 C2 监测（影子）：程序按你的打法发现的入场信号，结果和 AI 意见
        c2w = _q(con, "SELECT ai_stance, r_result FROM c2_live WHERE phase = '入场' AND date BETWEEN ? AND ?", (a, b))
        c2a = _q(con, "SELECT ai_stance, r_result FROM c2_live WHERE phase = '入场' AND r_result IS NOT NULL")
        if c2a or c2w:
            def c2txt(rows):
                rs = [r for _, r in rows if r is not None]
                return f"{len(rows)} 个，" + (f"平均 {sum(rs) / len(rs):+.2f}R，赚钱 {sum(1 for x in rs if x > 0)} 个" if rs else "还没结果")
            by = {}
            for st, r in c2a:
                by.setdefault(st or "没给意见", []).append(r)
            L.append(f"\n**早盘 C2（程序按你的打法，影子记录）**：本周 {c2txt(c2w)}；累计 {c2txt(c2a)}")
            if by:
                L.append("　AI 意见分组（累计）：" + "；".join(f"{k} {len(v)} 个 平均 {sum(v) / len(v):+.2f}R" for k, v in by.items()))
            F.append(f"早盘 C2 信号本周 {c2txt(c2w)}；累计 {c2txt(c2a)}")

        # ⑤ 练习
        dr = _q(con, "SELECT COUNT(*), SUM(correct = 1), SUM(correct = 0) FROM drills WHERE substr(ts, 1, 10) BETWEEN ? AND ?", (a, b))
        dc = _q(con, "SELECT COUNT(*), AVG(r_result) FROM drills WHERE mode = 'c2' AND r_result IS NOT NULL AND substr(ts, 1, 10) BETWEEN ? AND ?", (a, b))
        n_dr = dr[0][0] if dr else 0
        if n_dr:
            L.append(f"\n**练习**：盘感训练 {n_dr} 题" + (f"，方向题准确率 {_rate(dr[0][1] or 0, dr[0][2] or 0)}" if n_dr else "")
                     + (f"；C2 入场练习 {dc[0][0]} 笔，平均 {dc[0][1]:+.2f}R" if dc and dc[0][0] else ""))
    finally:
        con.close()

    text = "\n".join(L)
    try:
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / f"周报_{fri}.md").write_text(text.replace("-# ", "> "), encoding="utf-8")
    except Exception:
        pass
    return text, F
