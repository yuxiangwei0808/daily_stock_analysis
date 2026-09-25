"""US session data contracts, covering provider failures without live network calls."""
from datetime import datetime, date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from data_provider.realtime_types import UnifiedRealtimeQuote
from data_provider.us_session import NEW_YORK, apply_us_quote_metadata, session_window
from src.services.screening.snapshot_us import fetch_us_snapshot


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=NEW_YORK)


@pytest.mark.parametrize("value,session", [
    ("2026-09-21T08:00", "premarket"), ("2026-09-21T10:00", "regular"),
    ("2026-09-21T17:00", "postmarket"), ("2026-09-21T21:00", "closed"),
    ("2026-09-20T10:00", "closed"), ("2026-12-25T10:00", "closed"),
    ("2026-11-27T13:10", "postmarket"),
])
def test_session_calendar(value, session):
    assert session_window(at(value))[0] == session


def info():
    return {"regularMarketPrice": 100, "regularMarketPreviousClose": 98,
            "regularMarketTime": at("2026-09-18T16:00").timestamp(),
            "preMarketPrice": 105, "preMarketTime": at("2026-09-21T08:59").timestamp()}


def test_premarket_price_uses_matching_timestamp_and_reference():
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL", price=100, volume=900), info(), at("2026-09-21T09:00"))
    assert quote.price == 105
    assert quote.change_pct == 5
    assert quote.quote_session == "premarket"
    assert quote.volume is None
    assert quote.is_stale is False
    assert datetime.fromisoformat(quote.provider_timestamp).astimezone(NEW_YORK).hour == 8
    assert quote.to_dict()["quote_session"] == "premarket"


def test_missing_premarket_quote_is_not_fresh_friday_quote():
    payload = info()
    payload.pop("preMarketTime")
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL", price=100), payload, at("2026-09-21T09:00"))
    assert quote.price == 100
    assert quote.is_stale is True
    assert "fresh_session_quote" in quote.missing_fields


def test_afterhours_change_uses_todays_regular_close():
    payload = {**info(), "regularMarketTime": at("2026-09-21T16:00").timestamp(),
               "postMarketPrice": 97, "postMarketTime": at("2026-09-21T17:59").timestamp()}
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), payload, at("2026-09-21T18:00"))
    assert quote.change_pct == -3
    assert quote.quote_session == "postmarket"
    assert not quote.is_stale


@pytest.mark.parametrize("session,now,prefix", [
    ("premarket", "2026-09-21T09:00", "preMarket"),
    ("regular", "2026-09-21T10:00", "regularMarket"),
    ("postmarket", "2026-09-21T18:00", "postMarket"),
])
@pytest.mark.parametrize("missing_timestamp", [False, True])
def test_share_class_realtime_quote_keeps_us_session_contract(monkeypatch, session, now, prefix, missing_timestamp):
    from data_provider.base import DataFetcherManager
    from data_provider.yfinance_fetcher import YfinanceFetcher

    current = at(now)
    stamp = current - pd.Timedelta(minutes=1)
    payload = info()
    if session == "postmarket":
        payload["regularMarketTime"] = at("2026-09-21T16:00").timestamp()
    payload.update({prefix + "Price": 105, prefix + "Time": stamp.timestamp()})
    if missing_timestamp:
        payload.pop(prefix + "Time")
    ticker = SimpleNamespace(fast_info=SimpleNamespace(lastPrice=100, previousClose=98), info=payload)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr("data_provider.us_session.datetime", FrozenDatetime)
    manager = DataFetcherManager.__new__(DataFetcherManager)
    with (
        patch("yfinance.Ticker", return_value=ticker) as factory,
        patch.object(manager, "_utc_now_iso", return_value=current.isoformat()),
    ):
        quote = manager._enrich_realtime_quote(YfinanceFetcher().get_realtime_quote("BRK.B"))
    factory.assert_called_once_with("BRK-B")
    assert quote.market == "us"
    assert quote.code == "BRK.B"
    assert quote.is_stale is missing_timestamp
    if missing_timestamp:
        assert "fresh_session_quote" in quote.missing_fields
    else:
        assert quote.price == 105
        assert quote.quote_session == session
        assert datetime.fromisoformat(quote.provider_timestamp) == stamp
        reference = 98 if session == "regular" else 100
        assert quote.change_pct == pytest.approx((105 / reference - 1) * 100)


def test_share_class_empty_yahoo_history_reaches_stooq_with_canonical_code():
    from data_provider.yfinance_fetcher import YfinanceFetcher

    fetcher = YfinanceFetcher()
    ticker = SimpleNamespace(fast_info=None, history=lambda **kwargs: pd.DataFrame())
    fallback = UnifiedRealtimeQuote(code="BRK.B", market="us", price=100, is_stale=True)
    with (
        patch("yfinance.Ticker", return_value=ticker),
        patch.object(fetcher, "_get_us_stock_quote_from_stooq", return_value=fallback) as stooq,
    ):
        quote = fetcher.get_realtime_quote("BRK.B")
    stooq.assert_called_once_with("BRK.B")
    assert quote is fallback


def bars(index, prices, volumes=None):
    return pd.DataFrame({"Close": prices, "Volume": volumes or [100000] * len(prices)}, index=pd.DatetimeIndex(index))


@pytest.mark.parametrize("now,session,reference", [
    ("2026-09-21T09:00", "premarket", 100),
    ("2026-09-21T10:00", "regular", 100),
    ("2026-09-21T17:00", "postmarket", 110),
])
def test_snapshot_uses_fresh_session_bars(monkeypatch, now, session, reference):
    current = at(now)
    latest = current - pd.Timedelta(minutes=5)
    earlier = at("2026-09-21T15:55") if session == "postmarket" else at("2026-09-18T15:55")
    intraday = bars([earlier, latest], [110, 120])
    daily = bars([datetime(2026, 9, 18)], [100])
    requests = []
    def download(*args, **kwargs):
        requests.append(kwargs)
        return intraday if kwargs["interval"] == "5m" else daily
    monkeypatch.setattr("yfinance.download", download)
    frame = fetch_us_snapshot(["AAPL"], now=current)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row.quote_session == session
    assert row.change_pct == pytest.approx((120 / reference - 1) * 100, abs=0.0001)
    assert row.volume == 100000
    assert row.volume_ratio is None
    assert requests[0]["prepost"] is True
    assert frame.attrs["available_count"] == 1


def test_snapshot_excludes_stale_bars_and_exposes_coverage(monkeypatch):
    old = bars([at("2026-09-18T15:55")], [100])
    monkeypatch.setattr("yfinance.download", lambda *a, **k: old)
    frame = fetch_us_snapshot(["AAPL"], now=at("2026-09-21T09:00"))
    assert frame.empty
    assert frame.attrs["excluded_count"] == 1
    assert "0/1" in frame.attrs["source_errors"][0]


