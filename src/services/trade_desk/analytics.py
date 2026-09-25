"""Deterministic option strategy construction, pricing, and payoff analytics.

The trade desk deliberately keeps this module independent of an option chain
provider.  A :class:`~.models.QuoteSnapshot` is the complete universe from
which candidates may be built; every option leg therefore retains the exact
contract id and quote prices supplied by the caller.

The numerical routines use simple, inspectable models:

* quoted entry prices are ask for buys and bid for sells;
* expiration payoffs are piecewise linear and are analysed exactly;
* risk-neutral profit probabilities use a lognormal distribution and the
  supplied IV/rate/dividend assumptions;
* pre-expiry marks use an American CRR tree with fractional-second time to
  expiry.

These are valuation aids for a paper/manual-trading workflow.  They are not a
forecast of real-world returns or a substitute for an execution model.
"""

from __future__ import annotations

import itertools
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from .models import (
    PLAN_STRATEGY,
    OptionLeg,
    OptionQuote,
    PayoffAnalysis,
    ProbabilityEstimate,
    QuoteSnapshot,
    StrategyCandidate,
    TradeAdviceRequest,
)

SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0
_LATE_CLOSE_ALLOWANCE = timedelta(minutes=15)
_EPS = 1.0e-10
_MONEY_EPS = 1.0e-8


_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "long_call",
        "title": "Long call",
        "category": "directional",
        "bias": "bullish",
        "required_contracts": 1,
        "max_gain": "unbounded",
        "max_loss": "debit",
        "description": "Buy one call for defined downside and leveraged upside.",
    },
    {
        "id": "long_put",
        "title": "Long put",
        "category": "directional",
        "bias": "bearish",
        "required_contracts": 1,
        "max_gain": "bounded",
        "max_loss": "debit",
        "description": "Buy one put for defined downside and a downside hedge.",
    },
    {
        "id": "bull_call_debit",
        "title": "Bull call debit spread",
        "category": "directional",
        "bias": "bullish",
        "required_contracts": 2,
        "max_gain": "defined",
        "max_loss": "defined",
        "description": "Buy a lower-strike call and sell a higher-strike call.",
    },
    {
        "id": "bear_put_debit",
        "title": "Bear put debit spread",
        "category": "directional",
        "bias": "bearish",
        "required_contracts": 2,
        "max_gain": "defined",
        "max_loss": "defined",
        "description": "Buy a higher-strike put and sell a lower-strike put.",
    },
    {
        "id": "bull_put_credit",
        "title": "Bull put credit spread",
        "category": "income",
        "bias": "bullish",
        "required_contracts": 2,
        "max_gain": "defined",
        "max_loss": "defined",
        "description": "Sell a higher-strike put and buy a lower-strike put.",
    },
    {
        "id": "bear_call_credit",
        "title": "Bear call credit spread",
        "category": "income",
        "bias": "bearish",
        "required_contracts": 2,
        "max_gain": "defined",
        "max_loss": "defined",
        "description": "Sell a lower-strike call and buy a higher-strike call.",
    },
    {
        "id": "long_straddle",
        "title": "Long straddle",
        "category": "volatility",
        "bias": "volatile",
        "required_contracts": 2,
        "max_gain": "unbounded",
        "max_loss": "debit",
        "description": "Buy a call and put at the same strike.",
    },
    {
        "id": "short_straddle",
        "title": "Short straddle",
        "category": "volatility",
        "bias": "neutral",
        "required_contracts": 2,
        "max_gain": "credit",
        "max_loss": "unbounded",
        "description": "Sell a call and put at the same strike.",
    },
    {
        "id": "long_strangle",
        "title": "Long strangle",
        "category": "volatility",
        "bias": "volatile",
        "required_contracts": 2,
        "max_gain": "unbounded",
        "max_loss": "debit",
        "description": "Buy an out-of-the-money put and call.",
    },
    {
        "id": "short_strangle",
        "title": "Short strangle",
        "category": "volatility",
        "bias": "neutral",
        "required_contracts": 2,
        "max_gain": "credit",
        "max_loss": "unbounded",
        "description": "Sell an out-of-the-money put and call.",
    },
    {
        "id": "call_butterfly",
        "title": "Call butterfly",
        "category": "defined_risk",
        "bias": "neutral",
        "required_contracts": 3,
        "max_gain": "defined",
        "max_loss": "defined",
        "aliases": ["butterfly_call"],
        "description": "Buy one lower call, sell two middle calls, and buy one upper call.",
    },
    {
        "id": "put_butterfly",
        "title": "Put butterfly",
        "category": "defined_risk",
        "bias": "neutral",
        "required_contracts": 3,
        "max_gain": "defined",
        "max_loss": "defined",
        "aliases": ["butterfly_put"],
        "description": "Buy one lower put, sell two middle puts, and buy one upper put.",
    },
    {
        "id": "iron_condor",
        "title": "Iron condor",
        "category": "defined_risk",
        "bias": "neutral",
        "required_contracts": 4,
        "max_gain": "defined",
        "max_loss": "defined",
        "description": "Sell an inner put and call with protective outer wings.",
    },
    {
        "id": "covered_call",
        "title": "Covered call",
        "category": "stock_income",
        "bias": "neutral",
        "required_contracts": 1,
        "max_gain": "defined",
        "max_loss": "defined",
        "description": "Own or buy 100 shares and sell one call against them.",
    },
    {
        "id": "cash_secured_put",
        "title": "Cash-secured put",
        "category": "stock_income",
        "bias": "bullish",
        "required_contracts": 1,
        "max_gain": "credit",
        "max_loss": "defined",
        "description": "Sell a put while reserving the full strike commitment in cash.",
    },
    {
        "id": "uncovered_call",
        "title": "Uncovered call",
        "category": "naked",
        "bias": "bearish",
        "required_contracts": 1,
        "max_gain": "credit",
        "max_loss": "unbounded",
        "description": "Sell a call without stock or a protective call wing.",
    },
    {
        "id": "uncovered_put",
        "title": "Uncovered put",
        "category": "naked",
        "bias": "bullish",
        "required_contracts": 1,
        "max_gain": "credit",
        "max_loss": "defined_at_zero",
        "description": "Sell a put without reserving a cash-secured commitment.",
    },
)

_CATALOG_BY_ID = {item["id"]: item for item in _CATALOG}
_ALIASES = {
    alias: item["id"]
    for item in _CATALOG
    for alias in [item["id"], *item.get("aliases", [])]
}
_ALIASES.update(
    {
        "naked_call": "uncovered_call",
        "naked_put": "uncovered_put",
        "call_butterfly": "call_butterfly",
        "put_butterfly": "put_butterfly",
    }
)


def strategy_catalog() -> list[dict[str, Any]]:
    """Return the stable, UI-friendly strategy catalog.

    A fresh deep copy is returned so callers cannot mutate the module-level
    registry while constructing an advice response.
    """

    return deepcopy(list(_CATALOG))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _norm_cdf(value: float) -> float:
    """Standard normal CDF without a scipy dependency."""

    if value == math.inf:
        return 1.0
    if value == -math.inf:
        return 0.0
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _year_fraction(start: datetime, end: datetime) -> float:
    start_utc = _utc(start)
    end_utc = _utc(end)
    if start_utc is None or end_utc is None:
        return 0.0
    return (end_utc - start_utc).total_seconds() / SECONDS_PER_YEAR


def _normalise_right(right: str) -> str:
    value = str(right).strip().lower()
    if value in {"c", "call"}:
        return "call"
    if value in {"p", "put"}:
        return "put"
    raise ValueError(f"Unsupported option right: {right}")


def black_scholes_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    right: str = "call",
) -> float:
    """Return a European Black-Scholes value.

    ``time_to_expiry`` is in years and may be derived from fractional seconds
    by :func:`_year_fraction`.  Zero time returns intrinsic value exactly.
    """

    right = _normalise_right(right)
    spot = float(spot)
    strike = float(strike)
    time_to_expiry = float(time_to_expiry)
    volatility = float(volatility)
    if spot < 0 or strike <= 0:
        raise ValueError("spot must be non-negative and strike must be positive")
    if time_to_expiry <= 0:
        return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
    if volatility <= 0:
        forward = spot * math.exp((risk_free_rate - dividend_yield) * time_to_expiry)
        intrinsic_forward = max(forward - strike, 0.0) if right == "call" else max(strike - forward, 0.0)
        return math.exp(-risk_free_rate * time_to_expiry) * intrinsic_forward

    root_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(max(spot, _EPS) / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility * volatility) * time_to_expiry
    ) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    discount_spot = spot * math.exp(-dividend_yield * time_to_expiry)
    discount_strike = strike * math.exp(-risk_free_rate * time_to_expiry)
    if right == "call":
        return discount_spot * _norm_cdf(d1) - discount_strike * _norm_cdf(d2)
    return discount_strike * _norm_cdf(-d2) - discount_spot * _norm_cdf(-d1)


