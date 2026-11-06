"""Shadow mode for exits on the live feed: what would have been sold, when and at what quote; nothing is ever sent."""
import asyncio
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.trade_desk import holdings as h
from src.services.trade_desk import live_feed as lf
from src.services.trade_desk import position_plans as pp
from src.services.trade_desk import providers
from src.services.trade_desk import shadow_exits as se
from src.services.trade_desk import shadow_watch as sw
from src.services.trade_desk.providers import MoomooProvider, ProviderError
from tests.test_broker_holdings import MIDDAY, TODAY, _monitor, repo, store  # noqa: F401  (fixtures)

LEVELS = {"NVDA": {"atr": 5.0, "last_close": 200.0, "low20": 190.0, "high20": 230.0, "ma50": 199.0},
          "SOXS": {"atr": 2.0, "last_close": 30.0, "low20": 28.0, "high20": 35.0, "ma50": 31.0},
          "USO": {"atr": 2.0, "last_close": 150.0, "low20": 140.0, "high20": 160.0, "ma50": 148.0},
          "AAPL": {"atr": 4.0, "last_close": 300.0, "low20": 280.0, "high20": 320.0, "ma50": 295.0}}
SINGLE = {"code": "US.AAPL261016C300000", "name": "AAPL 261016 300.00C", "qty": 2.0, "side": "LONG",
          "average_cost": 10.0, "price": 10.0}
CALL = "AAPL261016C300000"
SESSIONS = [date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2),
            date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]


class FakeFeed:
    """The live feed's memory, filled from the fixture's provider; each check sees it just arrived."""

    def __init__(self, store):
        self.store, self.clock = store, 0.0
        self.alive_now = self.pushing_now = True
        self.reads, self.refreshes, self.closed = [], 0, 0
        self.watched = ([], [])
        self.fill_prices = {}
        self.source, self.check_ms = "push", 0.0
        self.opened = None

    def _provider(self):
        return self.store.service.provider("live")

    def open(self, on_update, on_fill):
        self.opened = (on_update, on_fill)

    def close(self):
        self.closed += 1

    def adopt(self, earlier):
        self.adopted = earlier

    def raw(self):
        return {**self.held, "synced_at": self.now.isoformat()}  # in memory, like the real feed

    def read_positions(self, now, *, refresh=False):
        self.reads.append(refresh)
        return True

    def refresh(self):
        self.refreshes += 1

    def watch(self, quote_codes, book_codes):
        self.watched = (list(quote_codes), list(book_codes))

    def fills(self):
        return self.fill_prices

    def quotes(self, now, opening=None):
        arrived = now - timedelta(milliseconds=self.check_ms)
        return {code: {**quote, "session": "regular", "fetched_at": arrived.isoformat(), "received": arrived,
                       "data_at": arrived - timedelta(seconds=1), "source": self.source}
                for code, quote in self._provider().quotes.items()}

    def alive(self, now):
        return self.alive_now

    def pushing(self, now):
        return self.pushing_now

    def status(self, now):
        return {"pushes": {"quote": 5, "book": 7, "fill": 0}, "stock_quote_s": {"median": 0.5, "p95": 1.0, "n": 5},
                "option_quote_s": None, "book_s": None, "fill_to_position_s": None, "subscribed": {"quote": 2, "order_book": 1},
                "refused": 0, "last_quote_s": 0.2, "positions_read_s": 3.0}


class Harness:
    """The watcher with its check run by hand, as if it had been watching until just before."""

    def __init__(self, store):
        self.store = store
        self.monitor, self.events = _monitor(store)
        self.feed = FakeFeed(store)
        self.watch = sw.ShadowWatch(store, self.monitor, feed_factory=lambda: self.feed, clock=lambda: self.feed.clock)
        self.watch._open()

    def check(self, now, watching=True):
        self.feed.now, self.feed.held = now, self.store.raw()
        self.watch._plans, self.watch._rules = self.store.plans()["plans"], self.store.rules()
        if watching:
            self.watch._watched_at = now - timedelta(seconds=1)
        self.watch.check(now)
        self.watch.save(force=True)


def _with_plans(store):
    store.mutate_plans(lambda stored: {**stored, "plans": pp.ensure(stored["plans"], store.view(live=False), LEVELS, TODAY,
                                                                    lambda ticker: ticker == "SOXS")})


def _provider(store):
    return store.service.provider("live")


def _hold_single(store, quote=None):
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [*provider.raw["positions"], SINGLE]}
    provider.quotes.update({CALL: quote or {"price": 10.0, "bid": 9.9, "ask": 10.1}, "AAPL": {"price": 300.0}})
    store.sync()
    _with_plans(store)


def _drop(store, code):
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [row for row in provider.raw["positions"] if row["code"] != code]}
    store.sync()


def _started(store):
    """A watcher past shadow mode's first check, so later crossings count as new."""
    harness = Harness(store)
    harness.check(MIDDAY - timedelta(minutes=10))
    return harness


def _exits(store):
    return (store.repo.setting(se.EXITS_SETTING, {}) or {}).get("exits") or []


def _one(store, kind):
    [record] = [record for record in _exits(store) if record["kind"] == kind and record["ticker"] in {"NVDA", "AAPL", "USO"}]
    return record


def _before_close(day):
    return datetime(day.year, day.month, day.day, 19, 55, tzinfo=timezone.utc)  # 15:55 New York


def test_no_order_call_exists_anywhere_in_the_app():
    root = Path(__file__).resolve().parents[1]
    calls = re.compile(r"\b(place_order|modify_order|cancel_order|cancel_all_order|unlock_trade)\b")
    found = [str(path) for folder in ("src", "api", "bot", "data_provider", "scripts") for path in (root / folder).rglob("*.py")
             if calls.search(path.read_text(encoding="utf-8", errors="ignore"))]
    assert not found
    source = (root / "src/services/trade_desk/shadow_exits.py").read_text()
    assert "provider" not in source and "OpenSecTradeContext" not in source  # quotes are handed in
    # The kept-open trade connection can read positions and be closed, nothing else.
    assert {name for name in vars(providers.AccountWatch) if not name.startswith("_")} == {"positions", "close"}


def test_a_crossed_stop_records_a_would_be_sale_at_the_bid_and_sends_nothing(store):
    _with_plans(store)
    watch = _started(store)
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 188.9, "ask": 189.1}
    synced = _provider(store).synced
    watch.check(MIDDAY)
    watch.check(MIDDAY + timedelta(seconds=1))  # still below: one record per level
    record = _one(store, "stop")
    assert record["level"] == 190.0 and record["source"] == "rule" and record["price"] == 189.0
    assert record["exit"]["fill"] == 188.9 and record["exit"]["pnl_pct"] == 57.42
    assert record["exit"]["legs"] == [{"code": "NVDA", "bid": 188.9, "ask": 189.1}]
    assert not record["skip"] and record["exit"]["wait_s"] == 0.0
    assert watch.events == []  # Discord alerts stay with the one-minute loop
    assert _provider(store).synced == synced  # no extra broker call


def test_a_record_says_how_the_crossing_was_seen(store):
    _with_plans(store)
    watch = _started(store)
    watch.feed.check_ms = 40.0
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 188.9, "ask": 189.1}
    watch.check(MIDDAY)
    assert _one(store, "stop")["seen"] == {"source": "push", "check_ms": 40.0, "quote_age_s": 1.0}
    speed = store.shadow()["speed"]
    assert speed["from_push"] == 1 and speed["from_snapshot"] == 0
    assert speed["check_ms"]["median"] == 40.0 and speed["exit_wait_s"]["median"] == 0.0


def test_a_check_needs_neither_the_network_nor_the_database(store):
    _with_plans(store)
    watch = _started(store)
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 188.9, "ask": 189.1}
    watch.feed.now, watch.feed.held = MIDDAY, store.raw()
    watch.watch._watched_at = MIDDAY - timedelta(seconds=1)
    original = store.repo.setting, store.repo.set_setting, watch.feed.read_positions, watch.feed.refresh

    def forbidden(*args, **kwargs):
        raise AssertionError("a check must not wait on anything")
    store.repo.setting = store.repo.set_setting = watch.feed.read_positions = watch.feed.refresh = forbidden
    watch.watch.check(MIDDAY)
    store.repo.setting, store.repo.set_setting, watch.feed.read_positions, watch.feed.refresh = original
    assert watch.watch._exits_dirty
    watch.watch.save(force=True)
    assert _one(store, "stop")["exit"]["fill"] == 188.9


def test_an_order_waits_for_a_usable_quote_and_the_wait_is_recorded(store):
    _hold_single(store, {"price": 3.0, "bid": 0.30, "ask": 5.70})  # far too wide to sell into
    watch = _started(store)
    _provider(store).quotes["AAPL"] = {"price": 289.0}  # through the 2 ATR stop at 292
    watch.check(MIDDAY)
    record = _one(store, "stop")
    assert record["exit"] is None and record["problems"] == ["wide_quote"]
    _provider(store).quotes[CALL] = {"price": 0.05, "bid": 0, "ask": 0.08}
    watch.check(MIDDAY + timedelta(seconds=30))
    assert _one(store, "stop")["problems"] == ["no_bid"]
    _provider(store).quotes[CALL] = {"price": 3.0, "bid": 2.9, "ask": 3.1}
    later = MIDDAY + timedelta(minutes=2)
    watch.check(later)
    record = _one(store, "stop")
    assert record["exit"]["at"] == later.isoformat() and record["exit"]["fill"] == 2.9 and record["problems"] == []
    assert record["exit"]["pnl_pct"] == -71.0 and record["exit"]["wait_s"] == 120.0


def test_an_order_that_cannot_go_in_time_gives_up_instead_of_grading_a_later_price(store):
    _hold_single(store, {"price": 13.0, "bid": 1.0, "ask": 25.0})  # up on paper, far too wide to sell
    watch = _started(store)
    _provider(store).quotes["AAPL"] = {"price": 289.0}
    watch.check(MIDDAY)
    assert _one(store, "stop")["exit"] is None
    _provider(store).quotes[CALL] = {"price": 3.0, "bid": 2.9, "ask": 3.1}  # an hour later, a much worse price
    watch.check(MIDDAY + timedelta(hours=1))
    record = _one(store, "stop")
    assert record["exit"] is None and record["ended"] == "no_exit" and record["gave_up"] is True
    assert store.shadow()["speed"]["gave_up"] == 1
    assert next(item for item in store.shadow()["recent"] if item["kind"] == "stop")["gave_up"] is True