def test_closed_session_does_not_download(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("No download is allowed outside a supported session")
    monkeypatch.setattr("yfinance.download", unexpected)
    assert fetch_us_snapshot(["AAPL"], now=at("2026-09-20T10:00")).empty


@pytest.mark.parametrize("session,stamp,stale,append", [
    ("regular", "2026-09-21T09:59", False, True),
    ("regular", "2026-09-18T15:59", False, False),
    ("premarket", "2026-09-21T09:29", False, False),
    ("regular", "2026-09-21T09:59", True, False),
    ("regular", None, False, False),
])
def test_pipeline_never_creates_daily_bar_from_stale_or_extended_quote(session, stamp, stale, append):
    from src.core.pipeline import StockAnalysisPipeline
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(enable_realtime_technical_indicators=True)
    history = pd.DataFrame([{"date": date(2026, 9, 18), "close": 100, "volume": 1}])
    quote = SimpleNamespace(price=105, quote_session=session, is_stale=stale,
                            provider_timestamp=at(stamp).isoformat() if stamp else None)
    with patch("src.core.pipeline.get_market_now", return_value=at("2026-09-21T10:00")):
        result = pipeline._augment_historical_with_realtime(history, quote, "AAPL", market="us")
    assert len(result) == (2 if append else 1)
    assert len(history) == 1


def test_manager_preserves_provider_stale_flag():
    from data_provider.base import DataFetcherManager
    manager = DataFetcherManager.__new__(DataFetcherManager)
    quote = UnifiedRealtimeQuote(code="AAPL", is_stale=True, provider_timestamp=at("2026-09-21T09:00").isoformat())
    with patch.object(manager, "_utc_now_iso", return_value=at("2026-09-21T09:00").isoformat()):
        manager._enrich_realtime_quote(quote)
    assert quote.is_stale is True


@pytest.mark.parametrize("session", ["premarket", "postmarket", "regular", None])
def test_quote_supplement_does_not_fill_extended_hours_with_regular_metrics(session):
    from data_provider.base import DataFetcherManager

    primary = UnifiedRealtimeQuote(code="AAPL", market="us", price=105, quote_session=session, is_stale=False)
    secondary = UnifiedRealtimeQuote(code="AAPL", price=100, quote_session="regular", is_stale=True,
                                     volume_ratio=2, turnover_rate=3, amplitude=4, pe_ratio=18)
    manager = DataFetcherManager.__new__(DataFetcherManager)
    with patch.object(manager, "_try_fetcher_quote", return_value=secondary):
        result = manager._supplement_quote("AAPL", primary, "LongbridgeFetcher")
    assert result is primary
    assert result.price == 105
    assert result.quote_session == session
    assert result.is_stale is False
    assert result.pe_ratio == 18
    expected = (None, None, None) if session in {"premarket", "postmarket"} else (2, 3, 4)
    assert (result.volume_ratio, result.turnover_rate, result.amplitude) == expected


@pytest.mark.parametrize("now", ["2026-09-21T09:00", "2026-09-21T10:00", "2026-09-21T17:00"])
@pytest.mark.parametrize("stale_data", [False, True])
def test_market_scan_preserves_coverage_when_all_quotes_are_missing_or_stale(monkeypatch, now, stale_data):
    from src.services import us_market_scan as scan

    provider_rows = bars([at("2026-09-18T15:55")], [100]) if stale_data else pd.DataFrame()
    monkeypatch.setattr("yfinance.download", lambda *args, **kwargs: provider_rows)
    monkeypatch.setattr(scan, "fetch_us_universe", lambda source: ["AAPL", "MSFT"])
    monkeypatch.setattr(scan, "SECTOR_ETFS", {})
    result = scan.collect_us_market_scan(now=at(now))
    assert result["available"] is False
    assert result["requested_count"] == 2
    assert result["available_count"] == 0
    assert result["excluded_count"] == 2
    assert result["gainers"] == result["losers"] == result["unverified_movers"] == []
    assert result["as_of"] == at(now).isoformat()
    assert "0/2 fresh symbols" in scan.render_us_market_scan(result)
    assert not any("TypeError" in warning for warning in result["warnings"])


def test_market_scan_excludes_watchlist_and_labels_sampled_breadth(monkeypatch):
    from src.services import us_market_scan as scan
    rows = []
    for code, change in [("AAPL", 5), ("AMD", 4), ("TSLA", -3), ("XLK", 2)]:
        rows.append({"code": code, "price": 100, "change_pct": change, "amount": 2_000_000,
                     "volume": 20000, "provider_timestamp": "2026-09-21T09:55:00-04:00"})
    frame = pd.DataFrame(rows)
    frame.attrs.update(as_of="2026-09-21T10:00:00-04:00", coverage_note="Configured universe only")
    monkeypatch.setattr(scan, "fetch_us_universe", lambda source: ["AAPL", "AMD", "TSLA", "NVDA"])
    monkeypatch.setattr(scan, "fetch_us_snapshot", lambda *a, **k: frame)
    result = scan.collect_us_market_scan(["AAPL"], now=at("2026-09-21T10:00"))
    assert result["available_count"] == 3
    assert result["excluded_count"] == 1
    assert result["advancers"] == 2
    assert [r["code"] for r in result["gainers"]] == ["AMD"]
    assert [r["code"] for r in result["losers"]] == ["TSLA"]
    assert "not all US listings" in scan.render_us_market_scan(result)


def test_us_strategies_reach_real_filters_and_ranking(monkeypatch):
    from src.services.screening import pipeline
    from src.services.screening.config import Config
    frame = pd.DataFrame([{"code": "AMD", "name": "AMD", "price": 100, "change_pct": 4,
                           "amount": 2_000_000, "industry": "", "pe_ratio": None}])
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: frame)
    config = Config(daily_enrich_enabled=False, post_analyzers=[], risk_enabled=False,
                    portfolio_diversity_enabled=False)
    result = pipeline.screen("us_gainers", market="us", use_llm=False, config=config)
    assert [p.code for p in result.picks] == ["AMD"]
    result = pipeline.screen("us_decliners", market="us", use_llm=False, config=config)
    assert result.picks == []


def test_market_report_keeps_scan_when_llm_uses_fallback():
    from src.market_analyzer import MarketAnalyzer, MarketOverview
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    overview = MarketOverview(date="2026-09-21", us_session_scan={
        "available": False, "warnings": ["No current session data"]})
    with patch.object(analyzer, "_generate_market_review", return_value="# Market report"), \
            patch.object(analyzer, "_get_review_language", return_value="en"):
        report = analyzer.generate_market_review(overview, [])
    assert "US session scan" in report
    assert "No current session data" in report


def test_us_overview_does_not_call_cn_sector_providers():
    from src.market_analyzer import MarketAnalyzer
    from src.core.market_profile import US_PROFILE
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer.region = "us"
    analyzer.profile = US_PROFILE
    analyzer.config = SimpleNamespace(screening_enabled=True, stock_list=["AAPL"])
    scan = {"available": True, "sectors": [{"name": "Technology (XLK ETF)", "change_pct": 2}]}
    with patch.object(analyzer, "_get_main_indices", return_value=[]), \
            patch.object(analyzer, "_get_sector_rankings") as cn_sectors, \
            patch("src.services.us_market_scan.collect_us_market_scan", return_value=scan) as collect:
        overview = analyzer.get_market_overview()
    cn_sectors.assert_not_called()
    collect.assert_called_once_with(["AAPL"])
    assert overview.top_sectors[0]["name"] == "Technology (XLK ETF)"
    assert overview.us_session_scan is scan


def test_quote_service_and_api_preserve_provider_freshness():
    from src.services.stock_service import StockService
    from api.v1.schemas.stocks import StockQuote
    service = StockService.__new__(StockService)
    quote = UnifiedRealtimeQuote(code="AAPL", price=100, provider_timestamp="2026-09-18T20:00:00Z",
                                 quote_session="regular", is_stale=True, fetched_at="2026-09-21T13:00:00Z")
    with patch("data_provider.base.DataFetcherManager") as manager:
        manager.return_value.get_realtime_quote.return_value = quote
        result = service.get_realtime_quote("AAPL")
    payload = StockQuote(**result).model_dump()
    assert payload["update_time"] == "2026-09-18T20:00:00Z"
    assert payload["fetched_at"] == "2026-09-21T13:00:00Z"
    assert payload["is_stale"] is True
    assert payload["quote_session"] == "regular"


def test_profile_marks_stale_quote_partial():
    from src.services.stock_profile_service import StockProfileService
    service = StockProfileService.__new__(StockProfileService)
    with patch.object(service, "_stock_service") as stock_service:
        stock_service.return_value.get_realtime_quote.return_value = {"stock_code": "AAPL", "is_stale": True}
        result = service._quote_block("AAPL")
    assert result["status"] == "partial"
    assert "quote_stale" in result["limitations"]


def test_snapshot_rejects_a_stale_reference_close(monkeypatch):
    intraday = bars([at("2026-09-21T08:55")], [120])
    stale_daily = bars([datetime(2026, 9, 17)], [100])
    monkeypatch.setattr("yfinance.download", lambda *a, **k: intraday if k["interval"] == "5m" else stale_daily)
    assert fetch_us_snapshot(["AAPL"], now=at("2026-09-21T09:00")).empty