def crr_binomial_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    right: str = "call",
    *,
    steps: int = 200,
    american: bool = True,
) -> float:
    """Price an option with an American or European Cox-Ross-Rubinstein tree.

    The routine accepts non-integer time to expiry and handles the zero-time
    boundary directly.  ``steps`` is intentionally bounded for callers that
    use this in a response path.
    """

    right = _normalise_right(right)
    spot = float(spot)
    strike = float(strike)
    time_to_expiry = float(time_to_expiry)
    volatility = float(volatility)
    if spot < 0 or strike <= 0:
        raise ValueError("spot must be non-negative and strike must be positive")
    if time_to_expiry <= 0:
        return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
    if volatility <= 0:
        # With no diffusion, immediate exercise remains available for an
        # American option.  The deterministic European value can be below
        # current intrinsic (notably for puts with positive rates), so retain
        # the American exercise lower bound explicitly.
        european = black_scholes_price(
            spot,
            strike,
            time_to_expiry,
            volatility,
            risk_free_rate,
            dividend_yield,
            right,
        )
        current_intrinsic = max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
        return max(european, current_intrinsic) if american else european
    steps = max(1, min(int(steps), 500))
    dt = time_to_expiry / steps
    root_dt = math.sqrt(dt)
    up = math.exp(volatility * root_dt)
    down = 1.0 / up
    growth = math.exp((risk_free_rate - dividend_yield) * dt)
    probability = (growth - down) / (up - down)
    # An invalid risk-neutral probability means the requested tree parameters
    # are outside the CRR model's no-arbitrage region.  Surface that fact to
    # callers instead of silently changing the requested model by clamping.
    if probability < -1.0e-10 or probability > 1.0 + 1.0e-10:
        raise ValueError("CRR risk-neutral probability is outside [0, 1]")
    probability = min(1.0, max(0.0, probability))
    discount = math.exp(-risk_free_rate * dt)

    values = []
    for down_moves in range(steps + 1):
        node_spot = spot * (up ** (steps - down_moves)) * (down**down_moves)
        intrinsic = max(node_spot - strike, 0.0) if right == "call" else max(strike - node_spot, 0.0)
        values.append(intrinsic)

    for level in range(steps - 1, -1, -1):
        next_values = []
        for index in range(level + 1):
            continuation = discount * (probability * values[index] + (1.0 - probability) * values[index + 1])
            if american:
                node_spot = spot * (up**(level - index)) * (down**index)
                intrinsic = max(node_spot - strike, 0.0) if right == "call" else max(strike - node_spot, 0.0)
                next_values.append(max(continuation, intrinsic))
            else:
                next_values.append(continuation)
        values = next_values
    return float(values[0])


def _intrinsic(right: str, underlying_price: float, strike: float) -> float:
    return max(underlying_price - strike, 0.0) if right == "call" else max(strike - underlying_price, 0.0)


def _entry_debit(legs: Sequence[OptionLeg]) -> float:
    total = 0.0
    for leg in legs:
        if leg.existing:
            continue
        sign = 1.0 if leg.side == "buy" else -1.0
        total += sign * float(leg.entry_price) * int(leg.quantity) * int(leg.multiplier)
    return total


def fees_for_legs(legs: Sequence[OptionLeg], fee_per_contract: float = 0.65) -> float:
    """Return option contract fees; stock legs do not consume option contracts."""

    fee_per_contract = max(0.0, float(fee_per_contract))
    return fee_per_contract * sum(int(leg.quantity) for leg in legs if leg.right != "stock")


def payoff_at_price(legs: Sequence[OptionLeg], underlying_price: float, fees: float = 0.0) -> float:
    """Return expiration P&L at a non-negative underlying price.

    Existing stock is marked from the current spot stored in its leg's
    ``entry_price``.  Its acquisition cost is excluded from the cash debit but
    the share price change remains in P&L, making owned-share covered calls
    economically comparable to buy-writes.
    """

    underlying_price = float(underlying_price)
    if underlying_price < 0 or not _finite(underlying_price):
        raise ValueError("underlying_price must be a finite non-negative number")
    result = -float(fees)
    for leg in legs:
        quantity = int(leg.quantity)
        multiplier = int(leg.multiplier)
        sign = 1.0 if leg.side == "buy" else -1.0
        if leg.right == "stock":
            result += sign * quantity * multiplier * (underlying_price - float(leg.entry_price))
            continue
        if leg.strike is None:
            raise ValueError("option legs require a strike")
        intrinsic = _intrinsic(leg.right, underlying_price, float(leg.strike))
        result += sign * quantity * multiplier * (intrinsic - float(leg.entry_price))
    return float(result)


def strategy_payoff(legs: Sequence[OptionLeg], underlying_price: float, fees: float = 0.0) -> float:
    """Alias with a discoverable name for callers rendering a payoff chart."""

    return payoff_at_price(legs, underlying_price, fees)


def _breakpoints(legs: Sequence[OptionLeg]) -> list[float]:
    return sorted({float(leg.strike) for leg in legs if leg.right != "stock" and leg.strike is not None})


def _affine_segments(legs: Sequence[OptionLeg], fees: float = 0.0) -> list[tuple[float, float | None, float, float]]:
    """Return ``(left, right, slope, intercept)`` expiration payoff segments."""

    strikes = _breakpoints(legs)
    boundaries: list[tuple[float, float | None]] = []
    if not strikes:
        boundaries.append((0.0, None))
    else:
        boundaries.append((0.0, strikes[0]))
        boundaries.extend((left, right) for left, right in zip(strikes, strikes[1:]))
        boundaries.append((strikes[-1], None))

    segments: list[tuple[float, float | None, float, float]] = []
    for left, right in boundaries:
        if right is None:
            probe = left + max(1.0, left * 0.1)
        elif right <= left:
            probe = left
        else:
            probe = (left + right) / 2.0
        slope = 0.0
        intercept = -float(fees)
        for leg in legs:
            quantity = int(leg.quantity)
            multiplier = int(leg.multiplier)
            sign = 1.0 if leg.side == "buy" else -1.0
            scale = sign * quantity * multiplier
            if leg.right == "stock":
                slope += scale
                intercept -= scale * float(leg.entry_price)
                continue
            if leg.strike is None:
                raise ValueError("option legs require a strike")
            strike = float(leg.strike)
            entry = float(leg.entry_price)
            if leg.right == "call" and probe > strike:
                slope += scale
                intercept += scale * (-strike - entry)
            elif leg.right == "put" and probe < strike:
                slope -= scale
                intercept += scale * (strike - entry)
            else:
                intercept -= scale * entry
        segments.append((left, right, slope, intercept))
    return segments


def _dedupe_sorted(values: Iterable[float], tolerance: float = 1.0e-7) -> list[float]:
    result: list[float] = []
    for value in sorted(float(v) for v in values if _finite(v) and v >= 0):
        if not result or abs(value - result[-1]) > tolerance * max(1.0, abs(value), abs(result[-1])):
            result.append(value)
    return result


def analyze_payoff(
    legs: Sequence[OptionLeg],
    *,
    fees: float = 0.0,
    capital_required: float | None = None,
    capital_note: str = "",
    assignment_note: str = "",
) -> PayoffAnalysis:
    """Compute exact expiration bounds and break-even prices for a strategy."""

    legs = list(legs)
    segments = _affine_segments(legs, fees)
    strikes = _breakpoints(legs)
    finite_points = [0.0, *strikes]
    if strikes:
        # Payoff bounds stay exact from the affine segments; these extra finite
        # tail samples keep the chart informative beyond the highest strike.
        highest = max(strikes)
        finite_points.extend((highest * 1.10, highest * 1.50, highest * 2.0))
    finite_points = _dedupe_sorted(finite_points)
    point_values = [payoff_at_price(legs, price, fees) for price in finite_points]
    upper_slope = segments[-1][2] if segments else 0.0
    gain_unbounded = upper_slope > _EPS
    loss_unbounded = upper_slope < -_EPS
    max_gain = None if gain_unbounded else max(0.0, max(point_values, default=0.0))
    max_loss = None if loss_unbounded else max(0.0, -min(point_values, default=0.0))

    breakevens: list[float] = []
    for left, right, slope, intercept in segments:
        if abs(slope) <= _EPS:
            if abs(intercept) <= _MONEY_EPS:
                # A zero-valued interval is represented by its finite endpoints
                # only; returning every point would not be useful to clients.
                if left > _EPS:
                    breakevens.append(left)
                if right is not None:
                    breakevens.append(right)
            continue
        root = -intercept / slope
        if root >= left - _MONEY_EPS and (right is None or root <= right + _MONEY_EPS):
            breakevens.append(max(0.0, root))
    breakevens = _dedupe_sorted(breakevens)
    points = [{"price": float(price), "underlying": float(price), "pnl": float(value)} for price, value in zip(finite_points, point_values)]
    return PayoffAnalysis(
        entry_debit=float(_entry_debit(legs)),
        fees=float(fees),
        max_gain=None if max_gain is None else float(max_gain),
        max_loss=None if max_loss is None else float(max_loss),
        gain_bound="unbounded" if gain_unbounded else "bounded",
        loss_bound="unbounded" if loss_unbounded else "bounded",
        breakevens=breakevens,
        capital_required=None if capital_required is None else float(capital_required),
        capital_note=capital_note,
        assignment_note=assignment_note,
        points=points,
    )


