"""Shared Trade Desk contracts. Monetary values are USD; datetimes are aware."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def identity() -> str:
    return uuid4().hex


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value):
        return value.astimezone(timezone.utc) if isinstance(value, datetime) else value


class OptionQuote(Model):
    contract_id: str
    underlying: str
    right: Literal["call", "put"]
    strike: float = Field(gt=0)
    expiry: AwareDatetime
    expiry_verified: bool = False
    multiplier: int = Field(default=100, gt=0)
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    quoted_at: AwareDatetime
    iv: Optional[float] = Field(default=None, gt=0)
    bid_size: Optional[int] = Field(default=None, ge=0)
    ask_size: Optional[int] = Field(default=None, ge=0)
    volume: Optional[int] = Field(default=None, ge=0)
    open_interest: Optional[int] = Field(default=None, ge=0)
    standard: bool = True
    exercise_style: Literal["american", "european"] = "american"


class QuoteSnapshot(Model):
    id: str = Field(default_factory=identity)
    underlying: str
    spot: float = Field(gt=0)
    bid: Optional[float] = Field(default=None, ge=0)
    ask: Optional[float] = Field(default=None, ge=0)
    quoted_at: AwareDatetime
    received_at: AwareDatetime = Field(default_factory=utcnow)
    provider: str
    mode: Literal["live", "replay"]
    session: str
    source_verified: bool = False
    stale: bool = False
    options: list[OptionQuote] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    bars: list[dict[str, Any]] = Field(default_factory=list)


class OptionLeg(Model):
    contract_id: str
    right: Literal["call", "put", "stock"]
    side: Literal["buy", "sell"]
    quantity: int = Field(default=1, gt=0)
    strike: Optional[float] = Field(default=None, gt=0)
    expiry: Optional[AwareDatetime] = None
    multiplier: int = Field(default=100, gt=0)
    entry_price: float = Field(ge=0)
    iv: Optional[float] = Field(default=None, gt=0)
    existing: bool = False
    exercise_style: Literal["american", "european"] = "american"


class PayoffAnalysis(Model):
    entry_debit: float
    fees: float = 0
    max_gain: Optional[float] = None
    max_loss: Optional[float] = None
    gain_bound: Literal["bounded", "unbounded", "unknown"] = "unknown"
    loss_bound: Literal["bounded", "unbounded", "unknown"] = "unknown"
    breakevens: list[float] = Field(default_factory=list)
    capital_required: Optional[float] = None
    capital_note: str = ""
    assignment_note: str = ""
    points: list[dict[str, float]] = Field(default_factory=list)


class ProbabilityEstimate(Model):
    available: bool = False
    probability_of_profit: Optional[float] = Field(default=None, ge=0, le=1)
    method: str = "iv_lognormal_risk_neutral"
    horizon_at: Optional[AwareDatetime] = None
    horizon_label: str = "expiration"
    reason: str = ""
    assumptions: dict[str, Any] = Field(default_factory=dict)
    sensitivity: list[dict[str, Any]] = Field(default_factory=list)


class StrategyCandidate(Model):
    id: str = Field(default_factory=identity)
    strategy: str
    title: str
    underlying: str
    horizon: Literal["intraday", "swing"]
    snapshot_id: str
    legs: list[OptionLeg]
    payoff: PayoffAnalysis
    probability: ProbabilityEstimate = Field(default_factory=ProbabilityEstimate)
    scenarios: list[dict[str, Any]] = Field(default_factory=list)
    quantity_for_allocation: Optional[int] = None
    evidence_confidence: Literal["low", "medium", "high"] = "low"
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    entry_conditions: list[str] = Field(default_factory=list)
    invalidation: str = ""
    exit_conditions: list[str] = Field(default_factory=list)
    calculation_version: str = "trade-desk-v1"


PLAN_STRATEGY = "custom"
_PLAN_LINE = re.compile(
    r"^\s*(buy|sell)\s+(?:(\d+)\s*x?\s+)?(?:(call|put)\s+\$?(\d+(?:\.\d+)?)\s+(\d{4}-\d{2}-\d{2})|(stock|shares?))\s*$",
    re.IGNORECASE)


class PlanLeg(Model):
    """One leg of a user's own trade plan, priced from the live chain."""
    side: Literal["buy", "sell"]
    right: Literal["call", "put", "stock"]
    quantity: int = Field(default=1, ge=1, le=10000)
    strike: Optional[float] = Field(default=None, gt=0)
    expiry: Optional[date] = None

    @model_validator(mode="after")
    def _option_fields(self):
        if self.right != "stock" and (self.expiry is None or self.strike is None):
            raise ValueError("Option legs need a strike and an expiry")
        return self

    def spec(self) -> str:
        """Provider lookup key for an option leg: ``right:strike:YYYY-MM-DD``."""
        return f"{self.right}:{self.strike:g}:{self.expiry.isoformat()}"


