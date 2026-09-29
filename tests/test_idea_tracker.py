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


def _rising(start, days, first=50.0, step=0.2):
    return _bars(start, [(first + i * step, first + i * step + 0.5, first + i * step - 0.5, first + i * step)
                         for i in range(days)])


def test_nx_snapshot_uses_bars_before_the_signal_and_the_alert_price():
    history = _rising("2025-06-01", 400)  # a steady rise: the slow tunnel sits below recent prices
    signal_day = history[-1]["date"]
    last_close = history[-2]["close"]
    agree = it.nx_snapshot(history, signal_day, last_close, "long")
    assert agree["slow"] == "above" and agree["alignment"] == "agree" and agree["structure"] == "fast_above_slow"
    assert it.nx_snapshot(history, signal_day, last_close, "short")["alignment"] == "against"
    inside = it.nx_snapshot(history, signal_day, (agree["slow_bottom"] + agree["slow_top"]) / 2, "long")
    assert inside["slow"] == "inside" and inside["alignment"] == "neutral"
    # Only bars before the signal day count: a huge bar on the signal day changes nothing.
    spiked = history[:-1] + [{**history[-1], "high": 10_000.0, "close": 9_000.0}]
    assert it.nx_snapshot(spiked, signal_day, last_close, "long") == agree
    assert it.nx_snapshot(history[:100], history[99]["date"], 60.0, "long") is None


def test_settle_open_fills_nx_once_and_summarises_by_alignment(repo):
    long_history = _rising("2025-06-01", 400)
    signal_day = long_history[-1]["date"]
    price = long_history[-2]["close"]
    for ticker, direction in (("AAA", "long"), ("BBB", "short")):
        repo.track_idea(f"idea:x:{ticker}", _record(ticker=ticker, direction=direction, signal_day=signal_day,
                                                     entry=price, stop=price * 0.97 if direction == "long" else price * 1.03,
                                                     target=price * 1.06 if direction == "long" else price * 0.94))
    later = _bars(signal_day, [(price, price * 1.07, price * 0.99, price * 1.06)])  # longs hit target, shorts stop
    daily = {"SPY": later, "AAA": later, "BBB": later}
    fetched = []

    def nx_bars(tickers):
        fetched.append(tuple(tickers))
        return {ticker: long_history for ticker in tickers}

    it.settle_open(repo, date.fromisoformat(later[-1]["date"]), bars=lambda tickers: daily, nx_bars=nx_bars)
    records = {r["ticker"]: r for r in repo.tracked_ideas()}
    assert records["AAA"]["nx"]["alignment"] == "agree" and records["BBB"]["nx"]["alignment"] == "against"
    assert [set(call) for call in fetched] == [{"AAA", "BBB"}]
    stats = it.track_record(repo, now=datetime.now(timezone.utc))
    assert stats["by_nx"]["agree"]["closed"] == 1 and stats["by_nx"]["agree"]["avg_return_pct"] > 0
    assert stats["by_nx"]["against"]["closed"] == 1 and stats["by_nx"]["against"]["avg_return_pct"] < 0
    assert {r["ticker"]: r["nx_alignment"] for r in stats["recent"]} == {"AAA": "agree", "BBB": "against"}
    text = it.format_track_record(stats)
    assert "By your NX slow tunnel" in text and "NX agrees 1" in text and "too few to judge yet" in text
    # Already filled: nothing is fetched again.
    it.settle_open(repo, date.fromisoformat(later[-1]["date"]), bars=lambda tickers: daily, nx_bars=nx_bars)
    assert [set(call) for call in fetched] == [{"AAA", "BBB"}]


def test_open_records_get_nx_even_before_they_settle(repo):
    long_history = _rising("2025-06-01", 400)
    signal_day = long_history[-1]["date"]
    repo.track_idea("idea:y:AAA", _record(signal_day=signal_day, entry=long_history[-2]["close"]))
    it.settle_open(repo, date.fromisoformat(signal_day), bars=lambda tickers: {"SPY": [], "AAA": []},
                   nx_bars=lambda tickers: {"AAA": long_history})
    (record,) = repo.tracked_ideas()
    assert record["status"] == "open" and record["nx"]["alignment"] == "agree"


def test_nx_download_failure_does_not_stop_settling(repo):
    repo.track_idea("idea:z:AAA", _record())

    def broken(tickers):
        raise RuntimeError("network")

    it.settle_open(repo, date(2026, 9, 30), bars=lambda tickers: {"SPY": [], "AAA": []}, nx_bars=broken)
    assert "nx" not in repo.tracked_ideas()[0]


