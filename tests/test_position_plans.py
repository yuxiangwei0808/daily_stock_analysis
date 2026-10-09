"""Position plans: one action, stop and target per position, trailing stops, the daily review,
your own alerts on top, and one alert when a plan's level is crossed."""
from datetime import date, datetime, timedelta, timezone

from src.services.trade_desk import holdings as h
from src.services.trade_desk import position_plans as pp
from tests.test_broker_holdings import MIDDAY, TODAY, _monitor, repo, store  # noqa: F401  (fixtures)

LEVELS = {"NVDA": {"atr": 5.0, "last_close": 200.0, "low20": 190.0, "high20": 230.0, "ma50": 199.0},
          "SOXS": {"atr": 2.0, "last_close": 30.0, "low20": 28.0, "high20": 35.0, "ma50": 31.0},
          "USO": {"atr": 2.0, "last_close": 150.0, "low20": 140.0, "high20": 160.0, "ma50": 148.0}}


def _geared(ticker):
    return ticker == "SOXS"


def _plans(store):
    return pp.ensure({}, store.view(live=False), LEVELS, TODAY, _geared)


def test_new_positions_get_the_atr_rule_and_closed_ones_are_dropped(store):
    plans = _plans(store)
    assert set(plans) == {"NVDA", "SOXS", "USO 2026-10-16"}
    nvda = plans["NVDA"]
    assert (nvda["stop"], nvda["target"], nvda["stop_source"], nvda["exposure"]) == (190.0, 215.0, "rule", "long")
    assert plans["SOXS"]["target"] is None  # a leveraged/inverse fund gets no ATR target
    assert plans["USO 2026-10-16"]["stop"] == 146.0  # the bull call spread is long USO
    view = store.view(live=False)
    view["stocks"] = [row for row in view["stocks"] if row["ticker"] != "SOXS"]
    assert "SOXS" not in pp.ensure(plans, view, LEVELS, TODAY, _geared)


def test_a_stop_only_moves_toward_the_price():
    plan = {"exposure": "long", "atr": 5.0, "stop": 190.0, "stop_source": "rule", "trail_mark": 200.0}
    assert pp.trail(plan, [204.0, 212.0, 208.0]) and plan["stop"] == 202.0 and plan["trail_mark"] == 212.0
    assert "best close 212.00" in plan["stop_basis"]
    assert not pp.trail(plan, [195.0, 190.0]) and plan["stop"] == 202.0  # a drop never lowers it
    short = {"exposure": "short", "atr": 2.0, "stop": 34.0, "stop_source": "review", "trail_mark": 30.0}
    assert pp.trail(short, [29.0, 27.5]) and short["stop"] == 31.5
    for frozen in ({**plan, "stop_hit_at": "x"}, {**plan, "stop_source": "you"}):
        assert not pp.trail(frozen, [300.0])


def test_closes_since_the_stop_was_set():
    bars = [{"date": f"2026-09-{day:02d}", "close": float(day)} for day in range(20, 26)]
    assert pp.closes_since(bars, "2026-09-23", TODAY, include_through=False) == [23.0, 24.0]
    assert pp.closes_since(bars, "2026-09-23", TODAY, include_through=True) == [23.0, 24.0, 25.0]


