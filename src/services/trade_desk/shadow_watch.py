"""Shadow exits on the live feed: every quote is checked as it arrives, apart from the one-minute loop.

Two threads, so that neither a slow database nor OpenD can hold up a check:

- **check** runs on each pushed quote, at most ``MIN_PASS_SECONDS`` apart, on what is in memory. It
  never touches the network or the database. A slow alert, a locked database or a failed Discord
  send in the one-minute loop cannot delay it.
- **io** reads the position list (every ``POSITION_SECONDS``, at once after a fill is heard, and a
  few more times until the list shows it), plans and alerts, the snapshot backstop, and saves results.

It connects from a few minutes before the open to just after the close on trading days, only on the
server that holds the monitor lease. Nothing here can trade (see ``live_feed.py``).

A level found crossed after more than ``GAP_SECONDS`` of not watching (a restart, a lost feed) has no
trigger moment, so it is counted as carried and not graded. Levels are still set once a day by the
review, or when a position appears: this watches in real time, it does not decide in real time.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import position_plans, shadow_exits
from .holdings import _local, _prices, account, build_view, parse_code
from .live_feed import LiveFeed
from .models import utcnow

logger = logging.getLogger(__name__)

POSITION_SECONDS = 15
FAST_READ_SECONDS = 4  # after a fill: asking the broker counts against 10 calls per 30 s
FAST_READ_WINDOW = 12
SETTINGS_SECONDS = 10
SNAPSHOT_SECONDS = 30
MIN_PASS_SECONDS = 0.1
GAP_SECONDS = 60
OPEN_EARLY = timedelta(minutes=5)
CLOSE_LATE = timedelta(minutes=1)
SAVE_SECONDS = 2
SAVE_SECONDS_MARKING = 30  # the marks change on every check in the last minutes
FEED_SAVE_SECONDS = 60
RETRY_SECONDS = 15
BOUNDS_RETRY_SECONDS = 60
REOPEN_SECONDS = 120  # no push from SPY this long in the session: start the connections over
DOWN_IS_ERROR_SECONDS = 120
FEED_DAYS = 30
PASS_SAMPLES = 2000


def _live_feed() -> LiveFeed:
    return LiveFeed(account(), os.getenv("TRADE_DESK_BROKER_SECURITY_FIRM", "FUTUINC") or "FUTUINC")


class ShadowWatch:
    def __init__(self, holdings: Any, monitor: Any, *, leader: Optional[Callable[[], bool]] = None,
                 feed_factory: Callable[[], Any] = _live_feed, clock: Callable[[], float] = time.monotonic):
        self.holdings, self.monitor = holdings, monitor
        self._leader = leader or (lambda: True)
        self._feed_factory = feed_factory
        self._clock = clock
        self._stop = threading.Event()
        self._quote_arrived = threading.Event()
        self._io_wake = threading.Event()
        self._state = threading.RLock()
        self._threads: List[threading.Thread] = []
        self._feed: Any = None
        self._exits: Optional[Dict[str, Any]] = None
        self._levels: Dict[str, Any] = {}
        self._plans: Dict[str, Dict[str, Any]] = {}
        self._rules: List[Dict[str, Any]] = []
        self._bounds: Tuple[Optional[date], Any, Any] = (None, None, None)
        self._bounds_retry = 0.0
        self._watched_at: Optional[datetime] = None
        self._seen: Optional[set] = None  # what has been judged since watching last lapsed; None if it has not
        self._lows_day: Optional[date] = None
        self._last_feed: Tuple[Optional[date], Any] = (None, None)
        self._last_read = -FAST_READ_SECONDS
        self._silent_since: Optional[datetime] = None
        self._exits_dirty = self._levels_dirty = False
        self._marking = False
        self._next_positions = self._next_settings = self._next_snapshot = 0.0
        self._fast_until = 0.0
        self._saved_at = self._next_feed_save = 0.0
        self._pass_ms: deque = deque(maxlen=PASS_SAMPLES)
        self.passes = 0
        self.errors: Dict[str, str] = {}
        holdings.shadow_feed = self.status

    # -- lifecycle -------------------------------------------------------------------------------
    def start(self) -> None:
        self._threads = [threading.Thread(target=self._io_loop, name="shadow-io", daemon=True),
                         threading.Thread(target=self._check_loop, name="shadow-check", daemon=True)]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._quote_arrived.set()
        self._io_wake.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def _session(self, now: datetime) -> Tuple[Any, Any]:
        """Today's regular open and close; (None, None) on a closed day. Cached: checks run many times a second."""
        day = _local(now).date()
        clock = self._clock()
        # None is a closed day or a calendar error, which cannot be told apart: asked again now and then.
        if self._bounds[0] != day or (self._bounds[1] is None and clock >= self._bounds_retry):
            from src.core.trading_calendar import get_market_session_bounds
            opening, closing = get_market_session_bounds("us", now)
            self._bounds = (day, opening, closing)
            self._bounds_retry = clock + BOUNDS_RETRY_SECONDS
        return self._bounds[1], self._bounds[2]

    def _in_window(self, now: datetime) -> bool:
        opening, closing = self._session(now)
        return opening is not None and opening - OPEN_EARLY <= now < closing + CLOSE_LATE

    def _in_session(self, now: datetime) -> bool:
        opening, closing = self._session(now)
        return opening is not None and opening <= now < closing

    def _open(self) -> None:
        if self._feed is not None:
            return
        if self._exits is None:
            exits = self.holdings.repo.setting(shadow_exits.EXITS_SETTING, {}) or {}
            levels = self.holdings.repo.setting(shadow_exits.LEVELS_SETTING, {}) or {}
            with self._state:  # both or neither: a saved history must not be overwritten by a failed read
                self._exits, self._levels = exits, levels
        self._load_settings()  # before the first check, or it would record every level as gone
        feed = self._feed_factory()
        day = _local(utcnow()).date()
        if self._last_feed[0] == day:
            feed.adopt(self._last_feed[1])
        self._last_feed = (day, feed)
        feed.open(self._quote_arrived.set, self._heard_fill)
        self._next_positions = self._next_settings = self._next_snapshot = 0.0
        self._silent_since = None
        self._feed = feed

    def _shut(self) -> None:
        feed, self._feed = self._feed, None
        try:
            if feed is not None:
                self._save_feed_day(feed, utcnow())
        finally:
            if feed is not None:
                feed.close()
        self.save(force=True)

    def _heard_fill(self) -> None:
        self._fast_until = self._clock() + FAST_READ_WINDOW
        # A burst of partial fills must not exceed the broker's limit on these reads.
        self._next_positions = min(self._next_positions, self._last_read + FAST_READ_SECONDS)
        self._io_wake.set()

    # -- threads ---------------------------------------------------------------------------------
    def _check_loop(self) -> None:
        while not self._stop.is_set():
            self._quote_arrived.wait(timeout=1.0)  # the timeout covers what depends on time alone: giving up on an order
            self._quote_arrived.clear()
            try:
                self.check(utcnow())
                self.errors.pop("check", None)
            except Exception as exc:
                self._failed("check", exc)
            self._stop.wait(MIN_PASS_SECONDS)

    def _io_loop(self) -> None:
        while not self._stop.is_set():
            now = utcnow()
            wait = 1.0
            try:
                if self._leader() and self._in_window(now):
                    self._open()
                    self.step(now)
                    self.errors.pop("feed", None)
                else:
                    self._shut()
                    self._exits = self._seen = None  # the next holder of the lease, or tomorrow, reads what was saved
                    wait = RETRY_SECONDS
            except Exception as exc:  # OpenD down or logged out: try again
                self._failed("feed", exc)
                wait = RETRY_SECONDS
            self._io_wake.wait(timeout=wait)
            self._io_wake.clear()
        try:
            self._shut()
        except Exception as exc:
            self._failed("feed", exc)

    def _failed(self, name: str, exc: Exception) -> None:
        if self.errors.get(name) != type(exc).__name__:
            logger.warning("Shadow exits %s failed: %s", name, type(exc).__name__)
        self.errors[name] = type(exc).__name__

    # -- the check -------------------------------------------------------------------------------
    def check(self, now: datetime) -> None:
        """One pass over what is in memory; no network and no database."""
        feed = self._feed
        opening, closing = self._session(now)
        if feed is None or self._exits is None or not self._in_session(now):
            return
        raw = feed.raw()
        if raw is None or not feed.alive(now):
            return  # the time without a pass counts as not watching
        started = time.perf_counter()
        quotes = feed.quotes(now, opening)
        day = _local(now).date()
        view = build_view(raw, quotes, day, now=now, require_fresh=True)
        blind = (now - max(self._watched_at or opening, opening)).total_seconds() > GAP_SECONDS
        if blind:
            self._seen = set()
        lows = self.monitor.price_levels(day)
        trend_blind = bool(lows) and self._lows_day != day and (now - opening).total_seconds() > GAP_SECONDS
        if lows:
            self._lows_day = day
        self._marking = now >= closing - timedelta(minutes=shadow_exits.MARK_MINUTES)
        with self._state:
            exits_changed, levels_changed = shadow_exits.observe(
                self._exits, self._levels, view, self._plans, self._rules, _prices(view), quotes, now,
                marking=self._marking, price_levels=lows, blind=blind, seen=self._seen, trend_blind=trend_blind,
                fills=feed.fills())
            self._exits_dirty = self._exits_dirty or exits_changed
            self._levels_dirty = self._levels_dirty or levels_changed
        self._watched_at = now
        self.passes += 1
        self._pass_ms.append((time.perf_counter() - started) * 1000)
        if exits_changed or levels_changed:
            self._io_wake.set()

    # -- the slower work -------------------------------------------------------------------------
    def step(self, now: datetime) -> None:
        """Everything that may wait on OpenD or the database. One part failing does not stop the others."""
        clock = self._clock()
        for name, part in (("positions", self._read_positions), ("settings", self._read_settings),
                           ("snapshot", self._read_snapshot)):
            try:
                part(now, clock)
                self.errors.pop(name, None)
            except Exception as exc:  # the position list ageing blocks would-be orders on its own
                self._failed(name, exc)
        try:
            self.save()
            self.errors.pop("save", None)
        except Exception as exc:  # kept in memory; the next step saves it
            self._failed("save", exc)
        self._watch_the_feed(now, clock)

    def _read_positions(self, now: datetime, clock: float) -> None:
        if clock < self._next_positions:
            return
        fast = clock < self._fast_until
        self._last_read = clock
        self._next_positions = clock + (FAST_READ_SECONDS if fast else POSITION_SECONDS)
        settled = self._feed.read_positions(now, refresh=fast)
        if not settled:
            self._next_positions = min(self._next_positions, clock + FAST_READ_SECONDS)
        raw = self._feed.raw() or {}
        rows = raw.get("positions") or []
        with self._state:
            followed = shadow_exits.codes(self._exits or {}, _local(now).date())
            followed_legs = shadow_exits.codes(self._exits or {}, _local(now).date(), legs_only=True)
        book = [parse_code(row["code"])["ticker"] for row in rows]
        self._feed.watch([*self.holdings.codes(raw), *followed], [*book, *followed_legs])
        self._quote_arrived.set()

    def _read_settings(self, now: datetime, clock: float) -> None:
        if clock < self._next_settings:
            return
        self._next_settings = clock + SETTINGS_SECONDS
        self._load_settings()
        raw = self._feed.raw()
        opening, _ = self._session(now)
        if raw is None or not self._in_session(now):
            return
        day = _local(now).date()
        view = build_view(raw, self._feed.quotes(now, opening), day, now=now, require_fresh=True)
        with self._state:
            outdated = shadow_exits.outdated_plans(self._exits or {}, self._plans)
        if outdated or any(row["key"] not in self._plans for row in position_plans.open_rows(view)):
            self.monitor.plan_new_positions(view, day, outdated)

    def _load_settings(self) -> None:
        self._plans = self.holdings.plans()["plans"]
        self._rules = self.holdings.rules()

    def _read_snapshot(self, now: datetime, clock: float) -> None:
        if clock < self._next_snapshot:
            return
        self._next_snapshot = clock + SNAPSHOT_SECONDS
        self._feed.refresh()

    def _watch_the_feed(self, now: datetime, clock: float) -> None:
        feed = self._feed
        if clock >= self._next_feed_save:
            self._next_feed_save = clock + FEED_SAVE_SECONDS
            try:
                self._save_feed_day(feed, now)
                self.errors.pop("save", None)
            except Exception as exc:
                self._failed("save", exc)
        if not self._in_session(now) or feed.pushing(now):
            self._silent_since = None
            return
        self._silent_since = self._silent_since or now
        if (now - self._silent_since).total_seconds() >= REOPEN_SECONDS:
            logger.warning("Shadow exits: no pushed quotes for %d s, reconnecting", REOPEN_SECONDS)
            self._shut()  # the next pass of the loop opens it again

    def save(self, *, force: bool = False) -> None:
        """Results go to the database when they change, a couple of seconds apart at most."""
        clock = self._clock()
        if not force and clock < self._saved_at + (SAVE_SECONDS_MARKING if self._marking else SAVE_SECONDS):
            return
        with self._state:
            if not self._exits_dirty and not self._levels_dirty:
                return
            exits = copy.deepcopy(self._exits) if self._exits_dirty else None
            levels = copy.deepcopy(self._levels) if self._levels_dirty else None
            self._exits_dirty = self._levels_dirty = False
        self._saved_at = clock
        try:
            if exits is not None:
                self.holdings.repo.set_setting(shadow_exits.EXITS_SETTING, exits)
            if levels is not None:
                self.holdings.repo.set_setting(shadow_exits.LEVELS_SETTING, levels)
        except Exception:
            with self._state:
                self._exits_dirty = self._exits_dirty or exits is not None
                self._levels_dirty = self._levels_dirty or levels is not None
            raise

    def _save_feed_day(self, feed: Any, now: datetime) -> None:
        """The day's measured delays, kept for a month: most days nothing triggers, and this still says how fast it was."""
        state = feed.status(now)
        if not any(state["pushes"].values()):
            return
        days = self.holdings.repo.setting(shadow_exits.FEED_SETTING, {}) or {}
        days[_local(now).date().isoformat()] = {key: state[key] for key in (
            "pushes", "stock_quote_s", "option_quote_s", "book_s", "fill_to_position_s", "subscribed", "refused")}
        self.holdings.repo.set_setting(shadow_exits.FEED_SETTING,
                                       {day: days[day] for day in sorted(days)[-FEED_DAYS:]})

    # -- what the card and the Status page show --------------------------------------------------
    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        feed = self._feed
        state = "closed"
        if self._in_session(now) and self._leader():
            state = "down"
            if feed is not None and feed.alive(now):
                state = "live" if feed.pushing(now) else "snapshot"
        pass_ms = list(self._pass_ms)
        return {"state": state, "passes": self.passes, "errors": dict(self.errors),
                "pass_ms": shadow_exits.spread(pass_ms),
                **(feed.status(now) if feed is not None else {})}

    def live_view(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """The positions as the live feed last read them (every 15 s, at once after a fill), priced on its quotes.

        None outside the session or before the feed's first read; the trading assistant then uses the last sync."""
        now = now or utcnow()
        feed = self._feed
        raw = feed.raw() if feed is not None else None
        if raw is None or not self._in_session(now):
            return None
        opening, _ = self._session(now)
        return build_view(raw, feed.quotes(now, opening), _local(now).date(), now=now)

    def health(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        state = self.status(now)
        if state["state"] == "closed":
            return {"state": "off", "detail": "connects a few minutes before the regular session"}
        if state["state"] == "live":
            detail = (f"live: last quote {state.get('last_quote_s')} s ago, positions read "
                      f"{state.get('positions_read_s')} s ago, {state['passes']} checks")
            return {"state": "warn" if state["errors"] else "ok",
                    "detail": detail + (f"; failing: {', '.join(sorted(state['errors']))}" if state["errors"] else "")}
        if state["state"] == "snapshot":
            return {"state": "warn", "detail": f"pushes are silent: checking on {SNAPSHOT_SECONDS}-second snapshots"}
        last = self._watched_at
        down_for = (now - max(last or self._session(now)[0], self._session(now)[0])).total_seconds()
        if down_for < DOWN_IS_ERROR_SECONDS:
            return {"state": "warn", "detail": "waiting for live quotes"}
        return {"state": "error", "detail": f"no live quotes for {int(down_for // 60)} min"
                                            + (f" ({', '.join(sorted(state['errors'].values()))})" if state["errors"] else "")}

