"""Chart pictures for the models (Pillow), drawn like the Chart tab: candles, the user's indicators and levels.

The models already get every price as a number; the picture adds the shape (trend, ranges, how the price
reached a level). One picture per timeframe: a price panel with the NX tunnels (the user's EMA 26/89 of
highs and lows), moving averages, VWAP, boxes, levels and a volume-by-price profile of the visible range
(VRVP: its point of control and 70 % value area), a volume panel, and a CD panel (MACD 12/26/9 with the
CD script's buy and sell marks). Indicators are computed on every bar passed (the warm-up included) and
drawn on the last ``visible``. The prompt tells the model to take every price it writes from the numbers.
``TRADE_DESK_CHART_IMAGES=false`` sends no picture.
"""
from __future__ import annotations

import base64
import io
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

_NEW_YORK = ZoneInfo("America/New_York")
WIDTH, HEIGHT = 1200, 800
UP, DOWN = (22, 163, 74), (220, 38, 38)
INK, GRID, MUTED = (17, 24, 39), (229, 231, 235), (107, 114, 128)
NX_FAST, NX_SLOW = (37, 99, 235), (234, 88, 12)
MA_COLORS = {20: (217, 119, 6), 50: (124, 58, 237), 200: (17, 24, 39)}
KIND_COLORS = {"vwap": (124, 58, 237), "premarket_high": (234, 88, 12), "premarket_low": (234, 88, 12),
               "prior_high": (75, 85, 99), "prior_low": (75, 85, 99), "prior_close": (75, 85, 99),
               "stop": (220, 38, 38), "target": (22, 163, 74), "trigger": (37, 99, 235)}
DEFAULT_LEVEL = (8, 145, 178)
PROFILE_BINS = 48
VALUE_AREA = 0.70


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
    """Session VWAP: it starts again with each New York day."""
    total = weight = 0.0
    day = None
    out: List[Optional[float]] = []
    for bar in bars:
        when = _when(bar["time"]).date()
        if when != day:
            total = weight = 0.0
            day = when
        volume = float(bar.get("volume") or 0)
        total += (bar["high"] + bar["low"] + bar["close"]) / 3 * volume
        weight += volume
        out.append(total / weight if weight else None)
    return out


