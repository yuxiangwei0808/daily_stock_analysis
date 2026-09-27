"""Day-trading backtests on intraday bars (research only; nothing is traded).

Strategies, fixed before their results were seen; every trade is flat by the close:

- ``orb30``: first bar closing beyond the first 30 minutes' range; enter at the
  next open, stop at the other side of the range, else exit at the close.
- ``gap_go`` / ``gap_fade``: open at least 2% away from the prior close; enter at
  the 09:35 open with / against the gap, exit at the close.
- ``move_follow`` / ``move_fade``: the day change first crossing +/-3% from 09:45
  on (the market-pulse alert); enter at the next open with / against it.
- ``breakout_day``: the live breakout alert (0.15 ATR past the prior 20-day
  high/low, right side of MA50, volume pace >= 1.3, two consecutive bars),
  entered at the next open and held to the close.
- ``momentum_last30``: the return from the prior close to 10:00 sets the
  direction of a 15:30 -> 16:00 trade (intraday momentum).

Each trade gets random twins: the same ticker, side, entry time of day and exit
rule (an ORB stop at the same distance) on random other days of the sample, so
the baseline carries the ticker's drift and the time-of-day effect but not the
signal. ``COST_BPS`` per side.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from . import trend

NEW_YORK = ZoneInfo("America/New_York")
COST_BPS = 5.0
GAP_PCT, MOVE_PCT = 2.0, 3.0
ORB_MINUTES = 30
FIRST_ENTRY, LAST_ENTRY = dtime(9, 45), dtime(15, 30)
STRATEGIES = ("orb30", "gap_go", "gap_fade", "move_follow", "move_fade", "breakout_day", "momentum_last30")


@dataclass
class DayTrade:
    strategy: str
    ticker: str
    day: str
    direction: str
    entry_time: str
    entry: float
    exit: float
    reason: str
    signal_day: str = ""  # for twins: the day of the trade they mirror

    @property
    def return_pct(self) -> float:
        sign = 1 if self.direction == "long" else -1
        return sign * (self.exit / self.entry - 1) * 100 - 2 * COST_BPS / 100

    @property
    def gross_pct(self) -> float:
        sign = 1 if self.direction == "long" else -1
        return sign * (self.exit / self.entry - 1) * 100


def sessions(bars: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Regular-session bars grouped by New York date; each bar has ``t`` (datetime NY)."""
    days: Dict[str, List[Dict[str, Any]]] = {}
    for bar in bars:
        stamp = bar["t"].astimezone(NEW_YORK)
        if dtime(9, 30) <= stamp.time() < dtime(16, 0):
            days.setdefault(stamp.date().isoformat(), []).append({**bar, "t": stamp})
    return {day: sorted(rows, key=lambda b: b["t"]) for day, rows in sorted(days.items())}


def _run(ticker: str, strategy: str, day: str, bars: List[Dict[str, Any]], index: int, direction: str,
         stop: Optional[float] = None, exit_index: Optional[int] = None) -> Optional[DayTrade]:
    """Enter at bars[index] open; exit at the stop (a gap fills at the open) or at the close of the last bar."""
    if index >= len(bars):
        return None
    entry = float(bars[index]["open"])
    if entry <= 0:
        return None
    sign = 1 if direction == "long" else -1
    last = len(bars) - 1 if exit_index is None else exit_index
    for i in range(index, last + 1):
        bar = bars[i]
        if stop is not None and ((bar["low"] <= stop) if sign > 0 else (bar["high"] >= stop)):
            gapped = (bar["open"] <= stop) if sign > 0 else (bar["open"] >= stop)
            price = bar["open"] if gapped and i > index else stop
            return DayTrade(strategy, ticker, day, direction, bars[index]["t"].strftime("%H:%M"), entry, float(price),
                            "stop")
    return DayTrade(strategy, ticker, day, direction, bars[index]["t"].strftime("%H:%M"), entry,
                    float(bars[last]["close"]), "close")


