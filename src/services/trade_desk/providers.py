"""Read-only quote providers for the Trade Desk.

The replay provider is deliberately synthetic. It is useful for exercising
the paper/advice flow, but it must never be presented as historical or live
market data. The live provider only reads quote APIs exposed by the
moomoo/futu-compatible OpenD SDK. It never imports trading contexts and never
calls unlock, order, or account endpoints.

The implementation keeps the dependency on the optional SDK lazy. This is
important for API startup and for deterministic tests: a machine without
moomoo (or the pinned futu-api compatibility package) can still use replay
mode and can report an honest live-provider health state.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .models import OptionQuote, QuoteSnapshot
from .quality import CLOCK_SKEW_SECONDS, MAX_QUOTE_AGE_SECONDS, snapshot_fresh

try:  # pragma: no cover - pandas is an optional implementation detail
    import pandas as _pd
except Exception:  # pragma: no cover
    _pd = None


UTC = timezone.utc
US_EASTERN = ZoneInfo("America/New_York")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_MAX_REPLAY_EXPIRIES = 4
_DEFAULT_MAX_CONTRACTS = 48
_DEFAULT_CHAIN_CACHE_SECONDS = 300.0
# OpenD rejects an unsubscribe issued less than one minute after subscribing.
_MIN_SUBSCRIPTION_SECONDS = 60.0
# Subscriptions kept for the underlying: quote, book, and bar context.
_UNDERLYING_SUBTYPES = ("QUOTE", "ORDER_BOOK", "K_DAY", "K_15M")
# exchange-calendars defaults to roughly one year ahead, which excludes LEAPS.
_CALENDAR_HORIZON_DAYS = 5 * 366
_XNYS_CALENDAR: Any = None
_XNYS_CALENDAR_LOCK = threading.Lock()


class ProviderError(RuntimeError):
    """A provider failure with a stable, machine-readable error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "provider_error")
        self.message = str(message)
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - convenience when debugging
        return f"ProviderError(code={self.code!r}, message={self.message!r})"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _aware_datetime(value: Any, *, default_tz: timezone | ZoneInfo = UTC) -> Optional[datetime]:
    """Convert a datetime-like value without inventing a timestamp."""

    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif _pd is not None and isinstance(value, getattr(_pd, "Timestamp", ())):
        result = value.to_pydatetime()
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        result = None
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S.%f",
                "%Y/%m/%d %H:%M:%S",
            ):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if result is None:
            return None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # SDKs occasionally expose epoch seconds or milliseconds. Refuse
        # implausible values instead of turning an arbitrary number into time.
        number = float(value)
        if not math.isfinite(number):
            return None
        if number > 10_000_000_000:
            number /= 1000.0
        if number < 500_000_000 or number > 5_000_000_000:
            return None
        try:
            result = datetime.fromtimestamp(number, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=default_tz)
    return result


def _coerce_now(value: Any, *, default_tz: timezone | ZoneInfo = UTC) -> datetime:
    # A naive clock is ambiguous (server-local, New York, or UTC); never guess.
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock value must be a timezone-aware datetime")
    result = _aware_datetime(value, default_tz=default_tz)
    if result is None:
        raise ValueError("clock value must be a timezone-aware datetime")
    return result


def _safe_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if value in {"", "-", "--", "N/A", "NA", "NULL", "None"}:
            return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _safe_int(value: Any) -> Optional[int]:
    result = _safe_float(value)
    if result is None:
        return None
    try:
        return int(result)
    except (TypeError, ValueError, OverflowError):
        return None


def _nonneg_int(value: Any) -> Optional[int]:
    result = _safe_int(value)
    return result if result is not None and result >= 0 else None


