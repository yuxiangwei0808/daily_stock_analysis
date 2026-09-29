"""NX tunnel context in stock reports: levels, wording, prompt guard, rendering."""
from types import SimpleNamespace

from src.services import nx_tunnel


def _bars(closes):
    return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": c, "high": c * 1.01, "low": c * 0.99, "close": c}
            for i, c in enumerate(closes)]


def _uptrend():
    return [50 + i * 0.2 for i in range(300)]


def test_describe_an_uptrend_sits_above_both_tunnels():
    nx = nx_tunnel.describe(_bars(_uptrend()))
    assert nx["fast"]["state"] == "above" and nx["slow"]["state"] == "above"
    assert nx["structure"] == "fast_above_slow" and nx["changes_today"] == []
    assert nx["fast"]["bottom"] < nx["fast"]["top"] and nx["to_fast_bottom_pct"] > 0
    assert nx_tunnel.describe(_bars(_uptrend()[:100])) is None  # too little history for the 89-bar tunnel


def test_a_drop_into_the_fast_tunnel_is_reported_as_a_change():
    closes = _uptrend()
    nx_before = nx_tunnel.describe(_bars(closes))
    closes[-1] = nx_before["fast"]["bottom"] * 1.001 / 1.0  # close just above the fast tunnel's lower edge
    nx = nx_tunnel.describe(_bars(closes))
    assert nx["fast"]["state"] == "inside" and nx["changes_today"] == ["fast:above->inside"]
    line = nx_tunnel.summary_line(nx, "en")
    assert "inside the fast tunnel" in line and "changed today: fast:above->inside" in line
    assert "快通道" in nx_tunnel.summary_line(nx, "zh")


def test_prompt_section_says_nx_is_context_only():
    nx = nx_tunnel.describe(_bars(_uptrend()))
    section = nx_tunnel.prompt_section(nx)
    assert "NX 通道" in section and "不得仅凭 NX 调整评分或买卖结论" in section
    assert nx_tunnel.prompt_section(None) == ""


def test_attach_and_report_lines():
    nx = nx_tunnel.describe(_bars(_uptrend()))
    result = SimpleNamespace(dashboard=None)
    nx_tunnel.attach(result, nx)
    assert result.dashboard["data_perspective"]["nx_tunnel"] is nx
    lines = nx_tunnel.report_lines(result.dashboard["data_perspective"], "en")
    assert lines[0].startswith("**NX tunnel**: Price") and lines[1] == ""
    assert nx_tunnel.report_lines({}, "zh") == [] and nx_tunnel.report_lines({"nx_tunnel": {"bad": 1}}, "zh") == []
    nx_tunnel.attach(result, None)  # nothing to attach leaves the result alone
    assert result.dashboard["data_perspective"]["nx_tunnel"] is nx


def test_for_ticker_caches_and_never_raises(monkeypatch):
    calls = []

    def fake_download(tickers, period):
        calls.append((tuple(tickers), period))
        return {"BRK-B": _bars(_uptrend())}

    monkeypatch.setattr("src.services.trade_desk.trend.download_bars", fake_download)
    nx_tunnel._cache.clear()
    assert nx_tunnel.for_ticker("brk.b")["slow"]["state"] == "above"
    assert nx_tunnel.for_ticker("BRK.B") is not None and calls == [(("BRK.B",), "2y")]

    def broken(tickers, period):
        raise RuntimeError("network")

    monkeypatch.setattr("src.services.trade_desk.trend.download_bars", broken)
    assert nx_tunnel.for_ticker("ZZZZ") is None
    nx_tunnel._cache.clear()


def test_dashboard_report_renders_the_nx_line():
    from src.analyzer import AnalysisResult
    from src.notification import NotificationService

    result = AnalysisResult(code="SOXS", name="SOXS", sentiment_score=40, trend_prediction="震荡", operation_advice="观望",
                            analysis_summary="测试", report_language="en", success=True)
    nx_tunnel.attach(result, nx_tunnel.describe(_bars(_uptrend())))
    report = NotificationService().generate_dashboard_report([result])
    assert "**NX tunnel**: Price" in report and "the fast tunnel" in report


def test_standard_prompt_includes_the_nx_section():
    from unittest.mock import patch

    from src.analyzer import GeminiAnalyzer

    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    nx = nx_tunnel.describe(_bars(_uptrend()))
    prompt = analyzer._format_prompt({"code": "SOXS", "stock_name": "SOXS", "nx_tunnel": nx}, "SOXS", news_context=None)
    assert "### NX 通道" in prompt and "不得仅凭 NX" in prompt
    plain = analyzer._format_prompt({"code": "SOXS", "stock_name": "SOXS"}, "SOXS", news_context=None)
    assert "NX 通道" not in plain


def test_agent_prompt_includes_the_nx_section():
    from src.agent import executor as agent_executor

    source = agent_executor.__loader__.get_source(agent_executor.__name__)
    assert 'context.get("nx_tunnel")' in source and "prompt_section(context[\"nx_tunnel\"])" in source
