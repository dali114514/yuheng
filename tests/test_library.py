"""策略库：执行时点、行情分类、整体构建（离线、合成数据）。"""
import math

from optbot import library as lib


def _rows(n=900, seed=1):
    import random
    rng = random.Random(seed)
    rows, p = [], 100.0
    for i in range(n):
        drift = 0.0006 if (i // 150) % 2 == 0 else -0.0005
        p *= 1 + drift + rng.gauss(0, 0.01) + 0.004 * math.sin(i / 9)
        o = p * (1 + rng.gauss(0, 0.002))
        rows.append({"date": f"{2009 + i // 80}-{1 + (i % 80) // 8:02d}-{1 + i % 8:02d}", "open": o,
                     "high": max(o, p) * 1.004, "low": min(o, p) * 0.996, "close": p, "volume": 1e6})
    return rows


def test_machine_executes_next_open():
    b = lib.Bars(_rows())
    trades, pos, pending = lib.machine(b, lambda i, p: 1 if i % 40 < 20 else 0, start=300)
    assert trades
    for t in trades:
        assert t["entry"] == b.o[t["e"]] and t["exit"] == b.o[t["x"]]
        assert (t["e"] - 1) % 40 == 0 and (t["x"] - 1) % 40 == 20   # 信号 K 的下一根开盘


def test_classify_is_causal():
    rows = _rows()
    full = lib.classify(lib.Bars(rows))
    cut = lib.classify(lib.Bars(rows[:700]))
    assert [r and r["r"] for r in full[:700]] == [r and r["r"] for r in cut]
    assert {r["r"] for r in full if r} <= set(lib.RID)


def test_build_all_strategies():
    rows = _rows()
    res = lib.build({"US.SPY": rows}, {})
    y = res["symbols"]["US.SPY"]
    assert len(y["strategies"]) == len(lib.STRATEGIES)
    bh = y["strategies"]["buyhold"]
    assert bh["exposure"] > 0.99 and bh["live"]["position"] == 1
    assert len(y["stops"]) == 2 * len(lib.STOPS)
    for e in y["strategies"].values():
        assert set(e["regimes"]) == set(lib.RID)
