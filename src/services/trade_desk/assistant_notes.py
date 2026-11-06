"""What the trading assistant asks its model (assistant_model.py: your Claude Code plan first, the API last).

Phase 3: a plan you type in your own words becomes the fields the session watches. You confirm the
form before anything is watched; the model's reading is never acted on by itself.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import assistant_model

_NEW_YORK = ZoneInfo("America/New_York")

PARSE_SYSTEM = """You turn a trader's plan for one stock, written in their own words, into fields a computer can watch.
Reply with one JSON object and nothing else:
{"direction": "long" or "short",
 "trigger": {"kind": "now" | "above" | "below" | "close_above" | "close_below", "price": number or null, "bar_minutes": 1 | 5 | 15 or null},
 "invalidation": number or null, "stop": number or null, "target": number or null,
 "time_stop": "HH:MM" (New York time) or null, "instrument": "calls" | "puts" | "shares" or null,
 "questions": [what is missing or unclear, in short sentences]}
Rules: calls mean long, puts mean short. "Breaks", "reclaims" or "over" a price is "above" (or "below" for a short),
unless the trader says a bar must close past it: then close_above / close_below with that bar size (5 when unsaid).
Use only prices the trader wrote, or the price of a level from the list you are given when the trader names it
("VWAP", "the prior day high", "the opening range low"). Never invent a price: leave it null and add a question.
"Out by 3:30" is a time stop of 15:30. Give no advice and no opinion on the plan."""


def _levels(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"label": level["label"], "price": level["price"]} for level in state.get("levels") or []]


def parse_plan(text: str, ticker_state: Dict[str, Any], now: datetime, *,
               generate: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The model's reading of a typed plan: ``draft`` (the fields), ``questions``, and which model read it."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Type your plan first")
    prompt = json.dumps({"ticker": ticker_state["ticker"], "last_price": ticker_state.get("price"),
                         "time_now_new_york": now.astimezone(_NEW_YORK).strftime("%H:%M"),
                         "levels": _levels(ticker_state), "plan": text[:1000]}, ensure_ascii=False)
    result = (generate or assistant_model.generate)(prompt, PARSE_SYSTEM)
    data = result["data"]
    trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
    draft = {"direction": data.get("direction"), "trigger": {"kind": trigger.get("kind") or "now", "price": trigger.get("price"),
                                                          "bar_minutes": trigger.get("bar_minutes")},
             "invalidation": data.get("invalidation"), "stop": data.get("stop"), "target": data.get("target"),
             "time_stop": data.get("time_stop"), "instrument": data.get("instrument")}
    questions = [str(q)[:200] for q in (data.get("questions") or []) if str(q).strip()][:5]
    return {"draft": draft, "questions": questions, "text": text, "model": result.get("label", ""),
            "seconds": result.get("seconds")}


def answer_failed(exc: Exception) -> str:
    """Why the model gave nothing usable, for the page."""
    if isinstance(exc, ValueError):
        return str(exc)
    return f"No model answered ({str(exc)[:200]})"


def as_of(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(_NEW_YORK)).astimezone(_NEW_YORK).strftime("%H:%M")
