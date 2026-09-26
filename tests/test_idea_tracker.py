"""Forward track record: recording ideas and breakouts, settling them, summarising."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.services.trade_desk import idea_tracker as it
from src.services.trade_desk.repository import TradeDeskRepository


@pytest.fixture
def repo():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(engine)

    @contextmanager
    def transaction():
        with sessions() as session:
            with session.begin():
                yield session

    yield TradeDeskRepository(SimpleNamespace(_engine=engine, get_session=sessions, session_scope=transaction))
    engine.dispose()


def _bars(start, rows):
    day = date.fromisoformat(start)
    out = []
    for o, h, l, c in rows:
        day += timedelta(days=1)
        out.append({"date": day.isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 1})
    return out


def _record(**kw):
    return {"kind": "idea", "verdict": "medium", "ticker": "AAA", "direction": "long", "signal_day": "2026-09-01",
            "entry": 100.0, "stop": 97.0, "target": 106.0, **kw}


def test_settle_target_stop_gap_time_and_shorts():
    market = _bars("2026-08-31", [(500, 501, 499, 500)] + [(500, 506, 499, 505)] * 20)
    target = it.settle(_record(), _bars("2026-09-01", [(100, 102, 99, 101), (101, 107, 100, 106)]), market, date(2026, 9, 30))
    assert (target["status"], target["reason"], target["exit"]) == ("closed", "target", 106.0)
    assert target["return_pct"] == pytest.approx(5.9) and target["r"] == pytest.approx(5.9 / 3, abs=0.001)
    assert target["spy_return_pct"] == pytest.approx(1.0)
    gap = it.settle(_record(), _bars("2026-09-01", [(95, 96, 94, 95)]), market, date(2026, 9, 30))
    assert (gap["reason"], gap["exit"]) == ("stop", 95)
    still = it.settle(_record(), _bars("2026-09-01", [(100, 101, 99, 100.5)] * 3), market, date(2026, 9, 30))
    assert "status" not in still and still["days"] == 3 and still["mark"] == 100.5
    timed = it.settle(_record(), _bars("2026-09-01", [(100, 101, 99, 100.5)] * 20), market, date(2026, 9, 30))
    assert (timed["reason"], timed["days"]) == ("time", it.MAX_DAYS)
    short = it.settle(_record(direction="short", stop=103.0, target=94.0),
                      _bars("2026-09-01", [(99, 99.5, 93, 94)]), market, date(2026, 9, 30))
    assert short["reason"] == "target" and short["return_pct"] == pytest.approx(5.9)
    assert it.settle(_record(), _bars("2026-08-20", [(1, 1, 1, 1)]), market, date(2026, 9, 30)) is None


def test_scans_and_breakouts_are_recorded_once_and_summarised(repo):
    result = {"slot": "2026-09-01 12:00", "day": date(2026, 9, 1), "tracked": [
        {"ticker": "AAA", "direction": "long", "price": 100.0, "stop": 97.0, "targets": [106.0], "verdict": "medium"},
        {"ticker": "BBB", "direction": "short", "price": 50.0, "stop": 52.0, "targets": [46.0], "verdict": "rejected"}]}
    assert it.record_scan(repo, result) == 2 and it.record_scan(repo, result) == 0  # a rescan adds nothing
    assert it.record_breakout(repo, "CCC", "long", 20.0, 19.0, 22.0, date(2026, 9, 1))
    bars = {"SPY": _bars("2026-08-31", [(500, 501, 499, 500)] + [(500, 502, 499, 501)] * 20),
            "AAA": _bars("2026-09-01", [(100, 107, 99, 106)]),
            "BBB": _bars("2026-09-01", [(50, 53, 49, 52)]),
            "CCC": _bars("2026-09-01", [(20, 20.5, 19.5, 20.2)] * 3)}
    assert it.settle_open(repo, date(2026, 9, 30), bars=lambda tickers: bars) == 2
    stats = it.track_record(repo, datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert stats["groups"]["medium"]["closed"] == 1 and stats["groups"]["rejected"]["closed"] == 1
    assert stats["groups"]["breakout"]["open"] == 1
    text = it.format_track_record(stats)
    assert text.startswith("📒 **Idea track record**") and "Rejected by the review: 1 closed" in text
    assert "Too few closed ideas yet" in text and "$" not in text


def test_the_weekly_summary_goes_out_on_the_last_trading_day(repo, monkeypatch):
    sent = []
    monkeypatch.setattr(it, "_trading_day", lambda day: day.weekday() < 5)
    job = it.TrackerJob(repo, lambda t, p, k: sent.append((t, k)), bars=lambda tickers: {})
    it.record_breakout(repo, "CCC", "long", 20.0, 19.0, 22.0, date(2026, 9, 24))
    job.tick(datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc))  # Thursday 17:00 New York
    job._task.result(timeout=5)
    assert sent == []
    job.tick(datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc))  # Friday
    job._task.result(timeout=5)
    assert sent == [("track_record", "track-record:2026-39")]
    job.stop()


def test_a_scan_tracks_approved_and_rejected_candidates():
    import sys
    sys.path.insert(0, "tests")
    from test_trade_opportunities import FakeService, NY_MIDDAY, _run_batch, _runner
    clock, service, sent = {"now": NY_MIDDAY}, FakeService(), []
    tracked = []
    service.repo.track_idea = lambda record_id, payload: tracked.append((record_id, payload)) or True
    review = lambda prompt: [{"ticker": "NVDA", "direction": "long", "conviction": "medium"}]  # noqa: E731
    runner = _runner(service, sent, clock, review)
    runner.tick(True)
    _run_batch(runner, service, clock, 10)
    verdicts = {payload["ticker"]: payload["verdict"] for _, payload in tracked}
    assert verdicts["NVDA"] == "medium" and set(verdicts.values()) >= {"medium", "rejected"}
