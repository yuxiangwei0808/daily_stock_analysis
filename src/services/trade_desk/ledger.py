"""Fill-based positions. Alerts and model marks never create or close holdings."""
from __future__ import annotations

from datetime import datetime

from .models import TradeFill, utcnow


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def sort_fills(fills):
    """Order by actual time, then recorded sequence; ISO strings do not sort by time."""
    return sorted(fills, key=lambda row: (_time(row["filled_at"]), row.get("sequence", 0), row["id"]))


def pending_share_deliveries(plan, fills):
    """Shares owed by recorded exercises/assignments, less recorded deliveries."""
    specs = {item["contract_id"]: item for item in plan["candidate"]["legs"]}
    obligations = {}
    for fill in fills:
        if fill["intent"] not in {"assignment", "exercise"}:
            continue
        if fill["contract_id"] == plan["candidate"]["underlying"]:
            side, quantity = fill["side"], -fill["quantity"]
        else:
            spec = specs[fill["contract_id"]]
            buy_shares = (spec["right"] == "call") == (fill["intent"] == "exercise")
            side, quantity = "buy" if buy_shares else "sell", fill["quantity"] * spec["multiplier"]
        key = (fill["intent"], side)
        obligations[key] = obligations.get(key, 0) + quantity
    return obligations


def position_from_fills(plan, fills, snapshot=None, now=None):
    now = now or utcnow()
    specs = {item["contract_id"]: item for item in plan["candidate"]["legs"]}
    underlying = plan["candidate"]["underlying"]
    if any(item["right"] == "stock" and (item["contract_id"] != underlying or item["multiplier"] != 1)
           for item in specs.values()):
        raise ValueError("Stock leg metadata must match the underlying with a multiplier of one")
    specs.setdefault(underlying, {"contract_id": underlying, "right": "stock", "multiplier": 1,
                                  "quantity": 0, "side": "buy", "entry_price": 0})
    balances = {}
    # Explicitly owned shares are economic exposure and call coverage, but
    # they are not a position opened by this plan.
    baseline = {}
    for code, spec in specs.items():
        owned = spec.get("existing", False) and spec["right"] == "stock"
        existing_quantity = plan.get("existing_share_quantity")
        if existing_quantity is None:
            existing_quantity = spec["quantity"]
        balances[code] = {"quantity": existing_quantity if owned else 0,
                          "average": spec["entry_price"] if owned else 0.0}
        if owned:
            baseline[code] = existing_quantity
    realized = 0.0
    fees = 0.0
    reconciliation = (plan.get("status") == "reconciliation_required"
                      or any(pending_share_deliveries(plan, fills).values()))
    for fill in sort_fills(fills):
        code = fill["contract_id"]
        if code not in specs:
            raise ValueError("Fill contract is not part of this plan")
        spec, balance = specs[code], balances[code]
        delta = fill["quantity"] * (1 if fill["side"] == "buy" else -1)
        previous, average = balance["quantity"], balance["average"]
        if previous and previous * delta < 0:
            closed = min(abs(previous), abs(delta))
            realized += (fill["price"] - average) * closed * spec["multiplier"] * (1 if previous > 0 else -1)
        if not previous or previous * delta > 0:
            balance["average"] = ((abs(previous) * average + abs(delta) * fill["price"])
                                  / (abs(previous) + abs(delta)))
        elif abs(delta) > abs(previous):
            balance["average"] = fill["price"]
        balance["quantity"] += delta
        fees += fill["fees"]
        realized -= fill["fees"]
        if fill["intent"] in {"assignment", "exercise"}:
            reconciled_at = plan.get("reconciled_at")
            if not reconciled_at or _time(fill["filled_at"]) > _time(reconciled_at):
                reconciliation = True
    quotes = {item.contract_id: item for item in snapshot.options} if snapshot else {}
    snapshot_valid = False
    if snapshot:
        from .quality import quote_age_ok, quote_matches_leg, snapshot_fresh
        snapshot_valid = snapshot.underlying == underlying and snapshot_fresh(snapshot, now=now)
    legs, total_unrealized, all_marked = [], 0.0, True
    for code, balance in balances.items():
        quantity, average = balance["quantity"], balance["average"]
        if not quantity:
            continue
        spec = specs[code]
        mark = None
        if spec.get("expiry") and _time(spec["expiry"]) <= now:
            reconciliation = True
        if snapshot_valid:
            if spec["right"] == "stock":
                mark = snapshot.bid if quantity > 0 else snapshot.ask
            elif code in quotes:
                quote = quotes[code]
                if (quote_matches_leg(snapshot, quote, spec) and quote_age_ok(quote.quoted_at, now)
                        and quote.expiry > now and quote.bid <= quote.ask and quote.bid > 0):
                    mark = quote.bid if quantity > 0 else quote.ask
        pnl = (mark - average) * quantity * spec["multiplier"] if mark is not None else None
        if pnl is None:
            all_marked = False
        else:
            total_unrealized += pnl
        legs.append({"contract_id": code, "right": spec["right"], "quantity": abs(quantity),
                     "signed_quantity": quantity, "multiplier": spec["multiplier"],
                     "strike": spec.get("strike"), "expiry": spec.get("expiry"),
                     "exercise_style": spec.get("exercise_style", "american"),
                     "average_price": average, "mark_price": mark, "unrealized_pnl": pnl})
    changed = any(leg["signed_quantity"] != baseline.get(leg["contract_id"], 0) for leg in legs)
    status = ("reconciliation_required" if reconciliation else "watching" if not fills
              else "open" if changed else "closed")
    return {"plan_id": plan["id"], "ledger": plan["ledger"], "underlying": underlying,
            "status": status, "legs": legs, "realized_pnl": round(realized, 4),
            "unrealized_pnl": round(total_unrealized, 4) if all_marked else None,
            "valuation_status": "current" if all_marked and legs else "unavailable" if legs else "no_position",
            "valuation_at": snapshot.quoted_at.isoformat() if snapshot_valid else None,
            "fees": fees, "plan": plan}


