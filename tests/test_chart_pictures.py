"""Chart pictures by timeframe (trade_desk/chart_frames.py) and where they go: Ask about a stock on the ordered
models (Opus first, Codex for recalculations), second opinions and the daily reports."""
import base64
import io
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from src.services.trade_desk import advisor as adv
from src.services.trade_desk import assistant_model, chart_frames, chart_image
from tests.test_trade_desk_service import candidate, snapshot

NY = ZoneInfo("America/New_York")


def _hours(days=40):
    """moomoo 1-hour rows (end times) of ``days`` sessions: 10:30 .. 15:30 and the 30-minute 16:00 bar."""
    rows, start = [], datetime(2026, 9, 1, tzinfo=NY)
    for d in range(days):
        day = start + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for end in ("10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:00"):
            price = 100 + d * 0.3 + len(rows) % 7 * 0.1
            rows.append({"time_key": f"{day.date()} {end}:00", "open": price, "high": price + 1, "low": price - 1,
                         "close": price + 0.2, "volume": 1000 + len(rows)})
    return rows


class FakeHistory:
    def __init__(self):
        self.asked = []

    def history_bars(self, code, start, end, subtype):
        self.asked.append((code, subtype))
        return _hours()


def test_hour_bars_are_restamped_with_their_start_and_grouped_into_the_two_four_hour_bars():
    bars = chart_frames._moomoo(FakeHistory(), "TEST", "1h", datetime(2026, 10, 15, 12, tzinfo=NY))
    first_day = [bar["time"][11:16] for bar in bars[:7]]
    assert first_day == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    four = chart_frames.four_hour(bars)
    assert [bar["time"][11:16] for bar in four[:2]] == ["09:30", "13:30"]
    assert four[0]["open"] == bars[0]["open"] and four[0]["close"] == bars[3]["close"]
    assert four[0]["high"] == max(bar["high"] for bar in bars[:4]) and four[1]["volume"] == sum(b["volume"] for b in bars[4:7])


def test_bars_come_from_moomoo_first_and_a_live_session_never_falls_back_to_yahoo(monkeypatch):
    chart_frames._cache.clear()
    monkeypatch.setattr(chart_frames, "_yahoo", lambda ticker, frame: pytest.fail("Yahoo must not be asked"))
    provider = FakeHistory()
    assert chart_frames.bars("TEST", "4h", provider=provider)[0]["time"].endswith("09:30:00-04:00")
    assert provider.asked == [("US.TEST", "K_60M")]

    class Broken:
        def history_bars(self, *args):
            raise RuntimeError("OpenD down")
    assert chart_frames.bars("NONE", "1h", provider=Broken(), fallback=False) == []
    yahoo = []
    monkeypatch.setattr(chart_frames, "_yahoo", lambda ticker, frame: yahoo.append(frame) or [{"time": "x"}])
    assert chart_frames.bars("NONE", "15m", provider=Broken()) == [{"time": "x"}] and yahoo == ["15m"]


def test_pictures_carry_the_indicators_and_the_volume_profile(monkeypatch):
    chart_frames._cache.clear()
    rows = chart_frames._moomoo(FakeHistory(), "TEST", "1h", datetime(2026, 10, 15, 12, tzinfo=NY))
    png = chart_image.render(rows, title="TEST 1-hour", visible=120, nx=True, cd=True, vrvp=True, vwap=False,
                             levels=[{"label": "Your stop", "price": 105.0, "kind": "stop"}])
    image = Image.open(io.BytesIO(png))
    assert image.size == (chart_image.WIDTH, chart_image.HEIGHT)
    profile = chart_image.volume_profile(rows)
    assert profile["val"] <= profile["poc"] <= profile["vah"]
    low_k, high_k = profile["value_bins"]
    assert sum(profile["volume"][low_k:high_k + 1]) >= 0.7 * sum(profile["volume"])
    monkeypatch.setattr(chart_frames, "bars", lambda ticker, frame, **kw: rows)
    images = chart_frames.pictures("TEST", ["15m", "1h", "4h", "1d", "1m"], provider=None)
    assert len(images) == 4  # 1m is shadow mode's own
    monkeypatch.setenv("TRADE_DESK_CHART_IMAGES", "false")
    assert chart_frames.pictures("TEST", ["1h"]) == []
    assert chart_frames.frames() == ["15m", "1h", "4h"]


