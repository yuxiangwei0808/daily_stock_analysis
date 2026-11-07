"""0DTE quote recorder: near-the-money quotes of the nearest expiry, every 15 seconds of the session.

There are no historical option quotes (docs/trading-methodology.md §7 and §14), so 0DTE rules can't be
tested honestly until DSA has its own. From ``PREPARE`` before the open it reads each underlying's
nearest expiry and chain once a day, paced under OpenD's limit on chain requests. From the open to
``AFTER_CLOSE`` past the close (SPY and QQQ options trade 15 minutes longer) it takes a snapshot every
``RECORD_SECONDS``: each underlying, then the calls and puts of the ``2 x STRIKES`` strikes nearest its
price. ``get_market_snapshot`` needs no subscription and takes 400 codes a request.

Underlyings: ``OPTION_RECORDER_TICKERS`` (SPY and QQQ by default), then the option underlyings you hold
and the stocks of your trade journal, at most ``MAX_UNDERLYINGS``.

Files: ``<OPTION_RECORDER_DIR>/<day>/<TICKER>.csv.gz``, one row per code per snapshot (``right`` is S
for the underlying), appended as gzip members so a restart adds to the day's file. Read only: it never
subscribes, trades or writes the database. It runs on the server holding the monitor lease, on a
thread and a moomoo connection of its own.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .models import utcnow

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
RECORD_SECONDS = 15
STRIKES = 12  # each side of the price
MAX_UNDERLYINGS = 12
PREPARE = timedelta(minutes=10)
AFTER_CLOSE = timedelta(minutes=15)
CHAIN_PAUSE = 3.5  # seconds between chain reads: OpenD allows about 10 per 30 seconds
LATE = timedelta(seconds=60)  # no snapshot this long in the window: an error on the Status page
NAMES_EVERY = timedelta(minutes=10)  # a position opened during the day joins at the next look
RETRY = timedelta(minutes=5)  # a chain that failed to load
COLUMNS = ("ts", "code", "right", "strike", "expiry", "bid", "ask", "bid_size", "ask_size", "last", "volume",
           "open_interest", "iv", "delta", "gamma", "theta", "vega", "updated")
_FIELDS = {"bid": "bid_price", "ask": "ask_price", "bid_size": "bid_vol", "ask_size": "ask_vol", "last": "last_price",
           "volume": "volume", "open_interest": "option_open_interest", "iv": "option_implied_volatility",
           "delta": "option_delta", "gamma": "option_gamma", "theta": "option_theta", "vega": "option_vega",
           "updated": "update_time"}


def enabled() -> bool:
    return os.getenv("OPTION_RECORDER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def configured_tickers() -> List[str]:
    raw = os.getenv("OPTION_RECORDER_TICKERS", "SPY,QQQ")
    return list(dict.fromkeys(t.strip().upper() for t in raw.split(",") if t.strip()))


def directory() -> Path:
    path = os.getenv("OPTION_RECORDER_DIR", "").strip()
    if path:
        return Path(path)
    from src.config import get_config
    return Path(get_config().database_path).parent / "option_quotes"


def session_bounds(day: date) -> Tuple[Optional[datetime], Optional[datetime]]:
    """The regular session's open and close in New York, or (None, None) when the market is closed."""
    from src.core.trading_calendar import get_market_session_bounds
    return get_market_session_bounds("us", datetime.combine(day, datetime.min.time(), _NEW_YORK))


def _cell(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):  # numpy floats too, which print as np.float64(...) otherwise
        value = float(value)
        return "" if math.isnan(value) else repr(value)
    text = str(value)
    return "" if text in ("N/A", "nan") else text


def near_money(chain: List[Dict[str, Any]], price: float, strikes: int = STRIKES) -> List[Dict[str, Any]]:
    """The contracts of the ``2 x strikes`` strikes nearest ``price``, calls and puts."""
    nearest = set(sorted({c["strike"] for c in chain}, key=lambda strike: (abs(strike - price), strike))[:2 * strikes])
    return [c for c in chain if c["strike"] in nearest]


