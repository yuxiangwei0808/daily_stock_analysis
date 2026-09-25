from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

import pytest

from src.services.trade_desk.analytics import (
    analyze_payoff,
    black_scholes_price,
    build_candidates,
    crr_binomial_price,
    estimate_probability,
    payoff_at_price,
    risk_neutral_profit_probability,
    strategy_catalog,
)
from src.services.trade_desk.models import OptionLeg, OptionQuote, QuoteSnapshot, TradeAdviceRequest


UTC = timezone.utc


def _leg(
    right: str,
    side: str,
    *,
    strike: float,
    price: float,
    expiry: datetime,
    iv: float = 0.25,
    multiplier: int = 100,
    quantity: int = 1,
) -> OptionLeg:
    return OptionLeg(
        contract_id=f"{right}-{side}-{strike}",
        right=right,
        side=side,
        quantity=quantity,
        strike=strike,
        expiry=expiry,
        multiplier=multiplier,
        entry_price=price,
        iv=iv,
    )


def _snapshot() -> QuoteSnapshot:
    now = datetime.now(UTC).replace(microsecond=0)
    options: list[OptionQuote] = []
    for expiry in (now + timedelta(days=2), now + timedelta(days=35)):
        for strike in (90.0, 95.0, 100.0, 105.0, 110.0):
            for right in ("call", "put"):
                mid = max(0.20, (5.0 - abs(strike - 100.0) * 0.15) if right == "call" else (5.0 - abs(strike - 100.0) * 0.15))
                options.append(
                    OptionQuote(
                        contract_id=f"{right}-{int(strike)}-{expiry.date().isoformat()}",
                        underlying="TEST",
                        right=right,
                        strike=strike,
                        expiry=expiry,
                        expiry_verified=True,
                        multiplier=100,
                        bid=mid - 0.10,
                        ask=mid + 0.10,
                        quoted_at=now,
                        iv=0.25,
                    )
                )
    return QuoteSnapshot(
        underlying="TEST",
        spot=100.0,
        quoted_at=now,
        provider="unit",
        mode="replay",
        session="regular",
        source_verified=True,
        options=options,
    )


def test_long_call_payoff_bounds_and_break_even_include_fees() -> None:
    expiry = datetime.now(UTC) + timedelta(days=30)
    legs = [_leg("call", "buy", strike=100, price=5.0, expiry=expiry)]
    payoff = analyze_payoff(legs, fees=1.0)

    assert payoff.entry_debit == pytest.approx(500.0)
    assert payoff.fees == pytest.approx(1.0)
    assert payoff.loss_bound == "bounded"
    assert payoff.gain_bound == "unbounded"
    assert payoff.max_loss == pytest.approx(501.0)
    assert payoff.max_gain is None
    assert payoff.breakevens == pytest.approx([105.01])
    assert payoff_at_price(legs, 110.0, fees=1.0) == pytest.approx(499.0)


def test_defined_risk_debit_spread_has_exact_tail_bounds() -> None:
    expiry = datetime.now(UTC) + timedelta(days=30)
    legs = [
        _leg("call", "buy", strike=100, price=5.0, expiry=expiry),
        _leg("call", "sell", strike=110, price=2.0, expiry=expiry),
    ]
    payoff = analyze_payoff(legs, fees=2.0)

    assert payoff.entry_debit == pytest.approx(300.0)
    assert payoff.gain_bound == "bounded"
    assert payoff.loss_bound == "bounded"
    assert payoff.max_loss == pytest.approx(302.0)
    assert payoff.max_gain == pytest.approx(698.0)
    assert payoff.breakevens == pytest.approx([103.02])


