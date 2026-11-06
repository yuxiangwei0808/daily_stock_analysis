"""The trading assistant's session: one to three stocks you start, watched in real time until you stop.

Phase 2 of docs/trading-assistant-plan.md. Nothing runs before **Start**; **Stop**, or 15 minutes after
the close, ends it. Everything here is computed, never written by a model: the day's levels, your
positions on the stocks, the option contracts around the price and the alerts. The model's notes
come later and never hold up an alert. Read only: DSA never sends, changes or cancels an order.

Two threads, like shadow mode (``shadow_watch.py``):

- **check**, every ``CHECK_SECONDS``, reads only memory: the chart service's pushed trades and 1- and
  15-minute bars (``chart.py``). It alerts when the price crosses a level, notes a 15-minute close and
  warns before the close on a 0DTE position.
- **io** reads OpenD and the database: each stock's chart (levels and flags) at the start and after
  every 15-minute close, today's 1-minute bars once, the contract panel's quotes every
  ``CONTRACT_SECONDS`` and your positions every ``POSITION_SECONDS``.

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
PANEL_STRIKES = 5  # each side of the price
REPEAT = timedelta(minutes=15)  # one alert per level and direction in this long
PNL_STEP = {"option": 10.0, "stock": 2.0}  # an alert each time P&L on cost crosses a multiple of this
CLOSE_WARNINGS = (30, 15)  # minutes before the close, for a position expiring today
EVENTS = 300
SILENT = timedelta(seconds=60)
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
        self.bar_seq: Optional[int] = None
        self.premarket: Tuple[Optional[float], Optional[float]] = (None, None)
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
        self.pnl_steps: Dict[str, int] = {}
        self.pending15: List[datetime] = []  # 15-minute bars seen forming, by end, until their close is handled
        self.closed_through: Optional[datetime] = None
        self.reload_at: Optional[datetime] = None
        self.loaded = False
        self.warned: set = set()
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
            self._next = {}
        for item in self._tickers.values():
            item.stream = self.charts.stream(item.ticker, "1m")  # quote, 1- and 15-minute bars, with push
        self._event(None, "session", f"Watching {', '.join(names)}", f"{HORIZONS[horizon]}; alerts on this page", now)
        if threads:
            self._stop.clear()
            self._threads = [threading.Thread(target=self._loop, args=(self.check, CHECK_SECONDS), name="assistant-check", daemon=True),
                             threading.Thread(target=self._loop, args=(self.io, IO_SECONDS), name="assistant-io", daemon=True)]
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
            for item in self._tickers.values():
                if item.stream is not None:
                    item.stream.close()
            self._event(None, "session", "Stopped", reason, now)
            with self._lock:
                self.stopped = {"at": now.isoformat(), "reason": reason}
                self.started_at = None
        return self.state(now)

    def close(self) -> None:
        if self.running:
            self.stop("the server stopped")

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
    def _event(self, ticker: Optional[str], kind: str, title: str, detail: str, now: datetime, **extra: Any) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "at": now.isoformat(), "ticker": ticker, "kind": kind,
                                 "title": title, "detail": detail, **extra})

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
        _, closing = _session_bounds(_local(now).date())
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
            quote = self.charts.pushed_quote(item.code)
            if quote and quote.get("seq") != item.quote_seq:
                item.quote_seq = quote["seq"]
                self._price(item, float(quote["price"]), quote.get("at"), quote.get("received") or now, now)
            if closing is not None:
                self._close_warnings(item, now, closing)

    def _price(self, item: _Ticker, price: float, at: Optional[str], received: datetime, now: datetime) -> None:
        with self._lock:
            item.price, item.price_at, item.received = price, at, received
            levels = item.levels()
        for level in levels:
            side = 1 if price > level["price"] else -1 if price < level["price"] else 0
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
        for item in list(self._tickers.values()):
            try:
                if not item.loaded:
                    self._load(item, now)
                elif item.reload_at is not None and now >= item.reload_at:
                    item.reload_at = None
                    self._load_chart(item, now, alert=True)
                item.error = None
            except Exception as exc:
                item.error = type(exc).__name__
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

    def _load(self, item: _Ticker, now: datetime) -> None:
        self._load_chart(item, now, alert=False)
        provider = self._provider()
        day = _local(now).date()
        rows = provider.kline(item.code, "K_1M", 400, push=True)
        bars = [row for row in rows if _regular(row["time"]) and _local(datetime.fromisoformat(row["time"])).date() == day]
        snap = (provider.snapshot_rows([item.code]) or [{}])[0]
        with self._lock:
            pushed = [bar for bar in item.bars if not bars or bar["time"] > bars[-1]["time"]]
            item.bars = [{**{k: bar[k] for k in ("time", "open", "high", "low", "close", "volume")}, "partial": bar.get("partial", False)}
                         for bar in bars] + pushed
            item.premarket = (_num(snap.get("pre_high_price")), _num(snap.get("pre_low_price")))
            item.prev_close = _num(snap.get("prev_close_price"))
            if item.price is None and _num(snap.get("last_price")):
                item.price = _num(snap.get("last_price"))
        self._load_chain(item, provider)
        item.loaded = True

    def _load_chart(self, item: _Ticker, now: datetime, *, alert: bool) -> None:
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
        with self._lock:
            item.chart_levels = list(snapshot.get("levels") or [])
            item.flags, item.flag_states = flags, states

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
                 "in_30m": round(black_scholes_price(level["price"], strike, max(minutes - 30, 0) / MINUTES_A_YEAR, sigma,
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
                pnl, step = row.get("pnl_pct"), PNL_STEP.get(row.get("type"), PNL_STEP["stock"])
                if pnl is None:
                    continue
                level = int(pnl // step) if pnl >= 0 else -int(-pnl // step)
                before = item.pnl_steps.get(key)
                item.pnl_steps[key] = level
                if item.held is not None and before is not None and level != before and level != 0:
                    self._event(item.ticker, "pnl", f"{row['label']} {pnl:+.1f}% on cost",
                                f"{_weight(row)} of the account", now)
            with self._lock:
                item.positions, item.held = mine, current

    # -- what the page shows ---------------------------------------------------------------------------
    def state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._clock()
        with self._lock:
            tickers = []
            for item in self._tickers.values():
                levels = item.levels()
                price = item.price
                tickers.append({
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
                })
            return {"running": self.running, "horizon": self.horizon, "horizons": HORIZONS,
                    "started_at": self.started_at.isoformat() if self.started_at else None, "stopped": self.stopped,
                    "max_tickers": MAX_TICKERS, "tickers": tickers, "events": [dict(e) for e in list(self._events)[-60:]][::-1],
                    "seq": self._seq,
                    "note": "Levels and contract values are computed; none of these levels has passed a test. "
                            "Contract values at a level are Black-Scholes estimates at the quoted implied volatility. "
                            "Nothing is sent to moomoo."}

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


def _flag_key(flag: Dict[str, Any]) -> str:
    return f"{flag['setup']}:{flag['kind']}:{flag['box']}:{flag['direction']}"


def _regular(text: str) -> bool:
    from datetime import time as dtime
    end = _local(datetime.fromisoformat(text)).time()
    return dtime(9, 30) < end <= dtime(16, 0)


def _weight(row: Dict[str, Any]) -> str:
    weight = row.get("weight_pct")
    return f"{weight:.1f}%" if weight is not None else "—"
