"""Boxes from a big move on intraday bars, the trades placed on them, and weekly value areas.

The rules of the trading video tested on 2026-11-05 (docs/strategy-backtest.md,
docs/trading-methodology.md), shared by the backtest and the live chart so the chart draws
exactly the rule that was tested:

- T: the median session range (high - low) over the ``t_sessions`` sessions before a bar's session.
- Impulse: one bar whose change from the previous close (an opening gap included) is at least
  ``impulse_t`` x T.
- Range box: the bodies of the ``box_bars`` bars after the impulse; base box: those before it. A box
  counts when its height is at most ``max_height`` x the impulse (and, with ``tidy``, at most half of
  it with first and last closes within half the box height: no drift). A setup lives until the end of
  the ``life_sessions``-th session after the impulse's.
- Break and retest (the video's worked example): after a close outside the range box on the side away
  from the impulse, a limit at the box's near edge, against the impulse; stop past the range's extreme
  wick (impulse bar included), target the base box's near edge; skipped when the target is nearer than
  the stop or was reached before the retest.
- Fade: the first return to a box from outside (previous close outside, this bar touching the edge); a
  limit at the edge, stop past the box's own extreme wick, target twice the stop distance.

A decision uses only bars up to and including the bar it is made in; ``pending`` gives what the
next bar could fill (the live chart's flags). Research context, not signals: neither trade beat the
same trades at random times.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np

Box = Tuple[float, float]


@dataclass(frozen=True)
class Params:
    impulse_t: float = 1.0
    box_bars: int = 8
    max_height: float = 0.75
    life_sessions: int = 5
    t_sessions: int = 10
    tidy: bool = False
    fade_reward: float = 2.0


@dataclass(frozen=True)
class Setup:
    i: int  # the impulse bar
    up: bool
    range_box: Optional[Box]
    base_box: Optional[Box]
    range_wick: Box  # the impulse bar and the range bars
    range_own_wick: Box
    base_wick: Box
    first: int  # first bar after the range
    last: int  # last bar of the setup's last session


@dataclass(frozen=True)
class Entry:
    kind: str  # "break_retest" or "fade"
    bar: int
    price: float
    stop: float
    target: float
    direction: int  # 1 long, -1 short
    box: str  # "range" or "base"
    span: Box  # the traded box including its wicks
    break_bar: Optional[int] = None


class Bars:
    """Regular-session bars as arrays; ``start`` holds each bar's start time (datetime64)."""

    def __init__(self, start: Sequence[Any], o: Sequence[float], h: Sequence[float], low: Sequence[float],
                 c: Sequence[float], v: Optional[Sequence[float]] = None, *, t_sessions: int = Params.t_sessions):
        self.start = np.asarray(start, dtype="datetime64[ns]")
        self.o, self.h, self.l, self.c = (np.asarray(x, dtype=float) for x in (o, h, low, c))
        self.v = np.asarray(v, dtype=float) if v is not None else np.zeros(len(self.c))
        day = self.start.astype("datetime64[D]")
        self.days, self.sess = np.unique(day, return_inverse=True)
        n_sessions = len(self.days)
        self.s_first = np.searchsorted(self.sess, np.arange(n_sessions), "left")
        self.s_last = np.searchsorted(self.sess, np.arange(n_sessions), "right") - 1
        ranges = np.array([self.h[a:b + 1].max() - self.l[a:b + 1].min() for a, b in zip(self.s_first, self.s_last)])
        per_session = np.full(n_sessions, np.nan)
        for i in range(t_sessions, n_sessions):
            per_session[i] = np.median(ranges[i - t_sessions:i])
        self.T = per_session[self.sess]
        self.chg = np.r_[np.nan, np.diff(self.c)]

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, Any]], *, minutes: int = 15, **kwargs: Any) -> "Bars":
        """From moomoo K-line rows: ``time_key`` is the bar's end, New York time."""
        end = np.array([str(row["time_key"]) for row in rows], dtype="datetime64[ns]")
        return cls(end - np.timedelta64(minutes, "m"), [r["open"] for r in rows], [r["high"] for r in rows],
                   [r["low"] for r in rows], [r["close"] for r in rows], [r.get("volume", 0.0) for r in rows], **kwargs)

    def __len__(self) -> int:
        return len(self.c)

    def session_end(self, bar: int, after: int) -> int:
        """The last bar of the ``after``-th session after ``bar``'s (or of the data)."""
        return int(self.s_last[min(self.sess[bar] + after, len(self.days) - 1)])


