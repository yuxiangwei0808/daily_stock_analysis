"""Backtest of the box trades in ``trade_desk/boxes.py`` (research only; nothing is traded).

Exits: stop or target, whichever comes first; both in one bar count as the stop, and in the entry bar
only the stop counts. A bar opening past the stop fills at its open (past the target: at its open, the
better price). Otherwise the last close of the ``life_sessions``-th session after entry. ``COST`` per
round trip. Results are in R (multiples of the stop distance) and in percent.

Twins copy the entry mechanics at random times: a limit the same percent from the previous close as the
real entry, in the same direction, on random bars of the same session that reach it (the five sessions
either side when fewer than five do), with the same stop and target distances, exits and costs. Twins
entered at a bar's open instead faked +0.03..0.24 R on shuffled bars, because a limit entry and an
open entry see different parts of the entry bar.

``shuffled`` is the random-data control: each bar keeps its gap and shape, the order is random.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .. import boxes as bx

COST = 0.0005
N_TWINS = 20


@dataclass
class BoxTrade:
    ticker: str
    kind: str
    box: str
    impulse_t: float
    tidy: bool
    entry_time: str
    direction: int
    entry: float
    stop: float
    target: float
    exit: float
    how: str
    R: float
    ret_pct: float
    twin_R: float
    T: float
    span: tuple
    impulse_bar: int
    entry_bar: int
    exit_bar: int
    break_bar: Optional[int] = None
    levels: List[float] = field(default_factory=list)

    @property
    def edge(self) -> float:
        return self.R - self.twin_R

    @property
    def month(self) -> str:
        return self.entry_time[:7]


def simulate(bars: bx.Bars, e: int, entry: float, stop: float, target: float, d: int, last: int,
             cost: float = COST) -> Optional[Dict[str, Any]]:
    """R and % after costs, exit bar, price and reason for a position entered in bar ``e``."""
    risk = abs(entry - stop)
    if risk <= 0 or e > last:
        return None
    highs, lows, opens = bars.h[e:last + 1], bars.l[e:last + 1], bars.o[e:last + 1]
    stop_hit = (lows <= stop) if d > 0 else (highs >= stop)
    target_hit = (highs >= target) if d > 0 else (lows <= target)
    target_hit[0] = False
    never = 10 ** 9
    first_stop = int(np.argmax(stop_hit)) if stop_hit.any() else never
    first_target = int(np.argmax(target_hit)) if target_hit.any() else never
    if first_stop == never and first_target == never:
        k, price, how = last, bars.c[last], "time"
    elif first_stop <= first_target:
        k = e + first_stop
        price = opens[first_stop] if first_stop > 0 and (opens[first_stop] - stop) * d < 0 else stop
        how = "stop"
    else:
        k = e + first_target
        price = opens[first_target] if (opens[first_target] - target) * d > 0 else target
        how = "target"
    pnl = (price - entry) * d - cost * entry
    return {"R": pnl / risk, "ret_pct": pnl / entry * 100, "exit_bar": int(k), "exit": float(price), "how": how}


def twin_candidates(bars: bx.Bars, e: int, entry: float, d: int):
    """Bars that reach the trade's limit (same distance from the previous close) and their levels, and the last
    session they come from: the trade's own, or five sessions either side when fewer than five fill in it."""
    s = bars.sess[e]
    dip = (bars.c[e - 1] - entry) * d / bars.c[e - 1]  # how far the price came against the trade to fill it
    for lo_s, hi_s in ((s, s), (max(s - 5, 0), min(s + 5, len(bars.days) - 1))):
        lo, hi = max(bars.s_first[lo_s], 1), bars.s_last[hi_s]
        cand = np.arange(lo, hi + 1)
        level = bars.c[cand - 1] * (1 - d * dip)
        ok = (bars.l[cand] <= level) if d > 0 else (bars.h[cand] >= level)
        cand, level = cand[ok], level[ok]
        if len(cand) >= 5 or (lo_s, hi_s) != (s, s):
            break
    return cand, level, int(hi_s)


def twins(bars: bx.Bars, e: int, entry: float, stop: float, target: float, d: int, rng: np.random.Generator,
          life: int = bx.Params.life_sessions) -> float:
    """Mean R of ``N_TWINS`` limit entries at random times with the trade's distances (NaN when none fill)."""
    stop_pct, target_pct = abs(stop - entry) / entry, abs(target - entry) / entry
    cand, level, _ = twin_candidates(bars, e, entry, d)
    if not len(cand):
        return float("nan")
    out = []
    for j in rng.integers(0, len(cand), N_TWINS):
        r, lv = cand[j], level[j]
        price = bars.o[r] if (bars.o[r] - lv) * d < 0 else lv
        res = simulate(bars, r, price, price * (1 - d * stop_pct), price * (1 + d * target_pct), d,
                       bars.session_end(r, life))
        if res:
            out.append(res["R"])
    return float(np.mean(out)) if out else float("nan")


def market_twins(bars: bx.Bars, e: int, stop_pct: float, target_pct: float, d: int, rng: np.random.Generator,
                 life: int = bx.Params.life_sessions) -> float:
    """Mean R of ``N_TWINS`` entries at the open of random bars of the trade's session, with its distances.

    The twin of an entry at a bar's open (your marks), as ``twins`` is of a limit entry."""
    s = bars.sess[e]
    cand = np.arange(bars.s_first[s], bars.s_last[s] + 1)
    out = []
    for r in cand[rng.integers(0, len(cand), N_TWINS)]:
        price = bars.o[r]
        res = simulate(bars, r, price, price * (1 - d * stop_pct), price * (1 + d * target_pct), d,
                       bars.session_end(r, life))
        if res:
            out.append(res["R"])
    return float(np.mean(out)) if out else float("nan")