def validate_fills(plan, prior, incoming):
    specs = {item["contract_id"]: item for item in plan["candidate"]["legs"]}
    underlying = plan["candidate"]["underlying"]
    position = position_from_fills(plan, prior)
    quantities = {leg["contract_id"]: leg["signed_quantity"] for leg in position["legs"]}
    newest = max((_time(item["filled_at"]) for item in prior), default=None)
    share_obligations = pending_share_deliveries(plan, prior)
    for raw in incoming:
        fill = TradeFill.model_validate(raw)
        if fill.plan_id != plan["id"]:
            raise ValueError("Fill belongs to another plan")
        if fill.filled_at > utcnow():
            raise ValueError("Actual fill time cannot be in the future")
        if newest and fill.filled_at < newest:
            raise ValueError("Record fills in chronological order")
        newest = fill.filled_at
        if fill.contract_id not in specs:
            if fill.contract_id != underlying or fill.intent not in {"assignment", "exercise", "close"}:
                raise ValueError("Fill contract is not part of this plan")
        quantity = quantities.get(fill.contract_id, 0)
        delta = fill.quantity * (1 if fill.side == "buy" else -1)
        if fill.intent == "close":
            if not quantity or delta * quantity >= 0 or abs(delta) > abs(quantity):
                raise ValueError("Closing fill exceeds or increases the recorded position")
        elif fill.intent == "open":
            spec = specs[fill.contract_id]
            if spec["side"] != fill.side:
                raise ValueError("Opening side differs from the selected strategy")
            if quantity and quantity * delta < 0:
                raise ValueError("Use a closing fill to reduce a position")
        elif fill.intent in {"assignment", "exercise"}:
            if fill.contract_id == underlying:
                remaining = share_obligations.get((fill.intent, fill.side), 0)
                if fill.quantity > remaining:
                    raise ValueError("Record the matching option exercise/assignment first; resulting share side or quantity is unmatched")
            else:
                expected_sign = 1 if fill.intent == "exercise" else -1
                if (quantity * expected_sign <= 0 or delta * quantity >= 0
                        or abs(delta) > abs(quantity)):
                    raise ValueError("Exercise requires recorded long options; assignment requires recorded short options, closed with the matching side and quantity")
            for key, amount in pending_share_deliveries(plan, [raw]).items():
                share_obligations[key] = share_obligations.get(key, 0) + amount
        quantities[fill.contract_id] = quantity + delta
    if plan['ledger'] == 'paper' and plan['candidate']['strategy'] == 'covered_call':
        available_shares = quantities.get(underlying, 0)
        required_shares = sum(max(0, -quantities.get(code, 0)) * spec['multiplier']
                              for code, spec in specs.items() if spec['right'] == 'call')
        if required_shares > available_shares:
            raise ValueError('The recorded shares do not cover the resulting paper call position')