def compute_payoff_analysis(
    legs: Sequence[OptionLeg],
    fee_per_contract: float = 0.65,
    *,
    capital_required: float | None = None,
    capital_note: str = "",
    assignment_note: str = "",
) -> PayoffAnalysis:
    """Convenience wrapper that derives total fees from option quantities."""

    return analyze_payoff(
        legs,
        fees=fees_for_legs(legs, fee_per_contract),
        capital_required=capital_required,
        capital_note=capital_note,
        assignment_note=assignment_note,
    )


def _lognormal_parameters(spot: float, time_to_horizon: float, volatility: float, rate: float, dividend: float) -> tuple[float, float]:
    sigma = max(0.0, float(volatility))
    variance = sigma * sigma * max(0.0, time_to_horizon)
    mean = math.log(max(float(spot), _EPS)) + (float(rate) - float(dividend) - 0.5 * sigma * sigma) * max(
        0.0, time_to_horizon
    )
    return mean, variance


def _lognormal_cdf(price: float, mean: float, variance: float) -> float:
    if price <= 0:
        return 0.0
    if variance <= _EPS:
        return 1.0 if math.exp(mean) <= price else 0.0
    sigma = math.sqrt(variance)
    return _norm_cdf((math.log(price) - mean) / sigma)


def _lognormal_first_moment_cdf(price: float, mean: float, variance: float) -> float:
    """Return E[S 1(S <= price)] for the lognormal distribution."""

    if price <= 0:
        return 0.0
    if variance <= _EPS:
        return math.exp(mean) if math.exp(mean) <= price else 0.0
    sigma = math.sqrt(variance)
    return math.exp(mean + 0.5 * variance) * _norm_cdf((math.log(price) - mean - variance) / sigma)


def _positive_interval_probability(
    left: float,
    right: float | None,
    slope: float,
    intercept: float,
    mean: float,
    variance: float,
) -> float:
    """Integrate a positive affine payoff over one lognormal interval."""

    lower = left
    upper = math.inf if right is None else right
    if abs(slope) <= _EPS:
        if intercept <= 0:
            return 0.0
        return _lognormal_cdf(upper, mean, variance) - _lognormal_cdf(lower, mean, variance)
    root = -intercept / slope
    if slope > 0:
        lower = max(lower, root)
    else:
        upper = min(upper, root)
    if upper <= lower:
        return 0.0
    return max(0.0, _lognormal_cdf(upper, mean, variance) - _lognormal_cdf(lower, mean, variance))


def risk_neutral_profit_probability(
    legs: Sequence[OptionLeg],
    spot: float,
    horizon_at: datetime,
    *,
    as_of: datetime | None = None,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    fees: float = 0.0,
    expiry_verified: bool = True,
) -> float | None:
    """Return expiration P(profit > 0) under the IV lognormal model.

    This helper intentionally returns ``None`` when any option lacks IV or a
    verified expiry.  Use :func:`estimate_probability` when the structured
    reason and assumptions are needed.
    """

    estimate = estimate_probability(
        legs,
        spot,
        horizon_at,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        fees=fees,
        expiry_verified=expiry_verified,
    )
    return estimate.probability_of_profit if estimate.available else None


def risk_neutral_expected_payoff(
    legs: Sequence[OptionLeg],
    spot: float,
    horizon_at: datetime,
    *,
    as_of: datetime | None = None,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    fees: float = 0.0,
    expiry_verified: bool = True,
) -> float | None:
    """Return the undiscounted risk-neutral expected expiration P&L."""

    option_legs = [leg for leg in legs if leg.right != "stock"]
    expiries = [_utc(leg.expiry) for leg in option_legs]
    ivs = [float(leg.iv) for leg in option_legs if leg.iv is not None and _finite(leg.iv) and float(leg.iv) > 0]
    expiry = expiries[0] if expiries and all(item == expiries[0] for item in expiries) else None
    horizon = _utc(horizon_at)
    valuation = _utc(as_of) if as_of is not None else None
    if expiry is None or horizon is None or not expiry_verified or len(ivs) != len(option_legs):
        return None
    if valuation is None:
        valuation = horizon - timedelta(days=30)
    time_to_horizon = _year_fraction(valuation, horizon)
    if time_to_horizon <= 0:
        return payoff_at_price(legs, spot, fees)
    volatility = sum(ivs) / len(ivs)
    mean, variance = _lognormal_parameters(spot, time_to_horizon, volatility, risk_free_rate, dividend_yield)
    if variance <= _EPS:
        return payoff_at_price(legs, math.exp(mean), fees)
    expected = 0.0
    for left, right, slope, intercept in _affine_segments(legs, fees):
        probability = _lognormal_cdf(right if right is not None else math.inf, mean, variance) - _lognormal_cdf(
            left, mean, variance
        )
        first_moment = _lognormal_first_moment_cdf(right if right is not None else math.inf, mean, variance) - (
            _lognormal_first_moment_cdf(left, mean, variance)
        )
        expected += slope * first_moment + intercept * probability
    return float(expected)


def _common_expiry(legs: Sequence[OptionLeg]) -> datetime | None:
    expiries = [_utc(leg.expiry) for leg in legs if leg.right != "stock"]
    if not expiries or any(item is None for item in expiries):
        return None
    first = expiries[0]
    if any(item != first for item in expiries[1:]):
        return None
    return first


def _mean_iv(legs: Sequence[OptionLeg]) -> tuple[float | None, list[float]]:
    option_legs = [leg for leg in legs if leg.right != "stock"]
    values: list[float] = []
    for leg in option_legs:
        if leg.iv is None or not _finite(leg.iv) or float(leg.iv) <= 0:
            return None, values
        values.append(float(leg.iv))
    if not values:
        return None, values
    return sum(values) / len(values), values


def _option_value_at(
    leg: OptionLeg,
    underlying_price: float,
    valuation_at: datetime,
    *,
    risk_free_rate: float,
    dividend_yield: float,
    steps: int,
    iv_scale: float = 1.0,
    time_scale: float = 1.0,
) -> float:
    if leg.right == "stock":
        return float(underlying_price)
    if leg.strike is None or leg.expiry is None:
        return float("nan")
    expiry = _utc(leg.expiry)
    valuation = _utc(valuation_at)
    if expiry is None or valuation is None:
        return float("nan")
    remaining = _year_fraction(valuation, expiry)
    if remaining <= 0:
        return _intrinsic(leg.right, underlying_price, float(leg.strike))
    if leg.iv is None or not _finite(leg.iv) or float(leg.iv) <= 0:
        return float("nan")
    iv = float(leg.iv) * max(0.0, float(iv_scale))
    remaining *= max(0.0, float(time_scale))
    if iv <= 0:
        return float("nan")
    try:
        return crr_binomial_price(
            underlying_price,
            float(leg.strike),
            remaining,
            iv,
            risk_free_rate,
            dividend_yield,
            leg.right,
            steps=steps,
            american=leg.exercise_style == "american",
        )
    except ValueError:
        # A pre-expiry candidate can retain its payoff while explicitly
        # reporting probability/scenario unavailability for invalid CRR inputs.
        return float("nan")


def mark_to_market_payoff(
    legs: Sequence[OptionLeg],
    underlying_price: float,
    valuation_at: datetime,
    *,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    fees: float = 0.0,
    steps: int = 100,
    iv_scale: float = 1.0,
    time_scale: float = 1.0,
) -> float:
    """Mark option legs with CRR values and return P&L at a pre-expiry exit."""

    result = -float(fees)
    for leg in legs:
        sign = 1.0 if leg.side == "buy" else -1.0
        scale = sign * int(leg.quantity) * int(leg.multiplier)
        current = _option_value_at(
            leg,
            float(underlying_price),
            valuation_at,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            steps=steps,
            iv_scale=iv_scale,
            time_scale=time_scale,
        )
        if not _finite(current):
            return float("nan")
        if leg.right == "stock":
            result += scale * (float(underlying_price) - float(leg.entry_price))
        else:
            result += scale * (current - float(leg.entry_price))
    return float(result)


