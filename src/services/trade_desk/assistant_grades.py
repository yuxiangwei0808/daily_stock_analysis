"""The plans you type and the assistant's calls: checked, watched live, then graded (research only).

Phases 3 and 4 of docs/trading-assistant-plan.md. A plan or call is a same-day trade on one stock:

- ``trigger``: ``now``, ``above`` or ``below`` a price (the first trade there), or ``close_above`` /
  ``close_below`` a price on a completed 1-, 5- or 15-minute bar;
- ``invalidation`` (optional): a price that cancels it if traded before the trigger;
- ``stop`` and ``target`` on the stock, and ``time_stop`` (``HH:MM`` New York, by default the close).

The session (assistant.py) watches it on the pushed trades and alerts on each step. The grade comes
after the session, from that day's 1-minute bars so that it and its twins are measured alike: from the
entry (the trade that triggered it) to the stop or target, whichever a bar reaches first (the stop when
both; at a bar's open when it gaps past; in the entry bar only the stop counts), else the last trade
before the time stop, in R after ``COST`` per round trip. Its twins are the same trade at ``N_TWINS``
random minutes of the same session, entered at a minute's open, with the same stop and target distances
in % and the same time stop: they differ only in the time. Clusters are days (methodology §6), so for
these kinds the grade table's ``months`` count days. Records live in ``trade_desk_chart_trades``.

The assistant's calls carry an ``action``. ``enter`` is a new trade and ``add`` more of a held position on
its side; both are the trade above, graded the same way. ``trim`` and ``exit`` (kind ``exit``) sell part or
all of a held position when the trigger trades, and have no stop or target: their ``time_stop`` is how long
the call stands. The rule for grading them, fixed here before any result (methodology §3, §9, §15):

- **The sale** is the trade that triggered it. **Holding** is the last trade before the time stop (by
  default the close; for a 0DTE contract, its expiry). Per unit and on the stock (for options an estimate,
  §14): ``saved_pct = -side x (holding - sale) / sale x 100`` in % of the sale price, where ``side`` is +1
  for a long position and -1 for a short one. Positive: selling there beat holding. Selling now or at the
  time stop costs one sale either way, so no cost is taken off.
- **Twins:** the same sale at ``N_TWINS`` random minutes of the session (at a minute's open, from 09:35 to 5
  minutes before the time stop), held to the same end: they differ only in the time.
- **The bar** is section 6's, with % for R: the mean edge over the twins at the day-clustered critical t,
  and a positive mean ``saved_pct`` (the sales beat holding), after 30 graded calls.
- A trim and an exit grade alike per unit; how much is sold is the trader's choice and is never recorded.
"""
from __future__ import annotations

import math
import re
import uuid
import zlib
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from .backtest import boxes as bb

_NEW_YORK = ZoneInfo("America/New_York")
COST = bb.COST
N_TWINS = 20
FIRST_ENTRY = dtime(9, 35)  # twins enter from here, after the opening print
LAST_ENTRY_GAP = timedelta(minutes=5)  # and no later than this before the time stop
TRIGGERS = ("now", "above", "below", "close_above", "close_below")
BAR_MINUTES = (1, 5, 15)
WATCHED = ("waiting", "open")
ACTIONS = ("enter", "add", "trim", "exit")
SALES = ("trim", "exit")  # kind "exit": graded against holding, not by a stop and a target


def _local(when: datetime) -> datetime:
    return when.astimezone(_NEW_YORK)


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(float(value), 4)


def _time(text: Any) -> Optional[dtime]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return dtime(hour, minute) if 9 <= hour <= 16 and minute < 60 else None


