"""The chart picture the Trade Desk models get (trade_desk/chart_image.py)."""
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from src.services.trade_desk import chart_image

NY = ZoneInfo("America/New_York")


def _minutes(n, start=datetime(2026, 11, 9, 9, 30, tzinfo=NY)):
    return [{"time": (start + timedelta(minutes=k)).isoformat(), "open": 100 + k * 0.01, "high": 100.2 + k * 0.01,
             "low": 99.8 + k * 0.01, "close": 100.1 + k * 0.01, "volume": 1000 + k} for k in range(n)]


def test_one_minute_bars_group_into_five_minute_bars_of_the_same_clock():
    five = chart_image.aggregate(_minutes(12), 5)
    assert [bar["time"].strftime("%H:%M") for bar in five] == ["09:30", "09:35", "09:40"]
    first = five[0]
    assert first["open"] == 100 and first["close"] == pytest.approx(100.14) and first["high"] == pytest.approx(100.24)
    assert first["volume"] == sum(1000 + k for k in range(5))
    assert chart_image.vwap_series(five)[0] == pytest.approx((first["high"] + first["low"] + first["close"]) / 3)


def test_a_png_with_levels_near_the_bars_and_the_far_ones_named():
    png = chart_image.render(chart_image.aggregate(_minutes(120), 5), title="SPY today",
                             levels=[{"label": "VWAP", "price": 100.5, "kind": "vwap"},
                                     {"label": "Value-area high, last week", "price": 140.0, "kind": "vah"}],
                             boxes=[{"label": "range box", "low": 100.2, "high": 100.6}])
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.size == (chart_image.WIDTH, chart_image.HEIGHT)
    assert len(image.convert("RGB").getcolors(maxcolors=100000) or []) > 10  # candles, lines and labels, not a blank page


def test_daily_rows_with_bare_dates_draw_and_an_empty_chart_is_refused(monkeypatch):
    rows = [{"time": f"2026-10-{day:02d}", "open": 50, "high": 52, "low": 49, "close": 51, "volume": 10} for day in range(1, 29)]
    assert chart_image.render(rows, title="NVDA daily", vwap=False, time_format="%b %d")[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        chart_image.render([], title="nothing")
    assert chart_image.enabled()
    monkeypatch.setenv("TRADE_DESK_CHART_IMAGES", "false")
    assert not chart_image.enabled()