def test_stale_quotes_and_an_old_position_list_block_the_order():
    record = {"legs": [{"code": CALL, "ratio": 1.0}]}
    fresh = {"price": 3.0, "bid": 2.9, "ask": 3.1, "updated_at": MIDDAY.isoformat()}
    assert se.blockers(record, {CALL: fresh}, MIDDAY, MIDDAY.isoformat()) == []
    assert se.blockers(record, {}, MIDDAY, MIDDAY.isoformat()) == ["stale_quote"]
    assert se.blockers(record, {CALL: fresh}, MIDDAY, (MIDDAY - timedelta(minutes=2)).isoformat()) == ["old_position"]
    assert se.blockers(record, {CALL: fresh}, MIDDAY, None) == ["old_position"]


def test_a_spread_is_valued_leg_by_leg_and_flagged_for_the_first_live_version(store):
    _with_plans(store)
    watch = _started(store)
    _provider(store).quotes["USO"] = {"price": 145.0}  # through the 2 ATR stop at 146
    watch.check(MIDDAY)
    record = _one(store, "stop")
    assert record["skip"] == "multi_leg"
    assert [leg["ratio"] for leg in record["legs"]] == [1.0, -1.0]
    # Sell the long leg at its 3.30 bid, buy the short leg back at its 1.65 ask, on a net cost of 2.19.
    assert record["exit"]["fill"] == 1.65 and record["exit"]["mid"] == 1.8 and record["exit"]["pnl_pct"] == -24.66
    rows = {row["label"]: row for row in store.shadow()["rows"]}
    assert rows["Spreads (the first live version would skip these)"]["positions"] == 1


def test_sizes_and_amounts_are_never_stored(store):
    _with_plans(store)
    watch = _started(store)
    _provider(store).quotes["USO"] = {"price": 145.0}
    watch.check(MIDDAY)
    watch.watch._save_feed_day(watch.feed, MIDDAY)  # sizes come from the position list; none of them reach here
    stored = "".join(json.dumps(store.repo.setting(key)) for key in (se.EXITS_SETTING, se.LEVELS_SETTING, se.FEED_SETTING))
    assert '"qty"' not in stored and "total_assets" not in stored and "weight_pct" not in stored and "average_cost" not in stored


def test_follow_up_grades_selling_against_holding_for_ten_sessions(store):
    _with_plans(store)
    watch = _started(store)
    quotes = _provider(store).quotes
    quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}
    watch.check(MIDDAY)
    assert _one(store, "stop")["marks"] == []  # the mark waits for the last minutes of the session
    for index, day in enumerate(SESSIONS):
        quotes["NVDA"] = {"price": 180.0 - index, "bid": 180.0 - index, "ask": 180.0 - index}
        watch.check(_before_close(day))
        quotes["NVDA"] = {"price": 170.0 - index, "bid": 170.0 - index, "ask": 170.0 - index}
        watch.check(_before_close(day) + timedelta(minutes=3))  # one mark a day, and the last one stands
    record = _one(store, "stop")
    assert [mark["n"] for mark in record["marks"]] == list(range(11))
    assert record["marks"][0]["price"] == 170.0
    assert record["ended"] is None  # the last day's mark is still moving
    watch.check(MIDDAY.replace(month=10, day=12))
    assert _one(store, "stop")["ended"] == "done"
    row = store.shadow()["rows"][0]
    assert row["label"] == "All positions" and row["finished"] == 1 and row["running"] == 0
    assert row["if_sold_pct"] == 57.5  # (189 - 120) / 120
    assert row["if_held_5_pct"] == 37.5  # 165
    assert row["if_held_10_pct"] == 33.33  # 160
    assert row["yours_pct"] == 33.33  # you held it
    assert row["sold_minus_held_pct"] == 24.17 and row["sold_better_pct"] == 100
    assert se.codes(store.repo.setting(se.EXITS_SETTING), SESSIONS[-1]) == []  # a finished record is no longer quoted


def test_an_option_held_to_expiry_is_worth_what_it_settles_at(store):
    _hold_single(store)
    watch = _started(store)
    quotes = _provider(store).quotes
    quotes["AAPL"] = {"price": 289.0}
    quotes[CALL] = {"price": 4.0, "bid": 4.0, "ask": 4.0}
    watch.check(MIDDAY)
    expiry = date(2026, 10, 16)
    quotes[CALL] = {"price": 9.0, "bid": 2.0, "ask": 16.0}  # the book in the last minutes says little
    quotes["AAPL"] = {"price": 302.0}
    watch.check(_before_close(expiry))
    assert _one(store, "stop")["marks"][-1]["value"] == 2.0
    del quotes[CALL]
    quotes["AAPL"] = {"price": 305.0}
    watch.check(_before_close(expiry) + timedelta(minutes=4, seconds=59))  # 15:59:59
    record = _one(store, "stop")
    assert record["marks"][-1]["value"] == 5.0 and record["marks"][-1]["pnl_pct"] == -50.0
    assert record["ended"] is None
    watch.check(datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc))
    record = _one(store, "stop")
    assert record["ended"] == "done"
    grade = se.grade(record)
    assert grade["finished"] and grade["if_sold_pct"] == -60.0 and grade["if_held_5_pct"] == grade["if_held_10_pct"] == -50.0


def test_closing_it_yourself_uses_your_fill_and_it_keeps_being_followed(store):
    _with_plans(store)
    watch = _started(store)
    quotes = _provider(store).quotes
    quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}
    watch.check(MIDDAY)
    _drop(store, "US.NVDA")
    quotes["NVDA"] = {"price": 186.0, "bid": 186.0, "ask": 186.0}
    filled = MIDDAY + timedelta(seconds=2)
    watch.feed.fill_prices = {"NVDA": {"sell": {"price": 185.0, "at": filled}, "buy": {"price": 150.0, "at": filled}}}
    watch.check(MIDDAY + timedelta(seconds=3))
    closed = _one(store, "stop")["closed"]
    assert closed["pnl_pct"] == 54.17 and closed["from"] == "fill"  # sold at 185; the earlier purchase is not the close
    for day in SESSIONS:
        quotes["NVDA"] = {"price": 150.0, "bid": 150.0, "ask": 150.0}
        watch.check(_before_close(day))
    watch.check(MIDDAY.replace(month=10, day=12))
    row = store.shadow()["rows"][0]
    assert row["finished"] == 1 and row["yours_pct"] == 54.17 and row["if_held_10_pct"] == 25.0


def test_a_close_with_no_fill_heard_uses_the_mid_when_it_was_seen_gone(store):
    _with_plans(store)
    watch = _started(store)
    quotes = _provider(store).quotes
    quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}
    watch.check(MIDDAY)
    _drop(store, "US.NVDA")
    quotes["NVDA"] = {"price": 186.0, "bid": 186.0, "ask": 186.0}
    earlier = MIDDAY - timedelta(minutes=1)
    # A purchase cannot have closed a long position, and a trim before the trigger is not this close.
    watch.feed.fill_prices = {"NVDA": {"buy": {"price": 150.0, "at": MIDDAY + timedelta(seconds=1)}, "sell": {"price": 170.0, "at": earlier}}}
    watch.check(MIDDAY + timedelta(seconds=15))
    closed = _one(store, "stop")["closed"]
    assert closed["pnl_pct"] == 55.0 and closed["from"] == "mid"


def test_closing_it_before_the_order_could_go_counts_as_no_exit(store):
    _hold_single(store, {"price": 3.0, "bid": 0.30, "ask": 5.70})
    watch = _started(store)
    _provider(store).quotes["AAPL"] = {"price": 289.0}
    watch.check(MIDDAY)
    _drop(store, SINGLE["code"])
    watch.check(MIDDAY + timedelta(seconds=15))
    record = _one(store, "stop")
    assert record["ended"] == "no_exit" and record["exit"] is None and not record.get("gave_up")
    assert store.shadow()["rows"][0]["no_exit"] == 1


def test_built_in_review_and_your_own_levels_all_trigger(store):
    _hold_single(store)
    watch = _started(store)
    store.add_rule({"position_key": "NVDA", "kind": "price_above", "value": 195})
    store.mutate_plans(lambda stored: {**stored, "plans": {key: {**plan, **({"action": "close", "reviewed_at": "x", "reason": "weak"}
                                                                             if key == "AAPL 2026-10-16" else {})}
                                                           for key, plan in stored["plans"].items()}})
    quotes = _provider(store).quotes
    quotes[CALL] = {"price": 4.0, "bid": 4.0, "ask": 4.1}  # about -60 % on cost
    watch.check(MIDDAY)
    found = {record["kind"]: record for record in _exits(store)}
    assert found["rule_loss"]["level"] == -50.0 and found["rule_loss"]["source"] == "built-in"
    assert found["review"]["level"] == "close" and found["review"]["basis"] == "weak"
    assert found["target"]["ticker"] == "NVDA" and found["target"]["source"] == "you" and found["target"]["level"] == 195


def test_levels_already_crossed_at_the_start_are_counted_but_not_graded(store):
    _with_plans(store)
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}  # the stop was hit days ago
    watch = Harness(store)
    watch.check(MIDDAY)
    watch.check(MIDDAY + timedelta(minutes=1))
    [record] = _exits(store)
    assert record["ended"] == "carried" and record["exit"] is None and record["marks"] == []
    assert se.codes(store.repo.setting(se.EXITS_SETTING), TODAY) == []
    result = store.shadow()
    assert result["carried"] == 1 and result["recorded"] == 0 and result["rows"] == [] and result["recent"] == []
    # A level crossed after the first few minutes is new.
    _provider(store).quotes["SOXS"] = {"price": 25.0, "bid": 25.0, "ask": 25.0}
    watch.check(MIDDAY + timedelta(minutes=6))
    assert store.shadow()["recorded"] == 1