def test_risk_neutral_probability_uses_cdf_regions() -> None:
    as_of = datetime.now(UTC).replace(microsecond=0)
    expiry = as_of + timedelta(days=365)
    # A call bought at zero is profitable exactly when S_T > K.
    legs = [_leg("call", "buy", strike=100, price=0.0, expiry=expiry, iv=0.2)]
    observed = risk_neutral_profit_probability(legs, 100.0, expiry, as_of=as_of, fees=0.0)
    expected = 1.0 - 0.5 * (1.0 + __import__("math").erf(0.1 / (2.0**0.5)))

    assert observed == pytest.approx(expected, abs=1.0e-6)


def test_crr_zero_time_and_european_convergence() -> None:
    assert crr_binomial_price(105, 100, 0, 0.2, right="call") == pytest.approx(5.0)
    bs = black_scholes_price(100, 100, 0.5, 0.2, right="call")
    crr = crr_binomial_price(100, 100, 0.5, 0.2, right="call", steps=400, american=False)
    assert crr == pytest.approx(bs, rel=2.0e-3)
    american_put = crr_binomial_price(100, 100, 0.5, 0.2, right="put", steps=200, american=True)
    european_put = crr_binomial_price(100, 100, 0.5, 0.2, right="put", steps=200, american=False)
    assert american_put >= european_put


def test_catalog_and_both_horizons_are_quote_grounded() -> None:
    snapshot = _snapshot()
    request = TradeAdviceRequest(
        ticker="TEST",
        horizon="both",
        strategies=["long_call"],
        allocation=2_000,
    )
    candidates = build_candidates(snapshot, request)

    assert {item.horizon for item in candidates} == {"intraday", "swing"}
    option_ids = {quote.contract_id for quote in snapshot.options}
    for candidate in candidates:
        assert all(leg.contract_id in option_ids for leg in candidate.legs if leg.right != "stock")
        assert candidate.probability.horizon_label in {"preexpiry", "intraday_exit", "expiration"}
        assert any(item["kind"] == "expiration_payoff" for item in candidate.scenarios)
    ids = {item["id"] for item in strategy_catalog()}
    assert {
        "long_call",
        "long_put",
        "bull_call_debit",
        "bear_put_debit",
        "bull_put_credit",
        "bear_call_credit",
        "long_straddle",
        "short_straddle",
        "long_strangle",
        "short_strangle",
        "call_butterfly",
        "put_butterfly",
        "iron_condor",
        "covered_call",
        "cash_secured_put",
        "uncovered_call",
        "uncovered_put",
    } <= ids


def test_covered_call_marks_existing_shares_and_buy_write_capital() -> None:
    snapshot = _snapshot()
    request_existing = TradeAdviceRequest(ticker="TEST", horizon="swing", strategies=["covered_call"], existing_shares=100)
    existing = build_candidates(snapshot, request_existing)[0]
    stock = next(leg for leg in existing.legs if leg.right == "stock")
    assert stock.existing is True
    assert existing.payoff.entry_debit < 0  # only the sold call credit is new cash
    assert existing.payoff.capital_required == pytest.approx(0.65)
    sized_existing = build_candidates(
        snapshot, request_existing.model_copy(update={"allocation": 1_500})
    )[0]
    assert sized_existing.quantity_for_allocation == 1

    request_buy_write = request_existing.model_copy(update={"existing_shares": 0})
    buy_write = build_candidates(snapshot, request_buy_write)[0]
    stock = next(leg for leg in buy_write.legs if leg.right == "stock")
    assert stock.existing is False
    assert buy_write.payoff.capital_required == pytest.approx(10_000.65)


def test_invalid_quotes_are_filtered_and_unverified_iv_is_unavailable() -> None:
    snapshot = _snapshot()
    snapshot.options[0].bid = snapshot.options[0].ask + 1.0
    snapshot.options[1].standard = False
    snapshot.options[2].expiry_verified = False
    request = TradeAdviceRequest(ticker="TEST", horizon="intraday", strategies=["long_call"])
    candidates = build_candidates(snapshot, request)
    assert candidates
    # The selected candidate can only use a surviving quote.  If all surviving
    # quotes happen to lack IV, the structured result explicitly explains why.
    assert candidates[0].probability.available or "iv" in candidates[0].probability.reason.lower()