def test_explicit_universe_is_canonical_and_unique(monkeypatch):
    from src.services.screening.snapshot_us import fetch_us_universe
    monkeypatch.setenv("SCREENING_US_TICKERS", "aapl,BRK.B,AAPL")
    assert fetch_us_universe("env") == ["AAPL", "BRK-B"]


@pytest.mark.parametrize("session,stamp,stale,now,overlay", [
    ("regular", "2026-09-18T15:59", True, "2026-09-21T09:00", False),
    ("regular", "2026-09-18T15:59", False, "2026-09-21T10:00", False),
    ("regular", None, False, "2026-09-21T10:00", False),
    ("premarket", "2026-09-21T08:59", False, "2026-09-21T09:00", False),
    ("postmarket", "2026-09-21T17:59", False, "2026-09-21T18:00", False),
    ("regular", "2026-09-21T09:59", False, "2026-09-21T10:00", True),
])
def test_report_context_respects_daily_quote_guard(session, stamp, stale, now, overlay):
    from src.core.pipeline import StockAnalysisPipeline
    from src.stock_analyzer import TrendAnalysisResult
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(report_language="en")
    pipeline.search_service = None
    context = {"code": "AAPL", "date": "2026-09-18",
               "today": {"date": "2026-09-18", "close": 100, "open": 99},
               "yesterday": {"close": 98}}
    quote = UnifiedRealtimeQuote(code="AAPL", price=105, quote_session=session,
                                 provider_timestamp=at(stamp).isoformat() if stamp else None,
                                 is_stale=stale, pre_close=100, change_pct=5)
    trend = TrendAnalysisResult(code="AAPL", ma5=100, ma10=99, ma20=98)
    with patch("src.core.pipeline.get_market_now", return_value=at(now)):
        result = pipeline._enhance_context(context, quote, None, trend, fundamental_context={})
    if overlay:
        assert result["today"]["close"] == 105
        assert result["today"]["date"] == "2026-09-21"
    else:
        assert result["today"] == context["today"]
        assert result["date"] == context["date"]
    assert result["realtime"]["quote_session"] == session
    assert result["realtime"]["pre_close"] == 100
    assert context["today"]["close"] == 100


@pytest.mark.parametrize("session,now,reference_stamp", [
    ("preMarket", "2026-09-21T09:00", "2026-09-17T16:00"),
    ("postMarket", "2026-09-21T18:00", "2026-09-18T16:00"),
    ("postMarket", "2026-09-21T18:00", "2026-09-21T10:00"),
    ("postMarket", "2026-09-21T18:00", None),
])
def test_extended_quote_omits_unverified_reference_change(session, now, reference_stamp):
    current = at(now)
    payload = {"regularMarketPrice": 100,
               "regularMarketTime": at(reference_stamp).timestamp() if reference_stamp else None,
               session + "Price": 105, session + "Time": current.timestamp() - 60}
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), payload, current)
    assert quote.price == 105
    assert quote.is_stale is False  # Fresh price remains usable for price alerts.
    assert quote.pre_close is None
    assert quote.change_pct is None
    assert quote.change_amount is None
    assert quote.data_quality == "partial"
    assert "regular_session_reference_close" in quote.missing_fields


def test_screening_preserves_session_metadata_through_ranking_and_api(monkeypatch):
    from src.services.screening import pipeline
    from src.services.screening.config import Config
    from src.config import Config as AppConfig
    from src.services.screening_service import ScreeningService
    from src.storage import DatabaseManager
    metadata = {"quote_session": "postmarket", "provider_timestamp": "2026-09-21T17:55:00-04:00",
                "reference_price": 100, "is_stale": False}
    frame = pd.DataFrame([{"code": "AMD", "name": "AMD", "price": 104, "change_pct": 4,
                           "amount": 2_000_000, "industry": "", **metadata}])
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: frame)
    config = Config(daily_enrich_enabled=False, post_analyzers=[], risk_enabled=False,
                    portfolio_diversity_enabled=False)
    result = pipeline.screen("us_gainers", market="us", use_llm=False, config=config)
    db = DatabaseManager(db_url="sqlite:///:memory:")
    service = ScreeningService(AppConfig(screening_enabled=True), db_manager=db)
    with patch("src.services.screening_service._call_screening_screen", return_value=result), \
            patch("src.services.screening_service._get_screening_status_snapshot", return_value=({}, True, None)), \
            patch("src.services.screening_service._enrich_candidates_with_dsa", side_effect=lambda rows: (rows, {})):
        response = service.screen(strategy="us_gainers", market="us", max_results=3)
    candidate = response["candidates"][0]
    saved = db.get_screening_run(response["run_id"])["result"]["candidates"][0]
    assert candidate["change_pct"] == 4
    for key, value in metadata.items():
        assert candidate[key] == value
        assert candidate["raw"][key] == value
        assert saved[key] == value


@pytest.mark.parametrize("now,reference_stamp,session", [
    ("2026-11-27T14:00", "2026-11-27T13:00", "postMarket"),
    ("2026-11-30T09:00", "2026-11-27T13:00", "preMarket"),
    ("2026-09-08T09:00", "2026-09-04T16:00", "preMarket"),
])
def test_reference_close_accepts_early_closes_and_holiday_weekends(now, reference_stamp, session):
    current = at(now)
    payload = {"regularMarketPrice": 100, "regularMarketTime": at(reference_stamp).timestamp(),
               session + "Price": 105, session + "Time": current.timestamp() - 60}
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), payload, current)
    assert quote.change_pct == 5
    assert quote.pre_close == 100
    assert not quote.is_stale


@pytest.mark.parametrize("stale", [True, False])
def test_report_prompt_labels_us_quote_time_session_and_freshness(stale):
    from src.analyzer import GeminiAnalyzer
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    context = {"code": "AAPL", "date": "2026-09-18", "today": {"close": 100},
               "realtime": {"price": 105, "quote_session": "premarket", "change_pct": 5,
                            "pre_close": 100, "provider_timestamp": "2026-09-21T08:59:00-04:00",
                            "is_stale": stale}}
    prompt = analyzer._format_prompt(context, "Apple", report_language="en")
    assert "| Session | premarket |" in prompt
    assert "2026-09-21T08:59:00-04:00" in prompt
    assert "| Reference close | 100 |" in prompt
    assert "| Session move | 5% |" in prompt
    if stale:
        assert "Stale/unverified; historical reference only" in prompt
        assert "| 当前价格 | 105" not in prompt
    else:
        assert "| 当前价格 | 105" in prompt


@pytest.mark.parametrize("price", [None, 0, -1, float("nan")])
def test_provider_timestamp_cannot_freshen_a_retained_price(price):
    from data_provider.us_session import is_fresh_regular_quote
    now = at("2026-09-21T10:00")
    quote = apply_us_quote_metadata(
        UnifiedRealtimeQuote(code="AAPL", price=100),
        {"regularMarketPrice": price, "regularMarketTime": now.timestamp() - 60,
         "regularMarketPreviousClose": 98}, now,
    )
    assert quote.price == 100  # Retain the reference price without inventing its age or move.
    assert quote.is_stale is True
    assert quote.provider_timestamp is None
    assert quote.quote_session == "unknown"
    assert quote.change_pct is None
    assert "fresh_session_quote" in quote.missing_fields
    assert not is_fresh_regular_quote(quote, now)


def _us_review_analyzer(language="en"):
    from src.config import Config
    from src.core.market_profile import US_PROFILE
    from src.core.market_strategy import get_market_strategy_blueprint
    from src.market_analyzer import MarketAnalyzer
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer.config = Config(report_language=language, screening_enabled=False)
    analyzer.region = "us"
    analyzer.profile = US_PROFILE
    analyzer.strategy = get_market_strategy_blueprint("us")
    analyzer.analyzer = None
    return analyzer