def check_plan(fields: Dict[str, Any], price: float, closing: datetime, now: datetime) -> Dict[str, Any]:
    """A plan's fields, cleaned and checked against the last price; ValueError says what is wrong."""
    direction = str(fields.get("direction") or "").lower()
    if direction not in ("long", "short"):
        raise ValueError("Say long or short")
    d = 1 if direction == "long" else -1
    trigger = _trigger(fields)
    kind, level = trigger["kind"], trigger["price"]
    stop, target = _num(fields.get("stop")), _num(fields.get("target"))
    if not stop or not target:
        raise ValueError("A plan needs a stop and a target")
    entry = price if kind == "now" else level
    if (entry - stop) * d <= 0 or (target - entry) * d <= 0:
        side = ("below", "above") if d > 0 else ("above", "below")
        raise ValueError(f"For a {direction} plan the stop goes {side[0]} the entry ({entry:g}) and the target {side[1]} it")
    invalidation = _num(fields.get("invalidation"))
    if invalidation is not None and kind != "now" and (level - invalidation) * d <= 0:
        raise ValueError("The invalidation must be on the stop's side of the trigger")
    instrument = str(fields.get("instrument") or "").lower() or None
    return {"direction": d, "trigger": trigger, "invalidation": invalidation,
            "stop": stop, "target": target, "time_stop": _time_stop(fields, closing, now),
            "instrument": instrument if instrument in ("calls", "puts", "shares") else None}


def check_sale(fields: Dict[str, Any], position: Dict[str, Any], closing: datetime, now: datetime) -> Dict[str, Any]:
    """A trim or exit call on a held position (``key``, ``label``, ``exposure``), cleaned; ValueError says what is wrong."""
    action = str(fields.get("action") or "").lower()
    if action not in SALES:
        raise ValueError("A sale is a trim or an exit")
    side = {"long": 1, "short": -1}.get(str(position.get("exposure") or ""))
    if side is None:
        raise ValueError(f"{position.get('label')} is neither long nor short, so a sale has no side to grade")
    return {"action": action, "direction": side, "trigger": _trigger(fields), "invalidation": None, "stop": None,
            "target": None, "time_stop": _time_stop(fields, closing, now), "instrument": None,
            "position": position["label"], "position_key": position["key"]}


def _trigger(fields: Dict[str, Any]) -> Dict[str, Any]:
    trigger = dict(fields.get("trigger") or {})
    kind = str(trigger.get("kind") or "now").lower()
    if kind not in TRIGGERS:
        raise ValueError(f"The trigger is one of {', '.join(TRIGGERS)}")
    level = None if kind == "now" else _num(trigger.get("price"))
    if kind != "now" and not level:
        raise ValueError("The trigger needs a price")
    minutes = int(trigger.get("bar_minutes") or 5) if kind.startswith("close_") else None
    if minutes is not None and minutes not in BAR_MINUTES:
        raise ValueError("A close trigger uses 1-, 5- or 15-minute bars")
    return {"kind": kind, "price": level, "bar_minutes": minutes}


def _time_stop(fields: Dict[str, Any], closing: datetime, now: datetime) -> str:
    stop_at = _time(fields.get("time_stop")) if fields.get("time_stop") else _local(closing).time()
    if stop_at is None:
        raise ValueError("The time stop is HH:MM, New York time, within the session")
    if datetime.combine(_local(now).date(), stop_at, _NEW_YORK) <= now:
        raise ValueError("The time stop has already passed")
    return stop_at.strftime("%H:%M")


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def new_record(ticker: str, source: str, plan: Dict[str, Any], price: float, now: datetime, *, text: str = "",
               why: str = "", model: str = "") -> Dict[str, Any]:
    kind = "plan" if source == "you" else "exit" if plan.get("action") in SALES else "call"
    rec = {"id": f"{source}|{ticker}|{kind}|{uuid.uuid4().hex[:12]}", "ticker": ticker, "status": "waiting",
           "source": source, "kind": kind, **plan, "day": _local(now).date().isoformat(), "created_at": now.isoformat(),
           "reference": price, "text": text[:500], "why": why[:500], "model": model}
    if plan["trigger"]["kind"] == "now":
        rec.update(status="open", entry_time=now.isoformat(), price=round(price, 4))
    return rec


def _bucket_close(bars: List[Dict[str, Any]], minutes: int) -> Optional[Tuple[str, float]]:
    """The last completed ``minutes`` bar of the session as ``(end time, close)``, from 1-minute bars."""
    done = [bar for bar in bars if not bar.get("partial")]
    if not done:
        return None
    last = done[-1]
    end = _local(datetime.fromisoformat(last["time"]))
    since_open = (end.hour * 60 + end.minute) - (9 * 60 + 30)
    if since_open <= 0 or since_open % minutes:
        return None
    return last["time"], float(last["close"])