def _first_index_at(bars: List[Dict[str, Any]], moment: dtime) -> Optional[int]:
    return next((i for i, bar in enumerate(bars) if bar["t"].time() >= moment), None)


def day_signals(ticker: str, day: str, bars: List[Dict[str, Any]], prev_close: Optional[float],
                levels: Optional[Dict[str, float]] = None) -> List[DayTrade]:
    """All strategy trades for one ticker-day."""
    trades: List[DayTrade] = []
    if len(bars) < 40 or not prev_close:
        return trades
    # Opening-range breakout
    k = _orb_bars(bars)
    high, low = max(b["high"] for b in bars[:k]), min(b["low"] for b in bars[:k])
    for i in range(k, len(bars) - 1):
        if bars[i]["t"].time() >= LAST_ENTRY:
            break
        if bars[i]["close"] > high or bars[i]["close"] < low:
            up = bars[i]["close"] > high
            trade = _run(ticker, "orb30", day, bars, i + 1, "long" if up else "short", stop=low if up else high)
            if trade:
                trades.append(trade)
            break
    # Gaps
    gap = (bars[0]["open"] / prev_close - 1) * 100
    if abs(gap) >= GAP_PCT:
        with_gap = "long" if gap > 0 else "short"
        against = "short" if gap > 0 else "long"
        for name, direction in (("gap_go", with_gap), ("gap_fade", against)):
            trade = _run(ticker, name, day, bars, 1, direction)
            if trade:
                trades.append(trade)
    # Big move (the pulse alert's first level)
    start = _first_index_at(bars, FIRST_ENTRY)
    if start is not None:
        for i in range(start, len(bars) - 1):
            if bars[i]["t"].time() >= LAST_ENTRY:
                break
            change = (bars[i]["close"] / prev_close - 1) * 100
            if abs(change) >= MOVE_PCT:
                follow = "long" if change > 0 else "short"
                fade = "short" if change > 0 else "long"
                for name, direction in (("move_follow", follow), ("move_fade", fade)):
                    trade = _run(ticker, name, day, bars, i + 1, direction)
                    if trade:
                        trades.append(trade)
                break
    # Live breakout alert as a day trade
    if levels and start is not None:
        cumulative, streak = 0.0, {"long": 0, "short": 0}
        for i, bar in enumerate(bars):
            cumulative += float(bar.get("volume") or 0)
            if i < start:
                continue
            if bar["t"].time() >= LAST_ENTRY:
                break
            end_of_bar = bar["t"] + (bars[1]["t"] - bars[0]["t"])
            pace = cumulative / (levels["avg_volume"] * trend.volume_fraction(end_of_bar)) if levels["avg_volume"] else 0
            margin = 0.15 * levels["atr"]
            hits = {"long": bar["close"] > levels["high20"] + margin and bar["close"] > levels["ma50"],
                    "short": bar["close"] < levels["low20"] - margin and bar["close"] < levels["ma50"]}
            fired = None
            for direction, hit in hits.items():
                streak[direction] = streak[direction] + 1 if hit and pace >= 1.3 else 0
                if streak[direction] >= 2:
                    fired = direction
            if fired:
                trade = _run(ticker, "breakout_day", day, bars, i + 1, fired)
                if trade:
                    trades.append(trade)
                break
    trade = momentum_trade(ticker, day, bars, prev_close)
    if trade:
        trades.append(trade)
    return trades


def momentum_trade(ticker: str, day: str, bars: List[Dict[str, Any]], prev_close: Optional[float]) -> Optional[DayTrade]:
    """Intraday momentum: prior close -> the first bar(s) up to 10:00 set the direction of the 15:30 -> close trade.

    On hourly bars the first bar ends at 10:30, so the signal is the first hour.
    """
    if not bars or not prev_close:
        return None
    first_after = _first_index_at(bars, dtime(10, 0))
    last_half = _first_index_at(bars, dtime(15, 30))
    if last_half is None:
        return None
    signal_bar = bars[0] if first_after in (None, 0) else bars[first_after - 1]
    early = signal_bar["close"] / prev_close - 1
    if not early:
        return None
    return _run(ticker, "momentum_last30", day, bars, last_half, "long" if early > 0 else "short")


