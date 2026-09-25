from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from src.services.trade_desk import providers
from src.services.trade_desk.models import QuoteSnapshot


UTC = timezone.utc


@pytest.fixture(autouse=True)
def _isolate_from_local_opend_settings(monkeypatch):
    # Other suites load the developer's .env; these fixtures model OpenD themselves.
    for name in ("TRADE_DESK_OPEND_RSA_KEY_FILE", "TRADE_DESK_OPEND_HOST", "TRADE_DESK_OPEND_PORT",
                 "FUTU_OPEND_HOST", "FUTU_OPEND_PORT"):
        monkeypatch.delenv(name, raising=False)


def test_replay_is_deterministic_and_explicitly_synthetic() -> None:
    now = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)  # Friday, 15:00 ET
    first = providers.ReplayProvider(now=now).snapshot("SPY")
    second = providers.ReplayProvider(now=now).snapshot("SPY")

    assert first.model_dump() == second.model_dump()
    assert first.provider == "replay_synthetic"
    assert first.mode == "replay"
    assert first.source_verified is False
    assert "synthetic_replay_data_not_historical" in first.warnings
    assert "synthetic_replay_data_not_live" in first.warnings
    assert len({option.expiry.date() for option in first.options}) >= 3
    assert any(option.expiry.date() == now.astimezone(providers.US_EASTERN).date() for option in first.options)
    assert all(option.bid <= option.ask and option.iv and option.expiry_verified for option in first.options)
    assert providers.snapshot_fresh(first, now=now)


def test_replay_requested_expiry_and_close() -> None:
    now = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)
    expiry = datetime(2026, 9, 25, tzinfo=UTC).date()
    provider = providers.ReplayProvider(now=now)
    snapshot = provider.snapshot("AAPL", expiry=expiry)
    assert snapshot.options
    assert {option.expiry.date() for option in snapshot.options} == {expiry}
    provider.close()
    with pytest.raises(providers.ProviderError) as exc_info:
        provider.snapshot("AAPL", now=now)
    assert exc_info.value.code == "provider_closed"


def test_live_health_is_clear_when_open_d_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADE_DESK_OPEND_HOST", raising=False)
    monkeypatch.delenv("FUTU_OPEND_HOST", raising=False)
    provider = providers.MoomooProvider(sdk=object())
    health = provider.health()
    assert health["status"] == "blocked"
    assert health["code"] == "opend_not_configured"
    assert health["configured"] is False


class _FakeSubType:
    QUOTE = "QUOTE"
    ORDER_BOOK = "ORDER_BOOK"


class _FakeSDK:
    RET_OK = 0
    SubType = _FakeSubType
    OptionType = type("OptionType", (), {"ALL": "ALL"})
    IndexOptionType = type("IndexOptionType", (), {"NORMAL": "NORMAL"})


_FUND_UNDERLYINGS = {"US.SPY", "US.QQQ", "US.ARKK"}


def _is_underlying(code: str) -> bool:
    return re.fullmatch(r"US\.[A-Z.]+", code) is not None


