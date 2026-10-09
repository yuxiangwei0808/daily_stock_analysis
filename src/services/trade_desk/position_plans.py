"""One plan per position you hold — an action, a stop and a target — shared by the Home panel,
the Holdings tab and the Discord cards, so they never disagree.

Where a level comes from, first wins:
- ``you``: your own active alerts on the position (price at or below / above, P&L levels);
- ``review``: the daily review — after the close (``REVIEW_AT``, New York) the model reviews
  every position once (``position_questions``) and sets an action, a stop and a target;
- ``rule``: a position not reviewed yet: a stop ``STOP_ATR`` ATR(14) from its last close and a
  target ``TARGET_ATR`` ATR away (no target for leveraged/inverse funds).

A stop only moves toward the price. Each morning and after the close it trails the best close
since it was set: max(stop, highest close − 2 ATR) for a long position, min(stop, lowest close
+ 2 ATR) for a short one. A review can tighten a stop but not loosen it, except once the stop
was hit and the position is still held: then the review sets a new one. Your own alert levels
never move. Targets change only with a review or your alerts.

Crossing a plan's stop or target sends one position card per level (holding alerts
``plan_stop`` / ``plan_target``); a level from your own alert fires as your alert instead.
These are risk-management levels, not tested signals; nothing is traded. The plans are kept
in ``trade_desk_settings`` (id ``position_plans``).
"""
from __future__ import annotations

from datetime import date, time as dtime
from typing import Any, Callable, Dict, List, Optional

from .models import utcnow
from .position_plan import STOP_ATR, TARGET_ATR

REVIEW_AT = dtime(17, 0)
NEAR_ATR = 0.5  # within half an ATR of a level: "near"
NEAR_PCT = 2.0  # the same without an ATR
REVIEW_QUESTION = ("Daily review of every position: for each give an action, a stop and a target for the next "
                   "sessions (and P&L levels for options).")
SOURCE_LABEL = {"you": "your alert", "review": "daily review", "rule": f"{STOP_ATR:g} ATR rule"}


