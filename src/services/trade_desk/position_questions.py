"""Questions about the positions you hold (Trade Desk → Holdings → Ask about your positions).

The model gets each position as prices and percentages — the average cost and current price,
P&L on cost and weight of the account, never share or contract counts or account amounts —
with its levels: the prior 20-day low/high, the 50-day average, ATR(14), the ATR rule's
stop/target (``position_plan``), your NX tunnel, the latest daily report verdict, the next
earnings date and your alerts on it. It answers your question and gives, per position it
discusses, an action and stop/target levels. A level on the wrong side of the price (a stop
above a long position's price) is replaced by the ATR rule's level and marked as such.

Nothing is traded: a level becomes an alert only when you set it on the dashboard. The answers
are kept (the newest ``MAX_KEPT``) in ``trade_desk_settings`` (id ``position_questions``); they
never go to Discord.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from . import position_plan
from .models import utcnow

logger = logging.getLogger(__name__)

MAX_KEPT = 30
STALE_MINUTES = 15  # a question still "running" this long was cut off by a restart
ACTIONS = ("hold", "add", "trim", "take_profit", "close", "roll", "hedge")

INSTRUCTIONS = """You review a trader's existing US stock and option positions and answer their question.
Use only the data below. Never place or suggest placing orders for them; they act in their own broker.

For each position the question concerns (every position when it asks about all of them), give:
- action: one of hold, add, trim, take_profit, close, roll, hedge;
- stop: a price of the stock (for options: of the underlying) where the position is wrong and should be cut,
  and stop_basis: why that level (e.g. "below the 20-day low 18.10", "1.5 ATR under the price");
- target: a price of the stock / underlying to take profit, and target_basis; null when holding without a target
  makes more sense (e.g. a hedge);