def test_a_level_found_crossed_after_a_gap_has_no_trigger_moment(store):
    _with_plans(store)
    watch = _started(store)
    watch.check(MIDDAY)
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}
    watch.check(MIDDAY + timedelta(minutes=10), watching=False)  # the server was off, or the feed lost
    assert _one(store, "stop")["ended"] == "carried"
    # Once watching again, the next crossing is new, and a short gap does not count as one.
    _provider(store).quotes["SOXS"] = {"price": 25.0, "bid": 25.0, "ask": 25.0}
    watch.check(MIDDAY + timedelta(minutes=10, seconds=45), watching=False)
    assert store.shadow()["recorded"] == 1


def test_restarting_before_the_open_does_not_count_as_a_gap(store):
    _with_plans(store)
    watch = _started(store)
    watch.watch._watched_at = None  # the server restarted overnight
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}  # opened below the stop
    watch.check(datetime(2026, 9, 28, 13, 30, 5, tzinfo=timezone.utc), watching=False)  # 09:30:05 New York
    assert _one(store, "stop")["ended"] is None and _one(store, "stop")["exit"]
    watch.watch._watched_at = None  # restarted during the session instead
    _provider(store).quotes["SOXS"] = {"price": 25.0, "bid": 25.0, "ask": 25.0}
    watch.check(datetime(2026, 9, 28, 13, 40, tzinfo=timezone.utc), watching=False)
    assert [record["ended"] for record in _exits(store) if record["ticker"] == "SOXS"] == ["carried"]


def test_a_frozen_feed_checks_nothing_and_that_time_counts_as_not_watching(store):
    _with_plans(store)
    watch = _started(store)
    watch.check(MIDDAY)
    watch.feed.alive_now = False
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}
    watch.check(MIDDAY + timedelta(minutes=5), watching=False)
    assert not [record for record in _exits(store) if record["kind"] == "stop"]
    watch.feed.alive_now = True
    watch.check(MIDDAY + timedelta(minutes=6), watching=False)
    assert _one(store, "stop")["ended"] == "carried"


def test_closing_and_reopening_can_trigger_a_level_again_even_a_carried_one(store):
    _with_plans(store)
    provider = _provider(store)
    provider.quotes["NVDA"] = {"price": 189.0, "bid": 189.0, "ask": 189.0}
    watch = Harness(store)
    watch.check(MIDDAY - timedelta(minutes=10))
    assert [record["ended"] for record in _exits(store) if record["key"] == "NVDA"] == ["carried"]
    row = next(item for item in provider.raw["positions"] if item["code"] == "US.NVDA")
    _drop(store, "US.NVDA")
    watch.check(MIDDAY)
    assert "NVDA" not in store.repo.setting(se.EXITS_SETTING)["holdings"]
    provider.raw = {**provider.raw, "positions": [*provider.raw["positions"], row]}
    store.sync()
    watch.check(MIDDAY + timedelta(minutes=1))
    records = [record for record in _exits(store) if record["key"] == "NVDA"]
    assert [record["ended"] for record in records] == ["carried", None]
    assert records[1]["exit"] and records[1]["held_since"] == (MIDDAY + timedelta(minutes=1)).isoformat()
    # The same list of positions again does not fire it twice.
    watch.check(MIDDAY + timedelta(minutes=2))
    assert len([record for record in _exits(store) if record["key"] == "NVDA"]) == 2


def test_a_stock_breaking_its_20_day_low_triggers_once(store):
    _with_plans(store)
    watch = _started(store)
    watch.monitor._levels = LEVELS
    watch.monitor._levels_day = TODAY
    watch.check(MIDDAY - timedelta(seconds=1))
    _provider(store).quotes["NVDA"] = {"price": 189.5}
    watch.check(MIDDAY)
    watch.monitor._levels = {**LEVELS, "NVDA": {**LEVELS["NVDA"], "low20": 189.9}}  # the level moves, the record does not
    watch.check(MIDDAY + timedelta(seconds=1))
    record = _one(store, "trend")
    assert record["basis"] == "below the 20-day low 190.00" and record["ended"] is None


def test_only_todays_20_day_lows_count_and_late_ones_have_no_trigger_moment(store):
    _with_plans(store)
    watch = _started(store)
    _provider(store).quotes["NVDA"] = {"price": 189.5}
    watch.monitor._levels = LEVELS
    watch.monitor._levels_day = TODAY - timedelta(days=1)  # yesterday's: not used
    watch.check(MIDDAY)
    assert not [record for record in _exits(store) if record["kind"] == "trend"]
    watch.monitor._levels_day = TODAY  # today's arrive hours late, with the stock already below
    watch.check(MIDDAY + timedelta(seconds=1))
    assert _one(store, "trend")["ended"] == "carried"
    # On time, the break at the open is real.
    opening = datetime(2026, 9, 28, 13, 30, 20, tzinfo=timezone.utc)
    watch.monitor._levels_day = opening.date()
    _provider(store).quotes["SOXS"] = {"price": 27.0, "bid": 27.0, "ask": 27.0}
    watch.check(opening)
    assert [record["ended"] for record in _exits(store) if record["ticker"] == "SOXS" and record["kind"] == "trend"] == [None]


def test_after_a_gap_a_holding_not_priced_yet_is_still_carried_when_its_quote_arrives(store):
    _with_plans(store)
    watch = _started(store)
    watch.check(MIDDAY)
    provider = _provider(store)
    all_quotes = dict(provider.quotes)
    provider.quotes.clear()
    provider.quotes["SPY"] = {"price": 600.0}  # reconnected: the feed is alive, NVDA is not subscribed yet
    watch.check(MIDDAY + timedelta(minutes=10), watching=False)
    assert not _exits(store)
    provider.quotes.update(all_quotes)
    provider.quotes["NVDA"] = {"price": 189.0, "bid": 188.9, "ask": 189.1}  # it fell during the gap
    watch.check(MIDDAY + timedelta(minutes=10, seconds=1), watching=False)
    assert _one(store, "stop")["ended"] == "carried"
    # Priced once, its next crossing is real, and so is a position opened from here on.
    provider.quotes["SOXS"] = {"price": 25.0, "bid": 25.0, "ask": 25.0}
    watch.check(MIDDAY + timedelta(minutes=10, seconds=2), watching=False)
    assert [record["ended"] for record in _exits(store) if record["ticker"] == "SOXS"] == [None]
    _hold_single(store)
    watch.check(MIDDAY + timedelta(minutes=10, seconds=3), watching=False)
    provider.quotes[CALL] = {"price": 4.0, "bid": 4.0, "ask": 4.1}
    watch.check(MIDDAY + timedelta(minutes=10, seconds=4), watching=False)
    assert [record["ended"] for record in _exits(store) if record["kind"] == "rule_loss"] == [None]


def _replace_with_puts(store, position_cost=6.0):
    """Out of the AAPL calls, into puts on the same expiry: the same plan key, new contracts."""
    put = {"code": "US.AAPL261016P280000", "name": "AAPL 261016 280.00P", "qty": 2.0, "side": "LONG", "average_cost": position_cost, "price": position_cost}
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [*[row for row in provider.raw["positions"] if row["code"] != SINGLE["code"]], put]}
    provider.quotes["AAPL261016P280000"] = {"price": position_cost, "bid": position_cost - 0.1, "ask": position_cost + 0.1}
    store.sync()


def test_a_new_holding_on_the_same_key_ignores_the_old_plan_until_it_has_its_own(store, monkeypatch):
    monkeypatch.setattr(pp, "utcnow", lambda: MIDDAY - timedelta(hours=1))  # the plan was made before
    _hold_single(store)
    watch = _started(store)
    watch.check(MIDDAY)
    key = "AAPL 2026-10-16"
    assert store.plans()["plans"][key]["exposure"] == "long" and store.plans()["plans"][key]["stop"] == 292.0
    _replace_with_puts(store)
    _provider(store).quotes["AAPL"] = {"price": 285.0}  # below the calls' stop: good news for puts
    watch.check(MIDDAY + timedelta(seconds=15))
    assert not [record for record in _exits(store) if record["kind"] == "stop"]
    stored = store.repo.setting(se.EXITS_SETTING)
    assert se.outdated_plans(stored, store.plans()["plans"]) == [key]
    calls = []
    watch.monitor.plan_new_positions = lambda view, day, replace: calls.append(list(replace))
    watch.feed.clock += sw.SETTINGS_SECONDS
    watch.feed.held = store.raw()
    watch.watch.step(MIDDAY + timedelta(seconds=16))
    assert calls == [[key]]
    # The new plan measures from the price now, for the puts.
    view = store.view(now=MIDDAY, quotes={code: {**quote, "updated_at": MIDDAY.isoformat()} for code, quote in _provider(store).quotes.items()},
                      require_fresh=True)
    del watch.monitor.plan_new_positions
    monkeypatch.setattr(pp, "utcnow", lambda: MIDDAY + timedelta(seconds=17))
    watch.monitor._levels = {**LEVELS}
    watch.monitor._plan_new_positions(view, TODAY, [key])
    plan = store.plans()["plans"][key]
    assert plan["exposure"] == "short" and plan["trail_mark"] == 285.0 and plan["stop"] == 293.0
    assert se.outdated_plans(stored, store.plans()["plans"]) == []
    assert store.plans()["plans"]["NVDA"]["stop"] == 190.0  # the others are untouched


def test_reopening_after_a_review_said_close_does_not_fire_that_review(store, monkeypatch):
    monkeypatch.setattr(pp, "utcnow", lambda: MIDDAY - timedelta(hours=1))
    _hold_single(store)
    watch = _started(store)
    watch.check(MIDDAY)
    _drop(store, SINGLE["code"])
    watch.check(MIDDAY + timedelta(seconds=15))
    store.mutate_plans(lambda stored: {**stored, "plans": {key: {**plan, **({"action": "close", "reviewed_at": (MIDDAY - timedelta(days=1)).isoformat()}
                                                                             if key == "AAPL 2026-10-16" else {})}
                                                           for key, plan in stored["plans"].items()}})
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [*provider.raw["positions"], SINGLE]}
    store.sync()
    watch.check(MIDDAY + timedelta(seconds=30))
    assert not [record for record in _exits(store) if record["kind"] == "review"]