def parse_plan_legs(value: Any) -> Any:
    """Accept legs as objects or as lines like ``buy 1 call 230 2026-10-17`` / ``sell 100 stock``."""
    if not isinstance(value, str):
        return value
    legs = []
    for line in re.split(r"[\n;]+", value):
        if not line.strip():
            continue
        match = _PLAN_LINE.match(line)
        if not match:
            raise ValueError(f"Cannot read plan leg '{line.strip()}'; use e.g. 'buy 1 call 230 2026-10-17'")
        side, quantity, right, strike, expiry, stock = match.groups()
        legs.append({"side": side.lower(), "quantity": int(quantity or (100 if stock else 1)),
                     "right": "stock" if stock else right.lower(),
                     "strike": None if stock else float(strike), "expiry": expiry})
    return legs


class TradeAdviceRequest(Model):
    ticker: str
    allocation: Optional[float] = Field(default=None, gt=0)
    direction: Literal["auto", "bullish", "bearish", "neutral", "volatile"] = "auto"
    horizon: Literal["intraday", "swing", "both"] = "both"
    data_mode: Literal["live", "replay"] = "live"
    expiry: Optional[date] = None
    strategies: list[str] = Field(default_factory=list)
    message: str = Field(default="", max_length=6000)
    parent_advice_id: Optional[str] = None
    source_report_id: Optional[int] = None
    existing_shares: int = Field(default=0, ge=0)
    fee_per_contract: float = Field(default=0.65, ge=0)
    risk_free_rate: float = Field(default=0.0, ge=-0.1, le=1)
    dividend_yield: float = Field(default=0.0, ge=0, le=1)
    margin_per_unit: Optional[float] = Field(default=None, gt=0)
    plan_legs: list[PlanLeg] = Field(default_factory=list, max_length=4)

    @field_validator("plan_legs", mode="before")
    @classmethod
    def _read_plan_legs(cls, value):
        return parse_plan_legs(value)

    @model_validator(mode="after")
    def _plan_expiry(self):
        # Load the plan's own expiry and compare alternatives on the same date.
        expiries = {leg.expiry for leg in self.plan_legs if leg.right != "stock"}
        if self.expiry is None and len(expiries) == 1:
            self.expiry = expiries.pop()
        return self

    def plan_contracts(self) -> list[str]:
        return [leg.spec() for leg in self.plan_legs if leg.right != "stock"]

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        value = value.strip().upper()
        if value.startswith("US."):
            value = value[3:]
        elif value.endswith(".US"):
            value = value[:-3]
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value):
            raise ValueError("Enter an exact US stock or ETF ticker")
        return value


class TradePlan(Model):
    id: str = Field(default_factory=identity)
    advice_id: str
    candidate: StrategyCandidate
    ledger: Literal["paper", "manual_live"] = "paper"
    status: str = "watching"
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)
    data_mode: Literal["live", "replay"] = "live"
    notes: str = ""
    monitoring: bool = True
    trigger_price: Optional[float] = Field(default=None, gt=0)
    trigger_direction: Literal["above", "below"] = "above"
    invalidation_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    exit_at: Optional[AwareDatetime] = None
    reconciled_at: Optional[AwareDatetime] = None
    existing_share_quantity: Optional[int] = Field(default=None, ge=0)


class TradeFill(Model):
    id: str = Field(default_factory=identity)
    plan_id: str
    contract_id: str
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0, ge=0)
    filled_at: AwareDatetime
    intent: Literal["open", "close", "assignment", "exercise"] = "open"
    note: str = ""