def step(rec: Dict[str, Any], price: float, bars: List[Dict[str, Any]], now: datetime) -> Optional[str]:
    """Moves a watched record on the last trade; returns what happened (for the alert), or None. A sale's
    ``measured`` (its time stop) is kept without an alert."""
    d, sale = rec["direction"], rec.get("kind") == "exit"
    stop_at = datetime.combine(_local(now).date(), _time(rec["time_stop"]), _NEW_YORK)
    if rec["status"] == "waiting":
        if now >= stop_at:
            rec.update(status="expired")
            return "expired"
        inval = rec.get("invalidation")
        if inval is not None and (price - inval) * d <= 0:
            rec.update(status="void", void="invalidated before it triggered", void_time=now.isoformat())
            return "void"
        trigger = rec["trigger"]
        kind, level = trigger["kind"], trigger["price"]
        hit = (kind == "above" and price >= level) or (kind == "below" and price <= level)
        if kind.startswith("close_"):
            closed = _bucket_close(bars, trigger["bar_minutes"])
            if closed and closed[0] != rec.get("checked_close"):
                rec["checked_close"] = closed[0]
                hit = closed[1] > level if kind == "close_above" else closed[1] < level
        if not hit:
            return None
        if not sale and ((price - rec["stop"]) * d <= 0 or (rec["target"] - price) * d <= 0):
            rec.update(status="void", void="triggered past its stop or target", void_time=now.isoformat())
            return "void"
        rec.update(status="open", entry_time=now.isoformat(), price=round(price, 4))
        return "triggered"
    if rec["status"] == "open" and not rec.get("live_exit") and sale:
        if now >= stop_at:  # the end the sale is graded against: kept, with no alert
            rec["live_exit"] = {"how": "held", "price": price, "time": now.isoformat()}
            return "measured"
        return None
    if rec["status"] == "open" and not rec.get("live_exit"):
        if (price - rec["stop"]) * d <= 0:
            rec["live_exit"] = {"how": "stop", "price": price, "time": now.isoformat()}
            return "stop"
        if (price - rec["target"]) * d >= 0:
            rec["live_exit"] = {"how": "target", "price": price, "time": now.isoformat()}
            return "target"
        if now >= stop_at:
            rec["live_exit"] = {"how": "time", "price": price, "time": now.isoformat()}
            return "time"
    return None


def _bar_index(bars: List[Dict[str, Any]], when: datetime) -> Optional[int]:
    """The 1-minute bar that was forming at ``when`` (bars end at their ``time``)."""
    for k, bar in enumerate(bars):
        if datetime.fromisoformat(bar["time"]) > when:
            return k
    return None


def _last_bar(bars: List[Dict[str, Any]], stop_at: datetime) -> int:
    last = 0
    for k, bar in enumerate(bars):
        if datetime.fromisoformat(bar["time"]) <= stop_at:
            last = k
    return last


def simulate(bars: List[Dict[str, Any]], k: int, entry: float, stop: float, target: float, d: int,
             last: int) -> Tuple[float, str, int]:
    """``(exit price, how, exit bar)`` from bar ``k`` (entered during it) to bar ``last``."""
    for j in range(k, last + 1):
        bar = bars[j]
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        if j > k:
            if (o - stop) * d <= 0:
                return o, "stop", j
            if (o - target) * d >= 0:
                return o, "target", j
        if (l if d > 0 else h) * d <= stop * d:
            return stop, "stop", j
        if j > k and (h if d > 0 else l) * d >= target * d:
            return target, "target", j
    return float(bars[last]["close"]), "time", last


def r_after_cost(entry: float, exit_price: float, stop: float, d: int) -> float:
    risk = abs(entry - stop)
    return ((exit_price - entry) * d - COST * entry) / risk if risk else float("nan")