def test_a_position_held_since_before_watching_keeps_its_plan_and_review(store):
    _hold_single(store)
    store.mutate_plans(lambda stored: {**stored, "plans": {key: {**plan, **({"action": "close", "reviewed_at": (MIDDAY - timedelta(days=1)).isoformat(), "reason": "weak"}
                                                                             if key == "AAPL 2026-10-16" else {})}
                                                           for key, plan in stored["plans"].items()}})
    watch = Harness(store)
    watch.check(MIDDAY)  # the first check: it was already held, so its plan is not "older" than anything
    assert store.repo.setting(se.EXITS_SETTING)["holdings"]["AAPL 2026-10-16"]["opened"] is False
    assert se.outdated_plans(store.repo.setting(se.EXITS_SETTING), store.plans()["plans"]) == []
    assert [record["ended"] for record in _exits(store) if record["kind"] == "review"] == ["carried"]


def test_adding_a_contract_is_not_closing_and_does_not_fire_again(store):
    _hold_single(store)
    watch = _started(store)
    quotes = _provider(store).quotes
    quotes[CALL] = {"price": 4.0, "bid": 4.0, "ask": 4.1}
    watch.check(MIDDAY)
    first = [record for record in _exits(store) if record["key"] == "AAPL 2026-10-16"]
    assert [record["kind"] for record in first] == ["rule_loss"] and first[0]["exit"]
    since = first[0]["held_since"]
    added = {"code": "US.AAPL261016C305000", "name": "AAPL 261016 305.00C", "qty": 2.0, "side": "LONG", "average_cost": 8.0, "price": 8.0}
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [*provider.raw["positions"], added]}
    quotes["AAPL261016C305000"] = {"price": 3.0, "bid": 2.9, "ask": 3.1}
    store.sync()
    watch.check(MIDDAY + timedelta(seconds=15))
    records = [record for record in _exits(store) if record["key"] == "AAPL 2026-10-16"]
    assert len(records) == 1 and records[0]["closed"] is None  # the first contract is still held
    holding = store.repo.setting(se.EXITS_SETTING)["holdings"]["AAPL 2026-10-16"]
    assert holding["since"] == since and holding["codes"] == [CALL, "AAPL261016C305000"]
    # Selling the first contract closes that record, and the holding goes on.
    provider.raw = {**provider.raw, "positions": [row for row in provider.raw["positions"] if row["code"] != SINGLE["code"]]}
    store.sync()
    watch.check(MIDDAY + timedelta(seconds=30))
    records = [record for record in _exits(store) if record["key"] == "AAPL 2026-10-16"]
    assert records[0]["closed"] and store.repo.setting(se.EXITS_SETTING)["holdings"]["AAPL 2026-10-16"]["since"] == since


def test_a_long_position_turned_short_on_the_same_contract_is_closed():
    record = {"key": "A", "legs": [{"code": CALL, "ratio": 1.0}], "day": "2026-09-25", "expiry": None, "marks": [], "cost": 10.0,
              "at": MIDDAY.isoformat(), "exit": {"pnl_pct": 1.0}, "closed": None, "ticker": "AAPL"}
    assert se.follow(record, {}, {CALL: -1}, MIDDAY, MIDDAY.isoformat(), False) is True
    assert record["closed"]


def test_a_carried_first_trigger_means_the_holding_is_not_graded_on_later_ones():
    def item(kind, at, ended):
        return {"id": f"NVDA:{kind}", "key": "NVDA", "kind": kind, "at": at, "day": at[:10], "ticker": "NVDA", "type": "stock",
                "cost": 100.0, "exit": {"pnl_pct": 5.0} if ended == "done" else None, "ended": ended, "closed": None, "legs": [],
                "held_since": "2026-10-01T10:00", "marks": [{"n": 10, "pnl_pct": 0.0, "day": at[:10]}] if ended == "done" else []}
    records = [item("review", "2026-10-01T11:00", "carried"), item("stop", "2026-10-01T12:00", "done")]
    result = se.summary({"exits": records}, {})
    assert result["rows"] == [] and result["recorded"] == 1 and result["carried"] == 1
    # Within one kind, the stop stands on its own.
    assert {row["label"]: row["positions"] for row in result["by_trigger"]} == {"Stop": 1}


def test_each_holding_counts_once_until_it_is_reopened():
    def item(kind, at, key="NVDA", held="2026-10-01T10:00"):
        return {"id": f"{key}:{kind}:{at}", "key": key, "kind": kind, "at": at, "day": at[:10], "ticker": key, "type": "stock",
                "cost": 100.0, "exit": {"pnl_pct": 5.0}, "ended": "done", "closed": None, "legs": [], "held_since": held,
                "marks": [{"n": 10, "pnl_pct": 0.0, "day": at[:10]}]}
    records = [item("stop", "2026-10-01T15:00"), item("trend", "2026-10-02T15:00"),
               item("stop", "2026-10-03T15:00", key="MU"), item("review", "2026-10-04T15:00", key="MU")]
    first = se.summary({"exits": records}, {})
    assert first["rows"][0]["positions"] == 2
    assert {row["label"]: row["positions"] for row in first["by_trigger"]} == {"Stop": 2, "20-day low": 1, "Review action": 1}
    records[1]["held_since"] = "2026-10-02T10:00"  # closed, and bought back before the 20-day low
    assert se.summary({"exits": records}, {})["rows"][0]["positions"] == 3


def test_a_level_change_joins_the_history_and_an_unchanged_check_writes_nothing(store):
    _with_plans(store)
    watch = _started(store)
    watch.check(MIDDAY)
    first = store.repo.setting(se.LEVELS_SETTING)["changes"]
    assert {item["key"] for item in first} == {"NVDA", "SOXS", "USO 2026-10-16"}
    nvda = next(item for item in first if item["key"] == "NVDA")
    assert nvda["stop"] == 190.0 and nvda["stop_from"] == "2 ATR rule"
    writes = []
    original = store.repo.set_setting
    store.repo.set_setting = lambda key, value: (writes.append(key), original(key, value))[1]
    watch.check(MIDDAY + timedelta(seconds=1))
    assert se.LEVELS_SETTING not in writes and se.EXITS_SETTING not in writes
    store.add_rule({"position_key": "NVDA", "kind": "price_below", "value": 180})
    watch.check(MIDDAY + timedelta(seconds=2))
    assert writes.count(se.LEVELS_SETTING) == 1
    last = store.repo.setting(se.LEVELS_SETTING)["changes"][-1]
    assert last["key"] == "NVDA" and last["stop"] == 180 and last["stop_from"] == "your alert"
    assert se.summary({}, store.repo.setting(se.LEVELS_SETTING))["level_changes"] == 4


def test_a_locked_database_keeps_results_in_memory_and_saves_them_later(store):
    _with_plans(store)
    watch = _started(store)
    original = store.repo.set_setting

    def locked(*args):
        raise RuntimeError("database is locked")
    store.repo.set_setting = locked
    _provider(store).quotes["NVDA"] = {"price": 189.0, "bid": 188.9, "ask": 189.1}
    watch.feed.now, watch.feed.held = MIDDAY, store.raw()
    watch.watch._watched_at = MIDDAY - timedelta(seconds=1)
    watch.watch.check(MIDDAY)
    with pytest.raises(RuntimeError):
        watch.watch.save(force=True)
    store.repo.set_setting = original
    watch.watch.save(force=True)
    assert _one(store, "stop")["exit"]["fill"] == 188.9


def test_results_are_saved_at_most_every_couple_of_seconds_and_rarely_while_marks_move(store):
    _with_plans(store)
    watch = _started(store)
    writes = []
    original = store.repo.set_setting
    store.repo.set_setting = lambda key, value: (writes.append(key), original(key, value))[1]
    watch.watch._exits_dirty = True
    watch.feed.clock = 100.0
    watch.watch.save()
    watch.watch._exits_dirty = True
    watch.feed.clock = 101.0
    watch.watch.save()
    assert writes == [se.EXITS_SETTING]
    watch.feed.clock = 102.5
    watch.watch.save()
    assert len(writes) == 2
    watch.watch._exits_dirty, watch.watch._marking = True, True
    watch.feed.clock = 110.0
    watch.watch.save()
    assert len(writes) == 2
    watch.feed.clock = 133.0
    watch.watch.save()
    assert len(writes) == 3


def test_a_burst_of_partial_fills_stays_under_the_brokers_limit(store):
    watch = _started(store)
    watch.feed.now, watch.feed.held = MIDDAY, store.raw()
    watch.watch.step(MIDDAY)
    watch.feed.clock += 16
    watch.feed.opened[1]()
    watch.watch.step(MIDDAY)
    reads = len(watch.feed.reads)
    for _ in range(50):  # within the same second
        watch.feed.opened[1]()
        watch.watch.step(MIDDAY)
    assert len(watch.feed.reads) == reads
    watch.feed.clock += sw.FAST_READ_SECONDS
    watch.watch.step(MIDDAY)
    assert len(watch.feed.reads) == reads + 1


def test_the_first_check_has_the_plans_and_a_failed_read_cannot_overwrite_saved_history(store):
    _with_plans(store)
    monitor, _ = _monitor(store)
    feed = FakeFeed(store)
    watch = sw.ShadowWatch(store, monitor, feed_factory=lambda: feed)
    store.repo.set_setting(se.LEVELS_SETTING, {"changes": [{"at": "before", "key": "OLD"}], "latest": {}})
    original = store.repo.setting

    def fail_on_levels(key, default=None):
        if key == se.LEVELS_SETTING:
            raise RuntimeError("database is locked")
        return original(key, default)
    store.repo.setting = fail_on_levels
    with pytest.raises(RuntimeError):
        watch._open()
    store.repo.setting = original
    assert watch._exits is None and watch._feed is None
    watch._open()
    assert watch._plans and watch._feed is feed  # before any check can run
    assert watch._levels["changes"][0]["key"] == "OLD"


def test_todays_delays_carry_over_when_the_connections_start_over(store):
    watch = _started(store)
    first = watch.feed
    watch.watch._shut()
    second = FakeFeed(store)
    watch.watch._feed_factory = lambda: second
    watch.watch._open()
    assert second.adopted is first


