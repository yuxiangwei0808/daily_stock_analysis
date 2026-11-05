"""Shadow mode for exits: what the system would have sold, when and at what quote. Nothing is sent.

This module grades; it holds no broker connection and no order code, and ``tests/test_shadow_exits.py``
fails if order calls appear anywhere in the app. ``shadow_watch.py`` hands it the live quotes and
keeps its two settings (``shadow_exits``, ``shadow_levels``). On each check of the regular session:

- **Levels:** a position's stop, target, P&L levels and action join a history when they change, so
  levels replaced before they were hit can still be graded.
- **Triggers:** the first time a level is crossed while you hold the position, one record per level:
  the plan's or your own stop and target, P&L levels, the built-in option levels, a held stock's
  first break of its 20-day low, and a review saying trim, take profit or close. Each records how it
  was seen: the quote's source, its age on arrival and the wait for the check. A level already crossed
  when watching starts or resumes has no trigger moment to grade: it is counted as ``carried``.
  Adding a contract keeps the holding; replacing them all starts a new one, which ignores the old plan.
- **Exit:** the would-be order closes the whole position at a limit on the side you would be filled
  at (the bid to sell, the ask to buy back). It waits while a quote is stale or too wide, a long
  option has no bid, or the position list is old, which are the checks a live version would make.
  The wait is recorded, and after ``EXIT_MAX_WAIT`` it gives up: a later price would not grade this
  trigger. Spreads are recorded too but flagged ``multi_leg``: the first live version would skip them.
- **Follow-up:** one mark a day, updated until the close, for 10 sessions or until expiry, also
  after you close the position, so selling can be compared with holding and with what you did.
  On expiration day the mark is what the contract settles at: its intrinsic value at the close.

Results are P&L on cost per unit. Sizes and amounts are never stored. Fills at the bid or ask are
assumed and commissions are ignored, so a real order could do worse.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import position_plans

EXITS_SETTING = "shadow_exits"
LEVELS_SETTING = "shadow_levels"
FEED_SETTING = "shadow_feed"  # the live feed's measured delays, per day
FOLLOW_SESSIONS = 10
HELD_SESSIONS = (5, 10)
STALE_SESSIONS = 5  # marks missing this long past the follow-up (the server was off): stop waiting
MARK_MINUTES = 10  # before the close: the mark updates from here on, so the last one stands
POSITION_MAX_AGE = timedelta(seconds=60)  # the watcher reads the list every 15 seconds
EXIT_MAX_WAIT = timedelta(minutes=5)
CARRIED_WINDOW = timedelta(minutes=5)  # after the very first check, long enough for the day's levels to load
EXIT_ACTIONS = ("close", "take_profit", "trim")
MIN_FINISHED = 30
MAX_EXITS = 1000
MAX_LEVEL_CHANGES = 5000
RECENT = 12
NOTE = "Shadow mode: nothing is sent to moomoo. This is what the system would have sold."
BASIS = {"stop": "price", "target": "price", "trend": "trend", "pnl_stop": "pnl", "pnl_target": "pnl",
         "rule_loss": "pnl", "rule_profit": "pnl"}  # what a level is judged on; a review's action needs no quote
KINDS = {"stop": "Stop", "target": "Target", "pnl_stop": "P&L cut", "pnl_target": "P&L take-profit",
         "rule_loss": "Built-in option loss", "rule_profit": "Built-in option profit", "trend": "20-day low",
         "review": "Review action"}


def spread(values: Iterable[float]) -> Optional[Dict[str, Any]]:
    """Median, 95th percentile and count; None without values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return {"median": round(statistics.median(ordered), 3), "p95": round(ordered[int(len(ordered) * 0.95)], 3),
            "n": len(ordered)}