def _preexpiry_profit_probability(
    legs: Sequence[OptionLeg],
    spot: float,
    horizon_at: datetime,
    as_of: datetime,
    *,
    volatility: float,
    risk_free_rate: float,
    dividend_yield: float,
    fees: float,
    steps: int,
) -> float | None:
    time_to_horizon = _year_fraction(as_of, horizon_at)
    if time_to_horizon <= 0:
        value = mark_to_market_payoff(
            legs,
            spot,
            horizon_at,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            fees=fees,
            steps=steps,
        )
        return 1.0 if value > _MONEY_EPS else 0.0
    sigma_t = max(volatility, 1.0e-6) * math.sqrt(time_to_horizon)
    # Cover the risk-neutral mass while adding dense neighborhoods around all
    # strikes and expiration break-evens.  The latter matters for narrow
    # butterfly/condor profit islands close to expiration.
    span = min(20.0, max(0.5, 8.0 * sigma_t))
    grid_count = 241
    log_spot = math.log(max(spot, _EPS))
    lower_log = log_spot - span
    upper_log = log_spot + span
    seeds = [
        float(value)
        for value in [
            *[float(leg.strike) for leg in legs if leg.strike is not None],
            *analyze_payoff(legs, fees=fees).breakevens,
        ]
        if _finite(value) and value > 0
    ]
    for seed in seeds:
        lower_log = min(lower_log, math.log(seed) - 0.05)
        upper_log = max(upper_log, math.log(seed) + 0.05)
    grid = [math.exp(lower_log + (upper_log - lower_log) * i / (grid_count - 1)) for i in range(grid_count)]
    for seed in seeds:
        for factor in (0.97, 0.99, 0.999, 1.0, 1.001, 1.01, 1.03):
            candidate = seed * factor
            if candidate > 0 and math.isfinite(candidate):
                grid.append(candidate)
    grid = _dedupe_sorted(grid)
    values = [
        mark_to_market_payoff(
            legs,
            value,
            horizon_at,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            fees=fees,
            steps=steps,
        )
        for value in grid
    ]
    if any(not _finite(value) for value in values):
        return None

    roots: list[float] = []
    for left, right, left_value, right_value in zip(grid, grid[1:], values, values[1:]):
        if left_value == 0.0:
            roots.append(left)
        if left_value * right_value < 0.0:
            low, high = left, right
            low_value = left_value
            for _ in range(50):
                middle = (low + high) / 2.0
                middle_value = mark_to_market_payoff(
                    legs,
                    middle,
                    horizon_at,
                    risk_free_rate=risk_free_rate,
                    dividend_yield=dividend_yield,
                    fees=fees,
                    steps=steps,
                )
                if low_value * middle_value <= 0:
                    high = middle
                else:
                    low, low_value = middle, middle_value
            roots.append((low + high) / 2.0)
    if values[-1] == 0.0:
        roots.append(grid[-1])
    roots = _dedupe_sorted(roots)

    mean, variance = _lognormal_parameters(spot, time_to_horizon, volatility, risk_free_rate, dividend_yield)
    boundaries = [grid[0], *roots, grid[-1]]
    probability = 0.0
    for left, right in zip(boundaries, boundaries[1:]):
        if right <= left:
            continue
        midpoint = math.sqrt(left * right)
        value = mark_to_market_payoff(
            legs,
            midpoint,
            horizon_at,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            fees=fees,
            steps=steps,
        )
        if value > 0:
            probability += _lognormal_cdf(right, mean, variance) - _lognormal_cdf(left, mean, variance)
    if values[0] > 0:
        probability += _lognormal_cdf(grid[0], mean, variance)
    if values[-1] > 0:
        probability += 1.0 - _lognormal_cdf(grid[-1], mean, variance)
    return min(1.0, max(0.0, float(probability)))


def estimate_probability(
    legs: Sequence[OptionLeg],
    spot: float,
    horizon_at: datetime,
    *,
    as_of: datetime | None = None,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    fees: float = 0.0,
    expiry_verified: bool = True,
    steps: int = 100,
) -> ProbabilityEstimate:
    """Estimate P(profit > 0) and preserve the model assumptions in JSON."""

    horizon = _utc(horizon_at)
    expiry = _common_expiry(legs)
    volatility, ivs = _mean_iv(legs)
    if horizon is None:
        return ProbabilityEstimate(available=False, reason="Horizon must be timezone-aware.")
    if expiry is None:
        return ProbabilityEstimate(available=False, horizon_at=horizon, reason="All option legs must share one expiry.")
    if not expiry_verified:
        return ProbabilityEstimate(
            available=False,
            horizon_at=horizon,
            horizon_label="expiration" if horizon >= expiry else "preexpiry",
            reason="Verified expiry data is required for an IV probability.",
        )
    if volatility is None:
        return ProbabilityEstimate(
            available=False,
            horizon_at=horizon,
            horizon_label="expiration" if horizon >= expiry else "preexpiry",
            reason="Every option leg needs a positive implied volatility.",
        )
    valuation = _utc(as_of) if as_of is not None else datetime.now(timezone.utc)
    assert valuation is not None
    if horizon < valuation:
        return ProbabilityEstimate(
            available=False,
            horizon_at=horizon,
            horizon_label="expiration" if horizon >= expiry else "preexpiry",
            reason="The requested horizon is before the valuation timestamp.",
        )
    time_to_horizon = _year_fraction(valuation, horizon)
    assumptions: dict[str, Any] = {
        "distribution": "lognormal_risk_neutral",
        "risk_free_rate": float(risk_free_rate),
        "dividend_yield": float(dividend_yield),
        "initial_spot": float(spot),
        "mean_iv": float(volatility),
        "per_leg_iv": [float(value) for value in ivs],
        "time_to_horizon_years": float(max(0.0, time_to_horizon)),
        "real_world_success_probability": False,
        "entry_fees_only": True,
        "anticipated_close_fees": None,
        "close_fees_included": False,
    }
    if horizon >= expiry:
        if time_to_horizon <= 0:
            value = payoff_at_price(legs, spot, fees)
            probability = 1.0 if value > _MONEY_EPS else 0.0
        else:
            mean, variance = _lognormal_parameters(spot, time_to_horizon, volatility, risk_free_rate, dividend_yield)
            probability = sum(
                _positive_interval_probability(left, right, slope, intercept, mean, variance)
                for left, right, slope, intercept in _affine_segments(legs, fees)
            )
        assumptions["pricing_model"] = "expiration_intrinsic_payoff"
        return ProbabilityEstimate(
            available=True,
            probability_of_profit=min(1.0, max(0.0, float(probability))),
            method="iv_lognormal_risk_neutral",
            horizon_at=horizon,
            horizon_label="expiration",
            reason="Integrated positive expiration-payoff regions under the IV lognormal model; entry fees only, no closing fees assumed.",
            assumptions=assumptions,
        )

    probability = _preexpiry_profit_probability(
        legs,
        float(spot),
        horizon,
        valuation,
        volatility=volatility,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        fees=fees,
        steps=max(30, min(int(steps), 160)),
    )
    if probability is None:
        return ProbabilityEstimate(
            available=False,
            horizon_at=horizon,
            horizon_label="preexpiry",
            reason="CRR mark-to-market could not be evaluated for this horizon.",
            assumptions=assumptions,
        )
    assumptions["pricing_model"] = "american_crr_mark_to_market"
    return ProbabilityEstimate(
        available=True,
        probability_of_profit=probability,
        method="iv_lognormal_crr_mark_to_market",
        horizon_at=horizon,
        horizon_label="preexpiry",
        reason="Integrated positive pre-expiry CRR mark-to-market regions under the IV lognormal model; entry fees only, no closing fees assumed.",
        assumptions=assumptions,
    )


def probability_of_profit(*args: Any, **kwargs: Any) -> ProbabilityEstimate:
    """Public structured probability helper (alias for :func:`estimate_probability`)."""

    return estimate_probability(*args, **kwargs)


