"""moomoo's live quotes and fills on connections of their own, for what must react within the second.

Read only: quote and order-book push, this account's fill push, position reads and a snapshot every
so often. ``watch`` adds codes, ``quotes`` and ``raw`` give the latest state from memory without
touching OpenD, so a check never waits on the network. The push callbacks run on the SDK's thread.

The snapshot is a backstop: it re-confirms a thin contract whose book has not changed (no change
means no push), and keeps checks going, slower, if pushes stop. SPY trades constantly in the regular
session, so a silent SPY means the feed is frozen, not the market.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Optional, Set
from zoneinfo import ZoneInfo

from .holdings import parse_code
from .models import utcnow
from .providers import ProviderError, _safe_float, build_provider
from .shadow_exits import spread

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
CANARY = "SPY"
MAX_QUOTE_AGE = timedelta(seconds=45)  # with no push or snapshot this long, a quote is not used
MAX_MARKET_SILENCE = timedelta(seconds=30)  # SPY's last trade this old: the feed is frozen
SAME_MOMENT = timedelta(seconds=2)  # a push this recent beats a snapshot taken about now
OPEN_BOOK_GRACE = timedelta(minutes=2)  # a book that has not moved by then is today's; before, it may be yesterday's
LAG_SAMPLES = 5000
_CONNECTION_ERRORS = {"connection_unavailable", "request_timeout", "opend_not_configured", "provider_closed"}


def _local_time(text: Any) -> Optional[datetime]:
    """OpenD's US timestamps are New York time."""
    try:
        return datetime.fromisoformat(str(text).strip()).replace(tzinfo=_NEW_YORK)
    except ValueError:
        return None


