"""Backtest engine mechanics: fills, exits, membership, baselines, option pricing."""
from datetime import date, timedelta

import pytest

from src.services.trade_desk import strategy_backtest as bt


def _bar(i, o, h, l, c, v=1_000_000):
    return {"date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(), "open": o, "high": h, "low": l, "close": c,
            "volume": v}


def test_stop_target_gap_and_time_exits():
    flat = [_bar(i, 100, 101, 99, 100) for i in range(5)]
    # target hit on the second session after entry (atr 1 -> target 103)
    trade = bt.simulate(flat + [_bar(5, 100, 101, 99.5, 100), _bar(6, 101, 104, 100, 103)], 4, "long", 1.0,
                        plan="p", ticker="T")
    assert (trade.reason, trade.exit, trade.days) == ("target", 103.0, 2)
    # gap through the stop fills at the open, not the stop
    trade = bt.simulate(flat + [_bar(5, 100, 100.5, 99.5, 100), _bar(6, 95, 96, 94, 95)], 4, "long", 1.0,
                        plan="p", ticker="T")
    assert (trade.reason, trade.exit) == ("stop", 95.0)
    # a day touching both counts as the stop
    trade = bt.simulate(flat + [_bar(5, 100, 104, 98, 100)], 4, "long", 1.0, plan="p", ticker="T")
    assert trade.reason == "stop" and trade.exit == 98.5
    # shorts mirror; time exit at the close after max_days
    bars = flat + [_bar(5 + i, 100, 100.5, 99.6, 99.8) for i in range(20)]
    trade = bt.simulate(bars, 4, "short", 1.0, plan="p", ticker="T", max_days=3)
    assert (trade.reason, trade.days, trade.exit) == ("time", 3, 99.8)
    assert trade.return_pct == pytest.approx(0.2 - 0.1)  # +0.2% gross, 10 bps round trip
    assert bt.simulate(flat, 4, "long", 1.0, plan="p", ticker="T") is None  # no next bar


def test_point_in_time_membership():
    changes = [{"date": "2024-06-01", "added": "NEW", "removed": "OLD"},
               {"date": "2023-01-01", "added": "OLD", "removed": ""}]
    member = bt.membership_from_changes(["NEW", "KEEP"], changes)
    assert member("KEEP", "2020-01-01") and member("NEW", "2024-06-01") and not member("NEW", "2024-05-31")
    assert member("OLD", "2023-06-01") and not member("OLD", "2024-06-01") and not member("OLD", "2022-12-31")
    assert "OLD" in member.tickers


def test_breakout_signals_respect_the_cooldown():
    bars = [_bar(i, 100, 100.5, 99.5, 100) for i in range(60)]
    bars += [_bar(60 + i, 102 + i, 104 + i, 101 + i, 103.5 + i, 3_000_000) for i in range(10)]
    signals = list(bt.breakout_signals(bars))
    assert signals and all(direction == "long" for _, direction, _ in signals)
    indexes = [index for index, _, _ in signals]
    assert all(b - a >= bt.BREAKOUT_COOLDOWN for a, b in zip(indexes, indexes[1:]))


def test_summary_portfolio_and_options():
    trades = []
    for i, (exit_price, reason) in enumerate([(103, "target"), (98.5, "stop"), (103, "target")]):
        t = bt.Trade("p", f"T{i}", "long", f"2025-01-0{i + 1}", f"2025-01-0{i + 2}", 100, 98.5, 103,
                     exit_date=f"2025-01-1{i}", exit=exit_price, reason=reason, days=3, strength=80, atr=1)
        trades.append(t)
    stats = bt.summarize(trades)
    assert stats["trades"] == 3 and stats["win_rate"] == pytest.approx(200 / 3)
    assert stats["avg_r"] == pytest.approx(((2.9 - 1.6 + 2.9) / 1.5) / 3, abs=0.01)  # -1.5% stop, 10 bps costs
    sim = bt.portfolio(trades)
    assert sim["taken"] == 3 and sim["final_equity"] > 100_000
    call = bt.bs_price(100, 100, 30 / 365, 0.3, "call")
    put = bt.bs_price(100, 100, 30 / 365, 0.3, "put")
    assert 3 < call < 4 and call - put == pytest.approx(100 - 100 * 2.718281828 ** (-0.04 * 30 / 365), abs=0.01)
    assert bt.bs_price(110, 100, 0, 0.3, "call") == 10


def test_random_walks_show_no_edge_beyond_the_known_fill_bias():
    walks = bt.random_walk_bars(40, 700, seed=3)
    trades = bt.run_plans(walks, member=lambda ticker, day: True)
    stats = bt.summarize([t for t in trades if t.plan == "swing_trend"])
    assert stats["trades"] > 300
    assert -0.6 < stats["avg_return_pct"] < 0.1  # costs and stop-first fills only; a big profit means look-ahead