class _FakeQuoteContext:
    """OpenD-like fake: per-connection, per-subtype subscriptions.

    Like the real SDK, get_stock_quote has no bid/ask columns and requires an
    own-connection QUOTE subscription; get_order_book requires ORDER_BOOK.
    """

    def __init__(self, other_connection=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.subscriptions: dict[str, set[str]] = {}
        self.other_connection: dict[str, set[str]] = {
            kind: set(codes) for kind, codes in (other_connection or {}).items()
        }
        self.books: dict[str, dict] = {}
        self.reject_unsubscribe = False
        self.closed = False

    def query_subscription(self, is_all_conn: bool = True):
        self.calls.append(("query_subscription", is_all_conn))
        own_used = sum(len(values) for values in self.subscriptions.values())
        other_used = sum(len(values) for values in self.other_connection.values())
        sources = [self.subscriptions] + ([self.other_connection] if is_all_conn else [])
        sub_list: dict[str, list[str]] = {}
        for source in sources:
            for kind, codes in source.items():
                sub_list.setdefault(kind, [])
                sub_list[kind] = sorted(set(sub_list[kind]) | codes)
        return 0, {
            "total_used": own_used + other_used,
            "remain": 100 - own_used - other_used,
            "own_used": own_used,
            "sub_list": sub_list,
        }

    def subscribe(self, code_list, subtype_list, **kwargs):
        self.calls.append(("subscribe", list(code_list)))
        for subtype in subtype_list:
            self.calls.append(("subscribe_type", (str(subtype), list(code_list))))
            self.subscriptions.setdefault(str(subtype), set()).update(code_list)
        return 0, None

    def unsubscribe(self, code_list, subtype_list, **kwargs):
        self.calls.append(("unsubscribe", list(code_list)))
        if self.reject_unsubscribe:
            return -1, "Unsubscription is allowed at least one minute after subscribing"
        for subtype in subtype_list:
            self.subscriptions.setdefault(str(subtype), set()).difference_update(code_list)
        return 0, None

    def get_global_state(self):
        return 0, {"quote_right": True, "option_quote_right": True, "qot_logined": "1"}

    def get_stock_quote(self, code_list):
        missing = [code for code in code_list if code not in self.subscriptions.get("QUOTE", set())]
        if missing:
            return -1, f"Please subscribe to QUOTE first: {missing}"
        return 0, [
            {
                "code": code,
                "data_date": "2026-09-18",
                "data_time": "15:00:00",
                "last_price": 500.0,
            }
            for code in code_list
        ]

    def get_option_chain(self, code, **kwargs):
        root = code.replace("US.", "")
        return 0, [
            {
                "code": f"US.{root}260918C00500000",
                "strike_time": "2026-09-18",
                "expiry_time": "16:00:00",
                "option_type": "CALL",
                "strike_price": 500,
                "option_standard": "STANDARD",
                "index_option_type": "NORMAL",
                "contract_size": 100,
                "option_style": "AMERICAN",
            },
            {
                "code": f"US.{root}260918P00500000",
                "strike_time": "2026-09-18",
                "expiry_time": "16:00:00",
                "option_type": "PUT",
                "strike_price": 500,
                "option_standard": "STANDARD",
                "index_option_type": "NORMAL",
                "contract_size": 100,
                "option_style": "AMERICAN",
            },
            {
                "code": f"US.{root}260925C00500000",
                "strike_time": "2026-09-25",
                "expiry_time": "16:00:00",
                "option_type": "CALL",
                "strike_price": 500,
                "option_standard": "STANDARD",
                "index_option_type": "NORMAL",
                "contract_size": 100,
                "option_style": "AMERICAN",
            },
        ]

    def get_market_snapshot(self, code_list):
        self.calls.append(("get_market_snapshot", list(code_list)))
        rows = []
        for code in code_list:
            if _is_underlying(code):
                fund = code in _FUND_UNDERLYINGS
                rows.append(
                    {
                        "code": code,
                        "update_time": "2026-09-18 15:00:00",
                        "last_price": 500.0,
                        "bid_price": 499.9,
                        "ask_price": 500.1,
                        "equity_valid": not fund,
                        "trust_valid": fund,
                    }
                )
            else:
                rows.append(
                    {
                        "code": code,
                        "bid_price": 4.9,
                        "ask_price": 5.1,
                        "last_price": 5.0,
                        "option_implied_volatility": 22.0,
                        "option_open_interest": 321,
                        "update_time": "2026-09-18 15:00:00",
                    }
                )
        return 0, rows

    def get_order_book(self, code, num=10):
        if code not in self.subscriptions.get("ORDER_BOOK", set()):
            return -1, f"Please subscribe to ORDER_BOOK first: {code}"
        if code not in self.books:
            return -1, "order book unavailable"
        return 0, dict(self.books[code], code=code)

    def close(self):
        self.closed = True


def _session_close_16(expiry):
    return datetime.combine(expiry, datetime.min.time(), tzinfo=providers.US_EASTERN).replace(hour=16)


def test_live_fake_sdk_uses_provider_timestamps_and_releases_subscriptions(monkeypatch) -> None:
    context = _FakeQuoteContext()
    monkeypatch.setattr(
        providers,
        "_resolve_session_close",
        lambda expiry: datetime.combine(expiry, datetime.min.time(), tzinfo=providers.US_EASTERN).replace(
            hour=16
        ),
    )
    provider = providers.MoomooProvider(
        sdk=_FakeSDK,
        context=context,
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
        max_contracts=4,
    )
    snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))

    assert snapshot.mode == "live"
    assert snapshot.source_verified is True
    assert snapshot.quoted_at == datetime(2026, 9, 18, 15, 0, tzinfo=providers.US_EASTERN)
    assert snapshot.options
    assert all(option.expiry_verified and option.quoted_at == snapshot.quoted_at for option in snapshot.options)
    # Subscriptions are retained between ticks but remain bounded; close() releases
    # the provider-owned records explicitly.
    assert sum(len(values) for values in context.subscriptions.values()) <= 2 * (provider._max_contracts + 1)
    assert provider.health()["status"] == "ready"
    provider.close()
    assert not any(context.subscriptions.values())
    assert any(name == "unsubscribe" for name, _ in context.calls)
    assert context.closed is True


def test_live_does_not_fabricate_expiry_when_calendar_is_unknown(monkeypatch) -> None:
    context = _FakeQuoteContext()
    monkeypatch.setattr(providers, "_resolve_session_close", lambda expiry: None)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context)
    snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    assert snapshot.options == []
    assert any("expiry_unverified" in warning for warning in snapshot.warnings)


def test_build_provider_and_freshness_reject_crossed_or_old_quotes() -> None:
    assert isinstance(providers.build_provider("replay"), providers.ReplayProvider)
    assert isinstance(providers.build_provider("live"), providers.MoomooProvider)
    now = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)
    base = providers.ReplayProvider(now=now).snapshot("SPY", now=now)
    assert providers.snapshot_fresh(base, now=now)
    old = base.model_copy(update={"quoted_at": now - timedelta(seconds=31)})
    assert not providers.snapshot_fresh(old, now=now)
    crossed = base.model_copy(update={"bid": base.ask + 1})
    assert not providers.snapshot_fresh(crossed, now=now)


