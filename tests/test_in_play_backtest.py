"""The in-play range test's engine (backtest/in_play.py) on synthetic bars."""
from datetime import datetime, timedelta

import numpy as np

from src.services.trade_desk import boxes as bx
from src.services.trade_desk.backtest import in_play as ip


def _stock(rng, days=80, busy=None):
    """26 fifteen-minute bars a weekday; on ``busy`` days the whole session moves 3x more."""
    start, o, h, l, c, v = [], [], [], [], [], []
    price, day, made = 100.0, datetime(2024, 1, 2), 0
    while made < days:
        if day.weekday() < 5:
            scale = 3.0 if busy is not None and busy[made] else 1.0
            for k in range(26):
                move = rng.normal(0, 0.2 * scale)
                op = price + (rng.normal(0, 0.3 * scale) if k == 0 else 0)
                cl = op + move
                start.append(day + timedelta(hours=9, minutes=30 + 15 * k))
                o.append(op), h.append(max(op, cl) + abs(rng.normal(0, 0.05 * scale))), l.append(min(op, cl) - abs(rng.normal(0, 0.05 * scale)))
                c.append(cl), v.append(1000 * scale)
                price = cl
            made += 1
        day += timedelta(days=1)
    return bx.Bars(start, o, h, l, c, v)


def test_busy_days_are_found_and_random_scores_find_nothing():
    rng = np.random.default_rng(1)
    tables = {}
    for n in range(30):
        busy = rng.random(80) < 0.15  # a few days a stock is in play from the open on
        tables[f"S{n}"] = ip.features(_stock(rng, busy=busy))
    some = next(iter(tables["S0"].values()))
    assert set(some) == {"S", "gap", "range1", "up", "Y", "vol_ratio"}
    edges = ip.daily_edges(tables, min_stocks=20)
    assert len(edges) > 50
    split = edges[len(edges) // 2][0]
    result = ip.summary(edges, split)
    assert ip.passes(result) and result["A"]["edge"] > 0.5
    shuffled = ip.shuffled_scores(tables, np.random.default_rng(2), draws=30)
    assert abs(np.mean(shuffled)) < 0.1


def test_on_calm_random_walks_the_edge_is_about_zero():
    rng = np.random.default_rng(3)
    tables = {f"S{n}": ip.features(ip.random_walk(_stock(rng), rng)) for n in range(30)}
    edges = ip.daily_edges(tables, min_stocks=20)
    split = edges[len(edges) // 2][0]
    result = ip.summary(edges, split)
    assert not ip.passes(result)
    assert all(abs(row["edge"]) < 0.25 for row in result.values())
