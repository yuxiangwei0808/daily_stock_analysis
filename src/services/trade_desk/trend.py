"""Deterministic swing-trend scoring from daily bars (no model calls).

A setup is scored for both directions and keeps the stronger one. Points
(0-100): moving-average alignment 30, MA slopes 20, 20-day breakout 25,
20-day momentum 15, volume 10; a price stretched more than 3 ATR past MA20
loses 10 (chasing risk). ``STRONG`` (70) or more is a strong trend.

Levels use ATR14: stop 1.5 ATR, targets 3 and 4.5 ATR (2R and 3R) from the
last price. They are starting points for the review, not orders.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time as dtime
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
STRONG = 70
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20e6
MIN_BARS = 60
# Share of a regular session's volume typically traded by N minutes after the
# open (U-shaped intraday profile); used to compare partial-day volume.
_VOLUME_CURVE = [(0, 0.0), (30, 0.13), (60, 0.21), (120, 0.34), (180, 0.45), (240, 0.55),
                 (300, 0.66), (360, 0.80), (390, 1.0)]


def volume_fraction(now: datetime) -> float:
    """Expected share of the day's volume already traded at ``now`` (1.0 outside the session)."""
    local = now.astimezone(_NEW_YORK)
    opened = datetime.combine(local.date(), dtime(9, 30), _NEW_YORK)
    minutes = (local - opened).total_seconds() / 60
    if minutes <= 0 or minutes >= 390:
        return 1.0
    for (m0, f0), (m1, f1) in zip(_VOLUME_CURVE, _VOLUME_CURVE[1:]):
        if minutes <= m1:
            return max(0.05, f0 + (f1 - f0) * (minutes - m0) / (m1 - m0))
    return 1.0


def _num(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _mean(values: List[float]) -> float:
    return sum(values) / len(values)


def score_bars(bars: List[Dict[str, float]], *, partial_fraction: float = 1.0) -> Optional[Dict[str, Any]]:
    """Score oldest-first daily bars (dicts with open/high/low/close/volume).

    ``partial_fraction`` < 1 marks the last bar as today's unfinished session;
    its volume is scaled up by the typical intraday profile before comparing.
    Returns None when history is too short or the stock is illiquid.
    """
    rows = [bar for bar in bars if all(_num(bar.get(key)) is not None for key in ("high", "low", "close"))]
    if len(rows) < MIN_BARS:
        return None
    closes = [float(bar["close"]) for bar in rows]
    highs = [float(bar["high"]) for bar in rows]
    lows = [float(bar["low"]) for bar in rows]
    volumes = [max(0.0, _num(bar.get("volume")) or 0.0) for bar in rows]
    close = closes[-1]
    if close < MIN_PRICE:
        return None
    avg_volume = _mean(volumes[-51:-1])
    dollar_volume = _mean([c * v for c, v in zip(closes[-21:-1], volumes[-21:-1])])
    if dollar_volume < MIN_DOLLAR_VOLUME or avg_volume <= 0:
        return None

    def ma(n, end=0):
        window = closes[len(closes) - n - end:len(closes) - end]
        return _mean(window)

    ma20, ma50 = ma(20), ma(50)
    ma20_slope = (ma20 / ma(20, 5) - 1) * 100
    ma50_slope = (ma50 / ma(50, 10) - 1) * 100
    true_ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                   for i in range(len(rows) - 14, len(rows))]
    atr = _mean(true_ranges)
    high20, low20 = max(highs[-21:-1]), min(lows[-21:-1])  # prior 20 sessions, excluding the last bar
    momentum = (close / closes[-21] - 1) * 100
    last_volume = volumes[-1] / max(0.05, min(1.0, partial_fraction))
    volume_ratio = last_volume / avg_volume

    def side(sign: int) -> Dict[str, Any]:
        points, notes = 0, []
        above = (lambda a, b: a > b) if sign > 0 else (lambda a, b: a < b)
        if above(close, ma20) and above(ma20, ma50):
            points += 30
            notes.append("price, MA20 and MA50 stacked" + (" up" if sign > 0 else " down"))
        elif above(close, ma20) and above(close, ma50):
            points += 15
        for slope, full, label in ((ma20_slope, 1.0, "MA20"), (ma50_slope, 1.0, "MA50")):
            if sign * slope >= full:
                points += 10
            elif sign * slope > 0:
                points += 5
        level = high20 if sign > 0 else low20
        if above(close, level) or close == level:
            points += 25
            notes.append(f"{'broke above' if sign > 0 else 'broke below'} 20-day {'high' if sign > 0 else 'low'} {level:.2f}")
        elif abs(close / level - 1) <= 0.02:
            points += 12
            notes.append(f"within 2% of 20-day {'high' if sign > 0 else 'low'} {level:.2f}")
        move = sign * momentum
        points += 15 if move >= 10 else 10 if move >= 5 else 5 if move > 0 else 0
        if move > 0:
            notes.append(f"{momentum:+.1f}% over 20 days")
        if volume_ratio >= 1.5:
            points += 10
            notes.append(f"volume {volume_ratio:.1f}x average")
        elif volume_ratio >= 1.2:
            points += 5
        extended = atr > 0 and sign * (close - ma20) > 3 * atr
        if extended:
            points -= 10
            notes.append(f"stretched {abs(close - ma20) / atr:.1f} ATR from MA20 (chasing risk)")
        stop = close - sign * 1.5 * atr
        return {"direction": "long" if sign > 0 else "short", "strength": max(0, min(100, points)),
                "notes": notes, "extended": extended, "trigger": round(level, 2),
                "stop": round(stop, 2), "targets": [round(close + sign * 3 * atr, 2), round(close + sign * 4.5 * atr, 2)]}

    best = max((side(1), side(-1)), key=lambda item: item["strength"])
    return {**best, "price": round(close, 2), "ma20": round(ma20, 2), "ma50": round(ma50, 2),
            "ma20_slope_pct": round(ma20_slope, 2), "ma50_slope_pct": round(ma50_slope, 2),
            "atr": round(atr, 2), "atr_pct": round(atr / close * 100, 2), "high20": round(high20, 2),
            "low20": round(low20, 2), "momentum20_pct": round(momentum, 2), "volume_ratio": round(volume_ratio, 2),
            "avg_volume": round(avg_volume), "dollar_volume_m": round(dollar_volume / 1e6, 1)}