def moving_average(values: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def volume_profile(bars: Sequence[Dict[str, Any]], bins: int = PROFILE_BINS) -> Optional[Dict[str, Any]]:
    """Volume by price over ``bars`` (each bar's volume spread evenly over its high-low range): the bins,
    the point of control (the fullest bin's middle) and the value area holding 70 % of the volume around it."""
    lo = min(bar["low"] for bar in bars)
    hi = max(bar["high"] for bar in bars)
    if hi <= lo:
        return None
    size = (hi - lo) / bins
    volume = [0.0] * bins
    for bar in bars:
        amount = float(bar.get("volume") or 0)
        first = min(bins - 1, int((bar["low"] - lo) / size))
        last = min(bins - 1, int((bar["high"] - lo) / size))
        share = amount / (last - first + 1)
        for k in range(first, last + 1):
            volume[k] += share
    total = sum(volume)
    if total <= 0:
        return None
    poc = max(range(bins), key=lambda k: volume[k])
    low_k = high_k = poc
    held = volume[poc]
    while held < VALUE_AREA * total and (low_k > 0 or high_k < bins - 1):
        below = volume[low_k - 1] if low_k > 0 else -1.0
        above = volume[high_k + 1] if high_k < bins - 1 else -1.0
        if above >= below:
            high_k += 1
            held += above
        else:
            low_k -= 1
            held += below
    return {"low": lo, "size": size, "volume": volume, "poc": lo + (poc + 0.5) * size,
            "val": lo + low_k * size, "vah": lo + (high_k + 1) * size, "value_bins": (low_k, high_k)}


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
           visible: Optional[int] = None, nx: bool = False, cd: bool = False, vrvp: bool = False,
           mas: Sequence[int] = (), width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """A PNG of ``bars`` (oldest first). ``levels``: ``label``, ``price``, ``kind``; ``boxes``: ``label``, ``low``,
    ``high``, optional ``start``/``end`` times. Indicators use every bar; the last ``visible`` are drawn. Levels
    far outside the drawn range are listed under the title instead of squeezing the candles."""
    from PIL import Image, ImageDraw
    if not bars:
        raise ValueError("no bars to draw")
    all_bars = list(bars)
    shown_from = max(0, len(all_bars) - visible) if visible else 0
    shown = all_bars[shown_from:]
    series: List[Tuple[str, Tuple[int, int, int], List[Optional[float]], int]] = []  # name, color, values, width
    if nx:
        from .moomoo_indicators import nx as nx_lines
        lines = nx_lines(all_bars)
        for name, key, color in (("NX fast top (EMA26 of highs)", "A", NX_FAST), ("NX fast bottom (EMA26 of lows)", "B", NX_FAST),
                                 ("NX slow top (EMA89 of highs)", "A1", NX_SLOW), ("NX slow bottom (EMA89 of lows)", "B1", NX_SLOW)):
            series.append((name, color, lines[key][shown_from:], 2))
    closes = [float(bar["close"]) for bar in all_bars]
    for n in mas:
        if len(all_bars) >= n:
            series.append((f"MA{n}", MA_COLORS.get(n, MUTED), moving_average(closes, n)[shown_from:], 1))
    if vwap:
        series.append(("VWAP", KIND_COLORS["vwap"], vwap_series(shown), 2))
    macd = None
    if cd and len(all_bars) >= 35:
        from .moomoo_indicators import cd as cd_lines
        lines = cd_lines(all_bars)
        macd = {key: lines[key][shown_from:] for key in ("DIF", "DEA", "MACD", "buy", "sell")}

    levels = [level for level in levels if isinstance(level.get("price"), (int, float))]
    boxes = [box for box in boxes if isinstance(box.get("low"), (int, float)) and isinstance(box.get("high"), (int, float))]
    lo = min(bar["low"] for bar in shown)
    hi = max(bar["high"] for bar in shown)
    margin = max((hi - lo) * 0.6, hi * 0.004)
    near = [level for level in levels if lo - margin <= level["price"] <= hi + margin]
    far = [level for level in levels if level not in near]
    extra = [level["price"] for level in near] + [v for box in boxes for v in (box["low"], box["high"])
                                                    if lo - margin <= v <= hi + margin]
    extra += [v for _, _, values, _ in series for v in values if v is not None and lo - margin <= v <= hi + margin]
    for value in extra:
        lo, hi = min(lo, value), max(hi, value)
    pad = (hi - lo) * 0.04 or hi * 0.002
    lo, hi = lo - pad, hi + pad

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image, "RGBA")
    small, label_font, title_font = _font(13), _font(14), _font(18)
    left, right, top = 12, width - 96, 74
    price_bottom = int(height * (0.62 if macd else 0.74))
    vol_top = price_bottom + 14
    vol_bottom = int(height * (0.75 if macd else 0.95)) - (0 if macd else 20)
    macd_top, macd_bottom = vol_bottom + 18, height - 34

    def y(price: float) -> float:
        return top + (hi - price) / (hi - lo) * (price_bottom - top)

    step = (right - left) / len(shown)

    def x(index: float) -> float:
        return left + (index + 0.5) * step

    draw.text((left, 8), title, fill=INK, font=title_font)
    legend = [name.split(" (")[0] for name, *_ in series if not name.startswith("NX")]
    if nx:
        legend = ["NX fast tunnel (blue, EMA26 highs/lows)", "NX slow tunnel (orange, EMA89)"] + legend
    if vrvp:
        legend.append("VRVP (grey, POC orange, value area darker)")
    if macd:
        legend.append("CD/MACD 12,26,9 (green triangle = CD buy, red = CD sell)")
    draw.text((left, 32), " · ".join(legend) or "Price", fill=MUTED, font=small)
    if far:
        draw.text((left, 50), "Off the chart: " + "; ".join(f"{lv['label']} {lv['price']:g}" for lv in far[:6]),
                  fill=MUTED, font=small)
    for tick in _ticks(lo, hi):
        draw.line([(left, y(tick)), (right, y(tick))], fill=GRID, width=1)
        draw.text((right + 6, y(tick) - 7), f"{tick:g}", fill=MUTED, font=small)

    times = [_when(bar["time"]) for bar in shown]
    if vrvp:
        profile = volume_profile(shown)
        if profile:
            widest = max(profile["volume"]) or 1.0
            span = (right - left) * 0.22
            low_k, high_k = profile["value_bins"]
            for k, amount in enumerate(profile["volume"]):
                y0, y1 = y(profile["low"] + (k + 1) * profile["size"]), y(profile["low"] + k * profile["size"])
                shade = (107, 114, 128, 95) if low_k <= k <= high_k else (156, 163, 175, 55)
                draw.rectangle([(right - span * amount / widest, y0 + 0.5), (right, max(y0 + 1, y1 - 0.5))], fill=shade)
            draw.line([(right - span, y(profile["poc"])), (right, y(profile["poc"]))], fill=(234, 88, 12), width=2)
            draw.text((right - span, y(profile["poc"]) - 15), f"VRVP POC {profile['poc']:.2f}", fill=(194, 65, 12), font=small)

    for box in boxes:
        start, end = box.get("start"), box.get("end")
        if end is not None and _when(end) < times[0]:
            continue  # it ended before the drawn bars
        i0 = 0 if start is None else next((i for i, t in enumerate(times) if t >= _when(start)), len(shown) - 1)
        i1 = len(shown) - 1 if end is None else max(i0, max((i for i, t in enumerate(times) if t <= _when(end)), default=i0))
        x0, x1 = x(i0) - step / 2, x(i1) + step / 2
        draw.rectangle([(x0, y(box["high"])), (x1, y(box["low"]))], fill=(14, 165, 233, 40), outline=(14, 165, 233, 160))
        draw.text((x0 + 3, y(box["high"]) - 15), str(box.get("label") or "box"), fill=(3, 105, 161), font=small)

    for name, color, values, line_width in series:
        points = [(x(i), y(v)) for i, v in enumerate(values) if v is not None]
        if len(points) > 1:
            draw.line(points, fill=color, width=line_width)

    body = max(1.0, step * 0.6)
    for i, bar in enumerate(shown):
        color = UP if bar["close"] >= bar["open"] else DOWN
        draw.line([(x(i), y(bar["high"])), (x(i), y(bar["low"]))], fill=color, width=1)
        top_body, bottom_body = y(max(bar["open"], bar["close"])), y(min(bar["open"], bar["close"]))
        draw.rectangle([(x(i) - body / 2, top_body), (x(i) + body / 2, max(bottom_body, top_body + 1))], fill=color)

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

    last = shown[-1]["close"]
    draw.rectangle([(right + 2, y(last) - 9), (width - 4, y(last) + 9)], fill=INK)
    draw.text((right + 6, y(last) - 8), f"{last:g}", fill="white", font=small)

    top_volume = max(float(bar.get("volume") or 0) for bar in shown) or 1.0
    for i, bar in enumerate(shown):
        height_bar = (float(bar.get("volume") or 0) / top_volume) * (vol_bottom - vol_top)
        color = UP if bar["close"] >= bar["open"] else DOWN
        draw.rectangle([(x(i) - body / 2, vol_bottom - height_bar), (x(i) + body / 2, vol_bottom)], fill=color + (150,))
    draw.text((left, vol_top - 2), "Volume", fill=MUTED, font=small)

    if macd:
        values = [v for key in ("DIF", "DEA", "MACD") for v in macd[key] if v is not None]
        top_value = max((abs(v) for v in values), default=0.0) or 1.0
        middle = (macd_top + macd_bottom) / 2

        def my(value: float) -> float:
            return middle - value / top_value * (macd_bottom - macd_top) / 2 * 0.92
        draw.line([(left, middle), (right, middle)], fill=GRID, width=1)
        for i, value in enumerate(macd["MACD"]):
            if value is not None:
                draw.rectangle([(x(i) - body / 2, min(my(value), middle)), (x(i) + body / 2, max(my(value), middle))],
                               fill=(UP if value >= 0 else DOWN) + (150,))
        for key, color in (("DIF", (17, 24, 39)), ("DEA", (234, 88, 12))):
            points = [(x(i), my(v)) for i, v in enumerate(macd[key]) if v is not None]
            if len(points) > 1:
                draw.line(points, fill=color, width=1)
        for i in range(len(shown)):
            if macd["buy"][i]:
                draw.polygon([(x(i), macd_bottom - 14), (x(i) - 6, macd_bottom - 2), (x(i) + 6, macd_bottom - 2)], fill=UP)
                draw.text((x(i) + 7, macd_bottom - 16), "CD buy", fill=UP, font=small)
            if macd["sell"][i]:
                draw.polygon([(x(i), macd_top + 14), (x(i) - 6, macd_top + 2), (x(i) + 6, macd_top + 2)], fill=DOWN)
                draw.text((x(i) + 7, macd_top), "CD sell", fill=DOWN, font=small)
        caption = "CD / MACD (12, 26, 9): DIF black, DEA orange"
        draw.text((right - draw.textlength(caption, font=small), macd_bottom + 2), caption, fill=MUTED, font=small)

    every = max(1, len(shown) // 8)
    for i in range(0, len(shown), every):
        draw.text((x(i) - 16, height - 24), times[i].strftime(time_format), fill=MUTED, font=small)

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