def test_a_calendar_error_is_asked_again_and_a_real_answer_is_kept(store, monkeypatch):
    import src.core.trading_calendar as calendar
    monitor, _ = _monitor(store)
    clock = [0.0]
    answers = [(None, None), (MIDDAY - timedelta(hours=3), MIDDAY + timedelta(hours=3))]
    asked = []
    monkeypatch.setattr(calendar, "get_market_session_bounds", lambda market, now: (asked.append(1), answers[min(len(asked) - 1, 1)])[1])
    watch = sw.ShadowWatch(store, monitor, feed_factory=lambda: None, clock=lambda: clock[0])
    assert watch._session(MIDDAY) == (None, None)
    assert watch._session(MIDDAY) == (None, None) and len(asked) == 1
    clock[0] = sw.BOUNDS_RETRY_SECONDS
    assert watch._session(MIDDAY)[0] == MIDDAY - timedelta(hours=3)
    clock[0] = sw.BOUNDS_RETRY_SECONDS * 2
    watch._session(MIDDAY)
    assert len(asked) == 2


def test_positions_are_read_every_15_seconds_and_soon_after_a_fill(store):
    watch = _started(store)
    watch.feed.now, watch.feed.held = MIDDAY, store.raw()
    watch.watch.step(MIDDAY)
    assert watch.feed.reads == [False]
    watch.feed.clock += 14
    watch.watch.step(MIDDAY)
    assert len(watch.feed.reads) == 1
    watch.feed.clock += 2
    watch.watch.step(MIDDAY)
    assert watch.feed.reads == [False, False]
    watch.feed.clock += sw.FAST_READ_SECONDS
    watch.feed.opened[1]()  # a fill is heard: ask the broker now, and again until the list shows it
    watch.watch.step(MIDDAY)
    assert watch.feed.reads == [False, False, True]
    watch.feed.clock += 4
    watch.watch.step(MIDDAY)
    assert watch.feed.reads == [False, False, True, True]
    watch.feed.clock += 16
    watch.watch.step(MIDDAY)
    assert watch.feed.reads == [False, False, True, True, False]


def test_a_position_list_that_needs_a_second_look_is_read_again_soon(store):
    watch = _started(store)
    watch.feed.now = MIDDAY
    watch.feed.read_positions = lambda now, refresh=False: (watch.feed.reads.append(refresh), False)[1]
    watch.watch.step(MIDDAY)
    assert watch.watch._next_positions == watch.feed.clock + sw.FAST_READ_SECONDS


def test_the_feed_watches_books_on_what_you_hold_and_only_prices_on_underlyings(store):
    _hold_single(store)
    watch = _started(store)
    watch.feed.now, watch.feed.held = MIDDAY, store.raw()
    watch.watch.step(MIDDAY)
    quote_codes, book_codes = watch.feed.watched
    assert CALL in book_codes and "NVDA" in book_codes and "AAPL" not in book_codes and "AAPL" in quote_codes
    # A contract you closed stays watched while its record is being followed.
    _provider(store).quotes["AAPL"] = {"price": 289.0}
    watch.check(MIDDAY)
    _drop(store, SINGLE["code"])
    watch.feed.clock += 16
    watch.feed.held = store.raw()
    watch.watch.step(MIDDAY)
    quote_codes, book_codes = watch.feed.watched
    assert CALL in book_codes and "AAPL" in quote_codes


def test_a_new_position_gets_a_plan_at_once_from_the_price_it_opened_at(store):
    monitor, _ = _monitor(store)
    _with_plans(store)
    bars = [{"date": (TODAY - timedelta(days=60 - index)).isoformat(), "open": 100.0, "high": 110.0, "low": 90.0, "close": 100.0}
            for index in range(60)]
    asked = []
    monitor._bars = lambda tickers: (asked.append(list(tickers)), {ticker: bars for ticker in tickers})[1]
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [*provider.raw["positions"], SINGLE]}
    provider.quotes.update({CALL: {"price": 10.0, "bid": 9.9, "ask": 10.1}, "AAPL": {"price": 130.0}})
    store.sync()
    before = store.plans()["plans"]["NVDA"]
    view = store.view(now=MIDDAY, quotes={code: {**quote, "updated_at": MIDDAY.isoformat()} for code, quote in provider.quotes.items()},
                      require_fresh=True)
    monitor._plan_new_positions(view, TODAY)
    plans = store.plans()["plans"]
    plan = plans["AAPL 2026-10-16"]
    assert asked == [["AAPL"]] and plan["stop_source"] == "rule"
    assert plan["trail_mark"] == 130.0 and plan["stop"] < 130.0 < plan["target"]  # last close 100 would have crossed the target
    assert "the price when it opened" in plan["stop_basis"]
    assert plans["NVDA"] == before  # the others are untouched


def test_a_new_position_without_bars_waits_for_them_instead_of_getting_an_empty_plan(store):
    clock = [0.0]
    monitor, _ = _monitor(store, clock=lambda: clock[0])
    asked = []
    monitor._bars = lambda tickers: (asked.append(list(tickers)), {})[1]
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [SINGLE]}
    provider.quotes.update({CALL: {"price": 10.0, "bid": 9.9, "ask": 10.1}, "AAPL": {"price": 300.0}})
    store.sync()
    view = store.view(now=MIDDAY, quotes={code: {**quote, "updated_at": MIDDAY.isoformat()} for code, quote in provider.quotes.items()},
                      require_fresh=True)
    monitor._plan_new_positions(view, TODAY)
    monitor._plan_new_positions(view, TODAY)
    assert asked == [["AAPL"]] and store.plans()["plans"] == {}  # asked once, and retried later
    clock[0] = h.LEVELS_RETRY_SECONDS + 1
    monitor._plan_new_positions(view, TODAY)
    assert asked == [["AAPL"], ["AAPL"]]


def test_the_watcher_asks_for_plans_only_when_a_position_has_none(store):
    _with_plans(store)
    watch = _started(store)
    calls = []
    watch.monitor.plan_new_positions = lambda view, day, replace: calls.append(day)
    watch.feed.now, watch.feed.held = MIDDAY, store.raw()
    watch.watch.step(MIDDAY)
    assert calls == []
    _hold_single_only = dict(SINGLE)
    provider = _provider(store)
    provider.raw = {**provider.raw, "positions": [*provider.raw["positions"], _hold_single_only]}
    provider.quotes.update({CALL: {"price": 10.0, "bid": 9.9, "ask": 10.1}, "AAPL": {"price": 300.0}})
    store.sync()
    watch.feed.clock += sw.SETTINGS_SECONDS
    watch.feed.held = store.raw()
    watch.watch.step(MIDDAY)
    assert calls == [TODAY]


def test_silent_pushes_start_the_connections_over(store):
    watch = _started(store)
    feed = watch.feed
    feed.now, feed.held = MIDDAY, store.raw()
    feed.pushing_now = False
    watch.watch.step(MIDDAY)
    assert feed.closed == 0 and watch.watch._feed is feed
    watch.watch.step(MIDDAY + timedelta(seconds=sw.REOPEN_SECONDS))
    assert feed.closed == 1 and watch.watch._feed is None
    watch.watch._open()
    assert watch.watch._feed is feed and watch.watch._next_positions == 0.0


def test_the_days_delays_are_kept_for_a_month(store):
    watch = _started(store)
    days = {f"2026-08-{day:02d}": {"pushes": {"quote": 1}} for day in range(1, 31)}
    store.repo.set_setting(se.FEED_SETTING, days)
    watch.watch._save_feed_day(watch.feed, MIDDAY)
    kept = store.repo.setting(se.FEED_SETTING)
    assert len(kept) == sw.FEED_DAYS and "2026-08-01" not in kept
    assert kept["2026-09-25"]["stock_quote_s"]["median"] == 0.5 and kept["2026-09-25"]["pushes"]["book"] == 7
    result = store.shadow()
    assert result["feed_last_day"]["day"] == "2026-09-25"


def test_status_and_card_data_say_what_the_feed_is_doing(store):
    watch = _started(store)
    feed, shadow = watch.feed, watch.watch
    feed.now = MIDDAY
    assert shadow.status(MIDDAY)["state"] == "live" and shadow.health(MIDDAY)["state"] == "ok"
    feed.pushing_now = False
    assert shadow.status(MIDDAY)["state"] == "snapshot" and shadow.health(MIDDAY)["state"] == "warn"
    feed.alive_now = False
    shadow._watched_at = MIDDAY - timedelta(seconds=30)
    assert shadow.status(MIDDAY)["state"] == "down" and shadow.health(MIDDAY)["state"] == "warn"
    shadow._watched_at = MIDDAY - timedelta(minutes=5)
    assert shadow.health(MIDDAY) == {"state": "error", "detail": "no live quotes for 5 min"}
    evening = MIDDAY.replace(hour=23)
    assert shadow.status(evening)["state"] == "closed" and shadow.health(evening)["state"] == "off"
    assert store.shadow()["feed"]["state"] in {"live", "snapshot", "down", "closed"}


def test_the_status_page_lists_the_feed():
    from src.services.trade_desk.status import build
    item = {"state": "ok", "detail": "live"}
    worker = SimpleNamespace(status=lambda: {"running": True, "leader": True, "last_tick": None, "error": None},
                             part_status={}, _holdings=None, _shadow_watch=SimpleNamespace(health=lambda now: item))
    service = SimpleNamespace(worker=worker, enabled=True, version=None, started_at=None, health=lambda: {"live": {"available": True}},
                              repo=SimpleNamespace(preferences=lambda: {}, events=lambda **kwargs: []))
    found = {component["key"]: component for component in build(service)["components"]}
    assert found["shadow_feed"]["state"] == "ok" and found["shadow_feed"]["label"] == "Shadow exits live feed"


# -- the feed itself ------------------------------------------------------------------------------
def _stamp(when):
    return when.astimezone(lf._NEW_YORK).strftime("%Y-%m-%d %H:%M:%S")


def _quote_row(code, when, price=100.0, volume=10.0):
    day, clock = _stamp(when).split()
    return {"code": f"US.{code}", "last_price": price, "prev_close_price": 99.0, "volume": volume, "data_date": day, "data_time": clock}