def test_a_review_sets_levels_but_never_loosens_a_stop(store):
    view = store.view(live=False)
    plans = _plans(store)
    answer = {"positions": [{"key": "NVDA", "action": "trim", "stop": 185.0, "stop_basis": "below the 50-day",
                             "target": 230.0, "target_basis": "20-day high", "reason": "r", "risk": "k"},
                            {"key": "AAPL", "action": "close", "stop": 1.0}]}
    reviewed = pp.apply_review(plans, answer, view, TODAY, "2026-09-25T21:00:00+00:00")
    nvda = reviewed["NVDA"]
    assert (nvda["stop"], nvda["stop_source"], nvda["target"], nvda["action"]) == (185.0, "review", 230.0, "trim")
    assert "AAPL" not in reviewed  # not held
    looser = pp.apply_review(reviewed, {"positions": [{"key": "NVDA", "action": "hold", "stop": 170.0}]}, view,
                             TODAY, "later")
    assert looser["NVDA"]["stop"] == 185.0 and "would loosen" in looser["NVDA"]["stop_note"]
    tighter = pp.apply_review(reviewed, {"positions": [{"key": "NVDA", "action": "hold", "stop": 195.0}]}, view,
                              TODAY, "later")
    assert tighter["NVDA"]["stop"] == 195.0
    after_hit = {**reviewed, "NVDA": {**reviewed["NVDA"], "stop_hit_at": "x"}}
    rearmed = pp.apply_review(after_hit, {"positions": [{"key": "NVDA", "action": "hold", "stop": 170.0}]}, view,
                              TODAY, "later")
    assert rearmed["NVDA"]["stop"] == 170.0 and rearmed["NVDA"]["stop_hit_at"] is None


def test_your_alerts_win_and_the_panel_lists_urgent_positions_first(store):
    store.add_rule({"position_key": "NVDA", "kind": "price_below", "value": 198})
    store.add_rule({"position_key": "NVDA", "kind": "price_above", "value": 240})
    plans = _plans(store)
    rows = pp.panel_rows(store.view(live=False), plans, store.rules())
    assert rows[0]["key"] == "NVDA" and rows[0]["status"] == "near_stop"  # 200 is within half an ATR of 198
    nvda = rows[0]
    assert (nvda["stop"], nvda["stop_source"], nvda["target"], nvda["target_source"]) == (198, "you", 240, "you")
    assert nvda["stop_distance_pct"] == -1.0 and nvda["target_distance_pct"] == 20.0
    assert pp.status({"exposure": "long", "stop": 190.0, "target": 215.0, "atr": 5.0}, 189.0) == "stop_hit"
    assert pp.status({"exposure": "short", "stop": 210.0, "target": 185.0, "atr": 5.0}, 184.0) == "target_hit"
    assert pp.status({"exposure": "long", "stop": 190.0, "target": 215.0, "atr": 5.0}, 200.0) == "ok"


def test_crossing_a_plan_level_sends_one_card_and_your_own_levels_do_not_repeat(store):
    store.mutate_plans(lambda stored: {**stored, "plans": _plans(store)})
    monitor, events = _monitor(store)
    quotes = store.service.provider("live").quotes
    quotes["NVDA"] = {"price": 189.0}
    monitor.check(MIDDAY)
    [stop] = [payload for _, payload, _ in events if payload["kind"] == "plan_stop"]
    assert stop["card"]["title"] == "NVDA at 189.00: crossed the plan's stop 190.00"
    assert stop["card"]["plan"][0].startswith("The plan's stop is hit")
    assert store.plans()["plans"]["NVDA"]["stop_hit_at"]
    monitor.check(MIDDAY + timedelta(minutes=1))
    assert len([e for e in events if e[1]["kind"] == "plan_stop"]) == 1  # once per level
    # A stop from your own alert fires as your alert, not twice.
    store.add_rule({"position_key": "USO 2026-10-16", "kind": "price_below", "value": 151})
    monitor.check(MIDDAY + timedelta(minutes=2))
    assert not [e for e in events if e[1]["kind"] == "plan_stop" and e[1]["underlying"] == "USO"]