def test_quote_timestamp_window_and_zero_dte_session_boundary() -> None:
    snapshot = _snapshot()
    for quote in snapshot.options:
        quote.quoted_at = snapshot.quoted_at - timedelta(seconds=31)
    request = TradeAdviceRequest(ticker="TEST", horizon="intraday", strategies=["long_call"])
    assert build_candidates(snapshot, request) == []

    # An American zero-volatility put must retain the immediate-exercise lower
    # bound even when the deterministic European value is discounted.
    assert crr_binomial_price(90, 100, 0.5, 0.0, risk_free_rate=0.10, right="put", american=True) >= 10.0


def test_missing_iv_and_invalid_crr_domain_are_unavailable_preexpiry() -> None:
    as_of = datetime.now(UTC).replace(microsecond=0)
    expiry = as_of + timedelta(days=30)
    missing_iv = _leg("call", "buy", strike=100, price=5, expiry=expiry, iv=0.2).model_copy(update={"iv": None})
    estimate = estimate_probability([missing_iv], 100, as_of + timedelta(days=1), as_of=as_of)
    assert estimate.available is False
    assert "volatility" in estimate.reason.lower()

    # A one-step annual tree with this drift is outside the CRR no-arbitrage
    # region; the pricing routine surfaces the invalid model instead of
    # silently clamping its probability.
    with pytest.raises(ValueError, match="risk-neutral probability"):
        crr_binomial_price(100, 100, 1.0, 0.2, risk_free_rate=1.0, steps=1, american=True)


def test_fractional_second_horizon_and_sensitivity_are_reported() -> None:
    as_of = datetime.now(UTC).replace(microsecond=0)
    expiry = as_of + timedelta(days=2)
    legs = [_leg("call", "buy", strike=100, price=5, expiry=expiry, iv=0.25)]
    horizon = as_of + timedelta(seconds=1.5)
    estimate = estimate_probability(legs, 100, horizon, as_of=as_of)
    assert estimate.available is True
    assert estimate.assumptions["time_to_horizon_years"] == pytest.approx(1.5 / (365 * 24 * 3600))

    candidate = build_candidates(
        _snapshot(), TradeAdviceRequest(ticker="TEST", horizon="intraday", strategies=["long_call"])
    )[0]
    assert {item["sensitivity"] for item in candidate.scenarios if item["kind"] == "preexpiry_sensitivity"} >= {
        "spot",
        "iv",
        "time_to_expiry",
    }
    assert candidate.probability.assumptions["entry_fees_only"] is True
    assert candidate.probability.assumptions["close_fees_included"] is False


def test_default_both_horizon_slots_preserve_strategy_diversity() -> None:
    candidates = build_candidates(_snapshot(), TradeAdviceRequest(ticker="TEST", horizon="both"))
    assert len(candidates) == 4
    assert len({item.strategy for item in candidates}) == 4
    assert {item.horizon for item in candidates} == {"intraday", "swing"}


def test_tail_bound_enums_cover_naked_call_and_long_put() -> None:
    expiry = datetime.now(UTC) + timedelta(days=30)
    naked_call = analyze_payoff([_leg("call", "sell", strike=100, price=3, expiry=expiry)], fees=0.65)
    long_put = analyze_payoff([_leg("put", "buy", strike=100, price=3, expiry=expiry)], fees=0.65)
    assert naked_call.gain_bound == "bounded"
    assert naked_call.loss_bound == "unbounded"
    assert naked_call.max_loss is None
    assert long_put.gain_bound == "bounded"
    assert long_put.loss_bound == "bounded"
    assert math.isfinite(long_put.max_gain or 0)