@pytest.mark.parametrize("language,label", [("en", "regular-session daily bar"), ("zh", "常规时段日线")])
def test_previous_index_session_survives_provider_prompt_and_saved_report(language, label):
    from data_provider.yfinance_fetcher import YfinanceFetcher
    hist = pd.DataFrame({
        "Close": [100, 105], "Open": [100, 100], "High": [101, 106],
        "Low": [99, 100], "Volume": [1000, 1200],
    }, index=pd.DatetimeIndex([at("2026-09-17T00:00"), at("2026-09-18T00:00")]))
    yf = SimpleNamespace(Ticker=lambda code: SimpleNamespace(history=lambda **kwargs: hist))
    row = YfinanceFetcher.__new__(YfinanceFetcher)._fetch_yf_ticker_data(yf, "^GSPC", "S&P 500", "SPX")
    analyzer = _us_review_analyzer(language)
    analyzer.data_manager = SimpleNamespace(get_main_indices=lambda **kwargs: [row])
    overview = analyzer.get_market_overview()
    overview.date = "2026-09-21"
    assert overview.indices[0].daily_bar_date == "2026-09-18"
    expected = f"{label}: 2026-09-18"
    assert expected in analyzer._build_review_prompt(overview, [])
    assert expected in analyzer._build_indices_block(overview)
    report = analyzer.generate_market_review(overview, [])
    assert expected in report
    assert "Today's US" not in report
    assert "今日美股市场整体" not in report
    payload = analyzer.build_market_review_payload(overview, [], report)
    assert payload["date"] == "2026-09-21"
    assert payload["indices"][0]["daily_bar_date"] == "2026-09-18"
    assert expected in payload["markdown_report"]


def _available_us_scan():
    return {"available": True, "session": "premarket", "as_of": "2026-09-21T08:45:00-04:00",
            "available_count": 1, "requested_count": 1, "excluded_count": 0,
            "sector_available_count": 0, "advancers": 1, "decliners": 0, "unchanged": 0,
            "gainers": [{"code": "AMD", "price": 105, "change_pct": 5, "amount": 2000000,
                         "provider_timestamp": "2026-09-21T08:40:00-04:00"}],
            "losers": [], "sectors": [], "warnings": []}


@pytest.mark.parametrize("code", ["timeout", "non_zero_exit", "empty_output"])
def test_operational_generation_failure_retains_scan_in_report_and_payload(code):
    from src.llm.generation_backend import GenerationError, GenerationErrorCode
    from src.market_analyzer import MarketOverview
    analyzer = _us_review_analyzer()
    error = GenerationError(GenerationErrorCode(code), "execution", True, True, "codex_cli")
    def generate(*args, **kwargs):
        raise error
    analyzer.analyzer = SimpleNamespace(
        is_available=lambda: True,
        generate_text_with_metadata=generate,
        get_generation_backend_config_error=lambda: None,
        get_generation_backend_identity=lambda: ("codex_cli", "codex_cli"),
    )
    overview = MarketOverview(date="2026-09-21", us_session_scan=_available_us_scan())
    with patch.object(analyzer, "get_market_overview", return_value=overview), \
            patch.object(analyzer, "search_market_news", return_value=[]), \
            patch.object(analyzer, "_merge_persisted_market_intelligence", return_value=[]), \
            patch("src.market_analyzer.record_llm_run") as diagnostic, \
            patch("src.market_analyzer.record_llm_run_started"):
        result = analyzer._run_daily_review_parts()
    assert "AI narrative unavailable" in result.report
    assert "US session scan" in result.report
    assert "AMD" in result.report
    assert result.structured_payload["markdown_report"] == result.report
    assert result.structured_payload["us_session_scan"]["gainers"][0]["code"] == "AMD"
    assert code in result.structured_payload["us_session_scan"]["warnings"][-1]
    assert diagnostic.call_args.kwargs["success"] is False
    assert diagnostic.call_args.kwargs["error_message"] is error


@pytest.mark.parametrize("code,stage,available", [
    ("timeout", "execution", False),
    ("command_not_found", "configuration", True),
    ("login_required", "execution", True),
    ("approval_required", "execution", True),
])
def test_scan_fallback_does_not_hide_setup_errors_or_empty_data(code, stage, available):
    from src.llm.generation_backend import GenerationError, GenerationErrorCode
    from src.market_analyzer import MarketOverview
    analyzer = _us_review_analyzer()
    scan = _available_us_scan()
    scan["available"] = available
    error = GenerationError(GenerationErrorCode(code), stage, False, True, "codex_cli")
    with patch.object(analyzer, "_generate_market_review", side_effect=error):
        with pytest.raises(GenerationError) as raised:
            analyzer.generate_market_review(MarketOverview(date="2026-09-21", us_session_scan=scan), [])
    assert raised.value is error


@pytest.mark.parametrize("now", ["2026-09-21T09:00", "2026-09-21T17:00"])
def test_extended_price_movers_survive_missing_volume_without_passing_liquidity_filter(monkeypatch, now):
    from src.services import us_market_scan as scan
    from src.services.screening import pipeline
    from src.services.screening.config import Config
    current = at(now)
    regular = at("2026-09-21T15:55") if current.hour == 17 else at("2026-09-18T15:55")
    latest = current - pd.Timedelta(minutes=5)
    quotes = {"AMD": (104, 20000), "NVDA": (104, 0), "TSLA": (96, None),
              "AAPL": (108, 0), "MSFT": (104, 10), "PENNY": (3, 0)}
    intraday = pd.concat({
        code: bars([regular, latest], [100, price], [100000, volume])
        for code, (price, volume) in quotes.items()
    }, axis=1, names=["Ticker", "Price"])
    daily = pd.concat({code: bars([datetime(2026, 9, 18)], [100]) for code in quotes},
                      axis=1, names=["Ticker", "Price"])
    monkeypatch.setattr("yfinance.download", lambda *a, **k: intraday if k["interval"] == "5m" else daily)
    monkeypatch.setattr(scan, "fetch_us_universe", lambda source: list(quotes))
    result = scan.collect_us_market_scan(["AAPL"], now=current)
    assert [row["code"] for row in result["gainers"]] == ["AMD"]
    assert result["losers"] == []
    assert {row["code"] for row in result["unverified_movers"]} == {"NVDA", "TSLA"}
    assert all(row["liquidity_status"] == "unverified" for row in result["unverified_movers"])
    assert any("volume unavailable/unverified" in warning for warning in result["warnings"])

    # The same disclosure and price-only movers reach prompts, saved reports and payloads.
    from src.market_analyzer import MarketOverview
    analyzer = _us_review_analyzer()
    overview = MarketOverview(date="2026-09-21", us_session_scan=result)
    prompt = analyzer._build_review_prompt(overview, [])
    report = analyzer.generate_market_review(overview, [])
    payload = analyzer.build_market_review_payload(overview, [], report)
    for text in (prompt, report, payload["markdown_report"]):
        assert "Price movers outside your watchlist — liquidity unverified" in text
        assert "NVDA" in text and "TSLA" in text
    assert payload["us_session_scan"]["unverified_movers"] == result["unverified_movers"]

    # Manual screening keeps its existing liquidity requirement and explains the missing data.
    frame = fetch_us_snapshot(list(quotes), now=current)
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: frame)
    config = Config(daily_enrich_enabled=False, post_analyzers=[], risk_enabled=False,
                    portfolio_diversity_enabled=False)
    screened = pipeline.screen("us_gainers", market="us", use_llm=False, config=config)
    assert [pick.code for pick in screened.picks] == ["AMD"]
    assert any("volume unavailable/unverified" in warning for warning in screened.source_errors)