def test_live_crossed_underlying_is_stale_and_unverified(monkeypatch) -> None:
    context = _FakeQuoteContext()
    monkeypatch.setattr(
        providers,
        "_resolve_session_close",
        lambda expiry: datetime.combine(expiry, datetime.min.time(), tzinfo=providers.US_EASTERN).replace(
            hour=16
        ),
    )

    original_snapshot = context.get_market_snapshot

    def crossed_quote(code_list):
        ret, rows = original_snapshot(code_list)
        for row in rows:
            if _is_underlying(row["code"]):
                row.update(bid_price=501.0, ask_price=499.0)
        return ret, rows

    monkeypatch.setattr(context, "get_market_snapshot", crossed_quote)
    provider = providers.MoomooProvider(
        sdk=_FakeSDK,
        context=context,
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
        max_contracts=2,
    )
    snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))

    assert snapshot.stale is True
    assert snapshot.source_verified is False
    assert snapshot.bid == 501.0 and snapshot.ask == 499.0
    assert not providers.snapshot_fresh(snapshot, now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    provider.close()


def test_order_book_fallback_preserves_market_metadata() -> None:
    primary = providers._QuoteData(
        bid=5.5,
        ask=4.5,
        iv=0.22,
        volume=17,
        open_interest=31,
        timestamp=datetime(2026, 9, 18, 15, 0, tzinfo=providers.US_EASTERN),
        crossed=True,
    )
    fallback = providers._QuoteData(
        bid=4.9,
        ask=5.1,
        bid_size=8,
        ask_size=9,
        timestamp=datetime(2026, 9, 18, 15, 0, 1, tzinfo=providers.US_EASTERN),
    )

    merged = providers._merge_quote(primary, fallback)

    assert merged is not None
    assert (merged.bid, merged.ask) == (4.9, 5.1)
    assert (merged.iv, merged.volume, merged.open_interest) == (0.22, 17, 31)
    assert merged.timestamp == fallback.timestamp


def test_known_etf_date_only_chain_uses_published_late_close(monkeypatch) -> None:
    context = _FakeQuoteContext()
    monkeypatch.setattr(
        providers,
        "_resolve_session_close",
        lambda expiry: datetime.combine(expiry, datetime.min.time(), tzinfo=providers.US_EASTERN).replace(
            hour=16
        ),
    )
    original = context.get_option_chain

    def date_only_chain(code, **kwargs):
        ret, rows = original(code, **kwargs)
        for row in rows:
            row.pop("expiry_time", None)
        return ret, rows

    monkeypatch.setattr(context, "get_option_chain", date_only_chain)
    provider = providers.MoomooProvider(
        sdk=_FakeSDK,
        context=context,
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
    )
    snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))

    assert snapshot.options
    assert all(
        option.expiry.astimezone(providers.US_EASTERN).hour == 16
        and option.expiry.astimezone(providers.US_EASTERN).minute == 15
        for option in snapshot.options
    )
    assert "expiry_cutoff_nyse_arca_late_close_1615_et" in snapshot.warnings
    provider.close()


def test_health_reports_permission_and_quota_blockers(monkeypatch) -> None:
    context = _FakeQuoteContext()
    monkeypatch.setattr(
        context,
        "get_global_state",
        lambda: (0, {"quote_right": False, "option_quote_right": False}),
    )
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context)
    health = provider.health()
    assert health["status"] == "blocked"
    assert health["code"] == "quote_permission"
    assert health["quote_right"] is False and health["option_right"] is False
    provider.close()

    quota_context = _FakeQuoteContext()
    monkeypatch.setattr(
        quota_context,
        "query_subscription",
        lambda is_all_conn=True: (0, {"remain": 0, "own_used": 100, "sub_list": {}}),
    )
    quota_provider = providers.MoomooProvider(sdk=_FakeSDK, context=quota_context)
    with pytest.raises(providers.ProviderError) as exc_info:
        quota_provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    assert exc_info.value.code == "subscription_quota"
    quota_provider.close()


class _DisconnectingContext(_FakeQuoteContext):
    def get_stock_quote(self, code_list):
        raise RuntimeError("connection reset by peer")

    def get_market_snapshot(self, code_list):
        raise RuntimeError("connection reset by peer")


def test_connection_reset_reconnects_once_then_retries(monkeypatch) -> None:
    first_context = _DisconnectingContext()
    replacement_contexts = []

    class ReconnectSDK(_FakeSDK):
        @staticmethod
        def OpenQuoteContext(host=None, port=None):
            replacement = _FakeQuoteContext()
            replacement_contexts.append(replacement)
            return replacement

    provider = providers.MoomooProvider(
        sdk=ReconnectSDK,
        context=first_context,
        host="127.0.0.1",
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
    )
    snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))

    assert snapshot.spot == 500.0
    assert first_context.closed is True
    assert len(replacement_contexts) == 1
    provider.close()


def test_connection_reset_without_recovery_is_reported() -> None:
    first_context = _DisconnectingContext()

    class FailingReconnectSDK(_FakeSDK):
        @staticmethod
        def OpenQuoteContext(host=None, port=None):
            return _DisconnectingContext()

    provider = providers.MoomooProvider(
        sdk=FailingReconnectSDK,
        context=first_context,
        host="127.0.0.1",
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
    )
    with pytest.raises(providers.ProviderError) as exc_info:
        provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    assert exc_info.value.code == "connection_unavailable"
    provider.close()