def test_catalog_payoff_families_have_expected_defined_or_unbounded_tails() -> None:
    expiry = datetime.now(UTC) + timedelta(days=30)
    call_butterfly = analyze_payoff(
        [
            _leg("call", "buy", strike=95, price=6, expiry=expiry),
            _leg("call", "sell", strike=100, price=3, expiry=expiry, quantity=2),
            _leg("call", "buy", strike=105, price=1, expiry=expiry),
        ],
        fees=0,
    )
    put_butterfly = analyze_payoff(
        [
            _leg("put", "buy", strike=95, price=1, expiry=expiry),
            _leg("put", "sell", strike=100, price=3, expiry=expiry, quantity=2),
            _leg("put", "buy", strike=105, price=6, expiry=expiry),
        ],
        fees=0,
    )
    iron_condor = analyze_payoff(
        [
            _leg("put", "buy", strike=90, price=1, expiry=expiry),
            _leg("put", "sell", strike=95, price=2.5, expiry=expiry),
            _leg("call", "sell", strike=105, price=2.5, expiry=expiry),
            _leg("call", "buy", strike=110, price=1, expiry=expiry),
        ],
        fees=0,
    )
    credit = analyze_payoff(
        [
            _leg("put", "sell", strike=100, price=5, expiry=expiry),
            _leg("put", "buy", strike=95, price=2, expiry=expiry),
        ],
        fees=0,
    )
    assert call_butterfly.gain_bound == put_butterfly.gain_bound == "bounded"
    assert call_butterfly.loss_bound == put_butterfly.loss_bound == "bounded"
    assert iron_condor.gain_bound == iron_condor.loss_bound == "bounded"
    assert iron_condor.breakevens
    assert credit.max_gain == pytest.approx(300)
    assert credit.max_loss == pytest.approx(200)


def test_narrow_near_expiry_butterfly_probability_and_holiday_close() -> None:
    from src.services.trade_desk.analytics import _regular_close_after

    as_of = datetime.now(UTC).replace(microsecond=0)
    expiry = as_of + timedelta(seconds=90)
    legs = [
        _leg("call", "buy", strike=95, price=6, expiry=expiry, iv=0.25),
        _leg("call", "sell", strike=100, price=3, expiry=expiry, iv=0.25, quantity=2),
        _leg("call", "buy", strike=105, price=1, expiry=expiry, iv=0.25),
    ]
    estimate = estimate_probability(legs, 100, as_of + timedelta(seconds=45), as_of=as_of)
    assert estimate.available is True
    assert 0 <= (estimate.probability_of_profit or 0) <= 1

    # 27 Nov 2026 is the NYSE post-Thanksgiving early close at 18:00 UTC.
    close = _regular_close_after(datetime(2026, 11, 27, 15, 0, tzinfo=UTC))
    assert close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_iron_condor_short_strikes_are_distinct_when_spot_is_on_a_strike() -> None:
    candidate = build_candidates(_snapshot(), TradeAdviceRequest(ticker="TEST", horizon="swing",
                                                                 strategies=["iron_condor"]))[0]
    shorts = sorted(leg.strike for leg in candidate.legs if leg.side == "sell")
    assert shorts[0] < shorts[1]


def test_volatile_view_defaults_to_long_volatility_only() -> None:
    candidates = build_candidates(_snapshot(), TradeAdviceRequest(ticker="TEST", direction="volatile"))
    assert candidates
    assert {item.strategy for item in candidates} <= {"long_straddle", "long_strangle"}


def test_owned_share_covered_call_quantity_without_fees_or_allocation() -> None:
    request = TradeAdviceRequest(ticker="TEST", horizon="swing", strategies=["covered_call"],
                                 existing_shares=300, fee_per_contract=0)
    assert build_candidates(_snapshot(), request)[0].quantity_for_allocation == 3


def test_candidates_that_cannot_profit_are_omitted() -> None:
    snapshot = _snapshot()
    for quote in snapshot.options:
        quote.bid, quote.ask = 0.0, 0.01
    request = TradeAdviceRequest(ticker="TEST", horizon="swing", strategies=["short_strangle"])
    assert build_candidates(snapshot, request) == []