def _option_quote_valid(quote: OptionQuote, snapshot: QuoteSnapshot, as_of: datetime) -> bool:
    expiry = _utc(getattr(quote, "expiry", None))
    quoted_at = _utc(getattr(quote, "quoted_at", None))
    if expiry is None or quoted_at is None:
        return False
    if not bool(getattr(quote, "expiry_verified", False)):
        return False
    if not bool(getattr(quote, "standard", False)):
        return False
    if str(getattr(quote, "underlying", "")).upper() != str(snapshot.underlying).upper():
        return False
    if expiry <= max(as_of, quoted_at):
        return False
    # Option quotes are usable only when they describe the same observation
    # as the snapshot.  A future or stale leg can otherwise make a multi-leg
    # candidate look executable while its underlying quote has moved on.
    if abs((quoted_at - as_of).total_seconds()) > 30.0:
        return False
    # A same-session expiry is valid only while the US regular session is
    # still open and the expiry itself is no later than that session close.
    # This keeps valid 0DTE quotes usable without accepting an after-hours
    # timestamp or a made-up midnight expiry.
    eastern = ZoneInfo("America/New_York")
    local_as_of = as_of.astimezone(eastern)
    local_expiry = expiry.astimezone(eastern)
    if local_expiry.date() == local_as_of.date():
        open_at = _session_open_for_observation(as_of)
        close = _session_close_for_observation(as_of)
        # NYSE Arca late-close ETF classes (SPY, QQQ, ...) legitimately expire at
        # 16:15; the provider sets that cutoff only for verified contracts.
        if (open_at is None or close is None or as_of < open_at or as_of >= close
                or expiry > close + _LATE_CLOSE_ALLOWANCE):
            return False
    if not _finite(getattr(quote, "bid", None)) or not _finite(getattr(quote, "ask", None)):
        return False
    bid = float(quote.bid)
    ask = float(quote.ask)
    if bid < 0 or ask < 0 or bid > ask:
        return False
    if int(getattr(quote, "multiplier", 0)) <= 0:
        return False
    if quote.iv is not None and (not _finite(quote.iv) or float(quote.iv) <= 0):
        return False
    return True


def _valid_quote_groups(snapshot: QuoteSnapshot, request: TradeAdviceRequest) -> tuple[datetime | None, dict[datetime, list[OptionQuote]]]:
    snapshot_at = _utc(snapshot.quoted_at)
    if snapshot_at is None:
        return None, {}
    groups: dict[datetime, list[OptionQuote]] = {}
    for quote in snapshot.options:
        if not _option_quote_valid(quote, snapshot, snapshot_at):
            continue
        expiry = _utc(quote.expiry)
        if expiry is None:
            continue
        if request.expiry is not None and expiry.date() != request.expiry:
            continue
        groups.setdefault(expiry, []).append(quote)
    for expiry in list(groups):
        groups[expiry].sort(key=lambda item: (str(item.right), float(item.strike), str(item.contract_id)))
    return snapshot_at, dict(sorted(groups.items()))


def _best_quote(quotes: Iterable[OptionQuote], spot: float, *, minimum: float | None = None, maximum: float | None = None) -> OptionQuote | None:
    candidates = [
        quote
        for quote in quotes
        if (minimum is None or float(quote.strike) >= minimum)
        and (maximum is None or float(quote.strike) <= maximum)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda quote: (abs(float(quote.strike) - spot), float(quote.strike), str(quote.contract_id)))


def _same_multiplier(left: OptionQuote, right: OptionQuote) -> bool:
    return int(left.multiplier) == int(right.multiplier)


def _best_pair(
    quotes: Sequence[OptionQuote],
    spot: float,
    *,
    lower_first: bool = True,
    short_otm: str | None = None,
) -> tuple[OptionQuote, OptionQuote] | None:
    pairs = [
        (left, right)
        for left, right in itertools.combinations(quotes, 2)
        if float(left.strike) != float(right.strike) and _same_multiplier(left, right)
    ]
    if short_otm is not None:
        # Credit spreads sell the out-of-the-money strike: the higher put
        # (bull put) or the lower call (bear call) must not be in the money.
        def short_strike(pair: tuple[OptionQuote, OptionQuote]) -> float:
            strikes = sorted(float(item.strike) for item in pair)
            return strikes[1] if short_otm == "put" else strikes[0]

        otm = [pair for pair in pairs
               if (short_strike(pair) <= spot if short_otm == "put" else short_strike(pair) >= spot)]
        pairs = otm or pairs
    if not pairs:
        return None
    pairs.sort(
        key=lambda pair: (
            abs(((float(pair[0].strike) + float(pair[1].strike)) / 2.0) - spot),
            abs(float(pair[1].strike) - float(pair[0].strike)),
            float(pair[0].strike),
        )
    )
    left, right = pairs[0]
    if float(left.strike) > float(right.strike):
        left, right = right, left
    return (left, right) if lower_first else (right, left)


def _make_option_leg(quote: OptionQuote, side: str, quantity: int = 1) -> OptionLeg:
    return OptionLeg(
        contract_id=str(quote.contract_id),
        right=quote.right,
        side=side,
        quantity=quantity,
        strike=float(quote.strike),
        expiry=quote.expiry,
        multiplier=int(quote.multiplier),
        entry_price=float(quote.ask if side == "buy" else quote.bid),
        iv=None if quote.iv is None else float(quote.iv),
        existing=False,
        exercise_style=quote.exercise_style,
    )


def _make_stock_leg(snapshot: QuoteSnapshot, existing_shares: int, stock_quantity: int) -> OptionLeg:
    quantity = max(1, int(stock_quantity))
    existing = int(existing_shares) >= quantity
    # Existing holdings are marked from the current spot.  A buy-write uses
    # the displayed stock ask so its capital requirement is conservative.
    stock_entry = float(snapshot.spot) if existing else float(snapshot.ask if snapshot.ask is not None else snapshot.spot)
    return OptionLeg(
        contract_id=str(snapshot.underlying),
        right="stock",
        side="buy",
        quantity=quantity,
        strike=None,
        expiry=None,
        multiplier=1,
        entry_price=stock_entry,
        iv=None,
        existing=existing,
        exercise_style="american",
    )


