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
    assert target["spy_return_pct"] is None  # no stock close on the signal day: no aligned benchmark
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
    bars = {"SPY": _bars("2026-08-31", [(500, 501, 499, 500)] + [(500, 502, 499, 501)] * 5),
            "AAA": _bars("2026-09-01", [(100, 107, 99, 106)]),
            "BBB": _bars("2026-09-01", [(50, 53, 49, 52)]),
            "CCC": _bars("2026-09-01", [(20, 20.5, 19.5, 20.2)] * 3)}
    assert it.settle_open(repo, date(2026, 9, 30), bars=lambda tickers: bars) == 2
    stats = it.track_record(repo, datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert stats["groups"]["medium"]["closed"] == 1 and stats["groups"]["rejected"]["closed"] == 1
    assert stats["groups"]["breakout"]["open"] == 1
    text = it.format_track_record(stats)
    assert text.startswith("📒 **Idea track record**") and "Rejected by the review: 1 finished" in text
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
    with_signal = [{"date": signal_day, "open": price, "high": price, "low": price, "close": price}] + later
    daily = {"SPY": spy, "AAA": with_signal, "BBB": with_signal}
    part = it.settle_verdict({"signal_day": signal_day, "entry": price}, later[:6], spy, date.fromisoformat(later[5]["date"]))
    assert "status" not in part and part["return_5d_pct"] == pytest.approx(5.0) and "return_10d_pct" not in part
    it.settle_open(repo, date.fromisoformat(later[-1]["date"]), bars=lambda tickers: daily,
                   nx_bars=lambda tickers: {t: long_history for t in tickers})
    records = {r["ticker"]: r for r in repo.tracked_ideas()}
    aaa, bbb = records["AAA"], records["BBB"]
    assert aaa["status"] == "closed" and aaa["return_10d_pct"] == pytest.approx(10.0) and aaa["spy_10d_pct"] == pytest.approx(5.0)
    assert aaa["nx"]["slow"] == "above" and aaa["nx"]["alignment"] == "against"  # a bearish call above the tunnel
    assert bbb["nx"]["alignment"] is None  # watch takes no side
    repo.set_setting(it.PEERS_SETTING, {signal_day: {"h10": 4.0}})
    stats = it.track_record(repo, now=datetime.now(timezone.utc))
    assert stats["recent"] == [] and not any(g["closed"] for g in stats["groups"].values())  # kept apart from ideas
    cell = stats["verdicts"]["bearish"]["by_nx"]["above"]
    assert cell["closed"] == 1 and cell["avg_10d_vs_spy_pct"] == pytest.approx(5.0)
    assert cell["avg_10d_vs_peers_pct"] == pytest.approx(6.0)
    assert stats["verdicts"]["watch"]["by_nx"]["all"]["closed"] == 1
    text = it.format_track_record(stats)
    assert "Report calls, 10 sessions later vs same-day stocks" in text and "• Bearish calls: above 1 · +6.00%" in text


def test_tracker_job_reads_report_calls_before_settling(repo, monkeypatch):
    created = datetime(2026, 9, 29, 9, 45)
    monkeypatch.setattr(it, "_trading_day", lambda day: True)
    job = it.TrackerJob(repo, lambda *a: None, bars=lambda tickers: {"SPY": [], "NVDA": []},
                        nx_bars=lambda tickers: {}, reports=lambda: [_report("NVDA", "reduce", 180.0, created)])
    job._run(date(2026, 9, 29), datetime(2026, 9, 29, 20, 45, tzinfo=timezone.utc))
    job.stop()
    assert [r["kind"] for r in repo.tracked_ideas()] == ["verdict"]


def test_records_without_bars_close_as_no_data_after_a_month(repo):
    repo.track_idea("idea:q:GONE", _record(ticker="GONE", signal_day="2026-08-01"))
    repo.track_idea("idea:q:NEW", _record(ticker="NEW", signal_day="2026-09-25"))
    spy = _bars("2026-07-31", [(100, 100, 100, 100.0)] * 60)
    it.settle_open(repo, date(2026, 9, 30), bars=lambda tickers: {}, nx_bars=lambda tickers: {})
    assert all(r["status"] == "open" for r in repo.tracked_ideas())  # a failed download closes nothing
    it.settle_open(repo, date(2026, 9, 30), bars=lambda tickers: {"SPY": spy}, nx_bars=lambda tickers: {})
    records = {r["ticker"]: r for r in repo.tracked_ideas()}
    assert records["GONE"]["status"] == "closed" and records["GONE"]["reason"] == "no_data"
    assert records["NEW"]["status"] == "open"
    assert it.track_record(repo, now=datetime.now(timezone.utc))["groups"]["medium"]["closed"] == 0  # no return: not counted


def test_dashed_tickers_find_dotted_bars(repo):
    long_history = _rising("2025-06-01", 400)
    signal_day = long_history[-1]["date"]
    repo.track_idea("idea:d:BRK-B", _record(ticker="BRK-B", signal_day=signal_day, entry=long_history[-2]["close"]))
    it.settle_open(repo, date.fromisoformat(signal_day), bars=lambda tickers: {"SPY": [], "BRK.B": []},
                   nx_bars=lambda tickers: {"BRK.B": long_history})
    assert repo.tracked_ideas()[0]["nx"]["alignment"] == "agree"


def test_a_failed_run_is_retried_after_half_an_hour_and_a_success_ends_the_day(repo, monkeypatch):
    monkeypatch.setattr(it, "_trading_day", lambda day: True)
    calls = []

    def flaky(tickers):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("yahoo down")
        return {"SPY": []}

    repo.track_idea("idea:r:AAA", _record())
    job = it.TrackerJob(repo, lambda *a: None, bars=flaky, nx_bars=lambda tickers: {}, reports=lambda: [])
    start = datetime(2026, 9, 29, 20, 31, tzinfo=timezone.utc)  # 16:31 New York
    job._run(date(2026, 9, 29), start)
    assert job._done_day is None and job._retry_after == start + timedelta(minutes=30)
    job.tick(start + timedelta(minutes=5))
    assert job._task is None  # waiting out the retry delay
    job._run(date(2026, 9, 29), start + timedelta(minutes=31))
    assert job._done_day == date(2026, 9, 29) and job._retry_after is None and len(calls) == 2
    job.stop()


def test_weekly_summary_is_sent_when_only_report_calls_have_closed(repo, monkeypatch):
    monkeypatch.setattr(it, "_trading_day", lambda day: True)
    monkeypatch.setattr(it, "_last_trading_day_of_week", lambda day: True)
    repo.track_idea("verdict:2026-09-01:AAA", {"kind": "verdict", "verdict": "watch", "group": "watch", "ticker": "AAA",
                                                "direction": "none", "signal_day": "2026-09-01", "entry": 10.0})
    record = repo.tracked_ideas()[0]
    repo.update_tracked_idea(record["id"], {**{k: v for k, v in record.items() if k not in {"id", "status", "created_at"}},
                                            "return_10d_pct": 2.0, "spy_10d_pct": 1.0, "return_pct": 2.0}, "closed")
    sent = []
    job = it.TrackerJob(repo, lambda *a: sent.append(a), bars=lambda tickers: {"SPY": []},
                        nx_bars=lambda tickers: {}, reports=lambda: [])
    job._run(date(2026, 10, 2), datetime(2026, 10, 2, 20, 45, tzinfo=timezone.utc))
    job.stop()
    assert sent and sent[0][0] == "track_record" and "Report calls" in sent[0][1]["message"]


def test_repository_lookups_by_id_prefix_and_conversation(repo):
    repo.track_idea("verdict:2026-09-01:AAA", {"kind": "verdict", "ticker": "AAA"})
    repo.track_idea("idea:s:AAA", {"kind": "idea", "ticker": "AAA"})
    assert repo.tracked_idea_ids("verdict:") == {"verdict:2026-09-01:AAA"}
    first = repo.create_advice({"ticker": "AAA"})
    second = repo.create_advice({"ticker": "AAA", "parent_advice_id": first["id"]})
    repo.create_advice({"ticker": "BBB"})
    assert [job["id"] for job in repo.conversation_advice(first["conversation_id"])] == [second["id"], first["id"]]


def test_the_scoreboard_puts_every_source_side_by_side_with_a_cautious_verdict():
    def closed(kind, n, excess, **extra):
        rows = []
        for i in range(n):
            month = 7 + i % 3
            row = {"kind": kind, "status": "closed", "ticker": f"{kind}{i}", "signal_day": f"2026-{month:02d}-{10 + i % 15:02d}", **extra}
            if kind in ("idea", "breakout"):
                row.update(return_pct=excess + 1.0 + (i % 5) * 0.1)  # the same-day long and short both made +1.0
            elif kind == "verdict":
                row.update(benchmark_return_10d_pct=excess + 0.5 + (i % 5) * 0.1)
            else:
                row.update(benchmark_return_20d_pct=excess + 2.0 + (i % 5) * 0.1)
            rows.append(row)
        return rows
    peers = {f"2026-{month:02d}-{day:02d}": {"long": 1.0, "short": 1.0, "h10": 0.5, "h20": 2.0}
             for month in (7, 8, 9) for day in range(10, 25)}
    everything = (closed("idea", 36, 1.2, verdict="high", direction="long")
                  + closed("idea", 12, -0.5, verdict="rejected", direction="short")
                  + closed("verdict", 40, -1.0, group="bearish", direction="short")
                  + closed("influencer", 5, 3.0, channel="Meet Kevin", direction="long")
                  + [{"kind": "social", "status": "open", "signal_day": "2026-09-29"}])
    board = {item["key"]: item for item in it.scoreboard(everything, peers, date(2026, 12, 1))}
    assert board["idea:high"]["verdict"] == "ahead" and board["idea:high"]["closed"] == 36
    assert board["idea:high"]["avg_vs_peers_pct"] == pytest.approx(1.4, abs=0.01)
    assert board["idea:rejected"]["verdict"] == "too_early"
    # A bearish report call on a stock that lagged the same-day stocks by 1% (plus noise) is 1% in the call's favour.
    assert board["verdict:bearish"]["avg_vs_peers_pct"] == pytest.approx(0.8, abs=0.01)
    assert board["verdict:bearish"]["verdict"] == "ahead"
    assert board["youtube:Meet Kevin"]["verdict"] == "too_early" and board["social"]["open"] == 1
    assert "breakout" not in board  # nothing tracked, nothing listed


def test_a_few_months_need_a_larger_t_before_a_source_is_called_ahead():
    def month_rows(month, excess, n=12):
        return [{"kind": "idea", "verdict": "high", "ticker": f"T{month}{i}", "direction": "long", "status": "closed",
                 "signal_day": f"2026-{month:02d}-{10 + i:02d}", "return_pct": 1.0 + excess}
                for i in range(n)]
    peers = {f"2026-{month:02d}-{10 + i:02d}": {"long": 1.0} for month in (7, 8, 9) for i in range(12)}
    # Monthly means 1.0, 0.2 and 0.6: t = 2.6, past 2 but short of the 4.30 three months need.
    board = {item["key"]: item for item in it.scoreboard(month_rows(7, 1.0) + month_rows(8, 0.2) + month_rows(9, 0.6),
                                                         peers, date(2026, 12, 1))}
    assert board["idea:high"]["t"] == pytest.approx(2.6, abs=0.01)
    assert board["idea:high"]["t_needed"] == pytest.approx(4.30) and board["idea:high"]["verdict"] == "no_difference"
    assert it.t_needed(1) is None and it.t_needed(2) == pytest.approx(12.71) and it.t_needed(40) == pytest.approx(1.96)


def test_a_halted_ticker_closes_at_its_last_price_instead_of_staying_open(repo):
    repo.track_idea("social:2026-09-01:PUMP", {"kind": "social", "group": "hot", "verdict": "social", "ticker": "PUMP",
                                              "direction": "long", "signal_day": "2026-09-01", "entry": None})
    spy = _bars("2026-08-31", [(100, 100, 100, 100.0)] * 30)
    pump = _bars("2026-08-31", [(10, 10, 10, 10.0)] + [(8, 8, 8, 8.0)] * 3 + [(1.2, 1.2, 1.2, 1.2)])  # halted after 4 sessions
    it.settle_open(repo, date(2026, 10, 1), bars=lambda tickers: {"SPY": spy, "PUMP": pump}, nx_bars=lambda tickers: {})
    [row] = repo.tracked_ideas()
    assert row["status"] == "closed" and row["reason"] == "halted"
    assert row["return_20d_pct"] == pytest.approx(-88.0) and row["spy_20d_pct"] == pytest.approx(0.0)


def test_the_signal_close_is_kept_on_the_signal_day_so_a_split_the_next_day_is_caught(repo):
    repo.track_idea("breakout:2026-09-01:SPLT:up", {"kind": "breakout", "verdict": "breakout", "ticker": "SPLT",
                                                    "direction": "long", "signal_day": "2026-09-01", "entry": 100.0,
                                                    "stop": 90.0, "target": 130.0})
    spy = _bars("2026-08-31", [(100, 100, 100, 100.0)] * 12)
    signal_day = _bars("2026-08-31", [(100, 101, 99, 100.0)])  # the after-close run on the signal day
    it.settle_open(repo, date(2026, 9, 1), bars=lambda tickers: {"SPY": spy, "SPLT": signal_day}, nx_bars=lambda tickers: {})
    assert repo.tracked_ideas()[0]["ref_close"] == pytest.approx(100.0)
    # A 10:1 split goes ex the next day: the whole history is rescaled.
    after = [{**bar, **{k: bar[k] / 10 for k in ("open", "high", "low", "close")}} for bar in _bars("2026-08-31", [(100, 101, 99, 100.0)] * 2)]
    it.settle_open(repo, date(2026, 9, 2), bars=lambda tickers: {"SPY": spy, "SPLT": after}, nx_bars=lambda tickers: {})
    [row] = repo.tracked_ideas()
    assert row["status"] == "open" and row["return_pct"] == pytest.approx(-0.05)  # not a -90% stop-out


def test_records_wait_when_spy_is_missing_instead_of_closing_without_a_benchmark(repo):
    repo.track_idea("social:2026-09-01:AAA", {"kind": "social", "group": "hot", "verdict": "social", "ticker": "AAA",
                                             "direction": "long", "signal_day": "2026-09-01", "entry": None})
    aaa = _bars("2026-08-31", [(10, 10, 10, 10.0)] * 30)
    assert it.settle_open(repo, date(2026, 10, 1), bars=lambda tickers: {"AAA": aaa}, nx_bars=lambda tickers: {}) == 0
    assert repo.tracked_ideas()[0]["status"] == "open"


def test_a_split_after_the_signal_does_not_skew_returns(repo):
    repo.track_idea("influencer:v1:SPLT", {"kind": "influencer", "group": "bullish", "ticker": "SPLT", "direction": "long",
                                           "signal_day": "2026-09-01", "entry": None, "channel": "x"})
    repo.track_idea("breakout:2026-09-01:SPLT:up", {"kind": "breakout", "verdict": "breakout", "ticker": "SPLT",
                                                    "direction": "long", "signal_day": "2026-09-01", "entry": 100.0,
                                                    "stop": 90.0, "target": 130.0})
    spy = _bars("2026-08-31", [(100, 100, 100, 100.0)] * 12)
    before = _bars("2026-08-31", [(100, 101, 99, 100.0)] * 3)
    it.settle_open(repo, date(2026, 9, 3), bars=lambda tickers: {"SPY": spy, "SPLT": before}, nx_bars=lambda tickers: {})
    # A 1:10 reverse split later: the provider rescales the whole history by 10.
    after = [{**bar, **{k: bar[k] * 10 for k in ("open", "high", "low", "close")}} for bar in _bars("2026-08-31", [(100, 101, 99, 100.0)] * 6)]
    it.settle_open(repo, date(2026, 9, 6), bars=lambda tickers: {"SPY": spy, "SPLT": after}, nx_bars=lambda tickers: {})
    rows = {row["kind"]: row for row in repo.tracked_ideas()}
    assert rows["influencer"]["mark_return_pct"] == pytest.approx(0.0) and rows["influencer"]["entry"] == pytest.approx(1000.0)
    assert rows["breakout"]["status"] == "open" and rows["breakout"]["return_pct"] == pytest.approx(-0.05)  # not +900%
    assert rows["breakout"]["entry"] == 100.0  # the printed price is kept


@pytest.mark.parametrize("kind", ["verdict", "idea", "breakout"])
def test_benchmark_excludes_the_intraday_move_before_the_signal_day_close(kind):
    record = _record(kind=kind, status="closed")
    stock = _bars("2026-08-31", [(100, 110, 100, 110), (110, 111, 109, 110)])
    spy = _bars("2026-08-31", [(500, 550, 500, 550), (550, 551, 549, 550)])
    update = (it.settle_verdict(record, stock, spy, date(2026, 9, 2), horizons=(1,)) if kind == "verdict"
              else it.settle(record, stock, spy, date(2026, 9, 2)))
    stock_key, spy_key = ("return_1d_pct", "spy_1d_pct") if kind == "verdict" else ("return_pct", "spy_return_pct")
    assert update[stock_key] > 0  # keep the alert-entry simulation separate
    assert it._excess({**record, **update}, stock_key, spy_key) == 0
    assert it._excess({**record, **update, "direction": "short"}, stock_key, spy_key) == 0


def test_benchmark_never_substitutes_a_different_spy_session():
    stock = [{"date": "2026-09-01", "close": 100}, {"date": "2026-09-02", "close": 110}]
    spy = [{"date": "2026-08-31", "close": 500}, {"date": "2026-09-02", "close": 550}]
    assert it._aligned_returns(stock, spy, "2026-09-01", "2026-09-02") == (None, None)
    assert it._excess({"kind": "idea", "direction": "long", "return_pct": 10, "spy_return_pct": 0},
                      "return_pct", "spy_return_pct") is None  # old unequal windows are not used


def test_closed_legacy_benchmarks_are_rebuilt_without_changing_trade_results(repo):
    record = _record(return_pct=9.9, r=3.3, exit_day="2026-09-02", exit=110, reason="target", spy_return_pct=0)
    repo.track_idea("legacy", record)
    repo.update_tracked_idea("legacy", record, "closed")
    bars = {"AAA": _bars("2026-08-31", [(100, 110, 100, 110), (110, 111, 109, 110)]),
            "SPY": _bars("2026-08-31", [(500, 550, 500, 550), (550, 551, 549, 550)])}
    assert it.settle_open(repo, date(2026, 9, 3), bars=lambda tickers: bars) == 0
    [saved] = repo.tracked_ideas()
    assert saved["return_pct"] == 9.9 and saved["r"] == 3.3 and saved["exit"] == 110
    assert saved["benchmark_return_pct"] == 0 and it._excess(saved, "return_pct", "spy_return_pct") == 0


def _finished_idea(ticker, signal_day, ret, verdict="medium", direction="long", **extra):
    return {"id": f"idea:{signal_day}:{ticker}:{verdict}", "kind": "idea", "verdict": verdict, "ticker": ticker,
            "direction": direction, "signal_day": signal_day, "status": "closed", "return_pct": ret,
            "created_at": signal_day, **extra}


def test_an_idea_counts_once_and_only_with_all_its_sessions_behind_it():
    today = date(2026, 10, 30)
    rows = [
        _finished_idea("AAA", "2026-09-01", -3.0),  # first listing
        _finished_idea("AAA", "2026-09-02", 9.0),  # relisted the next day: the same idea
        _finished_idea("AAA", "2026-09-03", 9.0, verdict="high"),  # relisted at another conviction: still the same idea
        _finished_idea("AAA", "2026-09-04", 9.0, verdict="rejected"),  # the review's other answer is its own record
        _finished_idea("AAA", "2026-09-03", 9.0, direction="short"),  # the other side is another idea
        _finished_idea("AAA", "2026-10-15", -5.0),  # stopped out early: not all sessions behind it yet
        {**_finished_idea("BBB", "2026-09-01", None), "status": "open", "return_pct": 2.0},  # no exit yet
        {**_finished_idea("GONE", "2026-09-01", None), "reason": "no_data"},  # closed without a result
    ]
    finished, running = it._split_ideas(rows, today)
    assert [(r["ticker"], r["signal_day"], r["direction"], r["verdict"]) for r in finished] == [
        ("AAA", "2026-09-01", "long", "medium"), ("AAA", "2026-09-03", "short", "medium"),
        ("AAA", "2026-09-04", "long", "rejected")]
    assert [(r["ticker"], r["signal_day"]) for r in running] == [("BBB", "2026-09-01"), ("AAA", "2026-10-15")]
    # A record outside MAX_DAYS sessions of the first is a new idea.
    finished, _ = it._split_ideas([_finished_idea("AAA", "2026-08-03", 1.0), _finished_idea("AAA", "2026-09-01", 2.0)], today)
    assert len(finished) == 2


def test_the_summary_grades_finished_ideas_against_same_day_stocks(repo):
    for record in [_finished_idea("AAA", "2026-09-01", -3.0, spy_return_pct=1.0, benchmark_return_pct=-2.0),
                   _finished_idea("AAA", "2026-09-02", 9.0, spy_return_pct=1.0, benchmark_return_pct=8.0)]:
        repo.track_idea(record["id"], {k: v for k, v in record.items() if k not in {"id", "status", "created_at"}})
        repo.update_tracked_idea(record["id"], {k: v for k, v in record.items() if k not in {"id", "status", "created_at"}}, "closed")
    repo.set_setting(it.PEERS_SETTING, {"2026-09-01": {"long": 2.0}})
    stats = it.track_record(repo, now=datetime(2026, 10, 30, 21, tzinfo=timezone.utc), window_days=3650)
    medium = stats["groups"]["medium"]
    assert medium["closed"] == 1 and medium["avg_return_pct"] == -3.0 and medium["win_rate"] == 0
    assert medium["avg_vs_peers_pct"] == pytest.approx(-5.0) and medium["avg_vs_spy_pct"] == pytest.approx(-3.0)
    assert "vs same-day stocks -5.00%" in it.format_track_record(stats)
    # Not measured yet: left out, not compared against something else.
    repo.set_setting(it.PEERS_SETTING, {})
    assert it.track_record(repo, now=datetime(2026, 10, 30, 21, tzinfo=timezone.utc), window_days=3650)["groups"]["medium"]["avg_vs_peers_pct"] is None


def test_the_review_is_judged_on_same_day_same_direction_pairs():
    days = [f"2026-{month:02d}-{day:02d}" for month in (7, 8) for day in (6, 7, 8, 9, 10, 13)]
    finished = [row for i, day in enumerate(days) for row in (
        _finished_idea(f"A{i}", day, 1.5 if day < "2026-08" else 1.51), _finished_idea(f"R{i}", day, 0.5, verdict="rejected"))]
    finished.append(_finished_idea("LONE", "2026-09-01", 50.0))  # no rejected idea that day: not a pair
    finished.append(_finished_idea("SIDE", "2026-07-06", -50.0, verdict="rejected", direction="short"))  # other direction
    value = it.review_value(finished)
    assert value["days"] == 12 and value["gap_pct"] == pytest.approx(1.005) and value["months"] == 2
    assert value["t_needed"] == pytest.approx(12.71) and value["verdict"] == "helps"
    assert it.review_value(finished[:6])["verdict"] == "too_early"
    # Months that disagree are not a verdict, however large the average gap looks.
    mixed = [{**row, "return_pct": row["return_pct"] + (3.0 if row["signal_day"] < "2026-08" and row["verdict"] == "medium" else 0.0)}
             for row in finished[:24]]
    assert it.review_value(mixed)["verdict"] == "no_difference"


def _stock_bars(close=100.0, step=0.0, sessions=60, high=None, low=None):
    days = it._session_days("2026-07-01", date(2026, 10, 30))[:sessions]
    return [{"date": day, "open": close + i * step, "high": (high or close + i * step + 1), "low": (low or close + i * step - 1),
             "close": close + i * step, "volume": 1} for i, day in enumerate(days)]


def test_same_day_stocks_use_the_ideas_rule_and_each_horizon(monkeypatch):
    monkeypatch.setattr(it, "PEER_MIN_STOCKS", 2)
    flat, rising = _stock_bars(), _stock_bars(step=1.0)
    histories = [{"bars": bars, "dates": [bar["date"] for bar in bars]} for bars in (flat, rising)]
    day = flat[20]["date"]
    measured = it.peer_baseline(day, histories, date(2026, 10, 30))
    entry = rising[20]["close"]
    assert measured["h5"] == pytest.approx((rising[25]["close"] / entry - 1) * 100 / 2, abs=0.001)
    assert measured["h20"] == pytest.approx((rising[40]["close"] / entry - 1) * 100 / 2, abs=0.001)
    # Flat stock: no exit, closes at the limit for the costs; rising stock: 3 ATR target long, 1.5 ATR stop short.
    assert measured["long"] > 0 and measured["short"] < 0
    # Only the sessions that have happened count: no trade result before the limit is reached.
    early = it.peer_baseline(day, histories, date.fromisoformat(flat[26]["date"]))
    assert "h5" in early and "h10" not in early and "long" not in early
    # Too few stocks is not "the average stock".
    assert it.peer_baseline(day, histories[:1], date(2026, 10, 30)) == {}


def test_peer_baselines_download_only_for_a_new_horizon(repo, monkeypatch):
    monkeypatch.setattr(it, "PEER_MIN_STOCKS", 1)
    bars = _stock_bars(step=0.5)
    day = bars[20]["date"]
    repo.track_idea("idea:x:AAA", _record(signal_day=day))
    calls = []

    def download(tickers):
        calls.append(list(tickers))
        return {"BRK.B": bars}
    measured_on = date.fromisoformat(bars[26]["date"])  # 6 sessions on: only the 5-session horizon is reachable
    assert it.update_peer_baselines(repo, measured_on, download, lambda: ["BRK-B"]) == 1
    assert set(repo.setting(it.PEERS_SETTING)[day]) == {"h5"}
    assert it.update_peer_baselines(repo, measured_on, download, lambda: ["BRK-B"]) == 0 and len(calls) == 1
    later = date.fromisoformat(bars[45]["date"])
    assert it.update_peer_baselines(repo, later, download, lambda: ["BRK-B"]) == 1
    assert set(repo.setting(it.PEERS_SETTING)[day]) == {"h5", "h10", "h20", "long", "short"}
    assert len(calls) == 2
    assert it.update_peer_baselines(repo, later, lambda tickers: 1 / 0, lambda: ["BRK-B"]) == 0  # complete: no download
    # A signal day older than the download reaches is not fetched again every day.
    repo.track_idea("idea:x:OLD", _record(ticker="OLD", signal_day="2025-01-02"))
    assert it.update_peer_baselines(repo, later, lambda tickers: 1 / 0, lambda: ["BRK-B"]) == 0


def test_a_failed_peer_download_does_not_stop_settling_or_the_summary(repo, monkeypatch):
    monkeypatch.setattr(it, "_trading_day", lambda day: day.weekday() < 5)
    monkeypatch.setattr(it, "_last_trading_day_of_week", lambda day: True)
    it.record_breakout(repo, "CCC", "long", 20.0, 19.0, 22.0, date(2026, 9, 1))
    sent = []

    def broken():
        raise RuntimeError("no universe")
    job = it.TrackerJob(repo, lambda *a: sent.append(a), bars=lambda tickers: {"SPY": []}, nx_bars=lambda tickers: {},
                        reports=lambda: [], universe=broken)
    job._run(date(2026, 10, 2), datetime(2026, 10, 2, 20, 45, tzinfo=timezone.utc))
    job.stop()
    assert job.last_error is None and sent and sent[0][0] == "track_record"


def _stored_report(report_id, code, created, answer, action="watch", name=""):
    import json
    from src.storage import AnalysisHistory
    raw = {"action": action, "name": name, "current_price": 100.0, "raw_response": answer}
    return AnalysisHistory(id=report_id, query_id=str(report_id), code=code, name=name, report_type="simple",
                           raw_result=json.dumps(raw), created_at=created)


def test_report_calls_keep_the_models_own_call_before_any_guard(repo):
    answer = '```json\n{"sentiment_score": 78, "decision_type": "buy", "operation_advice": "Buy"}\n```'
    created = datetime(2026, 9, 29, 9, 45)
    [record] = [it.verdict_record(SimpleNamespace(id=7, code="NVDA", report_type="simple", sentiment_score=52, created_at=created,
                                                  raw_result='{"action": "watch", "current_price": 180.0, "raw_response": %s}' % __import__("json").dumps(answer)))]
    assert record[1]["group"] == "watch" and record[1]["model_call"] == "buy" and record[1]["geared"] is False
    geared = it.verdict_record(SimpleNamespace(id=8, code="SOXL", report_type="simple", sentiment_score=52, created_at=created,
                                               raw_result='{"action": "watch", "name": "Direxion Daily Semiconductor Bull 3X", "current_price": 20.0}'))
    assert geared[1]["model_call"] is None and geared[1]["geared"] is True


def test_older_report_calls_are_filled_in_once_from_their_stored_report(repo):
    from src.storage import AnalysisHistory
    AnalysisHistory.__table__.create(repo.db._engine)
    with repo.db.session_scope() as session:
        session.add(_stored_report(1, "AAA", datetime(2026, 9, 1, 9, 45), '{"decision_type": "sell"}'))
        session.add(_stored_report(2, "SOXL", datetime(2026, 9, 1, 9, 45), '{"decision_type": "buy"}',
                                   name="Direxion Daily Semiconductor Bull 3X"))
    base = {"kind": "verdict", "verdict": "watch", "group": "watch", "direction": "none", "signal_day": "2026-09-01", "entry": 10.0}
    repo.track_idea("verdict:2026-09-01:AAA", {**base, "ticker": "AAA", "report_id": 1})
    repo.track_idea("verdict:2026-09-01:SOXL", {**base, "ticker": "SOXL", "report_id": 2})
    repo.track_idea("verdict:2026-09-01:OLD", {**base, "ticker": "OLD", "report_id": 99})  # its report is gone
    assert it.fill_model_calls(repo) == 3
    records = {r["ticker"]: r for r in repo.tracked_ideas()}
    assert records["AAA"]["model_call"] == "sell" and records["AAA"]["status"] == "open"
    assert records["SOXL"]["model_call"] == "buy" and records["SOXL"]["geared"] is True
    assert records["OLD"]["model_call"] is None
    assert it.fill_model_calls(repo) == 0


def test_model_calls_are_measured_against_the_rest_of_the_watchlist():
    def call(ticker, model_call, ret, verdict="watch", **extra):
        return {"kind": "verdict", "ticker": ticker, "model_call": model_call, "verdict": verdict, "group": "watch",
                "direction": "none", "signal_day": "2026-09-01", "status": "closed", "return_10d_pct": ret,
                "benchmark_return_10d_pct": ret, **extra}
    rows = [call("A", "buy", 2.0, verdict="buy"), call("B", "buy", 0.0), call("C", "hold", -2.0), call("D", "sell", -8.0),
            call("LEV", "buy", 90.0, geared=True)]
    stats = it.model_call_stats(it._stock_calls(rows))
    assert stats["buy"]["closed"] == 2 and stats["buy"]["shown"] == 1
    assert stats["buy"]["avg_10d_vs_watchlist_pct"] == pytest.approx(3.0)  # 1.0 against a watchlist average of -2.0
    assert stats["sell"]["avg_10d_vs_watchlist_pct"] == pytest.approx(-6.0)
    text = it.format_track_record({"window_days": 90, "groups": {key: {"closed": 0, "open": 0} for key, _ in it.GROUPS},
                                   "model_calls": stats})
    assert "Buy 2 · +3.00% | Hold 1 · +0.00% | Sell 1 · -6.00% (1 of 2 Buys were shown as Buy)" in text
    # On the scoreboard a Sell gains when the stock lags, whatever the final call was.
    peers = {"2026-09-01": {"h10": -2.0}}
    board = {item["key"]: item for item in it.scoreboard(rows, peers, date(2026, 10, 30))}
    assert board["model:sell"]["avg_vs_peers_pct"] == pytest.approx(6.0)
    assert board["model:buy_shown"]["avg_vs_peers_pct"] == pytest.approx(4.0) and board["model:buy_shown"]["closed"] == 1
    assert board["model:buy_softened"]["avg_vs_peers_pct"] == pytest.approx(2.0) and board["model:buy_softened"]["closed"] == 1


def test_recent_ideas_list_scans_and_breakouts_with_their_rules_verdicts(repo):
    result = {"slot": "2026-11-06 12:00", "day": date(2026, 11, 6), "tracked": [
        {"ticker": "AAA", "direction": "long", "price": 100.0, "stop": 97.0, "targets": [106.0], "verdict": "high"},
        {"ticker": "BBB", "direction": "short", "price": 50.0, "stop": 52.0, "targets": [46.0], "verdict": "rejected"}]}
    it.record_scan(repo, result)
    it.record_breakout(repo, "CCC", "long", 20.0, 19.0, 22.0, date(2026, 11, 6))
    repo.track_idea("social:2026-11-06:DDD", {"kind": "social", "ticker": "DDD", "direction": "long", "signal_day": "2026-11-06"})
    out = it.recent_ideas(repo, days=7)
    assert {(item["ticker"], item["kind"], item["verdict"]) for item in out["items"]} == {
        ("AAA", "idea", "high"), ("BBB", "idea", "rejected"), ("CCC", "breakout", "breakout")}  # social picks are not ideas
    aaa = next(item for item in out["items"] if item["ticker"] == "AAA")
    assert (aaa["entry"], aaa["stop"], aaa["target"], aaa["status"]) == (100.0, 97.0, 106.0, "open")
    assert [rule["id"] for rule in out["rules"]["idea"]] == ["swing_trend", "model_review"]
    assert out["rules"]["breakout"][0]["verdict"] == "no edge shown"
    assert out["discord"] == {"idea": False, "breakout": False}  # nothing has passed: dashboard only
    assert it.recent_ideas(repo, days=7, now=datetime.now(timezone.utc) + timedelta(days=30))["items"] == []