class _DayFiles:
    """One gzip file per underlying for the day; each write is its own member, flushed at once."""

    def __init__(self, root: Path, day: date):
        self.folder = root / day.isoformat()
        self.rows = 0

    def write(self, ticker: str, rows: List[List[str]]) -> None:
        if not rows:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{ticker}.csv.gz"
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        if not path.exists():
            writer.writerow(COLUMNS)
        writer.writerows(rows)
        with gzip.open(path, "at", newline="") as handle:
            handle.write(buffer.getvalue())
        self.rows += len(rows)


class OptionRecorder:
    def __init__(self, service: Any, *, leader: Optional[Callable[[], bool]] = None,
                 underlyings: Optional[Callable[[], List[str]]] = None, root: Optional[Path] = None,
                 clock: Callable[[], datetime] = utcnow, pause: float = CHAIN_PAUSE):
        self.service = service
        self._leader = leader or (lambda: True)
        self._underlyings = underlyings or self._default_underlyings
        self._root = root
        self._clock = clock
        self._pause = pause
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._provider: Any = None
        self._day: Optional[date] = None
        self._chains: Dict[str, Tuple[date, List[Dict[str, Any]]]] = {}
        self._failed: Dict[str, str] = {}
        self._retry_at: Dict[str, datetime] = {}
        self._no_options: set = set()
        self._names: List[str] = []
        self._names_at: Optional[datetime] = None
        self._files: Optional[_DayFiles] = None
        self.last_snapshot: Optional[datetime] = None
        self.error: Optional[str] = None

    # -- what to record ----------------------------------------------------------------------
    def _default_underlyings(self) -> List[str]:
        """Configured tickers, then option underlyings held, then the trade journal's stocks."""
        from .holdings import parse_code
        names = configured_tickers()
        holdings = getattr(self.service, "holdings", None)
        if holdings is not None:
            try:
                for code in holdings.codes():
                    info = parse_code(code)
                    if info["kind"] == "option":
                        names.append(info["underlying"])
            except Exception as exc:  # holdings are optional here
                logger.info("Option recorder: holdings unavailable (%s)", type(exc).__name__)
            try:
                journal = holdings.journal() or {}
                names += [row["ticker"] for row in journal.get("by_underlying") or [] if row.get("ticker")]
            except Exception as exc:
                logger.info("Option recorder: journal unavailable (%s)", type(exc).__name__)
        clean = [name.split(".")[-1].upper() for name in names if name]
        return list(dict.fromkeys(clean))[:MAX_UNDERLYINGS]

    # -- the thread ---------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="option-recorder", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._disconnect()

    def _loop(self) -> None:
        while not self._stop.wait(self._wait()):
            try:
                self.tick()
            except Exception as exc:  # the next snapshot tries again
                self.error = type(exc).__name__
                logger.warning("Option recorder: %s", self.error)

    def _wait(self) -> float:
        """Seconds to the next multiple of ``RECORD_SECONDS``, so snapshots fall on round times."""
        now = time.time()
        return RECORD_SECONDS - (now % RECORD_SECONDS) or RECORD_SECONDS

    def _disconnect(self) -> None:
        provider, self._provider = self._provider, None
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass

    def _connection(self) -> Any:
        if self._provider is None:
            provider = self.service.provider_factory("live")
            quiet = getattr(provider, "quiet_sdk_log", None)
            if callable(quiet):  # before the exit test's feed does it at 9:25
                quiet()
            self._provider = provider
        return self._provider

    # -- one step ------------------------------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or self._clock()
        day = now.astimezone(_NEW_YORK).date()
        opening, closing = session_bounds(day)
        if not self._leader() or opening is None or not (opening - PREPARE <= now < closing + AFTER_CLOSE):
            if self._provider is not None or self._day is not None:
                self._disconnect()
                self._day, self._files = None, None
            return
        if self._day != day:
            self._day, self._chains, self._failed, self._retry_at, self._no_options = day, {}, {}, {}, set()
            self._names_at = None
            self._files = _DayFiles(self._root or directory(), day)
        if self._names_at is None or now - self._names_at >= NAMES_EVERY:
            self._names, self._names_at = self._underlyings(), now
        self._prepare(now)
        if now >= opening:
            self._record(now)

    def _prepare(self, now: datetime) -> None:
        """Each underlying's nearest expiry and chain, read once a day; a failure is retried after ``RETRY``."""
        provider = self._connection()
        for ticker in self._names:
            if ticker in self._chains or ticker in self._no_options or self._stop.is_set() or \
                    now < self._retry_at.get(ticker, now):
                continue
            try:
                expiry, chain = provider.nearest_chain(f"US.{ticker}")
                if expiry is None or not chain:
                    self._no_options.add(ticker)  # a stock without listed options: not asked again today
                else:
                    self._chains[ticker] = (expiry, chain)
                    self._failed.pop(ticker, None)
            except Exception as exc:
                self._failed[ticker] = type(exc).__name__
                self._retry_at[ticker] = now + RETRY
            if self._pause:
                self._stop.wait(self._pause)

    def _record(self, now: datetime) -> None:
        if not self._chains:
            return
        provider = self._connection()
        stamp = f"{now.timestamp():.3f}"
        spots = {str(row.get("code") or ""): row for row in provider.snapshot_rows([f"US.{t}" for t in self._chains])}
        wanted: Dict[str, Tuple[str, Dict[str, Any], date]] = {}
        out: Dict[str, List[List[str]]] = {}
        for ticker, (expiry, chain) in self._chains.items():
            spot = spots.get(f"US.{ticker}")
            price = spot and spot.get("last_price")
            if not spot or not isinstance(price, (int, float)) or not price > 0:
                continue
            out[ticker] = [[stamp, f"US.{ticker}", "S", "", "", *(_cell(spot.get(_FIELDS[name])) for name in COLUMNS[5:])]]
            for contract in near_money(chain, float(price)):
                wanted[contract["code"]] = (ticker, contract, expiry)
        for row in provider.snapshot_rows(list(wanted)):
            code = str(row.get("code") or "")
            if code not in wanted:
                continue
            ticker, contract, expiry = wanted[code]
            out[ticker].append([stamp, code, "C" if contract["right"] == "call" else "P", _cell(contract["strike"]),
                                expiry.isoformat(), *(_cell(row.get(_FIELDS[name])) for name in COLUMNS[5:])])
        for ticker, rows in out.items():
            self._files.write(ticker, rows)
        self.last_snapshot = now
        self.error = None

    # -- the Status page -------------------------------------------------------------------------
    def health(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._clock()
        day = now.astimezone(_NEW_YORK).date()
        opening, closing = session_bounds(day)
        if opening is None or not (opening <= now < closing + AFTER_CLOSE):
            return {"state": "off", "detail": "records from the open to 15 minutes after the close on trading days"}
        if not self._leader():
            return {"state": "off", "detail": "runs on the server holding the monitor lease"}
        failed = f"; no chain for {', '.join(f'{t} ({why})' for t, why in sorted(self._failed.items()))}" if self._failed else ""
        rows = self._files.rows if self._files else 0
        if self.last_snapshot is None or now - self.last_snapshot > LATE:
            since = "no snapshot yet" if self.last_snapshot is None else \
                f"last snapshot {int((now - self.last_snapshot).total_seconds())} s ago"
            state = "error" if now - opening > LATE else "warn"
            return {"state": state, "detail": since + (f" ({self.error})" if self.error else "") + failed}
        return {"state": "warn" if self._failed else "ok",
                "detail": f"{len(self._chains)} underlyings, {rows} rows today, last snapshot "
                          f"{int((now - self.last_snapshot).total_seconds())} s ago{failed}"}
