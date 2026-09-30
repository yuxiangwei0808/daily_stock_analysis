"""Your own trade journal from moomoo fills (read-only): round trips and realized P&L.

Fills come from ``history_deal_list_query`` (the last year, see ``providers.broker_deals``).
Per contract they are matched first-in, first-out into round trips: a buy against an open
short closes it, a sell against an open long closes it, the rest opens a new lot. An option
still open after its expiry is settled at intrinsic value from the underlying's close on the
expiry day (worthless when that close is unknown, flagged). Spreads appear as their legs.
Amounts are gross of commissions (the fill history does not carry them).

Each round trip is grouped three ways: by type (long/short stock, calls, puts), by holding
time, and by whether it agreed with one of the system's tracked signals on the same ticker in
the five days before it was opened (a trade idea, breakout, report call, social pick or
YouTube call), to compare your own results with and without the system.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

SIGNAL_DAYS = 5
HOLD_BUCKETS = (("same_day", "Same day", 0, 0), ("days_1_5", "1–5 days", 1, 5), ("days_6_20", "6–20 days", 6, 20),
                ("over_20", "Over 20 days", 21, 10 ** 6))


def _when(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("/", "-")[:19])


def round_trips(deals: List[Dict[str, Any]], today: date,
                expiry_close: Optional[Callable[[str, date], Optional[float]]] = None) -> List[Dict[str, Any]]:
    """Closed round trips from fills, oldest first; expired options settle at intrinsic value."""
    from .holdings import parse_code
    lots: Dict[str, List[Dict[str, Any]]] = {}
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

    for deal in deals:
        code = deal["code"][3:] if deal["code"].startswith("US.") else deal["code"]
        info = parse_code(code)
        side = str(deal["side"]).upper()
        signed = deal["qty"] if side in ("BUY", "BUY_BACK") else -deal["qty"] if side in ("SELL", "SELL_SHORT") else 0
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
        underlying = expiry_close(info["underlying"], info["expiry"]) if expiry_close else None
        value = (max(0.0, underlying - info["strike"]) if info["right"] == "call" else max(0.0, info["strike"] - underlying)) \
            if underlying is not None else 0.0
        for lot in book:
            close(code, info, lot, abs(lot["qty"]), value, datetime.combine(info["expiry"], datetime.min.time()),
                  "expired" if underlying is not None else "expired, assumed worthless")
    return sorted(trips, key=lambda trip: trip["closed"])


def _stats(trips: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [trip["pnl"] for trip in trips]
    returns = [trip["return_pct"] for trip in trips if trip["return_pct"] is not None]
    return {"trades": len(trips), "total_pnl": round(sum(pnls), 2),
            "win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls) * 100, 1) if pnls else None,
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "avg_hold_days": round(sum(trip["hold_days"] for trip in trips) / len(trips), 1) if trips else None}


def signal_match(trip: Dict[str, Any], signals: List[Dict[str, Any]]) -> str:
    """"agreed" / "against" / "none": the latest tracked signal on the ticker up to five days before the open."""
    opened = trip["opened"][:10]
    earliest = (date.fromisoformat(opened) - timedelta(days=SIGNAL_DAYS)).isoformat()
    related = [signal for signal in signals if signal.get("ticker") == trip["ticker"]
               and earliest <= str(signal.get("signal_day", ""))[:10] <= opened
               and signal.get("direction") in ("long", "short")]
    if not related:
        return "none"
    latest = max(related, key=lambda signal: str(signal.get("signal_day")))
    return "agreed" if latest["direction"] == trip["view"] else "against"


def build(deals: List[Dict[str, Any]], signals: List[Dict[str, Any]], today: date,
          expiry_close: Optional[Callable[[str, date], Optional[float]]] = None) -> Dict[str, Any]:
    trips = round_trips(deals, today, expiry_close)
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
        "total": _stats(trips),
        "first_fill": deals[0]["time"][:10] if deals else None,
        "by_type": [{"label": label, **_stats(rows)} for label, rows in sorted(types.items(), key=lambda item: -len(item[1]))],
        "by_hold": [{"key": key, "label": label, **_stats([t for t in trips if low <= t["hold_days"] <= high])}
                    for key, label, low, high in HOLD_BUCKETS],
        "by_signal": [{"key": key, "label": label, **_stats([t for t in trips if t["signal"] == key])}
                      for key, label in (("agreed", "Agreed with a system signal"), ("against", "Against a system signal"),
                                         ("none", "No system signal"))],
        "by_underlying": sorted(({"ticker": ticker, **_stats(rows)} for ticker, rows in by_underlying.items()),
                                key=lambda item: -abs(item["total_pnl"]))[:10],
        "best": sorted(trips, key=lambda trip: -trip["pnl"])[:5],
        "worst": sorted(trips, key=lambda trip: trip["pnl"])[:5],
        "open_lots_note": "Open positions are not counted until they are closed or expire.",
    }
