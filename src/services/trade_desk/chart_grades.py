"""Forward grades for the chart's flags and for your marks and boxes (research only; nothing is traded).

What counts:

- A system flag (box retest, fade at a box) counts only if the chart showed it, armed or watching,
  before the bar it entered in started: what was seen in time, never a trade found afterwards. Each
  of the four box variants is its own record.
- Your mark ("I'd go long / short here", with your stop and target) enters at the open of the first
  15-minute bar that starts after you mark it.
- A box you keep is traded by the system's fade rule: the first return to it from outside within
  five sessions, entered at its near edge, the stop at its far edge and the target at 2R.

Exits are the backtest's (``backtest/boxes.py``): stop or target, whichever comes first, else the
close of the fifth session after entry, with the same cost. Each finished trade is compared with the
same trade at random times (twins): limit entries for flags and boxes, entries at a bar's open for
marks, so the comparison sees the same entry mechanics. Records move forward whenever the chart of
their stock is read, and once a day after the close for the stocks with trades still running.

A kind gets a verdict after ``MIN_TRADES`` graded trades: it passes when its edge over the twins
has the month-clustered t the methodology needs (``t_needed``) and its trades made money on
average; until then it is too early.
"""
from __future__ import annotations

import math
import uuid
import zlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from . import boxes as bx
from .backtest import boxes as bb

_NEW_YORK = ZoneInfo("America/New_York")
MIN_TRADES = 30
BAR = timedelta(minutes=15)
OPEN_STATES = ("waiting", "open", "closed")  # still moving: entry, exit or twins to come
KINDS = (("system", "break_retest", "System: break and retest"), ("system", "fade", "System: fade at a box"),
         ("you", "mark", "Your marks"), ("you", "box", "Your boxes"))