def test_holiday_and_early_close_rows_have_no_fabricated_expiry(monkeypatch) -> None:
    context = _FakeQuoteContext()
    original = context.get_option_chain

    def date_only_chain(code, **kwargs):
        ret, rows = original(code, **kwargs)
        for row in rows:
            row.pop("expiry_time", None)
        return ret, rows

    monkeypatch.setattr(context, "get_option_chain", date_only_chain)
    monkeypatch.setattr(providers, "_resolve_session_close", lambda expiry: None)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context)
    holiday_snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    assert holiday_snapshot.options == []
    assert any("expiry_unverified" in warning for warning in holiday_snapshot.warnings)
    provider.close()

    early_context = _FakeQuoteContext()
    monkeypatch.setattr(early_context, "get_option_chain", date_only_chain)
    monkeypatch.setattr(
        providers,
        "_resolve_session_close",
        lambda expiry: datetime.combine(expiry, datetime.min.time(), tzinfo=providers.US_EASTERN).replace(
            hour=13
        ),
    )
    early_provider = providers.MoomooProvider(sdk=_FakeSDK, context=early_context)
    early_snapshot = early_provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    assert early_snapshot.options == []
    assert any("expiry_unverified" in warning for warning in early_snapshot.warnings)
    early_provider.close()


def test_adjusted_and_index_contracts_are_excluded(monkeypatch) -> None:
    context = _FakeQuoteContext()

    def unsupported_chain(code, **kwargs):
        return 0, [
            {
                "code": "US.SPY260925C00500000",
                "strike_time": "2026-09-25",
                "expiry_time": "16:00:00",
                "option_type": "CALL",
                "strike_price": 500,
                "option_standard": "NON_STANDARD",
                "index_option_type": "NORMAL",
                "contract_size": 100,
            },
            {
                "code": "US.SPY260925P00500000",
                "strike_time": "2026-09-25",
                "expiry_time": "16:00:00",
                "option_type": "PUT",
                "strike_price": 500,
                "option_standard": "STANDARD",
                "index_option_type": "INDEX",
                "contract_size": 100,
            },
        ]

    monkeypatch.setattr(context, "get_option_chain", unsupported_chain)
    monkeypatch.setattr(
        providers,
        "_resolve_session_close",
        lambda expiry: datetime.combine(expiry, datetime.min.time(), tzinfo=providers.US_EASTERN).replace(
            hour=16
        ),
    )
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context)
    snapshot = provider.snapshot("SPY", now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    assert snapshot.options == []
    provider.close()


def test_replay_future_clock_keeps_valid_multiple_sessions() -> None:
    now = datetime(2038, 7, 16, 19, 0, tzinfo=UTC)
    snapshot = providers.ReplayProvider(now=now).snapshot("QQQ")
    expiry_dates = {option.expiry.date() for option in snapshot.options}
    assert len(expiry_dates) >= 3
    assert all(option.expiry.astimezone(providers.US_EASTERN).date().weekday() < 5 for option in snapshot.options)
    assert all(option.iv and option.bid <= option.ask for option in snapshot.options)


@pytest.mark.parametrize('field,value,expected', [
    ('option_implied_volatility', 1.5, 0.015),
    ('option_implied_volatility', 22.0, 0.22),
    ('option_implied_volatility', 450.0, 4.5),
    ('iv', 4.5, 4.5),
])
def test_snapshot_iv_units_are_explicit_not_inferred_from_magnitude(field, value, expected):
    quote = providers._quote_from_row({field: value}, default_tz=providers.US_EASTERN)
    assert quote.iv == pytest.approx(expected)


def test_live_shortlist_keeps_held_contracts_after_underlying_moves(monkeypatch):
    context = _FakeQuoteContext()
    original = context.get_option_chain
    held = "US.SPY260918C00100000"

    def chain_with_distant_strike(code, **kwargs):
        ret, rows = original(code, **kwargs)
        rows.append(dict(rows[0], code=held, strike_price=100))
        return ret, rows

    monkeypatch.setattr(context, "get_option_chain", chain_with_distant_strike)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context,
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC), max_contracts=2)
    try:
        market = provider.snapshot("SPY", expiry="2026-09-18", required_contracts=[held])
        assert held in {item.contract_id for item in market.options}
        assert len(market.options) <= 2
        # Option quotes come from the (subscription-free) market snapshot.
        assert any(name == "get_market_snapshot" and held in codes for name, codes in context.calls)
        with pytest.raises(providers.ProviderError, match="Required contracts"):
            provider.snapshot("SPY", required_contracts=[held, "second", "third"])
    finally:
        provider.close()


NOW_UTC = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)  # Friday 15:00 ET
BOOK_TIME = "2026-09-18 15:00:00"


def _book(bid, ask, bid_time=BOOK_TIME, ask_time=BOOK_TIME):
    return {"svr_recv_time_bid": bid_time, "svr_recv_time_ask": ask_time,
            "Bid": [(bid, 7, 1, {})], "Ask": [(ask, 9, 1, {})]}


def test_order_book_fallback_subscribes_order_book_subtype(monkeypatch):
    # Regression: a code already held as QUOTE was treated as subscribed for
    # ORDER_BOOK, so get_order_book failed and aborted the whole snapshot.
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_market_snapshot

    def no_option_timestamp(code_list):
        ret, rows = original(code_list)
        for row in rows:
            if not _is_underlying(row["code"]):
                row.pop("update_time")
        return ret, rows

    monkeypatch.setattr(context, "get_market_snapshot", no_option_timestamp)
    for code in ("US.SPY260918C00500000", "US.SPY260918P00500000"):
        context.books[code] = _book(4.8, 5.2)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert {"US.SPY260918C00500000", "US.SPY260918P00500000"} <= context.subscriptions["ORDER_BOOK"]
        assert {(o.bid, o.ask) for o in snapshot.options} == {(4.8, 5.2)}
        assert all(o.bid_size == 7 and o.ask_size == 9 and o.iv == pytest.approx(0.22) for o in snapshot.options)
    finally:
        provider.close()