@pytest.mark.parametrize("now,latest_day", [
    ("2026-09-21T09:00", "2026-09-18"),
    ("2026-09-21T15:59", "2026-09-18"),
    ("2026-09-21T16:30", "2026-09-21"),
    ("2026-09-21T21:00", "2026-09-21"),
    ("2026-11-27T12:59", "2026-11-25"),
    ("2026-11-27T13:30", "2026-11-27"),
    ("2026-11-28T10:00", "2026-11-27"),
])
def test_us_daily_history_includes_completed_close_without_partial_or_extended_bar(monkeypatch, now, latest_day):
    from data_provider.yfinance_fetcher import YfinanceFetcher
    from src.core.pipeline import StockAnalysisPipeline
    from src.services.screening.snapshot_us import fetch_daily_history_yfinance
    current = at(now)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr("data_provider.base.datetime", FrozenDatetime)
    monkeypatch.setattr("data_provider.us_session.datetime", FrozenDatetime)
    dates = ["2026-09-18", "2026-09-21", "2026-11-25", "2026-11-27"]
    raw = pd.DataFrame({"Open": [99, 100, 109, 110], "High": [101, 112, 111, 122],
                        "Low": [98, 99, 108, 109], "Close": [100, 110, 110, 120],
                        "Volume": [1000, 2000, 1000, 2000]}, index=pd.DatetimeIndex(dates))

    def download(*args, **kwargs):
        # Emulate Yahoo's documented exclusive end at the network boundary.
        return raw[(raw.index >= pd.Timestamp(kwargs["start"])) &
                   (raw.index < pd.Timestamp(kwargs["end"]))].copy()

    monkeypatch.setattr("yfinance.download", download)
    daily = YfinanceFetcher().get_daily_data("AAPL", start_date="2026-09-01")
    assert daily.date.max().date().isoformat() == latest_day
    enrichment = fetch_daily_history_yfinance("AAPL")
    assert enrichment["日期"].max().date().isoformat() == latest_day

    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(enable_realtime_technical_indicators=True)
    quote = UnifiedRealtimeQuote(code="AAPL", price=999, quote_session="postmarket",
                                 provider_timestamp=current.isoformat(), is_stale=False)
    monkeypatch.setattr("src.core.pipeline.get_market_now", lambda market: current)
    augmented = pipeline._augment_historical_with_realtime(daily, quote, "AAPL", market="us")
    pd.testing.assert_frame_equal(augmented, daily)


@pytest.mark.parametrize("code,now,end_date,calendar_available,expected_end", [
    ("AAPL", "2026-09-21T16:30", "2026-09-21", False, "2026-09-21"),
    ("AAPL", "2026-09-21T10:00", "2026-09-22", True, "2026-09-21"),
    ("AAPL", "2026-09-21T21:00", "2026-09-22", True, "2026-09-22"),
    ("AAPL", "2026-09-21T16:30", "2026-09-18", True, "2026-09-18"),
    ("SPX", "2026-09-21T16:30", "2026-09-21", True, "2026-09-22"),
    ("600519", "2026-09-21T16:30", "2026-09-21", True, "2026-09-21"),
])
def test_daily_request_preserves_historical_bounds_and_requires_verified_us_close(
    monkeypatch, code, now, end_date, calendar_available, expected_end,
):
    from data_provider.yfinance_fetcher import YfinanceFetcher
    current = at(now)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr("data_provider.us_session.datetime", FrozenDatetime)
    if not calendar_available:
        monkeypatch.setattr("data_provider.us_session.get_market_session_bounds", lambda *a: (None, None))
    with patch("yfinance.download", return_value=bars([datetime(2026, 9, 17)], [100])) as download:
        YfinanceFetcher()._fetch_raw_data(code, "2026-09-01", end_date)
    assert download.call_args.kwargs["end"] == expected_end


@pytest.mark.parametrize("code", ["AAPL", "SPX"])
@pytest.mark.parametrize("stamp,session", [(None, "regular"), ("2026-09-21T13:00:00Z", None)])
def test_unverified_us_provider_quote_cannot_trigger_either_alert_path(code, stamp, session):
    import asyncio
    from unittest.mock import AsyncMock
    from data_provider.base import DataFetcherManager
    from src.agent.events import EventMonitor, PriceAlert, PriceChangeAlert
    from src.services.alert_service import AlertService

    manager = DataFetcherManager.__new__(DataFetcherManager)
    quote = UnifiedRealtimeQuote(code=code, price=105, change_pct=5,
                                 provider_timestamp=stamp, quote_session=session)
    with patch.object(manager, "_utc_now_iso", return_value="2026-09-21T13:00:00Z"):
        quote = manager._enrich_realtime_quote(quote)
    assert quote.is_stale is True
    assert quote.data_quality == "partial"
    monitor = EventMonitor()
    monitor._get_realtime_quote = AsyncMock(return_value=quote)
    price = PriceAlert(stock_code=code, direction="above", price=100)
    change = PriceChangeAlert(stock_code=code, direction="up", change_pct=2)
    assert asyncio.run(monitor._check_price(price)) is None
    assert asyncio.run(monitor._check_price_change(change)) is None
    service = AlertService.__new__(AlertService)
    for method, rule in [(service._evaluate_price, price), (service._evaluate_price_change, change)]:
        result = asyncio.run(method(rule, monitor))
        assert result["record_status"] == "skipped"
        assert result["triggered"] is False


def test_unknown_us_quote_is_not_a_fresh_stock_profile():
    from src.services.stock_profile_service import StockProfileService
    service = StockProfileService.__new__(StockProfileService)
    with patch.object(service, "_stock_service") as stock_service:
        stock_service.return_value.get_realtime_quote.return_value = {"stock_code": "AAPL", "current_price": 105}
        result = service._quote_block("AAPL")
    assert result["status"] == "partial"
    assert result["limitations"] == ["quote_unverified"]


def test_discovered_share_class_can_be_analyzed_and_excluded_from_discovery(monkeypatch):
    from data_provider.yfinance_fetcher import YfinanceFetcher
    from src.services import us_market_scan as scan
    from src.services.stock_list_parser import parse_analysis_target
    from src.services.screening.snapshot_us import fetch_daily_history_yfinance
    current = at("2026-09-21T10:00")
    intraday = bars([at("2026-09-21T09:55")], [105])
    daily = bars([datetime(2026, 9, 18)], [100])
    requests = []
    def download(symbols, **kwargs):
        requests.append(symbols)
        return intraday if kwargs.get("interval") == "5m" else daily
    monkeypatch.setattr("yfinance.download", download)
    frame = fetch_us_snapshot(["BRK-B"], now=current)
    target = parse_analysis_target(frame.iloc[0].code)
    assert target.exchange == "US"
    assert target.canonical_id == "BRK.B"
    fetcher = YfinanceFetcher.__new__(YfinanceFetcher)
    assert fetcher._convert_stock_code(target.canonical_id) == "BRK-B"
    fetch_daily_history_yfinance(target.canonical_id)
    assert requests[-1] == "BRK-B"
    monkeypatch.setattr(scan, "fetch_us_universe", lambda source: ["BRK-B"])
    monkeypatch.setattr(scan, "SECTOR_ETFS", {})
    result = scan.collect_us_market_scan(["BRK.B"], now=current)
    assert result["available_count"] == 1
    assert result["gainers"] == []
    result = scan.collect_us_market_scan([], now=current)
    assert result["gainers"][0]["code"] == "BRK.B"


def test_scan_holds_one_time_when_universe_fetch_crosses_market_open(monkeypatch):
    from src.services import us_market_scan as scan
    from src.services.screening import snapshot_us
    before = at("2026-09-21T09:29:59")
    after = at("2026-09-21T09:30:01")
    monkeypatch.setattr(scan, "datetime", SimpleNamespace(now=lambda tz: before))
    monkeypatch.setattr(snapshot_us, "datetime", SimpleNamespace(now=lambda tz: after))
    monkeypatch.setattr(scan, "fetch_us_universe", lambda source: ["AMD"])
    monkeypatch.setattr(scan, "SECTOR_ETFS", {})
    intraday = bars([at("2026-09-21T09:25"), at("2026-09-21T09:30")], [105, 110])
    daily = bars([datetime(2026, 9, 18)], [100])
    monkeypatch.setattr("yfinance.download", lambda *a, **k: intraday if k["interval"] == "5m" else daily)
    result = scan.collect_us_market_scan()
    assert result["available"]
    assert result["session"] == "premarket"
    assert result["as_of"] == before.isoformat()
    assert result["gainers"][0]["price"] == 105