def open_rows(view: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [{**row, "type": "stock"} for row in view.get("stocks") or []]
    return rows + [{**row, "type": "option"} for row in view.get("options") or [] if not row.get("expired")]


def ticker_of(row: Dict[str, Any]) -> str:
    return row["ticker"] if row["type"] == "stock" else row["underlying"]


def price_of(row: Dict[str, Any]) -> Optional[float]:
    return row.get("price") if row["type"] == "stock" else row.get("underlying_price")


def exposure_of(row: Dict[str, Any]) -> str:
    from .holdings import option_side
    if row["type"] == "stock":
        return "long" if row["qty"] > 0 else "short"
    return option_side(row)


def _sign(exposure: str) -> int:
    return {"long": 1, "short": -1}.get(exposure, 0)


def ensure(plans: Dict[str, Dict[str, Any]], view: Dict[str, Any], levels: Dict[str, Dict[str, Any]], day: date,
           geared: Callable[[str], bool]) -> Dict[str, Dict[str, Any]]:
    """A plan for every open position (new ones from the ATR rule); closed positions' plans dropped."""
    out = {}
    for row in open_rows(view):
        key, ticker = row["key"], ticker_of(row)
        found = levels.get(ticker) or {}
        plan = dict(plans.get(key) or {})
        new = not plan
        if found.get("atr"):
            plan["atr"] = round(float(found["atr"]), 4)  # the panel's "near" and the trailing distance
        exposure = exposure_of(row)
        if new:
            sign, atr = _sign(exposure), plan.get("atr")
            anchor = found.get("last_close") or price_of(row)
            plan = {"key": key, "ticker": ticker, "type": row["type"], "exposure": exposure, "action": "",
                    "stop": None, "target": None, "set_on": day.isoformat(), "trail_mark": anchor,
                    "atr": atr, "created_at": utcnow().isoformat()}
            if sign and atr and anchor:
                plan.update(stop=round(anchor - sign * STOP_ATR * atr, 2), stop_source="rule",
                            stop_basis=f"{STOP_ATR:g} ATR {'under' if sign > 0 else 'over'} the last close {anchor:.2f}")
                if not geared(ticker):
                    plan.update(target=round(anchor + sign * TARGET_ATR * atr, 2), target_source="rule",
                                target_basis=f"{TARGET_ATR:g} ATR {'over' if sign > 0 else 'under'} the last close")
        plan["exposure"] = exposure  # a rolled or changed position can flip
        out[key] = plan
    return out


def trail(plan: Dict[str, Any], closes: List[float]) -> bool:
    """Moves the stop toward the best close since it was set; True when it moved."""
    sign, atr, stop = _sign(plan.get("exposure", "")), plan.get("atr"), plan.get("stop")
    if (not sign or not atr or stop is None or not closes or plan.get("stop_source") not in {"review", "rule"}
            or plan.get("stop_hit_at")):
        return False  # a hit stop waits for the next review
    best = max(closes) if sign > 0 else min(closes)
    mark = plan.get("trail_mark")
    mark = best if mark is None else (max(mark, best) if sign > 0 else min(mark, best))
    plan["trail_mark"] = mark
    candidate = round(mark - sign * STOP_ATR * atr, 2)
    if sign * (candidate - stop) > 0:
        plan["stop"] = candidate
        plan["stop_basis"] = f"trailing: {STOP_ATR:g} ATR {'under' if sign > 0 else 'over'} the best close {mark:.2f}"
        return True
    return False


def closes_since(bars: List[Dict[str, Any]], since: str, through: date, *, include_through: bool) -> List[float]:
    end = through.isoformat()
    return [float(bar["close"]) for bar in bars
            if since <= str(bar.get("date", ""))[:10] and (str(bar["date"])[:10] < end or
                                                          (include_through and str(bar["date"])[:10] == end))]


def apply_review(plans: Dict[str, Dict[str, Any]], answer: Dict[str, Any], view: Dict[str, Any], day: date,
                 when: str) -> Dict[str, Dict[str, Any]]:
    """The review's action and levels per position; a stop is tightened, never loosened (unless hit)."""
    rows = {row["key"]: row for row in open_rows(view)}
    out = {key: dict(plan) for key, plan in plans.items()}
    for item in answer.get("positions") or []:
        row = rows.get(item["key"])
        if row is None:
            continue
        plan = out.setdefault(item["key"], {"key": item["key"], "ticker": ticker_of(row), "type": row["type"],
                                            "stop": None, "target": None})
        sign = _sign(plan.get("exposure") or exposure_of(row))
        plan.update(action=item.get("action", ""), reason=item.get("reason", ""), risk=item.get("risk", ""),
                    reviewed_at=when, exposure=exposure_of(row))
        stop, current = item.get("stop"), plan.get("stop")
        loosens = current is not None and sign and stop is not None and sign * (stop - current) < 0
        if stop is not None and (current is None or plan.get("stop_source") != "review" or plan.get("stop_hit_at")
                                 or not loosens):
            plan.update(stop=stop, stop_source="review", stop_basis=item.get("stop_basis", ""), set_on=day.isoformat(),
                        trail_mark=price_of(row), stop_hit_at=None, stop_note="")
        elif stop is not None:
            plan["stop_note"] = f"kept: the review's {stop:.2f} would loosen it"
        plan.update(target=item.get("target"), target_source="review" if item.get("target") is not None else "",
                    target_basis=item.get("target_basis", ""), target_hit_at=None,
                    pnl_stop_pct=item.get("pnl_stop_pct"), pnl_target_pct=item.get("pnl_target_pct"))
    return out


def resolve(plan: Optional[Dict[str, Any]], row: Dict[str, Any], rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The plan with your own alert levels on top (they win and never move)."""
    merged = dict(plan or {"key": row["key"], "ticker": ticker_of(row), "type": row["type"], "stop": None,
                           "target": None})
    exposure = merged.get("exposure") or exposure_of(row)
    sign = _sign(exposure)
    mine = [rule for rule in rules if rule.get("position_key") == row["key"] and rule.get("status") == "active"]
    below = [rule["value"] for rule in mine if rule["kind"] == "price_below"]
    above = [rule["value"] for rule in mine if rule["kind"] == "price_above"]
    stops, targets = (below, above) if sign >= 0 else (above, below)
    if stops:
        merged.update(stop=max(stops) if sign >= 0 else min(stops), stop_source="you", stop_basis="your alert",
                      stop_note="")
    if targets:
        merged.update(target=min(targets) if sign >= 0 else max(targets), target_source="you",
                      target_basis="your alert")
    pnl_below = [rule["value"] for rule in mine if rule["kind"] == "pnl_below"]
    pnl_above = [rule["value"] for rule in mine if rule["kind"] == "pnl_above"]
    if pnl_below:
        merged.update(pnl_stop_pct=max(pnl_below), pnl_stop_source="you")
    if pnl_above:
        merged.update(pnl_target_pct=min(pnl_above), pnl_target_source="you")
    merged["exposure"] = exposure
    return merged


def status(plan: Dict[str, Any], price: Optional[float]) -> str:
    """stop_hit / target_hit / near_stop / near_target / ok."""
    sign = _sign(plan.get("exposure", ""))
    if not price or not sign:
        return "ok"
    stop, target, atr = plan.get("stop"), plan.get("target"), plan.get("atr")
    if stop is not None and sign * (price - stop) <= 0:
        return "stop_hit"
    if target is not None and sign * (price - target) >= 0:
        return "target_hit"

    def near(level):
        return level is not None and abs(price - level) <= (NEAR_ATR * atr if atr else price * NEAR_PCT / 100)
    if near(stop):
        return "near_stop"
    if near(target):
        return "near_target"
    return "ok"


def panel_rows(view: Dict[str, Any], plans: Dict[str, Dict[str, Any]], rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the Home panel lists: each open position with its plan, distances and status, urgent first."""
    from .holdings import describe_option
    out = []
    for row in open_rows(view):
        plan = resolve(plans.get(row["key"]), row, rules)
        price = price_of(row)

        def distance(level):
            return round((level / price - 1) * 100, 2) if level is not None and price else None
        state = status(plan, price)
        out.append({
            "key": row["key"], "ticker": ticker_of(row), "type": row["type"], "exposure": plan["exposure"],
            "label": (f"{row['ticker']} shares" if row["type"] == "stock"
                      else describe_option(row, with_days=False).split(" · ")[0]),
            "price": price, "day_pct": row.get("day_pct"), "pnl_pct": row.get("pnl_pct"),
            "weight_pct": row.get("weight_pct"), "days_left": row.get("days_left"),
            "action": plan.get("action") or "", "reason": plan.get("reason", ""), "risk": plan.get("risk", ""),
            "stop": plan.get("stop"), "stop_source": plan.get("stop_source", ""), "stop_basis": plan.get("stop_basis", ""),
            "stop_note": plan.get("stop_note", ""), "stop_distance_pct": distance(plan.get("stop")),
            "target": plan.get("target"), "target_source": plan.get("target_source", ""),
            "target_basis": plan.get("target_basis", ""), "target_distance_pct": distance(plan.get("target")),
            "pnl_stop_pct": plan.get("pnl_stop_pct"), "pnl_target_pct": plan.get("pnl_target_pct"),
            "status": state, "reviewed_at": plan.get("reviewed_at"),
        })
    order = {"stop_hit": 0, "near_stop": 1, "target_hit": 2, "near_target": 3, "ok": 4}
    return sorted(out, key=lambda item: (order[item["status"]], -(item["weight_pct"] or 0)))


def note(plan: Dict[str, Any], kind: str) -> str:
    """'daily review', 'trailing', 'your alert' or '2 ATR rule' for a level's source."""
    source = plan.get(f"{kind}_source", "")
    if kind == "stop" and str(plan.get("stop_basis", "")).startswith("trailing"):
        return "trailing stop"
    return SOURCE_LABEL.get(source, "")
