"""Trade Desk calculation tool within the existing owned-process boundary."""
from __future__ import annotations

import json
import re
from dataclasses import replace

from src.agent.codex_tool_process import CodexToolProcessRunner
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy, ToolRegistry

from .models import QuoteSnapshot, StrategyCandidate, TradeAdviceRequest


def mentioned_amounts(message):
    """Dollar amounts the user wrote, e.g. "5000.", "$1,500" or "$5k"."""
    amounts = set()
    for number, suffix in re.findall(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)([kK])?(?!\w|\.\d)", message):
        value = float(number.replace(",", ""))
        amounts.add(value * 1000 if suffix else value)
    return amounts


def effective_request(request, changes, original_allocation=None):
    if isinstance(changes, dict):
        changes = dict(changes)
        for key in ("ticker", "data_mode"):
            if key in changes:
                if changes.pop(key) != getattr(request, key):
                    raise ValueError("Ticker and data mode must be changed in the form")
    allowed = {"allocation", "direction", "horizon", "expiry", "strategies", "existing_shares",
               "fee_per_contract", "risk_free_rate", "dividend_yield", "margin_per_unit", "plan_legs"}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError("Only comparison parameters may change")
    if changes.get("plan_legs") and "expiry" not in changes:
        changes["expiry"] = None  # let the new plan's own expiry apply
    effective = TradeAdviceRequest.model_validate({**request.model_dump(), **changes})
    if effective.plan_legs != request.plan_legs:
        # The model may transcribe the user's plan, never invent one.
        written = {float(value) for value in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)", request.message)}
        if any(leg.right != "stock" and float(leg.strike) not in written for leg in effective.plan_legs):
            raise ValueError("Plan legs must use strikes the user wrote")
    form_only = {"existing_shares", "fee_per_contract", "risk_free_rate", "dividend_yield", "margin_per_unit"}
    if any(getattr(effective, key) != getattr(request, key) for key in form_only):
        raise ValueError("Ownership, margin, and pricing assumptions must be changed in the form")
    if effective.allocation != request.allocation and effective.allocation is not None:
        # Returning to the form's own allocation is always the user's input.
        if effective.allocation != original_allocation and effective.allocation not in mentioned_amounts(request.message):
            raise ValueError("A changed allocation must be supplied by the user")
    return effective


def comparison_registry(handler):
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="compare_trade_strategies",
        description="Recalculate supplied options strategies for the selected stock. Reflect only changes requested by the user; never invent ownership, allocation, margin or pricing assumptions. To price the user's own trade, set changes.plan_legs to the legs they wrote: [{side: buy|sell, right: call|put|stock, quantity, strike, expiry: YYYY-MM-DD}].",
        parameters=[ToolParameter("changes", "object", "Requested comparison parameter changes", required=False)],
        handler=handler, category="analysis",
        policy=ToolPolicy.declared(read_only=True, cancellation_safe=True),
    ))
    return registry


def execute_trade_tool(tool_name, arguments, context):
    """Top-level, spawn-safe handler; it has no order or ledger write interface."""
    from src.agent.tool_surface import ToolSurface
    from .analytics import build_candidates
    from .providers import build_provider
    from .quality import snapshot_fresh

    request = TradeAdviceRequest.model_validate(context.audit_context["trade_request"])
    original = QuoteSnapshot.model_validate(context.audit_context["trade_snapshot"])
    original_allocation = context.audit_context.get("trade_original_allocation")

    def compare(changes=None):
        effective = effective_request(request, changes or {}, original_allocation)
        if effective.expiry == request.expiry and effective.plan_legs == request.plan_legs \
                and snapshot_fresh(original):
            snapshot = original
        else:
            provider = build_provider(effective.data_mode)
            try:
                snapshot = provider.snapshot(effective.ticker, expiry=effective.expiry,
                                             required_contracts=effective.plan_contracts())
                snapshot.evidence.extend(original.evidence)
            finally:
                provider.close()
        if snapshot.underlying != effective.ticker or snapshot.mode != effective.data_mode:
            raise ValueError("Provider snapshot does not match the requested stock and data mode")
        if snapshot.mode == "live" and not snapshot_fresh(snapshot):
            raise ValueError("Fresh verified data is required for recalculation")
        candidates = build_candidates(snapshot, effective)
        return {"request": effective.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates]}

    return ToolSurface(comparison_registry(compare)).execute_tool(tool_name, arguments, context)


class TradeToolRunner(CodexToolProcessRunner):
    def __init__(self, request, snapshot, receive):
        super().__init__(worker=execute_trade_tool)
        self.original_allocation = request.allocation
        self.request = request
        self.market = snapshot
        self.receive = receive

    def execute(self, tool_name, arguments, context):
        # Context is set by the server, not by the model's tool arguments.
        context = replace(context, audit_context={
            "trade_request": self.request.model_dump(mode="json"),
            "trade_snapshot": self.market.model_dump(mode="json"),
            "trade_original_allocation": self.original_allocation,
        })
        result = super().execute(tool_name, arguments, context)
        if result.get("ok"):
            payload = json.loads(result["result_text"])
            request = TradeAdviceRequest.model_validate(payload["request"])
            snapshot = QuoteSnapshot.model_validate(payload["snapshot"])
            candidates = [StrategyCandidate.model_validate(item) for item in payload["candidates"]]
            self.receive(request, snapshot, candidates)
            self.request = request
            self.market = snapshot
        return result
