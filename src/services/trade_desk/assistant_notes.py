"""What the trading assistant asks its model (assistant_model.py: your Claude Code plan first, the API last).

Phase 3: a plan you type in your own words becomes the fields the session watches. You confirm the
form before anything is watched; the model's reading is never acted on by itself.
"""
from __future__ import annotations

import json
import re
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


# -- phase 4: notes, calls and your questions --------------------------------------------------------
_NOTE_HEAD = """You are a trading assistant beside a trader during the US session. You get one stock's state as the system
computed it: the price, levels with their test verdicts, box flags with their grades, option contracts near the price,
the trader's positions and plans, the latest events, and why you are asked now. Write for the trader in plain English.
"""
_NOTE_REPLY = """Reply with one JSON object and nothing else:
{"note": "two to four short sentences: what just changed, where the price sits against the levels that matter, and what
the trader's position or plan means now, with its risk in % of the account when the state gives one",
 "call": null, or {"action": "enter" | "add" | "trim" | "exit",
                   "position": for add, trim and exit, the held position's label exactly as the state gives it, else null,
                   "direction": "long" or "short" (enter; add, trim and exit follow the position's exposure),
                   "trigger": {"kind": "now" | "above" | "below" | "close_above" | "close_below", "price": number or null,
                               "bar_minutes": 1 | 5 | 15 or null},
                   "stop": number for enter and add, else null, "target": number for enter and add, else null,
                   "time_stop": "HH:MM" or null, "why": "one sentence"}}
"""
_RULES = """Rules:
- Every price you write, in the note or the call, must be one in the state: the last price, a level, a flag's or plan's
  price, stop or target, or a contract's strike, bid, ask or breakeven. Never compute, round or invent a price.
- Nothing here has passed a test except what the state marks "pass": say once in a note that the levels and flags you
  lean on are untested, not after each one.
- Leave out what is missing: don't say there is no position, no plan or no risk figure.
- A plan's "state" says whether it is waiting for its trigger, in the trade, or done; take it as given.
- When a chart image comes with the state, read the shape from it (the trend, ranges, how the price reached a
  level); every price you write still comes from the state, never from the picture's axis.
"""
_CALL_RULES = """- A call is a whole rule a computer can follow, every part a price from the state. "enter" is a new trade on the
  stock; "add" is more of a held position, on its side. Both need a trigger, a stop, a target and a time stop.
  "trim" sells part of a held position and "exit" all of it, when the trigger trades: a stop for a position is an
  exit with a "below" trigger for a long position ("above" for a short one), and taking profit at a price is a trim or
  exit with an "above" trigger for a long ("below" for a short). Their time_stop is how long the call stands.
- A position's stop and target come from the trader's alerts, the daily review or a 2 ATR rule: risk levels, not
  tested signals, and prices of the stock also for an option position.
- {readiness}
- Don't repeat a call of yours that the plans show still waiting or in the trade.
- Every call is an untested model call: it is recorded and graded, an entry or add against the same trade at random
  times, a trim or exit against holding to its time stop and against selling at random times.
- Never size a position or say how much to add or trim: the trader decides. The trader places every order; never
  say an order was or will be placed."""

CALL_STYLES = {
    "selective": "Make a call only when the state gives a clear reason; most notes have none.",
    "active": ("Make a call whenever the state gives a reason you can say in one sentence: completed bars confirming a "
               "move through a level, the price at a held position's or plan's level, or a position's P&L step. For each "
               "held position, say in the note whether you would hold, add, trim or exit, and make it a call unless it is "
               "hold. Make none when nothing has changed since the last note."),
}


def note_system(calls: str = "selective") -> str:
    """The analyst's instructions for a note; ``calls`` is how readily it makes a call (CALL_STYLES)."""
    return _NOTE_HEAD + _NOTE_REPLY + _RULES + _CALL_RULES.format(readiness=CALL_STYLES.get(calls, CALL_STYLES["selective"]))


NOTE_SYSTEM = note_system()

ASK_SYSTEM = (_NOTE_HEAD + """The trader asks you a question about the stock. Reply with one JSON object and nothing else:
{"answer": "answer the question in up to five short sentences, with the numbers from the state", "note": "", "call": null}
""" + _RULES + """- A question gets no call: answer it in words. Never size a position or say how much to add or trim: the
  trader decides. The trader places every order; never say an order was or will be placed.""")

PRICE = re.compile(r"(?<![\w.%])(\d{1,6}(?:\.\d{1,4})?)(?![\d%]|\s*(?:%|percent|R\b|min|minute|second|s\b|day|session|share|contract|x\b))")