@pytest.mark.parametrize("change", [None, 0, 5])
def test_saved_quote_provenance_survives_all_report_renderers(change):
    import json
    from src.analyzer import AnalysisResult
    from src.notification import NotificationService
    from src.report_language import get_report_labels
    from src.services.history_service import HistoryService
    from src.services.report_renderer import render

    result = AnalysisResult(code="AAPL", name="Apple", sentiment_score=50, trend_prediction="Neutral",
                            operation_advice="Watch", analysis_summary="Research", report_language="en")
    # A regular daily move must never replace a missing/zero session move.
    result.market_snapshot = {"date": "2026-09-18", "close": "100.00", "pct_chg": "+7.00%"}
    result.set_realtime_quote({"price": 105, "change_pct": change, "quote_session": "premarket",
                               "provider_timestamp": "2026-09-18T20:00:00Z", "is_stale": True,
                               "pre_close": 100})
    saved = json.loads(json.dumps(result.to_dict()))
    result.market_snapshot = saved["market_snapshot"]
    assert result.market_snapshot["change_pct"] == change
    history = []
    HistoryService._append_market_snapshot_to_report(history, result, get_report_labels("en"))
    history_text = "\n".join(history)
    assert "+7.00%" not in history_text.split("#### Regular-session daily bar")[0]
    assert ("(--)" if change is None else f"({change:+.2f}%)") in history_text
    notification = NotificationService.__new__(NotificationService)
    notification.config = SimpleNamespace(report_language="en")
    notice = []
    notification._append_market_snapshot(notice, result)
    with patch("src.services.report_renderer.get_config", return_value=SimpleNamespace(
        report_templates_dir="templates", report_language="en", report_show_llm_model=False,
    )):
        template = render("markdown", [result], "2026-09-21")
    assert template
    for text in (history_text, "\n".join(notice), template):
        assert "premarket" in text
        assert "2026-09-18T20:00:00Z" in text
        assert "Stale/unverified; reference only" in text
        session_text, daily_text = text.split("#### Regular-session daily bar — 2026-09-18", 1)
        assert "+7.00%" not in session_text
        assert "+7.00%" in daily_text


@pytest.mark.parametrize("current,previous_date", [
    ("2026-09-22T10:02", "2026-09-21"),
    ("2026-09-08T10:02", "2026-09-04"),  # holiday plus weekend
])
@pytest.mark.parametrize("daily_state", ["completed", "already_intraday", "missing_previous"])
def test_intraday_context_and_report_use_the_previous_session(current, previous_date, daily_state):
    from copy import deepcopy
    from src.analyzer import GeminiAnalyzer
    from src.core.pipeline import StockAnalysisPipeline
    from src.stock_analyzer import TrendAnalysisResult

    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(report_language="en")
    pipeline.search_service = None
    prior_bar = {"date": previous_date, "close": 501.61, "volume": 28_000_000,
                 "volume_ratio": 1.21, "turnover_rate": 2.2, "amplitude": 2.13}
    older_bar = {"date": "2026-09-03", "close": 493.78, "volume": 40_000_000}
    context = {"code": "MSFT", "date": previous_date, "today": prior_bar,
               "yesterday": older_bar, "price_change_ratio": 1.59, "volume_change_ratio": 0.7}
    if daily_state == "already_intraday":
        context.update(date=current[:10], today={"date": current[:10], "close": 498.5}, yesterday=prior_bar)
    elif daily_state == "missing_previous":
        context.update(date=older_bar["date"], today=older_bar, yesterday={})
    original = deepcopy(context)
    quote = UnifiedRealtimeQuote(code="MSFT", price=496.35, pre_close=501.61,
                                 quote_session="regular", provider_timestamp=at(current).isoformat(),
                                 is_stale=False, high=508.5, low=495.9, volume=3_500_000,
                                 change_pct=(496.35 / 501.61 - 1) * 100)
    trend = TrendAnalysisResult(code="MSFT", ma5=495.96, ma10=496.2, ma20=498.88)
    with patch("src.core.pipeline.get_market_now", return_value=at(current)):
        enhanced = pipeline._enhance_context(context, quote, None, trend, fundamental_context={},
                                             market_phase_context={"is_partial_bar": False})
    # The batch may have begun premarket; the current regular quote is still partial.
    assert enhanced["today"]["is_partial_bar"] is True
    assert context == original
    assert enhanced["price_change_ratio"] == -1.05
    assert enhanced["today"]["prev_close"] == 501.61
    assert enhanced["today"]["date"] == current[:10]
    assert "volume_ratio" not in enhanced["today"]
    assert "turnover_rate" not in enhanced["today"]
    if daily_state == "missing_previous":
        assert enhanced["yesterday"] == {}
        assert enhanced["volume_change_ratio"] is None
    else:
        assert enhanced["yesterday"]["date"] == previous_date
        assert enhanced["volume_change_ratio"] == 0.12
    snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(enhanced)
    assert snapshot["date"] == current[:10]
    assert snapshot["is_partial_bar"] is True
    assert snapshot["prev_close"] == "501.61"
    assert snapshot["change_amount"] == "-5.26"
    assert snapshot["pct_chg"] == "-1.05%"
    assert snapshot["amplitude"] == "2.51%"


@pytest.mark.parametrize("session", ["premarket", "postmarket"])
@pytest.mark.parametrize("language,session_heading,daily_heading", [
    ("en", "Session quote", "Regular-session daily bar"),
    ("zh", "时段报价", "常规时段日线"),
    ("ko", "세션 시세", "정규장 일봉"),
])
def test_session_quote_and_daily_metrics_are_separate_in_every_renderer(session, language, session_heading, daily_heading):
    from src.analyzer import AnalysisResult
    from src.notification import NotificationService
    from src.report_language import get_report_labels
    from src.services.history_service import HistoryService
    from src.services.report_renderer import render

    result = AnalysisResult(code="MSFT", name="Microsoft", sentiment_score=50, trend_prediction="Neutral",
                            operation_advice="Watch", analysis_summary="Research", report_language=language)
    result.market_snapshot = {"date": "2026-09-21", "close": "501.61", "high": "501.87",
                              "volume": "27873100", "pct_chg": "1.59%"}
    result.set_realtime_quote({"price": 507.60, "change_pct": 1.194, "quote_session": session,
                               "provider_timestamp": "2026-09-22T12:47:54+00:00", "is_stale": False,
                               "pre_close": 501.61})
    history = []
    HistoryService._append_market_snapshot_to_report(history, result, get_report_labels(language))
    service = NotificationService.__new__(NotificationService)
    service.config = SimpleNamespace(report_language=language)
    notification = []
    service._append_market_snapshot(notification, result)
    with patch("src.services.report_renderer.get_config", return_value=SimpleNamespace(
        report_templates_dir="templates", report_language=language, report_show_llm_model=False,
    )):
        template = render("markdown", [result], "2026-09-22")
    assert template
    for output in ("\n".join(history), "\n".join(notification), template):
        quote_text, daily_text = output.split(f"#### {daily_heading} — 2026-09-21", 1)
        assert f"#### {session_heading}" in quote_text
        assert "507.60" in quote_text and "+1.19%" in quote_text
        assert "501.87" not in quote_text and "27873100" not in quote_text
        assert "501.87" in daily_text and "27873100" in daily_text
        assert "1.59%" in daily_text


def test_daily_snapshot_keeps_bar_date_and_partial_status():
    from src.analyzer import GeminiAnalyzer
    from src.formatters import format_session_market_snapshot
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    snapshot = analyzer._build_market_snapshot({"date": "2026-09-22", "today": {
        "date": "2026-09-21", "close": 501.61}, "realtime": {"price": 507.60}})
    assert snapshot["date"] == "2026-09-21"
    snapshot.update(quote_session="premarket", price="507.60", change_pct=None)
    assert "Regular-session daily bar — 2026-09-21" in format_session_market_snapshot(snapshot)
    snapshot.update(date="2026-09-22", is_partial_bar=True, quote_session="regular")
    assert "2026-09-22 (in progress at analysis)" in format_session_market_snapshot(snapshot)


