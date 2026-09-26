"""User-supplied trade plans and free news evidence for Trade Desk."""
import threading
from datetime import date, timedelta

import pytest

from src.services.trade_desk.analytics import build_candidates, price_plan
from src.services.trade_desk.models import TradeAdviceRequest, utcnow
from src.services.trade_desk.tool_worker import effective_request
from tests.test_trade_desk_service import repo, service, snapshot, untiered_config  # noqa: F401


def _expiry(snap):
    return snap.options[0].expiry.date().isoformat()


def test_plan_text_is_parsed_and_sets_the_expiry():
    request = TradeAdviceRequest(ticker="aapl", plan_legs="buy 2 call 230 2026-10-16; sell call 240 2026-10-16")
    assert [(leg.side, leg.right, leg.quantity, leg.strike) for leg in request.plan_legs] == [
        ("buy", "call", 2, 230.0), ("sell", "call", 1, 240.0)]
    assert request.expiry == date(2026, 10, 16)
    assert request.plan_contracts() == ["call:230:2026-10-16", "call:240:2026-10-16"]
    with pytest.raises(ValueError):
        TradeAdviceRequest(ticker="AAPL", plan_legs="buy some calls")
    with pytest.raises(ValueError):
        TradeAdviceRequest(ticker="AAPL", plan_legs=[{"side": "buy", "right": "call"}])


def test_the_users_exact_contracts_are_priced_first():
    snap = snapshot()
    request = TradeAdviceRequest(ticker="TEST", plan_legs=f"buy 1 call 100 {_expiry(snap)}")
    plan, reason = price_plan(snap, request)
    assert reason == "" and plan.strategy == "custom" and plan.title == "Your plan (swing)"
    assert plan.legs[0].contract_id == "TEST_CALL" and plan.legs[0].entry_price == 2.1  # bought at the ask
    assert build_candidates(snap, request)[0].strategy == "custom"

    missing = TradeAdviceRequest(ticker="TEST", plan_legs=f"buy 1 call 105 {_expiry(snap)}")
    plan, reason = price_plan(snap, missing)
    assert plan is None and "105 call" in reason
    short_stock = TradeAdviceRequest(ticker="TEST", plan_legs=f"sell 100 stock; buy 1 call 100 {_expiry(snap)}")
    assert "Short stock" in price_plan(snap, short_stock)[1]


def test_provider_subscribes_plan_legs_by_strike_and_expiry():
    from src.services.trade_desk.providers import MoomooProvider, _Contract
    today = date(2026, 9, 24)
    far = today + timedelta(days=60)
    contracts = [_Contract(f"C{strike}", expiry, None, "call", strike, 100, "american", True, None)
                 for expiry in (today + timedelta(days=1), far) for strike in (90.0, 100.0, 110.0, 150.0)]
    selected = MoomooProvider._prioritize_contracts(
        contracts, 100.0, requested=None, max_contracts=3, max_expiries=1, current_date=today,
        required_contracts=[f"call:150:{far.isoformat()}"])
    assert (selected[0].strike, selected[0].expiry_date) == (150.0, far)


def test_codex_may_transcribe_but_not_invent_a_plan():
    request = TradeAdviceRequest(ticker="TEST", message="I want to buy the 230 call expiring 2026-10-16")
    legs = [{"side": "buy", "right": "call", "strike": 230, "expiry": "2026-10-16"}]
    assert effective_request(request, {"plan_legs": legs}).plan_legs[0].strike == 230
    with pytest.raises(ValueError):
        effective_request(request, {"plan_legs": [{**legs[0], "strike": 250}]})


def test_free_news_merges_sources_ranks_the_ticker_first_and_contains_failures(monkeypatch):
    from src.services import free_news
    now = utcnow()
    monkeypatch.setattr(free_news, "_cache", {})
    monkeypatch.setattr(free_news, "google_news", lambda query, days, limit: [
        free_news._item("Apple beats estimates", "u1", "Reuters", now - timedelta(hours=2), "", "google_news"),
        free_news._item("Old story", "u0", "CNBC", now - timedelta(days=9), "", "google_news")])
    monkeypatch.setattr(free_news, "yahoo_finance_news", lambda ticker, limit: [
        free_news._item("Stocks drift ahead of CPI", "u2", "Yahoo", now - timedelta(hours=1), "", "yahoo_finance"),
        free_news._item("Apple Beats Estimates!", "u3", "Yahoo", now, "", "yahoo_finance")])

    def broken(*args, **kwargs):
        raise RuntimeError("down")
    monkeypatch.setattr(free_news, "finnhub_news", broken)
    items = free_news.ticker_news("AAPL", ["google_news", "yahoo_finance", "finnhub"], finnhub_key="k")
    assert [item["url"] for item in items] == ["u3", "u2"]  # same headline from two feeds is kept once
    assert items[0]["related"] and not items[-1]["related"]  # market context last
    assert "u0" not in {item["url"] for item in items}  # outside the window
    assert free_news.ticker_news("AAPL", ["finnhub"]) == []  # a keyed source without a key is skipped