def unit(row: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """The position per unit, without its size: legs as ``{code, ratio}`` and the net cost."""
    if row["type"] == "stock":
        sign = 1 if row["qty"] > 0 else -1
        cost = row.get("average_cost")
        return [{"code": row["ticker"], "ratio": sign}], (sign * cost if cost and cost > 0 else None)
    size = min(abs(leg["qty"]) for leg in row["legs"])
    legs = [{"code": leg["code"], "ratio": leg["qty"] / size} for leg in row["legs"]]
    known = all(leg.get("average_cost") is not None for leg in row["legs"])
    return legs, (sum(leg["qty"] / size * leg["average_cost"] for leg in row["legs"]) if known else None)


def _values(legs: List[Dict[str, Any]], quotes: Dict[str, Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """(fill, mid) per unit, closing each leg on its side of the quote; None without every quote."""
    from .holdings import _mark
    fill = mid = 0.0
    for leg in legs:
        quote = quotes.get(leg["code"])
        mark = _mark(quote, None)
        if mark is None:
            return None
        side = quote.get("bid") if leg["ratio"] > 0 else quote.get("ask")
        fill += leg["ratio"] * (side or mark)  # a stock snapshot can lack a book; no bid on an option blocks first
        mid += leg["ratio"] * mark
    return fill, mid


def on_cost(record: Dict[str, Any], value: Optional[float]) -> Optional[float]:
    cost = record.get("cost")
    return round((value - cost) / abs(cost) * 100, 2) if value is not None and cost else None


def _fresh(code: str, quotes: Dict[str, Dict[str, Any]], now: datetime) -> bool:
    from .holdings import parse_code
    from .quality import watch_quote_fresh
    return watch_quote_fresh(quotes.get(code), now, option=parse_code(code)["kind"] == "option")


def blockers(record: Dict[str, Any], quotes: Dict[str, Dict[str, Any]], now: datetime,
             synced_at: Optional[str]) -> List[str]:
    """Why the would-be order waits this minute; empty when it would go."""
    from .holdings import _wide, parse_code
    out = set()
    for leg in record["legs"]:
        code, quote = leg["code"], quotes.get(leg["code"])
        if not _fresh(code, quotes, now):
            out.add("stale_quote")
            continue
        if _wide(quote):
            out.add("wide_quote")
        if leg["ratio"] > 0 and parse_code(code)["kind"] == "option" and not quote.get("bid"):
            out.add("no_bid")
    try:
        if now - datetime.fromisoformat(str(synced_at)) > POSITION_MAX_AGE:
            out.add("old_position")
    except (TypeError, ValueError):
        out.add("old_position")
    return sorted(out)


def triggers(row: Dict[str, Any], merged: Dict[str, Any], price: Optional[float],
             low20: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every exit level this position has crossed right now."""
    from .holdings import LOSS_ON_COST, PROFIT_OF_MAX, PROFIT_ON_COST

    def hit(kind, level, source, basis=""):
        return {"kind": kind, "level": level, "source": source, "basis": str(basis or "")[:160]}

    state = position_plans.status(merged, price)
    out = [hit(kind, merged[kind], merged.get(f"{kind}_source", ""), merged.get(f"{kind}_basis"))
           for kind in ("stop", "target") if state == f"{kind}_hit"]
    pnl = row.get("pnl_pct")
    if row["type"] == "option" and pnl is not None and not row.get("quotes_wide"):  # the alerts' own guard
        stop, target = merged.get("pnl_stop_pct"), merged.get("pnl_target_pct")
        if stop is not None and pnl <= stop:
            out.append(hit("pnl_stop", stop, merged.get("pnl_stop_source") or "review"))
        if target is not None and pnl >= target:
            out.append(hit("pnl_target", target, merged.get("pnl_target_source") or "review"))
        if row.get("pct_of_max") is not None:
            if row["pct_of_max"] >= PROFIT_OF_MAX:
                out.append(hit("rule_profit", PROFIT_OF_MAX, "built-in", "of the maximum profit"))
        else:
            out += [hit("rule_profit", level, "built-in", "on cost") for level in PROFIT_ON_COST if pnl >= level]
        if pnl <= LOSS_ON_COST:
            out.append(hit("rule_loss", LOSS_ON_COST, "built-in", "on cost"))
    if row["type"] == "stock" and row["qty"] > 0 and low20 and price and price < low20:
        out.append(hit("trend", "low20", "built-in", f"below the 20-day low {low20:.2f}"))
    if merged.get("action") in EXIT_ACTIONS and merged.get("reviewed_at"):
        out.append(hit("review", merged["action"], "review", merged.get("reason")))
    return out


def _signs(row: Dict[str, Any]) -> Dict[str, int]:
    """Each contract the position holds, and on which side."""
    if row["type"] == "stock":
        return {row["ticker"]: 1 if row["qty"] > 0 else -1}
    return {leg["code"]: 1 if leg["qty"] > 0 else -1 for leg in row["legs"]}


def _outdated(plan: Dict[str, Any], holding: Dict[str, Any]) -> bool:
    """A plan or review from before you opened this holding belongs to an earlier one on the same key."""
    made = max(str(plan.get("created_at") or ""), str(plan.get("reviewed_at") or ""))
    return bool(holding.get("opened")) and made < holding["since"]


def outdated_plans(exits: Dict[str, Any], plans: Dict[str, Dict[str, Any]]) -> List[str]:
    """Positions you reopened whose plan is still the old one: these need a new plan."""
    return [key for key, holding in (exits.get("holdings") or {}).items() if key in plans and _outdated(plans[key], holding)]


def _seen(record: Dict[str, Any], quotes: Dict[str, Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
    """The quote that showed this crossing: its source, its age on arrival and the wait for this check."""
    on_price = record["kind"] in ("stop", "target", "trend")
    if record["kind"] == "review":
        return None
    codes = [record["ticker"]] if on_price else [leg["code"] for leg in record["legs"]]
    arrived = [quotes[code] for code in codes if (quotes.get(code) or {}).get("received")]
    if not arrived:
        return None
    quote = max(arrived, key=lambda item: item["received"])
    stamp = quote.get("data_at") if on_price else quote.get("book_at")  # a thin option's last trade says nothing here
    return {"source": quote.get("source"), "check_ms": round((now - quote["received"]).total_seconds() * 1000, 1),
            "quote_age_s": round((quote["received"] - stamp).total_seconds(), 1) if stamp else None}


def _record(row: Dict[str, Any], found: Dict[str, Any], price: Optional[float], now: datetime) -> Dict[str, Any]:
    from .holdings import _local, describe_option
    legs, cost = unit(row)
    signature = row.get("signature", "")
    label = f"{row['ticker']} shares" if row["type"] == "stock" else describe_option(row, with_days=False).split(" · ")[0]
    return {"id": f"{row['key']}:{signature}:{found['kind']}:{found['level']}", "key": row["key"], "signature": signature,
            "ticker": position_plans.ticker_of(row), "type": row["type"], "label": label,
            "exposure": position_plans.exposure_of(row), "expiry": row.get("expiry"), **found,
            "at": now.isoformat(), "day": _local(now).date().isoformat(), "price": price,
            "pnl_pct": row.get("pnl_pct"), "legs": legs, "cost": cost,
            "skip": "multi_leg" if len(legs) > 1 else None, "problems": [], "exit": None, "closed": None,
            "marks": [], "ended": None}


def _end(record: Dict[str, Any]) -> None:
    record["ended"] = "no_exit" if not record.get("exit") else "done" if record.get("marks") else "unmarked"


def _mark_value(record: Dict[str, Any], quotes: Dict[str, Dict[str, Any]], now: datetime,
                expiring: bool) -> Optional[float]:
    """Today's value per unit at the mid; on expiration day what the contract settles at, its intrinsic value."""
    from .holdings import parse_code
    if expiring and _fresh(record["ticker"], quotes, now):
        spot = quotes[record["ticker"]]["price"]
        total = 0.0
        for leg in record["legs"]:
            info = parse_code(leg["code"])
            total += leg["ratio"] * max(0.0, spot - info["strike"] if info["right"] == "call" else info["strike"] - spot)
        return total
    if all(_fresh(leg["code"], quotes, now) for leg in record["legs"]):
        values = _values(record["legs"], quotes)
        if values:
            return values[1]
    return None


def _closed_at(record: Dict[str, Any], quotes: Dict[str, Dict[str, Any]],
               fills: Optional[Dict[str, Dict[str, Dict[str, Any]]]]) -> Dict[str, Any]:
    """What you got: your own closing fills since the trigger when every leg has one, else the mid when the position was seen gone."""
    triggered = datetime.fromisoformat(record["at"])
    prices = []
    for leg in record["legs"]:
        fill = ((fills or {}).get(leg["code"]) or {}).get("sell" if leg["ratio"] > 0 else "buy")
        prices.append(fill["price"] if fill and fill["at"] >= triggered else None)
    if all(price is not None for price in prices):
        value = sum(leg["ratio"] * price for leg, price in zip(record["legs"], prices))
        return {"pnl_pct": on_cost(record, value), "from": "fill"}
    values = _values(record["legs"], quotes)
    return {"pnl_pct": on_cost(record, values[1]) if values else None, "from": "mid"}


def follow(record: Dict[str, Any], quotes: Dict[str, Dict[str, Any]], held: Dict[str, int], now: datetime,
           synced_at: Optional[str], marking: bool, fills: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None) -> bool:
    """One check of an unfinished record; True when it changed."""
    from .holdings import _local, trading_days_until
    day = _local(now).date()
    today = day.isoformat()
    started = date.fromisoformat(record["day"])
    expiry = date.fromisoformat(record["expiry"]) if record.get("expiry") else None
    sessions = trading_days_until(day, started)
    marks = record["marks"]
    followed = bool(marks) and marks[-1]["n"] >= FOLLOW_SESSIONS and marks[-1]["day"] != today
    if followed or (expiry and day > expiry) or sessions > FOLLOW_SESSIONS + STALE_SESSIONS:
        _end(record)
        return True
    changed = False
    still_held = all(held.get(leg["code"]) == (1 if leg["ratio"] > 0 else -1) for leg in record["legs"])
    if not record.get("exit"):
        if not still_held:  # you closed it before the would-be order could go
            _end(record)
            return True
        waited = now - datetime.fromisoformat(record["at"])
        if waited > EXIT_MAX_WAIT:  # a later price would not grade this trigger
            record["gave_up"] = True
            _end(record)
            return True
        problems = blockers(record, quotes, now, synced_at)
        values = None if problems else _values(record["legs"], quotes)
        if values:
            record["exit"] = {"at": now.isoformat(), "wait_s": round(waited.total_seconds(), 1),
                              "fill": round(values[0], 4), "mid": round(values[1], 4),
                              "pnl_pct": on_cost(record, values[0]), "mid_pnl_pct": on_cost(record, values[1]),
                              "legs": [{"code": leg["code"], "bid": quotes[leg["code"]].get("bid"),
                                        "ask": quotes[leg["code"]].get("ask")} for leg in record["legs"]]}
            problems = []
            changed = True
        if problems != record.get("problems"):
            record["problems"] = problems
            changed = True
    elif not record.get("closed") and not still_held:
        record["closed"] = {"at": now.isoformat(), **_closed_at(record, quotes, fills)}
        changed = True
    if marking:
        value = _mark_value(record, quotes, now, day == expiry)
        if value is not None:
            spot = quotes.get(record["ticker"]) or {}
            mark = {"day": today, "n": sessions, "value": round(value, 4), "pnl_pct": on_cost(record, value),
                    "price": spot.get("price")}
            if not marks or marks[-1]["day"] != today:
                marks.append(mark)
                changed = True
            elif marks[-1] != mark:
                marks[-1] = mark
                changed = True
    return changed


def _levels_of(merged: Dict[str, Any]) -> Dict[str, Any]:
    return {"stop": merged.get("stop"), "target": merged.get("target"), "pnl_stop_pct": merged.get("pnl_stop_pct"),
            "pnl_target_pct": merged.get("pnl_target_pct"), "action": merged.get("action") or "",
            "stop_from": position_plans.note(merged, "stop"), "target_from": position_plans.note(merged, "target")}


def observe(exits: Dict[str, Any], levels: Dict[str, Any], view: Dict[str, Any], plans: Dict[str, Dict[str, Any]],
            rules: List[Dict[str, Any]], prices: Dict[str, float], quotes: Dict[str, Dict[str, Any]], now: datetime,
            *, marking: bool = False, price_levels: Optional[Dict[str, Dict[str, Any]]] = None, blind: bool = False,
            seen: Optional[set] = None, trend_blind: bool = False,
            fills: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None) -> Tuple[bool, bool]:
    """One check: new level versions, new triggers, and every unfinished record. Returns (exits, levels) changed.

    A level found crossed has no trigger moment, and is counted as carried, when:

    - ``blind``: watching resumes with this check, so the past of what is held now is unknown;
    - ``seen``: watching lapsed earlier (None if it never did) and this holding's price, P&L or 20-day low has
      not been judged since, because subscribing takes a moment and the first checks may not have priced it;
    - ``trend_blind``: today's 20-day lows only arrived, late, with this check.
    """
    records = exits.setdefault("exits", [])
    exits_changed = not exits.get("started_at")
    exits.setdefault("started_at", now.isoformat())
    unknown = blind or now - datetime.fromisoformat(exits["started_at"]) < CARRIED_WINDOW
    latest, changes = levels.setdefault("latest", {}), levels.setdefault("changes", [])
    holdings = exits.setdefault("holdings", {})
    rows = position_plans.open_rows(view)
    bases = ("price", "pnl", "trend")

    def forget(key):
        if seen is not None:
            seen.difference_update((key, basis) for basis in bases)

    for key in [key for key in holdings if key not in {row["key"] for row in rows}]:
        del holdings[key]  # a position you closed starts over if you reopen it
        forget(key)
        exits_changed = True
    levels_changed = False
    held: Dict[str, int] = {}
    for row in rows:
        key, signs = row["key"], _signs(row)
        held.update(signs)
        holding = holdings.get(key)
        # Adding a contract keeps the holding. Replacing them all (out of the calls, into puts) is a new one.
        if holding is None or not set(holding["codes"]) & set(signs):
            holding = holdings[key] = {"since": now.isoformat(), "codes": sorted(signs), "crossed": [], "opened": not unknown}
            exits_changed = True
            forget(key)
            if seen is not None and not unknown:  # opened while watching: nothing about it was missed
                seen.update((key, basis) for basis in bases)
        elif set(holding["codes"]) != set(signs):
            holding["codes"] = sorted(signs)
            exits_changed = True
        plan = plans.get(key)
        merged = position_plans.resolve(None if plan and _outdated(plan, holding) else plan, row, rules)
        ticker = position_plans.ticker_of(row)
        price = prices.get(ticker)
        current = _levels_of(merged)
        if latest.get(key) != current:
            latest[key] = current
            changes.append({"at": now.isoformat(), "key": key, "ticker": ticker, "price": price,
                            "pnl_pct": row.get("pnl_pct"), **current})
            levels_changed = True
        low20 = ((price_levels or {}).get(ticker) or {}).get("low20")
        for found in triggers(row, merged, price, low20):
            crossing = f"{found['kind']}:{found['level']}"
            if crossing in holding["crossed"]:
                continue
            holding["crossed"].append(crossing)
            basis = BASIS.get(found["kind"])
            carried = (unknown or (seen is not None and basis is not None and (key, basis) not in seen)
                       or (trend_blind and basis == "trend"))
            record = _record(row, found, price, now)
            record.update(held_since=holding["since"], ended="carried" if carried else None, seen=_seen(record, quotes, now))
            records.append(record)
            exits_changed = True
        if seen is not None:
            seen.update((key, basis) for basis, known in (("price", price), ("pnl", row.get("pnl_pct") is not None),
                                                          ("trend", price and low20)) if known)
    for record in records:
        if not record.get("ended") and follow(record, quotes, held, now, view.get("synced_at"), marking, fills):
            exits_changed = True
    if len(records) > MAX_EXITS:
        exits["exits"] = records[-MAX_EXITS:]
    if len(changes) > MAX_LEVEL_CHANGES:
        levels["changes"] = changes[-MAX_LEVEL_CHANGES:]
    return exits_changed, levels_changed


def codes(exits: Dict[str, Any], today: date, *, legs_only: bool = False) -> List[str]:
    """What unfinished records need quoted, including contracts you have already closed."""
    out: List[str] = []
    for record in exits.get("exits") or []:
        if record.get("ended") or (record.get("expiry") and record["expiry"] < today.isoformat()):
            continue
        out += [leg["code"] for leg in record["legs"]] + ([] if legs_only else [record["ticker"]])
    return list(dict.fromkeys(out))


def grade(record: Dict[str, Any]) -> Dict[str, Any]:
    """If sold at the would-be fill, if held for 5 and 10 sessions (or to expiry), and what you actually did."""
    marks = record.get("marks") or []
    done = record.get("ended") == "done" and bool(marks)
    held = {}
    for horizon in HELD_SESSIONS:
        mark = next((item for item in marks if item["n"] >= horizon), marks[-1] if done else None)
        held[horizon] = mark.get("pnl_pct") if mark else None
    sold = (record.get("exit") or {}).get("pnl_pct")
    final = held[HELD_SESSIONS[-1]]
    closed = (record.get("closed") or {}).get("pnl_pct")
    return {"if_sold_pct": sold, "if_held_5_pct": held[5], "if_held_10_pct": final,
            "yours_pct": closed if closed is not None else final,
            "finished": done and sold is not None and final is not None}


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _row(label: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    graded = [(record, grade(record)) for record in records]
    finished = [item for _, item in graded if item["finished"]]
    gaps = [item["if_sold_pct"] - item["if_held_10_pct"] for item in finished]
    return {"label": label, "positions": len(records), "tickers": len({record["ticker"] for record in records}),
            "finished": len(finished), "running": sum(1 for record in records if not record.get("ended")),
            "no_exit": sum(1 for record in records if record.get("ended") == "no_exit"),
            "gave_up": sum(1 for record in records if record.get("gave_up")),
            "if_sold_pct": _mean([item["if_sold_pct"] for item in finished]),
            "if_held_5_pct": _mean([item["if_held_5_pct"] for item in finished]),
            "if_held_10_pct": _mean([item["if_held_10_pct"] for item in finished]),
            "yours_pct": _mean([item["yours_pct"] for item in finished]),
            "sold_minus_held_pct": _mean(gaps),
            "sold_minus_held_median": round(statistics.median(gaps), 2) if gaps else None,
            "sold_better_pct": round(sum(gap > 0 for gap in gaps) / len(gaps) * 100) if gaps else None}


def _firsts(records: List[Dict[str, Any]], by_kind: bool = False) -> List[Dict[str, Any]]:
    """Each holding counts once: selling on its first trigger leaves nothing for later ones, until it is reopened."""
    seen = set()
    out = []
    for record in sorted(records, key=lambda item: item["at"]):
        key = (record["key"], record.get("held_since", ""), record["kind"] if by_kind else "")
        if key not in seen:
            seen.add(key)
            out.append(record)
    return out


def _speed(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How quickly crossings were seen and would-be orders could go, measured on the records themselves."""
    seen = [record["seen"] for record in records if record.get("seen")]
    return {"from_push": sum(1 for item in seen if item.get("source") == "push"),
            "from_snapshot": sum(1 for item in seen if item.get("source") == "snapshot"),
            "quote_age_s": spread(item["quote_age_s"] for item in seen if item.get("quote_age_s") is not None),
            "check_ms": spread(item["check_ms"] for item in seen),
            "exit_wait_s": spread(record["exit"]["wait_s"] for record in records if (record.get("exit") or {}).get("wait_s") is not None),
            "gave_up": sum(1 for record in records if record.get("gave_up"))}


def _verdict(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    from .idea_tracker import _month_t, t_needed
    values = []
    for record in records:
        item = grade(record)
        if item["finished"]:
            values.append((record["day"], item["if_sold_pct"] - item["if_held_10_pct"]))
    mean, t, months = _month_t(values)
    needed = t_needed(months)
    if len(values) < MIN_FINISHED or needed is None:
        verdict = "too_early"
    elif t is None or abs(t) < needed:
        verdict = "no_clear_difference"
    else:
        verdict = "selling_better" if t > 0 else "holding_better"
    return {"verdict": verdict, "finished": len(values), "needed": MIN_FINISHED, "months": months,
            "t": round(t, 2) if t is not None else None, "t_needed": needed}


def summary(exits: Dict[str, Any], levels: Dict[str, Any]) -> Dict[str, Any]:
    """The results: first trigger per position, by position type and by trigger."""
    everything = exits.get("exits") or []
    records = [record for record in everything if record.get("ended") != "carried"]
    # A carried first trigger still counts as the first: the would-be order would have gone then, so nothing later is graded.
    def graded(kept):
        return [record for record in kept if record.get("ended") != "carried"]
    firsts = graded(_firsts(everything))
    rows = [_row("All positions", firsts),
            _row("Single options", [r for r in firsts if r["type"] == "option" and not r.get("skip")]),
            _row("Spreads (the first live version would skip these)", [r for r in firsts if r.get("skip")]),
            _row("Stocks and funds", [r for r in firsts if r["type"] == "stock"])]
    by_trigger = [_row(label, graded(_firsts([r for r in everything if r["kind"] == kind], by_kind=True)))
                  for kind, label in KINDS.items()]
    changes = levels.get("changes") or []
    recent = [{**{key: record.get(key) for key in ("label", "ticker", "kind", "level", "source", "at", "price", "ended",
                                                   "skip", "problems", "gave_up", "seen")},
               "wait_s": (record.get("exit") or {}).get("wait_s"),
               "kind_label": KINDS.get(record["kind"], record["kind"]), **grade(record)}
              for record in sorted(records, key=lambda item: item["at"], reverse=True)[:RECENT]]
    return {"note": NOTE, "recorded": len(records), "carried": len(everything) - len(records), "level_changes": len(changes),
            "since": min([*(r["at"] for r in records), *(c["at"] for c in changes)], default=None),
            **_verdict(firsts), "speed": _speed(records), "rows": [row for row in rows if row["positions"]],
            "by_trigger": [row for row in by_trigger if row["positions"]], "recent": recent}
