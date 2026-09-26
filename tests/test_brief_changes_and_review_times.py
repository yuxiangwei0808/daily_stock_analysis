"""Discord trimming: market review only in MARKET_REVIEW_TIMES; changes-only briefs in later slots."""
from datetime import datetime
from types import SimpleNamespace

from src.scheduler import current_slot, parse_times
from src.services.brief_changes import changes_only


def test_slot_helpers():
    assert parse_times("16:10, 9:40, bad,12:00") == ["12:00", "16:10"]
    assert current_slot(["09:40", "12:00", "16:10"], datetime(2026, 9, 25, 12, 20)) == "12:00"
    assert current_slot(["09:40", "12:00"], datetime(2026, 9, 25, 14, 0)) is None


def test_market_review_runs_only_in_its_slots(monkeypatch):
    import main
    config = SimpleNamespace(market_review_times=["16:10"], schedule_times=["09:40", "12:00", "16:10"])
    monkeypatch.setattr("src.scheduler.current_slot", lambda times, now=None: "12:00")
    assert main._market_review_due(config, SimpleNamespace(schedule=True)) is False
    assert main._market_review_due(config, SimpleNamespace(schedule=False)) is True  # a manual run always may
    monkeypatch.setattr("src.scheduler.current_slot", lambda times, now=None: "16:10")
    assert main._market_review_due(config, SimpleNamespace(schedule=True)) is True
    assert main._market_review_due(SimpleNamespace(market_review_times=[]), SimpleNamespace(schedule=True)) is True


def _result(code, advice, score):
    return SimpleNamespace(code=code, operation_advice=advice, sentiment_score=score, decision_type=None,
                           action=None, report_language="en")


def test_changes_only_brief_keeps_changed_calls(monkeypatch):
    monkeypatch.setattr("src.schemas.decision_action.display_decision_type_for_result",
                        lambda result, report_language="en": {"Buy": "buy", "Sell": "sell"}.get(result.operation_advice, "hold"))
    morning = datetime(2026, 9, 25, 9, 50)
    earlier = [SimpleNamespace(id=1, code="AAA", operation_advice="Watch", sentiment_score=50, created_at=morning),
               SimpleNamespace(id=2, code="BBB", operation_advice="Watch", sentiment_score=50, created_at=morning),
               SimpleNamespace(id=3, code="CCC", operation_advice="Watch", sentiment_score=45, created_at=morning)]
    db = SimpleNamespace(get_analysis_history=lambda **kwargs: earlier)
    config = SimpleNamespace(brief_changes_only_times=["12:00"], schedule_times=["09:40", "12:00", "16:10"],
                             report_language="en")
    results = [_result("AAA", "Buy", 70), _result("BBB", "Watch", 52), _result("CCC", "Watch", 58),
               _result("NEW", "Watch", 50)]
    changed, header = changes_only(results, db, config, "q", now=datetime(2026, 9, 25, 12, 15))
    assert [r.code for r in changed] == ["AAA", "CCC", "NEW"]  # bucket change, 13-point move, new name
    assert header == "_12:00 update: only calls that changed since 09:50 (3 of 4)._"
    assert changes_only(results, db, config, "q", now=datetime(2026, 9, 25, 16, 20)) is None  # full brief at 16:10
    unchanged = [_result("AAA", "Watch", 51), _result("BBB", "Watch", 50)]
    changed, header = changes_only(unchanged, db, config, "q", now=datetime(2026, 9, 25, 12, 15))
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
