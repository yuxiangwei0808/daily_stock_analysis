"""The Trade Desk chart: moomoo bars with the system's boxes, levels and flags (``GET /chart/{ticker}``).

Read only; nothing is traded. Bars come from ``get_cur_kline`` on a moomoo connection of the
chart's own, so its subscriptions never clash with the advice snapshots' (each trims what it does
not use) or with shadow mode's live feed. The chart keeps the subscriptions of its ``MAX_CHARTS``
most recent tickers and releases the rest, and any idle for ``IDLE``, at the next request or the
minute's sweep (OpenD holds a new subscription for at least a minute).

Live updates (``stream``): an open chart also subscribes the quote and its bars with push. The
pushes land in memory on the SDK's thread, as plain rows (no pandas table per push), and the page's
stream reads them from there: the forming bar, the last trade, and each 15-minute close, after
which the page reloads the snapshot so boxes and flags move only on completed bars.

Boxes, levels and flags always come from completed 15-minute regular-session bars, through
``boxes.py``, so the chart draws exactly the rule that was backtested:

- ``boxes``: every setup in the window (range and base box, bodies and wicks); ``alive`` while the
  next bar can still trade it.
- ``flags``: each setup's trades. ``armed`` is a limit the next bar fills at its price, ``watching``
  a break and retest waiting for a close past its price, ``open`` and ``closed`` a trade taken, with
  its R after cost by the backtest's exits. Each carries its backtest grade (``register.BOX_VARIANTS``); none has
  passed, so they are context, not suggestions.
- ``levels``: the value-area low and high of the two prior weeks, the prior session's high, low and
  close, and for a stock you hold, each position's plan stop and target and your price alerts.
- ``nx``: the NX tunnel lines on the chart's own bars, as moomoo draws them.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import boxes as bx
from . import register
from .backtest import boxes as bb
from .chart_grades import ChartGrades, Context
from .models import utcnow
from .moomoo_indicators import NX_MIN_BARS, nx
from .providers import _safe_float
from .shadow_exits import spread

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
INTERVALS = {"1m": "K_1M", "5m": "K_5M", "15m": "K_15M", "1d": "K_DAY"}
MAX_BARS = 1000  # OpenD's most per get_cur_kline
MAX_CHARTS = 5
IDLE = timedelta(minutes=10)
VARIANTS = {(1.0, False), (0.6, False), (1.0, True), (0.6, True)}
SWEEP_SECONDS = 60
TOUCH = timedelta(seconds=30)  # an open stream marks its ticker used this often
CLOSE_GRACE = timedelta(seconds=3)  # after a 15-minute bar's end, before the page reloads the snapshot
SILENT = timedelta(seconds=60)  # no push this long while a chart is open in the session: a warning
SAMPLES = 5000
GRADE_AT = dtime(16, 20)  # New York: the daily pass over stocks with trades still running
MAX_DAILY_CHARTS = 30
NOTE = ("Boxes and flags follow the backtested rules on completed 15-minute bars. No flag type has "
        "beaten the same trades at random times, so they are context, not suggestions.")


def grade(kind: str, impulse_t: float, tidy: bool) -> Dict[str, Any]:
    verdict, t_a, t_b = register.box_grade(kind, impulse_t, tidy)
    return {"verdict": verdict, "t": [t_a, t_b],
            "label": f"{verdict.capitalize()} in backtest (t {t_a:+.2f} / {t_b:+.2f} vs random timing)",
            "source": register.BOX_SOURCE}


def _local(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(_NEW_YORK)


def _regular(row: Dict[str, Any]) -> bool:
    """An intraday bar ending inside the regular session (09:30 to 16:00 New York)."""
    end = _local(row["time"]).time()
    return dtime(9, 30) < end <= dtime(16, 0)


def _session_open(now: datetime) -> bool:
    from data_provider.us_session import session_window
    return session_window(now)[0] == "regular"


def _next_bar_day(now: datetime) -> date:
    """The day of the next regular-session bar: today until the close, else the next weekday."""
    local = now.astimezone(_NEW_YORK)
    day = local.date()
    if local.time() >= dtime(16, 0) or day.weekday() >= 5:
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return day


def _validate(ticker: str, interval: str, impulse_t: float = 1.0, tidy: bool = False) -> str:
    ticker = str(ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", ticker):
        raise ValueError("Choose a US ticker")
    if interval not in INTERVALS:
        raise ValueError(f"Intervals are {', '.join(INTERVALS)}")
    if (impulse_t, tidy) not in VARIANTS:
        raise ValueError("Only the backtested boxes: impulse 1.0 or 0.6 T, normal or tidy")
    return ticker


def _push_time(text: Any) -> Optional[datetime]:
    """OpenD's US push times are New York time."""
    try:
        return datetime.fromisoformat(str(text).strip()).replace(tzinfo=_NEW_YORK)
    except ValueError:
        return None


