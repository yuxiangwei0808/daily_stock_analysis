"""Model tiers: routine vs user-requested analyses and independent second opinions."""
import threading
from types import SimpleNamespace

import pytest

from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.llm.generation_backend import GenerationResult
from src.llm.second_opinion import (
    agreement,
    collect_opinions,
    is_targeted,
    second_opinion_backends,
    targeted_analysis,
    targeted_backend,
)
from src.notification import NotificationService


def _config(**overrides):
    values = dict(generation_backend="litellm", litellm_model="openai/google/gemini-3.8-flash",
                  targeted_generation_backend="codex_cli",
                  second_opinion_backends=["litellm", "claude_code_cli"],
                  claude_code_cli_model="claude-opus-5-5", generation_fallback_backend="")
    values.update(overrides)
    return SimpleNamespace(**values)


def test_tier_applies_only_inside_a_targeted_context_and_its_thread():
    config = _config()
    assert targeted_backend(config) is None and second_opinion_backends(config, "litellm") == []
    seen = []
    with targeted_analysis():
        assert is_targeted() and targeted_backend(config) == "codex_cli"
        assert second_opinion_backends(config, "codex_cli") == ["litellm", "claude_code_cli"]
        # A routine analysis running concurrently in another thread is unaffected.
        thread = threading.Thread(target=lambda: seen.append(is_targeted()))
        thread.start()
        thread.join()
    assert seen == [False] and not is_targeted()


def test_analyzer_uses_the_targeted_backend_only_for_user_requested_runs():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._get_runtime_config = lambda: _config()
    assert analyzer._resolve_generation_backend_config()[0] == "litellm"
    with targeted_analysis():
        assert analyzer._resolve_generation_backend_config()[0] == "codex_cli"


class _Backend:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.prompts = text, error, []

    def generate(self, prompt, generation_config, **kwargs):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return GenerationResult(text=self.text, model="m", provider="p", backend="b", usage={})


def test_collect_opinions_normalizes_verdicts_and_contains_failures():
    backends = {
        "litellm": _Backend('{"action": "Hold", "score": 61, "reason": "Range-bound.", "risk": "Earnings."}'),
        "claude_code_cli": _Backend(error=RuntimeError("not logged in")),
        "opencode_cli": _Backend('{"action": "moon", "score": 500}'),
    }
    opinions = collect_opinions(backends.__getitem__, list(backends), "data", config=_config())
    by_backend = {item["backend"]: item for item in opinions}
    assert by_backend["litellm"] == {"backend": "litellm", "model": "google/gemini-3.8-flash", "status": "ok",
                                     "action": "hold", "score": 61, "reason": "Range-bound.", "risk": "Earnings."}
    assert by_backend["claude_code_cli"]["status"] == "error"
    assert by_backend["claude_code_cli"]["model"] == "claude-opus-5-5"
    assert by_backend["opencode_cli"]["status"] == "error"  # invalid action is rejected, not guessed


def test_agreement_flags_split_decisions():
    assert agreement([{"status": "ok", "action": "hold"}, {"status": "ok", "action": "hold"},
                      {"status": "error"}]) == "agree"
    assert agreement([{"status": "ok", "action": "hold"}, {"status": "ok", "action": "buy"}]) == "split"


def test_reports_show_the_model_panel(monkeypatch):
    from src.config import Config
    monkeypatch.setattr("src.notification.get_config",
                        lambda: Config(stock_list=[], report_renderer_enabled=False, report_language="en"))
    result = AnalysisResult(
        code="NVDA", name="NVIDIA", sentiment_score=54, trend_prediction="Range-bound", operation_advice="Watch",
        analysis_summary="Wait", report_language="en", current_price=224.83, change_pct=-1.77,
        dashboard={"core_conclusion": {"one_sentence": "Wait for confirmation."}, "model_panel": {
            "agreement": "split",
            "opinions": [
                {"model": "gpt-6-astra", "role": "primary", "status": "ok", "action": "watch", "score": 54,
                 "reason": "Wait for confirmation."},
                {"model": "google/gemini-3.8-flash", "status": "ok", "action": "buy", "score": 66,
                 "reason": "Support held.", "risk": "Momentum fades."},
                {"model": "claude-opus-5-5", "status": "error", "error": "Unavailable"},
            ]}})
    service = NotificationService()
    brief = service.generate_brief_report([result], report_date="2026-09-23")
    assert "Panel: gpt-6-astra Watch · 54 · google/gemini-3.8-flash Buy · 66 · claude-opus-5-5 unavailable" in brief
    assert "Split decision" in brief
    simple = service.generate_single_stock_report(result)
    assert "Model panel" in simple and "**gpt-6-astra** (primary): Watch · 54" in simple
    assert "Risk: Momentum fades." in simple
    assert "Model panel" in service.generate_dashboard_report([result])