# -- Ask about a stock on the ordered models ---------------------------------------------------------------
class FakeCodex:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def explain(self, request, snap, candidates, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise adv.AdvisorError("codex down")
        narrative = adv.Narrative(assessment="wait", explanation="Codex recalculated.", selections=[])
        return narrative, {c.id: c for c in candidates}, {"_usage": {"model": "Codex (gpt-6.1-sol, xhigh)"}}


def _ask(monkeypatch, answers, codex=None, mode="live"):
    monkeypatch.setenv("TRADE_DESK_QUESTION_MODELS", "claude_code_cli:claude-opus-5-5:xhigh,codex_cli:gpt-6.1-sol:xhigh,"
                                                     "claude_code_cli:claude-sonnet-5-5:xhigh,litellm")
    seen = []

    def generate(prompt, system, **kwargs):
        seen.append({"system": system, **kwargs})
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        entry = kwargs["use"][0]
        return {"data": answer, "label": assistant_model.label(entry), "usage": {}}
    monkeypatch.setattr(assistant_model, "generate", generate)
    pictures = lambda ticker, frames: [{"mime_type": "image/png", "data": f"{ticker}-{'-'.join(frames)}"}]
    codex = codex or FakeCodex()
    snap = snapshot(mode)
    request = SimpleNamespace(ticker="TEST", model_dump=lambda mode="json": {"ticker": "TEST"})
    out = adv.OrderedTradeAdvisor(codex, pictures=pictures).explain(request, snap, [candidate(snap)], cancel=threading.Event())
    return out, seen, codex


def test_opus_answers_first_with_the_four_pictures(monkeypatch):
    (narrative, pool, contexts), seen, codex = _ask(monkeypatch, [
        {"assessment": "wait", "explanation": "The 4-hour NX tunnel is falling; wait.", "selections": []}])
    assert narrative.explanation.startswith("The 4-hour") and contexts["_usage"]["model"] == "Claude Code (claude-opus-5-5, xhigh)"
    assert seen[0]["use"][0].model == "claude-opus-5-5" and seen[0]["images"] == [
        {"mime_type": "image/png", "data": "TEST-15m-1h-4h-1d"}]
    assert "never from a picture's axis" in seen[0]["system"] and "needs_calculation" in seen[0]["system"]
    assert codex.calls == []


def test_a_question_that_needs_a_calculation_goes_to_codex_with_the_pictures(monkeypatch):
    (narrative, _, contexts), seen, codex = _ask(monkeypatch, [
        {"assessment": "wait", "explanation": "", "selections": [], "needs_calculation": "A put spread expiring in 3 weeks."}])
    assert narrative.explanation == "Codex recalculated." and contexts["_usage"]["model"].startswith("Codex")
    assert codex.calls[0]["needs"] == "A put spread expiring in 3 weeks." and codex.calls[0]["model"] == "gpt-6.1-sol"
    assert codex.calls[0]["effort"] == "xhigh" and codex.calls[0]["images"][0]["data"] == "TEST-15m-1h-4h-1d"

    (narrative, _, _), _, codex = _ask(monkeypatch, [
        {"assessment": "wait", "explanation": "", "selections": [], "needs_calculation": "A collar."}], codex=FakeCodex(fail=True))
    assert "needs a new calculation: A collar." in narrative.explanation  # Codex down: Opus's answer says what it lacked


def test_a_failing_entry_passes_to_the_next_and_replay_requests_get_no_pictures(monkeypatch):
    (_, _, contexts), seen, codex = _ask(monkeypatch, [RuntimeError("usage limit")], codex=FakeCodex())
    assert len(codex.calls) == 1 and codex.calls[0]["model"] == "gpt-6.1-sol"  # Opus failed: Codex is next in order
    (_, _, _), seen, _ = _ask(monkeypatch, [{"assessment": "wait", "explanation": "Synthetic.", "selections": []}],
                              mode="replay")
    assert seen[0]["images"] is None


def test_second_opinions_take_the_chosen_model_and_the_pictures(monkeypatch):
    from src.llm import second_opinion
    asked = {}

    class Backend:
        def generate(self, prompt, config, system_prompt=None):
            asked.update(config=config, system=system_prompt)
            return SimpleNamespace(text='{"action": "wait", "reason": "r", "risk": "k"}')
    out = second_opinion.collect_opinions(lambda backend_id: Backend(), ["codex_cli"], "prompt", config=SimpleNamespace(),
                                          labels={"codex_cli": "Codex (gpt-6.1-sol, xhigh)"},
                                          images=[{"mime_type": "image/png", "data": "QUJD"}])
    assert out[0]["model"] == "Codex (gpt-6.1-sol, xhigh)" and asked["config"]["images"][0]["data"] == "QUJD"


def test_reports_carry_pictures_only_for_us_stocks_when_frames_are_set(monkeypatch):
    from src.analyzer import GeminiAnalyzer
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    drawn = []
    monkeypatch.setattr(chart_frames, "with_provider", lambda fn: fn(None))
    monkeypatch.setattr(chart_frames, "pictures", lambda ticker, frames, provider=None: drawn.append((ticker, frames)) or ["img"])
    monkeypatch.delenv("REPORT_CHART_FRAMES", raising=False)
    assert analyzer._report_chart_images("NVDA") == []
    monkeypatch.setenv("REPORT_CHART_FRAMES", "15m,1h,4h")
    assert analyzer._report_chart_images("600519") == [] and drawn == []
    assert analyzer._report_chart_images("nvda") == ["img"] and drawn == [("NVDA", ["15m", "1h", "4h"])]
    monkeypatch.setattr(chart_frames, "pictures", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert analyzer._report_chart_images("NVDA") == []  # the report goes on without pictures