def _flag(value: Any) -> Optional[bool]:
    """Normalize SDK booleans such as True, 1, '1', 'YES', or 'N/A'."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and not math.isfinite(value) else bool(value)
    text = _norm_text(value)
    if text in {"TRUE", "YES", "Y", "1", "ON"}:
        return True
    if text in {"FALSE", "NO", "N", "NONE", "0", "OFF"}:
        return False
    return None


def _right_flag(value: Any) -> Optional[bool]:
    """Normalize a quote-right value; any non-denial level (e.g. LV1) grants."""

    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _norm_text(value)
    if text in {"", "N_A", "NA", "UNKNOWN"}:
        return None
    return text not in {"FALSE", "NO", "N", "NONE", "0", "OFF"}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return isinstance(value, float) and not math.isfinite(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name is not None:
        value = name
    return str(value).strip()


def _norm_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", _text(value).upper()).strip("_")


def _row_get(row: Any, *names: str) -> Any:
    """Read a field from a dict, pandas row, or small SDK object.

    ``names`` are in priority order; missing (None/blank/NaN) values fall
    through to the next name.
    """

    if row is None:
        return None
    if isinstance(row, Mapping):
        lowered: dict[str, Any] = {}
        for key, value in row.items():
            lowered.setdefault(str(key).lower(), value)
        for name in names:
            value = row.get(name) if name in row else lowered.get(name.lower())
            if not _is_missing(value):
                return value
        return None
    getter = getattr(row, "get", None)
    if callable(getter):
        for name in names:
            try:
                value = getter(name, None)
            except Exception:
                value = None
            if not _is_missing(value):
                return value
    for name in names:
        try:
            value = getattr(row, name)
        except Exception:
            value = None
        if not _is_missing(value):
            return value
    return None


def _row_items(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if _pd is not None and isinstance(row, getattr(_pd, "Series", ())):
        return row.to_dict()
    try:
        return vars(row)
    except TypeError:
        return {}


def _rows(data: Any) -> list[Any]:
    """Normalize the table/dict/list shapes returned by quote SDKs."""

    if data is None:
        return []
    if _pd is not None and isinstance(data, getattr(_pd, "DataFrame", ())):
        return data.to_dict(orient="records")
    if isinstance(data, Mapping):
        # A normal row, including the Futu order-book dictionary.
        if any(
            key in {str(k).lower() for k in data}
            for key in ("code", "last_price", "bid_price", "ask_price", "bid", "ask", "strike_price")
        ):
            return [data]
        for key in ("rows", "data", "items", "result", "list"):
            nested = data.get(key)
            if nested is not None:
                nested_rows = _rows(nested)
                if nested_rows:
                    return nested_rows
        return [data]
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict(orient="records")
        except TypeError:
            converted = to_dict()
        return _rows(converted)
    iterrows = getattr(data, "iterrows", None)
    if callable(iterrows):
        try:
            return [row for _, row in iterrows()]
        except Exception:
            return []
    if isinstance(data, (list, tuple)):
        return list(data)
    return [data]


def _unpack_response(response: Any) -> tuple[Any, Any]:
    if isinstance(response, tuple) and len(response) >= 2:
        return response[0], response[1]
    if isinstance(response, list) and len(response) == 2 and (
        isinstance(response[0], (int, bool, str)) or hasattr(response[0], "name")
    ):
        return response[0], response[1]
    return None, response


def _ret_ok(ret: Any, sdk: Any = None) -> bool:
    if ret is None:
        return True
    expected = getattr(sdk, "RET_OK", 0) if sdk is not None else 0
    if ret == expected or ret is True or ret == 0 or ret == "0":
        return True
    text = _norm_text(ret)
    return text in {"OK", "RET_OK", "SUCCESS", "SUCCEED"}


def _coerce_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if _pd is not None and isinstance(value, getattr(_pd, "Timestamp", ())):
        return value.date()
    if isinstance(value, (int, float)):
        stamp = _aware_datetime(value, default_tz=US_EASTERN)
        return stamp.date() if stamp else None
    text = str(value).strip()
    if not text:
        return None
    # Option APIs often return a date plus an optional time zone suffix.
    match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalize_ticker(ticker: Any) -> str:
    value = _text(ticker).upper()
    if value.startswith("US."):
        value = value[3:]
    elif value.endswith(".US"):
        value = value[:-3]
    if not _TICKER_RE.fullmatch(value):
        raise ProviderError("invalid_ticker", "Enter an exact US stock or ETF ticker")
    return value


def _us_symbol(ticker: str) -> str:
    return f"US.{ticker}"


def _xnys_calendar() -> Any:
    """Return a cached XNYS calendar whose horizon covers listed LEAPS."""

    global _XNYS_CALENDAR
    with _XNYS_CALENDAR_LOCK:
        today = _now_utc().astimezone(US_EASTERN).date()
        calendar = _XNYS_CALENDAR
        if calendar is None or calendar.last_session.date() < today + timedelta(days=_CALENDAR_HORIZON_DAYS // 2):
            import exchange_calendars as xcals

            end = today + timedelta(days=_CALENDAR_HORIZON_DAYS)
            calendar = xcals.get_calendar("XNYS", end=end.isoformat())
            _XNYS_CALENDAR = calendar
        return calendar


def _resolve_session_close(expiry_date: date) -> Optional[datetime]:
    """Resolve an exchange-calendars session close for a US expiry date.

    Returning None is intentional. A date-only chain row is not enough to
    calculate time-to-expiry when the repository calendar is unavailable or
    the date is a holiday.
    """

    try:
        from src.core import trading_calendar

        if not bool(getattr(trading_calendar, "_XCALS_AVAILABLE", False)):
            return None
        calendar = _xnys_calendar()
        if expiry_date > calendar.last_session.date():
            return None
        if not bool(calendar.is_session(expiry_date)):
            return None
        session = calendar.date_to_session(expiry_date, direction="none")
        close = calendar.session_close(session)
        if hasattr(close, "to_pydatetime"):
            close = close.to_pydatetime()
        if not isinstance(close, datetime):
            return None
        if close.tzinfo is None:
            close = close.replace(tzinfo=UTC)
        return close.astimezone(US_EASTERN)
    except Exception:
        return None


# A date-only ETF chain row does not identify the contract's actual final
# trading cutoff.  Several heavily traded ETF option classes have a 16:15 ET
# late close, while others follow the regular 16:00 ET close.  Fail closed
# unless the chain provides an explicit cutoff for ETF-like contracts.
# Published NYSE Arca late-close classes (16:15 ET), reviewed 2026-09-22
# from https://www.nyse.com/publicdocs/nyse/markets/arca-options/Options_Late_Close_ARCO.csv
# (linked by https://www.nyse.com/trade/trading-information).  The schedule is
# intentionally a finite allow-list; an ETF outside it remains unverified
# unless its chain supplies an explicit cutoff.  On an exchange early-close
# session the normal 16:15 class timing is not assumed; Cboe's option quote
# intervals document the 13:00/13:15 early-close classes.
_KNOWN_ETF_ROOTS = frozenset(
    {
        "DBA", "DBB", "DBC", "DBO", "DIA", "DRAM", "EEM", "EFA", "EWY", "EWZ",
        "FXI", "GLD", "HYG", "IBIT", "IEF", "IVV", "IWM", "IWN", "IWO", "IYR",
        "KBE", "KRE", "KWEB", "LQD", "MDY", "MOO", "OEF", "PTEST", "QQQ", "RSP",
        "SLV", "SMH", "SOXL", "SOXX", "SPY", "SVIX", "SVXY", "TIP", "TLT", "UNG",
        "UUP", "UVIX", "UVXY", "VIXM", "VIXY", "VOO", "VXX", "VXZ", "XHB", "XLB",
        "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "XME", "XOP", "XRT", "ZVZZT",
    }
)
_EXPIRY_DATETIME_FIELDS = (
    "expiry_datetime",
    "expiration_datetime",
    "expire_datetime",
    "last_trade_datetime",
    "last_trading_datetime",
    "expiry_timestamp",
    "expiration_timestamp",
    "last_trade_timestamp",
    "last_trading_timestamp",
    "strike_time",
)
_EXPIRY_TIME_FIELDS = (
    "expiry_time",
    "expiration_time",
    "expire_time",
    "last_trade_time",
    "last_trading_time",
    "trading_cutoff_time",
    "option_expiry_time",
)


def _explicit_contract_cutoff(row: Any, expiry_date: date) -> Optional[datetime]:
    """Parse a contract cutoff only when the row contains an actual time."""

    for field in _EXPIRY_DATETIME_FIELDS:
        value = _row_get(row, field)
        if value is None:
            continue
        if isinstance(value, str) and not re.search(r"\d{1,2}:\d{2}", value):
            # A date-only strike/expiry field is deliberately not a cutoff.
            continue
        stamp = _aware_datetime(value, default_tz=US_EASTERN)
        if stamp is None:
            continue
        local = stamp.astimezone(US_EASTERN)
        if local.date() == expiry_date and local.time() != datetime.min.time():
            return local
    for field in _EXPIRY_TIME_FIELDS:
        value = _row_get(row, field)
        if value is None:
            continue
        text = _text(value)
        match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            continue
        return datetime.combine(
            expiry_date,
            datetime.min.time().replace(hour=hour, minute=minute, second=second),
            tzinfo=US_EASTERN,
        )
    return None


def _is_known_late_close_etf(contract: _Contract, symbol: str) -> bool:
    roots = (
        _text(_row_get(contract.row, "underlying_code", "underlying", "stock_code", "stock_owner", "owner_code")),
        symbol,
        contract.contract_id,
    )
    for value in roots:
        normalized = _text(value).upper().replace("US.", "")
        if normalized in _KNOWN_ETF_ROOTS or any(
            normalized.startswith(f"{root}.") or f".{root}" in normalized
            for root in _KNOWN_ETF_ROOTS
        ):
            return True
    return False


def _contract_is_etf_like(contract: _Contract, symbol: str) -> bool:
    row = contract.row
    for field in (
        "stock_type",
        "security_type",
        "asset_type",
        "underlying_type",
        "option_underlying_type",
        "fund_type",
    ):
        text = _norm_text(_row_get(row, field))
        if any(marker in text for marker in ("ETF", "FUND", "TRUST", "ETN")):
            return True
    roots = (
        _text(_row_get(row, "underlying_code", "underlying", "stock_code", "stock_owner", "owner_code")),
        symbol,
        contract.contract_id,
    )
    for value in roots:
        normalized = _text(value).upper().replace("US.", "")
        if normalized in _KNOWN_ETF_ROOTS or any(
            normalized.startswith(f"{root}.") or f".{root}" in normalized
            for root in _KNOWN_ETF_ROOTS
        ):
            return True
    return False


def _underlying_is_fund(row: Any) -> Optional[bool]:
    """Classify the underlying from its market snapshot.

    Snapshot rows carry ``trust_valid`` (fund/ETF extension data) and
    ``equity_valid`` (common-stock extension data).  Option chain rows only
    report ``stock_type=DRVT`` and cannot identify an ETF underlying.
    Returns None when neither marker is present.
    """

    stock_type = _norm_text(_row_get(row, "stock_type", "security_type", "sec_type"))
    if any(marker in stock_type for marker in ("ETF", "FUND", "TRUST", "ETN")):
        return True
    if _flag(_row_get(row, "trust_valid")) is True:
        return True
    if stock_type in {"STOCK", "EQTY", "EQUITY"} or _flag(_row_get(row, "equity_valid")) is True:
        return False
    return None


def _resolve_contract_expiry(
    contract: _Contract,
    symbol: str,
    underlying_is_fund: Optional[bool] = None,
) -> Optional[datetime]:
    """Resolve a valid contract cutoff, refusing ambiguous ETF date-only rows.

    ``underlying_is_fund`` is None when the underlying type is unknown; a
    date-only row is then as ambiguous as an unlisted ETF and fails closed.
    """

    session_close = _resolve_session_close(contract.expiry_date)
    if session_close is None:
        return None
    explicit = _explicit_contract_cutoff(contract.row, contract.expiry_date)
    if explicit is not None:
        # The repository calendar remains authoritative for holidays and early
        # closes.  A late cutoff is valid only on a normal session; accepting a
        # 16:15 row on a 13:00 early-close date would fabricate time-to-expiry.
        local_session = session_close.astimezone(US_EASTERN)
        if explicit > local_session and local_session.time() < datetime.min.time().replace(hour=15):
            return None
        return explicit
    local_session = session_close.astimezone(US_EASTERN)
    if _is_known_late_close_etf(contract, symbol):
        # NYSE Arca's published late-close class schedule establishes 16:15 ET
        # on a normal XNYS session.  It does not establish the cutoff on a
        # holiday or early-close date, so those rows remain unavailable.
        if local_session.hour == 16 and local_session.minute == 0:
            return session_close + timedelta(minutes=15)
        return None
    if underlying_is_fund is not False or _contract_is_etf_like(contract, symbol):
        return None
    return session_close


def _replay_session_close(expiry_date: date) -> Optional[datetime]:
    """Return a deterministic close for synthetic replay data."""

    calendar_close = _resolve_session_close(expiry_date)
    if calendar_close is not None:
        return calendar_close
    # A small holiday calendar keeps replay usable in a dependency-light
    # environment while avoiding weekend/fixed-holiday fake expiries. The
    # exchange-calendar path remains authoritative when installed.
    if expiry_date.weekday() >= 5:
        return None
    fixed_holidays = {
        (1, 1),   # New Year's Day
        (6, 19),  # Juneteenth
        (7, 4),   # Independence Day
        (12, 25), # Christmas
    }
    if (expiry_date.month, expiry_date.day) in fixed_holidays:
        return None
    # A deterministic weekday close is suitable for synthetic data. It is
    # explicitly marked synthetic in every replay snapshot.
    return datetime.combine(expiry_date, datetime.min.time(), tzinfo=US_EASTERN).replace(
        hour=16, minute=0
    )


def _next_replay_sessions(start: date, count: int) -> list[datetime]:
    result: list[datetime] = []
    cursor = start
    for _ in range(90):
        close = _replay_session_close(cursor)
        if close is not None:
            result.append(close)
            if len(result) >= count:
                return result
        cursor += timedelta(days=1)
    return result


def _session_label(stamp: datetime) -> str:
    try:
        from src.core.trading_calendar import infer_market_phase

        phase = infer_market_phase("us", current_time=stamp)
        value = getattr(phase, "value", str(phase)).lower()
        if value in {"intraday", "closing_auction"}:
            return "regular"
        if value in {"premarket", "postmarket"}:
            return value
        if value == "non_trading":
            return "closed"
    except Exception:
        pass
    local = stamp.astimezone(US_EASTERN)
    if local.weekday() >= 5:
        return "closed"
    if local.time() < datetime.min.time().replace(hour=9, minute=30):
        return "premarket"
    if local.time() >= datetime.min.time().replace(hour=16):
        return "postmarket"
    return "regular"


@dataclass(frozen=True)
class _QuoteData:
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    iv: Optional[float] = None
    timestamp: Optional[datetime] = None
    crossed: bool = False


@dataclass(frozen=True)
class _Contract:
    contract_id: str
    expiry_date: date
    expiry: Optional[datetime]
    right: str
    strike: float
    multiplier: int
    exercise_style: str
    standard: bool
    row: Any


def _quote_from_row(row: Any, *, default_tz: timezone | ZoneInfo = US_EASTERN) -> _QuoteData:
    bid = _safe_float(_row_get(row, "bid_price", "bid", "Bid", "bidPrice"))
    ask = _safe_float(_row_get(row, "ask_price", "ask", "Ask", "askPrice"))
    last = _safe_float(
        _row_get(row, "last_price", "price", "last", "lastPrice", "cur_price", "current_price")
    )
    if bid is not None and bid < 0:
        bid = None
    if ask is not None and ask < 0:
        ask = None
    crossed = bid is not None and ask is not None and bid > ask
    timestamp_value = _row_get(
        row,
        "update_time",
        "quote_time",
        "timestamp",
        "time",
        "data_time",
        "last_update_time",
        "svr_recv_time",
        "svr_recv_time_bid",
        "svr_recv_time_ask",
    )
    timestamp = _aware_datetime(timestamp_value, default_tz=default_tz)
    # get_stock_quote exposes data_date and data_time as separate strings.
    if timestamp is None:
        data_date = _text(_row_get(row, "data_date", "quote_date"))
        data_time = _text(_row_get(row, "data_time", "quote_clock"))
        if data_date and data_time:
            timestamp = _aware_datetime(f"{data_date} {data_time}", default_tz=default_tz)
    # Snapshot OptionSnapshotExData.impliedVolatility is percentage-valued:
    # https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html
    # Never infer units from magnitude: 1.5 means 1.5%, while 450 means 450%.
    percent_iv = _safe_float(_row_get(row, "option_implied_volatility", "implied_volatility", "imp_volatility"))
    iv = percent_iv / 100.0 if percent_iv is not None else _safe_float(_row_get(row, "iv"))
    if iv is not None and iv <= 0:
        # The protobuf default 0.0 means "no IV", not zero volatility.
        iv = None
    return _QuoteData(
        bid=bid,
        ask=ask,
        last=last,
        bid_size=_nonneg_int(_row_get(row, "bid_volume", "bid_vol", "bid_size", "BidVol", "bidVol")),
        ask_size=_nonneg_int(_row_get(row, "ask_volume", "ask_vol", "ask_size", "AskVol", "askVol")),
        volume=_nonneg_int(_row_get(row, "volume", "vol")),
        open_interest=_nonneg_int(
            _row_get(row, "option_open_interest", "open_interest", "openInterest", "open_interest_qty")
        ),
        iv=iv,
        timestamp=timestamp,
        crossed=crossed,
    )


def _orderbook_quote(data: Any) -> _QuoteData:
    """Read the best bid/ask and exchange receipt times from an order book."""

    rows = _rows(data)
    row = rows[0] if rows else data
    bids = _row_get(row, "Bid", "bid", "bids")
    asks = _row_get(row, "Ask", "ask", "asks")

    def _best(levels: Any, side: str) -> tuple[Optional[float], Optional[int]]:
        if levels is None:
            return None, None
        if isinstance(levels, Mapping):
            levels = list(levels.values())
        if isinstance(levels, (tuple, list)) and levels and not isinstance(levels[0], (tuple, list, Mapping)):
            levels = [levels]
        if not isinstance(levels, (tuple, list)):
            return None, None
        candidates: list[tuple[float, Optional[int]]] = []
        for level in levels:
            if isinstance(level, Mapping):
                price = _safe_float(_row_get(level, "price", f"{side}_price"))
                volume = _safe_int(_row_get(level, "volume", f"{side}_volume", "qty", "size"))
            elif isinstance(level, (tuple, list)) and level:
                price = _safe_float(level[0])
                volume = _safe_int(level[1]) if len(level) > 1 else None
            else:
                price, volume = _safe_float(level), None
            if price is not None and price >= 0:
                candidates.append((price, volume))
        if not candidates:
            return None, None
        return (
            max(candidates, key=lambda item: item[0])
            if side == "bid"
            else min(candidates, key=lambda item: item[0])
        )

    bid, bid_size = _best(bids, "bid")
    ask, ask_size = _best(asks, "ask")
    flat = _quote_from_row(row)
    bid = bid if bid is not None else flat.bid
    ask = ask if ask is not None else flat.ask
    bid_size = bid_size if bid_size is not None else flat.bid_size
    ask_size = ask_size if ask_size is not None else flat.ask_size
    side_stamps = [
        value
        for value in (
            _aware_datetime(_row_get(row, "svr_recv_time_bid"), default_tz=US_EASTERN),
            _aware_datetime(_row_get(row, "svr_recv_time_ask"), default_tz=US_EASTERN),
        )
        if value is not None
    ]
    # A two-sided book is only as recent as its older side.
    stamp = min(side_stamps) if side_stamps else _aware_datetime(
        _row_get(row, "update_time", "data_time", "timestamp"),
        default_tz=US_EASTERN,
    )
    crossed = bid is not None and ask is not None and bid > ask
    return _QuoteData(
        bid=bid,
        ask=ask,
        last=flat.last,
        bid_size=bid_size,
        ask_size=ask_size,
        volume=flat.volume,
        open_interest=flat.open_interest,
        iv=flat.iv,
        timestamp=stamp or flat.timestamp,
        crossed=crossed,
    )


def _needs_order_book(quote: Optional[_QuoteData], now: datetime) -> bool:
    """Whether a snapshot quote cannot establish a fresh, executable bid/ask."""

    if quote is None or quote.bid is None or quote.ask is None or quote.timestamp is None or quote.crossed:
        return True
    return not -CLOCK_SKEW_SECONDS <= (now - quote.timestamp).total_seconds() <= MAX_QUOTE_AGE_SECONDS


def _merge_quote(primary: Optional[_QuoteData], fallback: Optional[_QuoteData]) -> Optional[_QuoteData]:
    """Fill missing quote fields from a timestamped order-book observation."""

    if primary is None:
        return fallback
    if fallback is None:
        return primary
    # Bid, ask, sizes and their timestamp are one observation and are never
    # mixed across sources.  A complete order book replaces a crossed or
    # incomplete snapshot quote; otherwise the snapshot quote stands as-is
    # (and is rejected downstream if incomplete).  IV/OI/volume metadata is
    # retained from the market snapshot.

    def _complete(quote: _QuoteData) -> bool:
        return quote.bid is not None and quote.ask is not None and quote.timestamp is not None

    # OpenD snapshot update_time is the last *trade* time; a complete order
    # book with a newer server receive time is the fresher quote observation.
    source = fallback if _complete(fallback) and (
        primary.crossed or not _complete(primary) or fallback.timestamp > primary.timestamp
    ) else primary
    return _QuoteData(
        bid=source.bid,
        ask=source.ask,
        last=primary.last if primary.last is not None else fallback.last,
        bid_size=source.bid_size,
        ask_size=source.ask_size,
        volume=primary.volume if primary.volume is not None else fallback.volume,
        open_interest=primary.open_interest if primary.open_interest is not None else fallback.open_interest,
        iv=primary.iv if primary.iv is not None else fallback.iv,
        timestamp=source.timestamp,
        crossed=source.bid is not None and source.ask is not None and source.bid > source.ask,
    )


def _option_right(row: Any) -> Optional[str]:
    value = _norm_text(_row_get(row, "option_type", "right", "call_put", "optionType", "type"))
    if value in {"CALL", "C", "CALL_OPTION", "1"} or value.endswith("_CALL"):
        return "call"
    if value in {"PUT", "P", "PUT_OPTION", "2"} or value.endswith("_PUT"):
        return "put"
    return None


def _chain_rows(data: Any) -> list[Any]:
    """Flatten flat DataFrames and nested call/put chain representations."""

    result: list[Any] = []
    for raw in _rows(data):
        nested_found = False
        base_expiry = _row_get(raw, "strike_time", "strikeTime", "expiry", "expiry_date", "expiration_date")
        for side_name, right in (("call", "call"), ("put", "put")):
            nested = _row_get(raw, side_name, side_name.upper())
            if nested is None:
                continue
            nested_found = True
            for child in _rows(nested):
                child_map = dict(_row_items(child))
                if base_expiry is not None:
                    child_map.setdefault("expiry_date", base_expiry)
                child_map.setdefault("option_type", right)
                result.append(child_map)
        if not nested_found:
            result.append(raw)
    return result


def _contract_from_row(row: Any) -> Optional[_Contract]:
    contract_id = _text(_row_get(row, "code", "contract_id", "option_code", "security_code"))
    if not contract_id:
        return None
    expiry_date = _coerce_date(
        _row_get(row, "strike_time", "strikeTime", "expiry_date", "expiration_date", "expiry", "expire_date")
    )
    right = _option_right(row)
    strike = _safe_float(_row_get(row, "strike_price", "strike", "exercise_price"))
    if expiry_date is None or right is None or strike is None or strike <= 0:
        return None

    option_standard = _row_get(
        row,
        "option_standard",
        "option_standard_type",
        "standard",
        "is_standard",
        "contract_standard",
    )
    if option_standard is None:
        # The SDK option_standard_type field is required to distinguish
        # adjusted contracts from standard share-delivered contracts.
        return None
    normalized = _norm_text(option_standard)
    standard = normalized in {"TRUE", "YES", "Y", "STANDARD", "STANDARD_TYPE", "1"}
    if normalized in {"FALSE", "NO", "N", "ADJUSTED", "NON_STANDARD", "NONSTANDARD", "0"}:
        standard = False
    index_option_type = _norm_text(_row_get(row, "index_option_type"))
    if index_option_type and index_option_type not in {"NORMAL", "NONE", "N_A", "0"}:
        return None
    index_marker = _row_get(
        row,
        "index_option_type",
        "is_index",
        "index",
        "security_type",
        "stock_type",
        "option_security_type",
    )
    index_text = _norm_text(index_marker)
    if index_text in {"INDEX", "INDEX_OPTION", "IDX", "SECURITY_TYPE_IDX", "TRUE", "YES", "1"} or "INDEX" in index_text:
        return None
    deliverable = _row_get(
        row,
        "share_delivered",
        "stock_delivered",
        "deliverable",
        "deliverable_type",
        "option_settlement_mode",
    )
    deliverable_text = _norm_text(deliverable)
    if deliverable is not None and deliverable_text in {
        "FALSE",
        "NO",
        "N",
        "CASH",
        "CASH_SETTLED",
        "INDEX",
        "0",
    }:
        return None
    if not standard:
        return None

    multiplier = _safe_int(_row_get(row, "contract_size", "multiplier", "lot_size", "contract_multiplier"))
    if multiplier is None or multiplier != 100:
        return None
    style_text = _norm_text(_row_get(row, "option_style", "exercise_style", "style"))
    style = "european" if "EUROPEAN" in style_text else "american"
    return _Contract(
        contract_id=contract_id,
        expiry_date=expiry_date,
        expiry=None,
        right=right,
        strike=strike,
        multiplier=multiplier,
        exercise_style=style,
        standard=True,
        row=row,
    )


def _requested_expiry(value: Any) -> Optional[date]:
    if value is None:
        return None
    result = _coerce_date(value)
    if result is None:
        raise ProviderError("invalid_expiry", f"Invalid option expiry: {value!r}")
    return result


def _provider_timestamp_or_error(stamp: Optional[datetime], *, label: str) -> datetime:
    if stamp is None:
        raise ProviderError(
            "timestamp_unavailable",
            f"{label} did not include a provider bid/ask timestamp; receipt time is not substituted",
        )
    return stamp


class ReplayProvider:
    """Deterministic, explicitly synthetic quote and option-chain provider."""

    provider_name = "replay_synthetic"

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime] | datetime] = None,
        now: Optional[datetime] = None,
        max_expiries: int = _MAX_REPLAY_EXPIRIES,
    ) -> None:
        if clock is not None and now is not None:
            raise ValueError("pass either clock or now, not both")
        self._clock = clock or now or _now_utc
        self._max_expiries = max(1, int(max_expiries))
        self._closed = False

    def _now(self, now: Optional[datetime]) -> datetime:
        if now is not None:
            return _coerce_now(now)
        value = self._clock() if callable(self._clock) else self._clock
        return _coerce_now(value)

    @staticmethod
    def _seed(ticker: str) -> int:
        return int.from_bytes(hashlib.sha256(ticker.encode("utf-8")).digest()[:8], "big")

    @staticmethod
    def _strike_increment(spot: float) -> float:
        if spot < 25:
            return 1.0
        if spot < 100:
            return 2.5
        if spot < 250:
            return 5.0
        return 10.0

    def _expiries(self, current: datetime, requested: Optional[date]) -> list[datetime]:
        local_date = current.astimezone(US_EASTERN).date()
        current_eastern = current.astimezone(US_EASTERN)
        if requested is not None:
            close = _replay_session_close(requested)
            if close is None or close <= current_eastern:
                raise ProviderError("expiry_unavailable", f"Synthetic replay has no future US session for {requested}")
            return [close]
        # Include one extra candidate so a clock after today's close still
        # receives the requested number of future sessions.
        sessions = _next_replay_sessions(local_date, self._max_expiries + 1)
        sessions = [close for close in sessions if close > current_eastern]
        if not sessions:
            raise ProviderError("expiry_unavailable", "Unable to construct synthetic US option sessions")
        return sessions[: self._max_expiries]

    def snapshot(
        self,
        ticker: str,
        expiry: Optional[date | datetime | str] = None,
        now: Optional[datetime] = None,
        required_contracts: Sequence[str] = (),
    ) -> QuoteSnapshot:
        if self._closed:
            raise ProviderError("provider_closed", "Replay provider is closed")
        symbol = _normalize_ticker(ticker)
        current = self._now(now)
        requested = _requested_expiry(expiry)
        seed = self._seed(symbol)
        # Stable but synthetic enough to avoid resembling a historical lookup.
        spot = round(20.0 + (seed % 48000) / 100.0, 2)
        increment = self._strike_increment(spot)
        spread = round(max(0.02, min(0.20, spot * 0.0008)), 2)
        bid = round(spot - spread, 2)
        ask = round(spot + spread, 2)
        expiries = self._expiries(current, requested)
        options: list[OptionQuote] = []
        for expiry_close in expiries:
            time_years = max((expiry_close - current.astimezone(US_EASTERN)).total_seconds(), 0) / (
                365.0 * 24.0 * 3600.0
            )
            for step in range(-4, 5):
                strike = round(max(increment, round(spot / increment) * increment + step * increment), 2)
                moneyness = abs(strike / spot - 1.0)
                iv = min(1.20, max(0.12, 0.18 + 0.55 * moneyness + 0.015 * (seed % 7)))
                for right in ("call", "put"):
                    intrinsic = max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
                    time_value = max(0.03, spot * iv * math.sqrt(max(time_years, 1 / 365000.0)) * 0.40)
                    mid = max(0.03, intrinsic + time_value)
                    option_spread = max(0.02, mid * 0.08)
                    option_bid = round(max(0.0, mid - option_spread / 2), 2)
                    option_ask = round(mid + option_spread / 2, 2)
                    contract_id = (
                        f"REPLAY.US.{symbol}.{expiry_close:%Y%m%d}."
                        f"{right[0].upper()}{strike:g}"
                    )
                    options.append(
                        OptionQuote(
                            contract_id=contract_id,
                            underlying=symbol,
                            right=right,
                            strike=strike,
                            expiry=expiry_close,
                            expiry_verified=True,
                            multiplier=100,
                            bid=option_bid,
                            ask=option_ask,
                            quoted_at=current,
                            iv=round(iv, 6),
                            bid_size=10 + ((seed + step + len(right)) % 40),
                            ask_size=10 + ((seed + step + len(right) + 7) % 40),
                            volume=100 + ((seed + step * 11) % 900),
                            open_interest=500 + ((seed + step * 17) % 5000),
                            standard=True,
                            exercise_style="american",
                        )
                    )
        replay_id = hashlib.sha256(
            f"replay:{symbol}:{current.isoformat()}:{requested.isoformat() if requested else 'all'}".encode()
        ).hexdigest()[:32]
        return QuoteSnapshot(
            id=replay_id,
            underlying=symbol,
            spot=spot,
            bid=bid,
            ask=ask,
            quoted_at=current,
            received_at=current,
            provider=self.provider_name,
            mode="replay",
            session="replay",
            source_verified=False,
            stale=False,
            options=options,
            warnings=[
                "synthetic_replay_data_not_historical",
                "synthetic_replay_data_not_live",
            ],
            evidence=[
                {
                    "source": self.provider_name,
                    "kind": "synthetic",
                    "historical": False,
                    "live": False,
                    "clock": current.isoformat(),
                    "note": "Generated deterministic values; do not treat as market observations.",
                }
            ],
        )

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "mode": "replay",
            "status": "ready",
            "ok": True,
            "synthetic": True,
            "historical": False,
            "live": False,
            "message": "Synthetic replay data only; it is not historical or live market data.",
        }

    def close(self) -> None:
        self._closed = True


class MoomooProvider:
    """Read-only moomoo/futu-compatible OpenD quote adapter."""

    provider_name = "moomoo"
    mode = "live"

    def __init__(
        self,
        *,
        sdk: Optional[ModuleType | Any] = None,
        context: Any = None,
        quote_context: Any = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        clock: Optional[Callable[[], datetime] | datetime] = None,
        now: Optional[datetime] = None,
        max_contracts: int = _DEFAULT_MAX_CONTRACTS,
        max_expiries: int = 4,
        chain_cache_seconds: float = _DEFAULT_CHAIN_CACHE_SECONDS,
    ) -> None:
        self._sdk = sdk
        self._sdk_name: Optional[str] = None
        self._sdk_package: Optional[str] = None
        self._ctx = context or quote_context
        self._host = (
            _text(host).strip()
            or (os.getenv("TRADE_DESK_OPEND_HOST") or "").strip()
            or (os.getenv("FUTU_OPEND_HOST") or "").strip()
            or None
        )
        self._port = self._resolve_port(port)
        if clock is not None and now is not None:
            raise ValueError("pass either clock or now, not both")
        self._clock = clock or now or _now_utc
        self._max_contracts = max(1, int(max_contracts))
        self._max_expiries = max(1, int(max_expiries))
        try:
            self._chain_cache_seconds = max(0.0, float(chain_cache_seconds))
        except (TypeError, ValueError):
            self._chain_cache_seconds = _DEFAULT_CHAIN_CACHE_SECONDS
        self._chain_cache: dict[str, tuple[datetime, list[_Contract]]] = {}
        self._subscriptions: set[tuple[str, str]] = set()
        self._subscribed_at: dict[tuple[str, str], datetime] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._reconnecting = False
        self._last_error: Optional[ProviderError] = None
        # SDK-level warnings persist; _call_warnings is reset per snapshot/health call.
        self._warnings: list[str] = []
        self._call_warnings: list[str] = []
        self._load_sdk()

    @staticmethod
    def _resolve_port(port: Optional[int]) -> int:
        candidate: Any = port
        if candidate is None:
            candidate = (os.getenv("TRADE_DESK_OPEND_PORT") or "").strip() or (
                os.getenv("FUTU_OPEND_PORT") or ""
            ).strip() or "11111"
        try:
            resolved = int(candidate)
        except (TypeError, ValueError):
            return 11111
        return resolved if 1 <= resolved <= 65535 else 11111

    @property
    def configured(self) -> bool:
        return bool(self._host) or self._ctx is not None

    def _load_sdk(self) -> Optional[Any]:
        if self._sdk is not None:
            if self._sdk_name is None:
                self._sdk_name = _text(getattr(self._sdk, "__name__", "injected_sdk")) or "injected_sdk"
                self._sdk_package = self._sdk_name
            return self._sdk
        for module_name in ("moomoo", "futu"):
            try:
                module = __import__(module_name)
            except ImportError:
                continue
            except Exception as exc:  # pragma: no cover - SDK import side effects vary
                self._warnings.append(f"{module_name}_sdk_import_failed:{exc}")
                continue
            self._sdk = module
            self._sdk_name = module_name
            self._sdk_package = module_name
            if module_name == "futu":
                self._warnings.append("moomoo_sdk_unavailable_using_futu_compatibility_sdk")
            return module
        return None

    def _now(self) -> datetime:
        value = self._clock() if callable(self._clock) else self._clock
        return _coerce_now(value)

    def _warn(self, message: str) -> None:
        self._call_warnings.append(message)

    def _track(self, records: Iterable[tuple[str, str]], *, subscribed_at: Optional[datetime] = None) -> None:
        stamp = subscribed_at or self._now()
        for record in records:
            self._subscriptions.add(record)
            if subscribed_at is not None:
                self._subscribed_at[record] = stamp
            else:
                self._subscribed_at.setdefault(record, stamp)

    def _forget(self, records: Iterable[tuple[str, str]]) -> None:
        for record in list(records):
            self._subscriptions.discard(record)
            self._subscribed_at.pop(record, None)

    def _ensure_context(self) -> Any:
        with self._lock:
            if self._closed:
                raise ProviderError("provider_closed", "Moomoo provider is closed")
            if self._ctx is not None:
                return self._ctx
            if not self.configured:
                raise ProviderError(
                    "opend_not_configured",
                    "Trade Desk OpenD is not configured; set TRADE_DESK_OPEND_HOST "
                    "or FUTU_OPEND_HOST (and optionally the matching *_PORT)",
                )
            sdk = self._load_sdk()
            if sdk is None:
                raise ProviderError(
                    "sdk_unavailable",
                    "Neither the optional moomoo SDK nor the pinned futu compatibility SDK is installed",
                )
            context_factory = getattr(sdk, "OpenQuoteContext", None)
            if not callable(context_factory):
                raise ProviderError("sdk_unsupported", "Quote SDK does not expose OpenQuoteContext")
            try:
                self._configure_encryption(sdk)
                self._ctx = context_factory(host=self._host, port=self._port)
            except ProviderError:
                raise
            except Exception as exc:
                error = self._translate_exception("connection_unavailable", "Unable to connect to OpenD", exc)
                self._last_error = error
                raise error from exc
            return self._ctx

    @staticmethod
    def _encryption_key_file() -> Optional[str]:
        return (os.getenv("TRADE_DESK_OPEND_RSA_KEY_FILE") or "").strip() or None

    def _configure_encryption(self, sdk: Any) -> None:
        """Encrypt the OpenD protocol when an RSA key is configured.

        On a shared host any local user can reach 127.0.0.1:11111; with
        OpenD's ``rsa_private_key`` set, only clients holding the same PKCS#1
        key file can use the logged-in session. The SDK setting is process-wide.
        """

        key_file = self._encryption_key_file()
        if key_file is None:
            return
        if not os.path.isfile(key_file) or not os.access(key_file, os.R_OK):
            raise ProviderError("opend_encryption_key_unavailable",
                                "TRADE_DESK_OPEND_RSA_KEY_FILE does not point to a readable key file")
        sys_config = getattr(sdk, "SysConfig", None)
        if sys_config is None or not callable(getattr(sys_config, "enable_proto_encrypt", None)):
            raise ProviderError("sdk_unsupported", "Quote SDK does not support OpenD protocol encryption")
        sys_config.set_init_rsa_file(key_file)
        sys_config.enable_proto_encrypt(True)

    def _translate_exception(self, fallback: str, prefix: str, exc: Exception) -> ProviderError:
        message = _text(exc) or prefix
        code = self._classify_error(message, fallback)
        return ProviderError(code, f"{prefix}: {message}")

    @staticmethod
    def _classify_error(message: Any, fallback: str = "provider_error") -> str:
        text = _norm_text(message)
        # OpenD answered the connection but not this request in time (it loads a
        # symbol's data on first use); the connection itself is fine.
        if "PACKETERR_TIMEOUT" in text:
            return "request_timeout"
        if any(token in text for token in ("PERMISSION", "NO_RIGHT", "NO_QUOTE_RIGHT", "AUTHORIZATION", "NOT_AUTH")):
            return "quote_permission"
        if any(token in text for token in ("QUOTA", "EXCEED", "LIMIT", "TOO_MANY", "SUBSCRIBE")):
            return "subscription_quota"
        if any(token in text for token in ("CONNECT", "CONNECTION", "DISCONNECT", "SOCKET", "TIMEOUT", "NETWORK", "RESET")):
            return "connection_unavailable"
        return fallback

    def _reconnect(self) -> Any:
        with self._lock:
            old = self._ctx
            records = set(self._subscriptions)
            self._ctx = None
            self._forget(records)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            ctx = self._ensure_context()
            # A new OpenD connection starts without subscriptions.  Restore this
            # provider's records so subscription-dependent retries can succeed.
            grouped: dict[str, list[str]] = {}
            for code, subtype_name in records:
                grouped.setdefault(subtype_name, []).append(code)
            self._reconnecting = True
            try:
                for subtype_name, codes in grouped.items():
                    try:
                        self._subscribe(sorted(codes), subtype_name)
                    except ProviderError as exc:
                        self._warn(f"resubscribe_failed:{subtype_name}:{exc.code}")
            finally:
                self._reconnecting = False
            return ctx

    def _call(
        self,
        method_name: str,
        *args: Any,
        retry_connection: bool = True,
        timeout_retries: int = 2,
        **kwargs: Any,
    ) -> Any:
        ctx = self._ensure_context()
        method = getattr(ctx, method_name, None)
        if not callable(method):
            raise ProviderError("sdk_unsupported", f"Quote SDK context does not expose {method_name}")
        try:
            response = method(*args, **kwargs)
        except TypeError:
            # Let _call_variants try the next documented SDK signature.  A
            # final signature mismatch is converted to ProviderError there.
            raise
        except Exception as exc:
            error = self._translate_exception("provider_error", f"{method_name} failed", exc)
            self._last_error = error
            if retry_connection and not self._reconnecting and error.code == "connection_unavailable":
                try:
                    self._reconnect()
                    return self._call(method_name, *args, retry_connection=False, **kwargs)
                except ProviderError:
                    pass
            raise error from exc
        ret, data = _unpack_response(response)
        if not _ret_ok(ret, self._sdk):
            message = _text(data) or _text(ret) or f"{method_name} returned an error"
            error = ProviderError(self._classify_error(message), f"{method_name}: {message}")
            self._last_error = error
            if error.code == "request_timeout" and timeout_retries > 0:
                return self._call(method_name, *args, retry_connection=retry_connection,
                                  timeout_retries=timeout_retries - 1, **kwargs)
            if retry_connection and not self._reconnecting and error.code == "connection_unavailable":
                try:
                    self._reconnect()
                    return self._call(method_name, *args, retry_connection=False, **kwargs)
                except ProviderError:
                    pass
            raise error
        return data

    def _call_variants(self, method_name: str, variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
        last_type_error: Optional[Exception] = None
        for args, kwargs in variants:
            try:
                return self._call(method_name, *args, **kwargs)
            except TypeError as exc:
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise self._translate_exception("sdk_unsupported", f"{method_name} signature unsupported", last_type_error)
        raise ProviderError("provider_error", f"{method_name} failed")

    def watchlist_groups(self, max_groups: int = 9) -> list[dict[str, Any]]:
        """The user's custom moomoo watchlist groups in app order, US codes without the ``US.`` prefix.

        OpenD allows 10 ``get_user_security`` requests per 30 seconds, so the
        group count is capped and callers should cache the result.
        """
        self._ensure_context()
        group_type = self._enum("UserSecurityGroupType", "CUSTOM", "CUSTOM")
        data = self._call_variants("get_user_security_group", [((), {"group_type": group_type}), ((group_type,), {})])
        rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data or [])
        groups = []
        for row in rows[:max_groups]:
            name = _text(row.get("group_name"))
            if not name:
                continue
            members = self._call("get_user_security", name)
            member_rows = members.to_dict("records") if hasattr(members, "to_dict") else list(members or [])
            codes = [_text(item.get("code"))[3:] for item in member_rows if _text(item.get("code")).startswith("US.")]
            if codes:
                groups.append({"name": name, "codes": list(dict.fromkeys(codes))})
        return groups

    def watchlist_quotes(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Latest US quotes for display, keyed by bare ticker, from one batched snapshot.

        ``extended`` carries the pre-market / after-hours price while that
        session is active; regular-session fields always describe the last
        regular trade versus the previous close.
        """
        from data_provider.us_session import session_window

        tickers = [code.strip().upper() for code in codes if code and code.strip()]
        if not tickers:
            return {}
        session = session_window()[0]
        extended_fields = {"premarket": ("pre_price", "pre_change_rate"),
                           "postmarket": ("after_price", "after_change_rate")}.get(session)
        quotes: dict[str, dict[str, Any]] = {}
        for start in range(0, len(tickers), 400):  # OpenD allows 400 codes per snapshot
            data = self._call("get_market_snapshot", [f"US.{ticker}" for ticker in tickers[start:start + 400]])
            rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data or [])
            for row in rows:
                ticker = _text(row.get("code"))[3:]
                price, prev_close = _safe_float(row.get("last_price")), _safe_float(row.get("prev_close_price"))
                if not ticker or price is None or price <= 0:
                    continue
                quote = {"price": price, "prev_close": prev_close, "name": _text(row.get("name")),
                         "change_pct": (price / prev_close - 1) * 100 if prev_close else None,
                         "updated_at": _text(row.get("update_time")), "session": session, "extended": None}
                if extended_fields:
                    ext_price, ext_change = (_safe_float(row.get(name)) for name in extended_fields)
                    if ext_price and ext_price > 0:
                        quote["extended"] = {"price": ext_price, "change_pct": ext_change}
                quotes[ticker] = quote
        return quotes

    def _enum(self, group_name: str, member: str, default: str = "") -> Any:
        group = getattr(self._sdk, group_name, None) if self._sdk is not None else None
        return getattr(group, member, default) if group is not None else default

    def _query_subscription(self) -> Any:
        ctx = self._ensure_context()
        method = getattr(ctx, "query_subscription", None)
        if not callable(method):
            return None
        try:
            # Subscriptions are per connection: another process holding a code
            # does not make it readable on this context.
            return self._call_variants(
                "query_subscription",
                [
                    ((), {"is_all_conn": False}),
                    ((False,), {}),
                ],
            )
        except ProviderError as exc:
            self._warn(f"subscription_query_failed:{exc.code}")
            return None

    @staticmethod
    def _subscription_details(data: Any) -> tuple[set[tuple[str, str]], Optional[int], Optional[int]]:
        """Return (code, normalized subtype) pairs, remaining quota, and own usage."""

        if data is None:
            return set(), None, None
        pairs: set[tuple[str, str]] = set()
        remain = _safe_int(_row_get(data, "remain", "remaining", "remain_quota", "remaining_quota"))
        used = _safe_int(_row_get(data, "own_used", "used", "total_used", "used_quota"))

        def _codes(value: Any) -> list[str]:
            items = list(value) if isinstance(value, (list, tuple, set)) else _rows(value)
            result = []
            for item in items:
                code = item if isinstance(item, str) else _row_get(item, "code", "security", "stock_code")
                if _text(code):
                    result.append(_text(code))
            return result

        sub_list = _row_get(data, "sub_list", "subscriptions", "subscription_list")
        if isinstance(sub_list, Mapping):
            # SDK shape: {"QUOTE": [codes], "ORDER_BOOK": [codes]}
            for subtype, value in sub_list.items():
                pairs.update((code, _norm_text(subtype)) for code in _codes(value))
        elif isinstance(sub_list, (list, tuple)):
            for item in sub_list:
                subtype = _norm_text(_row_get(item, "subtype", "sub_type"))
                if subtype:
                    pairs.update((code, subtype) for code in _codes(_row_get(item, "code_list", "codes") or []))
        return pairs, remain, used

    def _subscription_type(self, name: str) -> Any:
        return self._enum("SubType", name, name)

    def _subscribe(self, codes: Sequence[str], subtype_name: str) -> set[tuple[str, str]]:
        if not codes:
            return set()
        code_list = list(dict.fromkeys(codes))
        existing_data = self._query_subscription()
        existing, remain, _ = self._subscription_details(existing_data)
        subtype_key = _norm_text(subtype_name)
        held = [code for code in code_list if (code, subtype_key) in existing]
        missing = [code for code in code_list if (code, subtype_key) not in existing]
        # Codes already held on this provider's own connection belong to this
        # provider (for example after a rejected unsubscribe); keep them tracked
        # so they are released later instead of being orphaned.
        self._track((code, subtype_name) for code in held)
        if remain is not None and len(missing) > remain:
            raise ProviderError(
                "subscription_quota",
                f"OpenD subscription quota has {remain} slots; {len(missing)} are required",
            )
        method = getattr(self._ensure_context(), "subscribe", None)
        if not callable(method):
            raise ProviderError("sdk_unsupported", "Quote SDK context does not expose subscribe")
        if not missing:
            return set()
        subtype = self._subscription_type(subtype_name)
        variants = [
            ((missing, [subtype]), {"is_first_push": True, "subscribe_push": False}),
            ((missing, [subtype]), {"is_first_push": True}),
            ((missing, [subtype]), {}),
        ]
        try:
            self._call_variants("subscribe", variants)
        except ProviderError as exc:
            if exc.code == "provider_error":
                exc = ProviderError(self._classify_error(exc.message, "subscription_quota"), exc.message)
            raise exc
        records = {(code, subtype_name) for code in missing}
        self._track(records, subscribed_at=self._now())
        return records

    def release_subscriptions(self, subscriptions: Optional[Iterable[tuple[str, str]]] = None) -> None:
        """Release only subscriptions created by this provider instance.

        A record is forgotten only after OpenD accepts the unsubscribe; a
        rejected release (for example inside OpenD's one-minute minimum) stays
        tracked and is retried later.
        """

        with self._lock:
            records = set(subscriptions) if subscriptions is not None else set(self._subscriptions)
            if not records:
                return
            ctx = self._ctx
            if ctx is None:
                self._forget(records)
                return
            unsubscribe = getattr(ctx, "unsubscribe", None)
            if not callable(unsubscribe):
                self._warn("subscription_release_unsupported")
                return
            grouped: dict[str, list[str]] = {}
            for code, subtype in records:
                grouped.setdefault(subtype, []).append(code)
            for subtype_name, codes in grouped.items():
                subtype = self._subscription_type(subtype_name)
                try:
                    self._call_variants(
                        "unsubscribe",
                        [
                            ((codes, [subtype]), {}),
                            ((codes, [subtype]), {"is_all_conn": False}),
                        ],
                    )
                except ProviderError:
                    self._warn("subscription_release_failed")
                    continue
                self._forget((code, subtype_name) for code in codes)

    def _fetch_chain(self, symbol: str, requested: Optional[date]) -> list[_Contract]:
        now = self._now()
        cache_key = f"{symbol}|{requested.isoformat() if requested else '*'}"
        cached = self._chain_cache.get(cache_key)
        # A zero cache lifetime disables caching.
        if cached and self._chain_cache_seconds > 0 and (
            0 <= (now - cached[0]).total_seconds() <= self._chain_cache_seconds
        ):
            return list(cached[1])
        start_end: dict[str, Any] = {}
        if requested is not None:
            start_end = {"start": requested.isoformat(), "end": requested.isoformat()}
        else:
            # A daily-expiry ETF's default ~30-day chain holds thousands of
            # contracts; request only the nearest expiries that are used.
            upcoming = self._upcoming_expiries(symbol, now)
            if upcoming:
                start_end = {"start": upcoming[0].isoformat(),
                             "end": upcoming[: self._max_expiries][-1].isoformat()}
        option_type = self._enum("OptionType", "ALL", "ALL")
        index_type = self._enum("IndexOptionType", "NORMAL", "NORMAL")
        variants = [
            ((symbol,), {**start_end, "index_option_type": index_type, "option_type": option_type}),
            ((symbol,), start_end),
            ((), {"code": symbol, **start_end}),
            ((symbol,), {}),
        ]
        data = self._call_variants("get_option_chain", variants)
        contracts: list[_Contract] = []
        for row in _chain_rows(data):
            contract = _contract_from_row(row)
            if contract is not None:
                contracts.append(contract)
        unique: dict[str, _Contract] = {item.contract_id: item for item in contracts}
        contracts = list(unique.values())
        self._chain_cache[cache_key] = (now, contracts)
        return contracts

    def _upcoming_expiries(self, symbol: str, now: datetime) -> list[date]:
        try:
            data = self._call_variants("get_option_expiration_date", [((symbol,), {})])
        except ProviderError as exc:
            self._warn(f"expiration_dates_unavailable:{exc.code}")
            return []
        today = now.astimezone(US_EASTERN).date()
        dates = set()
        for row in _rows(data):
            value = _text(_row_get(row, "strike_time", "expiry", "expiration_date"))[:10]
            try:
                day = date.fromisoformat(value)
            except ValueError:
                continue
            if day >= today:
                dates.add(day)
        return sorted(dates)

    def _underlying_quote(self, symbol: str) -> tuple[_QuoteData, Optional[bool]]:
        """Return the underlying quote and whether the underlying is a fund.

        get_market_snapshot is preferred because it carries bid/ask with the
        same update_time; get_stock_quote has no bid/ask columns.  An
        incomplete or crossed quote is replaced by a complete order book.
        """

        errors: list[ProviderError] = []
        quote: Optional[_QuoteData] = None
        fund: Optional[bool] = None
        for method_name in ("get_market_snapshot", "get_stock_quote"):
            if not callable(getattr(self._ensure_context(), method_name, None)):
                continue
            try:
                data = self._call_variants(
                    method_name,
                    [
                        (([symbol],), {}),
                        ((), {"code_list": [symbol]}),
                        ((), {"codes": [symbol]}),
                    ],
                )
                rows = _rows(data)
                row = next(
                    (
                        candidate
                        for candidate in rows
                        if _text(_row_get(candidate, "code", "security_code", "stock_code")).upper()
                        == symbol.upper()
                    ),
                    rows[0] if rows else None,
                )
                if row is not None:
                    quote = _quote_from_row(row)
                    fund = _underlying_is_fund(row)
                    break
            except ProviderError as exc:
                errors.append(exc)
                if exc.code in {"quote_permission", "subscription_quota"}:
                    raise exc
        if quote is None:
            if errors:
                raise errors[-1]
            raise ProviderError("quote_unavailable", f"No underlying quote returned for {symbol}")
        if _needs_order_book(quote, self._now()):
            try:
                self._subscribe([symbol], "ORDER_BOOK")
                quote = _merge_quote(quote, self._orderbook_quote(symbol)) or quote
            except ProviderError as exc:
                # The snapshot is then reported unverified rather than failing.
                self._warn(f"underlying_order_book_unavailable:{exc.code}")
        return quote, fund

    def _market_quotes(self, symbols: Sequence[str]) -> dict[str, _QuoteData]:
        if not symbols:
            return {}
        data = self._call_variants(
            "get_market_snapshot",
            [
                ((list(symbols),), {}),
                ((), {"code_list": list(symbols)}),
                ((), {"codes": list(symbols)}),
            ],
        )
        result: dict[str, _QuoteData] = {}
        rows = _rows(data)
        for index, row in enumerate(rows):
            code = _text(_row_get(row, "code", "security_code", "stock_code")).upper()
            if not code and index < len(symbols):
                code = symbols[index].upper()
            if code:
                result[code] = _quote_from_row(row)
        return result

    def _orderbook_quote(self, symbol: str) -> Optional[_QuoteData]:
        try:
            data = self._call_variants(
                "get_order_book",
                [
                    ((symbol,), {"num": 1}),
                    ((symbol, 1), {}),
                    ((symbol,), {}),
                ],
            )
        except ProviderError as exc:
            if exc.code in {"quote_permission", "subscription_quota"}:
                raise
            self._warn(f"order_book_unavailable:{symbol}:{exc.code}")
            return None
        quote = _orderbook_quote(data)
        if (quote.timestamp is None and quote.bid is not None and quote.ask is not None
                and not quote.crossed and (symbol, "ORDER_BOOK") in self._subscriptions):
            # OpenD leaves svr_recv_time empty until a subscribed book changes.
            # A book held by this connection's live subscription is the current
            # book, so its observation time is the evidence of freshness.
            quote = replace(quote, timestamp=self._now())
            self._warn("order_book_time_unreported_observed_on_live_subscription")
        return quote

    def _recent_bars(self, symbol: str) -> list[dict[str, Any]]:
        """Recent daily and 15-minute bars as model context, never as prices.

        Bars are optional: a failure leaves them empty with a warning and never
        blocks the quote snapshot.
        """

        if not callable(getattr(self._ensure_context(), "get_cur_kline", None)):
            return []
        now = self._now()
        bars: list[dict[str, Any]] = []
        for interval, subtype, count in (("1d", "K_DAY", 20), ("15m", "K_15M", 26)):
            try:
                self._subscribe([symbol], subtype)
                data = self._call_variants(
                    "get_cur_kline",
                    [((symbol, count), {"ktype": self._enum("KLType", subtype, subtype)})],
                )
            except ProviderError as exc:
                self._warn(f"bars_unavailable:{interval}:{exc.code}")
                continue
            for row in _rows(data)[-count:]:
                stamp = _aware_datetime(_row_get(row, "time_key", "time"), default_tz=US_EASTERN)
                values = {name: _safe_float(_row_get(row, name)) for name in ("open", "high", "low", "close")}
                if stamp is None or any(value is None for value in values.values()):
                    continue
                # Intraday times are bar end times; today's daily bar forms until the close.
                if interval == "15m":
                    partial = stamp > now
                else:
                    close = _resolve_session_close(stamp.astimezone(US_EASTERN).date())
                    partial = stamp.astimezone(US_EASTERN).date() == now.astimezone(US_EASTERN).date() and (
                        close is None or now < close)
                bars.append({"interval": interval, "time": stamp.isoformat(), **values,
                             "volume": _nonneg_int(_row_get(row, "volume")), "partial": partial})
        return bars

    def _trim_subscriptions(self, desired: set[tuple[str, str]]) -> None:
        """Keep the provider-owned subscription set bounded between ticks.

        Records younger than OpenD's one-minute minimum are kept until a later
        tick, since an earlier unsubscribe would be rejected.
        """

        now = self._now()
        old = {
            record
            for record in self._subscriptions - desired
            if (now - self._subscribed_at.get(record, now)).total_seconds() >= _MIN_SUBSCRIPTION_SECONDS
        }
        if old:
            self.release_subscriptions(old)

    @staticmethod
    def _prioritize_contracts(
        contracts: Sequence[_Contract],
        spot: float,
        *,
        requested: Optional[date],
        max_contracts: int,
        max_expiries: int,
        current_date: date,
        required_contracts: Sequence[str] = (),
    ) -> list[_Contract]:
        required = set(required_contracts)
        if len(required) > max_contracts:
            raise ProviderError("subscription_quota", "Required contracts exceed the configured quote limit")

        def is_required(item):
            # Contract ids, or ``right:strike:YYYY-MM-DD`` for legs of a user's own plan.
            return (item.contract_id in required
                    or f"{item.right}:{item.strike:g}:{item.expiry_date.isoformat()}" in required)

        filtered = [item for item in contracts
                    if requested is None or item.expiry_date == requested or is_required(item)]
        if requested is None:
            expiry_dates = sorted(
                {item.expiry_date for item in filtered},
                key=lambda value: (value < current_date, value),
            )
            expiry_dates = expiry_dates[:max_expiries]
            filtered = [item for item in filtered
                        if item.expiry_date in expiry_dates or is_required(item)]
        groups: dict[tuple[date, str], list[_Contract]] = {}
        for item in filtered:
            groups.setdefault((item.expiry_date, item.right), []).append(item)
        for values in groups.values():
            values.sort(key=lambda item: (abs(item.strike - spot), item.strike, item.contract_id))
        ordered_dates = sorted(
            {key[0] for key in groups},
            key=lambda value: (value < current_date, value),
        )
        selected = [item for item in filtered if is_required(item)]
        while len(selected) < max_contracts:
            added = False
            for expiry_date in ordered_dates:
                for right in ("call", "put"):
                    values = groups.get((expiry_date, right), [])
                    while values and values[0] in selected:
                        values.pop(0)
                    if values:
                        selected.append(values.pop(0))
                        added = True
                        if len(selected) >= max_contracts:
                            break
                if len(selected) >= max_contracts:
                    break
            if not added:
                break
        return selected

    def _snapshot_impl(
        self,
        ticker: str,
        expiry: Optional[date | datetime | str] = None,
        now: Optional[datetime] = None,
        required_contracts: Sequence[str] = (),
    ) -> QuoteSnapshot:
        if self._closed:
            raise ProviderError("provider_closed", "Moomoo provider is closed")
        self._call_warnings = []
        symbol_name = _normalize_ticker(ticker)
        symbol = _us_symbol(symbol_name)
        requested = _requested_expiry(expiry)
        # The static chain can take many seconds on a symbol's first request;
        # load it before any timestamped quote so quotes are not aged by it.
        contracts = self._fetch_chain(symbol, requested)
        received_at = _coerce_now(now) if now is not None else self._now()
        self._subscribe([symbol], "QUOTE")
        underlying, underlying_is_fund = self._underlying_quote(symbol)
        underlying_crossed = underlying.crossed
        # Only a valid, two-sided, non-crossed book verifies the underlying.
        underlying_book_valid = (
            underlying.bid is not None
            and underlying.ask is not None
            and 0 < underlying.bid <= underlying.ask
        )
        if underlying_crossed:
            # Preserve the observed bid/ask for provenance, but never mark a
            # crossed market as source-verified or fresh enough for a fill.
            self._warn("underlying_crossed_quote")
        if underlying.last is None or underlying.last <= 0:
            if underlying_book_valid:
                spot = (underlying.bid + underlying.ask) / 2.0
                self._warn("spot_from_valid_bid_ask_midpoint")
            else:
                raise ProviderError("quote_missing", f"Underlying {symbol} has no usable last/bid/ask price")
        else:
            spot = underlying.last
        quoted_at = _provider_timestamp_or_error(underlying.timestamp, label=f"Underlying {symbol}")
        raw_contract_count = len(contracts)
        verified_contracts: list[_Contract] = []
        expired_count = 0
        for item in contracts:
            expiry_close = _resolve_contract_expiry(item, symbol, underlying_is_fund)
            if expiry_close is not None:
                if expiry_close <= received_at:
                    # Past its cutoff: not quotable, and must not take a quota slot.
                    expired_count += 1
                    continue
                if _is_known_late_close_etf(item, symbol) and _explicit_contract_cutoff(
                    item.row, item.expiry_date
                ) is None:
                    self._warn("expiry_cutoff_nyse_arca_late_close_1615_et")
                verified_contracts.append(replace(item, expiry=expiry_close))
        contracts = verified_contracts
        if expired_count:
            self._warn("expired_contracts_excluded")
        if raw_contract_count and len(contracts) + expired_count != raw_contract_count:
            self._warn("expiry_unverified_or_unavailable")
        if requested is not None and not any(item.expiry_date == requested for item in contracts):
            self._warn(f"expiry_unverified_or_unavailable:{requested.isoformat()}")
        contracts = self._prioritize_contracts(
            contracts,
            spot,
            requested=requested,
            max_contracts=self._max_contracts,
            max_expiries=self._max_expiries,
            current_date=received_at.astimezone(US_EASTERN).date(),
            required_contracts=required_contracts,
        )
        option_quotes: list[OptionQuote] = []
        option_warnings: list[str] = []
        if contracts:
            option_codes = [item.contract_id for item in contracts]
            # Any subscription type for a still-shortlisted code is retained.
            desired = {(symbol, name) for name in _UNDERLYING_SUBTYPES} | {
                (code, "ORDER_BOOK") for code in option_codes
            }
            self._trim_subscriptions(desired)
            # get_market_snapshot needs no subscription; only order books do.
            # A per-option QUOTE subscription would spend quota without being read.
            try:
                market_quotes = self._market_quotes(option_codes)
            except ProviderError as exc:
                if exc.code in {"quote_permission", "subscription_quota"}:
                    raise ProviderError("option_" + exc.code, exc.message)
                market_quotes = {}
                option_warnings.append(f"option_market_snapshot_failed:{exc.code}")
            def _snapshot_quote(contract: _Contract) -> Optional[_QuoteData]:
                return market_quotes.get(contract.contract_id.upper()) or market_quotes.get(contract.contract_id)

            # The snapshot's timestamp is the last trade, so a contract that has
            # not traded recently still needs its live order book for fresh bid/ask.
            needs_book = [
                contract.contract_id
                for contract in contracts
                if _needs_order_book(_snapshot_quote(contract), received_at)
            ]
            books: dict[str, Optional[_QuoteData]] = {}
            if needs_book:
                try:
                    self._subscribe(needs_book, "ORDER_BOOK")
                except ProviderError as exc:
                    if exc.code in {"quote_permission", "subscription_quota"}:
                        raise
                    option_warnings.append(f"option_order_book_unavailable:{exc.code}")
                    needs_book = []
                books = {code: self._orderbook_quote(code) for code in needs_book}
            # Ages are measured when quotes are evaluated: a slow (cold) snapshot
            # reads books seconds after it started, which is not a future quote.
            evaluated_at = max(received_at, self._now())
            for contract in contracts:
                quote = _snapshot_quote(contract)
                if contract.contract_id in books:
                    quote = _merge_quote(quote, books[contract.contract_id])
                if quote is None:
                    option_warnings.append(f"option_quote_missing:{contract.contract_id}")
                    continue
                if quote.crossed or quote.bid is None or quote.ask is None:
                    option_warnings.append(f"option_crossed_or_incomplete:{contract.contract_id}")
                    continue
                if quote.timestamp is None:
                    option_warnings.append(f"option_timestamp_missing:{contract.contract_id}")
                    continue
                if quote.bid > quote.ask:
                    option_warnings.append(f"option_crossed:{contract.contract_id}")
                    continue
                option_age = (evaluated_at - quote.timestamp).total_seconds()
                if not -CLOCK_SKEW_SECONDS <= option_age <= MAX_QUOTE_AGE_SECONDS:
                    option_warnings.append(f"option_quote_stale:{contract.contract_id}")
                    continue
                option_quotes.append(
                    OptionQuote(
                        contract_id=contract.contract_id,
                        underlying=symbol_name,
                        right=contract.right,
                        strike=contract.strike,
                        expiry=contract.expiry,
                        expiry_verified=True,
                        multiplier=contract.multiplier,
                        bid=quote.bid,
                        ask=quote.ask,
                        quoted_at=quote.timestamp,
                        iv=quote.iv,
                        bid_size=quote.bid_size,
                        ask_size=quote.ask_size,
                        volume=quote.volume,
                        open_interest=quote.open_interest,
                        standard=True,
                        exercise_style=contract.exercise_style,
                    )
                )
        else:
            # A request that yields no verified contracts must release option
            # subscriptions retained by the previous tick.
            self._trim_subscriptions({(symbol, name) for name in _UNDERLYING_SUBTYPES})
        # Fetched before warnings are assembled so bar failures are reported.
        bars = self._recent_bars(symbol)
        warnings = list(dict.fromkeys(self._warnings + self._call_warnings + option_warnings))
        age = (received_at - quoted_at).total_seconds()
        age_ok = -CLOCK_SKEW_SECONDS <= age <= MAX_QUOTE_AGE_SECONDS
        stale = not underlying_book_valid or not age_ok
        if underlying_crossed:
            warnings.append("underlying_quote_crossed")
        elif not underlying_book_valid:
            warnings.append("underlying_bid_ask_unavailable")
        if not age_ok:
            warnings.append("underlying_quote_stale")
        return QuoteSnapshot(
            underlying=symbol_name,
            spot=spot,
            bid=underlying.bid,
            ask=underlying.ask,
            quoted_at=quoted_at,
            received_at=received_at,
            provider=self.provider_name,
            mode="live",
            bars=bars,
            session=_session_label(quoted_at),
            source_verified=underlying_book_valid,
            stale=stale,
            options=option_quotes,
            warnings=list(dict.fromkeys(warnings)),
            evidence=[
                {
                    "kind": "quote_source",
                    "source": self.provider_name,
                    "sdk": self._sdk_name,
                    "endpoint": f"{self._host}:{self._port}",
                    "underlying_quote_timestamp": quoted_at.isoformat(),
                    "static_chain_cached": any(
                        key.startswith(f"{symbol}|") for key in self._chain_cache
                    ),
                    "option_count": len(option_quotes),
                    "read_only": True,
                }
            ],
        )
    def snapshot(
        self,
        ticker: str,
        expiry: Optional[date | datetime | str] = None,
        now: Optional[datetime] = None,
        required_contracts: Sequence[str] = (),
    ) -> QuoteSnapshot:
        # OpenD quote contexts are not documented as thread-safe. Serialize
        # snapshot, health, reconnect, and close operations on this provider.
        with self._lock:
            return self._snapshot_impl(ticker, expiry=expiry, now=now, required_contracts=required_contracts)

    def _health_impl(self) -> dict[str, Any]:
        """Probe only quote connection, rights, and subscription quota."""

        self._call_warnings = []
        health: dict[str, Any] = {
            "provider": self.provider_name,
            "mode": "live",
            "configured": self.configured,
            "host": self._host,
            "port": self._port,
            "sdk_available": False,
            "sdk": self._sdk_name,
            "connected": self._ctx is not None,
            "quote_right": None,
            "option_right": None,
            "quote_logged_in": None,
            "protocol_encrypted": self._encryption_key_file() is not None,
            "quota": {"available": None, "remaining": None, "used": None},
            "active_subscriptions": len(self._subscriptions),
            "warnings": list(self._warnings),
            "last_error": None,
        }
        sdk = self._load_sdk()
        health["sdk_available"] = sdk is not None
        health["sdk"] = self._sdk_name
        if not self.configured:
            health.update(
                status="blocked",
                ok=False,
                code="opend_not_configured",
                message="Set TRADE_DESK_OPEND_HOST or FUTU_OPEND_HOST to enable live OpenD quotes",
            )
            return health
        if sdk is None:
            health.update(
                status="blocked",
                ok=False,
                code="sdk_unavailable",
                message="Neither moomoo nor the pinned futu compatibility SDK is installed",
            )
            return health
        try:
            self._ensure_context()
        except ProviderError as exc:
            health.update(
                status="blocked",
                ok=False,
                code=exc.code,
                message=exc.message,
                last_error={"code": exc.code, "message": exc.message},
            )
            return health
        health["connected"] = True
        global_state = None
        try:
            global_state = self._call("get_global_state", retry_connection=False)
        except ProviderError as exc:
            health["warnings"].append(f"global_state_unavailable:{exc.code}")
        state_rows = _rows(global_state)
        state = state_rows[0] if state_rows else global_state
        quote_right = _row_get(state, "quote_right", "basic_quote_right", "us_quote_right", "normal_quote_right")
        option_right = _row_get(
            state,
            "option_quote_right",
            "derivative_quote_right",
            "option_right",
            "us_option_quote_right",
        )
        # Blockers use the normalized values: 0, "0", "NO" and "NONE" are denials.
        health["quote_right"] = _right_flag(quote_right)
        health["option_right"] = _right_flag(option_right)
        # get_global_state reports qot_logined ('1'/'0'); OpenD can be connected
        # while logged out of the quote server.
        health["quote_logged_in"] = _flag(_row_get(state, "qot_logined", "quote_logined"))
        sub_data = self._query_subscription()
        codes, remaining, used = self._subscription_details(sub_data)
        health["active_subscriptions"] = len(codes) if codes else len(self._subscriptions)
        health["quota"] = {
            "available": None if sub_data is None else (remaining is None or remaining > 0),
            "remaining": remaining,
            "used": used,
        }
        health["subscription_right"] = remaining is not None or sub_data is not None
        blockers = []
        if health["quote_logged_in"] is False:
            blockers.append("quote_login_required")
        if health["quote_right"] is False:
            blockers.append("quote_permission")
        if health["option_right"] is False:
            blockers.append("option_permission")
        if remaining is not None and remaining <= 0:
            blockers.append("subscription_quota")
        if blockers:
            health.update(
                status="blocked",
                ok=False,
                code=blockers[0],
                message="; ".join(blockers),
            )
        elif health["quote_right"] is not True or health["option_right"] is not True or sub_data is None:
            health.update(
                status="degraded",
                ok=False,
                code="rights_unknown",
                message="OpenD connected, but quote rights or subscription quota could not be verified",
            )
        else:
            health.update(status="ready", ok=True, code=None, message="Read-only quote connection is ready")
        health["warnings"] = list(dict.fromkeys(health["warnings"] + self._warnings + self._call_warnings))
        return health

    def health(self) -> dict[str, Any]:
        with self._lock:
            return self._health_impl()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.release_subscriptions()
            self._closed = True
            ctx = self._ctx
            self._ctx = None
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
            # Closing the connection releases any record OpenD refused to
            # unsubscribe (for example inside the one-minute minimum).
            self._forget(set(self._subscriptions))


def build_provider(mode: str):
    """Build the provider selected by the request's explicit data mode."""

    normalized = _text(mode).strip().lower()
    if normalized == "replay":
        return ReplayProvider()
    if normalized == "live":
        return MoomooProvider()
    raise ProviderError("unsupported_mode", f"Unsupported Trade Desk provider mode: {mode!r}")


__all__ = [
    "MoomooProvider",
    "ProviderError",
    "ReplayProvider",
    "build_provider",
    "snapshot_fresh",
]
