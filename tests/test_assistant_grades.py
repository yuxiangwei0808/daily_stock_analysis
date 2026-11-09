"""Plans you type and the assistant's calls (assistant_grades.py): checks, live steps and the day's grade."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.services.trade_desk import assistant_grades as ag

NY = ZoneInfo("America/New_York")
CLOSE = datetime(2026, 11, 9, 16, 0, tzinfo=NY)


def at(hour, minute, second=0):
    return datetime(2026, 11, 9, hour, minute, second, tzinfo=NY)


def _bars(path):
    """1-minute bars from 09:31, each (open, high, low, close)."""
    return [{"time": (at(9, 31) + timedelta(minutes=k)).isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 100}
            for k, (o, h, l, c) in enumerate(path)]


def test_a_plan_is_checked_against_the_price_and_the_session():
    plan = ag.check_plan({"direction": "long", "trigger": {"kind": "close_above", "price": 580, "bar_minutes": 5},
                          "invalidation": 577, "stop": 578, "target": 583, "time_stop": "15:30", "instrument": "Calls"},
                         579.0, CLOSE, at(10, 0))
    assert plan["direction"] == 1 and plan["trigger"] == {"kind": "close_above", "price": 580.0, "bar_minutes": 5}
    assert plan["time_stop"] == "15:30" and plan["instrument"] == "calls"
    assert ag.check_plan({"direction": "short", "stop": 581, "target": 575}, 579.0, CLOSE, at(10, 0))["time_stop"] == "16:00"
    for bad, why in (({"direction": "up", "stop": 1, "target": 2}, "long or short"),
                     ({"direction": "long", "stop": 581, "target": 583}, "stop goes below"),
                     ({"direction": "long", "trigger": {"kind": "above"}, "stop": 578, "target": 583}, "needs a price"),
                     ({"direction": "long", "trigger": {"kind": "close_above", "price": 580, "bar_minutes": 2}, "stop": 578,
                       "target": 583}, "1-, 5- or 15-minute"),
                     ({"direction": "long", "trigger": {"kind": "above", "price": 580}, "invalidation": 581, "stop": 578,
                       "target": 583}, "invalidation"),
                     ({"direction": "long", "stop": 578, "target": 583, "time_stop": "09:45"}, "already passed"),
                     ({"direction": "long", "stop": 578, "target": 583, "time_stop": "noon"}, "HH:MM")):
        with pytest.raises(ValueError, match=why):
            ag.check_plan(bad, 579.0, CLOSE, at(10, 0))


def test_live_steps_trigger_void_and_exit():
    plan = ag.check_plan({"direction": "long", "trigger": {"kind": "above", "price": 580}, "invalidation": 577,
                          "stop": 578, "target": 583, "time_stop": "11:00"}, 579.0, CLOSE, at(10, 0))
    rec = ag.new_record("SPY", "you", plan, 579.0, at(10, 0), text="long over 580")
    assert rec["status"] == "waiting" and rec["id"].startswith("you|SPY|plan|")
    assert ag.step(rec, 579.5, [], at(10, 1)) is None
    assert ag.step(rec, 580.1, [], at(10, 2)) == "triggered" and rec["price"] == 580.1
    assert ag.step(rec, 582.0, [], at(10, 3)) is None
    assert ag.step(rec, 583.2, [], at(10, 4)) == "target" and rec["live_exit"]["how"] == "target"
    assert ag.step(rec, 577.0, [], at(10, 5)) is None  # one live exit
    void = ag.new_record("SPY", "you", plan, 579.0, at(10, 0))
    assert ag.step(void, 576.9, [], at(10, 1)) == "void" and void["void"] == "invalidated before it triggered"
    late = ag.new_record("SPY", "you", plan, 579.0, at(10, 0))
    assert ag.step(late, 579.0, [], at(11, 0)) == "expired"
    now = ag.new_record("SPY", "assistant", ag.check_plan({"direction": "short", "stop": 581, "target": 575}, 579.0, CLOSE,
                                                          at(10, 0)), 579.0, at(10, 0), why="lost VWAP")
    assert now["status"] == "open" and now["kind"] == "call" and now["price"] == 579.0


def test_a_close_trigger_waits_for_a_completed_bar_of_its_size():
    plan = ag.check_plan({"direction": "long", "trigger": {"kind": "close_above", "price": 580, "bar_minutes": 5},
                          "stop": 578, "target": 583}, 579.0, CLOSE, at(9, 31))
    rec = ag.new_record("SPY", "you", plan, 579.0, at(9, 31))
    bars = _bars([(579, 581, 579, 580.5)] * 4)  # 09:31 .. 09:34 done, above 580, but no 5-minute close yet
    assert ag.step(rec, 580.5, bars, at(9, 34, 30)) is None
    bars = _bars([(579, 581, 579, 580.5)] * 5)
    assert ag.step(rec, 580.6, bars, at(9, 35, 1)) == "triggered"  # the 09:35 bar closed above 580
    below = ag.new_record("SPY", "you", plan, 579.0, at(9, 31))
    assert ag.step(below, 579.9, _bars([(579, 580, 579, 579.8)] * 5), at(9, 35, 1)) is None


def test_the_grade_comes_from_the_days_bars_with_twins_at_random_minutes():
    plan = ag.check_plan({"direction": "long", "trigger": {"kind": "above", "price": 100.5}, "stop": 99.5, "target": 102.5,
                          "time_stop": "15:00"}, 100.0, CLOSE, at(9, 40))
    rec = ag.new_record("XYZ", "you", plan, 100.0, at(9, 40))
    rec.update(status="open", entry_time=at(9, 45, 10).isoformat(), price=100.5)
    # Up 0.01 a minute all day, with the 10:30 bar spiking to the target.
    path = [(100 + 0.01 * k, 100 + 0.01 * k + 0.02, 100 + 0.01 * k - 0.02, 100 + 0.01 * k + 0.01) for k in range(390)]
    path[59] = (path[59][0], 103.0, path[59][2], path[59][3])
    bars = _bars(path)
    assert ag.settle(rec, bars, CLOSE)
    assert rec["status"] == "closed" and rec["how"] == "target" and rec["exit"] == 102.5 and rec["month"] == "2026-11-09"
    assert rec["R"] == pytest.approx((2.0 - ag.COST * 100.5) / 1.0, abs=1e-4)
    assert rec["twin_R"] is not None and rec["twin_R"] < rec["R"]  # most random minutes missed the spike
    again = dict(rec)
    assert ag.settle(again, bars, CLOSE) is False  # graded once
    waiting = ag.new_record("XYZ", "you", plan, 100.0, at(9, 40))
    assert ag.settle(waiting, bars, CLOSE) and waiting["status"] == "expired"


def test_stops_count_first_gaps_fill_at_the_open_and_the_entry_bar_ignores_targets():
    bars = _bars([(100, 103, 99, 100), (100, 101, 100, 100.5), (97, 98, 96, 97)])
    assert ag.simulate(bars, 0, 100, 99.5, 102, 1, 2) == (99.5, "stop", 0)  # both in the entry bar: the stop
    assert ag.simulate(bars, 1, 100, 99.5, 102, 1, 2) == (97.0, "stop", 2)  # gapped through: the open
    assert ag.simulate(_bars([(100, 103, 100, 101), (101, 101, 100.5, 101)]), 0, 100, 99, 102, 1, 1) == (101.0, "time", 1)
    short = _bars([(100, 100.2, 99.8, 100), (100, 100.1, 97, 98)])
    assert ag.simulate(short, 0, 100, 101, 98, -1, 1) == (98, "target", 1)


LONG = {"key": "SPY|C735", "label": "SPY 11/09 735C", "exposure": "long"}
SHORT = {"key": "SPY|P735", "label": "SPY 11/09 735P", "exposure": "short"}


def test_a_trim_or_exit_names_a_held_side_and_stands_until_its_time_stop():
    sale = ag.check_sale({"action": "exit", "trigger": {"kind": "below", "price": 578}}, LONG, CLOSE, at(10, 0))
    assert sale["action"] == "exit" and sale["direction"] == 1 and sale["time_stop"] == "16:00"
    assert sale["stop"] is None and sale["target"] is None and sale["position_key"] == "SPY|C735"
    rec = ag.new_record("SPY", "assistant", sale, 579.0, at(10, 0))
    assert rec["kind"] == "exit" and rec["id"].startswith("assistant|SPY|exit|") and rec["status"] == "waiting"
    assert ag.view(rec)["position"] == "SPY 11/09 735C" and ag.view(rec)["action"] == "exit"
    now = ag.new_record("SPY", "assistant", ag.check_sale({"action": "trim", "time_stop": "11:00"}, SHORT, CLOSE, at(10, 0)),
                        579.0, at(10, 0))
    assert now["status"] == "open" and now["price"] == 579.0 and now["direction"] == -1
    for bad, position, why in (({"action": "hold"}, LONG, "trim or an exit"),
                               ({"action": "exit"}, {**LONG, "exposure": "neutral"}, "neither long nor short"),
                               ({"action": "exit", "trigger": {"kind": "above"}}, LONG, "needs a price"),
                               ({"action": "exit", "time_stop": "09:45"}, LONG, "already passed")):
        with pytest.raises(ValueError, match=why):
            ag.check_sale(bad, position, CLOSE, at(10, 0))


def test_a_sale_triggers_at_its_price_and_keeps_its_time_stop_without_an_alert():
    rec = ag.new_record("SPY", "assistant", ag.check_sale({"action": "exit", "trigger": {"kind": "below", "price": 578},
                                                           "time_stop": "11:00"}, LONG, CLOSE, at(10, 0)), 579.0, at(10, 0))
    assert ag.step(rec, 578.5, [], at(10, 1)) is None
    assert ag.step(rec, 577.9, [], at(10, 2)) == "triggered" and rec["price"] == 577.9  # no stop or target to void it
    assert ag.step(rec, 570.0, [], at(10, 30)) is None
    assert ag.step(rec, 576.0, [], at(11, 0)) == "measured" and rec["live_exit"] == {"how": "held", "price": 576.0,
                                                                                   "time": at(11, 0).isoformat()}
    late = ag.new_record("SPY", "assistant", ag.check_sale({"action": "trim", "trigger": {"kind": "above", "price": 590},
                                                            "time_stop": "11:00"}, LONG, CLOSE, at(10, 0)), 579.0, at(10, 0))
    assert ag.step(late, 580.0, [], at(11, 0)) == "expired"


def test_a_sale_is_graded_against_holding_to_its_time_stop_and_against_random_minutes():
    # Up 0.01 a minute until 12:00, then down 0.02 a minute: selling near the top beats holding and most random minutes.
    path = []
    for k in range(390):
        mid = 100 + 0.01 * min(k, 149) - 0.02 * max(k - 149, 0)
        path.append((mid, mid + 0.01, mid - 0.01, mid))
    bars = _bars(path)
    sale = ag.check_sale({"action": "exit", "trigger": {"kind": "now"}}, LONG, CLOSE, at(9, 40))
    rec = ag.new_record("XYZ", "assistant", sale, 101.49, at(12, 0, 5))
    rec.update(entry_time=at(12, 0, 5).isoformat(), price=101.49)
    assert ag.settle(rec, bars, CLOSE)
    held = path[-1][3]
    assert rec["status"] == "closed" and rec["how"] == "held" and rec["exit"] == pytest.approx(held)
    assert rec["saved_pct"] == pytest.approx(-(held - 101.49) / 101.49 * 100, abs=1e-4) and rec["saved_pct"] > 0
    assert rec["twin_saved_pct"] is not None and rec["twin_saved_pct"] < rec["saved_pct"]
    short = ag.new_record("XYZ", "assistant", ag.check_sale({"action": "exit"}, SHORT, CLOSE, at(9, 40)), 101.49, at(12, 0, 5))
    short.update(entry_time=at(12, 0, 5).isoformat(), price=101.49)
    assert ag.settle(short, bars, CLOSE) and short["saved_pct"] == pytest.approx(-rec["saved_pct"], abs=1e-4)
    assert ag.view(rec)["saved_pct"] == rec["saved_pct"] and "R" not in ag.view(rec)


def test_on_random_walks_a_sale_at_a_random_minute_shows_no_edge_over_its_twins():
    """Methodology §5: the rule and its twins on random data must differ by about zero."""
    import numpy as np
    rng = np.random.default_rng(20261109)
    edges = []
    for day in range(300):
        closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.0008, 391)))
        path = [(closes[k], max(closes[k], closes[k + 1]) * 1.0002, min(closes[k], closes[k + 1]) * 0.9998, closes[k + 1])
                for k in range(390)]
        bars = _bars(path)
        k = int(rng.integers(4, 380))  # a bar from 09:35
        side = LONG if day % 2 else SHORT
        rec = ag.new_record("XYZ", "assistant", ag.check_sale({"action": "exit"}, side, CLOSE, at(9, 31)), path[k][0], at(9, 31))
        rec.update(entry_time=(at(9, 30) + timedelta(minutes=k, seconds=1)).isoformat(), price=round(float(path[k][0]), 4))
        assert ag.settle(rec, bars, CLOSE)
        edges.append(rec["saved_pct"] - rec["twin_saved_pct"])
    edges = np.array(edges)
    assert abs(edges.mean()) < 3 * edges.std(ddof=1) / np.sqrt(len(edges))
