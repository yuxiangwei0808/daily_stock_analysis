"""Price levels shared by the live watchers and the backtests: ATR and the breakout reference levels."""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

ATR_PERIOD = 14


def atr(bars: Sequence[Dict[str, Any]], index: Optional[int] = None, period: int = ATR_PERIOD) -> float:
    """Mean true range of the ``period`` bars ending at ``index`` (default: the last bar); 0.0 without history."""
    end = len(bars) - 1 if index is None else index
    start = max(1, end - period + 1)
    ranges = [max(float(bars[i]["high"]) - float(bars[i]["low"]),
                  abs(float(bars[i]["high"]) - float(bars[i - 1]["close"])),
                  abs(float(bars[i]["low"]) - float(bars[i - 1]["close"]))) for i in range(start, end + 1)]
    return sum(ranges) / len(ranges) if ranges else 0.0


def breakout_levels(bars: List[Dict[str, Any]], day: date) -> Optional[Dict[str, float]]:
    """Prior 20-session high/low, MA50, ATR and average volume as of ``day`` (today's bar excluded)."""
    history = [bar for bar in bars if str(bar.get("date", ""))[:10] < day.isoformat()]
    if len(history) < 50:
        return None
    closes = [bar["close"] for bar in history]
    highs, lows = [bar["high"] for bar in history], [bar["low"] for bar in history]
    volumes = [volume if isinstance(volume, (int, float)) and math.isfinite(volume) else 0
               for volume in (bar.get("volume") for bar in history[-50:])]
    return {"high20": max(highs[-20:]), "low20": min(lows[-20:]), "ma50": sum(closes[-50:]) / 50,
            "atr": atr(history), "avg_volume": sum(volumes) / len(volumes)}