def test_reports_without_a_panel_are_unchanged(monkeypatch):
    from src.config import Config
    monkeypatch.setattr("src.notification.get_config",
                        lambda: Config(stock_list=[], report_renderer_enabled=False, report_language="en"))
    result = AnalysisResult(code="NVDA", name="NVIDIA", sentiment_score=54, trend_prediction="Range-bound",
                            operation_advice="Watch", analysis_summary="Wait", report_language="en")
    assert "Panel" not in NotificationService().generate_brief_report([result])


def test_analyzer_collects_opinions_on_the_same_data_and_stores_the_panel():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    backends = {"litellm": _Backend('{"action": "hold", "score": 58, "reason": "Flat.", "risk": "Rates."}'),
                "claude_code_cli": _Backend('{"action": "watch", "score": 50, "reason": "Unclear.", "risk": "Gap."}')}
    analyzer._get_generation_backend = lambda backend_id=None: backends[backend_id]
    config = _config()
    assert analyzer._start_second_opinions(config, "codex_cli", "DATA", "SYS", "en") is None  # routine run
    with targeted_analysis():
        future = analyzer._start_second_opinions(config, "codex_cli", "DATA", "SYS", "en")
    result = AnalysisResult(code="AAPL", name="Apple", sentiment_score=55, trend_prediction="Range-bound",
                            operation_advice="Watch", decision_type="hold", action="watch",
                            dashboard={"core_conclusion": {"one_sentence": "Wait."}})
    analyzer._attach_model_panel(result, future, config, "codex_cli")
    panel = result.dashboard["model_panel"]
    assert [item["action"] for item in panel["opinions"]] == ["watch", "hold", "watch"]
    assert panel["opinions"][0]["role"] == "primary" and panel["agreement"] == "agree"  # hold and watch are the same verdict
    assert all(prompt.startswith("DATA") and "SECOND OPINION MODE" in prompt
               for backend in backends.values() for prompt in backend.prompts)


def test_brief_shows_the_days_close_and_the_afterhours_print(monkeypatch):
    from src.config import Config
    monkeypatch.setattr("src.notification.get_config",
                        lambda: Config(stock_list=[], report_renderer_enabled=False, report_language="en"))
    result = AnalysisResult(
        code="SPY", name="SPDR S&P 500", sentiment_score=59, trend_prediction="Range-bound",
        operation_advice="Watch", analysis_summary="Wait", report_language="en",
        current_price=766.72, change_pct=-0.08,
        market_snapshot={"quote_session": "postmarket", "regular_close": 767.32, "regular_change_pct": -0.06})
    brief = NotificationService().generate_brief_report([result], report_date="2026-09-24")
    assert "767.32 (-0.06%) · AH 766.72 (-0.08%)" in brief
    result.market_snapshot = {"quote_session": "regular"}
    assert "766.72 (-0.08%)" in NotificationService().generate_brief_report([result], report_date="2026-09-24")


def test_panel_primary_follows_the_final_guarded_call():
    from types import SimpleNamespace
    from src.analyzer import sync_model_panel_primary
    result = SimpleNamespace(action="hold", decision_type="hold", sentiment_score=48, dashboard={"model_panel": {
        "opinions": [{"role": "primary", "action": "buy", "score": 72, "status": "ok"},
                     {"role": "second", "action": "buy", "status": "ok"}],
        "agreement": "agree"}})
    sync_model_panel_primary(result)
    primary = result.dashboard["model_panel"]["opinions"][0]
    assert (primary["action"], primary["score"]) == ("hold", 48)
    assert result.dashboard["model_panel"]["agreement"] != "agree"


def test_hold_and_watch_agree():
    from src.llm.second_opinion import agreement
    assert agreement([{"action": "hold", "status": "ok"}, {"action": "watch", "status": "ok"}]) == "agree"
    assert agreement([{"action": "buy", "status": "ok"}, {"action": "watch", "status": "ok"}]) == "split"