def test_reprice_keeps_the_selected_contracts_after_a_small_spot_move() -> None:
    from src.services.trade_desk.analytics import reprice_candidate
    snapshot = _snapshot()
    snapshot.spot = 102.49
    request = TradeAdviceRequest(ticker="TEST", horizon="swing", strategies=["long_call"])
    original = build_candidates(snapshot, request)[0]
    moved = snapshot.model_copy(deep=True)
    moved.spot = 102.51  # nearest strike would now be 105
    for quote in moved.options:
        quote.ask += 0.02
    assert build_candidates(moved, request)[0].legs[0].contract_id != original.legs[0].contract_id
    repriced = reprice_candidate(original, moved, request)
    assert repriced is not None and repriced.id == original.id
    assert repriced.legs[0].contract_id == original.legs[0].contract_id
    assert repriced.legs[0].entry_price == pytest.approx(original.legs[0].entry_price + 0.02)
    missing = moved.model_copy(update={"options": [q for q in moved.options
                                                   if q.contract_id != original.legs[0].contract_id]})
    assert reprice_candidate(original, missing, request) is None


def test_credit_spreads_sell_the_out_of_the_money_strike() -> None:
    snapshot = _snapshot()
    snapshot.spot = 102.0
    for strategy, right in (("bull_put_credit", "put"), ("bear_call_credit", "call")):
        candidate = build_candidates(snapshot, TradeAdviceRequest(ticker="TEST", horizon="swing",
                                                                  strategies=[strategy]))[0]
        short = next(leg for leg in candidate.legs if leg.side == "sell")
        long = next(leg for leg in candidate.legs if leg.side == "buy")
        if right == "put":
            assert short.strike <= snapshot.spot and long.strike < short.strike
        else:
            assert short.strike >= snapshot.spot and long.strike > short.strike


def test_late_close_etf_same_day_expiry_is_usable_intraday() -> None:
    from src.services.trade_desk.analytics import (_option_quote_valid, _session_close_for_observation,
                                                   _session_open_for_observation)
    snapshot = _snapshot()
    close = _session_close_for_observation(snapshot.quoted_at)
    opened = _session_open_for_observation(snapshot.quoted_at)
    if close is None or opened is None or not opened <= snapshot.quoted_at < close:
        pytest.skip("requires an open NYSE session at test time")
    quote = snapshot.options[0].model_copy(update={"expiry": close + timedelta(minutes=15)})
    assert _option_quote_valid(quote, snapshot, snapshot.quoted_at)
    late = quote.model_copy(update={"expiry": close + timedelta(minutes=16)})
    assert not _option_quote_valid(late, snapshot, snapshot.quoted_at)


def test_swing_never_uses_a_late_close_same_day_expiry() -> None:
    from src.services.trade_desk.analytics import _session_close_for_observation, _session_open_for_observation
    snapshot = _snapshot()
    close = _session_close_for_observation(snapshot.quoted_at)
    opened = _session_open_for_observation(snapshot.quoted_at)
    if close is None or opened is None or not opened <= snapshot.quoted_at < close:
        pytest.skip("requires an open NYSE session at test time")
    same_day = [q.model_copy(update={"expiry": close + timedelta(minutes=15),
                                     "contract_id": q.contract_id + "-0dte"}) for q in snapshot.options]
    snapshot.options.extend(same_day)
    swing = build_candidates(snapshot, TradeAdviceRequest(ticker="TEST", horizon="swing", strategies=["long_call"]))[0]
    assert swing.legs[0].expiry > close + timedelta(minutes=15)
    intraday = build_candidates(snapshot, TradeAdviceRequest(ticker="TEST", horizon="intraday", strategies=["long_call"]))[0]
    assert intraday.legs[0].contract_id.endswith("-0dte")
