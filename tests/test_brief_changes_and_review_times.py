"""Discord trimming: market review only in MARKET_REVIEW_TIMES; changes-only briefs in later slots."""
from datetime import datetime
from types import SimpleNamespace

from src.scheduler import current_slot, parse_times
from src.services.brief_changes import changes_only


def test_slot_helpers():
    assert parse_times("16:10, 9:40, bad,12:00") == ["12:00", "16:10"]
    assert current_slot(["09:40", "12:00", "16:10"], datetime(2026, 9, 25, 12, 20)) == "12:00"
    assert current_slot(["09:40", "12:00"], datetime(2026, 9, 25, 14, 0)) is None


def test_market_review_runs_only_in_its_slots():
    import main
    config = SimpleNamespace(market_review_times=["16:10"], schedule_times=["09:40", "12:00", "16:10"])
    assert main._market_review_due(config, SimpleNamespace(scheduled_slot="12:00")) is False
    assert main._market_review_due(config, SimpleNamespace(scheduled_slot="16:10")) is True
    assert main._market_review_due(config, SimpleNamespace(scheduled_slot=None)) is True  # manual / Run now
    assert main._market_review_due(SimpleNamespace(market_review_times=[]), SimpleNamespace(scheduled_slot="12:00"))


def _result(code, advice, score):
    return SimpleNamespace(code=code, operation_advice=advice, sentiment_score=score, decision_type=None,
                           action=None, report_language="en")


def _row(i, code, advice, score, when):
    return SimpleNamespace(id=i, code=code, operation_advice=advice, sentiment_score=score, created_at=when)


def test_changes_only_brief_compares_with_rows_saved_before_this_run():
    morning, run_start, now = datetime(2026, 9, 25, 9, 50), datetime(2026, 9, 25, 12, 0), datetime(2026, 9, 25, 12, 8)
    earlier = [_row(1, "AAA", "Watch", 50, morning), _row(2, "BBB", "Watch", 50, morning),
               _row(3, "CCC", "Watch", 45, morning),
               # this run's own rows, saved before the push: never the baseline
               _row(10, "AAA", "Buy", 70, now), _row(11, "BBB", "Watch", 52, now), _row(12, "CCC", "Watch", 58, now)]
    db = SimpleNamespace(get_analysis_history=lambda **kwargs: earlier)
    config = SimpleNamespace(brief_changes_only_times=["12:00"], report_language="en")
    results = [_result("AAA", "Buy", 70), _result("BBB", "Watch", 52), _result("CCC", "Watch", 58),
               _result("NEW", "Watch", 50)]
    changed, header = changes_only(results, db, config, "12:00", run_start)
    assert [r.code for r in changed] == ["AAA", "CCC", "NEW"]  # bucket change, 13-point move, new name
    assert header == "_12:00 update: only calls that changed since 09:50 (3 of 4)._"
    assert changes_only(results, db, config, "16:10", run_start) is None  # full brief in other slots
    assert changes_only(results, db, config, None, run_start) is None  # manual runs are never trimmed
    first = SimpleNamespace(get_analysis_history=lambda **kwargs: [_row(10, "AAA", "Buy", 70, now)])
    assert changes_only(results, first, config, "12:00", run_start) is None  # first run of the day
    unchanged = [_result("AAA", "Watch", 51), _result("BBB", "Watch", 50)]
    changed, header = changes_only(unchanged, db, config, "12:00", run_start)
    assert changed == [] and "no call changed since 09:50" in header


def test_extended_hours_prints_are_labelled_without_a_regular_close():
    from src.notification import NotificationService
    result = SimpleNamespace(current_price=101.5, change_pct=0.4, report_language="en",
                             market_snapshot={"quote_session": "postmarket"})
    assert NotificationService._brief_price(result) == " | AH 101.50 (+0.40%)"
    result.market_snapshot = {"quote_session": "postmarket", "regular_close": 101.0, "regular_change_pct": 2.0}
    assert NotificationService._brief_price(result).startswith(" | 101.00 (+2.00%) · AH 101.50")
    result.market_snapshot = {"quote_session": "regular"}
    assert NotificationService._brief_price(result) == " | 101.50 (+0.40%)"