def _construct_strategy(
    strategy: str,
    quotes: Sequence[OptionQuote],
    snapshot: QuoteSnapshot,
    request: TradeAdviceRequest,
) -> list[OptionLeg] | None:
    strategy = _ALIASES.get(strategy, strategy)
    spot = float(snapshot.spot)
    calls = [quote for quote in quotes if quote.right == "call"]
    puts = [quote for quote in quotes if quote.right == "put"]
    calls.sort(key=lambda item: float(item.strike))
    puts.sort(key=lambda item: float(item.strike))

    if strategy == "long_call":
        quote = _best_quote(calls, spot)
        return None if quote is None else [_make_option_leg(quote, "buy")]
    if strategy == "long_put":
        quote = _best_quote(puts, spot)
        return None if quote is None else [_make_option_leg(quote, "buy")]
    if strategy in {"bull_call_debit", "bear_call_credit"}:
        pair = _best_pair(calls, spot, short_otm="call" if strategy == "bear_call_credit" else None)
        if pair is None:
            return None
        lower, upper = pair
        if strategy == "bull_call_debit":
            return [_make_option_leg(lower, "buy"), _make_option_leg(upper, "sell")]
        return [_make_option_leg(lower, "sell"), _make_option_leg(upper, "buy")]
    if strategy in {"bear_put_debit", "bull_put_credit"}:
        pair = _best_pair(puts, spot, short_otm="put" if strategy == "bull_put_credit" else None)
        if pair is None:
            return None
        lower, upper = pair
        if strategy == "bear_put_debit":
            return [_make_option_leg(upper, "buy"), _make_option_leg(lower, "sell")]
        return [_make_option_leg(upper, "sell"), _make_option_leg(lower, "buy")]
    if strategy in {"long_straddle", "short_straddle"}:
        by_strike_call = {float(quote.strike): quote for quote in calls}
        by_strike_put = {float(quote.strike): quote for quote in puts}
        strikes = sorted(set(by_strike_call) & set(by_strike_put))
        if not strikes:
            return None
        strike = min(strikes, key=lambda value: (abs(value - spot), value))
        call, put = by_strike_call[strike], by_strike_put[strike]
        if not _same_multiplier(call, put):
            return None
        side = "buy" if strategy == "long_straddle" else "sell"
        return [_make_option_leg(call, side), _make_option_leg(put, side)]
    if strategy in {"long_strangle", "short_strangle"}:
        otm_put = _best_quote(puts, spot, maximum=spot - _EPS)
        otm_call = _best_quote(calls, spot, minimum=spot + _EPS)
        if otm_put is None or otm_call is None or not _same_multiplier(otm_put, otm_call):
            return None
        side = "buy" if strategy == "long_strangle" else "sell"
        return [_make_option_leg(otm_put, side), _make_option_leg(otm_call, side)]
    if strategy in {"call_butterfly", "put_butterfly"}:
        source = calls if strategy == "call_butterfly" else puts
        triples = [
            triple
            for triple in itertools.combinations(source, 3)
            if len({int(item.multiplier) for item in triple}) == 1
        ]
        if not triples:
            return None
        triple = min(
            triples,
            key=lambda value: (
                abs(float(value[1].strike) - spot),
                abs((float(value[1].strike) - float(value[0].strike)) - (float(value[2].strike) - float(value[1].strike))),
                float(value[2].strike) - float(value[0].strike),
            ),
        )
        low, middle, high = sorted(triple, key=lambda item: float(item.strike))
        return [
            _make_option_leg(low, "buy"),
            _make_option_leg(middle, "sell", quantity=2),
            _make_option_leg(high, "buy"),
        ]
    if strategy == "iron_condor":
        condors: list[tuple[OptionQuote, OptionQuote, OptionQuote, OptionQuote]] = []
        for put_wing, put_short in itertools.combinations(puts, 2):
            if float(put_wing.strike) >= float(put_short.strike) or not _same_multiplier(put_wing, put_short):
                continue
            if float(put_short.strike) > spot:
                continue
            for call_short, call_wing in itertools.combinations(calls, 2):
                if float(call_short.strike) >= float(call_wing.strike) or not _same_multiplier(call_short, call_wing):
                    continue
                if float(call_short.strike) < spot:
                    continue
                if int(put_short.multiplier) != int(call_short.multiplier):
                    continue
                # Equal short strikes would make an iron butterfly, not a condor.
                if float(put_short.strike) >= float(call_short.strike):
                    continue
                condors.append((put_wing, put_short, call_short, call_wing))
        if not condors:
            return None
        put_wing, put_short, call_short, call_wing = min(
            condors,
            key=lambda value: (
                abs(float(value[1].strike) - spot) + abs(float(value[2].strike) - spot),
                abs(float(value[1].strike) - float(value[0].strike))
                + abs(float(value[3].strike) - float(value[2].strike)),
            ),
        )
        return [
            _make_option_leg(put_wing, "buy"),
            _make_option_leg(put_short, "sell"),
            _make_option_leg(call_short, "sell"),
            _make_option_leg(call_wing, "buy"),
        ]
    if strategy == "covered_call":
        quote = _best_quote(calls, spot, minimum=spot)
        if quote is None:
            quote = _best_quote(calls, spot)
        if quote is None:
            return None
        return [_make_stock_leg(snapshot, request.existing_shares, int(quote.multiplier)), _make_option_leg(quote, "sell")]
    if strategy == "cash_secured_put":
        quote = _best_quote(puts, spot, maximum=spot)
        if quote is None:
            quote = _best_quote(puts, spot)
        return None if quote is None else [_make_option_leg(quote, "sell")]
    if strategy == "uncovered_call":
        quote = _best_quote(calls, spot, minimum=spot)
        if quote is None:
            quote = _best_quote(calls, spot)
        return None if quote is None else [_make_option_leg(quote, "sell")]
    if strategy == "uncovered_put":
        quote = _best_quote(puts, spot, maximum=spot)
        if quote is None:
            quote = _best_quote(puts, spot)
        return None if quote is None else [_make_option_leg(quote, "sell")]
    return None


def _capital_for_strategy(
    strategy: str,
    legs: Sequence[OptionLeg],
    payoff: PayoffAnalysis,
    snapshot: QuoteSnapshot,
    request: TradeAdviceRequest,
    fees: float,
) -> tuple[float | None, str]:
    strategy = _ALIASES.get(strategy, strategy)
    if strategy == "covered_call":
        stock = next((leg for leg in legs if leg.right == "stock"), None)
        if stock is not None and stock.existing:
            return float(fees), "Uses existing shares; only entry option fees are new modeled cash."
        shares = next((leg for leg in legs if leg.right == "stock"), None)
        cost = 0.0 if shares is None else float(shares.quantity * shares.multiplier) * float(shares.entry_price)
        return cost + float(fees), "Buy-write capital uses the full stock ask cost plus entry fees; option credit is not treated as available margin."
    if strategy == "cash_secured_put":
        put = next((leg for leg in legs if leg.right == "put"), None)
        if put is None or put.strike is None:
            return None, "Cash-secured strike commitment is unavailable."
        return (
            float(put.strike) * int(put.multiplier) * int(put.quantity) + float(fees),
            "Full strike-times-multiplier cash commitment plus entry fees.",
        )
    if strategy in {"uncovered_call", "uncovered_put", "short_straddle", "short_strangle"}:
        if request.margin_per_unit is None:
            return None, "Capital is unknown until the broker supplies a naked-option margin requirement."
        return float(request.margin_per_unit), "Uses the user-supplied strategy-unit margin_per_unit; broker margin can change."
    if payoff.loss_bound == "bounded" and payoff.max_loss is not None:
        return float(payoff.max_loss), "Defined-risk capital is the exact modeled maximum loss including fees."
    return None, "Capital is unknown because the strategy has an unbounded or broker-dependent loss."


@lru_cache(maxsize=1)
def _exchange_calendar():
    """Load the confirmed NYSE calendar lazily, keeping imports optional."""

    try:
        import exchange_calendars as xcals

        # The default horizon is about one year ahead, which excludes LEAPS.
        end = datetime.now(timezone.utc).date() + timedelta(days=5 * 366)
        return xcals.get_calendar("XNYS", end=end.isoformat())
    except Exception:
        return None


def _session_open_for_observation(value: datetime) -> datetime | None:
    """Return the confirmed open on the observation's session date."""

    utc_value = _utc(value)
    if utc_value is None:
        return None
    calendar = _exchange_calendar()
    if calendar is None:
        return None
    try:
        next_open = _utc(calendar.next_open(utc_value).to_pydatetime())
        if next_open is not None and next_open.date() == utc_value.date():
            return next_open
        previous_open = _utc(calendar.previous_open(utc_value).to_pydatetime())
        if previous_open is not None and previous_open.date() == utc_value.date():
            return previous_open
    except Exception:
        return None
    return None


def _session_close_for_observation(value: datetime) -> datetime | None:
    """Return the confirmed close on the observation's session date."""

    utc_value = _utc(value)
    if utc_value is None:
        return None
    calendar = _exchange_calendar()
    if calendar is None:
        return None
    try:
        next_close = _utc(calendar.next_close(utc_value).to_pydatetime())
        if next_close is not None and next_close.date() == utc_value.date():
            return next_close
        previous_close = _utc(calendar.previous_close(utc_value).to_pydatetime())
        if previous_close is not None and previous_close.date() == utc_value.date():
            return previous_close
    except Exception:
        return None
    return None


def _regular_close_after(value: datetime) -> datetime | None:
    """Return the next confirmed NYSE close after a quote timestamp."""

    utc_value = _utc(value)
    if utc_value is None:
        return None
    calendar = _exchange_calendar()
    if calendar is None:
        return None
    try:
        return _utc(calendar.next_close(utc_value).to_pydatetime())
    except Exception:
        return None