@pytest.mark.parametrize("now,extended", [
    # Yesterday's after-hours print is still reported shortly after today's close.
    ("2026-09-22T16:05", {"postMarketPrice": 90, "postMarketTime": at("2026-09-21T19:59").timestamp()}),
    # Yesterday's pre-market print is still reported before today's first trade.
    ("2026-09-22T08:45", {"preMarketPrice": 90, "preMarketTime": at("2026-09-21T09:20").timestamp()}),
])
def test_extended_price_from_an_earlier_session_is_not_this_sessions_move(now, extended):
    payload = {"regularMarketPrice": 100, "regularMarketPreviousClose": 98,
               "regularMarketTime": at("2026-09-22T16:00" if "16:05" in now else "2026-09-21T16:00").timestamp(),
               **extended}
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), payload, at(now))
    assert quote.price == 100
    assert quote.quote_session == "regular"
    assert quote.is_stale is True
    assert quote.change_pct is None or abs(quote.change_pct - (100 / 98 - 1) * 100) < 1e-9


# --- Review fixes: Longbridge session contract, one US freshness threshold,
# --- non-US stale behavior unchanged, US sector capability, early-close postmarket.

def _longbridge_quote(**extra):
    from decimal import Decimal
    fields = dict(last_done=Decimal("102"), prev_close=Decimal("100"), open=Decimal("101"),
                  high=Decimal("103"), low=Decimal("99"), volume=5_000_000, turnover=Decimal("510000000"),
                  timestamp=None, pre_market_quote=None, post_market_quote=None)
    fields.update(extra)
    return SimpleNamespace(**fields)


def _longbridge_realtime(q, now):
    from data_provider import us_session
    from data_provider.longbridge_fetcher import LongbridgeFetcher

    fetcher = LongbridgeFetcher.__new__(LongbridgeFetcher)
    real_apply = us_session.apply_us_quote_metadata
    ctx = SimpleNamespace(quote=lambda symbols: [q])
    with patch.object(LongbridgeFetcher, "is_available_for_request", return_value=True), \
            patch.object(LongbridgeFetcher, "_get_ctx", return_value=ctx), \
            patch.object(LongbridgeFetcher, "_get_static_info", return_value=None), \
            patch.object(LongbridgeFetcher, "_compute_volume_ratio", return_value=1.4), \
            patch("data_provider.us_session.apply_us_quote_metadata",
                  side_effect=lambda quote, info, now_=None: real_apply(quote, info, at(now))):
        return fetcher.get_realtime_quote("AAPL")


def test_longbridge_regular_quote_follows_us_session_contract():
    quote = _longbridge_realtime(_longbridge_quote(timestamp=at("2026-09-21T09:58")), "2026-09-21T10:00")
    assert quote.market == "us"
    assert quote.quote_session == "regular"
    assert quote.is_stale is False
    assert quote.price == 102
    assert quote.pre_close == 100
    assert quote.change_pct == 2.0
    assert quote.volume_ratio == 1.4


def test_longbridge_postmarket_quote_uses_post_price_time_and_todays_close():
    post = SimpleNamespace(last_done="110.0", timestamp=at("2026-09-21T16:55"), prev_close="102")
    q = _longbridge_quote(timestamp=at("2026-09-21T16:00"), post_market_quote=post)
    quote = _longbridge_realtime(q, "2026-09-21T17:00")
    assert quote.quote_session == "postmarket"
    assert quote.is_stale is False
    assert quote.price == 110
    assert quote.pre_close == 102
    assert quote.change_pct == round((110 / 102 - 1) * 100, 2)
    # Regular-session activity must not describe the after-hours quote.
    assert quote.volume is quote.amount is quote.volume_ratio is quote.turnover_rate is None


def test_longbridge_quote_without_timestamp_is_an_explicit_reference_price():
    quote = _longbridge_realtime(_longbridge_quote(), "2026-09-21T10:00")
    assert quote.quote_session == "unknown"
    assert quote.is_stale is True
    assert quote.price == 102


def test_sdk_naive_datetimes_use_sdk_local_time_and_missing_stays_missing():
    from data_provider.us_session import sdk_epoch
    naive = datetime(2026, 9, 21, 9, 58)
    assert sdk_epoch(naive) == naive.timestamp()
    assert sdk_epoch(at("2026-09-21T09:58")) == at("2026-09-21T09:58").timestamp()
    assert sdk_epoch(None) is None
    assert sdk_epoch("not a time") is None


def test_us_session_verdict_is_not_overridden_by_realtime_cache_ttl():
    from data_provider.base import DataFetcherManager
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), {
        "regularMarketPrice": 101, "regularMarketPreviousClose": 100,
        "regularMarketTime": at("2026-09-21T10:00").timestamp(),
    }, at("2026-09-21T10:15"))
    assert quote.is_stale is False
    manager = DataFetcherManager.__new__(DataFetcherManager)
    # 15 minutes old: beyond the 600s cache TTL, within the 20-minute session contract.
    with patch.object(manager, "_utc_now_iso", return_value=at("2026-09-21T10:15").isoformat()):
        quote = manager._enrich_realtime_quote(quote, realtime_cache_ttl=600)
    assert quote.is_stale is False
    assert quote.stale_seconds == 900
    assert quote.quote_session == "regular"


def test_us_index_quote_gets_session_metadata_from_yahoo_regular_time():
    from data_provider.yfinance_fetcher import YfinanceFetcher
    from data_provider import us_session
    ticker = SimpleNamespace(
        fast_info=SimpleNamespace(lastPrice=5010.0, previousClose=5000.0, open=5001.0,
                                  dayHigh=5020.0, dayLow=4990.0, lastVolume=None),
        info={"regularMarketPrice": 5010.0, "regularMarketPreviousClose": 5000.0,
              "regularMarketTime": at("2026-09-21T10:59").timestamp(), "currency": "USD"},
    )
    real_apply = us_session.apply_us_quote_metadata
    with patch("yfinance.Ticker", return_value=ticker), \
            patch("data_provider.us_session.apply_us_quote_metadata",
                  side_effect=lambda quote, info, now=None: real_apply(quote, info, at("2026-09-21T11:00"))):
        quote = YfinanceFetcher()._get_us_index_realtime_quote("SPX", "^GSPC", "S&P 500")
    assert quote.quote_session == "regular"
    assert quote.is_stale is False
    assert round(quote.change_pct, 2) == 0.2


def _stale_hk_quote():
    return UnifiedRealtimeQuote(code="HK00700", market="hk", price=420, change_pct=5,
                                provider_timestamp="2026-09-21T16:08:00+08:00", is_stale=True)


def test_non_us_stale_quote_keeps_previous_alert_and_profile_behavior():
    import asyncio
    from unittest.mock import AsyncMock
    from data_provider.us_session import is_stale_us_quote
    from src.agent.events import EventMonitor, PriceAlert, PriceChangeAlert
    from src.services.alert_service import AlertService
    from src.services.stock_profile_service import StockProfileService

    quote = _stale_hk_quote()
    assert is_stale_us_quote(quote, "HK00700") is False
    monitor = EventMonitor()
    monitor._get_realtime_quote = AsyncMock(return_value=quote)
    price = PriceAlert(stock_code="HK00700", direction="above", price=400)
    change = PriceChangeAlert(stock_code="HK00700", direction="up", change_pct=2)
    assert asyncio.run(monitor._check_price(price)) is not None
    assert asyncio.run(monitor._check_price_change(change)) is not None
    service = AlertService.__new__(AlertService)
    for method, rule in [(service._evaluate_price, price), (service._evaluate_price_change, change)]:
        assert asyncio.run(method(rule, monitor))["triggered"] is True

    profile = StockProfileService.__new__(StockProfileService)
    with patch.object(profile, "_stock_service") as stock_service:
        stock_service.return_value.get_realtime_quote.return_value = {"stock_code": "HK00700", "is_stale": True}
        assert profile._quote_block("HK00700")["status"] == "fresh"
        stock_service.return_value.get_realtime_quote.return_value = {"stock_code": "AAPL", "is_stale": True}
        assert profile._quote_block("AAPL")["limitations"] == ["quote_stale"]