def _report(code, action, price, created, report_id=1, report_type="full"):
    import json
    raw = {"action": action, "current_price": price + 0.5,
           "dashboard": {"data_perspective": {"price_position": {"current_price": price}}}}
    return SimpleNamespace(id=report_id, code=code, report_type=report_type, raw_result=json.dumps(raw),
                           created_at=created, sentiment_score=40)


def test_verdict_records_group_calls_and_keep_the_first_report_of_a_day(repo):
    day = datetime(2026, 9, 21, 9, 45)
    made = it.verdict_record(_report("NVDA", "reduce", 180.0, day))
    assert made[0] == "verdict:2026-09-21:NVDA"
    assert made[1] | {} == {**made[1], "group": "bearish", "direction": "short", "entry": 180.0, "verdict": "reduce"}
    assert it.verdict_record(_report("NVDA", "watch", 180.0, day))[1]["direction"] == "none"
    assert it.verdict_record(_report("600519", "buy", 1500.0, day)) is None  # not a US ticker
    assert it.verdict_record(_report("MARKET", "watch", 1.0, day, report_type="market_review")) is None
    assert it.verdict_record(_report("NVDA", "unknown", 180.0, day)) is None
    reports = [_report("NVDA", "watch", 182.0, day.replace(hour=16), 3), _report("NVDA", "reduce", 180.0, day, 1),
               _report("NVDA", "watch", 181.0, day.replace(hour=12), 2)]
    assert it.sync_verdicts(repo, reports) == 1
    (record,) = repo.tracked_ideas()
    assert record["verdict"] == "reduce" and record["report_id"] == 1  # 09:45 wins
    assert it.sync_verdicts(repo, reports) == 0


def test_verdicts_settle_on_fixed_horizons_and_split_by_nx(repo):
    long_history = _rising("2025-06-01", 400)
    signal_day = long_history[-1]["date"]
    price = long_history[-2]["close"]
    created = datetime.fromisoformat(signal_day + "T09:45:00")
    it.sync_verdicts(repo, [_report("AAA", "reduce", price, created, 1), _report("BBB", "watch", price, created, 2)])
    later = _bars(signal_day, [(price, price * 1.01, price * 0.99, price * (1 + 0.01 * (i + 1))) for i in range(12)])
    spy = _bars(signal_day, [(100, 101, 99, 100 + 0.5 * (i + 1)) for i in range(12)])
    spy = [{"date": signal_day, "open": 100, "high": 100, "low": 100, "close": 100.0, "volume": 1}] + spy
    daily = {"SPY": spy, "AAA": later, "BBB": later}
    part = it.settle_verdict({"signal_day": signal_day, "entry": price}, later[:6], spy, date.fromisoformat(later[5]["date"]))
    assert "status" not in part and part["return_5d_pct"] == pytest.approx(5.0) and "return_10d_pct" not in part
    it.settle_open(repo, date.fromisoformat(later[-1]["date"]), bars=lambda tickers: daily,
                   nx_bars=lambda tickers: {t: long_history for t in tickers})
    records = {r["ticker"]: r for r in repo.tracked_ideas()}
    aaa, bbb = records["AAA"], records["BBB"]
    assert aaa["status"] == "closed" and aaa["return_10d_pct"] == pytest.approx(10.0) and aaa["spy_10d_pct"] == pytest.approx(5.0)
    assert aaa["nx"]["slow"] == "above" and aaa["nx"]["alignment"] == "against"  # a bearish call above the tunnel
    assert bbb["nx"]["alignment"] is None  # watch takes no side
    stats = it.track_record(repo, now=datetime.now(timezone.utc))
    assert stats["recent"] == [] and not any(g["closed"] for g in stats["groups"].values())  # kept apart from ideas
    cell = stats["verdicts"]["bearish"]["by_nx"]["above"]
    assert cell["closed"] == 1 and cell["avg_10d_vs_spy_pct"] == pytest.approx(5.0)
    assert stats["verdicts"]["watch"]["by_nx"]["all"]["closed"] == 1
    text = it.format_track_record(stats)
    assert "Report calls, 10 sessions later vs SPY" in text and "• Bearish calls: above 1 · +5.00%" in text


def test_tracker_job_reads_report_calls_before_settling(repo, monkeypatch):
    created = datetime(2026, 9, 29, 9, 45)
    monkeypatch.setattr(it, "_trading_day", lambda day: True)
    job = it.TrackerJob(repo, lambda *a: None, bars=lambda tickers: {"SPY": [], "NVDA": []},
                        nx_bars=lambda tickers: {}, reports=lambda: [_report("NVDA", "reduce", 180.0, created)])
    job._run(date(2026, 9, 29), datetime(2026, 9, 29, 20, 45, tzinfo=timezone.utc))
    job.stop()
    assert [r["kind"] for r in repo.tracked_ideas()] == ["verdict"]
