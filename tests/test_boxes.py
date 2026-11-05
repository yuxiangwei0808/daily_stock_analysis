import numpy as np
import pytest

from src.services.trade_desk import boxes as bx
from src.services.trade_desk.backtest import boxes as bb


def _weekdays(n, first="2026-01-05"):
    day, out = np.datetime64(first), []
    while len(out) < n:
        if np.is_busday(day):
            out.append(day)
        day += np.timedelta64(1, "D")
    return out


def _bars(sessions, volume=None):
    """Bars from per-session lists of (open, high, low, close), 15 minutes apart from 09:30."""
    start, rows = [], []
    for day, bars in zip(_weekdays(len(sessions)), sessions):
        for j, bar in enumerate(bars):
            start.append(np.datetime64(f"{day}T09:30") + np.timedelta64(15 * j, "m"))
            rows.append(bar)
    o, h, low, c = (list(x) for x in zip(*rows))
    return bx.Bars(start, o, h, low, c, volume if volume is not None else [1000.0] * len(rows))


QUIET = [(100.0, 100.5, 99.5, 100.0)] * 26  # session range 1.0, so T = 1.0


def _impulse_day(range_bars, after):
    """A gap up to 103 at the open, then the range bars, then ``after``; padded to 26 bars."""
    rows = [(103.0, 103.4, 102.9, 103.0), *range_bars, *after]
    return rows + [rows[-1][3:] * 4] * (26 - len(rows)) if len(rows) < 26 else rows[:26]


SIDEWAYS = [(103.0, 103.2, 102.85, 102.9), (102.9, 103.1, 102.8, 103.1)] * 4  # bodies 102.9 .. 103.1


def _break_and_retest():
    after = [(102.9, 102.95, 102.5, 102.6),   # closes below the range: the break
             (102.6, 102.95, 102.55, 102.7),  # back up to 102.9: the retest, short at the edge
             (102.7, 102.75, 101.0, 101.2),
             (101.2, 101.3, 99.9, 100.1)]     # reaches the base box's top (100): the target
    return _bars([QUIET] * 11 + [_impulse_day(SIDEWAYS, after)] + [QUIET] * 2)


def test_an_opening_gap_is_an_impulse_with_boxes_before_and_after():
    bars = _break_and_retest()
    found = list(bx.setups(bars))
    s = found[0]
    assert [x.up for x in found[1:]] == [False]  # the fall to the target is an impulse too
    assert s.up and s.i == 11 * 26
    assert s.range_box == (102.9, 103.1) and s.base_box == (100.0, 100.0)
    assert s.range_wick == (102.8, 103.4)  # the impulse bar's wick counts toward the stop
    assert s.last == bars.s_last[min(bars.sess[s.i] + 5, len(bars.days) - 1)]


def test_break_and_retest_shorts_the_edge_with_the_stop_past_the_wick_and_the_base_as_target():
    bars = _break_and_retest()
    s = next(bx.setups(bars))
    entry = bx.break_retest(bars, s)
    assert entry.direction == -1 and entry.price == 102.9 and entry.stop == 103.4 and entry.target == 100.0
    assert entry.break_bar == s.i + 9 and entry.bar == s.i + 10
    res = bb.simulate(bars, entry.bar, entry.price, entry.stop, entry.target, entry.direction, s.last)
    assert res["how"] == "target" and res["exit"] == 100.0
    assert res["R"] == pytest.approx((102.9 - 100.0 - bb.COST * 102.9) / 0.5)


def test_decisions_use_no_later_bars():
    bars = _break_and_retest()
    full = [e for s in bx.setups(bars) for e in bx.entries(bars, s)]
    assert full
    for e in full:
        cut = bx.Bars(bars.start[:e.bar + 1], bars.o[:e.bar + 1], bars.h[:e.bar + 1], bars.l[:e.bar + 1],
                      bars.c[:e.bar + 1])
        again = [x for s in bx.setups(cut) for x in bx.entries(cut, s)]
        assert any((x.kind, x.bar, x.price, x.stop, x.target) == (e.kind, e.bar, e.price, e.stop, e.target)
                   for x in again)


def test_a_fade_buys_the_first_return_to_a_box_from_above():
    after = [(103.2, 103.6, 103.15, 103.5), (103.5, 103.55, 103.0, 103.2)]  # leaves above, comes back to 103.1
    bars = _bars([QUIET] * 11 + [_impulse_day(SIDEWAYS, after)] + [QUIET] * 2)
    s = next(bx.setups(bars))
    fade = [e for e in bx.fades(bars, s) if e.box == "range"][0]
    assert fade.direction == 1 and fade.price == 103.1 and fade.stop == 102.8
    assert fade.target == pytest.approx(103.1 + 2 * 0.3)