class Charts:
    def __init__(self, service: Any, provider: Any = None):
        self.service = service
        self._provider = provider
        self._used: Dict[Tuple[str, str], datetime] = {}  # (code, subscription type) -> last use
        self._lock = threading.Lock()
        # Pushed state, written on the SDK's thread: the latest bar per (code, K-line type) and quote per code.
        self._bars: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._streams: Dict[str, int] = {}
        self.pushes = {"bar": 0, "quote": 0}
        self.lags: deque = deque(maxlen=SAMPLES)  # quote arrival minus its exchange time, seconds
        self.handler_ms: deque = deque(maxlen=SAMPLES)
        self.last_push: Optional[datetime] = None
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._sweeper: Optional[threading.Thread] = None
        self._graded_day: Optional[date] = None
        self._grading: Optional[threading.Thread] = None

    def _connection(self) -> Any:
        with self._lock:
            if self._provider is None:
                provider = self.service.provider_factory("live")
                listen = getattr(provider, "listen_bars", None)
                try:
                    if callable(listen):  # before any pushed subscription: the SDK's own handler is slow
                        listen(self._on_bar, self._on_quote)
                except Exception:
                    provider.close()
                    raise
                self._provider = provider
                self._stop.clear()
                self._sweeper = threading.Thread(target=self._sweep, name="chart-sweep", daemon=True)
                self._sweeper.start()
            return self._provider

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            provider, self._provider = self._provider, None
        if provider is not None:
            provider.close()

    def _sweep(self) -> None:
        """Releases idle subscriptions every minute, so a closed page frees them without a next request."""
        while not self._stop.wait(SWEEP_SECONDS):
            try:
                self._upkeep([], utcnow(), connect=False)
            except Exception as exc:  # the next sweep tries again
                logger.info("Chart subscription sweep: %s", type(exc).__name__)

    # -- pushes, on the SDK's callback thread -------------------------------------------------
    def _on_bar(self, row: Dict[str, Any]) -> None:
        started = time.thread_time()
        try:
            stamp = _push_time(row.get("time_key"))
            values = {name: _safe_float(row.get(name)) for name in ("open", "high", "low", "close")}
            if stamp is None or any(value is None for value in values.values()):
                return
            bar = {"time": stamp.isoformat(), **values, "volume": int(_safe_float(row.get("volume")) or 0)}
            with self._lock:
                self._seq += 1
                self._bars[(str(row.get("code") or ""), str(row.get("k_type") or ""))] = {**bar, "seq": self._seq}
                self.pushes["bar"] += 1
                self.last_push = utcnow()
                self.handler_ms.append((time.thread_time() - started) * 1000)
        except Exception as exc:  # the SDK's thread keeps delivering; Status shows this
            self.error = type(exc).__name__

    def _on_quote(self, row: Dict[str, Any]) -> None:
        started = time.thread_time()
        try:
            price = _safe_float(row.get("last_price"))
            if not price or price <= 0:
                return
            arrived = utcnow()
            data_at = _push_time(f"{row.get('data_date')} {row.get('data_time')}")
            with self._lock:
                self._seq += 1
                previous = self._quotes.get(str(row.get("code") or ""))
                self._quotes[str(row.get("code") or "")] = {
                    "price": price, "at": data_at.isoformat() if data_at else None, "seq": self._seq, "received": arrived}
                self.pushes["quote"] += 1
                self.last_push = arrived
                # The first push is only the last known state, and a quote without a new trade says nothing of delay.
                if previous and data_at and previous.get("at") != data_at.isoformat():
                    self.lags.append(max(0.0, (arrived - data_at).total_seconds()))
                self.handler_ms.append((time.thread_time() - started) * 1000)
        except Exception as exc:
            self.error = type(exc).__name__

    # -- forward grades (chart_grades.py) ------------------------------------------------------
    @property
    def grades(self) -> Optional[ChartGrades]:
        repo = getattr(self.service, "repo", None)
        return ChartGrades(repo) if repo is not None else None

    def _grades(self) -> ChartGrades:
        grades = self.grades
        if grades is None:
            raise ValueError("Grading needs the Trade Desk database")
        return grades

    def _last_price(self, code: str, now: datetime) -> float:
        """The last trade pushed in the past minute, else the latest 15-minute close."""
        with self._lock:
            quote = dict(self._quotes.get(code) or {})
        if quote.get("received") and now - quote["received"] <= SILENT:
            return float(quote["price"])
        rows = self._connection().kline(code, "K_15M", 1, push=True)
        if not rows:
            raise ValueError("No price for this stock yet")
        return float(rows[-1]["close"])

    def add_mark(self, ticker: str, direction: str, stop: float, target: float, note: str = "",
                 now: Optional[datetime] = None) -> Dict[str, Any]:
        """Your "I'd go long / short here": it enters at the next 15-minute bar's open and is graded like a flag."""
        ticker = _validate(ticker, "15m")
        now = now or utcnow()
        grades = self._grades()
        return _your_view(grades.add_mark(ticker, direction, stop, target, self._last_price(f"US.{ticker}", now), now, note))

    def add_box(self, ticker: str, top: float, bottom: float, note: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
        """A box you drew, kept: graded by the system's fade rule from now on."""
        ticker = _validate(ticker, "15m")
        return _your_view(self._grades().add_box(ticker, top, bottom, now or utcnow(), note))

    def remove_yours(self, record_id: str) -> bool:
        return self._grades().remove(record_id)

    def grade_table(self) -> Dict[str, Any]:
        return self._grades().summary()

    def grade_due(self, now: Optional[datetime] = None) -> None:
        """Once a day after the close, reads the charts of the stocks whose records are still moving.

        Runs on a thread of its own, so the minute's monitor goes on; records otherwise move only when a
        chart of their stock is read."""
        now = now or utcnow()
        local = now.astimezone(_NEW_YORK)
        grades = self.grades
        if grades is None or local.weekday() >= 5 or local.time() < GRADE_AT or self._graded_day == local.date():
            return
        if self._grading is not None and self._grading.is_alive():
            return
        self._graded_day = local.date()
        due = grades.unfinished()[:MAX_DAILY_CHARTS]

        def run():
            for ticker, impulse_t, tidy in due:
                try:
                    self.chart(ticker, "15m", impulse_t=impulse_t, tidy=tidy)
                except Exception as exc:  # the next day tries again
                    logger.info("Chart grades for %s: %s", ticker, type(exc).__name__)
        if due:
            self._grading = threading.Thread(target=run, name="chart-grades", daemon=True)
            self._grading.start()

    def pushed_bar(self, code: str, kind: str) -> Optional[Dict[str, Any]]:
        """The latest pushed bar of ``kind`` (``K_1M`` ..) for ``code``, with its ``seq``; None before the first push."""
        with self._lock:
            bar = self._bars.get((code, kind))
            return dict(bar) if bar else None

    def pushed_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """The last pushed trade for ``code``: ``price``, ``at`` (exchange time), ``received`` and ``seq``."""
        with self._lock:
            quote = self._quotes.get(code)
            return dict(quote) if quote else None

    def stream(self, ticker: str, interval: str = "15m") -> "ChartStream":
        """Subscribes the quote and the bars with push and returns what the page's stream reads."""
        ticker = _validate(ticker, interval)
        code = f"US.{ticker}"
        types = ["QUOTE", "K_15M"] + ([INTERVALS[interval]] if interval != "15m" else [])
        self._connection().watch_pushes(code, types)
        records = [(code, kind) for kind in types]
        with self._lock:
            self._streams[code] = self._streams.get(code, 0) + 1
        self._upkeep(records, utcnow())
        return ChartStream(self, code, interval, records)

    def _end_stream(self, code: str) -> None:
        with self._lock:
            self._streams[code] = self._streams.get(code, 1) - 1
            if self._streams[code] <= 0:
                del self._streams[code]

    def health(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """The Status page's line: open charts, pushes, delay and handler time."""
        now = now or utcnow()
        with self._lock:
            streams = sum(self._streams.values())
            tickers = len({code for code, _ in self._used})
            pushes, last = dict(self.pushes), self.last_push
            lag, cost = spread(self.lags), spread(self.handler_ms)
        if self.error:
            return {"state": "error", "detail": f"push handler failed: {self.error}"}
        if not streams:
            return {"state": "off", "detail": f"no chart open ({tickers} tickers still subscribed)" if tickers else "no chart open"}
        detail = (f"{streams} chart{'s' if streams != 1 else ''} open, {pushes['bar']} bar and {pushes['quote']} quote pushes"
                  + (f", quote delay median {lag['median']} s (p95 {lag['p95']})" if lag else "")
                  + (f", {cost['median']:.2f} ms per push" if cost else ""))
        if _session_open(now) and (last is None or now - last > SILENT):
            return {"state": "warn", "detail": f"no push for over {int(SILENT.total_seconds())} s; " + detail}
        return {"state": "ok", "detail": detail}

    def _upkeep(self, records: List[Tuple[str, str]], now: datetime, *, connect: bool = True) -> None:
        """Mark ``records`` used, then release what is beyond the newest ``MAX_CHARTS`` tickers or idle."""
        with self._lock:
            for record in records:
                self._used[record] = now
            latest: Dict[str, datetime] = {}
            for (code, _), when in self._used.items():
                latest[code] = max(when, latest.get(code, when))
            keep = set(sorted(latest, key=latest.get, reverse=True)[:MAX_CHARTS])
            self._used = {r: when for r, when in self._used.items() if r[0] in keep and now - when <= IDLE}
            wanted = set(self._used)
            provider = self._provider
        if provider is not None or connect:
            (provider or self._connection()).keep_subscriptions(wanted)

    def chart(self, ticker: str, interval: str = "15m", *, impulse_t: float = 1.0, tidy: bool = False,
              now: Optional[datetime] = None) -> Dict[str, Any]:
        ticker = _validate(ticker, interval, impulse_t, tidy)
        now = now or utcnow()
        code, subtype = f"US.{ticker}", INTERVALS[interval]
        self._upkeep(sorted({(code, "K_15M"), (code, subtype)}), now)
        provider = self._connection()
        # Subscribed with push, so the page's stream needs no second subscription.
        rows15 = provider.kline(code, "K_15M", MAX_BARS, push=True)
        shown = rows15 if subtype == "K_15M" else provider.kline(code, subtype, MAX_BARS, push=True)
        if subtype != "K_DAY":
            shown = [row for row in shown if _regular(row)]
        warnings: List[str] = []
        lines = nx(shown)
        if len(shown) < NX_MIN_BARS:
            warnings.append(f"nx_needs_{NX_MIN_BARS}_bars")
        out = {"ticker": ticker, "interval": interval, "as_of": now.isoformat(),
               "params": {"impulse_t": impulse_t, "tidy": tidy},
               "bars": [{**row, "start": _start(row, interval)} for row in shown],
               "nx": {name: [None if i < NX_MIN_BARS else value for i, value in enumerate(series)]
                      for name, series in lines.items()},
               "note": NOTE, "warnings": warnings}
        out.update(_overlays(rows15, bx.Params(impulse_t=impulse_t, tidy=tidy), now, warnings,
                             cut=len(rows15) >= MAX_BARS))
        grading = out.pop("_grading", None)
        out["yours"] = []
        grades = self.grades
        if grading is not None and grades is not None:
            try:
                out["yours"] = [_your_view(rec) for rec in grades.record(ticker, *grading)]
            except Exception as exc:  # the chart still shows; the records move at the next read
                logger.warning("Chart grades for %s: %s", ticker, type(exc).__name__)
                warnings.append("grades_unavailable")
        out["levels"] += self._your_levels(ticker)
        return out

    def _your_levels(self, ticker: str) -> List[Dict[str, Any]]:
        """Plan stops and targets of the positions on ``ticker`` and your price alerts on it."""
        from . import holdings, position_plans
        if not holdings.enabled():
            return []
        store = self.service.holdings
        rules = store.rules()
        out, yours = [], set()
        for item in position_plans.panel_rows(store.view(live=False), store.plans()["plans"], rules):
            if item["ticker"] != ticker:
                continue
            for kind in ("stop", "target"):
                if item.get(kind) is not None:
                    source = position_plans.note(item, kind)
                    out.append({"kind": kind, "price": item[kind], "position_key": item["key"],
                                "label": f"{item['label']} {kind}" + (f" ({source})" if source else "")})
                    if item.get(f"{kind}_source") == "you":
                        yours.add((item["key"], item[kind]))
        for rule in rules:  # an alert that already is a position's stop or target is drawn once, as that
            if rule.get("ticker") == ticker and rule.get("status") == "active" and \
                    rule.get("kind") in ("price_below", "price_above") and \
                    (rule.get("position_key"), rule["value"]) not in yours:
                out.append({"kind": "alert", "price": rule["value"], "rule_id": rule["id"],
                            "label": f"Your alert: {holdings.RULE_KINDS[rule['kind']]} {rule['value']:g}"
                                     + (f" ({rule['note']})" if rule.get("note") else "")})
        return out


class ChartStream:
    """One page's live view of a chart: what changed since its last read, from memory."""

    def __init__(self, charts: Charts, code: str, interval: str, records: List[Tuple[str, str]]):
        self.charts, self.code, self.interval, self.records = charts, code, interval, records
        self._kind = INTERVALS[interval]
        self._seen: Dict[Any, int] = {}
        # 15-minute bars seen forming, by end, until their close is signalled: OpenD pushes the next bar's
        # first trade well inside the grace, so the latest pushed bar has moved on when a close is due.
        self._ends: List[datetime] = []
        self._signalled: Optional[datetime] = None
        self._opened = self._touched = utcnow()
        self.closed = False

    def touch(self, now: Optional[datetime] = None) -> bool:
        """Keeps the subscriptions; False once the ticker was released (more than ``MAX_CHARTS`` charts)."""
        now = now or utcnow()
        self._touched = now
        with self.charts._lock:
            kept = all(record in self.charts._used for record in self.records)
        if kept:
            self.charts._upkeep(self.records, now)
        return kept

    def due(self, now: datetime) -> bool:
        return now - self._touched >= TOUCH

    def poll(self, now: Optional[datetime] = None) -> List[Tuple[str, Dict[str, Any]]]:
        """``bar`` (the shown interval's latest bar), ``quote`` and ``closed`` (a 15-minute bar ended)."""
        now = now or utcnow()
        out: List[Tuple[str, Dict[str, Any]]] = []
        with self.charts._lock:
            bar = self.charts._bars.get((self.code, self._kind))
            bar15 = self.charts._bars.get((self.code, "K_15M"))
            quote = self.charts._quotes.get(self.code)
        if bar and bar["seq"] != self._seen.get("bar") and (self.interval == "1d" or _regular(bar)):
            self._seen["bar"] = bar["seq"]
            row = {key: value for key, value in bar.items() if key != "seq"}
            end = datetime.fromisoformat(bar["time"])
            partial = end > now if self.interval != "1d" else _session_open(now)
            out.append(("bar", {"interval": self.interval, "bar": {**row, "start": _start(row, self.interval), "partial": partial}}))
        if quote and quote["seq"] != self._seen.get("quote"):
            self._seen["quote"] = quote["seq"]
            out.append(("quote", {"price": quote["price"], "at": quote["at"]}))
        if bar15 and _regular(bar15):
            end = datetime.fromisoformat(bar15["time"])
            if (self._signalled is None or end > self._signalled) and end not in self._ends:
                self._ends.append(end)
        for end in [end for end in self._ends if now >= end + CLOSE_GRACE]:
            self._ends.remove(end)
            self._signalled = end
            if end + CLOSE_GRACE > self._opened:  # one that ended before the page connected is in its snapshot
                out.append(("closed", {"bar_end": end.isoformat()}))
        return out

    def close(self) -> None:
        """Last use now: the sweep releases the subscriptions after ``IDLE`` unless the page comes back.

        No OpenD call, so the server can run it as the page disconnects."""
        if not self.closed:
            self.closed = True
            self.charts._end_stream(self.code)
            now = utcnow()
            with self.charts._lock:
                for record in self.records:
                    if record in self.charts._used:
                        self.charts._used[record] = now


def _p(price: Any) -> float:
    """A computed price for display (the rules ran on the unrounded values)."""
    return round(float(price), 4)


def _start(row: Dict[str, Any], interval: str) -> str:
    """A bar's start: moomoo stamps intraday bars at their end and daily bars at the date."""
    if interval == "1d":
        return row["time"]
    return (_local(row["time"]) - timedelta(minutes=int(interval[:-1]))).isoformat()


def _overlays(rows15: List[Dict[str, Any]], params: bx.Params, now: datetime, warnings: List[str], *,
              cut: bool) -> Dict[str, Any]:
    done = [row for row in rows15 if not row["partial"] and _regular(row)]
    if cut and done:  # the oldest session may be cut short, which would understate T
        first_day = _local(done[0]["time"]).date()
        done = [row for row in done if _local(row["time"]).date() != first_day]
    empty = {"boxes": [], "flags": [], "levels": [], "typical_range": None}
    if not done:
        warnings.append("no_completed_15m_bars")
        return empty
    bars = bx.Bars.from_rows([{**row, "time_key": _local(row["time"]).replace(tzinfo=None).isoformat(),
                               "volume": row.get("volume") or 0.0} for row in done])
    day = _next_bar_day(now)
    in_session = _session_open(now) and _local(done[-1]["time"]).date() == now.astimezone(_NEW_YORK).date()
    # Session indices: the next bar's, and the last one whose bars are all in.
    following = len(bars.days) - 1 if in_session else len(bars.days)
    last_complete = following - 1
    T = float(bars.T[-1])
    if math.isnan(T):  # fewer than t_sessions sessions before the last
        warnings.append(f"boxes_need_{params.t_sessions + 1}_sessions")
        empty["levels"] = _levels(bars, day)
        return empty
    n = len(bars)
    box_rows, flags = [], []
    system, live = [], []  # for the forward grades: every flag with its entry bar, and setups still pending
    for s in bx.setups(bars, params):
        alive = bool(following <= bars.sess[s.i] + params.life_sessions)
        setup_id = done[s.i]["time"]
        if alive:
            live.append((setup_id, s))
        for name, box, wick, part in (("range", s.range_box, s.range_own_wick, (s.i + 1, s.first - 1)),
                                      ("base", s.base_box, s.base_wick, (s.i - params.box_bars, s.i - 1))):
            if box is not None:
                box_rows.append({"setup": setup_id, "box": name, "bottom": _p(box[0]), "top": _p(box[1]),
                                 "wick_bottom": _p(wick[0]), "wick_top": _p(wick[1]), "up": s.up,
                                 "from": _start(done[part[0]], "15m"), "to": done[part[1]]["time"],
                                 "until": done[s.last]["time"], "alive": alive})
        for entry in bx.entries(bars, s, params):
            last = bars.session_end(entry.bar, params.life_sessions)
            result = bb.simulate(bars, entry.bar, entry.price, entry.stop, entry.target, entry.direction, last)
            if result is None:
                continue
            ended = bars.sess[entry.bar] + params.life_sessions <= last_complete
            state = "closed" if result["how"] != "time" or ended else "open"
            system.append((setup_id, state, entry, entry.bar))
            flags.append({**_flag(entry, setup_id, params), "state": state, "time": done[entry.bar]["time"],
                          "result": {"how": result["how"] if state == "closed" else None, "R": round(result["R"], 3),
                                     "exit": _p(result["exit"]) if state == "closed" else None,
                                     "exit_time": done[result["exit_bar"]]["time"] if state == "closed" else None}})
        for state, entry in bx.pending(bars, s, params, next_in_session=in_session):
            flags.append({**_flag(entry, setup_id, params), "state": state, "time": None, "result": None})
            system.append((setup_id, state, entry, None))
    order = {"armed": 0, "open": 1, "watching": 2, "closed": 3}
    flags.sort(key=lambda f: (order[f["state"]], -(datetime.fromisoformat(f["time"] or f["setup"]).timestamp())))
    return {"boxes": box_rows, "flags": flags, "levels": _levels(bars, day),
            "typical_range": {"T": _p(T), "impulse": _p(params.impulse_t * T), "bars_through": done[n - 1]["time"]},
            "_grading": (Context(bars, params, now, in_session, last_complete), system, live)}


_YOURS = ("id", "kind", "status", "price", "stop", "target", "top", "bottom", "armed", "entry_time", "exit_time", "how",
          "R", "twin_R", "marked_at", "kept_at", "reference", "note", "void")


def _your_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """A mark or box of yours as the page draws it."""
    view = {key: rec.get(key) for key in _YOURS if rec.get(key) is not None}
    if rec.get("direction") in (1, -1):
        view["direction"] = "long" if rec["direction"] > 0 else "short"
    return view


def _flag(entry: bx.Entry, setup_id: str, params: bx.Params) -> Dict[str, Any]:
    risk = abs(entry.price - entry.stop)
    return {"kind": entry.kind, "setup": setup_id, "box": entry.box,
            "direction": "long" if entry.direction > 0 else "short", "price": _p(entry.price),
            "stop": _p(entry.stop), "target": _p(entry.target),
            "reward_risk": round(abs(entry.target - entry.price) / risk, 2) if risk else None,
            "grade": grade(entry.kind, params.impulse_t, params.tidy)}


def _levels(bars: bx.Bars, day: date) -> List[Dict[str, Any]]:
    """The value areas of the two weeks before ``day``'s week and the last session before ``day``."""
    out = []
    areas = bx.prior_levels(bx.weekly_value_areas(bars), day)  # older week first
    for when, (val, vah) in zip(("2 weeks ago", "last week"), (areas[0:2], areas[2:4])) if areas else ():
        out += [{"kind": "val", "price": _p(val), "label": f"Value-area low, {when}"},
                {"kind": "vah", "price": _p(vah), "label": f"Value-area high, {when}"}]
    earlier = [k for k, d in enumerate(bars.days) if d.astype("datetime64[D]").item() < day]
    if earlier:
        k = earlier[-1]
        a, z = bars.s_first[k], bars.s_last[k]
        name = bars.days[k].astype("datetime64[D]").item().strftime("%b %-d")
        out += [{"kind": "prior_high", "price": _p(bars.h[a:z + 1].max()), "label": f"Prior day high ({name})"},
                {"kind": "prior_low", "price": _p(bars.l[a:z + 1].min()), "label": f"Prior day low ({name})"},
                {"kind": "prior_close", "price": _p(bars.c[z]), "label": f"Prior day close ({name})"}]
    return out
