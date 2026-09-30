"""Social media scan: free sources merged per ticker, report context, the evening digest and its tracking."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.services import social_scan as ss
from src.services.trade_desk import idea_tracker as it
from src.services.trade_desk.repository import TradeDeskRepository

REDDIT = {"results": [
    {"rank": 1, "ticker": "MU", "name": "Micron Technology", "mentions": 325, "rank_24h_ago": 2, "mentions_24h_ago": 180},
    {"rank": 2, "ticker": "SPY", "name": "SPDR S&amp;P 500 ETF Trust", "mentions": 259, "rank_24h_ago": 1,
     "mentions_24h_ago": 241},
    {"rank": 3, "ticker": "NVDA", "name": "NVIDIA", "mentions": 200, "rank_24h_ago": 3, "mentions_24h_ago": None},
    {"rank": 4, "ticker": "AAPL", "name": "Apple", "mentions": 150, "rank_24h_ago": 5, "mentions_24h_ago": 150},
]}
TRENDING = {"symbols": [
    {"symbol": "BTC.X", "region": "US", "title": "Bitcoin", "trends": {"summary": "crypto"}},
    {"symbol": "CAPR", "region": "US", "title": "Capricor", "trends": {"summary": "Trial data renewed focus on the FDA review."}},
    {"symbol": "MU", "region": "US", "title": "Micron", "trends": {"summary": "Earnings beat."}},
]}
WSB = [{"ticker": "AAPL", "sentiment": "Bearish", "sentiment_score": -0.1, "no_of_comments": 41},
       {"ticker": "MU", "sentiment": "Bullish", "sentiment_score": 0.03, "no_of_comments": 35}]
STREAM = {"messages": [{"entities": {"sentiment": {"basic": "Bullish"}}}] * 10
          + [{"entities": {"sentiment": {"basic": "Bearish"}}}] * 3 + [{"entities": {"sentiment": None}}] * 17}


@pytest.fixture(autouse=True)
def fresh_cache():
    ss._cache.clear()
    yield
    ss._cache.clear()


@pytest.fixture
def web(monkeypatch):
    answers = {"apewisdom": REDDIT, "trending": TRENDING, "tradestie": WSB, "streams": STREAM}
    calls = []

    def get(url):
        calls.append(url)
        for key, value in answers.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(url)
    monkeypatch.setattr(ss, "_get_json", get)
    return SimpleNamespace(answers=answers, calls=calls)


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


def test_the_hot_list_ranks_by_sources_then_mentions_and_skips_index_funds_and_crypto(web):
    hot = ss.hot_list()
    assert [row["ticker"] for row in hot] == ["MU", "AAPL", "NVDA", "CAPR"]
    assert hot[0]["sources"] == ["reddit", "stocktwits", "wsb"] and hot[0]["name"] == "Micron Technology"


def test_a_ticker_context_reads_every_source_and_the_message_tags(web):
    context = ss.ticker_context("MU")
    assert context["reddit"]["mentions"] == 325 and context["wsb"]["sentiment"] == "bullish"
    assert context["messages"] == {"messages": 30, "bullish": 10, "bearish": 3}
    line = ss.summary_line(context, "en")
    assert line.startswith("Reddit #1 by mentions (325, +81% vs a day earlier); WSB #2 by comments (bullish); "
                           "Stocktwits trending #3; latest 30 Stocktwits posts: 10 bullish, 3 bearish")
    assert line.endswith("Stocktwits says: Earnings beat.")
    assert "Reddit 提及第 1 名（325 次，较前一日 +81%）" in ss.summary_line(context, "zh")
    section = ss.prompt_section(context)
    assert "不得据此调整评分或买卖结论" in section and "Reddit 提及第 1 名" in section


def test_a_quiet_ticker_says_so_and_a_dead_source_is_named(web):
    web.answers["tradestie"] = RuntimeError("down")
    context = ss.ticker_context("ZZZ")
    assert context["reddit"] is None and context["unavailable"] == ["wsb"]
    assert ss.summary_line(context, "en").startswith("not among the most-discussed names")
    assert "不可用：wsb" in ss.prompt_section(context)


def test_nothing_answering_means_no_context_and_failures_are_not_retried_at_once(web):
    for key in web.answers:
        web.answers[key] = RuntimeError("down")
    assert ss.ticker_context("MU") is None
    count = len(web.calls)
    assert ss.ticker_context("MU") is None and len(web.calls) == count  # cached for FAILURE_SECONDS


def test_report_lines_and_idea_flags(web):
    result = SimpleNamespace(dashboard=None)
    ss.attach(result, ss.ticker_context("MU"))
    lines = ss.report_lines(result.dashboard["data_perspective"], "en")
    assert lines[0].startswith("**Social attention**: Reddit #1") and lines[1] == ""
    assert ss.report_lines({}, "zh") == []
    assert ss.flag("MU") == "🔥 Much discussed: Reddit #1 · WSB #2 · Stocktwits #3 (crowded; context only, not a signal)"
    assert ss.flag("ZZZ") == ""


def test_the_digest_goes_out_once_after_the_close_and_tracks_the_names(web, repo):
    sent = []

    def emit(event_type, payload, key):  # stored like the worker's: a restart sees it
        event = repo.event(event_type, payload, dedup_key=key)
        if event is not None:
            sent.append((event_type, payload, key))
        return event
    picks = [{"channel": "Meet Kevin", "group": "bullish", "ticker": "MU", "reason": "memory upcycle"}]
    job = ss.DigestJob(repo, emit, today_picks=lambda: picks, trading_day=lambda day: True)
    job.tick(datetime(2026, 9, 29, 19, 0, tzinfo=timezone.utc))  # 15:00 New York
    assert job._task is None
    evening = datetime(2026, 9, 29, 20, 25, tzinfo=timezone.utc)
    job.tick(evening)
    job._task.result(timeout=10)
    [(event_type, payload, key)] = sent
    assert event_type == "social_digest" and key == "social-digest:2026-09-29"
    assert "• **MU** Reddit #1 (325, +81% d/d) · WSB #2 bullish · Stocktwits #3" in payload["message"]
    assert "• Meet Kevin: 🟢 **MU** bullish — memory upcycle" in payload["message"]
    tracked = {row["ticker"]: row for row in repo.tracked_ideas()}
    assert set(tracked) == {"MU", "AAPL", "NVDA", "CAPR"} and tracked["MU"]["entry"] is None
    # Chosen after the 09-29 close: entered at the next session's close.
    assert tracked["MU"]["signal_day"] == "2026-09-30" and tracked["MU"]["reddit_change_pct"] == 80.6
    restarted = ss.DigestJob(repo, emit, trading_day=lambda day: True, hot=lambda: [{**ss.hot_list()[0], "ticker": "NEW"}])
    restarted.run(date(2026, 9, 29), evening)
    assert len(sent) == 1 and "NEW" not in {row["ticker"] for row in repo.tracked_ideas()}


def test_a_failed_digest_is_retried_later(repo):
    job = ss.DigestJob(repo, lambda *a: {"id": 1}, hot=lambda: [], trading_day=lambda day: True)
    evening = datetime(2026, 9, 29, 20, 25, tzinfo=timezone.utc)
    job.run(date(2026, 9, 29), evening)
    assert job._done_day is None and job._retry_at > evening


def test_social_names_settle_from_the_signal_day_close_over_twenty_sessions(repo):
    ids = ss.records([{"ticker": "MU", "reddit": None, "wsb": None, "stocktwits": None, "sources": ["wsb"]}],
                     date(2026, 9, 1))
    repo.track_idea(*ids[0])
    days = [f"2026-09-{d:02d}" for d in range(1, 30)] + [f"2026-10-{d:02d}" for d in range(1, 10)]
    bars = [{"date": day, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.0 + i, "volume": 1}
            for i, day in enumerate(days)]
    spy = [{**bar, "close": 100.0} for bar in bars]
    it.settle_open(repo, date(2026, 10, 9), bars=lambda tickers: {"MU": bars, "SPY": spy},
                   nx_bars=lambda tickers: {})
    [row] = repo.tracked_ideas()
    assert row["entry"] == 100.0 and row["status"] == "closed" and row["return_20d_pct"] == pytest.approx(20.0)
    assert row["spy_20d_pct"] == 0.0 and row["return_5d_pct"] == pytest.approx(5.0)
    stats = it.track_record(repo, now=datetime(2026, 10, 9, 21, tzinfo=timezone.utc))
    assert stats["social"]["20d"]["count"] == 1 and stats["social"]["20d"]["vs_spy_pct"] == pytest.approx(20.0)
    assert not any(group["closed"] for group in stats["groups"].values())  # kept apart from the ideas
    assert "Most-discussed names on social media" in it.format_track_record(stats)


def test_reports_and_trade_desk_answers_get_both_references(web, repo, monkeypatch):
    from src.core.pipeline import StockAnalysisPipeline
    from src.services import youtube_picks as yp
    from src.services.trade_desk.service import TradeDeskService
    monkeypatch.setenv("SOCIAL_SCAN_ENABLED", "true")
    monkeypatch.setenv("YOUTUBE_CHANNELS", "Meet Kevin=UCUvvj5lwue7PspotMDjk5UA")
    video = {"video_id": "v1", "title": "t", "published": datetime.now(timezone.utc)}
    repo.track_idea(*yp.record("Meet Kevin", "UCUvvj5lwue7PspotMDjk5UA", video,
                               {"ticker": "MU", "stance": "bullish", "reason": "memory"}, date.today(), "captions"))
    monkeypatch.setattr(yp, "for_report", lambda ticker, db=None: yp.recent_picks(repo, ticker))
    pipeline = SimpleNamespace(db=None)
    references = StockAnalysisPipeline._reference_context(pipeline, "MU")
    assert references["social_scan"]["reddit"]["rank"] == 1 and references["youtube_picks"][0]["channel"] == "Meet Kevin"
    assert StockAnalysisPipeline._reference_context(pipeline, "600519") == {}
    result = SimpleNamespace(dashboard=None)
    StockAnalysisPipeline._attach_references(result, references)
    perspective = result.dashboard["data_perspective"]
    assert set(perspective) == {"social_scan", "youtube_picks"}
    assert yp.report_lines(perspective, "zh")[0] == f"**YouTube 博主观点（近 30 天）**: Meet Kevin 看多（{date.today():%m-%d}）"
    service = SimpleNamespace(repo=repo)
    items = TradeDeskService._references(service, SimpleNamespace(data_mode="live", ticker="MU"))
    assert [item["kind"] for item in items] == ["social_scan", "youtube_picks"]
    assert items[1]["picks"][0]["reason"] == "memory"
    assert TradeDeskService._references(service, SimpleNamespace(data_mode="replay", ticker="MU")) == []


def test_without_reddit_the_order_follows_rank_and_nothing_is_tracked(web, repo):
    web.answers["apewisdom"] = RuntimeError("down")
    hot = ss.hot_list()
    assert [row["ticker"] for row in hot][:2] == ["MU", "AAPL"]  # both on two sources; MU ranks higher
    job = ss.DigestJob(repo, lambda *a: {"id": 1}, trading_day=lambda day: True)
    job.run(date(2026, 9, 29), datetime(2026, 9, 29, 20, 25, tzinfo=timezone.utc))
    assert repo.tracked_ideas() == [] and job.last_error is None  # sent as context, not tracked