def test_non_us_enrichment_matches_ttl_semantics():
    from data_provider.base import DataFetcherManager
    manager = DataFetcherManager.__new__(DataFetcherManager)
    fresh = UnifiedRealtimeQuote(code="HK00700", market="hk", price=420, is_stale=True,
                                 provider_timestamp="2026-09-21T16:05:00+08:00")
    with patch.object(manager, "_utc_now_iso", return_value="2026-09-21T08:08:00+00:00"):
        fresh = manager._enrich_realtime_quote(fresh, realtime_cache_ttl=600)
    # A provider-set flag no longer sticks; the TTL decides, as before.
    assert fresh.is_stale is False
    assert fresh.quote_session is None
    untimed = manager._enrich_realtime_quote(UnifiedRealtimeQuote(code="600519", price=1500))
    assert untimed.is_stale is None


@patch("src.core.pipeline.is_market_open", return_value=True)
@patch("src.core.pipeline.get_market_for_stock", return_value="hk")
def test_non_us_stale_quote_still_augments_daily_history(_market, _open):
    from src.core.pipeline import StockAnalysisPipeline
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(enable_realtime_technical_indicators=True)
    df = pd.DataFrame([{"code": "HK00700", "date": date(2026, 9, 18), "open": 400, "high": 400,
                        "low": 400, "close": 400, "volume": 1, "amount": 0, "pct_chg": 0}])
    with patch("src.core.pipeline.get_market_now",
               return_value=datetime(2026, 9, 21, 12, 30, tzinfo=NEW_YORK)):
        result = pipeline._augment_historical_with_realtime(df, _stale_hk_quote(), "HK00700")
    assert len(result) == 2
    assert result.iloc[-1]["close"] == 420


@pytest.mark.parametrize("sectors,expect_section", [([], False), (
    [{"name": "Technology (XLK ETF)", "code": "XLK", "change_pct": 1.2, "provider_timestamp": "t"}], True)])
def test_us_sector_section_only_with_sector_data(sectors, expect_section):
    from src.config import get_config
    from src.market_analyzer import MarketAnalyzer, MarketIndex, MarketOverview
    analyzer = MarketAnalyzer(region="us", config=get_config())
    overview = MarketOverview(date="2026-09-21", top_sectors=sectors[:5], bottom_sectors=sectors[-5:])
    overview.indices = [MarketIndex(code="SPX", name="S&P 500", current=5000, change_pct=0.5)]
    prompt = analyzer._build_review_prompt(overview, [])
    disclosures = ("Sector/theme ranking data is not available", "该市场暂无行业板块/概念题材涨跌榜")
    assert any(text in prompt for text in disclosures) is not expect_section
    assert ("Technology (XLK ETF)" in prompt) is expect_section
    # The next overview without sectors restores the disclosure on the same analyzer.
    if expect_section:
        prompt = analyzer._build_review_prompt(MarketOverview(date="2026-09-21"), [])
        assert any(text in prompt for text in disclosures)


@pytest.mark.parametrize("value,session", [
    ("2026-11-27T16:59", "postmarket"), ("2026-11-27T17:00", "closed"),
    ("2026-09-21T19:59", "postmarket"), ("2026-09-21T20:00", "closed"),
])
def test_postmarket_ends_four_hours_after_an_early_close(value, session):
    assert session_window(at(value))[0] == session


def test_provider_alert_time_is_converted_to_local_naive_time():
    from src.services.alert_service import AlertService
    quote = SimpleNamespace(provider_timestamp="2026-09-21T14:00:00+00:00")
    expected = datetime.fromisoformat("2026-09-21T14:00:00+00:00").astimezone().replace(tzinfo=None)
    assert AlertService._extract_quote_datetime(quote) == expected


@pytest.mark.parametrize("last_regular,expected", [
    ("2026-09-18T15:55", 110),   # prior session's last regular bar: usable close
    ("2026-09-18T14:30", None),  # ends 85 minutes before the close: no verified reference
])
def test_snapshot_uses_prior_session_bars_when_yahoo_daily_omits_it(monkeypatch, last_regular, expected):
    # Live 2026-09-23: Yahoo's daily series skipped 09-22 while its 5-minute bars had it,
    # so every symbol lost its baseline and the scan returned nothing.
    current = at("2026-09-21T10:00")
    intraday = bars([at(last_regular), current - pd.Timedelta(minutes=5)], [110, 120])
    daily = bars([datetime(2026, 9, 17)], [100])  # the 18th is missing
    monkeypatch.setattr("yfinance.download", lambda *a, **k: intraday if k["interval"] == "5m" else daily)
    frame = fetch_us_snapshot(["AAPL"], now=current)
    if expected is None:
        assert frame.empty
    else:
        assert frame.iloc[0].reference_price == expected
        assert frame.iloc[0].change_pct == pytest.approx((120 / expected - 1) * 100, abs=0.0001)


def test_index_change_survives_a_missing_daily_bar_and_a_nan_latest_close(monkeypatch):
    # Live 2026-09-23: Yahoo's daily series skipped 09-22, so period="2d" returned only
    # today's bar and every index change was reported as +0.00%.
    from data_provider.yfinance_fetcher import YfinanceFetcher
    tz = "America/New_York"
    today = pd.Timestamp.now(tz=tz).normalize()
    hist = pd.DataFrame(
        {"Open": [7650.0, 7761.9, float("nan")], "High": [7770.0, 7761.9, float("nan")],
         "Low": [7640.0, 7700.0, float("nan")], "Close": [7764.7, 7709.0, float("nan")],
         "Volume": [1, 1, 0]},
        index=pd.DatetimeIndex([today - pd.Timedelta(days=2), today, today + pd.Timedelta(hours=1)]),
    )
    ticker = SimpleNamespace(history=lambda period: hist, fast_info=SimpleNamespace(previous_close=7764.27))
    fetcher = YfinanceFetcher.__new__(YfinanceFetcher)
    item = fetcher._fetch_yf_ticker_data(SimpleNamespace(Ticker=lambda code: ticker), "^GSPC", "S&P 500", "SPX")
    assert item["current"] == 7709.0 and item["prev_close"] == 7764.27
    assert item["change_pct"] == pytest.approx((7709.0 / 7764.27 - 1) * 100)


def test_english_market_review_uses_english_us_index_names():
    from src.market_analyzer import MarketAnalyzer, MarketIndex
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer._get_review_language = lambda: "en"
    label = analyzer._index_data_label(MarketIndex(code="SPX", name="标普500指数", daily_bar_date="2026-09-23"))
    assert label == "S&P 500 (regular-session daily bar: 2026-09-23)"
    assert analyzer._index_data_label(MarketIndex(code="IXIC", name="Nasdaq")) == "Nasdaq"
    analyzer._get_review_language = lambda: "zh"
    assert analyzer._index_data_label(MarketIndex(code="SPX", name="标普500指数")) == "标普500指数"


def test_afterhours_quote_keeps_the_days_close_and_move_for_reports():
    payload = {**info(), "regularMarketTime": at("2026-09-21T16:00").timestamp(),
               "postMarketPrice": 97, "postMarketTime": at("2026-09-21T17:59").timestamp()}
    quote = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), payload, at("2026-09-21T18:00"))
    previous = payload["regularMarketPreviousClose"]
    assert quote.regular_close == payload["regularMarketPrice"]
    assert quote.regular_change_pct == pytest.approx((payload["regularMarketPrice"] / previous - 1) * 100)
    assert {"regular_close", "regular_change_pct"} <= set(quote.to_dict())
    regular = apply_us_quote_metadata(UnifiedRealtimeQuote(code="AAPL"), info(), at("2026-09-21T11:00"))
    assert regular.regular_close is None  # only after the close