def _scenario_bundle(
    legs: Sequence[OptionLeg],
    spot: float,
    horizon: datetime,
    expiry: datetime,
    *,
    as_of: datetime,
    risk_free_rate: float,
    dividend_yield: float,
    fees: float,
    expiration_probability: ProbabilityEstimate,
) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    strikes = [float(leg.strike) for leg in legs if leg.strike is not None]
    expiration_prices = _dedupe_sorted(
        [0.0, spot, spot * 0.80, spot * 0.90, spot * 1.10, spot * 1.20, *strikes]
    )
    if strikes:
        expiration_prices = _dedupe_sorted([*expiration_prices, max(strikes) * 1.50, max(strikes) * 2.0])
    for price in expiration_prices:
        scenarios.append(
            {
                "kind": "expiration_payoff",
                "horizon_label": "expiration",
                "underlying_price": float(price),
                "pnl": float(payoff_at_price(legs, price, fees)),
                "fees_assumption": "entry_fees_only; closing fees not modeled",
            }
        )
    scenarios.append(
        {
            "kind": "expiration_probability",
            "horizon_label": "expiration",
            "available": expiration_probability.available,
            "probability_of_profit": expiration_probability.probability_of_profit,
            "reason": expiration_probability.reason,
            "fees_assumption": "entry_fees_only; closing fees not modeled",
        }
    )
    if _utc(horizon) is None or _utc(expiry) is None or _utc(horizon) >= _utc(expiry):
        return scenarios
    base = mark_to_market_payoff(
        legs,
        spot,
        horizon,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        fees=fees,
        steps=60,
    )
    remaining = max(0.0, _year_fraction(horizon, expiry))
    scenarios.append(
        {
            "kind": "preexpiry_mark_to_market",
            "scenario": "base",
            "horizon_label": "intraday_exit" if _utc(horizon) != _utc(expiry) else "preexpiry",
            "underlying_price": float(spot),
            "pnl": None if not _finite(base) else float(base),
            "time_to_expiry_years": float(remaining),
            "fees_assumption": "entry_fees_only; closing fees not modeled",
        }
    )
    for spot_factor in (0.90, 1.10):
        price = spot * spot_factor
        pnl = mark_to_market_payoff(
            legs,
            price,
            horizon,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            fees=fees,
            steps=60,
        )
        scenarios.append(
            {
                "kind": "preexpiry_sensitivity",
                "sensitivity": "spot",
                "factor": float(spot_factor),
                "horizon_label": "preexpiry",
                "underlying_price": float(price),
                "pnl": None if not _finite(pnl) else float(pnl),
                "time_to_expiry_years": float(remaining),
            }
        )
    for iv_factor in (0.80, 1.20):
        pnl = mark_to_market_payoff(
            legs,
            spot,
            horizon,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            fees=fees,
            steps=60,
            iv_scale=iv_factor,
        )
        scenarios.append(
            {
                "kind": "preexpiry_sensitivity",
                "sensitivity": "iv",
                "factor": float(iv_factor),
                "horizon_label": "preexpiry",
                "underlying_price": float(spot),
                "pnl": None if not _finite(pnl) else float(pnl),
                "time_to_expiry_years": float(remaining),
            }
        )
    for time_factor in (0.50, 1.50):
        pnl = mark_to_market_payoff(
            legs,
            spot,
            horizon,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            fees=fees,
            steps=60,
            time_scale=time_factor,
        )
        scenarios.append(
            {
                "kind": "preexpiry_sensitivity",
                "sensitivity": "time_to_expiry",
                "factor": float(time_factor),
                "horizon_label": "preexpiry",
                "underlying_price": float(spot),
                "pnl": None if not _finite(pnl) else float(pnl),
                "time_to_expiry_years": float(remaining * time_factor),
            }
        )
    return scenarios


def _strategy_warnings(strategy: str, legs: Sequence[OptionLeg], snapshot: QuoteSnapshot) -> list[str]:
    warnings = [
        "Risk-neutral IV probabilities are model estimates and are not real-world success probabilities.",
        "Entry uses displayed bid/ask; slippage, spread changes, and exit liquidity are not modeled.",
        "Expiration exercise, assignment, and settlement mechanics are not modeled in the payoff chart.",
        "P&L and probabilities subtract entry fees only; closing commissions/fees are not modeled.",
    ]
    if any(leg.side == "sell" and leg.right != "stock" and leg.exercise_style == "american" for leg in legs):
        warnings.append("Short American options can be assigned early, especially around ex-dividend dates.")
    if strategy == "uncovered_call":
        warnings.append("Uncovered call loss is unbounded as the underlying rises.")
    if strategy == "uncovered_put":
        warnings.append("Uncovered put requires broker margin and can lose substantially if the underlying falls.")
    if snapshot.stale:
        warnings.append("The supplied quote snapshot is marked stale.")
    # Provider diagnostics (quote timing, contracts outside this trade) are not
    # risks of the trade itself; the model receives the same filtered list.
    from .advisor import _model_warnings
    warnings.extend(str(item) for item in _model_warnings(snapshot.warnings) if str(item))
    return list(dict.fromkeys(warnings))


def _default_strategies(direction: str) -> list[str]:
    return {
        "bullish": ["bull_call_debit", "bull_put_credit", "long_call", "covered_call"],
        "bearish": ["bear_put_debit", "bear_call_credit", "long_put", "uncovered_call"],
        "neutral": ["iron_condor", "short_strangle", "covered_call", "cash_secured_put"],
        # Short straddles/strangles are neutral (short-volatility) views.
        "volatile": ["long_straddle", "long_strangle"],
        "auto": ["bull_call_debit", "bear_put_debit", "long_straddle", "iron_condor"],
    }.get(direction, ["long_call", "long_put", "iron_condor", "long_straddle"])


def _normalise_requested_strategies(request: TradeAdviceRequest) -> list[str]:
    requested = request.strategies or _default_strategies(request.direction)
    result: list[str] = []
    for value in requested:
        key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        canonical = _ALIASES.get(key)
        if canonical is not None and canonical not in result:
            result.append(canonical)
    return result


def _candidate_reasons(strategy: str, request: TradeAdviceRequest, payoff: PayoffAnalysis) -> list[str]:
    item = _CATALOG_BY_ID.get(strategy, {"title": strategy})
    reasons = [str(item.get("description", ""))]
    if request.direction != "auto":
        reasons.append(f"Requested market view: {request.direction}.")
    if payoff.breakevens:
        reasons.append("Break-even levels come from the exact expiration payoff, including displayed fees.")
    if payoff.capital_required is None:
        reasons.append("Position size is withheld because required capital is not known from the request.")
    return [reason for reason in reasons if reason]


def _build_candidate(
    strategy: str,
    horizon_label: str,
    group_expiry: datetime,
    quotes: Sequence[OptionQuote],
    snapshot: QuoteSnapshot,
    request: TradeAdviceRequest,
    as_of: datetime,
    horizon_at: datetime,
) -> StrategyCandidate | None:
    legs = _construct_strategy(strategy, quotes, snapshot, request)
    if not legs:
        return None
    return _candidate_from_legs(strategy, horizon_label, group_expiry, legs, quotes, snapshot, request,
                                as_of, horizon_at)


