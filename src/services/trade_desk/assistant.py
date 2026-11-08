"""The trading assistant's session: one to three stocks you start, watched in real time until you stop.

Phase 2 of docs/trading-assistant-plan.md. Nothing runs before **Start**; **Stop**, or 15 minutes after
the close, ends it. Everything here is computed, never written by a model: the day's levels, your
positions on the stocks, the option contracts around the price and the alerts. The model's notes
come later and never hold up an alert. Read only: DSA never sends, changes or cancels an order.

Four threads; the first two are like the exit test's live feed (``shadow_watch.py``):

- **check**, every ``CHECK_SECONDS``, reads only memory: the chart service's pushed trades and 1- and
  15-minute bars (``chart.py``). It alerts when the price crosses a level, notes a 15-minute close and
  warns before the close on a 0DTE position.
- **io** reads OpenD and the database: each stock's chart (levels and flags) at the start and after
  every 15-minute close, today's 1-minute bars once, the contract panel's quotes every
  ``CONTRACT_SECONDS`` and your positions every ``POSITION_SECONDS``.
- **notes**, the analyst model's notes (``notes_step``), and **watch**, the fast watcher model's reads
  (``watch_step``, assistant_watch.py), so a slow analyst never holds up the watcher. Neither holds up an alert.

Levels carry their register verdicts (``register.py``); none has passed. Contract values at a level
are Black-Scholes estimates at the contract's quoted implied volatility.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import assistant_grades as ag
from . import register
from .analytics import black_scholes_price
from .chart import _validate
from .models import utcnow

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
MAX_TICKERS = 3
HORIZONS = {"0dte": "0DTE options", "intraday": "Intraday", "swing": "Swing"}
SWING_DAYS = 14  # a swing session's contracts: the first expiry at least this many days out
CHECK_SECONDS = 0.25
IO_SECONDS = 1.0
CONTRACT_SECONDS = 5
POSITION_SECONDS = 15
TOUCH_SECONDS = 30
RELOAD_AFTER = timedelta(seconds=3)  # after a 15-minute close, like the chart page
AFTER_CLOSE = timedelta(minutes=15)
PREMARKET_SETTLE = timedelta(seconds=30)  # the pre-market range is read until this long after the open
PANEL_STRIKES = 5  # each side of the price
REPEAT = timedelta(minutes=15)  # one alert per level and direction in this long
LEVEL_BAND = 0.0005  # a level counts as crossed once the price trades this far past it (0.05%)
PNL_STEP = {"option": 10.0, "stock": 2.0}  # P&L on cost, in points: see _pnl_alert
CLOSE_WARNINGS = (30, 15)  # minutes before the close, for a position expiring today
EVENTS = 300
SILENT = timedelta(seconds=60)
NOTES_KEPT = 12
NOTE_KINDS = ("level", "flag", "position", "pnl", "close_soon", "plan")  # events that ask for a note
URGENT = ("position", "plan")  # a fill, a plan's trigger, stop, target or time stop: a note without waiting
NOTES_SECONDS = 1.0
MINUTES_A_YEAR = 365 * 24 * 60
# What each level rests on, for its label (register.py); your own levels carry none.
LEVEL_RULES = {"val": "value_area_levels", "vah": "value_area_levels", "prior_high": "prior_day_levels",
               "prior_low": "prior_day_levels", "prior_close": "prior_day_levels", "vwap": "vwap",
               "or5_high": "opening_range", "or5_low": "opening_range", "or15_high": "opening_range",
               "or15_low": "opening_range", "pm_high": "premarket_levels", "pm_low": "premarket_levels"}


def _session_bounds(day: date) -> Tuple[Optional[datetime], Optional[datetime]]:
    from .option_recorder import session_bounds
    return session_bounds(day)


def _local(when: datetime) -> datetime:
    return when.astimezone(_NEW_YORK)


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value: Optional[float], base: Optional[float]) -> Optional[float]:
    return round((value / base - 1) * 100, 2) if value is not None and base else None


class _Ticker:
    def __init__(self, ticker: str):
        self.ticker, self.code = ticker, f"US.{ticker}"
        self.stream: Any = None
        self.chart_levels: List[Dict[str, Any]] = []  # prior day, value areas, your stops, targets and alerts
        self.flags: List[Dict[str, Any]] = []
        self.flag_states: Optional[Dict[str, str]] = None
        self.bars: List[Dict[str, Any]] = []  # today's regular 1-minute bars
        self.frame_history: Dict[str, List[Dict[str, Any]]] = {}  # earlier sessions' bars per picture timeframe
        self.bars15: List[Dict[str, Any]] = []  # the chart's 15-minute bars and boxes, for the pictures
        self.boxes15: List[Dict[str, Any]] = []
        self.bar_seq: Optional[int] = None
        self.premarket: Tuple[Optional[float], Optional[float]] = (None, None)
        self.premarket_final = False
        self.prev_close: Optional[float] = None
        self.price: Optional[float] = None
        self.price_at: Optional[str] = None
        self.received: Optional[datetime] = None
        self.quote_seq: Optional[int] = None
        self.sides: Dict[str, int] = {}
        self.fired: Dict[Tuple[str, str], datetime] = {}
        self.expiry: Optional[date] = None
        self.chain: List[Dict[str, Any]] = []
        self.chain_error: Optional[str] = None
        self.contracts: List[Dict[str, Any]] = []
        self.contracts_at: Optional[datetime] = None
        self.positions: List[Dict[str, Any]] = []
        self.held: Optional[Dict[str, Dict[str, Any]]] = None
        self.pnl_steps: Dict[str, Dict[str, Any]] = {}  # per position: best, worst, peak, gave_back (_pnl_alert)
        self.bars_stale = False  # a pushed bar skipped a minute: today's bars are read again
        self.pending15: List[datetime] = []  # 15-minute bars seen forming, by end, until their close is handled
        self.closed_through: Optional[datetime] = None
        self.reload_at: Optional[datetime] = None
        self.loaded = False
        self.warned: set = set()
        self.plans: List[Dict[str, Any]] = []  # today's plans and calls on this stock (assistant_grades.py)
        self.notes: deque = deque(maxlen=NOTES_KEPT)  # the model's notes and answers, newest first
        self.watch_latest: Optional[Dict[str, Any]] = None  # the watcher's last read (assistant_watch.py)
        self.watched_at: Optional[datetime] = None
        self.watched_minute: Optional[str] = None  # the end of the last completed 1-minute bar it read
        self.error: Optional[str] = None

    # -- levels --------------------------------------------------------------------------------
    def levels(self) -> List[Dict[str, Any]]:
        out = []
        for level in self.chart_levels:
            kind = level["kind"]
            ident = f"{kind}:{level.get('position_key') or level.get('rule_id') or level['label']}"
            out.append({"id": ident, "kind": kind, "label": level["label"], "price": level["price"],
                        "rule": LEVEL_RULES.get(kind)})
        high, low = self.premarket
        if high is not None:
            out.append({"id": "pm_high", "kind": "pm_high", "label": "Pre-market high", "price": high, "rule": "premarket_levels"})
        if low is not None:
            out.append({"id": "pm_low", "kind": "pm_low", "label": "Pre-market low", "price": low, "rule": "premarket_levels"})
        bars = [bar for bar in self.bars if not bar.get("partial")]
        for minutes in (5, 15):
            if len(bars) >= minutes:
                first = bars[:minutes]
                out.append({"id": f"or{minutes}_high", "kind": f"or{minutes}_high", "label": f"Opening range high ({minutes} min)",
                            "price": max(bar["high"] for bar in first), "rule": "opening_range"})
                out.append({"id": f"or{minutes}_low", "kind": f"or{minutes}_low", "label": f"Opening range low ({minutes} min)",
                            "price": min(bar["low"] for bar in first), "rule": "opening_range"})
        vwap = self.vwap()
        if vwap is not None:
            out.append({"id": "vwap", "kind": "vwap", "label": "VWAP", "price": round(vwap, 4), "rule": "vwap"})
        return out

    def vwap(self) -> Optional[float]:
        volume = sum(bar.get("volume") or 0 for bar in self.bars)
        if not volume:
            return None
        return sum((bar["high"] + bar["low"] + bar["close"]) / 3 * (bar.get("volume") or 0) for bar in self.bars) / volume

    def add_bar(self, bar: Dict[str, Any]) -> None:
        """A pushed 1-minute bar: the forming one replaces itself, a new one is appended."""
        row = {key: bar[key] for key in ("time", "open", "high", "low", "close", "volume")}
        row["partial"] = True
        if self.bars and self.bars[-1]["time"] == row["time"]:
            self.bars[-1] = row
        elif not self.bars or row["time"] > self.bars[-1]["time"]:
            if self.bars:
                self.bars[-1]["partial"] = False
                gap = datetime.fromisoformat(row["time"]) - datetime.fromisoformat(self.bars[-1]["time"])
                self.bars_stale = self.bars_stale or gap > timedelta(minutes=1)
            self.bars.append(row)


class Assistant:
    def __init__(self, service: Any, charts: Any, *, clock: Callable[[], datetime] = utcnow,
                 monotonic: Callable[[], float] = time.monotonic):
        self.service, self.charts = service, charts
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._tickers: Dict[str, _Ticker] = {}
        self.horizon: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.stopped: Optional[Dict[str, Any]] = None
        self._events: deque = deque(maxlen=EVENTS)
        self._seq = 0
        self._next: Dict[str, float] = {}
        self._dirty: Dict[str, Dict[str, Any]] = {}  # plan records to save, by id
        self._wants: Dict[str, List[str]] = {}  # why each stock wants a note
        self._watch_wants: Dict[str, List[str]] = {}  # the market events the watcher reads first, by stock
        self._last_note: Dict[str, datetime] = {}
        self._usage: Dict[str, Any] = {"day": None, "calls": 0, "failed": 0, "by": {}, "seconds": deque(maxlen=200),
                                       "last_error": None, "watch_calls": 0, "watch_by": {}, "escalated": 0}
        self._watch_log: List[Dict[str, Any]] = []  # the watcher's decisions this session, graded at the close
        self._analyst_times: List[datetime] = []  # escalations sent to the analyst, for the hourly limit
        self._watch_summary: Optional[Dict[str, Any]] = None  # the forward grade, read at the start and after grading

    # -- start and stop ----------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.started_at is not None

    def start(self, tickers: List[str], horizon: str = "0dte", now: Optional[datetime] = None, *,
              threads: bool = True) -> Dict[str, Any]:
        names = list(dict.fromkeys(_validate(ticker, "1m") for ticker in tickers if str(ticker or "").strip()))
        if not names:
            raise ValueError("Choose one to three US stocks")
        if len(names) > MAX_TICKERS:
            raise ValueError(f"At most {MAX_TICKERS} stocks at a time")
        if horizon not in HORIZONS:
            raise ValueError(f"Horizons are {', '.join(HORIZONS)}")
        now = now or self._clock()
        if self.running:
            self.stop("restarted with other stocks", now)
        with self._lock:
            self._tickers = {name: _Ticker(name) for name in names}
            self.horizon, self.started_at, self.stopped = horizon, now, None
            self._events.clear()
            self._next, self._wants, self._watch_wants, self._last_note = {}, {}, {}, {}
            self._watch_log, self._analyst_times = [], []
        self._watch_summary = self.watch_grades()
        for item in self._tickers.values():
            item.stream = self.charts.stream(item.ticker, "1m")  # quote, 1- and 15-minute bars, with push
        self._load_plans(now)
        self._event(None, "session", f"Watching {', '.join(names)}", f"{HORIZONS[horizon]}; alerts on this page", now)
        if threads:
            self._stop.clear()
            from . import assistant_watch
            self._threads = [threading.Thread(target=self._loop, args=(self.check, CHECK_SECONDS), name="assistant-check", daemon=True),
                             threading.Thread(target=self._loop, args=(self.io, IO_SECONDS), name="assistant-io", daemon=True),
                             threading.Thread(target=self._loop, args=(self.notes_step, NOTES_SECONDS), name="assistant-notes",
                                              daemon=True),
                             threading.Thread(target=self._loop, args=(self.watch_step, assistant_watch.WATCH_STEP_SECONDS),
                                              name="assistant-watch", daemon=True)]
            for thread in self._threads:
                thread.start()
        return self.state(now)

    def stop(self, reason: str = "stopped", now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._clock()
        self._stop.set()
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(timeout=5)
        self._threads = []
        if self.running:
            _, closing = _session_bounds(_local(now).date())
            for item in self._tickers.values():
                if item.stream is not None:
                    item.stream.close()
                if closing is not None and now >= closing:  # the day's bars are complete: grade what traded
                    bars = [bar for bar in item.bars if datetime.fromisoformat(bar["time"]) <= closing]
                    for rec in item.plans:
                        if rec["status"] in ag.WATCHED and ag.settle(rec, bars, closing):
                            self._dirty[rec["id"]] = rec
            self._save_plans()
            self._grade_watch(now)
            self._event(None, "session", "Stopped", reason, now)
            with self._lock:
                self.stopped = {"at": now.isoformat(), "reason": reason}
                self.started_at = None
        return self.state(now)

    def close(self) -> None:
        if self.running:
            self.stop("the server stopped")

    def _grade_watch(self, now: datetime) -> None:
        """The watcher's decisions with a full forward window in today's bars, added to the day's grades."""
        from . import assistant_watch
        repo = self._repo()
        with self._lock:
            decisions, self._watch_log = list(self._watch_log), []
            bars = {item.ticker: [dict(bar) for bar in item.bars if datetime.fromisoformat(bar["time"]) <= now]
                    for item in self._tickers.values()}
        if not decisions or repo is None or not callable(getattr(repo, "set_setting", None)):
            return
        rows = [row for ticker, mine in bars.items()
                for row in assistant_watch.grade_decisions([d for d in decisions if d["ticker"] == ticker], mine)]
        if not rows:
            return
        try:
            stored = repo.setting(assistant_watch.GRADES_SETTING, {}) or {}
            day = _local(now).date().isoformat()
            stored[day] = (stored.get(day) or []) + rows
            repo.set_setting(assistant_watch.GRADES_SETTING, stored)
            self._watch_summary = assistant_watch.summary(stored)
        except Exception as exc:  # the grades miss a day; the session still stops
            logger.warning("Watcher grades not saved: %s", type(exc).__name__)

    def watch_grades(self) -> Dict[str, Any]:
        from . import assistant_watch
        repo = self._repo()
        try:
            stored = repo.setting(assistant_watch.GRADES_SETTING, {}) if repo is not None else {}
        except Exception:
            stored = {}
        return assistant_watch.summary(stored or {})

    def _loop(self, step: Callable[[datetime], None], seconds: float) -> None:
        while not self._stop.wait(seconds):
            try:
                step(self._clock())
            except Exception as exc:  # the next pass tries again; the page shows the error
                logger.warning("Trading assistant %s: %s", getattr(step, "__name__", "step"), type(exc).__name__)

    def _due(self, key: str, seconds: float) -> bool:
        clock = self._monotonic()
        if clock < self._next.get(key, 0.0):
            return False
        self._next[key] = clock + seconds
        return True

    # -- events --------------------------------------------------------------------------------
    def _event(self, ticker: Optional[str], kind: str, title: str, detail: str, now: datetime, *, wants_note: bool = True,
               **extra: Any) -> None:
        from . import assistant_watch
        watcher = ticker and wants_note and kind in NOTE_KINDS and kind not in assistant_watch.ALWAYS \
            and assistant_watch.watch_enabled()
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "at": now.isoformat(), "ticker": ticker, "kind": kind,
                                 "title": title, "detail": detail, **extra})
            if watcher:  # a market event: the watcher reads it first
                self._watch_wants.setdefault(ticker, []).append(f"{kind}: {title}")
            elif ticker and wants_note and kind in NOTE_KINDS:
                self._wants.setdefault(ticker, []).append(f"{kind}: {title}")

    def events_after(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events if event["seq"] > seq]

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    # -- check: memory only, every CHECK_SECONDS ------------------------------------------------------
    def check(self, now: datetime) -> None:
        if not self.running:
            return
        opening, closing = _session_bounds(_local(now).date())
        if closing is not None and self.started_at < closing + AFTER_CLOSE <= now:
            self.stop("the session closed", now)
            return
        for item in list(self._tickers.values()):
            bar = self.charts.pushed_bar(item.code, "K_1M")
            if bar and bar.get("seq") != item.bar_seq and _regular(bar["time"]):
                item.bar_seq = bar["seq"]
                with self._lock:
                    item.add_bar(bar)
            bar15 = self.charts.pushed_bar(item.code, "K_15M")
            if bar15 and _regular(bar15["time"]):
                end = datetime.fromisoformat(bar15["time"])
                # OpenD pushes the next bar's first trade within the wait, so each forming bar is remembered.
                if (item.closed_through is None or end > item.closed_through) and end not in item.pending15:
                    item.pending15.append(end)
            done = [end for end in item.pending15 if now >= end + RELOAD_AFTER]
            if done:
                item.pending15 = [end for end in item.pending15 if end not in done]
                item.closed_through = max(done)
                if item.loaded:
                    item.reload_at = now  # boxes, flags and levels move on completed 15-minute bars
            if opening is not None and now < opening:
                continue  # before the open the price is the pre-market one (io), and plans wait for the session
            quote = self.charts.pushed_quote(item.code)
            if quote and quote.get("seq") != item.quote_seq:
                item.quote_seq = quote["seq"]
                self._price(item, float(quote["price"]), quote.get("at"), quote.get("received") or now, now)
            if item.price is not None:
                self._step_plans(item, now)
            if closing is not None:
                self._close_warnings(item, now, closing)

    def _price(self, item: _Ticker, price: float, at: Optional[str], received: datetime, now: datetime) -> None:
        with self._lock:
            item.price, item.price_at, item.received = price, at, received
            levels = item.levels()
        for level in levels:
            band = level["price"] * LEVEL_BAND
            side = 1 if price > level["price"] + band else -1 if price < level["price"] - band else 0
            before = item.sides.get(level["id"])
            if side:
                item.sides[level["id"]] = side
            if not side or before is None or before == side:
                continue
            direction = "above" if side > 0 else "below"
            last = item.fired.get((level["id"], direction))
            if last is not None and now - last < REPEAT:
                continue
            item.fired[(level["id"], direction)] = now
            verdict = register.label(level["rule"]) if level.get("rule") else "your level"
            self._event(item.ticker, "level", f"{item.ticker} {'above' if side > 0 else 'below'} {level['label']} {level['price']:g}",
                        f"Last {price:g}. {verdict}.", now, level=level["id"], price=price)

    def _close_warnings(self, item: _Ticker, now: datetime, closing: datetime) -> None:
        expiring = [row for row in item.positions if row.get("type") == "option" and row.get("days_left") == 0]
        if not expiring:
            return
        left = (closing - now).total_seconds() / 60
        for minutes in CLOSE_WARNINGS:
            if 0 < left <= minutes and minutes not in item.warned:
                item.warned.add(minutes)
                labels = ", ".join(row["label"] for row in expiring)
                self._event(item.ticker, "close_soon", f"{minutes} minutes to the close", f"Expiring today: {labels}.", now)

    # -- io: OpenD and the database --------------------------------------------------------------------
    def io(self, now: datetime) -> None:
        if not self.running:
            return
        self._save_plans()
        for item in list(self._tickers.values()):
            try:
                if not item.loaded:
                    self._load(item, now)
                elif item.reload_at is not None and now >= item.reload_at:
                    item.reload_at = None
                    self._load_chart(item, now, alert=True)
                    self._load_bars(item, self._provider(), now)  # a pushed update missed within a bar is mended too
                elif item.bars_stale:
                    self._load_bars(item, self._provider(), now)
                item.error = None
            except Exception as exc:
                item.error = type(exc).__name__
        if self._due("premarket", CONTRACT_SECONDS):
            self._premarket(now)
        if self._due("contracts", CONTRACT_SECONDS):
            self._contracts(now)
        if self._due("positions", POSITION_SECONDS):
            self._positions(now)
        if self._due("touch", TOUCH_SECONDS):
            for item in self._tickers.values():
                if item.stream is not None and not item.stream.touch(now):
                    item.stream.close()
                    item.stream = self.charts.stream(item.ticker, "1m")  # released for other charts: take it back

    def _provider(self) -> Any:
        return self.charts._connection()

    def _premarket(self, now: datetime) -> None:
        """Before the open: the last pre-market trade and the pre-market high and low, from the snapshot.

        OpenD's ``last_price`` stays at the prior close until the regular session trades."""
        opening, _ = _session_bounds(_local(now).date())
        items = [item for item in self._tickers.values() if item.loaded and not item.premarket_final]
        if opening is None or not items:
            return
        rows = {str(row.get("code") or ""): row for row in self._provider().snapshot_rows([item.code for item in items])}
        with self._lock:
            for item in items:
                self._set_premarket(item, rows.get(item.code) or {}, price=now < opening)
                # the range is final once the session trades: one more read after the open catches its last seconds
                item.premarket_final = now >= opening + PREMARKET_SETTLE

    @staticmethod
    def _set_premarket(item: _Ticker, snap: Dict[str, Any], *, price: bool = True) -> None:
        item.premarket = (_num(snap.get("pre_high_price")), _num(snap.get("pre_low_price")))
        if price and _num(snap.get("pre_price")):
            item.price, item.price_at = _num(snap.get("pre_price")), "pre-market"

    def _load(self, item: _Ticker, now: datetime) -> None:
        self._load_chart(item, now, alert=False)
        provider = self._provider()
        day = _local(now).date()
        self._load_bars(item, provider, now)
        self._load_frame_history(item, provider, now)
        snap = (provider.snapshot_rows([item.code]) or [{}])[0]
        opening, _ = _session_bounds(day)
        with self._lock:
            item.prev_close = _num(snap.get("prev_close_price"))
            if opening is not None and now < opening:
                self._set_premarket(item, snap)
            else:
                item.premarket = (_num(snap.get("pre_high_price")), _num(snap.get("pre_low_price")))
                item.premarket_final = opening is None or now >= opening + PREMARKET_SETTLE
            if item.price is None and _num(snap.get("last_price")):
                item.price = _num(snap.get("last_price"))
        self._load_chain(item, provider)
        item.loaded = True
        with self._lock:
            self._wants.setdefault(item.ticker, []).append("session: started, first look at the stock")

    def _load_bars(self, item: _Ticker, provider: Any, now: datetime) -> None:
        """Today's regular 1-minute bars from OpenD, then any pushed since: the opening ranges and VWAP rest on them."""
        day = _local(now).date()
        rows = provider.kline(item.code, "K_1M", 400, push=True)
        bars = [row for row in rows if _regular(row["time"]) and _local(datetime.fromisoformat(row["time"])).date() == day]
        with self._lock:
            pushed = [bar for bar in item.bars if not bars or bar["time"] > bars[-1]["time"]]
            item.bars = [{**{k: bar[k] for k in ("time", "open", "high", "low", "close", "volume")}, "partial": bar.get("partial", False)}
                         for bar in bars] + pushed
            item.bars_stale = False

    def _load_frame_history(self, item: _Ticker, provider: Any, now: datetime) -> None:
        """Earlier sessions' bars for the pictures (moomoo history, once a session), so the NX lines have their warm-up."""
        from . import chart_frames, chart_image
        if not chart_image.enabled():
            return
        day = _local(now).date()
        history = {}
        for frame in chart_frames.frames("TRADE_ASSISTANT_CHART_FRAMES", "1m,5m,15m"):
            if frame == "15m":
                continue  # the chart's own 15-minute bars
            rows = chart_frames.bars(item.ticker, frame, provider=provider, now=now, fallback=False)
            history[frame] = [row for row in rows if chart_image._when(row["time"]).date() < day]
        with self._lock:
            item.frame_history = history

    def _load_chart(self, item: _Ticker, now: datetime, *, alert: bool) -> None:
        _, closing = _session_bounds(_local(now).date())
        if item.chart_levels and closing is not None and now >= closing:
            return  # after the close the chart moves on to the next session's levels (today becomes "prior day")
        snapshot = self.charts.chart(item.ticker, "15m", now=now)
        flags = [flag for flag in snapshot.get("flags") or [] if flag["state"] in ("armed", "watching", "open")]
        states = {_flag_key(flag): flag["state"] for flag in snapshot.get("flags") or []}
        if alert and item.flag_states is not None:
            for flag in snapshot.get("flags") or []:
                key = _flag_key(flag)
                before = item.flag_states.get(key)
                if flag["state"] != before and flag["state"] in ("armed", "watching", "open"):
                    what = {"armed": "armed: a limit the next 15-minute bar fills", "watching": "watching for a close past its price",
                            "open": "filled"}[flag["state"]]
                    self._event(item.ticker, "flag", f"{item.ticker} {flag['kind'].replace('_', ' ')} {flag['direction']} {what}",
                                f"At {flag['price']:g}, stop {flag['stop']:g}, target {flag['target']:g}. {flag['grade']['label']}.", now)
        from . import chart_frames
        with self._lock:
            item.chart_levels = list(snapshot.get("levels") or [])
            item.flags, item.flag_states = flags, states
            item.bars15 = chart_frames.starts([row for row in snapshot.get("bars") or [] if not row.get("partial")], 15)
            item.boxes15 = chart_frames.boxes_from(snapshot.get("boxes") or [])

    def _load_chain(self, item: _Ticker, provider: Any) -> None:
        try:
            item.expiry, item.chain = provider.nearest_chain(item.code, SWING_DAYS if self.horizon == "swing" else 0)
            item.chain_error = None if item.chain else "no listed options"
        except Exception as exc:  # the panel says so; retried at the next contract pass
            item.chain_error = type(exc).__name__

    def _contracts(self, now: datetime) -> None:
        wanted: Dict[str, Tuple[_Ticker, Dict[str, Any]]] = {}
        for item in self._tickers.values():
            if not item.loaded:
                continue
            if not item.chain:
                if item.chain_error and item.chain_error != "no listed options":
                    self._load_chain(item, self._provider())
                continue
            if item.price is None:
                continue
            strikes = sorted({c["strike"] for c in item.chain}, key=lambda s: (abs(s - item.price), s))[:2 * PANEL_STRIKES]
            for contract in item.chain:
                if contract["strike"] in strikes:
                    wanted[contract["code"]] = (item, contract)
        if not wanted:
            return
        rows = self._provider().snapshot_rows(list(wanted))
        by_ticker: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            code = str(row.get("code") or "")
            if code in wanted:
                item, contract = wanted[code]
                by_ticker.setdefault(item.ticker, []).append(self._contract_row(item, contract, row, now))
        with self._lock:
            for item in self._tickers.values():
                if item.ticker in by_ticker:
                    item.contracts = sorted(by_ticker[item.ticker], key=lambda r: (r["strike"], r["right"]))
                    item.contracts_at = now

    def _contract_row(self, item: _Ticker, contract: Dict[str, Any], row: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        bid, ask = _num(row.get("bid_price")), _num(row.get("ask_price"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None and ask >= bid > 0 else None
        iv = _num(row.get("option_implied_volatility"))
        sigma = iv / 100 if iv else None
        strike, right, spot = contract["strike"], contract["right"], item.price
        out = {"code": contract["code"], "right": right, "strike": strike, "expiry": item.expiry.isoformat() if item.expiry else None,
               "bid": bid, "ask": ask, "mid": round(mid, 4) if mid is not None else None,
               "spread_pct": round((ask - bid) / mid * 100, 1) if mid else None, "iv": iv,
               "delta": _num(row.get("option_delta")), "volume": _num(row.get("volume")),
               "open_interest": _num(row.get("option_open_interest"))}
        if ask is not None and spot:
            breakeven = strike + ask if right == "call" else strike - ask
            out["breakeven"] = round(breakeven, 4)
            out["breakeven_move_pct"] = _pct(breakeven, spot)
        minutes = self._minutes_left(item, now)
        out["minutes_left"] = round(minutes, 1) if minutes is not None else None
        if sigma and minutes is not None and minutes > 0 and spot:
            value = black_scholes_price(spot, strike, minutes / MINUTES_A_YEAR, sigma, right=right)
            later = black_scholes_price(spot, strike, max(minutes - 1, 0) / MINUTES_A_YEAR, sigma, right=right)
            out["theta_per_min"] = round(later - value, 4)
            out["at_levels"] = [
                {"id": level["id"], "label": level["label"], "price": level["price"],
                 "now": round(black_scholes_price(level["price"], strike, minutes / MINUTES_A_YEAR, sigma, right=right), 3),
                 "in_half_hour": round(black_scholes_price(level["price"], strike, max(minutes - 30, 0) / MINUTES_A_YEAR, sigma,
                                                     right=right), 3)}
                for level in self._nearby_levels(item)]
        return out

    def _minutes_left(self, item: _Ticker, now: datetime) -> Optional[float]:
        if item.expiry is None:
            return None
        _, closing = _session_bounds(item.expiry)
        return max((closing - now).total_seconds() / 60, 0.0) if closing is not None else None

    def _nearby_levels(self, item: _Ticker) -> List[Dict[str, Any]]:
        """The nearest level above and below the price, and your own stops and targets on the stock."""
        levels = item.levels()
        price = item.price or 0
        above = sorted((l for l in levels if l["price"] > price and l.get("rule")), key=lambda l: l["price"])[:1]
        below = sorted((l for l in levels if l["price"] < price and l.get("rule")), key=lambda l: -l["price"])[:1]
        yours = [l for l in levels if l["kind"] in ("stop", "target")]
        return list({l["id"]: l for l in above + below + yours}.values())

    def _positions(self, now: datetime) -> None:
        from . import holdings, position_plans
        if not holdings.enabled():
            return
        store = self.service.holdings
        worker = getattr(self.service, "worker", None)
        shadow = getattr(worker, "_shadow_watch", None)
        view = shadow.live_view(now) if shadow is not None else None
        source = "live feed"
        if view is None:
            view, source = store.view(live=False), "last sync"
        rows = position_plans.panel_rows(view, store.plans()["plans"], store.rules())
        opening, closing = _session_bounds(_local(now).date())
        in_session = opening is not None and opening <= now < closing
        for item in self._tickers.values():
            mine = [{**row, "source": source} for row in rows if row["ticker"] == item.ticker]
            current = {row["key"]: row for row in mine}
            if item.held is not None:
                for key, row in current.items():
                    if key not in item.held:
                        self._event(item.ticker, "position", f"New position: {row['label']}",
                                    f"{_weight(row)} of the account", now)
                for key, row in item.held.items():
                    if key not in current:
                        self._event(item.ticker, "position", f"Position closed: {row['label']}", "", now)
            for key, row in current.items():
                why = self._pnl_alert(item, key, row) if item.held is not None else None
                if why and in_session:
                    self._event(item.ticker, "pnl", f"{row['label']} {row['pnl_pct']:+.1f}% on cost",
                                f"{_weight(row)} of the account; {why}", now)
            with self._lock:
                item.positions, item.held = mine, current

    @staticmethod
    def _pnl_alert(item: _Ticker, key: str, row: Dict[str, Any]) -> Optional[str]:
        """Why a position's P&L on cost is worth an alert now, or None.

        A 0DTE option swings across hundreds of points of its cost in a day, so a fixed step back and forth alerts
        every few minutes. Alerts come on a new high above cost (by a step, or a quarter of the last high when that
        is more), a new low below cost (by a step), and once when it gives back half of its best gain. The first
        reading only sets the marks."""
        pnl, step = row.get("pnl_pct"), PNL_STEP.get(row.get("type"), PNL_STEP["stock"])
        if pnl is None:
            return None
        marks = item.pnl_steps.get(key)
        if marks is None:
            item.pnl_steps[key] = {"best": max(pnl, 0.0), "worst": min(pnl, 0.0), "peak": max(pnl, 0.0), "gave_back": False}
            return None
        marks["peak"] = max(marks["peak"], pnl)
        if pnl >= marks["best"] + max(step, 0.25 * marks["best"]):
            marks.update(best=pnl, gave_back=False)
            return "its best this session"
        if pnl <= marks["worst"] - step:
            marks["worst"] = pnl
            marks["gave_back"] = marks["gave_back"] or pnl <= marks["peak"] / 2
            return "its worst this session"
        if not marks["gave_back"] and marks["peak"] >= 2 * step and pnl <= marks["peak"] / 2:
            marks["gave_back"] = True
            return f"it gave back half of its best this session ({marks['peak']:+.0f}%)"
        return None

    # -- plans and calls (assistant_grades.py) ------------------------------------------------------
    def _repo(self) -> Any:
        return getattr(self.service, "repo", None)

    def _load_plans(self, now: datetime) -> None:
        repo = self._repo()
        if repo is None:
            return
        day = _local(now).date().isoformat()
        for item in self._tickers.values():
            rows = repo.chart_trades(ticker=item.ticker, limit=200)
            item.plans = [row for row in rows if row.get("kind") in ("plan", "call") and row.get("day") == day][::-1]

    def _save_plans(self) -> None:
        with self._lock:
            dirty, self._dirty = list(self._dirty.values()), {}
        repo = self._repo()
        if dirty and repo is not None:
            try:
                repo.save_chart_trades(dirty)
            except Exception:  # kept for the next pass
                with self._lock:
                    for rec in dirty:
                        self._dirty.setdefault(rec["id"], rec)
                raise

    def _step_plans(self, item: _Ticker, now: datetime) -> None:
        for rec in item.plans:
            if rec["status"] not in ag.WATCHED:
                continue
            with self._lock:
                what = ag.step(rec, item.price, item.bars, now)
            if what is None:
                continue
            self._dirty[rec["id"]] = rec
            whose = "Your plan" if rec["source"] == "you" else "Assistant call"
            side = "long" if rec["direction"] == 1 else "short"
            titles = {"triggered": f"{whose} triggered: {item.ticker} {side} at {rec.get('price', item.price):g}",
                      "void": f"{whose} on {item.ticker} is void: {rec.get('void', '')}",
                      "expired": f"{whose} on {item.ticker} expired: not triggered by {rec['time_stop']}",
                      "stop": f"{whose} on {item.ticker} reached its stop {rec['stop']:g}",
                      "target": f"{whose} on {item.ticker} reached its target {rec['target']:g}",
                      "time": f"{whose} on {item.ticker}: time stop {rec['time_stop']}"}
            detail = f"Last {item.price:g}; stop {rec['stop']:g}, target {rec['target']:g}, out by {rec['time_stop']}."
            self._event(item.ticker, "plan", titles[what], detail, now, plan=rec["id"])

    def _item(self, ticker: str) -> _Ticker:
        name = _validate(ticker, "1m")
        item = self._tickers.get(name) if self.running else None
        if item is None:
            raise ValueError(f"Start the assistant on {name} first")
        return item

    def add_plan(self, ticker: str, fields: Dict[str, Any], *, text: str = "", source: str = "you", why: str = "",
                 model: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
        """A plan you confirmed (or a call of the assistant's), watched from now on and graded after the session."""
        now = now or self._clock()
        item = self._item(ticker)
        if item.price is None:
            raise ValueError(f"No price for {item.ticker} yet")
        opening, closing = _session_bounds(_local(now).date())
        if closing is None or now >= closing:
            raise ValueError("Plans are for the regular session")
        if now < opening and str((fields.get("trigger") or {}).get("kind") or "now") == "now":
            raise ValueError("Before the open a plan needs a trigger price: entering now would use a pre-market price")
        plan = ag.check_plan(fields, item.price, closing, now)
        rec = ag.new_record(item.ticker, source, plan, item.price, now, text=text, why=why, model=model)
        repo = self._repo()
        if repo is not None:
            repo.save_chart_trades([rec])
        with self._lock:
            item.plans.append(rec)
        whose = "Plan" if source == "you" else "Assistant call"
        self._event(item.ticker, "plan", f"{whose} watched: {item.ticker} {_plan_words(rec)}",
                    f"Stop {rec['stop']:g}, target {rec['target']:g}, out by {rec['time_stop']}.", now, plan=rec["id"],
                    wants_note=False)
        return ag.view(rec)

    def remove_plan(self, record_id: str, now: Optional[datetime] = None) -> bool:
        """Only a plan that has not triggered can be removed; once it traded it stays on the record."""
        now = now or self._clock()
        for item in self._tickers.values():
            for rec in item.plans:
                if rec["id"] == record_id and rec["status"] == "waiting":
                    repo = self._repo()
                    if repo is not None and not repo.delete_chart_trade(record_id, ("waiting",)):
                        return False
                    with self._lock:
                        item.plans.remove(rec)
                    self._event(item.ticker, "plan", f"Plan removed on {item.ticker}: {_plan_words(rec)}", "", now,
                                wants_note=False)  # which one: the models read the title
                    return True
        return False

    def parse_plan(self, ticker: str, text: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """The model's reading of a plan you typed, for you to check and confirm (nothing is watched yet)."""
        from . import assistant_notes
        now = now or self._clock()
        item = self._item(ticker)
        with self._lock:
            ticker_state = {"ticker": item.ticker, "price": item.price, "levels": item.levels()}
        self._check_cap(now)
        try:
            out = assistant_notes.parse_plan(text, ticker_state, now)
        except RuntimeError as exc:  # every backend failed: the page says so, and the form still works
            self._failed(exc)
            raise ValueError(assistant_notes.answer_failed(exc)) from exc
        self._count(out.get("model", ""), out.get("seconds"))
        return out

    # -- what the page shows ---------------------------------------------------------------------------
    def _ticker_state(self, item: _Ticker, now: datetime) -> Dict[str, Any]:
        levels = item.levels()
        price = item.price
        return {
            "ticker": item.ticker, "price": price, "price_at": item.price_at,
            "change_pct": _pct(price, item.prev_close),
            "quote_age_s": round((now - item.received).total_seconds(), 1) if item.received else None,
            "levels": sorted(({**{k: level[k] for k in ("id", "kind", "label", "price")},
                               "distance_pct": _pct(level["price"], price),
                               "verdict": register.get(level["rule"]).verdict if level.get("rule") else None,
                               "verdict_label": register.label(level["rule"]) if level.get("rule") else "Your level"}
                              for level in levels), key=lambda level: -level["price"]),
            "flags": [{k: flag.get(k) for k in ("kind", "box", "direction", "state", "price", "stop", "target", "time")}
                      | {"grade": flag["grade"]["label"]} for flag in item.flags],
            "contracts": {"expiry": item.expiry.isoformat() if item.expiry else None, "rows": list(item.contracts),
                          "as_of": item.contracts_at.isoformat() if item.contracts_at else None,
                          "error": item.chain_error},
            "positions": list(item.positions), "loaded": item.loaded, "error": item.error,
            "plans": [ag.view(rec) for rec in item.plans][::-1],
            "notes": [dict(note) for note in item.notes],
            "watch": dict(item.watch_latest) if item.watch_latest else None,
        }

    def state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._clock()
        with self._lock:
            tickers = [self._ticker_state(item, now) for item in self._tickers.values()]
            seconds = sorted(self._usage["seconds"])
            model = {"calls_today": self._usage["calls"], "cap": _daily_cap(), "failed": self._usage["failed"],
                     "median_seconds": seconds[len(seconds) // 2] if seconds else None, "by": [{"label": label, "calls": calls} for label, calls in self._usage["by"].items()],
                     "last_error": self._usage["last_error"], "note_minutes": _note_gap().total_seconds() / 60,
                     "order": _model_order(), "watch": _watch_state(self._usage, self._watch_summary)}
            return {"running": self.running, "horizon": self.horizon, "horizons": HORIZONS,
                    "started_at": self.started_at.isoformat() if self.started_at else None, "stopped": self.stopped,
                    "max_tickers": MAX_TICKERS, "tickers": tickers, "events": [dict(e) for e in list(self._events)[-60:]][::-1],
                    "seq": self._seq, "model": model,
                    "note": "Levels and contract values are computed; none of these levels has passed a test. "
                            "Contract values at a level are Black-Scholes estimates at the quoted implied volatility. "
                            "The assistant's calls are untested model calls, graded against random timing. "
                            "Nothing is sent to moomoo."}

    # -- notes, calls and questions (assistant_notes.py; your plans first, the API last) -----------------------
    def _roll_usage(self, now: datetime) -> None:
        day = _local(now).date()
        if self._usage["day"] != day:
            self._usage.update(day=day, calls=0, failed=0, by={}, last_error=None, watch_calls=0, watch_by={}, escalated=0)

    def _check_cap(self, now: datetime) -> None:
        self._roll_usage(now)
        if self._usage["calls"] >= _daily_cap():
            raise ValueError(f"The assistant's {_daily_cap()} model calls for today are used (TRADE_ASSISTANT_DAILY_CALLS)")

    def _count(self, label: str, seconds: Optional[float]) -> None:
        with self._lock:
            self._usage["calls"] += 1
            self._usage["by"][label or "model"] = self._usage["by"].get(label or "model", 0) + 1
            if seconds is not None:
                self._usage["seconds"].append(float(seconds))

    def _failed(self, exc: Exception) -> None:
        with self._lock:
            self._usage["failed"] += 1
            self._usage["last_error"] = str(exc)[:300]

    def _context(self, item: _Ticker, now: datetime, reasons: List[str]) -> Dict[str, Any]:
        from . import assistant_notes
        _, closing = _session_bounds(_local(now).date())
        from . import assistant_watch
        with self._lock:
            state = self._ticker_state(item, now)
            events = [dict(e) for e in self._events if e.get("ticker") == item.ticker]
            bars = [dict(bar) for bar in item.bars]
        repo = self._repo()
        try:
            in_play = repo.setting("in_play", None) if repo is not None and callable(getattr(repo, "setting", None)) else None
        except Exception:
            in_play = None
        if in_play and in_play.get("day") != _local(now).date().isoformat():
            in_play = None
        found = assistant_watch.facts(state, bars, now, band=LEVEL_BAND, in_play=in_play)
        return assistant_notes.context(state, horizon=self.horizon or "0dte", now=now, closing=closing, events=events,
                                       reasons=reasons, facts=found)

    def notes_step(self, now: datetime) -> None:
        """The analyst's notes, at most one a pass, each stock at most every few minutes unless urgent: a fill, a
        plan's event or what the watcher hands over. With a watcher, market events go to it first (watch_step); this
        queue then holds only your events, the first look and the watcher's hand-overs."""
        if not self.running:
            return
        self._roll_usage(now)
        for item in list(self._tickers.values()):
            with self._lock:
                reasons = list(self._wants.get(item.ticker) or [])
            if not reasons or not item.loaded or item.price is None:
                continue
            last = self._last_note.get(item.ticker)
            urgent = any(reason.split(":")[0] in URGENT or reason.startswith("watcher") for reason in reasons)
            if last is not None and now - last < _note_gap() and not urgent:
                continue
            with self._lock:  # what arrived meanwhile comes too
                reasons, self._wants[item.ticker] = list(self._wants.get(item.ticker) or []), []
            self._last_note[item.ticker] = now
            if self._usage["calls"] >= _daily_cap():
                return
            self._write_note(item, reasons, now)
            return

    def watch_step(self, now: datetime) -> None:
        """The watcher's thread (assistant_watch.py), one read a pass: a stock's market events first, at most every
        TRADE_ASSISTANT_WATCH_SECONDS; then, in the regular session, each completed 1-minute bar it hasn't read, the
        stock read longest ago first."""
        from . import assistant_watch
        if not self.running or not assistant_watch.watch_enabled():
            return
        self._roll_usage(now)
        if self._usage["watch_calls"] >= assistant_watch.watch_daily_cap():
            return
        opening, closing = _session_bounds(_local(now).date())
        session = opening is not None and closing is not None and opening <= now < closing and assistant_watch.every_minute()
        gap = assistant_watch.watch_gap()
        due = []
        for item in list(self._tickers.values()):
            if not item.loaded or item.price is None:
                continue
            with self._lock:
                waiting = bool(self._watch_wants.get(item.ticker))
                done = assistant_watch.completed(item.bars, now)
                minute = done[-1]["time"] if done else None
            read_at = item.watched_at.timestamp() if item.watched_at else 0.0
            if waiting and (item.watched_at is None or now - item.watched_at >= gap):
                due.append((0, read_at, item.ticker, "event", minute))
            elif session and minute is not None and minute != item.watched_minute:
                due.append((1, read_at, item.ticker, "minute", minute))
        if not due:
            return
        _, _, ticker, trigger, minute = min(due)
        item = self._tickers[ticker]
        with self._lock:
            reasons = list(self._watch_wants.get(ticker) or []) if trigger == "event" else []
            if trigger == "event":
                self._watch_wants[ticker] = []
        if trigger == "minute":
            reasons = [f"minute: the 1-minute bar to {_local(datetime.fromisoformat(minute)).strftime('%H:%M')} closed"]
        item.watched_at, item.watched_minute = now, minute or item.watched_minute
        self._watch(item, reasons, now, trigger)

    def _chart_images(self, item: _Ticker, frames: Optional[List[str]] = None) -> Optional[List[Dict[str, str]]]:
        """The stock's pictures for the model (TRADE_ASSISTANT_CHART_FRAMES, default 1m,5m,15m; chart_frames.py):
        earlier sessions from moomoo's history plus today's live 1-minute bars, with the levels, open plans and
        flags; the 15-minute one is the chart's own, with its boxes."""
        from . import chart_frames, chart_image
        from src.llm.generation_backend import MAX_GENERATION_IMAGES
        if not chart_image.enabled():
            return None
        with self._lock:
            today = [dict(bar) for bar in item.bars if not bar.get("partial")]
            history = {frame: list(rows) for frame, rows in item.frame_history.items()}
            bars15, boxes15 = list(item.bars15), list(item.boxes15)
            levels = [{"label": level["label"], "price": level["price"], "kind": level.get("kind")} for level in item.levels()]
            plans = [dict(plan) for plan in item.plans if plan.get("status") in ("waiting", "open")]
            flags = [dict(flag) for flag in item.flags]
        if len(today) < 2:
            return None  # before the open: the numbers say it all
        for plan in plans:
            who = "Your plan" if plan.get("source") == "you" else "Assistant call"
            trigger = (plan.get("trigger") or {}).get("price")
            for kind, value, word in (("trigger", trigger, "trigger"), ("stop", plan.get("stop"), "stop"),
                                      ("target", plan.get("target"), "target")):
                if isinstance(value, (int, float)):
                    levels.append({"label": f"{who} {word}", "price": value, "kind": kind})
        for flag in flags:
            levels.append({"label": f"{flag['kind'].replace('_', ' ')} {flag['direction']} flag", "price": flag.get("price"),
                           "kind": "flag"})
        minutes = chart_frames.starts(today, 1)
        out = []
        for frame in (frames or chart_frames.frames("TRADE_ASSISTANT_CHART_FRAMES", "1m,5m,15m"))[:MAX_GENERATION_IMAGES]:
            if frame == "1m":
                rows = history.get("1m", []) + minutes
            elif frame == "15m":
                rows = bars15 or history.get("15m", []) + chart_image.aggregate(minutes, 15)
            elif frame == "5m":
                rows = history.get("5m", []) + chart_image.aggregate(minutes, 5)
            else:
                rows = history.get(frame, [])
            image = chart_frames.picture(item.ticker, frame, rows, levels=levels, boxes=boxes15 if frame == "15m" else ())
            if image:
                out.append(image)
        return out or None

    def _watch(self, item: _Ticker, reasons: List[str], now: datetime, trigger: str = "event") -> None:
        """One watcher read: the short note and whether the analyst should look, kept for the forward grade. An
        escalation, or an event no watcher could read, joins the analyst's queue (notes_step) within its hourly
        limit, so the watcher never waits for the analyst. A minute read that doesn't escalate only replaces the
        stock's latest read; events and escalations also go to the page's notes."""
        from . import assistant_notes, assistant_watch
        ctx = {**self._context(item, now, reasons), **self._watch_memory(item)}
        try:
            result = assistant_notes.ask_model(assistant_watch.WATCH_SYSTEM, ctx, chain="watch",
                                               images=self._chart_images(item, frames=assistant_watch.watch_frames()))
        except RuntimeError as exc:  # no watcher answered: the analyst reads an event, within its hourly limit
            self._failed(exc)
            if trigger == "event" and self._analyst_room(now):
                self._hand_over(item, reasons + ["watcher: unavailable"], now)
            return
        with self._lock:
            self._usage["watch_calls"] += 1
            self._usage["watch_by"][result.get("label") or "model"] = self._usage["watch_by"].get(result.get("label") or "model", 0) + 1
        entry = {"at": now.isoformat(), "kind": "note", "tier": "watcher", "trigger": trigger, "model": result.get("label", ""),
                 "seconds": result.get("seconds"), "call": None}
        try:
            watched = assistant_watch.read_watch(result["data"], ctx)
        except ValueError as exc:
            entry.update(kind="held", text=f"A watcher note was held back: {exc}.")
            with self._lock:
                item.notes.appendleft(entry)
                item.watch_latest = dict(entry)
            return
        escalate = watched["escalate"]
        entry.update(text=watched["text"], escalate=escalate, why=watched["why"])
        room = escalate and self._analyst_room(now)
        if escalate and not room:
            entry["why"] += f" (the analyst's {assistant_watch.analyst_per_hour()} escalations this hour are used)"
        posted = trigger == "event" or escalate
        with self._lock:
            self._watch_log.append({"at": now.isoformat(), "ticker": item.ticker, "escalate": escalate, "trigger": trigger,
                                    "price": item.price, "why": watched["why"]})
            item.watch_latest = dict(entry)
            if posted:
                item.notes.appendleft(entry)
        if posted:
            self._event(item.ticker, "note", f"{item.ticker}: watcher note", entry["text"][:240], now, wants_note=False)
        if room:
            with self._lock:
                self._usage["escalated"] += 1
            self._hand_over(item, reasons + [f"watcher escalated: {watched['why']}"], now)

    def _hand_over(self, item: _Ticker, reasons: List[str], now: datetime) -> None:
        """Into the analyst's queue; one still waiting there for the stock takes this one along, as one note."""
        with self._lock:
            queue = self._wants.setdefault(item.ticker, [])
            waiting = any(reason.startswith("watcher") for reason in queue)
            queue.extend(reasons)
        if not waiting:
            self._analyst_used(now)

    def _watch_memory(self, item: _Ticker) -> Dict[str, Any]:
        """The watcher's own last read and the analyst's last note on the stock: it escalates what changed, not a repeat."""
        with self._lock:
            last = dict(item.watch_latest) if item.watch_latest else None
            analyst = next((dict(note) for note in item.notes if note.get("kind") == "note" and note.get("tier") != "watcher"), None)
        hhmm = lambda text: _local(datetime.fromisoformat(text)).strftime("%H:%M")
        out: Dict[str, Any] = {}
        if last and last.get("kind") == "note":
            out["your_last_read"] = {"time": hhmm(last["at"]), "note": last.get("text"), "escalated": bool(last.get("escalate"))}
        if analyst:
            out["analyst_last_note"] = {"time": hhmm(analyst["at"]), "note": str(analyst.get("text") or "")[:400]}
        return out

    def _analyst_room(self, now: datetime) -> bool:
        from . import assistant_watch
        with self._lock:
            self._analyst_times = [when for when in self._analyst_times if now - when < timedelta(hours=1)]
            return len(self._analyst_times) < assistant_watch.analyst_per_hour()

    def _analyst_used(self, now: datetime) -> None:
        with self._lock:
            self._analyst_times.append(now)

    def _write_note(self, item: _Ticker, reasons: List[str], now: datetime) -> None:
        from . import assistant_notes, assistant_watch
        ctx = self._context(item, now, reasons)
        try:
            result = assistant_notes.ask_model(assistant_notes.NOTE_SYSTEM, ctx, images=self._chart_images(item))
        except RuntimeError as exc:
            self._failed(exc)
            return
        self._count(result.get("label", ""), result.get("seconds"))
        entry = {"at": now.isoformat(), "kind": "note", "model": result.get("label", ""), "seconds": result.get("seconds"),
                 "call": None, **({"tier": "analyst"} if assistant_watch.watch_enabled() else {})}
        try:
            note = assistant_notes.read_note(result["data"], ctx)
        except ValueError as exc:  # it quoted a price the system doesn't show: the page says so, not what it wrote
            entry.update(kind="held", text=f"A note was held back: {exc}.")
            with self._lock:
                item.notes.appendleft(entry)
            return
        entry["text"] = note["text"]
        call = note["call"]
        if call:
            try:
                fields = {"direction": call.get("direction"), "trigger": call.get("trigger") or {"kind": "now"},
                          "stop": call.get("stop"), "target": call.get("target"), "time_stop": call.get("time_stop")}
                plan = self.add_plan(item.ticker, fields, source="assistant", why=str(call.get("why") or ""),
                                     model=entry["model"], now=now)
                entry["call"] = (f"{plan['direction']} {plan['trigger']['kind'].replace('_', ' ')}"
                                 + (f" {plan['trigger']['price']:g}" if plan["trigger"].get("price") else "")
                                 + f", stop {plan['stop']:g}, target {plan['target']:g}, out by {plan['time_stop']}")
            except ValueError as exc:
                entry["text"] += f" (Its call was not recorded: {exc}.)"
        with self._lock:
            item.notes.appendleft(entry)
        self._event(item.ticker, "note", f"{item.ticker}: assistant note", entry["text"][:240], now, wants_note=False)

    def ask(self, ticker: str, question: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Your question about a stock in the session, answered from its computed state."""
        from . import assistant_notes
        now = now or self._clock()
        item = self._item(ticker)
        question = (question or "").strip()[:500]
        if not question:
            raise ValueError("Ask a question")
        self._check_cap(now)
        ctx = self._context(item, now, ["question"])
        try:
            result = assistant_notes.ask_model(assistant_notes.ASK_SYSTEM, ctx, question, images=self._chart_images(item))
        except RuntimeError as exc:
            self._failed(exc)
            raise ValueError(assistant_notes.answer_failed(exc)) from exc
        self._count(result.get("label", ""), result.get("seconds"))
        entry = {"at": now.isoformat(), "kind": "answer", "question": question, "model": result.get("label", ""),
                 "seconds": result.get("seconds"), "call": None}
        try:
            entry["text"] = assistant_notes.read_note(result["data"], ctx, answer=True)["text"]
        except ValueError as exc:
            entry.update(kind="held", text=f"The answer was held back: {exc}. Ask again.")
        with self._lock:
            item.notes.appendleft(entry)
        return entry

    def health(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._clock()
        if not self.running:
            return {"state": "off", "detail": "no session (Chart tab → Start)"}
        names = ", ".join(self._tickers)
        quiet = [item.ticker for item in self._tickers.values()
                 if item.received is None or now - item.received > SILENT]
        opening, closing = _session_bounds(_local(now).date())
        in_session = opening is not None and opening <= now < closing
        if in_session and quiet:
            return {"state": "warn", "detail": f"{names}: no trade pushed for over a minute on {', '.join(quiet)}"}
        errors = [f"{item.ticker} ({item.error})" for item in self._tickers.values() if item.error]
        return {"state": "warn" if errors else "ok",
                "detail": f"watching {names} since {_local(self.started_at):%H:%M}" + (f"; failing: {', '.join(errors)}" if errors else "")}

    def stream(self) -> "AssistantStream":
        return AssistantStream(self)


class AssistantStream:
    """One page's live view: new alerts as they happen and the panel's state once a second."""

    STATE_SECONDS = 1.0

    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self._seq = assistant.seq
        self._state_at = 0.0
        self._running = assistant.running

    def poll(self, now: Optional[datetime] = None) -> List[Tuple[str, Dict[str, Any]]]:
        out: List[Tuple[str, Dict[str, Any]]] = []
        for event in self.assistant.events_after(self._seq):
            self._seq = event["seq"]
            out.append(("alert", event))
        clock = time.monotonic()
        running = self.assistant.running
        if (running or self._running) and (clock - self._state_at >= self.STATE_SECONDS or running != self._running):
            self._state_at, self._running = clock, running
            out.append(("state", self.assistant.state(now)))
        return out


def _watch_state(usage: Dict[str, Any], grade: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    from . import assistant_model, assistant_watch
    if not assistant_watch.watch_enabled():
        return None
    return {"order": assistant_model.describe("watch"), "calls_today": usage["watch_calls"], "cap": assistant_watch.watch_daily_cap(),
            "escalated_today": usage["escalated"], "analyst_per_hour": assistant_watch.analyst_per_hour(),
            "every_minute": assistant_watch.every_minute(), "event_seconds": assistant_watch.watch_gap().total_seconds(),
            "by": [{"label": label, "calls": calls} for label, calls in usage["watch_by"].items()], "grade": grade}


def _model_order() -> str:
    from . import assistant_model
    return assistant_model.describe("assistant")


def _daily_cap() -> int:
    import os
    try:
        return max(0, int(os.getenv("TRADE_ASSISTANT_DAILY_CALLS", "150")))
    except ValueError:
        return 150


def _note_gap() -> timedelta:
    import os
    try:
        return timedelta(minutes=max(0.5, float(os.getenv("TRADE_ASSISTANT_NOTE_MINUTES", "3"))))
    except ValueError:
        return timedelta(minutes=3)


def _plan_words(rec: Dict[str, Any]) -> str:
    """A plan's side and trigger in words, e.g. "short below 150" or "long close above 194.53 on a 5-minute bar"."""
    side = "long" if rec["direction"] == 1 else "short"
    trigger = rec["trigger"]
    when = "now" if trigger["kind"] == "now" else f"{trigger['kind'].replace('_', ' ')} {trigger['price']:g}" + (
        f" on a {trigger['bar_minutes']}-minute bar" if trigger["bar_minutes"] else "")
    return f"{side} {when}"


def _flag_key(flag: Dict[str, Any]) -> str:
    return f"{flag['setup']}:{flag['kind']}:{flag['box']}:{flag['direction']}"


def _regular(text: str) -> bool:
    from datetime import time as dtime
    end = _local(datetime.fromisoformat(text)).time()
    return dtime(9, 30) < end <= dtime(16, 0)


def _weight(row: Dict[str, Any]) -> str:
    weight = row.get("weight_pct")
    return f"{weight:.1f}%" if weight is not None else "—"


def settle_open_plans(repo: Any, provider: Any, now: datetime) -> int:
    """After the close: grades the day's plans and calls still waiting or open (a session stopped early, a
    restart) from the day's 1-minute bars; older ones that missed their day expire. Returns how many moved."""
    day = _local(now).date()
    _, closing = _session_bounds(day)
    rows = [row for row in repo.chart_trades(statuses=ag.WATCHED, limit=1000) if row.get("kind") in ("plan", "call")]
    changed = []
    bars_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for rec in rows:
        if rec.get("day") != day.isoformat():
            rec.update(status="expired", void="not graded on its day")
            changed.append(rec)
            continue
        if closing is None or now < closing:
            continue
        if rec["ticker"] not in bars_by_ticker:
            bars = provider.kline(f"US.{rec['ticker']}", "K_1M", 1000, push=False)
            bars_by_ticker[rec["ticker"]] = [bar for bar in bars if _regular(bar["time"])
                                             and _local(datetime.fromisoformat(bar["time"])).date() == day
                                             and datetime.fromisoformat(bar["time"]) <= closing]
        if ag.settle(rec, bars_by_ticker[rec["ticker"]], closing):
            changed.append(rec)
    if changed:
        repo.save_chart_trades(changed)
    return len(changed)