def shuffled(bars: bx.Bars, seed: int) -> bx.Bars:
    """The random-data control: bars in random order, each keeping its gap and shape; same timestamps."""
    rng = np.random.default_rng(seed)
    o, h, low, c = bars.o, bars.h, bars.l, bars.c
    prev = np.r_[o[0], c[:-1]]
    gap, hr, lr, cr = o / prev, h / o, low / o, c / o
    p = rng.permutation(len(o))
    gap, hr, lr, cr = gap[p], hr[p], lr[p], cr[p]
    close = c[0] * np.cumprod(gap * cr)
    op = np.r_[c[0], close[:-1]] * gap
    return bx.Bars(bars.start, op, op * hr, op * lr, close)


def run(bars: bx.Bars, ticker: str, params: bx.Params, seed: int,
        areas: Optional[Dict[Any, bx.Box]] = None) -> List[BoxTrade]:
    """Every box trade on one stock's bars, with its twins; ``areas`` adds the weekly value-area levels."""
    rng = np.random.default_rng(seed)
    trades = []
    for s in bx.setups(bars, params):
        for t in bx.entries(bars, s, params):
            res = simulate(bars, t.bar, t.price, t.stop, t.target, t.direction,
                           bars.session_end(t.bar, params.life_sessions))
            if res is None:
                continue
            twin = twins(bars, t.bar, t.price, t.stop, t.target, t.direction, rng, params.life_sessions)
            when = str(bars.start[t.bar])[:19]
            trades.append(BoxTrade(
                ticker=ticker, kind=t.kind, box=t.box, impulse_t=params.impulse_t, tidy=params.tidy,
                entry_time=when, direction=t.direction, entry=t.price, stop=t.stop, target=t.target,
                exit=res["exit"], how=res["how"], R=res["R"], ret_pct=res["ret_pct"], twin_R=twin,
                T=float(bars.T[t.bar]), span=t.span, impulse_bar=s.i, entry_bar=t.bar, exit_bar=res["exit_bar"],
                break_bar=t.break_bar,
                levels=bx.prior_levels(areas, bars.start[t.bar]) if areas is not None else []))
    return trades


def month_t(values: Sequence[tuple]) -> tuple:
    """(mean, month-clustered t, months) of (month, value) pairs; t is None under two months."""
    by_month: Dict[str, List[float]] = {}
    for month, value in values:
        by_month.setdefault(month, []).append(value)
    means = [sum(v) / len(v) for v in by_month.values()]
    mean = sum(v for _, v in values) / len(values) if values else None
    t = None
    if len(means) >= 2:
        centre = sum(means) / len(means)
        spread = math.sqrt(sum((m - centre) ** 2 for m in means) / (len(means) - 1))
        t = centre / (spread / math.sqrt(len(means))) if spread > 0 else None
    return mean, t, len(means)


def summarize(trades: Sequence[BoxTrade]) -> Dict[str, Any]:
    """Counts, averages and month-clustered t of R and of the edge over the twins."""
    with_twin = [t for t in trades if not math.isnan(t.twin_R)]
    mean_r, t_r, months = month_t([(t.month, t.R) for t in trades])
    edge, t_edge, _ = month_t([(t.month, t.edge) for t in with_twin])
    return {"trades": len(trades), "months": months, "mean_R": mean_r, "t_R": t_r,
            "win_pct": sum(t.R > 0 for t in trades) / len(trades) * 100 if trades else None,
            "mean_ret_pct": sum(t.ret_pct for t in trades) / len(trades) if trades else None,
            "twin_R": sum(t.twin_R for t in with_twin) / len(with_twin) if with_twin else None,
            "edge": edge, "t_edge": t_edge}


def passes(by_period: Dict[str, Dict[str, Any]], needed: float) -> bool:
    """The methodology's bar: beats its twins with t >= ``needed`` and makes money, in every period."""
    return bool(by_period) and all(
        row["edge"] is not None and row["edge"] > 0 and row["t_edge"] is not None and row["t_edge"] >= needed
        and row["mean_R"] is not None and row["mean_R"] > 0 for row in by_period.values())


def placebo_gaps(trades: Sequence[BoxTrade], rng: np.random.Generator, draws: int = 200) -> List[float]:
    """Confirmed-minus-unconfirmed edge with each trade's two weeks of levels moved 1-3 T at random."""
    rows = [t for t in trades if len(t.levels) == 4 and not math.isnan(t.twin_R)]
    if not rows:
        return []
    T = np.array([t.T for t in rows])
    lo = np.array([t.span[0] for t in rows]) - 0.1 * T
    hi = np.array([t.span[1] for t in rows]) + 0.1 * T
    levels = np.array([t.levels for t in rows])
    edge = np.array([t.edge for t in rows])
    out = []
    for _ in range(draws):
        shift = rng.uniform(1, 3, (len(rows), 2)) * rng.choice([-1, 1], (len(rows), 2)) * T[:, None]
        moved = levels + np.repeat(shift, 2, axis=1)
        conf = ((moved >= lo[:, None]) & (moved <= hi[:, None])).any(axis=1)
        out.append(float(edge[conf].mean() - edge[~conf].mean()) if conf.any() and (~conf).any() else float("nan"))
    return out