def test_after_the_close_the_stops_trail_and_the_review_runs_once(store, monkeypatch):
    store.mutate_plans(lambda stored: {**stored, "plans": _plans(store)})
    reviews = []
    monkeypatch.setattr(store, "review_positions", lambda day, **kw: reviews.append(day))
    bars = [{"date": f"2026-09-{day:02d}", "open": 1, "high": 211, "low": 199, "close": 200.0 + day % 3, "volume": 1}
            for day in range(1, 26)]
    bars += [{"date": f"2026-08-{day:02d}", "open": 1, "high": 211, "low": 199, "close": 200.0, "volume": 1}
             for day in range(1, 31)]
    bars.sort(key=lambda bar: bar["date"])
    monitor, _ = _monitor(store)
    monitor._bars = lambda tickers: {"NVDA": [*bars[:-1], {**bars[-1], "close": 230.0}]}
    monitor._after_close_day = monitor._summary_day = TODAY
    monitor._tasks["sync"] = monitor._pool.submit(lambda: None)
    evening = datetime(2026, 9, 25, 21, 30, tzinfo=timezone.utc)  # 17:30 New York
    monitor.tick(evening, "closed")
    monitor._tasks["plans"].result(timeout=5)
    nvda = store.plans()["plans"]["NVDA"]
    assert nvda["trail_mark"] == 230.0 and nvda["stop"] > 190.0  # today's close lifted it
    monitor.tick(evening + timedelta(minutes=1), "closed")
    monitor._tasks["review"].result(timeout=5)
    monitor.tick(evening + timedelta(minutes=2), "closed")
    assert reviews == [TODAY]


def test_the_review_updates_the_plans_and_the_panel(store):
    def generate(prompt):
        assert "Daily review of every position" in prompt
        return {"data": {"summary": "Trim NVDA.", "positions": [
            {"key": "NVDA", "action": "trim", "stop": 195.0, "stop_basis": "under the 20-day low", "target": 220.0}]},
            "model": "test-model"}
    store.review_positions(TODAY, generate=generate, bars=lambda tickers, period="2y": {},
                           report=lambda ticker, day: "", earnings_date=lambda ticker, day: None)
    panel = store.plans_view()
    nvda = next(item for item in panel["items"] if item["key"] == "NVDA")
    assert (nvda["action"], nvda["stop"], nvda["stop_source"], nvda["target"]) == ("trim", 195.0, "review", 220.0)
    assert panel["review"]["status"] == "done" and panel["review"]["model"] == "test-model"
    assert panel["review"]["summary"] == "Trim NVDA."

    def broken(prompt):
        raise RuntimeError("down")
    store.review_positions(TODAY, generate=broken, bars=lambda tickers, period="2y": {},
                           report=lambda ticker, day: "", earnings_date=lambda ticker, day: None)
    assert store.plans_view()["review"]["status"] == "failed"
    assert store.plans()["plans"]["NVDA"]["stop"] == 195.0  # a failed review keeps the levels
    store.mutate_plans(lambda stored: {**stored, "review": {"status": "running",
                                                            "started_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}})
    assert store.plans_view()["review"]["status"] == "failed"  # left running by a restart


def test_alert_text_uses_the_plan_levels(store):
    plans = _plans(store)
    plans["NVDA"].update(stop=195.0, stop_source="review", target=225.0, target_source="review")
    store.mutate_plans(lambda stored: {**stored, "plans": plans})
    monitor, events = _monitor(store)
    monitor._levels = {"NVDA": {**LEVELS["NVDA"], "low20": 205.0, "last_close": 210.0}}
    monitor._levels_day = TODAY
    monitor.check(MIDDAY)
    [trend] = [payload for _, payload, _ in events if payload["kind"] == "trend"]
    assert trend["card"]["plan"][-1] == "Levels: stop 195.00 (daily review) · target 225.00 (daily review)"


def test_mixed_positions_get_no_price_levels():
    assert pp.status({"exposure": "mixed", "stop": 1.0}, 2.0) == "ok"
    view = {"stocks": [], "options": [{"key": "SPY 2026-10-16", "underlying": "SPY", "underlying_price": 600.0,
                                       "expired": False, "legs": [{"qty": 1, "strike": 600.0, "right": "call"},
                                                                  {"qty": 1, "strike": 600.0, "right": "put"}]}]}
    [plan] = pp.ensure({}, view, {"SPY": {"atr": 5.0, "last_close": 600.0}}, date(2026, 9, 25), _geared).values()
    assert plan["exposure"] == "mixed" and plan["stop"] is None and plan["target"] is None
    assert h.option_side(view["options"][0]) == "mixed"