def _aware(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(_NEW_YORK)


def _start(bars: bx.Bars, k: int) -> datetime:
    return bars.start[k].astype("datetime64[s]").item().replace(tzinfo=_NEW_YORK)


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(float(value), 4)


class Context:
    """One stock's completed 15-minute bars as the chart computed them."""

    def __init__(self, bars: bx.Bars, params: bx.Params, now: datetime, in_session: bool, last_complete: int):
        self.bars, self.params, self.now, self.in_session = bars, params, now, in_session
        self.last_complete = last_complete
        self.following = last_complete + 1

    def first_starting(self, when: datetime) -> Optional[int]:
        """The first bar starting at or after ``when``."""
        stamp = np.datetime64(when.astimezone(_NEW_YORK).replace(tzinfo=None))
        k = int(np.searchsorted(self.bars.start, stamp, side="left"))
        return k if k < len(self.bars) else None

    def last_ended(self, when: datetime) -> Optional[int]:
        """The last bar that had ended by ``when``."""
        stamp = np.datetime64((when.astimezone(_NEW_YORK) - BAR).replace(tzinfo=None))
        k = int(np.searchsorted(self.bars.start, stamp, side="right")) - 1
        return k if k >= 0 else None

    def done(self, k: int) -> bool:
        """The session ``life`` sessions after bar ``k``'s is complete: the trade and same-session twins are over."""
        return self.bars.sess[k] + self.params.life_sessions <= self.last_complete


def _exit(ctx: Context, rec: Dict[str, Any], k: int, price: float, stop: float, target: float, d: int) -> bool:
    """Follows a trade entered in bar ``k``; True when the record changed."""
    bars = ctx.bars
    result = bb.simulate(bars, k, price, stop, target, d, bars.session_end(k, ctx.params.life_sessions))
    if result is None:
        return False
    before = dict(rec)
    rec.update(entry_time=_start(bars, k).isoformat(), price=round(price, 4), stop=round(stop, 4),
               target=round(target, 4), direction=d, R=_r(result["R"]), ret_pct=_r(result["ret_pct"]))
    if result["how"] != "time" or ctx.done(k):
        rec.update(status="closed", how=result["how"], exit=round(result["exit"], 4),
                   exit_time=(_start(bars, result["exit_bar"]) + BAR).isoformat(), month=rec["entry_time"][:7])
    else:
        rec.update(status="open")
    return rec != before


def _twin(ctx: Context, rec: Dict[str, Any], k: int, market: bool) -> bool:
    """The twins' mean R once every twin's own trade is over; True when added."""
    if rec.get("status") != "closed" or rec.get("twin_R") is not None:
        return False
    bars, d, life = ctx.bars, rec["direction"], ctx.params.life_sessions
    rng = np.random.default_rng(zlib.crc32(rec["id"].encode()))
    if market:
        if not ctx.done(k):
            return False
        price = rec["price"]
        twin = bb.market_twins(bars, k, abs(rec["stop"] - price) / price, abs(rec["target"] - price) / price, d, rng, life)
    else:
        _, _, reach = bb.twin_candidates(bars, k, rec["price"], d)
        if reach + life > ctx.last_complete:
            return False
        twin = bb.twins(bars, k, rec["price"], rec["stop"], rec["target"], d, rng, life)
    rec["twin_R"] = _r(twin)
    rec["twin_none"] = rec["twin_R"] is None  # no random time reached the price: left out of the edge
    return True


class ChartGrades:
    def __init__(self, repo: Any):
        self.repo = repo

    # -- the chart's pass over one stock ----------------------------------------------------
    def record(self, ticker: str, ctx: Context, system: Sequence[Tuple[str, str, bx.Entry, Optional[int]]],
               live: Sequence[Tuple[str, bx.Setup]]) -> List[Dict[str, Any]]:
        """Moves this stock's records forward and returns yours for drawing.

        ``system``: ``(setup id, state, entry, entry bar)`` for every flag the chart shows; ``live`` the
        ``(setup id, setup)`` pairs whose flags may still be pending."""
        existing = {r["id"]: r for r in self.repo.chart_trades(ticker=ticker, statuses=OPEN_STATES)}
        changed: Dict[str, Dict[str, Any]] = {}
        variant = f"{ctx.params.impulse_t:g}|{int(ctx.params.tidy)}"
        shown = set()
        for setup_id, state, entry, bar in system:
            rid = f"sys|{ticker}|{variant}|{setup_id}|{entry.kind}|{entry.box}"
            shown.add(rid)
            rec = existing.get(rid)
            if state in ("armed", "watching"):
                if rec is None:  # first seen: from now on its trade counts
                    changed[rid] = {"id": rid, "ticker": ticker, "status": "waiting", "source": "system", "kind": entry.kind,
                                    "box": entry.box, "setup": setup_id, "impulse_t": ctx.params.impulse_t,
                                    "tidy": ctx.params.tidy, "seen_at": ctx.now.isoformat(), "direction": entry.direction,
                                    "price": round(entry.price, 4), "stop": round(entry.stop, 4), "target": round(entry.target, 4)}
                continue
            if rec is None or bar is None:
                continue  # never shown before it traded: not a forward record
            if rec["status"] == "waiting" and _start(ctx.bars, bar) < _aware(rec["seen_at"]):
                changed[rid] = {**rec, "status": "late"}  # it filled in the bar that was forming when first seen
                continue
            touched = _exit(ctx, rec, bar, entry.price, entry.stop, entry.target, entry.direction)
            if _twin(ctx, rec, bar, market=False) or touched:
                changed[rid] = rec
        prefix = f"sys|{ticker}|{variant}|"
        pending = {f"{prefix}{sid}|" for sid, _ in live}
        for rid, rec in existing.items():
            # A waiting flag the chart no longer shows ended without a trade (expired, or its target came first).
            if rid.startswith(prefix) and rec["status"] == "waiting" and rid not in shown and \
                    not any(rid.startswith(p) for p in pending) and rid not in changed:
                changed[rid] = {**rec, "status": "expired"}
        yours = []
        for rec in existing.values():
            if rec.get("source") != "you":
                continue
            update = self._mark(ctx, rec) if rec["kind"] == "mark" else self._box(ctx, rec)
            if update:
                changed[rec["id"]] = rec
            yours.append(rec)
        if changed:
            self.repo.save_chart_trades(list(changed.values()))
        return yours

    def _mark(self, ctx: Context, rec: Dict[str, Any]) -> bool:
        k = ctx.first_starting(_aware(rec["marked_at"]))
        if k is None:
            return False
        d, price = rec["direction"], float(ctx.bars.o[k])
        if (price - rec["stop"]) * d <= 0 or (rec["target"] - price) * d <= 0:
            rec.update(status="void", entry_time=_start(ctx.bars, k).isoformat(), price=round(price, 4),
                       void="opened past your stop" if (price - rec["stop"]) * d <= 0 else "opened past your target")
            return True
        touched = _exit(ctx, rec, k, price, rec["stop"], rec["target"], d)
        return _twin(ctx, rec, k, market=True) or touched

    def _box(self, ctx: Context, rec: Dict[str, Any]) -> bool:
        bars, life = ctx.bars, ctx.params.life_sessions
        i = ctx.last_ended(_aware(rec["kept_at"]))
        if i is None or i + 1 >= len(bars):
            return False
        lo, hi = rec["bottom"], rec["top"]
        setup = bx.Setup(i=i, up=True, range_box=(lo, hi), base_box=None, range_wick=(lo, hi), range_own_wick=(lo, hi),
                         base_wick=(lo, hi), first=i + 1, last=bars.session_end(i, life))
        found = bx.fades(bars, setup, ctx.params)
        if not found:
            if ctx.following > bars.sess[i] + life:
                rec["status"] = "expired"
                return True
            armed = [e for state, e in bx.pending(bars, setup, ctx.params, next_in_session=ctx.in_session) if state == "armed"]
            side = armed[0] if armed else None
            new = {"armed": side is not None, "direction": side.direction if side else None,
                   "price": round(side.price, 4) if side else None, "stop": round(side.stop, 4) if side else None,
                   "target": round(side.target, 4) if side else None}
            if all(rec.get(key) == value for key, value in new.items()):
                return False
            rec.update(new)
            return True
        e = found[0]
        rec["armed"] = False
        touched = _exit(ctx, rec, e.bar, e.price, e.stop, e.target, e.direction)
        return _twin(ctx, rec, e.bar, market=False) or touched

    # -- yours ------------------------------------------------------------------------------
    def add_mark(self, ticker: str, direction: str, stop: float, target: float, reference: float, now: datetime,
                 note: str = "") -> Dict[str, Any]:
        d = 1 if direction == "long" else -1
        if not (reference > 0 and (reference - stop) * d > 0 and (target - reference) * d > 0):
            side = "below" if d > 0 else "above"
            raise ValueError(f"For a {direction} mark the stop goes {side} the last price ({reference:g}) and the target "
                             f"{'above' if d > 0 else 'below'} it")
        rec = {"id": f"you|{ticker}|mark|{uuid.uuid4().hex[:12]}", "ticker": ticker, "status": "waiting", "source": "you",
               "kind": "mark", "direction": d, "stop": float(stop), "target": float(target), "reference": reference,
               "marked_at": now.isoformat(), "note": note[:200]}
        self.repo.save_chart_trades([rec])
        return rec

    def add_box(self, ticker: str, top: float, bottom: float, now: datetime, note: str = "") -> Dict[str, Any]:
        if not (top > bottom > 0):
            raise ValueError("A box needs a top above its bottom")
        rec = {"id": f"you|{ticker}|box|{uuid.uuid4().hex[:12]}", "ticker": ticker, "status": "waiting", "source": "you",
               "kind": "box", "top": float(top), "bottom": float(bottom), "kept_at": now.isoformat(), "note": note[:200]}
        self.repo.save_chart_trades([rec])
        return rec

    def remove(self, record_id: str) -> bool:
        """Only a mark or box of yours that has not traded yet can be removed; a trade stays on the record."""
        if not record_id.startswith("you|"):
            return False
        return self.repo.delete_chart_trade(record_id, ("waiting",))

    # -- the grade table ----------------------------------------------------------------------
    def summary(self, recent: int = 30) -> Dict[str, Any]:
        from .idea_tracker import t_needed
        rows = self.repo.chart_trades()
        out = []
        for source, kind, label in KINDS:
            mine = [r for r in rows if r.get("source") == source and r.get("kind") == kind]
            graded = [SimpleNamespace(R=r["R"], twin_R=r["twin_R"], edge=r["R"] - r["twin_R"], month=r["month"],
                                      ret_pct=r.get("ret_pct") or 0.0)
                      for r in mine if r["status"] == "closed" and r.get("twin_R") is not None]
            stats = bb.summarize(graded) if graded else None
            needed = t_needed(stats["months"]) if stats else None
            verdict = "too_early"
            if len(graded) >= MIN_TRADES:
                verdict = "passes" if bb.passes({"all": stats}, needed or 99) else "no_edge"
            count = {status: sum(1 for r in mine if r["status"] == status) for status in
                     ("waiting", "open", "closed", "expired", "late", "void")}
            out.append({"source": source, "kind": kind, "label": label, "graded": len(graded), "needed": MIN_TRADES,
                        **count, "mean_R": _r(stats and stats["mean_R"]), "twin_R": _r(stats and stats["twin_R"]),
                        "edge": _r(stats and stats["edge"]), "t": _r(stats and stats["t_edge"]),
                        "months": stats["months"] if stats else 0, "t_needed": needed, "verdict": verdict})
        keep = ("id", "ticker", "status", "source", "kind", "direction", "price", "stop", "target", "R", "twin_R",
                "entry_time", "exit_time", "how", "seen_at", "marked_at", "kept_at", "top", "bottom", "impulse_t", "tidy",
                "void", "note")
        return {"kinds": out, "recent": [{k: r.get(k) for k in keep if k in r} for r in rows[:recent]],
                "min_trades": MIN_TRADES}

    def unfinished(self) -> List[Tuple[str, float, bool]]:
        """``(ticker, impulse_t, tidy)`` with records still moving, for the daily pass."""
        rows = self.repo.chart_trades(statuses=OPEN_STATES)
        out = set()
        for r in rows:
            if r.get("source") == "system":
                out.add((r["ticker"], r["impulse_t"], r["tidy"]))
            elif r["status"] != "closed" or r.get("twin_R") is None:
                out.add((r["ticker"], 1.0, False))
        return sorted(out)