def context(state: Dict[str, Any], *, horizon: str, now: datetime, closing: Optional[datetime], events: List[Dict[str, Any]],
            reasons: List[str], facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The part of a stock's state the model sees: small, and only computed values (``facts``: what completed bars
    say, assistant_watch.facts)."""
    price = state.get("price")
    contracts = sorted(state.get("contracts", {}).get("rows") or [], key=lambda r: abs(r["strike"] - (price or 0)))[:6]
    return {
        "ticker": state["ticker"], "horizon": horizon, "time_new_york": now.astimezone(_NEW_YORK).strftime("%H:%M"),
        "minutes_to_close": round((closing - now).total_seconds() / 60) if closing is not None else None,
        "price": price, "change_pct": state.get("change_pct"),
        "price_is": "the last pre-market trade: the session has not opened" if state.get("price_at") == "pre-market" else "the last trade",
        "levels": [{"label": l["label"], "price": l["price"], "distance_pct": l["distance_pct"], "verdict": l["verdict"] or "yours"}
                   for l in state.get("levels") or []],
        "flags": state.get("flags") or [],
        "contracts": [{k: r.get(k) for k in ("right", "strike", "bid", "ask", "iv", "delta", "breakeven", "breakeven_move_pct",
                                             "theta_per_min", "minutes_left")} for r in contracts],
        "positions": [{k: p.get(k) for k in ("label", "type", "exposure", "pnl_pct", "weight_pct", "stop", "target", "days_left")}
                      for p in state.get("positions") or []],
        "plans": [{"source": p.get("source"), "state": _plan_state(p),
                   **{k: p.get(k) for k in ("action", "position", "direction", "trigger", "stop", "target", "time_stop")
                      if p.get(k) is not None},
                   "entry_price": p.get("price"), "exit_price": (p.get("live_exit") or {}).get("price") or p.get("exit")}
                  for p in state.get("plans") or []],
        "recent_events": [{"time": datetime.fromisoformat(e["at"]).astimezone(_NEW_YORK).strftime("%H:%M"), "title": e["title"]}
                          for e in events[-6:]],  # events are stamped in UTC; the model reads New York time
        "asked_because": reasons,
        **({"facts": facts} if facts else {}),
    }


def _hhmm(text: Any) -> str:
    try:
        return datetime.fromisoformat(str(text)).astimezone(_NEW_YORK).strftime("%H:%M")
    except ValueError:
        return "?"


def _plan_state(plan: Dict[str, Any]) -> str:
    """A plan's status in words, with its prices: the bare status ("open") reads to a model like "not triggered"."""
    status, live = plan.get("status"), plan.get("live_exit")
    if plan.get("kind") == "exit" and status in ("open", "closed"):
        sold = f"{plan.get('action')} triggered at {plan.get('price'):g} at {_hhmm(plan.get('entry_time'))}"
        if status == "closed" and plan.get("saved_pct") is not None:
            return f"{sold}; graded: {plan['saved_pct']:+.2f}% against holding to {plan.get('time_stop')}"
        return f"{sold}: done for today, graded against holding to {plan.get('time_stop')} after the close"
    if status == "waiting":
        return "waiting for its trigger: not sold yet" if plan.get("kind") == "exit" else "waiting for its trigger: not in the trade"
    if status == "open" and live:
        return f"in the trade from {plan.get('price'):g} at {_hhmm(plan.get('entry_time'))}, then reached its {live['how']} " \
               f"at {live['price']:g} at {_hhmm(live['time'])}: done for today, graded after the close"
    if status == "open":
        return f"in the trade: triggered at {_hhmm(plan.get('entry_time'))}, entry {plan.get('price'):g}"
    if status == "closed":
        return f"graded: {plan.get('how')} at {plan.get('exit'):g}" + (f", {plan['R']:+.2f}R" if plan.get("R") is not None else "")
    if status == "void":
        return f"void: {plan.get('void') or 'cancelled'}"
    return "expired without triggering" if status == "expired" else str(status)


def known_prices(ctx: Dict[str, Any]) -> List[float]:
    out: List[float] = []
    def add(value: Any) -> None:
        if isinstance(value, (int, float)) and value > 0:
            out.append(float(value))
    add(ctx.get("price"))
    for level in ctx["levels"]:
        add(level["price"])
    for flag in ctx["flags"]:
        for key in ("price", "stop", "target"):
            add(flag.get(key))
    for row in ctx["contracts"]:
        for key in ("strike", "bid", "ask", "breakeven"):
            add(row.get(key))
    for level in (ctx.get("facts") or {}).get("levels_near_price") or []:
        add(level.get("price"))
    for row in ctx["positions"] + ctx["plans"]:
        for key in ("stop", "target", "price", "entry_price", "exit_price"):
            add(row.get(key))
        add((row.get("trigger") or {}).get("price") if isinstance(row.get("trigger"), dict) else None)
    return out


def _known(value: float, known: List[float]) -> bool:
    return any(abs(value - k) <= max(0.011, k * 0.0005) for k in known)


def unverified(text: str, ctx: Dict[str, Any]) -> List[float]:
    """Prices in ``text`` (numbers within half and twice the last price) that the state doesn't show."""
    price, known = ctx.get("price") or 0, known_prices(ctx)
    found = []
    for match in PRICE.finditer(text or ""):
        value = float(match.group(1))
        if price and 0.5 * price <= value <= 2 * price and not _known(value, known):
            found.append(value)
    return found


def read_note(data: Dict[str, Any], ctx: Dict[str, Any], *, answer: bool = False) -> Dict[str, Any]:
    """The model's note (or answer) and call, checked; ValueError when it quoted a price the state doesn't show."""
    text = str(data.get("answer" if answer else "note") or "").strip()[:1200]
    if not text:
        raise ValueError("the model wrote nothing")
    wrong = unverified(text, ctx)
    call = data.get("call") if not answer and isinstance(data.get("call"), dict) else None
    if call:
        trigger = call.get("trigger") if isinstance(call.get("trigger"), dict) else {}
        for value in (call.get("stop"), call.get("target"), trigger.get("price")):
            if isinstance(value, (int, float)) and not _known(float(value), known_prices(ctx)):
                wrong.append(float(value))
    if wrong:
        raise ValueError(f"it quoted {', '.join(f'{v:g}' for v in wrong[:3])}, which the system doesn't show")
    return {"text": text, "call": call}


def ask_model(system: str, ctx: Dict[str, Any], question: Optional[str] = None, *,
              images: Optional[List[Dict[str, str]]] = None, chain: Optional[str] = None,
              generate: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    payload = {**ctx, "question": question} if question else ctx
    kwargs: Dict[str, Any] = {"images": images} if images else {}
    if chain:
        kwargs["chain"] = chain
    return (generate or assistant_model.generate)(json.dumps(payload, ensure_ascii=False, default=str), system, **kwargs)