def recent_bars(bars: List[Dict[str, float]], count: int = 20) -> List[List[Any]]:
    """Compact [date, open, high, low, close, volume] rows for the model prompt."""
    return [[str(bar.get("date", ""))[:10], *(round(float(bar[key]), 2) for key in ("open", "high", "low", "close")),
             int(_num(bar.get("volume")) or 0)] for bar in bars[-count:]]


def download_bars(tickers: Iterable[str], *, period: str = "6mo", chunk: int = 100) -> Dict[str, List[Dict[str, Any]]]:
    """Daily bars per ticker (oldest first) from yfinance, in batched downloads."""
    import yfinance as yf

    symbols = list(dict.fromkeys(str(t).strip().upper().replace(".", "-") for t in tickers if str(t).strip()))
    result: Dict[str, List[Dict[str, Any]]] = {}
    for start in range(0, len(symbols), chunk):
        part = symbols[start:start + chunk]
        try:
            data = yf.download(part, period=period, interval="1d", group_by="ticker", auto_adjust=True,
                               progress=False, threads=True, timeout=20)
        except Exception as exc:  # one failed chunk leaves the others usable
            logger.warning("Daily bars unavailable for %d tickers: %s", len(part), type(exc).__name__)
            continue
        if data is None or data.empty:
            continue
        for symbol in part:
            try:
                frame = data[symbol] if len(part) > 1 or symbol in data.columns.get_level_values(0) else data
            except KeyError:
                continue
            frame = frame.dropna(subset=["Close"])
            if frame.empty:
                continue
            result[symbol.replace("-", ".")] = [
                {"date": index.date().isoformat(), "open": float(row["Open"]), "high": float(row["High"]),
                 "low": float(row["Low"]), "close": float(row["Close"]), "volume": float(row["Volume"] or 0)}
                for index, row in frame.iterrows()]
    return result