def _book_row(code, when, bid=9.9, ask=10.1):
    stamp = when.astimezone(lf._NEW_YORK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return {"code": f"US.{code}", "svr_recv_time_bid": stamp, "svr_recv_time_ask": stamp,
            "Bid": [(bid, 5, 1, {})] if bid is not None else [], "Ask": [(ask, 5, 1, {})] if ask is not None else []}


class FakeLiveProvider:
    def __init__(self, positions=None):
        self.subscribed, self.refuse, self.snapshot = [], set(), {}
        self.rows = positions if positions is not None else []
        self.reads = []
        self.closed = False

    def listen_quotes(self, on_quote, on_book):
        self.on_quote, self.on_book = on_quote, on_book

    def listen_account(self, account, firm, on_deal):
        self.on_deal = on_deal
        provider = self

        class Account:
            def positions(self, refresh=False):
                provider.reads.append(refresh)
                return [dict(row) for row in provider.rows]

            def close(self):
                pass
        return Account()

    def subscribe_push(self, codes, subtype):
        if any(code in self.refuse for code in codes):
            raise ProviderError("provider_error", "not subscribable")
        self.subscribed.append((list(codes), subtype))

    def watchlist_quotes(self, codes):
        return {code: self.snapshot[code] for code in codes if code in self.snapshot}

    def close(self):
        self.closed = True


def _feed(monkeypatch, positions=None):
    provider = FakeLiveProvider(positions)
    feed = lf.LiveFeed("1234", "FUTUINC", provider=provider)
    woke = []
    feed.open(lambda: woke.append("quote"), lambda: woke.append("fill"))
    monkeypatch.setattr(lf, "utcnow", lambda: MIDDAY)
    return feed, provider, woke


def test_pushes_build_one_quote_and_wake_the_check(monkeypatch):
    feed, provider, woke = _feed(monkeypatch)
    provider.on_quote(_quote_row("AAPL261016C300000", MIDDAY - timedelta(seconds=1), price=10.0, volume=1.0))
    provider.on_book(_book_row("AAPL261016C300000", MIDDAY - timedelta(milliseconds=50)))
    quote = feed.quotes(MIDDAY)["AAPL261016C300000"]
    assert quote["price"] == 10.0 and quote["bid"] == 9.9 and quote["ask"] == 10.1 and quote["source"] == "push"
    assert quote["option"] is True and quote["session"] == "regular" and quote["fetched_at"] == MIDDAY.isoformat()
    assert woke == ["quote", "quote"]
    from src.services.trade_desk.quality import watch_quote_fresh
    assert watch_quote_fresh(quote, MIDDAY, option=True)


def test_delays_are_measured_on_real_changes_only(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    provider.on_quote(_quote_row("NVDA", MIDDAY - timedelta(hours=3), volume=10.0))  # the first push is only the last known state
    provider.on_quote(_quote_row("NVDA", MIDDAY - timedelta(hours=3), volume=10.0))  # no new trade
    assert feed.status(MIDDAY)["stock_quote_s"] is None
    provider.on_quote(_quote_row("NVDA", MIDDAY - timedelta(seconds=2), volume=11.0))
    provider.on_quote(_quote_row("AAPL261016C300000", MIDDAY - timedelta(seconds=5), volume=1.0))
    provider.on_quote(_quote_row("AAPL261016C300000", MIDDAY - timedelta(seconds=3), volume=2.0))
    provider.on_book(_book_row("NVDA", MIDDAY - timedelta(milliseconds=500)))
    provider.on_book(_book_row("NVDA", MIDDAY - timedelta(milliseconds=200)))
    state = feed.status(MIDDAY)
    assert state["stock_quote_s"] == {"median": 2.0, "p95": 2.0, "n": 1}
    assert state["option_quote_s"] == {"median": 3.0, "p95": 3.0, "n": 1}
    assert state["book_s"] == {"median": 0.2, "p95": 0.2, "n": 1}
    assert state["pushes"] == {"quote": 5, "book": 2, "fill": 0}


def test_a_book_side_can_be_empty_and_a_bad_timestamp_is_harmless(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    provider.on_book({**_book_row("AAPL261016C300000", MIDDAY, bid=None, ask=0.08), "svr_recv_time_bid": "", "svr_recv_time_ask": ""})
    quote = feed.quotes(MIDDAY)["AAPL261016C300000"]
    assert quote["bid"] == 0.0 and quote["ask"] == 0.08 and quote["book_at"] is None
    assert feed.error is None


def test_a_broken_push_does_not_stop_the_sdks_thread_and_shows_in_status(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    provider.on_book({"code": "US.NVDA", "Bid": "not a book"})
    assert feed.status(MIDDAY)["handler_error"] == "ValueError"


def test_a_quote_nothing_has_refreshed_is_not_used(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    provider.on_quote(_quote_row("NVDA", MIDDAY))
    assert "NVDA" in feed.quotes(MIDDAY + lf.MAX_QUOTE_AGE)
    assert "NVDA" not in feed.quotes(MIDDAY + lf.MAX_QUOTE_AGE + timedelta(seconds=1))


def test_yesterdays_prices_and_books_are_not_used_after_the_open(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    opening = MIDDAY - timedelta(minutes=1)
    code = "AAPL261016C300000"
    provider.on_quote(_quote_row("NVDA", MIDDAY - timedelta(hours=20)))  # no trade yet today
    monkeypatch.setattr(lf, "utcnow", lambda: opening - timedelta(minutes=2))
    provider.on_book(_book_row(code, opening - timedelta(minutes=2)))  # a book from before the open
    monkeypatch.setattr(lf, "utcnow", lambda: MIDDAY)
    assert feed.quotes(MIDDAY, opening) == {}
    provider.on_quote(_quote_row("NVDA", MIDDAY))
    assert set(feed.quotes(MIDDAY, opening)) == {"NVDA"}
    # The same book again, even from a snapshot after the open, is not news.
    provider.snapshot = {code: {"price": 10.0, "bid": 9.9, "ask": 10.1, "updated_at": _stamp(opening - timedelta(hours=20))}}
    monkeypatch.setattr(lf, "utcnow", lambda: MIDDAY + timedelta(seconds=10))
    feed.watch([code], [code])
    feed.refresh()
    assert code not in feed.quotes(MIDDAY + timedelta(seconds=10), opening)
    # A book that moved is today's.
    provider.on_book(_book_row(code, MIDDAY, bid=9.8))
    assert code in feed.quotes(MIDDAY + timedelta(seconds=10), opening)


def test_a_first_book_after_the_open_waits_too_and_two_minutes_settle_it(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    opening = MIDDAY - timedelta(seconds=30)
    code = "AAPL261016C300000"
    provider.on_book(_book_row(code, MIDDAY))
    assert code not in feed.quotes(MIDDAY, opening)
    settled = opening + lf.OPEN_BOOK_GRACE
    monkeypatch.setattr(lf, "utcnow", lambda: settled)
    provider.on_book(_book_row(code, settled))
    assert code in feed.quotes(settled, opening)  # a thin contract that really has not moved


def test_a_silent_spy_means_the_feed_is_frozen(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    assert not feed.alive(MIDDAY)
    provider.on_quote(_quote_row("SPY", MIDDAY))
    assert feed.alive(MIDDAY) and feed.pushing(MIDDAY)
    later = MIDDAY + lf.MAX_MARKET_SILENCE + timedelta(seconds=1)
    assert not feed.alive(later) and not feed.pushing(later)
    # The connection still answers, with the same old trade: a frozen feed does not pass.
    provider.snapshot = {"SPY": {"price": 100.0, "updated_at": _stamp(MIDDAY)}}
    monkeypatch.setattr(lf, "utcnow", lambda: later)
    feed.refresh()
    assert not feed.alive(later)
    # A snapshot with a current trade keeps checks going, marked as not pushing.
    provider.snapshot = {"SPY": {"price": 101.0, "updated_at": _stamp(later)}}
    feed.refresh()
    assert feed.alive(later) and not feed.pushing(later) and feed.quotes(later)["SPY"]["source"] == "snapshot"


def test_the_snapshot_confirms_a_thin_quote_and_never_overwrites_a_newer_push(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    code = "AAPL261016C300000"
    provider.on_book(_book_row(code, MIDDAY, bid=1.0, ask=1.2))
    feed.watch([code], [code])
    provider.snapshot = {code: {"price": 1.1, "bid": 0.9, "ask": 1.3, "updated_at": _stamp(MIDDAY - timedelta(hours=1))}}
    feed.refresh()  # the same moment: the push is newer
    assert feed.quotes(MIDDAY)[code]["bid"] == 1.0
    later = MIDDAY + timedelta(seconds=30)
    monkeypatch.setattr(lf, "utcnow", lambda: later)
    provider.snapshot = {code: {"price": 1.1, "bid": 1.0, "ask": 1.2, "updated_at": _stamp(MIDDAY - timedelta(hours=1))}}
    feed.refresh()  # no push since: nothing changed, and now we know
    quote = feed.quotes(later + lf.MAX_QUOTE_AGE)[code]
    assert quote["source"] == "snapshot" and quote["bid"] == 1.0 and quote["received"] == later


def test_subscribing_asks_once_and_a_refused_code_does_not_stop_the_others(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    assert provider.subscribed == [(["SPY"], "QUOTE")]
    provider.refuse = {"BAD"}
    feed.watch(["SPY", "NVDA", "BAD"], ["NVDA"])
    assert (["NVDA"], "QUOTE") in provider.subscribed and (["NVDA"], "ORDER_BOOK") in provider.subscribed
    count = len(provider.subscribed)
    feed.watch(["SPY", "NVDA", "BAD"], ["NVDA"])
    assert len(provider.subscribed) == count  # not asked again
    assert feed.status(MIDDAY)["refused"] == 1 and feed.status(MIDDAY)["subscribed"] == {"quote": 2, "order_book": 1}
    provider.subscribe_push = lambda codes, subtype: (_ for _ in ()).throw(ProviderError("connection_unavailable", "down"))
    with pytest.raises(ProviderError):  # a lost connection is not a refused code
        feed.watch(["MU"], [])
    assert feed.status(MIDDAY)["refused"] == 1


NVDA_ROW = {"code": "US.NVDA", "qty": 5.0, "side": "LONG", "average_cost": 120.0}


def test_a_position_that_vanishes_with_no_fill_must_be_gone_on_two_reads(monkeypatch):
    feed, provider, _ = _feed(monkeypatch, [NVDA_ROW])
    assert feed.read_positions(MIDDAY) is True
    assert provider.reads == [True]  # the first read asks the broker
    assert [row["code"] for row in feed.raw()["positions"]] == ["US.NVDA"]
    provider.rows = []  # a bad read, or a contract that expired
    assert feed.read_positions(MIDDAY + timedelta(seconds=15)) is False
    assert provider.reads == [True, False]
    assert [row["code"] for row in feed.raw()["positions"]] == ["US.NVDA"]
    provider.rows = [NVDA_ROW]  # it was a bad read
    assert feed.read_positions(MIDDAY + timedelta(seconds=19)) is True
    provider.rows = []
    assert feed.read_positions(MIDDAY + timedelta(seconds=34)) is False
    assert feed.read_positions(MIDDAY + timedelta(seconds=38)) is True
    assert provider.reads[-1] is True  # the second look asks the broker, not OpenD's cache
    assert feed.raw()["positions"] == [] and feed.raw()["synced_at"] == (MIDDAY + timedelta(seconds=38)).isoformat()


def test_a_position_that_vanishes_after_a_fill_is_gone_at_once_and_the_lag_is_measured(monkeypatch):
    feed, provider, woke = _feed(monkeypatch, [NVDA_ROW])
    feed.read_positions(MIDDAY)
    provider.on_deal({"code": "US.NVDA", "price": 185.5, "trd_side": "SELL", "status": "OK", "qty": 5.0})
    assert woke == ["fill"] and feed.fills() == {"NVDA": {"sell": {"price": 185.5, "at": MIDDAY}}}
    provider.rows = []
    assert feed.read_positions(MIDDAY + timedelta(seconds=3)) is True
    assert feed.raw()["positions"] == []
    assert feed.status(MIDDAY)["fill_to_position_s"] == {"median": 3.0, "p95": 3.0, "n": 1}


def test_the_fill_that_opened_a_position_or_an_old_trim_does_not_explain_it_vanishing(monkeypatch):
    feed, provider, _ = _feed(monkeypatch, [])
    feed.read_positions(MIDDAY)
    provider.on_deal({"code": "US.NVDA", "price": 150.0, "trd_side": "BUY", "status": "OK"})  # opens it
    provider.rows = [NVDA_ROW]
    feed.read_positions(MIDDAY + timedelta(seconds=15))
    provider.rows = []  # a bad read: the purchase is not a close
    assert feed.read_positions(MIDDAY + timedelta(seconds=30)) is False
    provider.rows = [NVDA_ROW]
    assert feed.read_positions(MIDDAY + timedelta(seconds=34)) is True
    monkeypatch.setattr(lf, "utcnow", lambda: MIDDAY + timedelta(seconds=35))
    provider.on_deal({"code": "US.NVDA", "price": 160.0, "trd_side": "SELL", "status": "OK"})  # a trim
    provider.rows = [NVDA_ROW]
    assert feed.read_positions(MIDDAY + timedelta(seconds=50)) is True
    provider.rows = []  # later, a bad read: that trim is older than the last read that showed the position
    assert feed.read_positions(MIDDAY + timedelta(seconds=65)) is False


def test_buying_back_is_what_closes_a_short_position(monkeypatch):
    short = {"code": "US.NVDA", "qty": -5.0, "side": "SHORT", "average_cost": 120.0}
    feed, provider, _ = _feed(monkeypatch, [short])
    feed.read_positions(MIDDAY)
    provider.on_deal({"code": "US.NVDA", "price": 110.0, "trd_side": "SELL_SHORT", "status": "OK"})
    provider.rows = []
    assert feed.read_positions(MIDDAY + timedelta(seconds=3)) is False
    provider.on_deal({"code": "US.NVDA", "price": 100.0, "trd_side": "BUY_BACK", "status": "OK"})
    assert feed.read_positions(MIDDAY + timedelta(seconds=7)) is True


def test_a_cancelled_fill_is_ignored_and_sides_are_kept_apart(monkeypatch):
    feed, provider, woke = _feed(monkeypatch)
    provider.on_deal({"code": "US.NVDA", "price": 100.0, "trd_side": "SELL", "status": "CANCELLED"})
    assert feed.fills() == {} and woke == []
    provider.on_deal({"code": "US.NVDA", "price": 100.0, "trd_side": "BUY", "status": "OK"})
    provider.on_deal({"code": "US.NVDA", "price": 110.0, "trd_side": "SELL_SHORT", "status": "OK"})
    assert feed.fills() == {"NVDA": {"buy": {"price": 100.0, "at": MIDDAY}, "sell": {"price": 110.0, "at": MIDDAY}}}


def test_closing_the_feed_closes_its_connections(monkeypatch):
    feed, provider, _ = _feed(monkeypatch)
    feed.close()
    assert provider.closed is True


# -- the moomoo side, on a stand-in SDK -----------------------------------------------------------
class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows

    def __len__(self):
        return len(self.rows)


def _sdk(reply):
    class Base:
        def parse_rsp_pb(self, content):
            return reply(content)

        def on_recv_rsp(self, content):
            return reply(content)

    class QuoteBase(Base):
        def on_recv_rsp(self, content):
            raise AssertionError("the SDK's table for every quote push is most of the feed's CPU")

    class BookBase(Base):
        pass

    class DealBase(Base):
        pass

    return SimpleNamespace(__name__="stand_in", RET_OK=0, logger=SimpleNamespace(file_level=logging.DEBUG),
                           StockQuoteHandlerBase=QuoteBase, OrderBookHandlerBase=BookBase,
                           TradeDealHandlerBase=DealBase, SubType=SimpleNamespace(QUOTE="QUOTE", ORDER_BOOK="ORDER_BOOK"),
                           TrdEnv=SimpleNamespace(REAL="REAL"), TrdMarket=SimpleNamespace(NONE="N/A"),
                           SecurityFirm=SimpleNamespace(FUTUINC="FUTUINC"), Currency=SimpleNamespace(USD="USD"))


class _Context:
    def __init__(self):
        self.handlers, self.timeout, self.closed, self.subscribe_calls = [], None, False, []

    def set_handler(self, handler):
        self.handlers.append(handler)

    def set_sync_query_connect_timeout(self, seconds):
        self.timeout = seconds

    def get_acc_list(self):
        return 0, _Frame([{"acc_id": 991234, "trd_env": "REAL", "acc_type": "MARGIN"}])

    def position_list_query(self, **kwargs):
        self.position_call = kwargs
        return 0, _Frame([{"code": "US.NVDA", "qty": 5.0, "stock_name": "NVIDIA", "position_side": "LONG"},
                          {"code": "US.MU", "qty": 0.0}, {"code": "HK.00700", "qty": 1.0}])

    def query_subscription(self, *args, **kwargs):
        return 0, {"sub_list": {}, "remain": 900, "own_used": 0}

    def subscribe(self, codes, subtypes, **kwargs):
        self.subscribe_calls.append((codes, subtypes, kwargs))
        return 0, ""

    def close(self):
        self.closed = True


def test_quote_push_reaches_the_callbacks_with_the_connection_bounded():
    ctx = _Context()
    rows = [{"code": "US.NVDA", "last_price": 1.0}]
    book = {"code": "US.NVDA", "Bid": [], "Ask": []}
    sdk = _sdk(lambda content: (0, rows if content == "quote" else book))
    provider = MoomooProvider(sdk=sdk, context=ctx)
    quotes, books = [], []
    provider.listen_quotes(quotes.append, books.append)
    assert ctx.timeout == providers._SYNC_QUERY_CONNECT_SECONDS
    assert sdk.logger.file_level == logging.INFO  # debug level writes two lines per push to the home folder
    quote_handler, book_handler = ctx.handlers
    assert quote_handler.on_recv_rsp("quote") == (0, rows) and quotes == rows
    assert book_handler.on_recv_rsp("book") == (0, book) and books == [book]


@pytest.mark.parametrize("module", ["moomoo", "futu"])
def test_the_real_sdk_offers_the_quote_parse_and_can_keep_pushes_out_of_its_log_file(module, tmp_path):
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not installed")
    # In a process of its own: the two SDKs register the same proto files, so only the first import in a process works.
    check = f"""
import logging, {module} as sdk
assert callable(sdk.StockQuoteHandlerBase.parse_rsp_pb) and callable(sdk.CurKlineHandlerBase.parse_rsp_pb)
sdk.logger.file_level = logging.INFO
sdk.logger.debug("a push")
sdk.logger.info("a connection")
"""
    subprocess.run([sys.executable, "-c", check], check=True, timeout=60, env={**os.environ, "HOME": str(tmp_path)})
    written = "".join(path.read_text() for path in tmp_path.glob(".*/Log/*"))
    assert "a connection" in written and "a push" not in written


def test_once_listening_a_lost_connection_is_left_to_the_sdk_which_keeps_the_handlers():
    ctx = _Context()
    ctx.get_market_snapshot = lambda codes: (-1, "Connect timeout")
    provider = MoomooProvider(sdk=_sdk(lambda content: (0, None)), context=ctx)
    swapped = []
    provider._reconnect = lambda: swapped.append(1)
    with pytest.raises(ProviderError) as caught:
        provider._call("get_market_snapshot", ["US.NVDA"])
    assert caught.value.code == "connection_unavailable" and swapped == [1]  # the one-minute loop's provider reconnects
    swapped.clear()
    provider.listen_quotes(lambda row: None, lambda row: None)
    with pytest.raises(ProviderError):
        provider._call("get_market_snapshot", ["US.NVDA"])
    assert swapped == []


def test_a_failed_push_reaches_no_callback():
    ctx = _Context()
    provider = MoomooProvider(sdk=_sdk(lambda content: (-1, "bad packet")), context=ctx)
    quotes = []
    provider.listen_quotes(quotes.append, quotes.append)
    assert ctx.handlers[0].on_recv_rsp("x") == (-1, "bad packet") and quotes == []


def test_subscribing_for_push_asks_for_push_and_the_one_minute_loop_still_does_not():
    ctx = _Context()
    provider = MoomooProvider(sdk=_sdk(lambda content: (0, None)), context=ctx)
    provider.subscribe_push(["NVDA"], "QUOTE")
    provider._subscribe(["US.MU"], "QUOTE")
    assert ctx.subscribe_calls[0][0] == ["US.NVDA"] and ctx.subscribe_calls[0][2]["subscribe_push"] is True
    assert ctx.subscribe_calls[1][2]["subscribe_push"] is False


def test_the_account_connection_hears_only_this_accounts_fills_and_reads_positions(monkeypatch):
    ctx = _Context()
    frame = _Frame([{"code": "US.NVDA", "price": 10.0, "trd_side": "SELL", "status": "OK"}])
    sdk = _sdk(lambda content: (0, frame))
    sdk.OpenSecTradeContext = lambda **kwargs: ctx
    provider = MoomooProvider(sdk=sdk, host="127.0.0.1", port=11111)
    monkeypatch.setattr(provider, "_configure_encryption", lambda sdk: None)
    monkeypatch.setattr(provider, "_require_listening", lambda: None)
    deals = []
    watch = provider.listen_account("1234", "FUTUINC", deals.append)
    assert ctx.timeout == providers._SYNC_QUERY_CONNECT_SECONDS
    handler = ctx.handlers[0]
    mine = SimpleNamespace(s2c=SimpleNamespace(header=SimpleNamespace(accID=991234)))
    other = SimpleNamespace(s2c=SimpleNamespace(header=SimpleNamespace(accID=775555)))
    handler.on_recv_rsp(other)
    assert deals == []
    handler.on_recv_rsp(mine)
    assert deals == frame.rows
    rows = watch.positions(refresh=True)
    assert [row["code"] for row in rows] == ["US.NVDA"]  # open US positions only
    assert ctx.position_call == {"trd_env": "REAL", "acc_id": 991234, "refresh_cache": True}
    watch.close()
    assert ctx.closed


def test_a_failed_position_read_raises_rather_than_reading_as_no_positions():
    ctx = _Context()
    ctx.position_list_query = lambda **kwargs: (-1, "not logged in")
    with pytest.raises(ProviderError):
        providers.AccountWatch(ctx, 1, "REAL", 0).positions()


# -- the whole path, on real threads --------------------------------------------------------------
def test_a_pushed_quote_becomes_a_record_on_the_watchers_own_threads(store, monkeypatch):
    now = lf.utcnow()
    _with_plans(store)
    monitor, events = _monitor(store)
    provider = FakeLiveProvider([{"code": "US.NVDA", "name": "NVIDIA", "qty": 5.0, "side": "LONG", "average_cost": 120.0}])
    feed = lf.LiveFeed("1234", "FUTUINC", provider=provider)
    opened = threading.Event()
    real_open = feed.open

    def open_and_say(*args):
        real_open(*args)
        opened.set()
    feed.open = open_and_say
    watch = sw.ShadowWatch(store, monitor, feed_factory=lambda: feed)
    local = now.astimezone(lf._NEW_YORK)
    # An open session around this moment, already watched: the test runs at any hour of any day.
    watch._bounds = (local.date(), now - timedelta(hours=1), now + timedelta(hours=1))
    watch._watched_at = now
    an_hour_ago = (now - timedelta(hours=1)).isoformat()
    store.repo.set_setting(se.EXITS_SETTING, {"started_at": an_hour_ago, "holdings": {  # already held and watched
        "NVDA": {"since": an_hour_ago, "codes": ["NVDA"], "crossed": [], "opened": False}}})
    watch.start()
    try:
        assert opened.wait(20)
        deadline = time.monotonic() + 20
        while feed.raw() is None or not watch._plans:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        day, clock = _stamp(lf.utcnow()).split()
        provider.on_quote({**_quote_row("SPY", lf.utcnow())})
        provider.on_book(_book_row("NVDA", lf.utcnow(), bid=188.9, ask=189.1))
        provider.on_quote(_quote_row("NVDA", lf.utcnow(), price=189.0))
        while not _exits(store):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        [record] = _exits(store)
        assert record["kind"] == "stop" and record["seen"]["source"] == "push" and record["exit"]["fill"] == 188.9
        assert watch.status()["state"] == "live" and events == []
    finally:
        watch.stop()
    assert provider.closed is True


def test_nothing_connects_without_the_lease_or_outside_the_session(store):
    made = []

    def factory():
        made.append(1)
        raise AssertionError("must not connect")
    monitor, _ = _monitor(store)
    watch = sw.ShadowWatch(store, monitor, leader=lambda: False, feed_factory=factory)
    now = lf.utcnow()
    watch._bounds = (now.astimezone(lf._NEW_YORK).date(), now - timedelta(hours=1), now + timedelta(hours=1))
    watch.start()
    time.sleep(0.2)
    watch.stop()
    closed = sw.ShadowWatch(store, monitor, feed_factory=factory)
    closed._bounds = (now.astimezone(lf._NEW_YORK).date(), None, None)  # a closed day
    closed._bounds_retry = float("inf")
    closed.start()
    time.sleep(0.2)
    closed.stop()
    assert made == [] and not closed.errors


def test_opend_being_down_is_retried_and_shows_in_status(store):
    monitor, _ = _monitor(store)

    def factory():
        raise ProviderError("connection_unavailable", "OpenD is not listening")
    watch = sw.ShadowWatch(store, monitor, feed_factory=factory)
    now = lf.utcnow()
    watch._bounds = (now.astimezone(lf._NEW_YORK).date(), now - timedelta(hours=1), now + timedelta(hours=1))
    watch.start()
    deadline = time.monotonic() + 10
    while "feed" not in watch.errors:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    watch.stop()
    state = watch.status(now)
    assert state["state"] == "down" and state["errors"]["feed"] == "ProviderError"
    assert "ProviderError" in watch.health(now)["detail"]


def test_the_minute_loop_judges_quotes_by_a_time_taken_after_they_arrive(monkeypatch):
    """A slow plan check once made the minute's quotes look like they came from the future: every alert skipped that minute."""
    from src.services.trade_desk import worker as worker_module
    times = iter(MIDDAY + timedelta(seconds=seconds) for seconds in range(0, 100, 10))
    monkeypatch.setattr(worker_module, "utcnow", lambda: next(times))
    seen = {}
    worker = worker_module.TradeDeskWorker.__new__(worker_module.TradeDeskWorker)
    worker.owner, worker._leader, worker.part_status = "me", True, {}
    worker.repo = SimpleNamespace(lease=lambda owner: True)
    worker.service = SimpleNamespace()
    worker._check_plans = lambda now, regular: True
    worker._deliver_in_background = lambda: None
    worker._pulse = worker._tracker = worker._youtube = worker._social = worker._breakouts = worker._opportunities = None
    stamps = []

    def quotes(session):
        stamps.append(worker_module.utcnow())
        return {"NVDA": {"fetched_at": stamps[-1].isoformat()}}
    worker._shared_quotes = quotes
    worker._holdings = SimpleNamespace(tick=lambda now, session, quotes, shared: seen.update(now=now))
    worker.run_once()
    assert seen["now"] > stamps[0]


def test_expired_contracts_are_not_requested_from_the_broker():
    exits = {"exits": [{"legs": [{"code": CALL, "ratio": 1}], "ticker": "AAPL", "expiry": "2026-10-16"},
                       {"legs": [{"code": "NVDA", "ratio": 1}], "ticker": "NVDA", "expiry": None},
                       {"legs": [{"code": "MU", "ratio": 1}], "ticker": "MU", "expiry": None, "ended": "done"}]}
    assert se.codes(exits, date(2026, 10, 16)) == [CALL, "AAPL", "NVDA"]
    assert se.codes(exits, date(2026, 10, 16), legs_only=True) == [CALL, "NVDA"]
    assert se.codes(exits, date(2026, 10, 19)) == ["NVDA"]


def test_the_verdict_stays_too_early_until_enough_positions_over_two_months():
    def item(index, month, gap):
        day = f"2026-{month:02d}-{1 + index % 27:02d}"
        return {"key": f"T{index}", "ticker": f"T{index}", "kind": "stop", "at": f"{day}T15:00", "day": day, "type": "stock",
                "cost": 100.0, "exit": {"pnl_pct": gap}, "ended": "done", "closed": None, "legs": [],
                "marks": [{"n": 10, "pnl_pct": 0.0, "day": day}]}
    few = [item(index, 10, 5.0) for index in range(29)]
    assert se.summary({"exits": few}, {})["verdict"] == "too_early"
    one_month = [item(index, 10, 5.0) for index in range(40)]
    assert se.summary({"exits": one_month}, {})["verdict"] == "too_early"
    # Selling beat holding each month, by a steady amount, over four months.
    steady = [item(index, 7 + index % 4, 5.0 + index % 4 * 0.1) for index in range(40)]
    result = se.summary({"exits": steady}, {})
    assert result["verdict"] == "selling_better" and result["months"] == 4
    mixed = [item(index, 7 + index % 4, (1 if index % 4 < 2 else -1) * 5.0) for index in range(40)]
    assert se.summary({"exits": mixed}, {})["verdict"] == "no_clear_difference"


def test_empty_results_say_so_and_the_note_says_nothing_is_sent(store):
    result = store.shadow()
    assert result["verdict"] == "too_early" and result["recorded"] == 0 and result["rows"] == [] and result["since"] is None
    assert "nothing is sent" in result["note"]


def test_the_route_is_off_without_a_broker_account_and_reads_only_with_one(store, monkeypatch):
    from api.v1.endpoints import trade_desk as route
    monkeypatch.setattr(route, "get_service", lambda request: SimpleNamespace(holdings=store))
    assert asyncio.run(route.shadow_exits(None))["enabled"] is True
    monkeypatch.delenv("TRADE_DESK_BROKER_ACCOUNT")
    assert asyncio.run(route.shadow_exits(None)) == {"enabled": False}
