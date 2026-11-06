"""Reading the 0DTE quote recorder's files (option_recorder.py) for tests on real option quotes.

Phase 6 of docs/trading-assistant-plan.md: no rule is tested on these until about ``READY_SESSIONS``
sessions are recorded, and each test is frozen first (docs/trading-methodology.md §3 and §14). A day's
file per underlying holds one row per code per snapshot, every 15 seconds; ``right`` is ``S`` for the
underlying's own quote.
"""
from __future__ import annotations

import csv
import gzip
import math
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any, Dict, List, Optional

READY_SESSIONS = 60
NUMBERS = ("ts", "strike", "bid", "ask", "bid_size", "ask_size", "last", "volume", "open_interest", "iv", "delta",
           "gamma", "theta", "vega")


def recorded(root: Path) -> Dict[str, List[str]]:
    """``{ticker: [days]}`` of what the recorder wrote under ``root``, days in order."""
    out: Dict[str, List[str]] = {}
    for path in sorted(Path(root).glob("*/*.csv.gz")):
        out.setdefault(path.name.split(".")[0], []).append(path.parent.name)
    return out


def _value(text: str) -> Optional[float]:
    if text == "":
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def load_day(root: Path, day: str, ticker: str) -> Dict[str, List[Dict[str, Any]]]:
    """``{code: snapshots}`` for one underlying and day, each list in time order (``ts`` in Unix seconds)."""
    path = Path(root) / day / f"{ticker}.csv.gz"
    out: Dict[str, List[Dict[str, Any]]] = {}
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("ts") == "ts":  # the header of a later member (a restart that day)
                continue
            snap = {key: (_value(value) if key in NUMBERS else value) for key, value in row.items()}
            out.setdefault(row["code"], []).append(snap)
    for rows in out.values():
        rows.sort(key=lambda snap: snap["ts"])
    return out


def quote_at(snaps: List[Dict[str, Any]], ts: float, side: str) -> Optional[float]:
    """The ask (``buy``) or bid (``sell``) of the first snapshot at or after ``ts``: what a fill then would get."""
    times = [snap["ts"] for snap in snaps]
    k = bisect_left(times, ts)
    if k >= len(snaps):
        return None
    return snaps[k]["ask" if side == "buy" else "bid"]


def nearest_contract(day: Dict[str, List[Dict[str, Any]]], ts: float, right: str) -> Optional[str]:
    """The code of the ``right`` (``C`` or ``P``) contract nearest the money at ``ts``, by the underlying's last price."""
    under = next((rows for rows in day.values() if rows and rows[0]["right"] == "S"), None)
    if not under:
        return None
    k = bisect_right([snap["ts"] for snap in under], ts) - 1
    if k < 0 or under[k]["last"] is None:
        return None
    price = under[k]["last"]
    options = [(abs(rows[0]["strike"] - price), code) for code, rows in day.items()
               if rows and rows[0]["right"] == right and rows[0]["strike"] is not None]
    return min(options)[1] if options else None
