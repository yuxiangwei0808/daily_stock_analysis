import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.services.trade_desk.backtest import intraday as ib

NY = ZoneInfo("America/New_York")


def _day(day, prices, volume=1000.0, step=5):
    """Bars from a list of (open, high, low, close), starting 09:30."""
    start = datetime.fromisoformat(f"{day}T09:30").replace(tzinfo=NY)
    return [ib.to_bar(start + timedelta(minutes=step * i), o, h, low, c, volume) for i, (o, h, low, c) in enumerate(prices)]


def _flat(day, price=100.0, n=78):
    return _day(day, [(price, price + 0.1, price - 0.1, price)] * n)


def test_sessions_keep_regular_hours_only():
    bars = _flat("2026-09-01")
    early = ib.to_bar(datetime(2026, 9, 1, 8, 0, tzinfo=NY), 1, 1, 1, 1, 1)
    days = ib.sessions([early, *bars])
    assert list(days) == ["2026-09-01"] and len(days["2026-09-01"]) == 78


def test_orb_long_exits_at_stop_below_the_range():
    prices = [(100, 101, 99, 100)] * 6 + [(100, 102, 100, 101.5), (101.5, 101.6, 98.0, 98.5)] + [(98.5, 99, 98, 98.5)] * 70
    trades = [t for t in ib.day_signals("X", "2026-09-02", _day("2026-09-02", prices), 100.0) if t.strategy == "orb30"]
    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "long" and trade.entry == 101.5 and trade.exit == 99 and trade.reason == "stop"
    assert trade.entry_time == "10:05"  # the entry bar, not the stop bar
    assert abs(trade.return_pct - ((99 / 101.5 - 1) * 100 - 0.1)) < 1e-9


def test_gap_signals_mirror_each_other():
    bars = _day("2026-09-02", [(103, 103.5, 102.5, 103)] * 20 + [(103, 103, 101, 101)] * 58)
    trades = {t.strategy: t for t in ib.day_signals("X", "2026-09-02", bars, 100.0)}
    assert trades["gap_go"].direction == "long" and trades["gap_fade"].direction == "short"
    assert abs(trades["gap_go"].gross_pct + trades["gap_fade"].gross_pct) < 1e-9
    assert trades["gap_go"].exit == 101


def test_big_move_waits_until_0945():
    # +4% from the first bar, but only bars from 09:45 on can trigger.
    bars = _day("2026-09-02", [(104, 104.2, 103.8, 104)] * 78)
    trades = {t.strategy: t for t in ib.day_signals("X", "2026-09-02", bars, 100.0)}
    assert trades["move_follow"].entry_time == "09:50" and trades["move_fade"].direction == "short"


def test_momentum_uses_the_move_to_ten_oclock():
    prices = [(100, 100.5, 99.5, 99.5)] * 6 + [(99.5, 100, 99, 99.8)] * 72
    trade = ib.momentum_trade("X", "2026-09-02", _day("2026-09-02", prices), 100.0)
    assert trade.direction == "short" and trade.entry_time == "15:30"
    hourly = _day("2026-09-02", [(100, 101, 100, 101)] + [(101, 101, 100, 100.5)] * 6, step=60)
    trade = ib.momentum_trade("X", "2026-09-02", hourly, 100.0)
    assert trade.direction == "long" and trade.entry_time == "15:30"


def test_breakout_day_needs_two_bars_with_volume():
    levels = {"high20": 101.0, "low20": 90.0, "ma50": 95.0, "atr": 2.0, "avg_volume": 78 * 1000.0}
    quiet = _day("2026-09-02", [(102, 102.5, 101.8, 102)] * 78, volume=1000.0)
    assert not [t for t in ib.day_signals("X", "2026-09-02", quiet, 100.0, levels) if t.strategy == "breakout_day"]
    busy = _day("2026-09-02", [(102, 102.5, 101.8, 102)] * 78, volume=3000.0)
    trades = [t for t in ib.day_signals("X", "2026-09-02", busy, 100.0, levels) if t.strategy == "breakout_day"]
    assert trades and trades[0].direction == "long" and trades[0].entry_time == "09:55"


def test_twins_use_other_days_at_the_same_time():
    days = {d: _flat(d) for d in ("2026-09-01", "2026-09-02", "2026-09-03")}
    trade = ib.DayTrade("move_follow", "X", "2026-09-02", "long", "11:00", 100.0, 101.0, "close")
    twins = ib.twins(trade, days, random.Random(1))
    assert len(twins) == 5
    assert all(t.day != "2026-09-02" and t.entry_time == "11:00" and t.signal_day == "2026-09-02" for t in twins)
    assert all(t.direction == "long" and t.strategy == "random:move_follow" for t in twins)


def test_run_skips_the_first_day_and_summarizes():
    days = {"2026-09-01": _flat("2026-09-01"), "2026-09-02": _day("2026-09-02", [(103, 103.5, 102.5, 103)] * 78)}
    trades = ib.run({"X": days})
    rules = [t for t in trades if not t.strategy.startswith("random:")]
    assert rules and all(t.day == "2026-09-02" for t in rules)
    stats = ib.summarize(rules)
    assert stats["trades"] == len(rules) and stats["days"] == 1