def _box(bl: np.ndarray, bh: np.ndarray, c: np.ndarray, part: slice, size: float, params: Params) -> Optional[Box]:
    lo, hi = bl[part].min(), bh[part].max()
    if hi - lo > params.max_height * size:
        return None
    if params.tidy and (hi - lo > 0.5 * size or abs(c[part][-1] - c[part][0]) > 0.5 * (hi - lo)):
        return None
    return (float(lo), float(hi))


def setups(bars: Bars, params: Params = Params()) -> Iterator[Setup]:
    """Every impulse whose range bars are complete and hold no other impulse (the live chart sees a
    setup as soon as its last range bar closes)."""
    n = params.box_bars
    imp = np.abs(bars.chg) >= params.impulse_t * bars.T
    imp[np.isnan(bars.T)] = False
    bl, bh = np.minimum(bars.o, bars.c), np.maximum(bars.o, bars.c)
    for i in np.flatnonzero(imp):
        if i - n < 0 or i + n >= len(bars) or imp[i + 1:i + 1 + n].any():
            continue
        size = abs(bars.chg[i])
        after, before = slice(i + 1, i + 1 + n), slice(i - n, i)
        yield Setup(i=int(i), up=bool(bars.chg[i] > 0),
                    range_box=_box(bl, bh, bars.c, after, size, params),
                    base_box=_box(bl, bh, bars.c, before, size, params),
                    range_wick=(float(bars.l[i:i + 1 + n].min()), float(bars.h[i:i + 1 + n].max())),
                    range_own_wick=(float(bars.l[after].min()), float(bars.h[after].max())),
                    base_wick=(float(bars.l[before].min()), float(bars.h[before].max())),
                    first=i + n + 1, last=bars.session_end(i, params.life_sessions))


def _retest_levels(s: Setup) -> Optional[Tuple[int, float, float, float]]:
    """Direction, entry edge, stop and target of a setup's break and retest; None when it has none."""
    if s.range_box is None or s.base_box is None:
        return None
    d = -1 if s.up else 1
    (rlo, rhi), (blo, bhi) = s.range_box, s.base_box
    edge, target = (rlo, bhi) if d < 0 else (rhi, blo)
    stop = s.range_wick[1] if d < 0 else s.range_wick[0]
    if (target - edge) * d <= 0 or abs(target - edge) < abs(stop - edge):
        return None
    return d, edge, stop, target


def break_retest(bars: Bars, s: Setup) -> Optional[Entry]:
    levels = _retest_levels(s)
    if levels is None:
        return None
    d, edge, stop, target = levels
    rlo, rhi = s.range_box
    broke = None
    for k in range(s.first, s.last + 1):
        if (d < 0 and bars.c[k] < rlo) or (d > 0 and bars.c[k] > rhi):
            broke = k
            break
    if broke is None:
        return None
    for k in range(broke, s.last + 1):
        if k > broke and ((d < 0 and bars.h[k] >= edge) or (d > 0 and bars.l[k] <= edge)):
            price = bars.o[k] if (bars.o[k] - edge) * d < 0 else edge  # opened past the edge: filled at the open
            if (price - stop) * d <= 0:
                return None
            return Entry("break_retest", k, float(price), float(stop), float(target), d, "range", s.range_wick, broke)
        if (d < 0 and bars.l[k] <= target) or (d > 0 and bars.h[k] >= target):
            return None  # reached the target before any retest
    return None


def fades(bars: Bars, s: Setup, params: Params = Params()) -> List[Entry]:
    out = []
    for name, box, wick in (("range", s.range_box, s.range_own_wick), ("base", s.base_box, s.base_wick)):
        if box is None:
            continue
        lo, hi = box
        for k in range(s.first, s.last + 1):
            if bars.c[k - 1] > hi and bars.l[k] <= hi:
                d, edge, stop = 1, hi, wick[0]
            elif bars.c[k - 1] < lo and bars.h[k] >= lo:
                d, edge, stop = -1, lo, wick[1]
            else:
                continue
            price = bars.o[k] if (bars.o[k] - edge) * d < 0 else edge
            if (price - stop) * d > 0:
                out.append(Entry("fade", k, float(price), float(stop),
                                 float(price + params.fade_reward * (price - stop)), d, name, wick))
            break
    return out


def entries(bars: Bars, s: Setup, params: Params = Params()) -> List[Entry]:
    """Both trades on one setup, break and retest first."""
    found = break_retest(bars, s)
    return ([found] if found else []) + fades(bars, s, params)