def test_google_news_is_a_keyless_search_provider(monkeypatch):
    from src.search_service import SearchService
    from src.services import free_news
    monkeypatch.setattr(free_news, "google_news", lambda query, days, limit: [
        free_news._item("AMD wins a contract", "https://x", "Reuters", utcnow(), "", "google_news")])
    assert not SearchService()._providers
    service_ = SearchService(free_news_sources=["google_news"])
    response = service_._providers[0].search("AMD stock", max_results=5, days=3)
    assert response.success and response.results[0].source == "Reuters"


def test_user_requests_get_fresh_news_and_an_unpriced_plan_is_explained(repo, monkeypatch, untiered_config):
    from src.services.trade_desk import analytics
    snap = snapshot()
    monkeypatch.setattr(analytics, "build_candidates", lambda s, r: [])
    untiered_config.free_news_sources = ["google_news"]
    untiered_config.finnhub_api_key = None
    fetched = []
    monkeypatch.setattr("src.services.free_news.ticker_news", lambda ticker, sources, **kw: fetched.append(ticker) or [
        {"title": "TEST guides higher", "url": "u", "source": "Reuters", "published_at": utcnow().isoformat(),
         "summary": "", "feed": "google_news", "related": True}])
    svc = service(repo, snap)
    manual = repo.create_advice(TradeAdviceRequest(
        ticker="TEST", plan_legs=f"buy 1 call 105 {_expiry(snap)}").model_dump(mode="json"))
    svc._run_advice(manual["id"], threading.Event())
    stored = repo.advice(manual["id"])
    assert any(item.get("title") == "TEST guides higher" for item in stored["snapshot"]["evidence"])
    assert "105 call" in stored["plan_error"]
    assert fetched == ["TEST"]
    svc.stop()


def test_yfinance_timezone_cache_stays_in_memory():
    from yfinance import cache
    from src.utils.yfinance_cache import use_memory_tz_cache
    original = cache._TzCacheManager._tz_cache
    try:
        assert use_memory_tz_cache()
        tz = cache.get_tz_cache()
        tz.store("AAPL", "America/New_York")
        assert tz.lookup("AAPL") == "America/New_York" and tz.tz_db is None
        tz.store("AAPL", None)
        assert tz.lookup("AAPL") is None
    finally:
        cache._TzCacheManager._tz_cache = original


def test_dropping_a_plan_keeps_the_requested_expiry():
    request = TradeAdviceRequest(ticker="TEST", message="buy the 230 call", plan_legs="buy 1 call 230 2026-10-16")
    assert effective_request(request, {"plan_legs": []}).expiry == date(2026, 10, 16)


def test_watchlist_groups_endpoint_caches_and_degrades(monkeypatch):
    from types import SimpleNamespace
    from api.v1.endpoints import stocks

    calls = []

    class Provider:
        def watchlist_groups(self):
            calls.append(1)
            return [{"name": "etf", "codes": ["SPY"]}]

    service = SimpleNamespace(enabled=True, provider=lambda mode: Provider())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(trade_desk_service=service)))
    monkeypatch.setattr(stocks, "_watchlist_groups_cache", {})
    first = stocks.get_watchlist_groups(request)
    assert first.available and first.groups[0].codes == ["SPY"]
    stocks.get_watchlist_groups(request)
    assert len(calls) == 1  # OpenD allows 10 watchlist requests per 30 s; results are cached

    monkeypatch.setattr(stocks, "_watchlist_groups_cache", {})

    def broken(mode):
        raise RuntimeError("OpenD offline")
    request.app.state.trade_desk_service = SimpleNamespace(enabled=True, provider=broken)
    result = stocks.get_watchlist_groups(request)
    assert not result.available and result.groups == [] and "offline" in result.message