def _candidate_from_legs(
    strategy: str,
    horizon_label: str,
    group_expiry: datetime,
    legs: list[OptionLeg],
    quotes: Sequence[OptionQuote],
    snapshot: QuoteSnapshot,
    request: TradeAdviceRequest,
    as_of: datetime,
    horizon_at: datetime,
) -> StrategyCandidate | None:
    fees = fees_for_legs(legs, request.fee_per_contract)
    preliminary = analyze_payoff(legs, fees=fees)
    capital, capital_note = _capital_for_strategy(strategy, legs, preliminary, snapshot, request, fees)
    payoff = analyze_payoff(
        legs,
        fees=fees,
        capital_required=capital,
        capital_note=capital_note,
        assignment_note="Short American legs may be assigned before expiration."
        if any(leg.side == "sell" and leg.right != "stock" and leg.exercise_style == "american" for leg in legs)
        else "",
    )
    if payoff.gain_bound == "bounded" and (payoff.max_gain is None or payoff.max_gain <= _MONEY_EPS):
        # E.g. sub-cent credits against fees; such a structure can only lose.
        return None
    all_expiry_verified = all(
        quote.expiry_verified for quote in quotes if quote.contract_id in {leg.contract_id for leg in legs if leg.right != "stock"}
    )
    probability = estimate_probability(
        legs,
        snapshot.spot,
        horizon_at,
        as_of=as_of,
        risk_free_rate=request.risk_free_rate,
        dividend_yield=request.dividend_yield,
        fees=fees,
        expiry_verified=all_expiry_verified,
        steps=60,
    )
    expiration_probability = estimate_probability(
        legs,
        snapshot.spot,
        group_expiry,
        as_of=as_of,
        risk_free_rate=request.risk_free_rate,
        dividend_yield=request.dividend_yield,
        fees=fees,
        expiry_verified=all_expiry_verified,
        steps=60,
    )
    scenarios = _scenario_bundle(
        legs,
        snapshot.spot,
        horizon_at,
        group_expiry,
        as_of=as_of,
        risk_free_rate=request.risk_free_rate,
        dividend_yield=request.dividend_yield,
        fees=fees,
        expiration_probability=expiration_probability,
    )
    warnings = _strategy_warnings(strategy, legs, snapshot)
    if not probability.available:
        warnings.append(f"Probability unavailable: {probability.reason}")
    # Evidence confidence describes support for the trade thesis; verified
    # quotes are a precondition, not evidence. Only the model's assessment of
    # the supplied research can raise it.
    confidence = "low"
    quantity: int | None = None
    stock_leg = next((leg for leg in legs if leg.right == "stock"), None)
    owned_cover = strategy == "covered_call" and stock_leg is not None and stock_leg.existing
    if request.allocation is not None and capital is not None and capital > _EPS:
        quantity = max(0, int(float(request.allocation) // capital))
    if owned_cover:
        # Existing holdings are a known share-coverage constraint, unlike an
        # invented account-value or risk cap; with zero fees they are the only one.
        covered = int(request.existing_shares) // int(stock_leg.quantity)
        quantity = covered if quantity is None else min(quantity, covered)
    title = "Your plan" if strategy == PLAN_STRATEGY else _CATALOG_BY_ID.get(strategy, {}).get("title", strategy)
    return StrategyCandidate(
        strategy=strategy,
        title=f"{title} ({'intraday' if horizon_label == 'intraday' else 'swing'})",
        underlying=snapshot.underlying,
        horizon=horizon_label,
        snapshot_id=snapshot.id,
        legs=legs,
        payoff=payoff,
        probability=probability,
        scenarios=scenarios,
        quantity_for_allocation=quantity,
        evidence_confidence=confidence,
        reasons=_candidate_reasons(strategy, request, payoff),
        warnings=warnings,
        entry_conditions=["Use only if the displayed bid/ask remains executable and the quote timestamp is acceptable."],
        invalidation="Reassess if the underlying moves through the modeled break-even or the option chain becomes stale.",
        exit_conditions=[
            "For intraday candidates, evaluate exit before the regular close.",
            "For swing candidates, reassess before expiration and after material volatility or dividend changes.",
        ],
    )


def reprice_candidate(
    candidate: StrategyCandidate,
    snapshot: QuoteSnapshot,
    request: TradeAdviceRequest,
) -> StrategyCandidate | None:
    """Recalculate a candidate on its own contracts from a newer snapshot.

    Rebuilding by strategy would re-select strikes by distance to spot, so a
    tiny spot move could swap the contracts. Returns None when any leg lacks a
    valid quote in the snapshot.
    """

    if str(snapshot.underlying).upper() != str(candidate.underlying).upper():
        return None
    as_of, groups = _valid_quote_groups(snapshot, request.model_copy(update={"expiry": None}))
    if as_of is None:
        return None
    by_id = {quote.contract_id: quote for group in groups.values() for quote in group}
    legs: list[OptionLeg] = []
    for leg in candidate.legs:
        if leg.right == "stock":
            legs.append(_make_stock_leg(snapshot, request.existing_shares, leg.quantity))
            continue
        quote = by_id.get(leg.contract_id)
        if quote is None or quote.right != leg.right or float(quote.strike) != float(leg.strike or 0) \
                or _utc(quote.expiry) != _utc(leg.expiry) or int(quote.multiplier) != int(leg.multiplier):
            return None
        legs.append(_make_option_leg(quote, leg.side, leg.quantity))
    expiry = _common_expiry(legs)
    if expiry is None or expiry not in groups:
        return None
    if candidate.horizon == "intraday":
        regular_close = _regular_close_after(as_of)
        if regular_close is None:
            return None
        horizon_at = min(regular_close, expiry)
    else:
        horizon_at = expiry
    if horizon_at <= as_of:
        return None
    rebuilt = _candidate_from_legs(candidate.strategy, candidate.horizon, expiry, legs, groups[expiry],
                                   snapshot, request, as_of, horizon_at)
    return None if rebuilt is None else rebuilt.model_copy(update={"id": candidate.id})


def _horizon_slots(strategy_ids: Sequence[str], requested_horizon: str, close_known: bool) -> list[tuple[str, str]]:
    """Allocate four slots with strategy diversity and both-horizon coverage."""

    if requested_horizon != "both":
        return [(strategy, requested_horizon) for strategy in strategy_ids[:4]]
    if not close_known:
        return [(strategy, "swing") for strategy in strategy_ids[:4]]
    if len(strategy_ids) <= 2:
        return [
            (strategy, horizon)
            for horizon in ("intraday", "swing")
            for strategy in strategy_ids
        ][:4]
    return [
        (strategy, "intraday" if index % 2 == 0 else "swing")
        for index, strategy in enumerate(strategy_ids[:4])
    ]


def price_plan(snapshot: QuoteSnapshot, request: TradeAdviceRequest) -> tuple[StrategyCandidate | None, str]:
    """Price the user's own legs on their exact contracts; returns (candidate, reason if not priced)."""

    if not request.plan_legs:
        return None, ""
    if str(snapshot.underlying).upper() != str(request.ticker).upper():
        return None, "The quote snapshot is for a different stock."
    as_of, groups = _valid_quote_groups(snapshot, request.model_copy(update={"expiry": None}))
    if as_of is None:
        return None, "No valid option quotes are available."
    legs: list[OptionLeg] = []
    for leg in request.plan_legs:
        if leg.right == "stock":
            if leg.side != "buy":
                return None, "Short stock legs are not supported; plans may buy or use owned shares."
            legs.append(_make_stock_leg(snapshot, request.existing_shares, leg.quantity))
            continue
        quote = next((item for expiry, quotes in groups.items() if expiry.date() == leg.expiry
                      for item in quotes if item.right == leg.right
                      and abs(float(item.strike) - float(leg.strike)) < 1e-6), None)
        if quote is None:
            return None, (f"No valid two-sided quote for the {leg.expiry} {leg.strike:g} {leg.right}; "
                          "check the strike and expiry exist and are quoted.")
        legs.append(_make_option_leg(quote, leg.side, leg.quantity))
    if all(leg.right == "stock" for leg in legs):
        return None, "A plan needs at least one option leg."
    expiry = _common_expiry(legs)
    if expiry is None:
        return None, "Plan option legs must share one expiry (calendar spreads are not modeled)."
    regular_close = _regular_close_after(as_of)
    same_day = regular_close is not None and expiry <= regular_close + _LATE_CLOSE_ALLOWANCE
    horizon = "intraday" if request.horizon == "intraday" or (request.horizon == "both" and same_day) else "swing"
    horizon_at = expiry
    if horizon == "intraday":
        if regular_close is None:
            return None, "An intraday plan needs an open regular session."
        horizon_at = min(regular_close, expiry)
    if horizon_at <= as_of:
        return None, "The plan's expiry has passed."
    candidate = _candidate_from_legs(PLAN_STRATEGY, horizon, expiry, legs, groups[expiry], snapshot, request,
                                     as_of, horizon_at)
    if candidate is None:
        return None, "The plan can only lose after fees at current quotes."
    return candidate, ""


def build_candidates(snapshot: QuoteSnapshot, request: TradeAdviceRequest) -> list[StrategyCandidate]:
    """Build at most four quote-grounded, horizon-aware strategy candidates.

    A user's own plan (``plan_legs``) is priced first and kept beside up to
    three calculated alternatives.
    """

    if str(snapshot.underlying).upper() != str(request.ticker).upper():
        return []
    plan, _reason = price_plan(snapshot, request)
    if plan is not None:
        return [plan, *build_candidates(snapshot, request.model_copy(update={"plan_legs": []}))[:3]]
    as_of, groups = _valid_quote_groups(snapshot, request)
    if as_of is None or not groups:
        return []
    strategy_ids = _normalise_requested_strategies(request)
    if not strategy_ids:
        return []
    regular_close = _regular_close_after(as_of)
    if request.horizon == "intraday" and regular_close is None:
        return []
    ordered_expiries = list(groups)
    candidates: list[StrategyCandidate] = []

    for strategy, horizon_label in _horizon_slots(strategy_ids, request.horizon, regular_close is not None):
        if horizon_label == "intraday" and regular_close is None:
            continue
        if horizon_label == "intraday":
            expiry_choices = ordered_expiries
        else:
            # A late-close (16:15) same-day expiry is still today's contract, not a swing.
            later = [expiry for expiry in ordered_expiries
                     if regular_close is not None and expiry > regular_close + _LATE_CLOSE_ALLOWANCE]
            expiry_choices = later or ordered_expiries
        built: StrategyCandidate | None = None
        for expiry in expiry_choices:
            if horizon_label == "intraday":
                assert regular_close is not None
                horizon_at = min(regular_close, expiry)
            else:
                horizon_at = expiry
            if horizon_at <= as_of:
                continue
            built = _build_candidate(
                strategy,
                horizon_label,
                expiry,
                groups[expiry],
                snapshot,
                request,
                as_of,
                horizon_at,
            )
            if built is not None:
                break
        if built is not None:
            candidates.append(built)
        if len(candidates) >= 4:
            break
    return candidates[:4]


__all__ = [
    "analyze_payoff",
    "black_scholes_price",
    "build_candidates",
    "price_plan",
    "compute_payoff_analysis",
    "crr_binomial_price",
    "estimate_probability",
    "fees_for_legs",
    "mark_to_market_payoff",
    "payoff_at_price",
    "probability_of_profit",
    "reprice_candidate",
    "risk_neutral_expected_payoff",
    "risk_neutral_profit_probability",
    "strategy_catalog",
    "strategy_payoff",
]