def twins(trade: DayTrade, days: Dict[str, List[Dict[str, Any]]], rng: random.Random,
          draws: int = 5) -> List[DayTrade]:
    """The same ticker, side, entry time of day and exit rule on random other days.

    Same-day random entries would leak the signal day's path (a +3% day favours any
    long entered before the move), so the baseline comes from days without the signal.
    """
    others = [d for d in days if d != trade.day]
    stop_pct = None
    if trade.strategy == "orb30":
        bars = days[trade.day]
        k = _orb_bars(bars)
        stop_pct = (max(b["high"] for b in bars[:k]) - min(b["low"] for b in bars[:k])) / trade.entry
    moment = datetime.strptime(trade.entry_time, "%H:%M").time()
    out = []
    for _ in range(draws):
        if not others:
            break
        day = rng.choice(others)
        bars = days[day]
        index = _first_index_at(bars, moment)
        if index is None or bars[index]["t"].strftime("%H:%M") != trade.entry_time:
            continue  # a short session or a missing bar
        stop = None
        if stop_pct is not None:
            entry = bars[index]["open"]
            stop = entry * (1 - stop_pct) if trade.direction == "long" else entry * (1 + stop_pct)
        twin = _run(trade.ticker, "random:" + trade.strategy, day, bars, index, trade.direction, stop=stop)
        if twin:
            twin.signal_day = trade.day
            out.append(twin)
    return out


def _orb_bars(bars: List[Dict[str, Any]]) -> int:
    minutes = int((bars[1]["t"] - bars[0]["t"]).total_seconds() // 60) or 5
    return max(1, ORB_MINUTES // minutes)


def run(days_by_ticker: Dict[str, Dict[str, List[Dict[str, Any]]]],
        levels_by_ticker_day: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
        seed: int = 5, momentum_only: bool = False) -> List[DayTrade]:
    """Strategy trades plus their random twins over every ticker-day (the first day only sets a prior close)."""
    rng = random.Random(seed)
    trades: List[DayTrade] = []
    for ticker, days in days_by_ticker.items():
        prev_close = None
        for day, bars in days.items():
            levels = ((levels_by_ticker_day or {}).get(ticker) or {}).get(day)
            found = (day_signals(ticker, day, bars, prev_close, levels) if not momentum_only
                     else [t for t in [momentum_trade(ticker, day, bars, prev_close)] if t])
            for trade in found:
                trades.append(trade)
                trades.extend(twins(trade, days, rng))
            prev_close = bars[-1]["close"] if bars else prev_close
    return trades


def summarize(trades: Sequence[DayTrade]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    returns = [t.return_pct for t in trades]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else 0.0
    return {"trades": len(trades), "days": len({t.day for t in trades}),
            "win_rate": sum(1 for r in returns if r > 0) / len(returns) * 100,
            "avg_net_pct": mean, "avg_gross_pct": sum(t.gross_pct for t in trades) / len(trades),
            "t_stat": mean / (sd / math.sqrt(len(returns))) if sd else 0.0,
            "stopped_pct": sum(1 for t in trades if t.reason == "stop") / len(trades) * 100}


def levels_for(daily: List[Dict[str, Any]], day: str) -> Optional[Dict[str, float]]:
    """Prior 20-day high/low, MA50, ATR14 and 50-day average volume before ``day``."""
    from .opportunities import breakout_levels
    return breakout_levels(daily, date.fromisoformat(day))


def to_bar(stamp: datetime, o: float, h: float, low: float, c: float, v: float) -> Dict[str, Any]:
    return {"t": stamp, "open": float(o), "high": float(h), "low": float(low), "close": float(c), "volume": float(v)}
