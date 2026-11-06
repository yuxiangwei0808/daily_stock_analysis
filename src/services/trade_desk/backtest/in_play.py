"""The in-play range test (docs/strategy-backtest.md): stocks ranked at 09:45 by how far they already moved.

Frozen on 2026-11-06 before any real-data run. For each stock and session with a normal (T, the median
session range of the 10 prior sessions, as in ``boxes.py``) and a first 15-minute bar at 09:30:

- the score S = (|open - prior close| + the first bar's high - low) / T;
- the outcome Y = the rest of the session's range (09:45 to the close) / T2, where T2 is the median
  range of the sessions 11 to 20 before. Addendum of 2026-11-06, before any real-data result: with T
  in both, random walks "passed" (+0.11 T, t 2.5 / 3.6), because a stock-day whose T happened to be low
  got a high score and a high outcome alike. T2 shares no session with T.

Each day the top ``TOP`` stocks by S are the picks and every other stock that day is their twin; the
day's edge is the picks' mean Y minus the others'. The live in-play list (``in_play_list`` in
``trade_desk/in_play.py``) uses the same score.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .. import boxes as bx
from .boxes import month_t

TOP = 5
MIN_STOCKS = 20
SEED = 7
SHUFFLES = 200


def features(bars: bx.Bars) -> Dict[str, Dict[str, float]]:
    """Per session (``YYYY-MM-DD``): S and its parts, the first bar's volume ratio, and the outcome Y."""
    out: Dict[str, Dict[str, float]] = {}
    first_volume = bars.v[bars.s_first]
    ranges = np.array([bars.h[a:b + 1].max() - bars.l[a:b + 1].min() for a, b in zip(bars.s_first, bars.s_last)])
    for s in range(20, len(bars.days)):
        a, b = int(bars.s_first[s]), int(bars.s_last[s])
        T, T2 = float(bars.T[a]), float(np.median(ranges[s - 20:s - 10]))
        if not T > 0 or not T2 > 0 or b - a < 1 or str(bars.start[a])[11:16] != "09:30":
            continue
        prior_close = float(bars.c[bars.s_last[s - 1]])
        gap, first = float(bars.o[a]) - prior_close, float(bars.h[a] - bars.l[a])
        rest = float(bars.h[a + 1:b + 1].max() - bars.l[a + 1:b + 1].min())
        normal_volume = float(np.median(first_volume[max(0, s - 10):s])) if s >= 10 else math.nan
        out[str(bars.days[s])[:10]] = {"S": (abs(gap) + first) / T, "gap": abs(gap) / T, "range1": first / T,
                                       "up": float(gap > 0), "Y": rest / T2,
                                       "vol_ratio": float(bars.v[a]) / normal_volume if normal_volume > 0 else math.nan}
    return out


def daily_edges(tables: Dict[str, Dict[str, Dict[str, float]]], score: str = "S", top: int = TOP,
                min_stocks: int = MIN_STOCKS, keep: Optional[str] = None) -> List[Tuple[str, float]]:
    """``(day, picks' mean Y - the others' mean Y)`` for every day with enough stocks.

    ``score`` is a feature name or ``"S_vol"`` (S times the volume ratio); ``keep`` limits the day's stocks
    to ``"up"`` or ``"down"`` gaps (a secondary check)."""
    by_day: Dict[str, List[Tuple[float, float]]] = {}
    for rows in tables.values():
        for day, row in rows.items():
            value = row["S"] * row["vol_ratio"] if score == "S_vol" else row[score]
            if math.isnan(value) or (keep == "up" and not row["up"]) or (keep == "down" and row["up"]):
                continue
            by_day.setdefault(day, []).append((value, row["Y"]))
    edges = []
    for day in sorted(by_day):
        rows = sorted(by_day[day], key=lambda pair: -pair[0])
        if len(rows) < min_stocks:
            continue
        picks, others = [y for _, y in rows[:top]], [y for _, y in rows[top:]]
        edges.append((day, sum(picks) / len(picks) - sum(others) / len(others)))
    return edges


def summary(edges: Sequence[Tuple[str, float]], split: str, *, weeks: bool = False) -> Dict[str, Dict[str, Any]]:
    """Per period: days, mean edge and its t over day (or ISO week) clusters."""
    out = {}
    for name, rows in (("A", [e for e in edges if e[0] < split]), ("B", [e for e in edges if e[0] >= split])):
        key = (lambda day: "%d-%02d" % date.fromisoformat(day).isocalendar()[:2]) if weeks else (lambda day: day)
        mean, t, clusters = month_t([(key(day), value) for day, value in rows]) if rows else (None, None, 0)
        out[name] = {"days": len(rows), "clusters": clusters, "edge": mean, "t": t,
                     "share_positive": sum(1 for _, v in rows if v > 0) / len(rows) if rows else None}
    return out


def passes(by_period: Dict[str, Dict[str, Any]], needed: float = 2.0) -> bool:
    return all(row["edge"] is not None and row["edge"] > 0 and row["t"] is not None and row["t"] >= needed
               for row in by_period.values())


def shuffled_scores(tables: Dict[str, Dict[str, Dict[str, float]]], rng: np.random.Generator,
                    draws: int = SHUFFLES) -> List[float]:
    """The mean daily edge with each day's scores shuffled among its stocks, ``draws`` times."""
    by_day: Dict[str, List[Tuple[float, float]]] = {}
    for rows in tables.values():
        for day, row in rows.items():
            by_day.setdefault(day, []).append((row["S"], row["Y"]))
    days = [rows for rows in by_day.values() if len(rows) >= MIN_STOCKS]
    out = []
    for _ in range(draws):
        total = 0.0
        for rows in days:
            ys = np.array([y for _, y in rows])
            order = rng.permutation(len(rows))[:TOP]
            mask = np.zeros(len(rows), dtype=bool)
            mask[order] = True
            total += ys[mask].mean() - ys[~mask].mean()
        out.append(total / len(days) if days else math.nan)
    return out


def random_walk(bars: bx.Bars, rng: np.random.Generator) -> bx.Bars:
    """The stock's own 15-minute bars in shuffled order, each keeping its shape relative to the prior close."""
    prev = np.r_[bars.o[0], bars.c[:-1]]
    shape = np.stack([bars.o / prev, bars.h / prev, bars.l / prev, bars.c / prev], axis=1)
    shape = shape[rng.permutation(len(shape))]
    o, h, low, c = (np.empty(len(shape)) for _ in range(4))
    close = float(bars.o[0])
    for k, (ro, rh, rl, rc) in enumerate(shape):
        o[k], h[k], low[k], c[k] = close * ro, close * rh, close * rl, close * rc
        close = c[k]
    return bx.Bars(bars.start, o, h, low, c, bars.v[rng.permutation(len(bars.v))])