def test_tidy_boxes_reject_a_range_that_drifts():
    drift = [(103.0 + 0.2 * k, 103.25 + 0.2 * k, 102.95 + 0.2 * k, 103.2 + 0.2 * k) for k in range(8)]
    bars = _bars([QUIET] * 11 + [_impulse_day(drift, [])] + [QUIET])
    normal = next(bx.setups(bars))
    tidy = next(bx.setups(bars, bx.Params(tidy=True)))
    assert normal.range_box is not None and tidy.range_box is None


def _one_session_bars(rows):
    return _bars([rows])


def test_a_gap_past_the_stop_fills_at_the_open():
    bars = _one_session_bars([(100, 100.2, 99.8, 100), (98.0, 98.5, 97.5, 98.2)])
    res = bb.simulate(bars, 0, 100.0, 99.0, 102.0, 1, 1)
    assert res["how"] == "stop" and res["exit"] == 98.0 and res["R"] == pytest.approx(-2.0 - bb.COST * 100)


def test_stop_and_target_in_one_bar_count_as_the_stop_and_the_entry_bar_ignores_the_target():
    bars = _one_session_bars([(100, 102.5, 99.5, 100), (100, 102.5, 98.5, 100), (100, 100.1, 99.9, 100)])
    res = bb.simulate(bars, 0, 100.0, 99.0, 102.0, 1, 2)
    assert res["how"] == "stop" and res["exit_bar"] == 1
    quiet = _one_session_bars([(100, 102.5, 99.5, 100), (100, 100.5, 99.5, 100.3)])
    res = bb.simulate(quiet, 0, 100.0, 99.0, 102.0, 1, 1)
    assert res["how"] == "time" and res["exit"] == 100.3


def test_runs_with_the_same_seed_give_the_same_twins():
    bars = _break_and_retest()
    first = bb.run(bars, "X", bx.Params(), seed=7)
    second = bb.run(bars, "X", bx.Params(), seed=7)
    assert first and [t.twin_R for t in first] == [t.twin_R for t in second]
    assert all(t.kind in ("break_retest", "fade") for t in first)


def test_shuffled_bars_keep_each_bars_shape_and_the_timestamps():
    bars = _break_and_retest()
    mixed = bb.shuffled(bars, 3)
    assert (mixed.start == bars.start).all()
    shape = lambda b: np.sort(np.round(b.h / b.o, 9))  # noqa: E731
    assert np.allclose(shape(mixed), shape(bars))
    assert not np.allclose(mixed.c, bars.c)


def test_the_value_area_holds_most_of_the_volume_around_the_busiest_price():
    rows = [(100.0, 100.2, 100.0, 100.1)] * 120 + [(105.0, 106.0, 105.0, 105.5)] * 10
    volume = [1000.0] * 120 + [1.0] * 10
    bars = _bars([rows[i:i + 26] for i in range(0, 130, 26)], volume)
    (week, (val, vah)), = bx.weekly_value_areas(bars).items()
    assert val <= 100.0 < 100.2 <= vah < 101.0
    assert bx.prior_levels({week: (val, vah)}, bars.start[-1] + np.timedelta64(7, "D")) == []
    nxt = week + np.timedelta64(7, "D")
    assert bx.prior_levels({week: (val, vah), nxt: (1.0, 2.0)}, nxt + np.timedelta64(8, "D")) == [val, vah, 1.0, 2.0]


def test_a_box_is_confirmed_by_a_level_within_its_wicks_plus_a_tenth_of_t():
    assert bx.confirmed((10.0, 11.0), [11.05], 1.0)
    assert not bx.confirmed((10.0, 11.0), [11.2, 9.8], 1.0)


def test_month_t_and_the_pass_bar():
    mean, t, months = bb.month_t([("2026-01", 1.0), ("2026-01", 3.0), ("2026-02", 1.0), ("2026-03", 2.0)])
    assert mean == pytest.approx(1.75) and months == 3 and t == pytest.approx(5.0)
    good = {"edge": 0.1, "t_edge": 2.5, "mean_R": 0.05}
    assert bb.passes({"A": good, "B": good}, 2.0)
    assert not bb.passes({"A": good, "B": {**good, "mean_R": -0.01}}, 2.0)
