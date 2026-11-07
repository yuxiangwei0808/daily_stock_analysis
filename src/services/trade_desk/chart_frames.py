"""Chart pictures by timeframe for the Trade Desk models: the bars, then chart_image.py draws them.

Bars come from moomoo's history (``request_history_kline``: read only, no subscription; the same data as the
Chart tab), or from Yahoo when OpenD can't be reached. Each timeframe fetches enough history for the NX
tunnels to settle (EMA 89 seeded with the first bar) and draws its recent part. moomoo has no 4-hour bars:
they are built from 1-hour bars, 09:30-13:30 and 13:30-16:00 New York time. Times are each bar's start.

``TRADE_DESK_CHART_FRAMES`` (default ``15m,1h,4h``) is what holding questions, reports and scans get;
Ask about a stock adds ``1d``; shadow mode has its own list (``TRADE_ASSISTANT_CHART_FRAMES``).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from . import chart_image

logger = logging.getLogger(__name__)
_NEW_YORK = ZoneInfo("America/New_York")

# minutes per bar, the moomoo K-line type, calendar days of history to fetch, bars drawn, time format
FRAMES: Dict[str, Dict[str, Any]] = {
    "1m": {"minutes": 1, "ktype": "K_1M", "days": 3, "visible": 390, "format": "%H:%M", "name": "1-minute"},
    "5m": {"minutes": 5, "ktype": "K_5M", "days": 10, "visible": 156, "format": "%m-%d %H:%M", "name": "5-minute"},
    "15m": {"minutes": 15, "ktype": "K_15M", "days": 30, "visible": 130, "format": "%m-%d %H:%M", "name": "15-minute"},
    "1h": {"minutes": 60, "ktype": "K_60M", "days": 120, "visible": 140, "format": "%m-%d", "name": "1-hour"},
    "4h": {"minutes": 240, "ktype": "K_60M", "days": 400, "visible": 120, "format": "%m-%d", "name": "4-hour"},
    "1d": {"minutes": 0, "ktype": "K_DAY", "days": 800, "visible": 125, "format": "%Y-%m-%d", "name": "daily"},
}
YAHOO = {"5m": ("5m", "10d"), "15m": ("15m", "30d"), "1h": ("60m", "120d"), "4h": ("60m", "400d"), "1d": ("1d", "2y")}
CACHE_SECONDS = 120
_cache: Dict[Tuple[str, str, bool], Tuple[float, List[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()
_fetching = threading.BoundedSemaphore(2)  # reports run stocks in parallel: at most two history reads at once


def frames(variable: str = "TRADE_DESK_CHART_FRAMES", default: str = "15m,1h,4h") -> List[str]:
    raw = (os.getenv(variable, "") or "").strip() or default
    return [name for name in (part.strip() for part in raw.split(",")) if name in FRAMES]


def _start_times(rows: Sequence[Dict[str, Any]], minutes: int) -> List[Dict[str, Any]]:
    """moomoo stamps a bar with its end; the pictures use starts (a daily bar is its date)."""
    out = []
    for row in rows:
        stamp = datetime.fromisoformat(str(row["time"]))
        stamp = stamp.replace(tzinfo=_NEW_YORK) if stamp.tzinfo is None else stamp.astimezone(_NEW_YORK)
        if minutes:
            stamp -= timedelta(minutes=minutes)
            # the session's last hour bar ends at 16:00 and starts at 15:30
            if minutes == 60 and stamp.hour == 15 and stamp.minute == 0 and row.get("_end_minute") == 0:
                stamp = stamp.replace(minute=30)
        out.append({"time": stamp.isoformat(), **{k: float(row[k]) for k in ("open", "high", "low", "close")},
                    "volume": float(row.get("volume") or 0)})
    return out


def four_hour(hours: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """1-hour bars (start times) into 4-hour bars of the regular session: 09:30-13:30 and 13:30-16:00."""
    out: List[Dict[str, Any]] = []
    for bar in hours:
        start = chart_image._when(bar["time"])
        half = 0 if (start.hour, start.minute) < (13, 30) else 1
        key = (start.date(), half)
        if out and out[-1]["_key"] == key:
            last = out[-1]
            last.update(high=max(last["high"], bar["high"]), low=min(last["low"], bar["low"]), close=bar["close"],
                        volume=last["volume"] + bar["volume"])
        else:
            anchor = start.replace(hour=9 if not half else 13, minute=30, second=0, microsecond=0)
            out.append({"_key": key, "time": anchor.isoformat(), "open": bar["open"], "high": bar["high"],
                        "low": bar["low"], "close": bar["close"], "volume": bar["volume"]})
    for row in out:
        row.pop("_key")
    return out


def _regular(row: Dict[str, Any]) -> bool:
    start = chart_image._when(row["time"])
    return (9, 30) <= (start.hour, start.minute) < (16, 0)


def _moomoo(provider: Any, ticker: str, frame: str, now: datetime) -> List[Dict[str, Any]]:
    spec = FRAMES[frame]
    start = (now - timedelta(days=spec["days"])).astimezone(_NEW_YORK).date().isoformat()
    end = now.astimezone(_NEW_YORK).date().isoformat()
    rows = provider.history_bars(f"US.{ticker}", start, end, spec["ktype"])
    rows = [{"time": row["time_key"], **{k: row.get(k) for k in ("open", "high", "low", "close", "volume")},
             "_end_minute": datetime.fromisoformat(str(row["time_key"])).minute}
            for row in rows if all(row.get(k) is not None for k in ("open", "high", "low", "close"))]
    minutes = 60 if frame == "4h" else spec["minutes"]
    bars = _start_times(rows, minutes)
    if minutes:
        bars = [bar for bar in bars if _regular(bar)]
    return four_hour(bars) if frame == "4h" else bars


def _yahoo(ticker: str, frame: str) -> List[Dict[str, Any]]:
    import yfinance as yf
    interval, period = YAHOO[frame]
    data = yf.download(ticker.replace(".", "-"), period=period, interval=interval, auto_adjust=True, prepost=False,
                       progress=False, threads=False, timeout=20)
    if data is None or data.empty:
        return []
    if hasattr(data.columns, "levels") and len(data.columns.levels) > 1:
        data = data.droplevel(1, axis=1)
    bars = []
    for index, row in data.dropna(subset=["Close"]).iterrows():
        stamp = index.to_pydatetime()
        stamp = stamp.replace(tzinfo=_NEW_YORK) if stamp.tzinfo is None else stamp.astimezone(_NEW_YORK)
        bars.append({"time": (stamp.date().isoformat() if frame == "1d" else stamp.isoformat()),
                     **{k.lower(): float(row[k]) for k in ("Open", "High", "Low", "Close")}, "volume": float(row.get("Volume") or 0)})
    return four_hour(bars) if frame == "4h" else bars


def bars(ticker: str, frame: str, *, provider: Any = None, now: Optional[datetime] = None,
         fallback: bool = True) -> List[Dict[str, Any]]:
    """Bars of one timeframe (oldest first, start times), from moomoo when ``provider`` can read history, else
    Yahoo (unless ``fallback`` is off: a live session draws only moomoo's bars)."""
    now = now or datetime.now(_NEW_YORK)
    key = (ticker, frame, fallback)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    result: List[Dict[str, Any]] = []
    with _fetching:
        if provider is not None and callable(getattr(provider, "history_bars", None)):
            try:
                result = _moomoo(provider, ticker, frame, now)
            except Exception as exc:  # Yahoo next
                logger.info("Chart bars %s %s from moomoo failed: %s", ticker, frame, type(exc).__name__)
        if not result and fallback and frame in YAHOO:
            try:
                result = _yahoo(ticker, frame)
            except Exception as exc:  # no picture for this timeframe
                logger.info("Chart bars %s %s from Yahoo failed: %s", ticker, frame, type(exc).__name__)
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return result


def boxes_from(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The Chart tab's box rows as picture boxes."""
    return [{"label": f"{row.get('box', 'box')} box", "low": row["bottom"], "high": row["top"],
             "start": row.get("from"), "end": row.get("until") or row.get("to")} for row in rows
            if row.get("bottom") is not None and row.get("top") is not None]


def starts(rows: Sequence[Dict[str, Any]], minutes: int) -> List[Dict[str, Any]]:
    """Bars stamped with their end (OpenD's K-lines, the session's 1-minute bars) restamped with their start."""
    return [{**{k: row[k] for k in ("open", "high", "low", "close")}, "volume": float(row.get("volume") or 0),
             "time": (chart_image._when(row["time"]) - timedelta(minutes=minutes)).isoformat()} for row in rows]


def overlays(rows15: Sequence[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """The Chart tab's boxes, value areas and prior-day levels, from 15-minute bars (start times)."""
    from . import boxes as bx
    from .chart import _overlays
    rows = [{**bar, "time": (chart_image._when(bar["time"]) + timedelta(minutes=15)).isoformat(), "partial": False}
            for bar in rows15]
    try:
        found = _overlays(rows, bx.Params(), now, [], cut=False)
    except Exception as exc:  # the pictures go without boxes
        logger.info("Chart overlays failed: %s", type(exc).__name__)
        return {"boxes": [], "levels": []}
    return {"boxes": boxes_from(found.get("boxes") or []), "levels": [{"label": lv["label"], "price": lv["price"], "kind": lv["kind"]}
                                       for lv in found.get("levels") or []]}


def picture(ticker: str, frame: str, rows: Sequence[Dict[str, Any]], *, levels: Sequence[Dict[str, Any]] = (),
            boxes: Sequence[Dict[str, Any]] = (), title: str = "") -> Optional[Dict[str, str]]:
    """One timeframe's picture as an image entry, or None when there is nothing to draw."""
    spec = FRAMES[frame]
    if len(rows) < 2:
        return None
    try:
        png = chart_image.render(
            rows, title=title or f"{ticker} {spec['name']} bars (New York time), last {rows[-1]['close']:g}",
            levels=levels, boxes=boxes, visible=spec["visible"], nx=True, cd=True, vrvp=True,
            vwap=frame in ("1m", "5m", "15m"), mas=(20, 50, 200) if frame == "1d" else (), time_format=spec["format"])
    except Exception as exc:  # the question goes on without this picture
        logger.info("Chart picture %s %s failed: %s", ticker, frame, type(exc).__name__)
        return None
    return chart_image.as_image(png)


def pictures(ticker: str, wanted: Sequence[str], *, provider: Any = None, levels: Sequence[Dict[str, Any]] = (),
             now: Optional[datetime] = None) -> List[Dict[str, str]]:
    """Pictures of ``ticker`` in each of ``wanted`` (at most four), with the Chart tab's boxes on the 15-minute
    one and its value areas and prior-day levels plus ``levels`` on all. Empty when pictures are off."""
    from src.llm.generation_backend import MAX_GENERATION_IMAGES
    if not chart_image.enabled():
        return []
    now = now or datetime.now(_NEW_YORK)
    wanted = [frame for frame in wanted if frame in FRAMES and frame != "1m"][:MAX_GENERATION_IMAGES]
    fetched = {frame: bars(ticker, frame, provider=provider, now=now) for frame in wanted}
    marks = {"boxes": [], "levels": []}
    rows15 = fetched.get("15m") or (bars(ticker, "15m", provider=provider, now=now) if wanted else [])
    if rows15:
        marks = overlays(rows15, now)
    out = []
    for frame in wanted:
        image = picture(ticker, frame, fetched[frame], levels=[*marks["levels"], *levels],
                        boxes=marks["boxes"] if frame == "15m" else ())
        if image:
            out.append(image)
    return out


def with_provider(fn: Callable[[Any], Any]) -> Any:
    """Run ``fn`` with a short-lived moomoo connection (None when OpenD can't be reached; Yahoo is used then)."""
    provider = None
    try:
        from .providers import build_provider
        provider = build_provider("live")
    except Exception as exc:
        logger.info("Chart pictures without moomoo: %s", type(exc).__name__)
    try:
        return fn(provider)
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass
