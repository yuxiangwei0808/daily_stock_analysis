"""The Trade Desk chart: moomoo bars with the system's boxes, levels and flags (``GET /chart/{ticker}``).

Read only; nothing is traded. Bars come from ``get_cur_kline`` on a moomoo connection of the
chart's own, so its subscriptions never clash with the advice snapshots' (each trims what it does
not use) or with shadow mode's live feed. The chart keeps the subscriptions of its ``MAX_CHARTS``
most recent tickers and releases the rest, and any idle for ``IDLE``, at the next request (OpenD
holds a new subscription for at least a minute).

Boxes, levels and flags always come from completed 15-minute regular-session bars, through
``boxes.py``, so the chart draws exactly the rule that was backtested:

- ``boxes``: every setup in the window (range and base box, bodies and wicks); ``alive`` while the
  next bar can still trade it.
- ``flags``: each setup's trades. ``armed`` is a limit the next bar fills at its price, ``watching``
  a break and retest waiting for a close past its price, ``open`` and ``closed`` a trade taken, with
  its R after cost by the backtest's exits. Each carries its backtest grade (``GRADES``); none has
  passed, so they are context, not suggestions.
- ``levels``: the value-area low and high of the two prior weeks, the prior session's high, low and
  close, and for a stock you hold, each position's plan stop and target and your price alerts.
- ``nx``: the NX tunnel lines on the chart's own bars, as moomoo draws them.
"""
from __future__ import annotations

import math
import re
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import boxes as bx
from .backtest import boxes as bb
from .models import utcnow
from .moomoo_indicators import NX_MIN_BARS, nx

_NEW_YORK = ZoneInfo("America/New_York")
INTERVALS = {"1m": "K_1M", "5m": "K_5M", "15m": "K_15M", "1d": "K_DAY"}
MAX_BARS = 1000  # OpenD's most per get_cur_kline
MAX_CHARTS = 5
IDLE = timedelta(minutes=10)
VARIANTS = {(1.0, False), (0.6, False), (1.0, True), (0.6, True)}
# The 2026-11-05 backtest (docs/strategy-backtest.md): each trade against the same trade at random
# times; t by month in period A (2020-11 to 2023-11) and period B (to 2026-11).
GRADES = {
    ("break_retest", 1.0, False): ("rejected", -2.24, -2.90),
    ("fade", 1.0, False): ("no edge shown", -1.78, -0.90),
    ("break_retest", 0.6, False): ("rejected", -4.60, -3.57),
    ("fade", 0.6, False): ("rejected", -2.35, -1.10),
    ("break_retest", 1.0, True): ("no edge shown", -0.04, -0.11),
    ("fade", 1.0, True): ("no edge shown", -1.47, 0.26),
    ("break_retest", 0.6, True): ("rejected", -0.52, -0.65),
    ("fade", 0.6, True): ("rejected", -0.69, -0.49),
}
NOTE = ("Boxes and flags follow the backtested rules on completed 15-minute bars. No flag type has "
        "beaten the same trades at random times, so they are context, not suggestions.")


def grade(kind: str, impulse_t: float, tidy: bool) -> Dict[str, Any]:
    verdict, t_a, t_b = GRADES[(kind, impulse_t, tidy)]
    return {"verdict": verdict, "t": [t_a, t_b],
            "label": f"{verdict.capitalize()} in backtest (t {t_a:+.2f} / {t_b:+.2f} vs random timing)",
            "source": "docs/strategy-backtest.md, run of 2026-11-05"}


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


class Charts:
    def __init__(self, service: Any, provider: Any = None):
        self.service = service
        self._provider = provider
        self._used: Dict[Tuple[str, str], datetime] = {}  # (code, K-line type) -> last request
        self._lock = threading.Lock()

    def _connection(self) -> Any:
        with self._lock:
            if self._provider is None:
                self._provider = self.service.provider_factory("live")
            return self._provider

    def close(self) -> None:
        with self._lock:
            provider, self._provider = self._provider, None
        if provider is not None:
            provider.close()

    def _upkeep(self, records: List[Tuple[str, str]], now: datetime) -> None:
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
        self._connection().keep_subscriptions(wanted)

    def chart(self, ticker: str, interval: str = "15m", *, impulse_t: float = 1.0, tidy: bool = False,
              now: Optional[datetime] = None) -> Dict[str, Any]:
        ticker = str(ticker or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", ticker):
            raise ValueError("Choose a US ticker")
        if interval not in INTERVALS:
            raise ValueError(f"Intervals are {', '.join(INTERVALS)}")
        if (impulse_t, tidy) not in VARIANTS:
            raise ValueError("Only the backtested boxes: impulse 1.0 or 0.6 T, normal or tidy")
        now = now or utcnow()
        code, subtype = f"US.{ticker}", INTERVALS[interval]
        self._upkeep(sorted({(code, "K_15M"), (code, subtype)}), now)
        provider = self._connection()
        rows15 = provider.kline(code, "K_15M", MAX_BARS)
        shown = rows15 if subtype == "K_15M" else provider.kline(code, subtype, MAX_BARS)
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
    for s in bx.setups(bars, params):
        alive = bool(following <= bars.sess[s.i] + params.life_sessions)
        setup_id = done[s.i]["time"]
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
            flags.append({**_flag(entry, setup_id, params), "state": state, "time": done[entry.bar]["time"],
                          "result": {"how": result["how"] if state == "closed" else None, "R": round(result["R"], 3),
                                     "exit": _p(result["exit"]) if state == "closed" else None,
                                     "exit_time": done[result["exit_bar"]]["time"] if state == "closed" else None}})
        for state, entry in bx.pending(bars, s, params, next_in_session=in_session):
            flags.append({**_flag(entry, setup_id, params), "state": state, "time": None, "result": None})
    order = {"armed": 0, "open": 1, "watching": 2, "closed": 3}
    flags.sort(key=lambda f: (order[f["state"]], -(datetime.fromisoformat(f["time"] or f["setup"]).timestamp())))
    return {"boxes": box_rows, "flags": flags, "levels": _levels(bars, day),
            "typical_range": {"T": _p(T), "impulse": _p(params.impulse_t * T), "bars_through": done[n - 1]["time"]}}


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