def test_other_connection_subscription_does_not_skip_own_subscribe(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext(other_connection={"QUOTE": {"US.SPY", "US.SPY260918C00500000"}})
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        provider.snapshot("SPY", now=NOW_UTC)
        assert "US.SPY" in context.subscriptions["QUOTE"]
        # Option contracts are read via market snapshot; no QUOTE quota is spent on them.
        assert "US.SPY260918C00500000" not in context.subscriptions["QUOTE"]
        assert all(flag is False for name, flag in context.calls if name == "query_subscription")
    finally:
        provider.close()


def test_rejected_or_early_unsubscribe_keeps_records_tracked(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    clock = [NOW_UTC]
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, clock=lambda: clock[0], max_contracts=2)
    spy_records = {("US.SPY", "QUOTE")}
    try:
        provider.snapshot("SPY", now=clock[0])
        assert spy_records <= provider._subscriptions
        # Inside OpenD's one-minute minimum: no unsubscribe is attempted.
        clock[0] += timedelta(seconds=5)
        provider.snapshot("AAPL", now=clock[0])
        assert not any(name == "unsubscribe" for name, _ in context.calls)
        assert spy_records <= provider._subscriptions
        # Old enough, but OpenD rejects: records stay tracked, not orphaned.
        context.reject_unsubscribe = True
        clock[0] += timedelta(seconds=90)
        snapshot = provider.snapshot("AAPL", now=clock[0])
        assert any(name == "unsubscribe" for name, _ in context.calls)
        assert spy_records <= provider._subscriptions
        assert "subscription_release_failed" in snapshot.warnings
        # A later accepted release frees them on the server and in tracking.
        context.reject_unsubscribe = False
        clock[0] += timedelta(seconds=5)
        provider.snapshot("AAPL", now=clock[0])
        assert not spy_records & provider._subscriptions
        assert "US.SPY" not in context.subscriptions["QUOTE"]
    finally:
        provider.close()
    assert not any(context.subscriptions.values())


def test_codes_held_on_own_connection_are_tracked_for_release(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    context.subscriptions["QUOTE"] = {"US.SPY"}  # left over from a rejected release
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    provider.snapshot("SPY", now=NOW_UTC)
    assert ("US.SPY", "QUOTE") in provider._subscriptions
    provider.close()
    assert "US.SPY" not in context.subscriptions["QUOTE"]


def test_zero_iv_is_unavailable_not_a_validation_crash(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_market_snapshot

    def zero_iv(code_list):
        ret, rows = original(code_list)
        for row in rows:
            if not _is_underlying(row["code"]):
                row["option_implied_volatility"] = 0.0
        return ret, rows

    monkeypatch.setattr(context, "get_market_snapshot", zero_iv)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert snapshot.options and all(option.iv is None for option in snapshot.options)
        assert all(option.open_interest == 321 for option in snapshot.options)
    finally:
        provider.close()
    assert providers._quote_from_row({"option_implied_volatility": -3.0}).iv is None
    assert providers._quote_from_row({"option_open_interest": 1234}).open_interest == 1234


def test_underlying_bid_ask_comes_from_snapshot_and_unverified_without_it(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=_FakeQuoteContext(), now=NOW_UTC, max_contracts=2)
    snapshot = provider.snapshot("SPY", now=NOW_UTC)
    assert (snapshot.bid, snapshot.ask, snapshot.spot) == (499.9, 500.1, 500.0)
    assert snapshot.source_verified is True and providers.snapshot_fresh(snapshot, now=NOW_UTC)
    provider.close()

    context = _FakeQuoteContext()
    original = context.get_market_snapshot

    def no_underlying_snapshot(code_list):
        if any(_is_underlying(code) for code in code_list):
            return -1, "snapshot unavailable"
        return original(code_list)

    monkeypatch.setattr(context, "get_market_snapshot", no_underlying_snapshot)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        # get_stock_quote has no bid/ask and no order book is available.
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert snapshot.spot == 500.0 and snapshot.bid is None and snapshot.ask is None
        assert snapshot.source_verified is False and snapshot.stale is True
        assert "underlying_bid_ask_unavailable" in snapshot.warnings
        assert not providers.snapshot_fresh(snapshot, now=NOW_UTC)
        # A complete order book verifies the underlying instead.
        context.books["US.SPY"] = _book(499.8, 500.2)
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert (snapshot.bid, snapshot.ask) == (499.8, 500.2)
        assert snapshot.source_verified is True and snapshot.stale is False
    finally:
        provider.close()


def test_health_uses_normalized_rights_and_quote_login():
    class DeniedContext(_FakeQuoteContext):
        def get_global_state(self):
            return 0, {"quote_right": 0, "option_quote_right": "NO", "qot_logined": "1"}

    health = providers.MoomooProvider(sdk=_FakeSDK, context=DeniedContext()).health()
    assert (health["status"], health["code"]) == ("blocked", "quote_permission")
    assert health["quote_right"] is False and health["option_right"] is False

    class LoggedOutContext(_FakeQuoteContext):
        def get_global_state(self):
            return 0, {"quote_right": True, "option_quote_right": True, "qot_logined": "0"}

    health = providers.MoomooProvider(sdk=_FakeSDK, context=LoggedOutContext()).health()
    assert (health["status"], health["code"]) == ("blocked", "quote_login_required")

    class RealShapeContext(_FakeQuoteContext):
        def get_global_state(self):
            return 0, {"market_us": "AFTERNOON", "qot_logined": "1", "trd_logined": "0"}

    health = providers.MoomooProvider(sdk=_FakeSDK, context=RealShapeContext()).health()
    assert (health["status"], health["ok"], health["code"]) == ("degraded", False, "rights_unknown")
    assert health["quota"]["used"] == 0  # own_used, not total_used


def test_merge_quote_never_mixes_price_sources():
    stamp = datetime(2026, 9, 18, 15, 0, tzinfo=providers.US_EASTERN)
    snapshot_quote = providers._QuoteData(bid=4.0, ask=6.0, bid_size=1, ask_size=1, iv=0.3, timestamp=None)
    book = providers._QuoteData(bid=4.9, ask=5.1, bid_size=8, ask_size=9, timestamp=stamp)
    merged = providers._merge_quote(snapshot_quote, book)
    assert (merged.bid, merged.ask, merged.bid_size, merged.ask_size, merged.timestamp) == (4.9, 5.1, 8, 9, stamp)
    assert merged.iv == 0.3

    one_sided = providers._QuoteData(bid=None, ask=5.1, timestamp=stamp)
    incomplete_book = providers._QuoteData(bid=4.9, ask=5.0, timestamp=None)
    merged = providers._merge_quote(one_sided, incomplete_book)
    assert (merged.bid, merged.ask, merged.timestamp) == (None, 5.1, stamp)

    later = "2026-09-18 15:00:05"
    book_quote = providers._orderbook_quote(dict(_book(4.9, 5.1, ask_time=later), code="US.X"))
    assert book_quote.timestamp == stamp  # the older side dates the book


def test_warnings_do_not_leak_between_snapshots(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_option_chain

    def spy_date_only(code, **kwargs):
        ret, rows = original(code, **kwargs)
        if code == "US.SPY":
            for row in rows:
                row.pop("expiry_time", None)
        return ret, rows

    monkeypatch.setattr(context, "get_option_chain", spy_date_only)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        assert "expiry_cutoff_nyse_arca_late_close_1615_et" in provider.snapshot("SPY", now=NOW_UTC).warnings
        assert "expiry_cutoff_nyse_arca_late_close_1615_et" not in provider.snapshot("AAPL", now=NOW_UTC).warnings
    finally:
        provider.close()


def test_date_only_cutoff_uses_underlying_fund_type(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original_chain = context.get_option_chain
    original_snapshot = context.get_market_snapshot

    def date_only(code, **kwargs):
        ret, rows = original_chain(code, **kwargs)
        for row in rows:
            row.pop("expiry_time", None)
            row["stock_type"] = "DRVT"  # real chain rows cannot identify ETFs
        return ret, rows

    def unmarked(code_list):
        ret, rows = original_snapshot(code_list)
        for row in rows:
            if row["code"] == "US.MSFT":
                row.pop("equity_valid")
                row.pop("trust_valid")
        return ret, rows

    monkeypatch.setattr(context, "get_option_chain", date_only)
    monkeypatch.setattr(context, "get_market_snapshot", unmarked)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=4)
    try:
        equity = provider.snapshot("AAPL", now=NOW_UTC)
        assert equity.options
        assert all(o.expiry.astimezone(providers.US_EASTERN).hour == 16
                   and o.expiry.astimezone(providers.US_EASTERN).minute == 0 for o in equity.options)
        # An ETF outside the late-close list has an ambiguous cutoff.
        unlisted_etf = provider.snapshot("ARKK", now=NOW_UTC)
        assert unlisted_etf.options == []
        assert "expiry_unverified_or_unavailable" in unlisted_etf.warnings
        # Unknown underlying type also fails closed.
        assert provider.snapshot("MSFT", now=NOW_UTC).options == []
    finally:
        provider.close()


def test_expired_same_day_contracts_do_not_take_quote_slots(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    after_close = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)  # 17:00 ET
    context = _FakeQuoteContext()
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=after_close, max_contracts=1)
    try:
        snapshot = provider.snapshot("SPY", now=after_close)
        subscribed = {code for name, codes in context.calls if name == "subscribe" for code in codes}
        assert "US.SPY260925C00500000" in subscribed
        assert not any("260918" in code for code in subscribed)
        assert "expired_contracts_excluded" in snapshot.warnings
    finally:
        provider.close()


def test_reconnect_restores_subscriptions_before_retry():
    class SubscriptionCheckingSnapshot(_FakeQuoteContext):
        def get_market_snapshot(self, code_list):
            raise RuntimeError("connection reset by peer")

        def get_stock_quote(self, code_list):
            raise RuntimeError("connection reset by peer")

    replacements = []

    class ReconnectSDK(_FakeSDK):
        @staticmethod
        def OpenQuoteContext(host=None, port=None):
            class NoSnapshot(_FakeQuoteContext):
                get_market_snapshot = None  # forces the subscription-dependent quote path

            replacement = NoSnapshot()
            replacement.books["US.SPY"] = _book(499.9, 500.1)
            replacements.append(replacement)
            return replacement

    provider = providers.MoomooProvider(sdk=ReconnectSDK, context=SubscriptionCheckingSnapshot(),
                                        host="127.0.0.1", now=NOW_UTC)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert snapshot.spot == 500.0 and snapshot.source_verified is True
        assert "US.SPY" in replacements[0].subscriptions["QUOTE"]
    finally:
        provider.close()


def test_chain_cache_zero_disables_caching():
    context = _FakeQuoteContext()
    calls = []
    original = context.get_option_chain
    context.get_option_chain = lambda code, **kwargs: calls.append(code) or original(code, **kwargs)
    clock = [NOW_UTC]
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, clock=lambda: clock[0],
                                        chain_cache_seconds=0, max_contracts=2)
    provider._fetch_chain("US.SPY", None)
    provider._fetch_chain("US.SPY", None)
    assert len(calls) == 2
    cached = providers.MoomooProvider(sdk=_FakeSDK, context=context, clock=lambda: clock[0], max_contracts=2)
    cached._fetch_chain("US.SPY", None)
    cached._fetch_chain("US.SPY", None)
    assert len(calls) == 3
    clock[0] += timedelta(seconds=301)
    cached._fetch_chain("US.SPY", None)
    assert len(calls) == 4


def test_naive_clock_values_are_rejected():
    naive = datetime(2026, 9, 18, 15, 0)
    with pytest.raises(ValueError):
        providers.ReplayProvider(now=naive).snapshot("SPY")
    with pytest.raises(ValueError):
        providers.ReplayProvider().snapshot("SPY", now=naive)
    with pytest.raises(ValueError):
        providers.MoomooProvider(sdk=_FakeSDK, context=_FakeQuoteContext()).snapshot("SPY", now=naive)


def test_row_get_respects_name_priority_and_skips_missing():
    row = {"total_used": 9, "own_used": 2, "a": None, "b": "", "c": 3}
    assert providers._row_get(row, "own_used", "total_used") == 2
    assert providers._row_get(row, "a", "b", "c") == 3
    assert providers._row_get(row, "a") is None


def test_calendar_covers_leaps_expiries():
    from src.core import trading_calendar
    from src.services.trade_desk import analytics

    if not trading_calendar._XCALS_AVAILABLE:
        pytest.skip("exchange-calendars is not installed")
    today = datetime.now(providers.US_EASTERN).date()
    candidate = today + timedelta(days=2 * 365)
    closes = []
    for _ in range(14):
        close = providers._resolve_session_close(candidate)
        if close is not None:
            closes.append(close)
        candidate += timedelta(days=1)
    assert closes and all(close.tzinfo is not None for close in closes)
    assert analytics._exchange_calendar().last_session.date() > today + timedelta(days=3 * 365)


def test_configured_rsa_key_encrypts_the_opend_protocol(monkeypatch, tmp_path) -> None:
    calls = []

    class EncryptingSDK(_FakeSDK):
        class SysConfig:
            @staticmethod
            def set_init_rsa_file(path):
                calls.append(("key", path))

            @staticmethod
            def enable_proto_encrypt(value):
                calls.append(("encrypt", value))

        @staticmethod
        def OpenQuoteContext(host=None, port=None):
            calls.append(("connect", host))
            return _FakeQuoteContext()

    key = tmp_path / "opend_rsa.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
    monkeypatch.setenv("TRADE_DESK_OPEND_RSA_KEY_FILE", str(key))
    provider = providers.MoomooProvider(sdk=EncryptingSDK, host="127.0.0.1",
                                        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    provider._ensure_context()
    assert calls == [("key", str(key)), ("encrypt", True), ("connect", "127.0.0.1")]
    provider.close()

    monkeypatch.setenv("TRADE_DESK_OPEND_RSA_KEY_FILE", str(tmp_path / "missing.pem"))
    provider = providers.MoomooProvider(sdk=EncryptingSDK, host="127.0.0.1",
                                        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    with pytest.raises(providers.ProviderError) as exc_info:
        provider._ensure_context()
    assert exc_info.value.code == "opend_encryption_key_unavailable"
    provider.close()


def test_live_book_replaces_a_snapshot_whose_last_trade_is_old(monkeypatch):
    # OpenD's snapshot update_time is the last trade, not the quote: a contract
    # that has not traded for minutes can still have a live bid/ask.
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_market_snapshot

    def old_option_trades(code_list):
        ret, rows = original(code_list)
        for row in rows:
            if not _is_underlying(row["code"]):
                row["update_time"] = "2026-09-18 14:55:00"  # five minutes before NOW_UTC
        return ret, rows

    monkeypatch.setattr(context, "get_market_snapshot", old_option_trades)
    for code in ("US.SPY260918C00500000", "US.SPY260918P00500000"):
        context.books[code] = _book(4.8, 5.2)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert {o.contract_id for o in snapshot.options} == {"US.SPY260918C00500000", "US.SPY260918P00500000"}
        assert {(o.bid, o.ask) for o in snapshot.options} == {(4.8, 5.2)}
        assert not any(w.startswith("option_quote_stale") for w in snapshot.warnings)
    finally:
        provider.close()


def test_subscribed_book_without_server_time_is_observed_now(monkeypatch):
    # OpenD returns an empty svr_recv_time for a subscribed book that has not
    # changed yet; the live subscription makes it the current book.
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_market_snapshot

    def old_option_trades(code_list):
        ret, rows = original(code_list)
        for row in rows:
            if not _is_underlying(row["code"]):
                row["update_time"] = "2026-09-18 14:55:00"
        return ret, rows

    monkeypatch.setattr(context, "get_market_snapshot", old_option_trades)
    for code in ("US.SPY260918C00500000", "US.SPY260918P00500000"):
        context.books[code] = _book(4.8, 5.2, bid_time="", ask_time="")
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert {(o.bid, o.ask) for o in snapshot.options} == {(4.8, 5.2)}
        assert all(o.quoted_at == NOW_UTC for o in snapshot.options)
        assert "order_book_time_unreported_observed_on_live_subscription" in snapshot.warnings
    finally:
        provider.close()


def test_untracked_book_without_server_time_is_not_freshened():
    # Only this provider's own live subscription can vouch for an untimed book.
    context = _FakeQuoteContext()
    code = "US.SPY260918C00500000"
    context.subscriptions["ORDER_BOOK"] = {code}
    context.books[code] = _book(4.8, 5.2, bid_time="", ask_time="")
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC)
    try:
        assert provider._orderbook_quote(code).timestamp is None
    finally:
        provider.close()


def test_live_snapshot_carries_recent_bars_and_survives_their_failure(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()

    def get_cur_kline(code, num, ktype=None, **kwargs):
        kind = str(ktype)
        if kind not in context.subscriptions or code not in context.subscriptions[kind]:
            return -1, f"Please subscribe to {kind} first"
        if kind == "K_15M":
            return -1, "kline temporarily unavailable"
        return 0, [{"code": code, "time_key": f"2026-09-{day:02d} 00:00:00", "open": 499.0, "high": 502.0,
                    "low": 497.0, "close": 500.0 + day, "volume": 1000} for day in range(1, 19)]

    context.get_cur_kline = get_cur_kline
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert snapshot.options and not snapshot.stale
        assert len(snapshot.bars) == 18 and {bar["interval"] for bar in snapshot.bars} == {"1d"}
        assert snapshot.bars[-1]["close"] == 518.0 and snapshot.bars[-1]["time"].startswith("2026-09-18")
        # 15:00 ET on 2026-09-18: that day's bar is still forming; earlier days are complete.
        assert snapshot.bars[-1]["partial"] is True and snapshot.bars[-2]["partial"] is False
        assert any(w.startswith("bars_unavailable:15m") for w in snapshot.warnings)
    finally:
        provider.close()


def test_request_timeout_is_retried_without_reconnecting(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_option_chain
    attempts = []

    def slow_first_chain(code, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:  # OpenD loading a symbol on first use
            return -1, "PacketErr.Timeout"
        return original(code, **kwargs)

    context.get_option_chain = slow_first_chain
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY", now=NOW_UTC)
        assert snapshot.options and len(attempts) == 2
        assert provider._ctx is context and not context.closed  # no reconnect
    finally:
        provider.close()


def test_chain_request_spans_only_the_expiries_used(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    context.get_option_expiration_date = lambda code: (0, [
        {"strike_time": day} for day in ("2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22",
                                         "2026-09-23", "2026-10-16", "2026-12-18")])
    seen = []
    original = context.get_option_chain
    context.get_option_chain = lambda code, **kwargs: (seen.append(kwargs), original(code, **kwargs))[1]
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, now=NOW_UTC, max_contracts=2, max_expiries=4)
    try:
        provider.snapshot("SPY", now=NOW_UTC)
        assert seen[0]["start"] == "2026-09-18" and seen[0]["end"] == "2026-09-23"
    finally:
        provider.close()


def test_books_read_late_in_a_slow_snapshot_are_not_future_quotes(monkeypatch):
    # Cold symbols make OpenD slow: books are read seconds after the snapshot began.
    monkeypatch.setattr(providers, "_resolve_session_close", _session_close_16)
    context = _FakeQuoteContext()
    original = context.get_market_snapshot

    def old_option_trades(code_list):
        ret, rows = original(code_list)
        for row in rows:
            if not _is_underlying(row["code"]):
                row["update_time"] = "2026-09-18 14:55:00"
        return ret, rows

    monkeypatch.setattr(context, "get_market_snapshot", old_option_trades)
    for code in ("US.SPY260918C00500000", "US.SPY260918P00500000"):
        context.books[code] = _book(4.8, 5.2, bid_time="", ask_time="")
    clock = [NOW_UTC]
    real_book = context.get_order_book

    def slow_book(code, num=10):
        clock[0] += timedelta(seconds=4)
        return real_book(code, num)

    monkeypatch.setattr(context, "get_order_book", slow_book)
    provider = providers.MoomooProvider(sdk=_FakeSDK, context=context, clock=lambda: clock[0], max_contracts=2)
    try:
        snapshot = provider.snapshot("SPY")
        assert {o.contract_id for o in snapshot.options} == {"US.SPY260918C00500000", "US.SPY260918P00500000"}
    finally:
        provider.close()