def test_watchlist_quotes_use_one_snapshot_and_show_extended_hours_only_in_session(monkeypatch):
    from src.services.trade_desk.providers import MoomooProvider
    import data_provider.us_session as us_session

    provider = MoomooProvider.__new__(MoomooProvider)
    calls = []
    rows = [{"code": "US.AAPL", "last_price": 110.0, "prev_close_price": 100.0, "update_time": "2026-09-24 16:05:00",
             "pre_price": 101.0, "pre_change_rate": 1.0, "after_price": 111.0, "after_change_rate": 0.9},
            {"code": "US.DEAD", "last_price": 0, "prev_close_price": 5.0}]
    provider._call = lambda name, codes: calls.append((name, codes)) or rows
    monkeypatch.setattr(us_session, "session_window", lambda now=None: ("postmarket", None, None))
    quotes = provider.watchlist_quotes(["aapl", "DEAD"])
    assert calls == [("get_market_snapshot", ["US.AAPL", "US.DEAD"])]
    assert set(quotes) == {"AAPL"}  # no usable price, no quote
    assert round(quotes["AAPL"]["change_pct"], 6) == 10.0
    assert quotes["AAPL"]["extended"] == {"price": 111.0, "change_pct": 0.9}
    monkeypatch.setattr(us_session, "session_window", lambda now=None: ("regular", None, None))
    assert provider.watchlist_quotes(["AAPL"])["AAPL"]["extended"] is None


def test_watchlist_quotes_endpoint_quotes_only_us_tickers(monkeypatch):
    from types import SimpleNamespace
    from api.v1.endpoints import stocks

    asked = []

    class Provider:
        def watchlist_quotes(self, codes):
            asked.append(list(codes))
            return {"AAPL": {"price": 1.0, "session": "regular"}}

    service = SimpleNamespace(enabled=True, provider=lambda mode: Provider())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(trade_desk_service=service)))
    monkeypatch.setattr(stocks, "_watchlist_quotes_cache", {})
    monkeypatch.setattr(stocks, "_read_watchlist_codes", lambda svc: ["AAPL", "600519", "hk00700", "BRK.B"])
    result = stocks.get_watchlist_quotes(request, service=None)
    assert result.available and result.quotes["AAPL"].price == 1.0
    assert asked == [["AAPL", "BRK.B"]]


def test_google_news_retries_long_keyword_queries_with_fewer_terms(monkeypatch):
    from src.search_service import GoogleNewsSearchProvider
    from src.services import free_news
    asked = []

    def search(query, days, limit):
        asked.append(query)
        return [free_news._item("Broadcom faces lawsuit", "u", "Reuters", utcnow(), "", "google_news")] \
            if query == "Broadcom Inc. risk" else []
    monkeypatch.setattr(free_news, "google_news", search)
    response = GoogleNewsSearchProvider().search("Broadcom Inc. risk insider selling lawsuit litigation", 5, 3)
    assert response.success and asked == ["Broadcom Inc. risk insider selling lawsuit litigation",
                                          "Broadcom Inc. risk insider selling", "Broadcom Inc. risk"]


def test_short_tickers_need_a_marked_mention_and_outages_are_not_cached(monkeypatch):
    from src.services import free_news
    free_news._cache.clear()
    items = [{"title": "A look at the market today", "url": "1", "source": "X", "published_at": "2026-09-25T12:00:00+00:00",
              "summary": "", "feed": "google_news"},
             {"title": "Agilent (A) beats estimates", "url": "2", "source": "Y", "published_at": "2026-09-25T11:00:00+00:00",
              "summary": "", "feed": "google_news"}]
    monkeypatch.setattr(free_news, "google_news", lambda *a, **k: [dict(item) for item in items])
    result = free_news.ticker_news("A", ["google_news"])
    assert {item["url"]: item["related"] for item in result} == {"1": False, "2": True}
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise RuntimeError("429")
    monkeypatch.setattr(free_news, "google_news", down)
    free_news.ticker_news("ZZZZ", ["google_news"])
    free_news.ticker_news("ZZZZ", ["google_news"])
    assert len(calls) == 2  # not cached


def test_a_remembered_company_name_makes_short_ticker_news_relevant(monkeypatch):
    from src.services import free_news
    free_news._cache.clear()
    free_news.remember_name("F", "Ford Motor Company")
    items = [{"title": "Ford recalls 100,000 trucks", "url": "1", "source": "X", "published_at": "2026-09-25T12:00:00+00:00",
              "summary": "", "feed": "google_news"},
             {"title": "Update: A market wrap", "url": "2", "source": "Y", "published_at": "2026-09-25T11:00:00+00:00",
              "summary": "", "feed": "google_news"}]
    monkeypatch.setattr(free_news, "google_news", lambda *a, **k: [dict(item) for item in items])
    result = {item["url"]: item["related"] for item in free_news.ticker_news("F", ["google_news"])}
    assert result == {"1": True, "2": False}