class LiveFeed:
    def __init__(self, account: str, security_firm: str, *, provider: Any = None):
        self._account_id, self._firm = account, security_firm
        self._provider = provider
        self._account: Any = None
        self._lock = threading.Lock()
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._raw: Optional[Dict[str, Any]] = None
        self._held: Dict[str, str] = {}  # code -> the side of a fill that would close it
        self._read_at: Optional[datetime] = None
        self._vanished: Optional[Set[str]] = None
        self._fills: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._subscribed: Dict[str, Set[str]] = {"QUOTE": set(), "ORDER_BOOK": set()}
        self._refused: Set[tuple] = set()
        self._wanted: Set[str] = {CANARY}
        self.on_update: Callable[[], None] = lambda: None
        self.on_fill: Callable[[], None] = lambda: None
        self.pushes = {"quote": 0, "book": 0, "fill": 0}
        self.error: Optional[str] = None
        self.lags = {name: deque(maxlen=LAG_SAMPLES)
                     for name in ("stock_quote", "option_quote", "book", "fill_to_position")}

    def adopt(self, earlier: "LiveFeed") -> None:
        """Today's counts and delays carry over when the connections start over."""
        self.pushes, self.lags = earlier.pushes, earlier.lags

    def open(self, on_update: Callable[[], None], on_fill: Callable[[], None]) -> None:
        self.on_update, self.on_fill = on_update, on_fill
        self._provider = self._provider or build_provider("live")
        try:
            self._provider.listen_quotes(self._quote_push, self._book_push)
            self._account = self._provider.listen_account(self._account_id, self._firm, self._deal_push)
            self.watch([CANARY], [])
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for part in (self._account, self._provider):
            if part is not None:
                try:
                    part.close()
                except Exception as exc:  # closing the connection drops its subscriptions either way
                    logger.info("Live feed close: %s", type(exc).__name__)
        self._account = self._provider = None

    # -- pushes, on the SDK's callback thread ---------------------------------------------------
    def _entry(self, code: str) -> Dict[str, Any]:
        quote = self._quotes.get(code)
        if quote is None:
            quote = self._quotes[code] = {"session": "regular", "extended": None, "option": parse_code(code)["kind"] == "option"}
        return quote

    def _quote_push(self, row: Dict[str, Any]) -> None:
        try:
            code, price = str(row.get("code") or "")[3:], _safe_float(row.get("last_price"))
            if not code or not price or price <= 0:
                return
            arrived = utcnow()
            data_at = _local_time(f"{row.get('data_date')} {row.get('data_time')}")
            volume = _safe_float(row.get("volume"))
            with self._lock:
                quote = self._entry(code)
                traded = "volume" in quote and volume != quote["volume"]  # the first push is only the last known state
                quote.update(price=price, prev_close=_safe_float(row.get("prev_close_price")), volume=volume,
                             updated_at=f"{row.get('data_date')} {row.get('data_time')}", data_at=data_at,
                             received=arrived, pushed=arrived, fetched_at=arrived.isoformat(), source="push")
                self.pushes["quote"] += 1
                if traded and data_at:
                    self.lags["option_quote" if quote["option"] else "stock_quote"].append(
                        (arrived - data_at).total_seconds())
            self.on_update()
        except Exception as exc:  # the SDK's thread keeps delivering; Status shows this
            self.error = type(exc).__name__

    def _book_push(self, book: Dict[str, Any]) -> None:
        try:
            code = str(book.get("code") or "")[3:]
            bids, asks = book.get("Bid") or [], book.get("Ask") or []
            if not code:
                return
            arrived = utcnow()
            exchange = [stamp for stamp in (_local_time(book.get("svr_recv_time_bid")),
                                            _local_time(book.get("svr_recv_time_ask"))) if stamp]
            with self._lock:
                quote = self._entry(code)
                changed = "book_at" in quote and bool(exchange) and quote["book_at"] != max(exchange)
                self._set_book(quote, float(bids[0][0]) if bids else 0.0, float(asks[0][0]) if asks else None, arrived)
                quote.update(book_at=max(exchange) if exchange else None, received=arrived, pushed=arrived,
                             fetched_at=arrived.isoformat(), source="push")
                self.pushes["book"] += 1
                if changed:
                    self.lags["book"].append((arrived - max(exchange)).total_seconds())
            self.on_update()
        except Exception as exc:
            self.error = type(exc).__name__

    @staticmethod
    def _set_book(quote: Dict[str, Any], bid: float, ask: Optional[float], arrived: datetime) -> None:
        """``book_moved`` is when the book last changed; the first one seen says nothing about today."""
        if "ask" in quote and (quote.get("bid"), quote["ask"]) != (bid, ask):
            quote["book_moved"] = arrived
        quote.update(bid=bid, ask=ask)

    def _deal_push(self, row: Dict[str, Any]) -> None:
        try:
            if str(row.get("status") or "") not in ("OK", "CHANGED", "N/A", ""):
                return
            code, price = str(row.get("code") or "")[3:], _safe_float(row.get("price"))
            side = "sell" if "SELL" in str(row.get("trd_side") or "").upper() else "buy"
            with self._lock:
                if code and price:
                    self._fills.setdefault(code, {})[side] = {"price": price, "at": utcnow()}
                self.pushes["fill"] += 1
            self.on_fill()
        except Exception as exc:
            self.error = type(exc).__name__

    # -- reads, on the watcher's own thread -----------------------------------------------------
    def watch(self, quote_codes: Iterable[str], book_codes: Iterable[str]) -> None:
        """Subscribes what is new. A code OpenD refuses stays on the snapshot alone and is not asked again."""
        wanted = {"QUOTE": [*dict.fromkeys(quote_codes)], "ORDER_BOOK": [*dict.fromkeys(book_codes)]}
        self._wanted.update(wanted["QUOTE"], wanted["ORDER_BOOK"])
        for subtype, codes in wanted.items():
            missing = [code for code in codes if code not in self._subscribed[subtype]
                       and (code, subtype) not in self._refused]
            if not missing:
                continue
            try:
                self._provider.subscribe_push(missing, subtype)
                self._subscribed[subtype].update(missing)
            except ProviderError as error:
                if error.code in _CONNECTION_ERRORS:
                    raise
                for code in missing:
                    try:
                        self._provider.subscribe_push([code], subtype)
                        self._subscribed[subtype].add(code)
                    except ProviderError as single:
                        if single.code in _CONNECTION_ERRORS:
                            raise
                        logger.info("No %s push for %s: %s", subtype, code, single.code)
                        self._refused.add((code, subtype))

    def read_positions(self, now: datetime, *, refresh: bool = False) -> bool:
        """Reads the position list; False when a position vanished with no closing fill heard and needs a second look.

        A fill explains a vanished position only if it is on the closing side and newer than the last read that
        showed the position: the purchase that opened it, or an earlier trim, says nothing about this read.
        """
        rows = self._account.positions(refresh or self._vanished is not None or self._raw is None)
        held = {str(row["code"])[3:]: "buy" if row["qty"] < 0 or row.get("side") == "SHORT" else "sell" for row in rows}
        gone = set(self._held) - set(held)
        with self._lock:
            fills = {code: self._fills.get(code, {}).get(self._held[code]) for code in gone}
        closed = {code: fill for code, fill in fills.items() if fill and self._read_at and fill["at"] >= self._read_at}
        unexplained = gone - set(closed)
        if unexplained and self._vanished != unexplained:
            self._vanished = unexplained  # a position expired or assigned, or a bad read: the next read decides
            return False
        self._vanished = None
        with self._lock:
            for fill in closed.values():
                self.lags["fill_to_position"].append(max(0.0, (now - fill["at"]).total_seconds()))
            self._raw = {"positions": rows, "synced_at": now.isoformat()}
        self._held, self._read_at = held, now
        return True

    def refresh(self) -> None:
        """The snapshot backstop for every watched code."""
        rows = self._provider.watchlist_quotes(sorted(self._wanted))
        arrived = utcnow()
        with self._lock:
            for code, snapshot in rows.items():
                quote = self._entry(code)
                quote.update(received=arrived, fetched_at=arrived.isoformat())
                if quote.get("pushed") and arrived - quote["pushed"] < SAME_MOMENT:
                    continue
                quote.update(price=snapshot.get("price"), prev_close=snapshot.get("prev_close"),
                             volume=snapshot.get("volume"), updated_at=snapshot.get("updated_at"),
                             data_at=_local_time(snapshot.get("updated_at")), source="snapshot")
                if snapshot.get("ask"):
                    self._set_book(quote, snapshot.get("bid") or 0.0, snapshot["ask"], arrived)
        self.on_update()

    # -- the latest state, from memory ----------------------------------------------------------
    def raw(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._raw

    def fills(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Today's last fill per code and side: ``{code: {"sell": {"price", "at"}}}``."""
        with self._lock:
            return {code: dict(sides) for code, sides in self._fills.items()}

    def quotes(self, now: datetime, opening: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
        """Usable quotes. With ``opening``, a stock must have traded today, and just after the open an option's book must have moved."""
        with self._lock:
            out = {}
            for code, quote in self._quotes.items():
                if not quote.get("received") or now - quote["received"] > MAX_QUOTE_AGE:
                    continue
                if opening is not None:
                    if quote["option"]:
                        moved = quote.get("book_moved")
                        if now < opening + OPEN_BOOK_GRACE and not (moved and moved >= opening):
                            continue
                    elif not quote.get("data_at") or quote["data_at"] < opening:
                        continue
                out[code] = dict(quote)
            return out

    def alive(self, now: datetime) -> bool:
        with self._lock:
            canary = self._quotes.get(CANARY) or {}
            return bool(canary.get("received") and canary.get("data_at")
                        and now - canary["received"] <= MAX_QUOTE_AGE
                        and now - canary["data_at"] <= MAX_MARKET_SILENCE)

    def pushing(self, now: datetime) -> bool:
        with self._lock:
            pushed = (self._quotes.get(CANARY) or {}).get("pushed")
        return bool(pushed) and now - pushed <= MAX_MARKET_SILENCE

    def status(self, now: datetime) -> Dict[str, Any]:
        with self._lock:
            canary = self._quotes.get(CANARY) or {}
            synced = (self._raw or {}).get("synced_at")
            lags = {f"{name}_s": spread(values) for name, values in self.lags.items()}
            subscribed = {subtype.lower(): len(codes) for subtype, codes in self._subscribed.items()}
            refused = len(self._refused)
            pushes = dict(self.pushes)
        return {"pushes": pushes, **lags, "subscribed": subscribed, "refused": refused, "handler_error": self.error,
                "last_quote_s": round((now - canary["received"]).total_seconds(), 1) if canary.get("received") else None,
                "positions_read_s": round((now - datetime.fromisoformat(synced)).total_seconds(), 1) if synced else None}

