"""Shadow mode's three tiers, after docs/trading-methodology.md.

1. **Code**, every check (assistant.py): levels, flags, positions, P&L and plans, alerted at once and never waiting
   for a model. This module adds the facts the models get, computed only from completed bars (section 2: a cross
   is a trade past the level's band, a break is a completed 1- or 5-minute bar closing past it; nothing looks
   ahead), each level with its register verdict (section 11) and today's in-play rank, the one rule that has
   passed, as a range selector and never a direction (section 13).
2. **The watcher** (``TRADE_ASSISTANT_WATCH_MODELS``, a fast model with the 1-minute picture,
   ``TRADE_ASSISTANT_WATCH_FRAMES``), on its own thread so the analyst never holds it up, reads each stock once
   for every completed 1-minute bar of the regular session (``TRADE_ASSISTANT_WATCH_EVERY_MINUTE``): the bar is
   when its facts change. A market event (a level, a flag, a P&L step) is read at once, at most every
   ``TRADE_ASSISTANT_WATCH_SECONDS`` a stock. It writes the short note and says whether the analyst should look
   now; a minute read that doesn't escalate only replaces the stock's latest read, so the page's notes stay the
   events and the analyst.
3. **The analyst** (``TRADE_ASSISTANT_MODELS`` with every picture) answers your questions and, without waiting
   for the watcher, your plans', positions' and expiries' events and the first look at a stock; and what the
   watcher escalates, at most ``TRADE_ASSISTANT_ANALYST_PER_HOUR`` escalations an hour.

The watcher's escalations are a model's choice: they can't be replayed, so they are tracked forward with a fixed
rule written here before any result (sections 9, 13, 15). After the close each decision gets the stock's range
over the next ``FORWARD_MINUTES`` completed minutes, as % of the price at the decision; on each stock-day with
both kinds, escalated decisions are compared with the others (twins: same stock, same day, other times), and the
day differences give a day-clustered t. Too early before ``MIN_DAYS`` graded days. It grades whether the watcher
picks moments that move, not which way.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from . import assistant_notes

ALWAYS = ("position", "plan", "close_soon", "session")  # yours, and the first look: straight to the analyst
WATCH_STEP_SECONDS = 1.0
FORWARD_MINUTES = 30
MIN_DAYS = 30
GRADES_SETTING = "assistant_watch_grades"
NEAR_PCT = 1.5  # levels this close to the price go to the models with their bar facts

WATCH_SYSTEM = """You are the first, fast reader of one stock for a day trader during the US session. You are asked each time a
1-minute bar completes and right after the system's events. You get the stock's state as the system computed it: the
price, levels with their test verdicts, facts from completed bars, the trader's positions and plans, the latest
events, why you are asked, your own last read and the analyst's last note; and, when present, its 1-minute chart.
Reply with one JSON object and nothing else:
{"note": "one or two short sentences: what just happened and where the price sits against the levels that matter",
 "escalate": true or false,
 "why": "one short sentence: why the analyst should, or need not, look now"}
Escalate when the trader should look now: completed bars confirm a move through a level (the facts say whether a 1- or
5-minute bar closed past it), the volume of the last minutes is well above the session's normal, the price is near
an open plan's trigger, stop or target or acts at a level of a held position, or the state shows something unusual.
Don't escalate a crossing no completed bar confirms, a quiet drift, or what your last read or the analyst's last
note already covered, unless completed bars have changed it since.
Rules:
- Every price you write must be one in the state: never compute, round or invent a price, and never read one off the
  chart's axis.
- Nothing here has passed a test except what the state marks "pass"; in-play passed only as a range selector (which
  stocks move more today), never as a direction or a trade rule.