- for options also pnl_stop_pct and pnl_target_pct: the P&L on cost (percent) at which to cut or take profit;
- reason: at most two sentences; risk: the main risk in one sentence.
A long exposure's stop is below the current price and its target above; a short exposure's the other way.
The supplied reference levels (an ATR rule of thumb, 20-day low/high, 50-day average, the NX tunnel, the
daily report) are starting points; prefer a level with a clear structural reason and say which. Leveraged or
inverse funds decay: say so when holding them long. Rules of thumb have no tested edge: never promise outcomes.
Write in the language of the question. Return only JSON:
{"summary": "the direct answer in 2-5 sentences",
 "positions": [{"key": "<position key>", "action": "...", "stop": number|null, "stop_basis": "...",
   "target": number|null, "target_basis": "...", "pnl_stop_pct": number|null, "pnl_target_pct": number|null,
   "reason": "...", "risk": "..."}]}"""


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def positions_in(view: Dict[str, Any], position_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """The positions a question is about: one by its key, or every open one."""
    rows = [{**row, "type": "stock"} for row in view.get("stocks") or []]
    rows += [{**row, "type": "option"} for row in view.get("options") or [] if not row.get("expired")]
    if position_key:
        rows = [row for row in rows if row["key"] == position_key]
        if not rows:
            raise ValueError("That position is no longer held; sync and pick it again")
    return rows


def _ticker(row: Dict[str, Any]) -> str:
    return row["ticker"] if row["type"] == "stock" else row["underlying"]


def build_context(rows: List[Dict[str, Any]], view: Dict[str, Any], rules: List[Dict[str, Any]], today: date, *,
                  bars: Callable[..., Dict[str, List[Dict[str, Any]]]], geared: Callable[[str], bool], report: Callable[[str, date], str],
                  earnings_date: Callable[[str, date], Optional[date]]) -> Dict[str, Any]:
    """What the model sees: positions in prices and percentages, and each ticker's levels."""
    from src.services import nx_tunnel
    from .holdings import describe_option, moneyness, option_side
    from .levels import breakout_levels
    tickers = list(dict.fromkeys(_ticker(row) for row in rows))
    try:
        history = bars(tickers, period="2y") if tickers else {}
    except Exception as exc:  # the answer goes on without the levels
        logger.info("Position question bars unavailable: %s", type(exc).__name__)
        history = {}
    markets: Dict[str, Dict[str, Any]] = {}
    for ticker in tickers:
        rows_for = history.get(ticker) or history.get(ticker.replace("-", ".")) or []
        found = breakout_levels(rows_for, today) if rows_for else None
        nx = nx_tunnel.describe(rows_for) if rows_for else None
        try:
            when = earnings_date(ticker, today) if not geared(ticker) else None
        except Exception:
            when = None
        markets[ticker] = {
            "levels": ({key: round(found[key], 2) for key in ("low20", "high20", "ma50", "atr")} if found else None),
            "atr_pct": None, "nx_tunnel": nx_tunnel.summary_line(nx, "en") if nx else None,
            "daily_report": report(ticker, today) or None, "next_earnings": when.isoformat() if when else None,
            "leveraged_or_inverse_fund": geared(ticker), "_levels": found,
        }
    total = view.get("total_assets")
    positions = []
    for row in rows:
        ticker = _ticker(row)
        price = row.get("price") if row["type"] == "stock" else row.get("underlying_price")
        exposure = (("long" if row["qty"] > 0 else "short") if row["type"] == "stock" else option_side(row))
        ref = position_plan.reference_levels(exposure, price, markets[ticker]["_levels"], geared=geared(ticker))
        if markets[ticker]["levels"] and price:
            markets[ticker]["atr_pct"] = round(markets[ticker]["levels"]["atr"] / price * 100, 2)
        item: Dict[str, Any] = {
            "key": row["key"], "ticker": ticker, "type": row["type"], "exposure": exposure,
            "pnl_pct_on_cost": _round(row.get("pnl_pct")), "weight_pct_of_account": _round(row.get("weight_pct")),
            "day_change_pct": _round(row.get("day_pct")),
            "rule_of_thumb": ({"stop": ref["stop"], "target": ref["target"]} if ref else None),
            "your_alerts": [{"kind": rule["kind"], "value": rule["value"], "status": rule["status"]}
                            for rule in rules if rule.get("position_key") == row["key"]
                            or (not rule.get("position_key") and rule.get("ticker") == ticker)],
        }
        if row["type"] == "stock":
            item.update(name=row.get("name", ""), price=price, average_cost=row.get("average_cost"))
        else:
            item.update(description=describe_option(row), underlying_price=price, expiry=row["expiry"],
                        trading_days_left=row["days_left"], strategy=row["label"],
                        pct_of_max_profit=_round(row.get("pct_of_max")), moneyness=moneyness(row),
                        quotes_too_wide=bool(row.get("quotes_wide")),
                        legs=[{"side": "long" if leg["qty"] > 0 else "short", "right": leg["right"],
                               "strike": leg["strike"], "average_cost": leg.get("average_cost"),
                               "mark": leg.get("mark")} for leg in row["legs"]])
        positions.append(item)
    cash = view.get("cash")
    return {"as_of": utcnow().isoformat(), "today": today.isoformat(),
            "cash_pct_of_account": _round(cash / total * 100) if cash is not None and total else None,
            "positions": positions,
            "tickers": {ticker: {key: value for key, value in market.items() if not key.startswith("_")}
                        for ticker, market in markets.items()}}


def _round(value: Any, digits: int = 1) -> Optional[float]:
    number = _num(value)
    return round(number, digits) if number is not None else None