def settle(rec: Dict[str, Any], bars: List[Dict[str, Any]], closing: datetime) -> bool:
    """Grades a record of the day from that day's complete 1-minute bars; True when it changed."""
    if rec["status"] == "waiting":
        rec["status"] = "expired"  # the day ended before it triggered
        return True
    if rec["status"] != "open" or not bars:
        return False
    d = rec["direction"]
    stop_at = min(datetime.combine(date.fromisoformat(rec["day"]), _time(rec["time_stop"]), _NEW_YORK), closing)
    k = _bar_index(bars, datetime.fromisoformat(rec["entry_time"]))
    last = _last_bar(bars, stop_at)
    if k is None or k > last:
        rec.update(status="expired", void="no bars after the sale" if rec.get("kind") == "exit" else "no bars after the entry")
        return True
    if rec.get("kind") == "exit":
        held = float(bars[last]["close"])
        rec.update(status="closed", how="held", exit=round(held, 4), exit_time=bars[last]["time"], month=rec["day"],
                   saved_pct=_r(saved(rec["price"], held, d)))
        rec["twin_saved_pct"] = _r(sale_twins(bars, rec, stop_at))
        rec["twin_none"] = rec["twin_saved_pct"] is None
        return True
    entry = rec["price"]
    exit_price, how, j = simulate(bars, k, entry, rec["stop"], rec["target"], d, last)
    rec.update(status="closed", how=how, exit=round(exit_price, 4), exit_time=bars[j]["time"],
               R=_r(r_after_cost(entry, exit_price, rec["stop"], d)),
               ret_pct=_r(((exit_price - entry) * d / entry - COST) * 100), month=rec["day"])
    rec["twin_R"] = _r(twins(bars, rec, stop_at))
    rec["twin_none"] = rec["twin_R"] is None
    return True


def _twin_starts(bars: List[Dict[str, Any]], rec: Dict[str, Any], stop_at: datetime) -> List[int]:
    """Up to ``N_TWINS`` random minutes of the session to enter (or sell) at, seeded by the record's id."""
    day = date.fromisoformat(rec["day"])
    first = datetime.combine(day, FIRST_ENTRY, _NEW_YORK)
    last = _last_bar(bars, stop_at)
    starts = [k for k in range(len(bars))
              if first <= datetime.fromisoformat(bars[k]["time"]) - timedelta(minutes=1) <= stop_at - LAST_ENTRY_GAP and k < last]
    if not starts:
        return []
    rng = np.random.default_rng(zlib.crc32(rec["id"].encode()))
    return [int(k) for k in rng.choice(starts, size=min(N_TWINS, len(starts)), replace=False)]


def twins(bars: List[Dict[str, Any]], rec: Dict[str, Any], stop_at: datetime) -> Optional[float]:
    """The mean R of the same trade entered at random minutes of the session (same distances, same time stop)."""
    last = _last_bar(bars, stop_at)
    d, entry = rec["direction"], rec["price"]
    stop_pct, target_pct = abs(rec["stop"] - entry) / entry, abs(rec["target"] - entry) / entry
    out = []
    for k in _twin_starts(bars, rec, stop_at):
        price = float(bars[k]["open"])
        stop, target = price * (1 - d * stop_pct), price * (1 + d * target_pct)
        exit_price, _, _ = simulate(bars, k, price, stop, target, d, last)
        out.append(r_after_cost(price, exit_price, stop, d))
    return float(np.mean(out)) if out else None


def saved(sale: float, held: float, side: int) -> float:
    """What selling a position on ``side`` (+1 long, -1 short) at ``sale`` saved against holding to ``held``, in %."""
    return -side * (held - sale) / sale * 100


def sale_twins(bars: List[Dict[str, Any]], rec: Dict[str, Any], stop_at: datetime) -> Optional[float]:
    """The mean ``saved`` of the same sale at random minutes of the session, held to the same end."""
    held = float(bars[_last_bar(bars, stop_at)]["close"])
    out = [saved(float(bars[k]["open"]), held, rec["direction"]) for k in _twin_starts(bars, rec, stop_at)]
    return float(np.mean(out)) if out else None


def view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """A plan or call as the page shows it."""
    keep = ("id", "ticker", "status", "source", "kind", "action", "position", "trigger", "invalidation", "stop", "target",
            "time_stop", "instrument", "price", "entry_time", "live_exit", "how", "exit", "exit_time", "R", "twin_R",
            "saved_pct", "twin_saved_pct", "void", "text", "why", "model", "created_at", "day")
    out = {key: rec.get(key) for key in keep if rec.get(key) is not None}
    out["direction"] = "long" if rec.get("direction") == 1 else "short"
    return out