- Distances are in the state (each level's distance_pct); quote those rather than working one out.
- Leave out what is missing. Never size a position; the trader places every order."""


def watch_enabled() -> bool:
    from . import assistant_model
    return assistant_model.configured("watch")


def analyst_per_hour() -> int:
    try:
        return max(0, int((os.getenv("TRADE_ASSISTANT_ANALYST_PER_HOUR", "") or "20").strip()))
    except ValueError:
        return 20


def watch_daily_cap() -> int:
    try:
        return max(0, int((os.getenv("TRADE_ASSISTANT_WATCH_DAILY_CALLS", "") or "1500").strip()))
    except ValueError:
        return 1500


def watch_gap() -> timedelta:
    """The soonest a stock's market event is read after its last read."""
    try:
        return timedelta(seconds=max(1.0, float((os.getenv("TRADE_ASSISTANT_WATCH_SECONDS", "") or "10").strip())))
    except ValueError:
        return timedelta(seconds=10)


def every_minute() -> bool:
    return (os.getenv("TRADE_ASSISTANT_WATCH_EVERY_MINUTE", "") or "true").strip().lower() not in ("false", "0", "no", "off")


def watch_frames() -> List[str]:
    from . import chart_frames
    return chart_frames.frames("TRADE_ASSISTANT_WATCH_FRAMES", "1m")


# -- the code tier's facts ------------------------------------------------------------------------------------
def _end(bar: Dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(bar["time"]))


def completed(bars: Sequence[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Today's 1-minute bars that had ended by ``now`` (``time`` is the bar's end)."""
    return [bar for bar in bars if not bar.get("partial") and _end(bar) <= now]


def five_minute(bars: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Completed 1-minute bars into the completed 5-minute bars of the session clock (09:30-09:35, ...)."""
    groups: Dict[datetime, List[Dict[str, Any]]] = {}
    for bar in bars:
        start = _end(bar) - timedelta(minutes=1)
        slot = start.replace(minute=start.minute - start.minute % 5, second=0, microsecond=0)
        groups.setdefault(slot, []).append(bar)
    out = []
    for slot, rows in sorted(groups.items()):
        if _end(rows[-1]) < slot + timedelta(minutes=5):
            continue  # still forming
        out.append({"end": slot + timedelta(minutes=5), "open": rows[0]["open"], "high": max(r["high"] for r in rows),
                    "low": min(r["low"] for r in rows), "close": rows[-1]["close"], "volume": sum(r.get("volume") or 0 for r in rows)})
    return out


def _side(close: float, level: float, band: float) -> str:
    return "above" if close > level + band else "below" if close < level - band else "at"


def facts(state: Dict[str, Any], bars: Sequence[Dict[str, Any]], now: datetime, *, band: float,
          in_play: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What completed bars say, for the models: nothing here is a signal (see the verdicts)."""
    done = completed(bars, now)
    price = state.get("price")
    out: Dict[str, Any] = {"definitions": "cross: a trade past the level by 0.05 %; break: a completed bar closed past it "
                                          "by 0.05 %; facts use completed bars only"}
    if in_play and in_play.get("rows"):
        row = next((r for r in in_play["rows"] if r.get("ticker") == state.get("ticker")), None)
        if row is not None:
            out["in_play_today"] = {"rank": row.get("rank"), "of": len(in_play["rows"]), "S": row.get("S"),
                                    "verdict": "pass as a range selector (moves more today), not a direction or trade rule"}
    if len(done) < 2 or not price:
        return out
    last = done[-1]
    fives = five_minute(done)
    out["last_completed_minute_ends"] = _end(last).strftime("%H:%M")
    session_volume = sum(bar.get("volume") or 0 for bar in done)
    recent = done[-5:]
    normal = session_volume / len(done) * len(recent)
    if normal > 0:
        out["volume_last_5_minutes_vs_session_normal"] = round(sum(bar.get("volume") or 0 for bar in recent) / normal, 2)
    first_open = done[0]["open"]
    out["range_so_far_pct"] = round((max(b["high"] for b in done) - min(b["low"] for b in done)) / first_open * 100, 2)
    for minutes in (5, 15):
        if len(done) > minutes:
            out[f"change_last_{minutes}_minutes_pct"] = round((last["close"] / done[-1 - minutes]["close"] - 1) * 100, 2)
    near = []
    for level in state.get("levels") or []:
        value = level.get("price")
        if not isinstance(value, (int, float)) or abs(value / price - 1) * 100 > NEAR_PCT:
            continue
        width = value * band
        row = {"label": level["label"], "price": value, "verdict": level.get("verdict") or "yours",
               "last_1m_close": _side(last["close"], value, width)}
        if fives:
            row["last_5m_close"] = _side(fives[-1]["close"], value, width)
        side = row["last_1m_close"]
        if side != "at":  # how many completed minutes it has closed on this side
            run = 0
            for bar in reversed(done):
                if _side(bar["close"], value, width) != side:
                    break
                run += 1
            row["minutes_closed_on_this_side"] = run
        near.append(row)
    out["levels_near_price"] = near
    return out


# -- the watcher -------------------------------------------------------------------------------------------------
def read_watch(data: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The watcher's note, decision and reason, checked like a note; ValueError when it quoted a price the state
    doesn't show."""
    note = assistant_notes.read_note({"note": data.get("note"), "call": None}, ctx)
    why = " ".join(str(data.get("why") or "").split())[:300]
    wrong = assistant_notes.unverified(why, ctx)
    if wrong:
        raise ValueError(f"it quoted {', '.join(f'{v:g}' for v in wrong[:3])}, which the system doesn't show")
    escalate = data.get("escalate")
    escalate = escalate if isinstance(escalate, bool) else str(escalate).strip().lower() in ("true", "yes", "1")
    return {"text": note["text"], "escalate": escalate, "why": why}


# -- forward grading of the escalations ---------------------------------------------------------------------------
def grade_decisions(decisions: Sequence[Dict[str, Any]], bars: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each decision's range over the next FORWARD_MINUTES completed minutes, % of its price; decisions without
    that much session after them (a session turned off early, the close) are left out."""
    out = []
    last_end = max((_end(bar) for bar in bars), default=None)
    for decision in decisions:
        at = datetime.fromisoformat(decision["at"])
        end = at + timedelta(minutes=FORWARD_MINUTES)
        window = [bar for bar in bars if at < _end(bar) <= end]
        if not window or last_end is None or last_end < end or not decision.get("price"):
            continue  # the session (or the bars held) ended inside the window
        move = (max(b["high"] for b in window) - min(b["low"] for b in window)) / decision["price"] * 100
        out.append({"ticker": decision["ticker"], "at": decision["at"], "escalate": bool(decision["escalate"]),
                    "range_pct": round(move, 4)})
    return out


def summary(grades: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Escalated against not escalated, same stock and day, clustered by day."""
    diffs = []
    counted = 0
    for day, rows in sorted(grades.items()):
        per_stock = []
        for ticker in {row["ticker"] for row in rows}:
            mine = [row for row in rows if row["ticker"] == ticker]
            up = [row["range_pct"] for row in mine if row["escalate"]]
            rest = [row["range_pct"] for row in mine if not row["escalate"]]
            counted += len(mine)
            if up and rest:
                per_stock.append(sum(up) / len(up) - sum(rest) / len(rest))
        if per_stock:
            diffs.append(sum(per_stock) / len(per_stock))
    n = len(diffs)
    mean = sum(diffs) / n if n else None
    t = None
    if n >= 2:
        sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1))
        t = mean / (sd / math.sqrt(n)) if sd > 0 else None
    verdict = (f"too early: {n} of {MIN_DAYS} graded days" if n < MIN_DAYS else
               "escalated moments moved more (day-clustered)" if t is not None and t >= 2 else
               "no difference shown")
    return {"days": n, "decisions": counted, "mean_difference_pct": None if mean is None else round(mean, 3),
            "t": None if t is None else round(t, 2), "verdict": verdict, "forward_minutes": FORWARD_MINUTES}
