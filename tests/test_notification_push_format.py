"""Push-report formatting for chat channels such as Discord."""
from unittest import mock

from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.notification import NotificationService


def _us_result(**overrides):
    values = dict(
        code="NVDA", name="NVIDIA Corporation", sentiment_score=54, trend_prediction="Range-bound",
        operation_advice="Watch", analysis_summary="Wait", report_language="en",
        current_price=224.8300018310547, change_pct=-1.765194215056579,
        dashboard={
            "core_conclusion": {"one_sentence": "Wait for trend confirmation before any new position; "
                                                 "a close back above the MA10 would improve the setup."},
            "battle_plan": {"sniper_points": {
                "ideal_buy": "Ideal entry: $224.54, only if MA5 support holds.",
                "stop_loss": "Stop loss: $219.55, below MA10 and MA20 support.",
                "take_profit": "First target: $228.95, the intraday high."}},
        },
    )
    values.update(overrides)
    return AnalysisResult(**values)


def _config(**overrides):
    from src.config import Config
    return Config(stock_list=[], report_renderer_enabled=False, report_language="en", **overrides)


@mock.patch("src.notification.get_config")
def test_brief_shows_price_change_and_plausible_levels(get_config):
    get_config.return_value = _config()
    out = NotificationService().generate_brief_report([_us_result()], report_date="2026-09-23")
    assert "224.83 (-1.77%)" in out
    assert "Ideal Entry 224.54" in out and "Stop Loss 219.55" in out and "Target 228.95" in out
    # The conclusion is no longer cut at 60 characters mid-word.
    assert "improve the setup." in out
    assert len(out) < 1000


@mock.patch("src.notification.get_config")
def test_brief_omits_levels_parsed_from_non_price_numbers(get_config):
    # Live SPYR case: "extension below 5%" was parsed as a 5.0 entry for a $0.0008 stock.
    get_config.return_value = _config()
    result = _us_result(code="SPYR", name="SPYR, Inc.", current_price=0.0007999999797903001, change_pct=14.29,
                        dashboard={"core_conclusion": {"one_sentence": "Wait; do not chase."},
                                   "battle_plan": {"sniper_points": {
                                       "ideal_buy": "No entry until extension is below 5%.",
                                       "stop_loss": "Review after a 14.29% move.", "take_profit": "Target 1."}}})
    out = NotificationService().generate_brief_report([result], report_date="2026-09-23")
    assert "0.0008 (+14.29%)" in out
    assert "↳" not in out and "Stop Loss" not in out


def test_clip_keeps_words_and_marks_cuts():
    text = "NVDA is down 1.77% intraday and has not completed the bullish moving-average alignment " * 3
    clipped = NotificationService._clip(text, 60)
    assert clipped.endswith("…") and not clipped[:-1].endswith(("complet", "mov"))
    assert len(clipped) <= 151
    assert NotificationService._clip("基本面保持稳健，但盘中回调和均线未完成排列需要等待确认", 10).endswith("…")
    assert NotificationService._clip("short", 60) == "short"


def test_display_price_rounds_floats_and_keeps_text():
    assert NotificationService._display_price(0.0007999999797903001) == "0.0008"
    assert NotificationService._display_price(224.8300018310547) == "224.83"
    assert NotificationService._display_price("near 224.5") == "near 224.5"


def test_market_snapshot_units_follow_market_and_language():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    context = {"today": {"date": "2026-09-23", "close": 0.0008, "prev_close": 0.0007, "high": 0.0008,
                         "low": 0.0007, "volume": 36212800, "amount": 2410400655}}
    us_en = analyzer._build_market_snapshot(context, code="NVDA", report_language="en")
    assert us_en["volume"] == "36.21M shares" and us_en["amount"] == "$2.41B" and us_en["close"] == "0.0008"
    us_zh = analyzer._build_market_snapshot(context, code="NVDA", report_language="zh")
    assert us_zh["amount"].endswith("亿美元")
    cn = analyzer._build_market_snapshot(context, code="600519", report_language="zh")
    assert cn["volume"] == "3621.28 万股" and cn["amount"] == "24.10 亿元"
