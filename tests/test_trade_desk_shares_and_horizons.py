"""Share trades beside option structures, and the four holding horizons."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from src.services.trade_desk import analytics as a
from src.services.trade_desk.models import QuoteSnapshot, TradeAdviceRequest
from tests.test_trade_desk_analytics import _snapshot

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)  # Thursday 11:00 New York, regular session
DAILY = 100.0 * 0.25 / math.sqrt(252) * 1.2  # no bars: 1.2x the implied daily move at 25% IV


def _request(**fields):
    return TradeAdviceRequest(ticker="TEST", **fields)


def _only(candidates, strategy):
    [found] = [item for item in candidates if item.strategy == strategy]
    return found


def test_buying_shares_comes_with_a_volatility_stop_a_2r_target_and_a_time_stop():
    snapshot = _snapshot(NOW)
    share = _only(a.build_candidates(snapshot, _request(strategies=["long_stock"], horizon="swing")), "long_stock")
    assert share.title == "Buy shares (1-6 weeks)" and share.horizon == "swing"
    assert [(leg.right, leg.side, leg.quantity, leg.entry_price) for leg in share.legs] == [("stock", "buy", 100, 100.0)]
    assert share.stop_price == pytest.approx(round(100 - 1.5 * DAILY, 2))
    assert share.target_price == pytest.approx(round(100 + 3.0 * DAILY, 2))
    assert share.hold_until == a._closes_ahead(NOW, 20) and share.hold_until.date() == date(2026, 11, 25)
    assert share.payoff.capital_required == pytest.approx(10_000.0) and share.payoff.loss_bound == "bounded"
    assert share.probability.available and share.probability.horizon_label == "time stop"
    expected = math.log(100 / share.stop_price) / math.log(share.target_price / share.stop_price)
    assert share.probability.assumptions["targetBeforeStop"] == pytest.approx(expected, abs=1e-4)
    assert "implied daily move" in share.levels_basis and "20 sessions" in share.levels_basis
    assert any("by Nov 25" in line for line in share.exit_conditions)


def test_a_short_needs_margin_and_says_so_and_a_day_trade_ends_at_the_close():
    snapshot = _snapshot(NOW)
    short = _only(a.build_candidates(snapshot, _request(strategies=["short_stock"], horizon="interday",
                                                         existing_shares=40)), "short_stock")
    assert short.stop_price > 100 > short.target_price
    assert short.payoff.capital_required == pytest.approx(5_000.0) and short.payoff.loss_bound == "unbounded"
    assert any("borrow" in warning for warning in short.warnings)
    assert any("closes your position" in warning for warning in short.warnings)
    day = _only(a.build_candidates(snapshot, _request(strategies=["long_stock"], horizon="intraday")), "long_stock")
    assert day.hold_until == datetime(2026, 10, 29, 20, 0, tzinfo=UTC)  # today's 16:00 close
    assert day.stop_price == pytest.approx(round(100 - 0.5 * DAILY, 2))
    assert any("pattern-day-trader" in warning for warning in day.warnings)


def test_an_allocation_sizes_the_shares():
    snapshot = _snapshot(NOW)
    sized = _only(a.build_candidates(snapshot, _request(strategies=["long_stock"], allocation=2_550)), "long_stock")
    assert sized.legs[0].quantity == 25 and sized.payoff.capital_required == pytest.approx(2_500.0)
    tiny = _only(a.build_candidates(snapshot, _request(strategies=["long_stock"], allocation=50)), "long_stock")
    assert tiny.legs[0].quantity == 100 and any("below one share" in warning for warning in tiny.warnings)


def test_daily_bars_give_an_atr_stop():
    snapshot = _snapshot(NOW)
    bars = [{"interval": "1d", "time": (NOW - timedelta(days=30 - i)).isoformat(), "open": 100, "high": 102,
             "low": 98, "close": 100.0, "volume": 1, "partial": False} for i in range(20)]
    snapshot = snapshot.model_copy(update={"bars": bars})
    share = _only(a.build_candidates(snapshot, _request(strategies=["long_stock"], horizon="swing")), "long_stock")
    assert share.stop_price == pytest.approx(100 - 1.5 * 4.0) and "ATR(14)" in share.levels_basis


def test_shares_need_no_option_chain_and_auto_mixes_horizons():
    snapshot = _snapshot(NOW)
    candidates = a.build_candidates(snapshot, _request())  # auto view, auto instrument, auto horizon
    assert [(item.strategy, item.horizon) for item in candidates] == [
        ("long_stock", "swing"), ("short_stock", "swing"), ("bull_call_debit", "intraday"), ("bear_put_debit", "swing")]
    no_chain = snapshot.model_copy(update={"options": []})
    assert [item.strategy for item in a.build_candidates(no_chain, _request())] == ["long_stock", "short_stock"]
    options_only = a.build_candidates(snapshot, _request(instrument="options", direction="bullish"))
    assert all(item.strategy not in a.SHARE_STRATEGIES for item in options_only)


def test_option_expiries_follow_the_horizon():
    snapshot = _snapshot(NOW)  # quotes expire in 2 and 35 days
    swing = a.build_candidates(snapshot, _request(strategies=["long_call"], horizon="swing"))[0]
    interday = a.build_candidates(snapshot, _request(strategies=["long_call"], horizon="interday"))[0]
    position = a.build_candidates(snapshot, _request(strategies=["long_call"], horizon="position"))[0]
    assert (swing.legs[0].expiry - NOW).days == 35 and swing.title == "Long call (1-6 weeks)"
    assert (interday.legs[0].expiry - NOW).days == 2 and interday.title == "Long call (1-5 days)"
    assert (position.legs[0].expiry - NOW).days == 35  # nothing in 45-200 days: the closest to 90
    today = date(2026, 10, 29)
    fridays = [today + timedelta(days=1 + 7 * week) for week in range(30)]
    assert a.target_expiries("swing", fridays, today) == [date(2026, 11, 27)]  # 29 days: closest to 30
    assert a.target_expiries("position", fridays, today) == [date(2027, 1, 29)]  # 92 days: closest to 90
    assert a.target_expiries("auto", fridays, today) == [date(2026, 10, 30), date(2026, 11, 6), date(2026, 11, 27)]
    assert a.target_expiries("interday", fridays, today) == [date(2026, 10, 30), date(2026, 11, 6)]


def test_a_repriced_share_trade_keeps_its_levels():
    snapshot = _snapshot(NOW)
    share = _only(a.build_candidates(snapshot, _request(strategies=["long_stock"])), "long_stock")
    later = snapshot.model_copy(update={"spot": 101.0, "quoted_at": NOW + timedelta(minutes=30)})
    repriced = a.reprice_candidate(share, later, _request(strategies=["long_stock"]))
    assert repriced.id == share.id and repriced.legs[0].entry_price == 101.0
    assert (repriced.stop_price, repriced.target_price, repriced.hold_until) == (share.stop_price, share.target_price,
                                                                                 share.hold_until)


def test_old_requests_and_plans_still_read():
    assert _request(horizon="both").horizon == "auto"
    close = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    assert a.horizon_for_expiry(close, NOW, close) == "intraday"
    assert a.horizon_for_expiry(NOW + timedelta(days=7), NOW, close) == "interday"
    assert a.horizon_for_expiry(NOW + timedelta(days=30), NOW, close) == "swing"
    assert a.horizon_for_expiry(NOW + timedelta(days=120), NOW, close) == "position"
    assert isinstance(_snapshot(NOW), QuoteSnapshot)


def test_the_service_asks_for_a_longer_expiry_only_when_options_need_one(monkeypatch):
    from src.services.trade_desk import service as service_module
    from src.services.trade_desk.service import TradeDeskService
    monkeypatch.setattr(service_module, "utcnow", lambda: NOW)
    today = date(2026, 10, 29)

    class Provider:
        def expiration_dates(self, ticker):
            return [today + timedelta(days=1 + 7 * week) for week in range(30)]
    pick = TradeDeskService._horizon_expiries
    assert pick(Provider(), _request(horizon="position")) == [date(2027, 1, 29)]
    assert pick(Provider(), _request(horizon="auto")) == [date(2026, 11, 27)]  # the nearest two come anyway
    assert pick(Provider(), _request(horizon="interday")) == []
    assert pick(Provider(), _request(horizon="swing", instrument="stock")) == []  # shares need no chain
    assert pick(Provider(), _request(horizon="swing", expiry=date(2026, 11, 20))) == []  # your expiry wins
    assert pick(object(), _request(horizon="swing")) == []  # a provider without an expiry list


@pytest.mark.parametrize("question,horizon,instrument", [
    ("Day trade idea for this open?", "intraday", "auto"),
    ("Hold over the weekend?", "interday", "auto"),
    ("A swing long with shares?", "swing", "stock"),
    ("Long-term LEAPS call?", "position", "options"),
    ("intraday or interday put CRCL", "auto", "auto"),  # two horizons named; a bare "put" is not enough
    ("What do you think about this drop?", "auto", "auto"),
])
def test_your_question_can_set_the_horizon_and_what_to_trade(question, horizon, instrument):
    from src.services.trade_desk.service import infer_from_question
    inferred = infer_from_question(_request(message=question))
    assert (inferred.horizon, inferred.instrument) == (horizon, instrument)
    chosen = infer_from_question(_request(message="Day trade with shares", horizon="swing", instrument="options"))
    assert (chosen.horizon, chosen.instrument) == ("swing", "options")  # what you chose on the form wins
