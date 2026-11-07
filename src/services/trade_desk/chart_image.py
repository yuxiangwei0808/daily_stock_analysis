"""A chart picture for the models (Pillow): candles, volume, VWAP and the levels and boxes the state names.

The models already get every price as a number; the picture adds the shape (trend, ranges, how the price
reached a level). It shows only what the system computed, labelled as on the Chart tab, and the prompt
tells the model to take every price it writes from the numbers. ``TRADE_DESK_CHART_IMAGES=false``
sends no picture.
"""
from __future__ import annotations

import base64
import io
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

_NEW_YORK = ZoneInfo("America/New_York")
WIDTH, HEIGHT = 1200, 720
UP, DOWN = (22, 163, 74), (220, 38, 38)
INK, GRID, MUTED = (17, 24, 39), (229, 231, 235), (107, 114, 128)
KIND_COLORS = {"vwap": (124, 58, 237), "premarket_high": (234, 88, 12), "premarket_low": (234, 88, 12),
               "prior_high": (75, 85, 99), "prior_low": (75, 85, 99), "prior_close": (75, 85, 99),
               "stop": (220, 38, 38), "target": (22, 163, 74), "trigger": (37, 99, 235)}
DEFAULT_LEVEL = (8, 145, 178)


def enabled() -> bool:
    return (os.getenv("TRADE_DESK_CHART_IMAGES", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}


def as_image(png: bytes) -> Dict[str, str]:
    """The ``generation_config["images"]`` entry for a PNG."""
    return {"mime_type": "image/png", "data": base64.standard_b64encode(png).decode("ascii")}


def _when(value: Any) -> datetime:
    """A bar's time in New York; a bare date or a time without a zone is New York's (daily rows)."""
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return when.replace(tzinfo=_NEW_YORK) if when.tzinfo is None else when.astimezone(_NEW_YORK)


def aggregate(bars: Sequence[Dict[str, Any]], minutes: int) -> List[Dict[str, Any]]:
    """1-minute bars (``time`` = the bar's start) grouped into ``minutes`` bars of the same clock."""
    out: List[Dict[str, Any]] = []
    for bar in bars:
        local = _when(bar["time"])
        slot = local.replace(minute=local.minute - local.minute % minutes, second=0, microsecond=0)
        if out and out[-1]["slot"] == slot:
            last = out[-1]
            last.update(high=max(last["high"], bar["high"]), low=min(last["low"], bar["low"]), close=bar["close"],
                        volume=last["volume"] + (bar.get("volume") or 0))
        else:
            out.append({"slot": slot, "time": slot, "open": bar["open"], "high": bar["high"], "low": bar["low"],
                        "close": bar["close"], "volume": bar.get("volume") or 0})
    for row in out:
        row.pop("slot")
    return out


def vwap_series(bars: Sequence[Dict[str, Any]]) -> List[Optional[float]]:
    total = weight = 0.0
    out: List[Optional[float]] = []
    for bar in bars:
        volume = float(bar.get("volume") or 0)
        total += (bar["high"] + bar["low"] + bar["close"]) / 3 * volume
        weight += volume
        out.append(total / weight if weight else None)
    return out


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1
        return ImageFont.load_default()


def _ticks(low: float, high: float, count: int = 6) -> List[float]:
    span = high - low
    if span <= 0:
        return [low]
    raw = span / count
    magnitude = 10 ** int(f"{raw:e}".split("e")[1])
    step = min((m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw), default=raw)
    first = (low // step + 1) * step
    return [round(first + i * step, 6) for i in range(int((high - first) / step) + 1)]


def render(bars: Sequence[Dict[str, Any]], *, title: str, levels: Iterable[Dict[str, Any]] = (),
           boxes: Iterable[Dict[str, Any]] = (), vwap: bool = True, time_format: str = "%H:%M",
           width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """A PNG of ``bars`` (oldest first) with ``levels`` (``label``, ``price``, ``kind``) and ``boxes``
    (``label``, ``low``, ``high``, optional ``start``/``end`` times). Levels far outside the bars' range are
    listed under the title instead of squeezing the candles."""
    from PIL import Image, ImageDraw
    if not bars:
        raise ValueError("no bars to draw")
    levels = [level for level in levels if isinstance(level.get("price"), (int, float))]
    boxes = [box for box in boxes if isinstance(box.get("low"), (int, float)) and isinstance(box.get("high"), (int, float))]
    lo = min(bar["low"] for bar in bars)
    hi = max(bar["high"] for bar in bars)
    margin = max((hi - lo) * 0.6, hi * 0.004)
    near = [level for level in levels if lo - margin <= level["price"] <= hi + margin]
    far = [level for level in levels if level not in near]
    for value in [level["price"] for level in near] + [v for box in boxes for v in (box["low"], box["high"])
                                                         if lo - margin <= v <= hi + margin]:
        lo, hi = min(lo, value), max(hi, value)
    pad = (hi - lo) * 0.04 or hi * 0.002
    lo, hi = lo - pad, hi + pad

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image, "RGBA")
    small, label_font, title_font = _font(13), _font(14), _font(18)
    left, right, top = 12, width - 96, 58
    price_bottom = int(height * 0.74)
    vol_top, vol_bottom = price_bottom + 14, height - 34

    def y(price: float) -> float:
        return top + (hi - price) / (hi - lo) * (price_bottom - top)

    step = (right - left) / len(bars)
    def x(index: float) -> float:
        return left + (index + 0.5) * step

    draw.text((left, 10), title, fill=INK, font=title_font)
    if far:
        draw.text((left, 34), "Off the chart: " + "; ".join(f"{lv['label']} {lv['price']:g}" for lv in far[:6]),
                  fill=MUTED, font=small)
    for tick in _ticks(lo, hi):
        draw.line([(left, y(tick)), (right, y(tick))], fill=GRID, width=1)
        draw.text((right + 6, y(tick) - 7), f"{tick:g}", fill=MUTED, font=small)

    times = [_when(bar["time"]) for bar in bars]
    for box in boxes:
        start = box.get("start")
        end = box.get("end")
        i0 = 0 if start is None else next((i for i, t in enumerate(times) if t >= _when(start)), len(bars) - 1)
        i1 = len(bars) - 1 if end is None else max(i0, max((i for i, t in enumerate(times) if t <= _when(end)), default=i0))
        x0, x1 = x(i0) - step / 2, x(i1) + step / 2
        draw.rectangle([(x0, y(box["high"])), (x1, y(box["low"]))], fill=(14, 165, 233, 40), outline=(14, 165, 233, 160))
        draw.text((x0 + 3, y(box["high"]) - 15), str(box.get("label") or "box"), fill=(3, 105, 161), font=small)

    body = max(1.0, step * 0.6)
    for i, bar in enumerate(bars):
        color = UP if bar["close"] >= bar["open"] else DOWN
        draw.line([(x(i), y(bar["high"])), (x(i), y(bar["low"]))], fill=color, width=1)
        top_body, bottom_body = y(max(bar["open"], bar["close"])), y(min(bar["open"], bar["close"]))
        draw.rectangle([(x(i) - body / 2, top_body), (x(i) + body / 2, max(bottom_body, top_body + 1))], fill=color)

    if vwap:
        points = [(x(i), y(v)) for i, v in enumerate(vwap_series(bars)) if v is not None]
        if len(points) > 1:
            draw.line(points, fill=KIND_COLORS["vwap"], width=2)

    ordered = sorted(near, key=lambda lv: -lv["price"])
    for level in ordered:  # every line first, so no line crosses a label
        color = KIND_COLORS.get(str(level.get("kind") or ""), DEFAULT_LEVEL)
        for dash in range(int(left), int(right), 12):
            draw.line([(dash, y(level["price"])), (min(dash + 7, right), y(level["price"]))], fill=color, width=2)
    label_floor = -1e9  # labels of close levels stack downwards instead of overlapping
    for level in ordered:
        color = KIND_COLORS.get(str(level.get("kind") or ""), DEFAULT_LEVEL)
        ly = y(level["price"])
        text = f"{level['label']} {level['price']:g}"
        width_text = draw.textlength(text, font=label_font)
        label_y = max(ly - 17, label_floor)
        draw.rectangle([(right - width_text - 8, label_y), (right - 2, label_y + 16)], fill=(255, 255, 255, 220))
        draw.text((right - width_text - 5, label_y), text, fill=color, font=label_font)
        label_floor = label_y + 17

    last = bars[-1]["close"]
    draw.rectangle([(right + 2, y(last) - 9), (width - 4, y(last) + 9)], fill=INK)
    draw.text((right + 6, y(last) - 8), f"{last:g}", fill="white", font=small)

    top_volume = max(float(bar.get("volume") or 0) for bar in bars) or 1.0
    for i, bar in enumerate(bars):
        height_bar = (float(bar.get("volume") or 0) / top_volume) * (vol_bottom - vol_top)
        color = UP if bar["close"] >= bar["open"] else DOWN
        draw.rectangle([(x(i) - body / 2, vol_bottom - height_bar), (x(i) + body / 2, vol_bottom)], fill=color + (150,))
    draw.text((left, vol_top - 2), "Volume", fill=MUTED, font=small)
    every = max(1, len(bars) // 8)
    for i in range(0, len(bars), every):
        draw.text((x(i) - 16, vol_bottom + 6), times[i].strftime(time_format), fill=MUTED, font=small)

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