def pending(bars: Bars, s: Setup, params: Params = Params(), *,
            next_in_session: bool = True) -> List[Tuple[str, Entry]]:
    """What the next bar could fill on a setup, by the rules above, from the bars so far.

    ``("armed", entry)``: a limit the next bar fills if it reaches ``entry.price`` (at its open when
    it opens past it; nothing when it opens past the stop). ``("watching", entry)``: a break and
    retest that needs a close past ``entry.price`` first. ``entry.bar`` is the next bar's index and a
    fade's target assumes a fill at the limit. Empty once the setup has expired for the next bar
    (``next_in_session`` says whether that bar is in the last bar's session) or its trades were taken
    or ruled out.
    """
    n = len(bars)
    following = len(bars.days) - 1 if next_in_session else len(bars.days)
    if following > bars.sess[s.i] + params.life_sessions:
        return []
    out = []
    levels = _retest_levels(s)
    if levels is not None:
        d, edge, stop, target = levels
        broke = next((k for k in range(s.first, n) if (bars.c[k] - edge) * d > 0), None)
        # After the break: retested (taken) or the target reached first (ruled out).
        over = broke is not None and any(
            (k > broke and ((bars.h[k] >= edge) if d < 0 else (bars.l[k] <= edge)))
            or ((bars.l[k] <= target) if d < 0 else (bars.h[k] >= target)) for k in range(broke, n))
        if (edge - stop) * d > 0 and not over:
            out.append(("watching" if broke is None else "armed",
                        Entry("break_retest", n, edge, stop, target, d, "range", s.range_wick, broke)))
    for name, box, wick in (("range", s.range_box, s.range_own_wick), ("base", s.base_box, s.base_wick)):
        if box is None:
            continue
        lo, hi = box
        if any((bars.c[k - 1] > hi and bars.l[k] <= hi) or (bars.c[k - 1] < lo and bars.h[k] >= lo)
               for k in range(s.first, n)):
            continue  # its one return has come: taken or ruled out
        if bars.c[n - 1] > hi:
            d, edge, stop = 1, hi, wick[0]
        elif bars.c[n - 1] < lo:
            d, edge, stop = -1, lo, wick[1]
        else:
            continue
        if (edge - stop) * d > 0:
            out.append(("armed", Entry("fade", n, edge, stop, edge + params.fade_reward * (edge - stop), d, name,
                                       wick)))
    return out


def weekly_value_areas(bars: Bars, *, share: float = 0.7, bin_pct: float = 0.1) -> Dict[np.datetime64, Box]:
    """{week's Monday: (VAL, VAH)}: each bar's volume spread evenly over its range, bins ``bin_pct``% of
    the week's average close, grown from the busiest bin until ``share`` of the volume (an approximation
    of a trade-by-trade volume profile)."""
    day = bars.start.astype("datetime64[D]")
    monday = day - ((day.astype("int64") - 4) % 7).astype("timedelta64[D]")  # 1970-01-01 was a Thursday
    out = {}
    for week in np.unique(monday):
        m = monday == week
        h, low, v = bars.h[m], bars.l[m], bars.v[m]
        if v.sum() <= 0:
            continue
        width = bin_pct / 100 * bars.c[m].mean()
        lo0 = low.min()
        profile = np.zeros(int((h.max() - lo0) / width) + 2)
        for top, bottom, volume in zip(h, low, v):
            a, z = int((bottom - lo0) / width), int((top - lo0) / width)
            profile[a:z + 1] += volume / (z - a + 1)
        a = z = int(profile.argmax())
        held, total = profile[a], profile.sum()
        while held < share * total:
            up = profile[z + 1] if z + 1 < len(profile) else -1.0
            down = profile[a - 1] if a > 0 else -1.0
            if up >= down:
                z += 1
                held += up
            else:
                a -= 1
                held += down
        out[week.astype("datetime64[D]")] = (float(lo0 + a * width), float(lo0 + (z + 1) * width))
    return out


def prior_levels(areas: Mapping[np.datetime64, Box], when: Any, weeks: int = 2) -> List[float]:
    """VAL and VAH of the ``weeks`` completed weeks before ``when``'s week (empty when fewer exist)."""
    day = np.datetime64(when, "D")
    monday = day - np.timedelta64(int((day.astype("int64") - 4) % 7), "D")
    done = sorted(w for w in areas if w < monday)[-weeks:]
    return [x for w in done for x in areas[w]] if len(done) == weeks else []


def confirmed(span: Box, levels: Sequence[float], t: float, *, pad: float = 0.1) -> bool:
    """A value-area level within the traded box (wicks included), widened by ``pad`` x T."""
    lo, hi = span[0] - pad * t, span[1] + pad * t
    return any(lo <= level <= hi for level in levels)
