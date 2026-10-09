"""What to do about a position you hold: reference stop/target levels and the usual action per alert.

Levels: a stop 2 ATR(14) from the price and a target 3 ATR away, on the side your position is
exposed to (the ideas use 1.5 ATR for a fresh entry; a held position trails a little wider);
the prior 20-day low/high and the 50-day average are named as structure. A position's saved
plan (``position_plans``: your alerts, the daily review, a trailing stop) replaces these. Leveraged/inverse funds get no target (daily
reset: ATR targets mean little). The actions are the common rules of thumb for each alert
(cut a long option at -50 %, take some profit at +50/+100 %, close a spread near its maximum,
close or roll a short leg in the money before expiry). None of this is a tested edge
(``docs/strategy-backtest.md``); the alerts say so, and nothing is ever traded.

Everything here is Discord-safe: prices of the stock or underlying and percentages only.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STOP_ATR = 2.0
TARGET_ATR = 3.0
REPORT_MAX_AGE_DAYS = 3
FOOTER = "Rule-of-thumb levels, not tested signals · read-only: nothing is traded automatically"


def reference_levels(side: str, price: Optional[float], levels: Optional[Dict[str, Any]], *,
                     geared: bool = False) -> Optional[Dict[str, Any]]:
    """Stop and target for a long or short exposure from the current price, or None without ATR."""
    if side not in {"long", "short"} or not price or not levels or not levels.get("atr"):
        return None
    sign = 1 if side == "long" else -1
    atr = float(levels["atr"])
    out: Dict[str, Any] = {"side": side, "price": price, "atr": atr,
                           "stop": round(price - sign * STOP_ATR * atr, 2), "target": None,
                           "low20": levels.get("low20"), "high20": levels.get("high20"), "ma50": levels.get("ma50")}
    if not geared:
        out["target"] = round(price + sign * TARGET_ATR * atr, 2)
    return out


def levels_line(ref: Optional[Dict[str, Any]]) -> str:
    """'Stop 192.50 (1.5 ATR) · target 215.00 (3 ATR) · 20-day low 205.00'."""
    if not ref:
        return ""
    parts = [f"stop {ref['stop']:.2f} ({ref.get('stop_note') or f'{STOP_ATR:g} ATR'})"] if ref.get("stop") is not None else []
    if ref.get("target") is not None:
        parts.append(f"target {ref['target']:.2f} ({ref.get('target_note') or f'{TARGET_ATR:g} ATR'})")
    # The structure on the stop side: the level whose loss ends the trade.
    structure = ref.get("low20") if ref["side"] == "long" else ref.get("high20")
    if structure and (structure < ref["price"] if ref["side"] == "long" else structure > ref["price"]):
        parts.append(f"20-day {'low' if ref['side'] == 'long' else 'high'} {structure:.2f}")
    return "Levels: " + " · ".join(parts)


def _held_word(ref: Optional[Dict[str, Any]]) -> str:
    return "below" if not ref or ref["side"] == "long" else "above"


def stock_actions(kind: str, ref: Optional[Dict[str, Any]], *, geared: bool = False,
                  level: Optional[float] = None, change_pct: Optional[float] = None) -> List[str]:
    """The usual next step for a stock alert, then the levels."""
    stop = f" (stop {ref['stop']:.2f})" if ref and ref.get("stop") is not None else ""
    against = ref is not None and change_pct is not None and (change_pct < 0) == (ref["side"] == "long")
    if kind == "low20" and level:
        action = f"A close {_held_word(ref)} {level:.2f} (the 20-day low) is the usual exit signal: exit or cut{stop}."
    elif kind == "ma50" and level:
        action = (f"The trend is weakening: tighten the stop or trim; a close back above {level:.2f} "
                  f"(the 50-day average) repairs it.")
    elif kind == "earnings":
        action = "Decide before earnings: trim or hedge if the size is uncomfortable; a gap can jump past a stop."
    elif kind == "move" and change_pct is not None:
        if against:
            action = f"A big move against you: exit if it closes past your stop{stop}; do not average down by reflex."
        elif ref and ref.get("target") is not None and ref.get("stop") is not None:
            action = f"A big move your way: take some profit near {ref['target']:.2f} or trail the stop to {ref['stop']:.2f}."
        else:
            action = f"A big move your way: take some profit, or trail the stop{stop}."
    else:
        return [levels_line(ref)] if ref else []
    lines = [action]
    if geared:
        lines.append("Leveraged/inverse fund: it decays over time; keep it short-term.")
    if ref:
        lines.append(levels_line(ref))
    return lines


def option_actions(kind: str, position: Dict[str, Any], ref: Optional[Dict[str, Any]], *,
                   level: Optional[float] = None) -> List[str]:
    """The usual next step for an option alert, then the underlying's levels."""
    legs = position.get("legs") or []
    spot = position.get("underlying_price")
    longs = [leg for leg in legs if leg["qty"] > 0]
    shorts = [leg for leg in legs if leg["qty"] < 0]

    def itm(leg):
        return bool(spot) and (spot > leg["strike"] if leg["right"] == "call" else spot < leg["strike"])

    if kind == "loss":
        hold_if = (f" — keep it only if {position['underlying']} holds {'above' if ref['side'] == 'long' else 'below'} "
                   f"{ref['stop']:.2f}" if ref and ref.get("stop") is not None else "")
        action = f"The common rule cuts a long option at {abs(level or 50):g}% down: close it{hold_if}."
    elif kind == "profit_max":
        action = "Close it: the last part of the maximum profit rarely pays for holding the risk to expiry."
    elif kind == "profit":
        action = ("Take half off and let the rest run with a stop at breakeven." if (level or 0) < 100
                  else "The premium has doubled: take profit, or at least half.")
    elif kind == "assignment":
        action = "Close or roll the short leg before the close to avoid assignment."
    elif kind in {"expiry", "expiry_today"}:
        if shorts and any(itm(leg) for leg in shorts):
            action = "A short leg is in the money: close or roll it before the close to avoid assignment."
        elif longs and all(itm(leg) for leg in longs) and not shorts:
            action = ("In the money: sell to close before expiry rather than exercise "
                      "(exercise needs cash or margin for the shares).")
        elif shorts:
            action = "Close the spread before expiry so a single leg is not exercised or assigned on its own."
        elif spot:
            action = "Out of the money: time decay is fastest now — sell what value is left or decide your exit today."
        else:
            action = "Decide your exit before the close."
        if kind == "expiry_today":
            action += " In-the-money options are exercised automatically at expiry unless closed."
    elif kind == "earnings":
        action = ("Implied volatility usually drops after earnings: decide whether to hold through it, "
                  "close, or take profit first.")
    else:
        return [levels_line(ref)] if ref else []
    return [action, *([levels_line(ref).replace("Levels:", f"{position['underlying']} levels:", 1)] if ref else [])]


