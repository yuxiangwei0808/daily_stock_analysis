"""Next earnings date per US ticker from Yahoo's calendar, cached for the day.

Funds and tickers without a published date return None. When Yahoo gives a
window (an unconfirmed date), the earliest day is used.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

# Swing holds run one to three weeks: earnings this close sit inside the trade.
INSIDE_HOLD_DAYS = 14
NOTE_DAYS = 30

_cache: Dict[Tuple[str, date], Optional[date]] = {}
_lock = threading.Lock()


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def next_earnings(ticker: str, today: date) -> Optional[date]:
    key = (ticker.upper(), today)
    with _lock:
        if key in _cache:
            return _cache[key]
    found = None
    yf_logger = logging.getLogger("yfinance")
    level = yf_logger.level
    try:
        import yfinance as yf
        yf_logger.setLevel(logging.CRITICAL)  # funds answer 404 ("no fundamentals"), which is expected
        calendar = yf.Ticker(ticker.upper().replace(".", "-")).calendar or {}
        raw = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
        dates = sorted(d for d in (_as_date(item) for item in (raw if isinstance(raw, (list, tuple)) else [raw]))
                       if d is not None and d >= today)
        found = dates[0] if dates else None
    except Exception as exc:  # funds have no calendar; a lookup failure only drops the note
        logger.debug("Earnings date unavailable for %s: %s", ticker, type(exc).__name__)
    finally:
        yf_logger.setLevel(level)
    with _lock:
        _cache[key] = found
    return found


def many(tickers: Iterable[str], today: date, lookup=next_earnings) -> Dict[str, Optional[date]]:
    from concurrent.futures import ThreadPoolExecutor
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(tickers)), thread_name_prefix="earnings") as pool:
        return dict(zip(tickers, pool.map(lambda ticker: lookup(ticker, today), tickers)))


def note(when: Optional[date], today: date) -> str:
    """A short warning inside the hold, a plain note within a month, else nothing."""
    if when is None:
        return ""
    days = (when - today).days
    label = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    if days <= INSIDE_HOLD_DAYS:
        return f"⚠️ Earnings {when:%b} {when.day} ({label}) — inside the hold: gap risk; size down or exit before"
    if days <= NOTE_DAYS:
        return f"Earnings {when:%b} {when.day} ({label})"
    return ""