def parse_answer(raw: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """The model's answer, kept to known positions and levels on the right side of the price."""
    by_key = {item["key"]: item for item in context["positions"]}
    out = []
    for entry in raw.get("positions") or []:
        if not isinstance(entry, dict) or entry.get("key") not in by_key:
            continue
        item = by_key[entry["key"]]
        price = _num(item.get("price") if item["type"] == "stock" else item.get("underlying_price"))
        sign = {"long": 1, "short": -1}.get(item["exposure"], 0)
        rule = item.get("rule_of_thumb") or {}

        def level(name: str, below: bool):
            value, basis = _num(entry.get(name)), _text(entry.get(f"{name}_basis"), 140)
            wrong_side = value is not None and price and sign and ((value < price) != (below == (sign > 0)))
            if value is not None and value > 0 and not wrong_side:
                return round(value, 2), basis, "model"
            if rule.get(name) is not None and value is not None:
                # A level on the wrong side of the price: the rule's level instead, said plainly.
                atr = position_plan.STOP_ATR if name == "stop" else position_plan.TARGET_ATR
                return rule[name], f"{atr:g} ATR rule of thumb (the model's level was on the wrong side)", "rule"
            return None, "", ""

        stop, stop_basis, stop_source = level("stop", below=True)
        target, target_basis, target_source = level("target", below=False)
        pnl = _num(item.get("pnl_pct_on_cost"))
        pnl_stop, pnl_target = _num(entry.get("pnl_stop_pct")), _num(entry.get("pnl_target_pct"))
        if item["type"] != "option" or pnl_stop is None or not -100 <= pnl_stop < (pnl if pnl is not None else 0):
            pnl_stop = None
        if item["type"] != "option" or pnl_target is None or pnl_target <= (pnl if pnl is not None else 0):
            pnl_target = None
        action = str(entry.get("action") or "").strip().lower().replace(" ", "_")
        out.append({"key": item["key"], "ticker": item["ticker"], "type": item["type"], "exposure": item["exposure"],
                    "label": item.get("description") or f"{item['ticker']} shares", "price": price,
                    "pnl_pct": pnl, "action": action if action in ACTIONS else "review",
                    "stop": stop, "stop_basis": stop_basis, "stop_source": stop_source,
                    "target": target, "target_basis": target_basis, "target_source": target_source,
                    "pnl_stop_pct": round(pnl_stop, 1) if pnl_stop is not None else None,
                    "pnl_target_pct": round(pnl_target, 1) if pnl_target is not None else None,
                    "reason": _text(entry.get("reason"), 500), "risk": _text(entry.get("risk"), 300)})
    summary = _text(raw.get("summary"), 1500)
    if not summary and not out:
        raise ValueError("The model's answer could not be read")
    return {"summary": summary, "positions": out}


def generate(prompt: str) -> Dict[str, Any]:
    """The configured generation backend (with its fallback): the JSON answer and the model used."""
    from src.agent.runner import try_parse_json
    from src.analyzer import GeminiAnalyzer
    result = GeminiAnalyzer().generate_text_with_metadata(prompt, max_tokens=4096, temperature=0.2)
    data = try_parse_json((result.text if result else "") or "")
    if not isinstance(data, dict):
        raise ValueError("The model did not return a readable answer")
    return {"data": data, "model": getattr(result, "model", "") or getattr(result, "backend", "")}


def answer(question: str, context: Dict[str, Any], *, generate_fn: Callable[[str], Dict[str, Any]] = None) -> Dict[str, Any]:
    import json
    prompt = (f"{INSTRUCTIONS}\n\nQuestion: {question}\n\nPositions and levels (JSON):\n"
              f"{json.dumps(context, ensure_ascii=False, default=str)}")
    result = (generate_fn or generate)(prompt)
    return {**parse_answer(result["data"], context), "model": result.get("model", "")}


def visible(items: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Newest first; a question left running by a restart reads as failed."""
    now = now or utcnow()
    out = []
    for item in items:
        if item.get("status") == "running":
            try:
                started = datetime.fromisoformat(item["created_at"])
            except (KeyError, ValueError):
                started = now
            if now - started > timedelta(minutes=STALE_MINUTES):
                item = {**item, "status": "failed", "error": "Interrupted (the server restarted); ask again"}
        out.append(item)
    return sorted(out, key=lambda item: item.get("created_at", ""), reverse=True)