def rule_action(rule: Dict[str, Any], side: str) -> str:
    """Your own alert fired: a stop or a target by its direction against the holding."""
    kind = rule["kind"]
    if kind == "days_to_expiry":
        return "Decide your exit before expiry."
    below = kind in {"price_below", "pnl_below"}
    if side == "short" and kind.startswith("price"):
        below = not below
    return ("Your stop level: exit or cut as you planned." if below
            else "Your target: take profit, or trail a stop to protect the gain.")


def alerts_line(rules: List[Dict[str, Any]]) -> str:
    """Your active alerts on this position, or a nudge to set a stop."""
    active = [rule for rule in rules if rule.get("status") == "active"]
    if not active:
        return "Your alerts: none set — Trade Desk → Holdings can suggest a stop and set it."
    text = {"price_below": "at or below {v:g}", "price_above": "at or above {v:g}",
            "pnl_below": "P&L at or below {v:g}%", "pnl_above": "P&L at or above {v:g}%",
            "days_to_expiry": "{v:g} trading days to expiry"}
    return "Your alerts: " + " · ".join(text[rule["kind"]].format(v=rule["value"]) for rule in active[:4])


def report_line(ticker: str, today: date, *, lookup=None) -> str:
    """The latest daily report verdict on the ticker with its stop/target, when recent."""
    try:
        if lookup is None:
            from src.services.decision_signal_service import DecisionSignalService
            items = DecisionSignalService().get_latest_active(stock_code=ticker, market="us", limit=1)["items"]
        else:
            items = lookup(ticker)
    except Exception as exc:  # the alert goes out without it
        logger.info("Report verdict unavailable for %s: %s", ticker, type(exc).__name__)
        return ""
    if not items:
        return ""
    item = items[0]
    created = str(item.get("created_at") or "")[:10]
    try:
        made = date.fromisoformat(created)
    except ValueError:
        return ""
    if today - made > timedelta(days=REPORT_MAX_AGE_DAYS):
        return ""
    label = item.get("action_label") or str(item.get("action") or "").replace("_", " ")
    parts = [f"Daily report ({made:%b} {made.day}): {label}".rstrip(": ")]
    if item.get("stop_loss"):
        parts.append(f"stop {float(item['stop_loss']):.2f}")
    if item.get("target_price"):
        parts.append(f"target {float(item['target_price']):.2f}")
    return " · ".join(parts)


def card(title: str, detail: List[str], plan: List[str], *, report: str = "", alerts: str = "") -> Dict[str, Any]:
    """The structured alert: Discord renders it as a card, the dashboard as lines."""
    return {"title": title, "detail": [line for line in detail if line], "plan": [line for line in plan if line],
            "report": report, "alerts": alerts}


def card_text(item: Dict[str, Any]) -> List[str]:
    """The card's lines after the title, for the plain-text message."""
    lines = list(item.get("detail") or [])
    lines += [f"→ {line}" for line in item.get("plan") or []]
    lines += [line for line in (item.get("report"), item.get("alerts")) if line]
    return lines
