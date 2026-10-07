"""Your own trade journal from moomoo fills (read-only): round trips and realized P&L.

Fills come from ``history_deal_list_query`` (the last year, see ``providers.broker_deals``).
Per contract they are matched first-in, first-out into round trips: a buy against an open
short closes it, a sell against an open long closes it, the rest opens a new lot. An option
still open after expiry that finished out of the money (the underlying's actual close on the expiry
day) expired worthless: a certain result, so it counts. One that finished in the money, or whose
close is unknown, may have been exercised or assigned and requires reconciliation instead. Histories with
unexplained inventory are excluded until their cost basis can be established. Spreads appear as their legs.
Amounts are gross of commissions (the fill history does not carry them).

Each round trip is grouped three ways: by type (long/short stock, calls, puts), by holding
time, and by whether it agreed with one of the system's tracked signals on the same ticker in
the five days before it was opened (a trade idea, breakout, report call, social pick or
YouTube call), to compare your own results with and without the system.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, List, Optional

CALCULATION_VERSION = 3  # bump whenever saved journals must be rebuilt
SIGNAL_DAYS = 5
_NEW_YORK = ZoneInfo("America/New_York")  # moomoo reports US fills in New York time
HOLD_BUCKETS = (("same_day", "Same day", 0, 0), ("days_1_5", "1–5 days", 1, 5), ("days_6_20", "6–20 days", 6, 20),
                ("over_20", "Over 20 days", 21, 10 ** 6))


def _when(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("/", "-")[:19])


def split_adjusted(deals: List[Dict[str, Any]], splits: Dict[str, List[tuple]]) -> List[Dict[str, Any]]:
    """Stock fills restated in today's shares: a fill before a 10:1 split counts 10x the shares at a
    tenth of the price (the broker's holdings, and later fills, are in post-split shares)."""
    from .holdings import parse_code
    out = []
    for deal in deals:
        info = parse_code(_bare(deal["code"]))
        day = str(deal["time"]).replace("/", "-")[:10]
        ratio = 1.0
        if info["kind"] == "stock":
            for when, split in splits.get(info["ticker"]) or []:
                if when > day and split > 0:
                    ratio *= split
        out.append(deal if ratio == 1.0 else {**deal, "qty": deal["qty"] * ratio, "price": deal["price"] / ratio})
    return out


def starting_positions(deals: List[Dict[str, Any]], current: Dict[str, float], today: date) -> Dict[str, float]:
    """Held without a matching fill: today's quantity less the net fills.

    Shares bought before the window, or delivered by an assignment or exercise (neither is a fill),
    would otherwise make their later sale look like a new short. Expired options are no longer
    held; their residual lots require separate expiry reconciliation.
    """
    from .holdings import parse_code
    net: Dict[str, float] = {}
    for deal in deals:
        code = _bare(deal["code"])
        net[code] = net.get(code, 0.0) + _signed(deal)
    start = {}
    for code in set(net) | set(current):
        info = parse_code(code)
        if info["kind"] == "option" and info["expiry"] < today:
            continue
        quantity = current.get(code, 0.0) - net.get(code, 0.0)
        if abs(quantity) > 1e-9:
            start[code] = quantity
    return start


def _bare(code: str) -> str:
    return code[3:] if code.startswith("US.") else code


def _signed(deal: Dict[str, Any]) -> float:
    side = str(deal["side"]).upper()
    return deal["qty"] if side in ("BUY", "BUY_BACK") else -deal["qty"] if side in ("SELL", "SELL_SHORT") else 0.0


def round_trips(deals: List[Dict[str, Any]], today: date,
                expiry_close: Optional[Callable[[str, date], Optional[float]]] = None,
                starting: Optional[Dict[str, float]] = None, unmatched: Optional[List[int]] = None,
                unresolved: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Closed FIFO trades with known fills only.

    ``starting`` is an unexplained inventory difference, not a dated opening lot.
    All matching for affected codes is excluded: fills cannot date an assignment
    or establish which sale consumed shares held before the history window.
    ``expiry_close`` (underlying, day) gives the actual close on an expiry day: an option still open
    then that finished out of the money expired worthless; anything else needs reconciliation.
    """
    from .holdings import parse_code
    lots: Dict[str, List[Dict[str, Any]]] = {}
    ambiguous = {code for code, qty in (starting or {}).items() if abs(qty) > 1e-9}
    if unresolved is not None:
        unresolved.extend({"code": code, "reason": "inventory_difference", "quantity_difference": starting[code]}
                          for code in sorted(ambiguous))
    trips: List[Dict[str, Any]] = []

    def close(code: str, info: Dict[str, Any], lot: Dict[str, Any], qty: float, price: float, when: datetime,
              how: str) -> None:
        multiplier = 100 if info["kind"] == "option" else 1
        long = lot["qty"] > 0
        pnl = (price - lot["price"]) * qty * multiplier * (1 if long else -1)
        cost = lot["price"] * qty * multiplier
        kind = "stock" if info["kind"] == "stock" else info["right"]
        trips.append({"code": code, "ticker": info.get("underlying") or info["ticker"], "kind": kind,
                      "position": "long" if long else "short",
                      # the market view: long stock, long calls and short puts want the price up
                      "view": "long" if long == (kind in ("stock", "call")) else "short",
                      "qty": qty, "entry": lot["price"], "exit": price, "opened": lot["time"].isoformat(),
                      "closed": when.isoformat(), "hold_days": (when.date() - lot["time"].date()).days,
                      "pnl": round(pnl, 2), "return_pct": round(pnl / cost * 100, 2) if cost else None, "how": how})

    for deal in sorted(deals, key=lambda row: (row["time"], row.get("deal_id", ""))):
        code = _bare(deal["code"])
        if code in ambiguous:
            if unmatched is not None and _signed(deal) * starting[code] < 0:
                unmatched.append(1)
            continue
        info = parse_code(code)
        signed = _signed(deal)
        when = _when(deal["time"])
        book = lots.setdefault(code, [])
        while signed and book and (book[0]["qty"] > 0) != (signed > 0):
            lot = book[0]
            matched = min(abs(signed), abs(lot["qty"]))
            close(code, info, lot, matched, deal["price"], when, "closed")
            lot["qty"] += matched if lot["qty"] < 0 else -matched
            signed += -matched if signed > 0 else matched
            if abs(lot["qty"]) < 1e-9:
                book.pop(0)
        if abs(signed) > 1e-9:
            book.append({"qty": signed, "price": deal["price"], "time": when})
    for code, book in lots.items():
        info = parse_code(code)
        if info["kind"] != "option" or info["expiry"] >= today:
            continue
        close_on_expiry = expiry_close(info["underlying"], info["expiry"]) if expiry_close else None
        worthless = close_on_expiry is not None and (
            close_on_expiry < info["strike"] if info["right"] == "call" else close_on_expiry > info["strike"])
        for lot in book:
            if worthless:  # out of the money at expiry: nothing was exercised or assigned
                close(code, info, lot, abs(lot["qty"]), 0.0, datetime.combine(info["expiry"], datetime.min.time()),
                      "expired worthless")
            elif unresolved is not None:
                unresolved.append({"code": code, "reason": "expiry_reconciliation", "qty": lot["qty"],
                                   "opened": lot["time"].isoformat(), "expiry": info["expiry"].isoformat()})
    return sorted(trips, key=lambda trip: trip["closed"])


def _stats(trips: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [trip["pnl"] for trip in trips]
    returns = [trip["return_pct"] for trip in trips if trip["return_pct"] is not None]
    return {"trades": len(trips), "total_pnl": round(sum(pnls), 2),
            "win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls) * 100, 1) if pnls else None,
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "avg_hold_days": round(sum(trip["hold_days"] for trip in trips) / len(trips), 1) if trips else None}


def _known_before(signal: Dict[str, Any], opened: datetime) -> bool:
    """Whether the signal existed before the trade: its record was stored before the open (report
    calls are recorded after the close, so they count from the day after their report)."""
    day = str(signal.get("signal_day", ""))[:10]
    if signal.get("kind") == "verdict" or not signal.get("created_at"):
        return day < opened.date().isoformat()
    created = datetime.fromisoformat(str(signal["created_at"]))
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return created <= opened.replace(tzinfo=_NEW_YORK)


def signal_match(trip: Dict[str, Any], signals: List[Dict[str, Any]]) -> str:
    """"agreed" / "against" / "none": the latest tracked signal on the ticker known in the five days before the open."""
    opened_at = datetime.fromisoformat(trip["opened"])
    opened = trip["opened"][:10]
    earliest = (date.fromisoformat(opened) - timedelta(days=SIGNAL_DAYS)).isoformat()
    related = [signal for signal in signals if signal.get("ticker") == trip["ticker"]
               and earliest <= str(signal.get("signal_day", ""))[:10] <= opened
               and signal.get("direction") in ("long", "short") and _known_before(signal, opened_at)]
    if not related:
        return "none"
    latest = max(related, key=lambda signal: str(signal.get("signal_day")))
    return "agreed" if latest["direction"] == trip["view"] else "against"


def build(deals: List[Dict[str, Any]], signals: List[Dict[str, Any]], today: date,
          expiry_close: Optional[Callable[[str, date], Optional[float]]] = None,
          current: Optional[Dict[str, float]] = None, splits: Optional[Dict[str, List[tuple]]] = None) -> Dict[str, Any]:
    """``current`` is today's quantity per code, used to detect unexplained inventory;
    ``splits`` the (date, ratio) splits per stock ticker, to restate older fills in today's shares."""
    deals = split_adjusted(deals, splits or {})
    unmatched: List[int] = []
    unresolved: List[Dict[str, Any]] = []
    differences = starting_positions(deals, current, today) if current is not None else {}
    trips = round_trips(deals, today, expiry_close, differences, unmatched, unresolved)
    for trip in trips:
        trip["signal"] = signal_match(trip, signals)
    types = {}
    for trip in trips:
        label = ("Long " if trip["position"] == "long" else "Short ") + ("stock" if trip["kind"] == "stock" else trip["kind"] + "s")
        types.setdefault(label, []).append(trip)
    by_underlying: Dict[str, List[Dict[str, Any]]] = {}
    for trip in trips:
        by_underlying.setdefault(trip["ticker"], []).append(trip)
    return {
        "calculation_version": CALCULATION_VERSION, "unresolved": unresolved,
        "total": _stats(trips),
        "first_fill": deals[0]["time"][:10] if deals else None,
        "by_type": [{"label": label, **_stats(rows)} for label, rows in sorted(types.items(), key=lambda item: -len(item[1]))],
        "by_hold": [{"key": key, "label": label, **_stats([t for t in trips if low <= t["hold_days"] <= high])}
                    for key, label, low, high in HOLD_BUCKETS],
        "by_signal": [{"key": key, "label": label, **_stats([t for t in trips if t["signal"] == key])}
                      for key, label in (("agreed", "Agreed with a signal"), ("against", "Against a signal"),
                                         ("none", "No signal"))],
        "by_underlying": sorted(({"ticker": ticker, **_stats(rows)} for ticker, rows in by_underlying.items()),
                                key=lambda item: -abs(item["total_pnl"]))[:10],
        "best": sorted(trips, key=lambda trip: -trip["pnl"])[:5],
        "worst": sorted(trips, key=lambda trip: trip["pnl"])[:5],
        "open_lots_note": "Matched fills and options that expired out of the money count as realized results; in-the-money expiries and unexplained inventory require reconciliation.",
        "unmatched_closes": len(unmatched),  # sales of positions bought before the history (unknown cost)
    }
