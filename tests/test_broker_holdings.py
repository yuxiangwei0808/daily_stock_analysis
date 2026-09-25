"""Broker holdings (read only): grouping, P&L, Discord-safe wording, default alerts and your own rules."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.services.trade_desk import holdings as h
from src.services.trade_desk import opportunities as opp
from src.services.trade_desk.repository import TradeDeskRepository

TODAY = date(2026, 9, 25)  # a Friday
MIDDAY = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)  # 12:30 New York

RAW = {"account": "…1234", "account_type": "MARGIN", "total_assets": 10_000.0, "cash": 1_000.0, "positions": [
    {"code": "US.USO261016C160000", "name": "USO 261016 160.00C", "qty": 3.0, "side": "LONG",
     "average_cost": 4.35, "price": 3.4},
    {"code": "US.USO261016C170000", "name": "USO 261016 170.00C", "qty": -3.0, "side": "SHORT",
     "average_cost": 2.16, "price": 1.6},
    {"code": "US.NVDA", "name": "NVIDIA", "qty": 5.0, "side": "LONG", "average_cost": 120.0, "price": 200.0},
    {"code": "US.SOXS", "name": "Direxion Daily Semiconductor Bear 3x Shares ETF", "qty": 10.0, "side": "LONG",
     "average_cost": 50.0, "price": 30.0},
]}
QUOTES = {"USO261016C160000": {"price": 3.4, "bid": 3.3, "ask": 3.5},
          "USO261016C170000": {"price": 1.6, "bid": 1.55, "ask": 1.65},
          "USO": {"price": 150.0}, "NVDA": {"price": 200.0}, "SOXS": {"price": 30.0}}


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


class FakeProvider:
    def __init__(self, raw=RAW, quotes=QUOTES):
        self.raw, self.quotes, self.synced = raw, dict(quotes), 0

    def broker_positions(self, account, security_firm="FUTUINC"):
        self.synced += 1
        assert account == "1234" and security_firm == "FUTUINC"
        return {**self.raw, "positions": [dict(row) for row in self.raw["positions"]]}

    def watchlist_quotes(self, codes):
        return {code: self.quotes[code] for code in codes if code in self.quotes}


@pytest.fixture
def store(repo, monkeypatch):
    monkeypatch.setenv("TRADE_DESK_BROKER_ACCOUNT", "1234")
    provider = FakeProvider()
    service = SimpleNamespace(repo=repo, provider=lambda mode: provider)
    holdings = h.Holdings(service)
    holdings.sync()
    return holdings


def test_option_codes_and_trading_days():
    assert h.parse_code("US.USO261016C170000") == {"kind": "option", "ticker": "USO261016C170000", "underlying": "USO",
                                                   "expiry": date(2026, 10, 16), "right": "call", "strike": 170.0}
    assert h.parse_code("US.BRK.B") == {"kind": "stock", "ticker": "BRK.B"}
    assert h.parse_code("SPY261016P650500")["strike"] == 650.5
    assert h.trading_days_until(date(2026, 10, 16), TODAY) == 15
    assert h.trading_days_until(date(2026, 9, 28), TODAY) == 1  # over the weekend
    assert h.trading_days_until(TODAY, TODAY) == 0


def test_a_vertical_spread_is_one_position_marked_at_mid():
    view = h.build_view(RAW, QUOTES, TODAY)
    [spread] = view["options"]
    assert spread["key"] == "USO 2026-10-16" and spread["label"] == "160/170C spread" and spread["days_left"] == 15
    assert spread["cost"] == pytest.approx(3 * 4.35 * 100 - 3 * 2.16 * 100)
    assert spread["value"] == pytest.approx(3 * 3.4 * 100 - 3 * 1.6 * 100)
    assert spread["max_value"] == pytest.approx(3000)
    assert spread["pnl_pct"] == pytest.approx((540 - 657) / 657 * 100)
    nvda = next(row for row in view["stocks"] if row["ticker"] == "NVDA")
    assert nvda["weight_pct"] == pytest.approx(10.0) and nvda["pnl_pct"] == pytest.approx(66.67, abs=0.01)


def test_discord_wording_carries_percentages_only():
    view = h.build_view(RAW, QUOTES, TODAY)
    option_text = h.describe_option(view["options"][0])
    assert option_text == "USO 10/16 160/170C spread · -17.8% on cost · 15 trading days left"
    stock_text = h.describe_stock(view["stocks"][0])
    assert stock_text == "NVDA shares · 10.0% of account · +66.7% vs avg cost"
    for text in (option_text, stock_text):
        assert "$" not in text and " 5 " not in text and "120" not in text


@pytest.mark.parametrize("text,kind,value", [
    ("set a stop loss for this option, if the underlying stock price drop to 145", "price_below", 145),
    ("warn me 3 days before expiration", "days_to_expiry", 3),
    ("warn me at expire of 2 days", "days_to_expiry", 2),
    ("alert if I lose 40%", "pnl_below", -40),
    ("take profit at +60%", "pnl_above", 60),
    ("tell me when it goes above 155.5", "price_above", 155.5),
])
def test_rule_text_is_read_into_a_draft(text, kind, value):
    draft = h.parse_rule_text(text)
    assert draft["kind"] == kind and draft["value"] == value


def test_rules_are_validated_and_managed(store):
    with pytest.raises(ValueError, match="option position"):
        store.add_rule({"position_key": "NVDA", "kind": "days_to_expiry", "value": 2})
    with pytest.raises(ValueError, match="no longer held"):
        store.add_rule({"position_key": "AAPL", "kind": "price_below", "value": 2})
    with pytest.raises(ValueError, match="apply to a position"):
        store.add_rule({"ticker": "SPY", "kind": "pnl_below", "value": -10})
    rule = store.add_rule({"position_key": "USO 2026-10-16", "kind": "price_below", "value": 145, "note": "stop"})
    assert rule["ticker"] == "USO" and rule["status"] == "active" and rule["position_label"].startswith("USO 10/16")
    store.update_rule(rule["id"], {"status": "paused", "value": 144})
    assert store.rules()[0]["status"] == "paused" and store.rules()[0]["value"] == 144
    store.delete_rule(rule["id"])
    assert store.rules() == []
    with pytest.raises(KeyError):
        store.delete_rule(rule["id"])


def _monitor(store, clock=None):
    events = []
    keys = set()

    def emit(event_type, payload, key):
        if key in keys:
            return None
        keys.add(key)
        events.append((event_type, payload, key))
        return {"id": len(events)}

    monitor = h.HoldingsMonitor(store, emit, bars=lambda tickers: {}, earnings_date=lambda ticker, day: None,
                                clock=clock or (lambda: 0.0))
    return monitor, events


def test_your_rules_fire_once_or_daily(store):
    once = store.add_rule({"position_key": "USO 2026-10-16", "kind": "price_below", "value": 151, "note": "close the spread"})
    store.add_rule({"ticker": "SPY", "kind": "price_above", "value": 600, "repeat": "daily"})
    store.add_rule({"position_key": "USO 2026-10-16", "kind": "days_to_expiry", "value": 20})
    store.add_rule({"position_key": "NVDA", "kind": "pnl_above", "value": 80})  # +66.7%: not yet
    store.service.provider("live").quotes["SPY"] = {"price": 610.0}
    monitor, events = _monitor(store)
    monitor.check(MIDDAY)
    messages = [payload["message"] for _, payload, _ in events if payload["kind"] == "rule"]
    assert len(messages) == 3
    assert messages[0].startswith("Your alert: USO price at or below 151 — now 150.00.\n"
                                  "Position: USO 10/16 160/170C spread")
    assert "Note: close the spread" in messages[0] and "nothing is traded automatically" in messages[0]
    assert any("SPY price at or above 600 — now 610.00" in text for text in messages)
    assert any("15 trading days left" in text for text in messages)
    statuses = {rule["id"]: rule["status"] for rule in store.rules()}
    assert statuses[once["id"]] == "triggered"
    monitor.check(MIDDAY + timedelta(minutes=1))
    assert len([e for e in events if e[1]["kind"] == "rule"]) == 3  # once rules done; the daily one waits for tomorrow


def test_short_term_option_defaults(store, monkeypatch):
    raw = store.raw()
    for row in raw["positions"]:
        row["code"] = row["code"].replace("261016", "260929")  # expires Tuesday: 2 trading days
    store.repo.set_setting("broker_holdings", raw)
    store.service.provider("live").quotes.update({
        "USO260929C160000": {"price": 10.0, "bid": 9.9, "ask": 10.1},
        "USO260929C170000": {"price": 1.0, "bid": 0.9, "ask": 1.1},
        "USO": {"price": 171.0}})
    monitor, events = _monitor(store)
    monitor.check(MIDDAY)
    kinds = {payload["kind"]: payload["message"] for _, payload, _ in events}
    assert kinds["expiry"].startswith("Expires in 2 trading days: USO 09/29 160/170C spread")
    assert kinds["assignment"].startswith("Assignment risk: short 170C is in the money (USO 171.00)")
    assert kinds["profit"].startswith("Profit target:") and "of max profit" in kinds["profit"]
    early = datetime(2026, 9, 25, 13, 40, tzinfo=timezone.utc)  # 09:40 New York: before the morning warnings
    monitor2, events2 = _monitor(store)
    monitor2.check(early)
    assert "expiry" not in {payload["kind"] for _, payload, _ in events2}


def test_expiration_day_alerts_at_the_open_and_the_last_hour(store):
    raw = store.raw()
    for row in raw["positions"]:
        row["code"] = row["code"].replace("261016", "260925")
    store.repo.set_setting("broker_holdings", raw)
    monitor, events = _monitor(store)
    monitor.check(MIDDAY)
    monitor.check(datetime(2026, 9, 25, 19, 5, tzinfo=timezone.utc))  # 15:05 New York
    expiry = [payload["message"] for _, payload, _ in events if payload["kind"] == "expiry"]
    assert expiry[0].startswith("Expires today:") and expiry[1].startswith("One hour to the close")


def test_stock_trend_breaks_and_earnings(store):
    monitor, events = _monitor(store)
    monitor._earnings_date = lambda ticker, day: date(2026, 9, 30) if ticker == "NVDA" else date(2026, 9, 28)
    monitor._levels = {"NVDA": {"low20": 205.0, "ma50": 199.0, "last_close": 210.0, "high20": 230, "atr": 5,
                                "avg_volume": 1}}
    monitor._levels_day = TODAY
    monitor.check(MIDDAY)
    messages = [payload["message"] for _, payload, _ in events]
    assert any(text.startswith("Broke below its 20-day low 205.00 at 200.00: NVDA shares") for text in messages)
    assert not any("50-day" in text for text in messages)  # 200 is still above MA50 199
    assert any(text.startswith("⚠️ Earnings Sep 30 (in 5 days)") for text in messages)
    assert not any("SOXS" in text and "Earnings" in text for text in messages)  # funds have no earnings


def test_sync_every_ten_minutes_in_session_and_once_after_the_close(store):
    provider = store.service.provider("live")
    provider.synced = 0
    tick = {"clock": 0.0}
    monitor, _ = _monitor(store, clock=lambda: tick["clock"])
    monitor.tick(MIDDAY, "regular")
    monitor._tasks["sync"].result(timeout=5)
    tick["clock"] += 60
    monitor.tick(MIDDAY, "regular")
    assert provider.synced == 1
    tick["clock"] += h.SYNC_SECONDS
    monitor.tick(MIDDAY, "regular")
    monitor._tasks["sync"].result(timeout=5)
    assert provider.synced == 2
    after = datetime(2026, 9, 25, 20, 10, tzinfo=timezone.utc)  # 16:10 New York
    monitor.tick(after, "postmarket")
    monitor._tasks["sync"].result(timeout=5)
    monitor.tick(after + timedelta(minutes=5), "postmarket")
    assert provider.synced == 3
    monitor.stop()


def test_ideas_and_breakouts_speak_to_what_you_hold(store):
    long_held = {"ticker": "NVDA", "direction": "long", "conviction": "high", "held_note": store.note("NVDA")}
    assert store.note("NVDA") == "You hold: NVDA shares · 10.0% of account · +66.7% vs avg cost"
    assert opp.expressions(long_held, options_follow=False)[0] == "already held — hold, or add small"
    short_held = {"ticker": "NVDA", "direction": "short", "conviction": "medium", "held_note": "You hold: …"}
    assert opp.expressions(short_held, options_follow=False) == ["sell or trim your position"]
    short_not_held = {"ticker": "XOM", "direction": "short", "conviction": "medium", "held_note": ""}
    assert opp.expressions(short_not_held, options_follow=False)[0].startswith("short shares")
    geared = {"ticker": "SOXS", "name": "Direxion Daily Semiconductor Bear 3x Shares ETF", "direction": "short",
              "conviction": "medium", "held_note": "You hold: SOXS"}
    assert opp.expressions(geared, options_follow=False) == [
        "sell or trim your position (leveraged/inverse ETF: shorting it is not advised)"]
    assert store.note("USO").startswith("You hold: USO 10/16 160/170C spread")
    assert store.tickers() == ["USO", "NVDA", "SOXS"]


def test_api_lists_holdings_and_manages_rules(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints.trade_desk import router
    service = store.service
    service.holdings = store
    service.worker = None
    app = FastAPI()
    app.state.trade_desk_service = service
    app.include_router(router, prefix="/api/v1/trade-desk")
    with TestClient(app) as client:
        body = client.get("/api/v1/trade-desk/holdings").json()
        assert body["enabled"] and body["view"]["options"][0]["key"] == "USO 2026-10-16"
        draft = client.post("/api/v1/trade-desk/holdings/rules/parse",
                            json={"text": "stop if USO drops to 145", "position_key": "USO 2026-10-16"}).json()
        assert draft == {"kind": "price_below", "value": 145.0, "note": "stop if USO drops to 145",
                         "position_key": "USO 2026-10-16", "source": "pattern"}
        created = client.post("/api/v1/trade-desk/holdings/rules", json={
            "position_key": "USO 2026-10-16", "kind": "price_below", "value": 145}).json()
        assert client.patch(f"/api/v1/trade-desk/holdings/rules/{created['id']}",
                            json={"status": "paused"}).json()["status"] == "paused"
        assert client.post("/api/v1/trade-desk/holdings/rules", json={
            "position_key": "NVDA", "kind": "days_to_expiry", "value": 2}).status_code == 422
        assert client.delete(f"/api/v1/trade-desk/holdings/rules/{created['id']}").status_code == 204
        assert client.delete(f"/api/v1/trade-desk/holdings/rules/{created['id']}").status_code == 404
        monkeypatch.delenv("TRADE_DESK_BROKER_ACCOUNT")
        assert client.get("/api/v1/trade-desk/holdings").json()["enabled"] is False
        assert client.post("/api/v1/trade-desk/holdings/refresh").status_code == 409


def _bars_from(returns, start=100.0):
    rows, price = [], start
    for i, r in enumerate(returns):
        price *= 1 + r
        rows.append({"date": (date(2026, 6, 1) + timedelta(days=i)).isoformat(), "close": price})
    return rows


def test_portfolio_summary_exposure_hedges_and_wording():
    from src.services.trade_desk.portfolio import build_summary, format_summary
    import random
    rng = random.Random(7)
    spy = [rng.gauss(0, 0.01) for _ in range(70)]
    raw = {**RAW, "positions": [{**row, "today_pl": -100.0 if "USO" in row["code"] else 20.0} for row in RAW["positions"]]}
    view = h.build_view(raw, {**QUOTES, "NVDA": {"price": 200.0, "prev_close": 190.0}}, TODAY)
    bars = {"SPY": _bars_from(spy), "NVDA": _bars_from([2 * r for r in spy]), "SOXS": _bars_from([-3 * r for r in spy])}
    summary = build_summary(view, raw, bars, [{"status": "active"}, {"status": "triggered",
                            "triggered_at": "2026-09-26T00:30:00+00:00"}], TODAY,
                            earnings_date=lambda ticker, day: date(2026, 9, 29) if ticker == "NVDA" else None)
    assert summary["day_pct"] == pytest.approx((-200 + 40) / (10_000 + 160) * 100)
    assert summary["betas"] == {"NVDA": pytest.approx(2.0), "SOXS": pytest.approx(-3.0)}
    assert summary["beta_exposure"] == pytest.approx(0.10 * 2 - 0.03 * 3)
    assert summary["hedges"] == [{"ticker": "SOXS", "contribution": -0.09}]
    assert summary["offsets"] == [{"a": "NVDA", "b": "SOXS", "corr": -1.0}]
    assert summary["geared"] == ["SOXS"] and summary["alerts_triggered_today"] == 1  # 20:30 New York is still today
    text = format_summary(summary)
    assert text.startswith("📊 **Portfolio** · Fri Sep 25\nAccount -1.6% today")
    assert "a 1% SPY move ≈ +0.1% on the account" in text
    assert "• USO 10/16 160/170C spread 5.4% of account" in text and "NVDA 10.0% of account · +66.7% on cost · +5.3% today" in text
    assert "Hedging the book: SOXS -9% of a 1% SPY move" in text
    assert "NVDA and SOXS offset each other" in text and "NVDA earnings Sep 29 (in 4 days)" in text
    assert "$" not in text


def test_summary_is_sent_once_after_the_close_sync(store, monkeypatch):
    provider = store.service.provider("live")
    tick = {"clock": 0.0}
    monitor, events = _monitor(store, clock=lambda: tick["clock"])
    at = datetime(2026, 9, 25, 20, 16, tzinfo=timezone.utc)  # 16:16 New York
    monitor.tick(at, "postmarket")  # the post-close sync first
    monitor._tasks["sync"].result(timeout=5)
    monitor.tick(at, "postmarket")
    monitor._tasks["summary"].result(timeout=5)
    monitor.tick(at + timedelta(minutes=1), "postmarket")
    [(event_type, payload, key)] = [e for e in events if e[0] == "portfolio_summary"]
    assert key == "portfolio:2026-09-25" and payload["message"].startswith("📊 **Portfolio**")
    assert store.last_summary()["date"] == "2026-09-25" and provider.synced >= 2
    restarted, events2 = _monitor(store)
    restarted._emit = monitor._emit  # same dedup store: a restart does not send it twice
    restarted._after_close_day = date(2026, 9, 25)
    restarted._tasks["sync"] = monitor._tasks["sync"]
    restarted.tick(at + timedelta(minutes=5), "postmarket")
    restarted._tasks["summary"].result(timeout=5)
    assert len([e for e in events if e[0] == "portfolio_summary"]) == 1
    monitor.stop()
    restarted.stop()
