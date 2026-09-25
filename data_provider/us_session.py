"""US quote sessions and provider-time validation shared by research and screening."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import math

from src.core.trading_calendar import get_effective_trading_date, get_market_session_bounds

NEW_YORK = ZoneInfo("America/New_York")
MAX_QUOTE_AGE_SECONDS = 20 * 60
QUOTE_SESSIONS = frozenset({"premarket", "regular", "postmarket"})
# Extended trading ends at 20:00, or four hours after an early close (13:00 -> 17:00).
POSTMARKET_AFTER_CLOSE = timedelta(hours=4)


def session_window(now=None):
    """Return the active extended/regular session, respecting holidays and early closes.

    Unknown calendars fail closed. Overnight trading is outside this data contract.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("US session time must include a timezone")
    local = now.astimezone(NEW_YORK)
    opening, closing = get_market_session_bounds("us", local)
    if opening is None or closing is None:
        return "closed", None, None
    pre = datetime.combine(local.date(), time(4), NEW_YORK)
    post = min(datetime.combine(local.date(), time(20), NEW_YORK), closing + POSTMARKET_AFTER_CLOSE)
    for name, start, end in (("premarket", pre, opening), ("regular", opening, closing),
                             ("postmarket", closing, post)):
        if start <= local < end:
            return name, start, end
    return "closed", None, None


def _quote_field(quote, name):
    if isinstance(quote, dict):
        return quote.get(name)
    return getattr(quote, name, None)


def is_us_quote(quote, code=None):
    """Whether a quote (object or dict) belongs to the US session contract.

    ``code`` overrides the quote's own code when the caller knows the requested symbol.
    """
    from data_provider.us_index_mapping import is_us_index_code, is_us_stock_code

    if quote is not None and _quote_field(quote, "market") == "us":
        return True
    code = code if code is not None else _quote_field(quote, "code")
    return isinstance(code, str) and (is_us_stock_code(code) or is_us_index_code(code))


def is_stale_us_quote(quote, code=None):
    """Only US quotes are gated on staleness; other markets keep their prior behavior."""
    return quote is not None and _quote_field(quote, "is_stale") is True and is_us_quote(quote, code)


def has_us_session_contract(quote):
    """Whether apply_us_quote_metadata already decided this quote's session freshness."""
    return (_quote_field(quote, "quote_session") in QUOTE_SESSIONS
            and isinstance(_quote_field(quote, "is_stale"), bool)
            and quote_time(_quote_field(quote, "provider_timestamp")) is not None)


def sdk_epoch(value):
    """Epoch seconds from an SDK datetime (naive = SDK local time) or number; else None."""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if math.isfinite(value) and value > 0 else None
    except (OverflowError, OSError, ValueError):
        return None
    return None


def us_daily_history_end(end_date=None, now=None):
    """Yahoo's exclusive US daily end, including today only after a verified close.

    Preserve historical request bounds. Use New York's date for current/future
    bounds so a server in another timezone cannot include an unfinished session.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("US daily history time must include a timezone")
    local = now.astimezone(NEW_YORK)
    requested = date.fromisoformat(end_date) if end_date else local.date()
    if requested < local.date():
        return requested.isoformat()
    _, closing = get_market_session_bounds("us", local)
    include_today = closing is not None and local >= closing
    return (local.date() + timedelta(days=int(include_today))).isoformat()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def quote_time(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def is_fresh_regular_quote(quote, now=None):
    """Whether a US quote can update regular daily OHLC in calculations or reports."""
    if getattr(quote, "is_stale", None) is True or getattr(quote, "quote_session", None) != "regular":
        return False
    stamp = quote_time(getattr(quote, "provider_timestamp", None))
    if stamp is None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NEW_YORK)
    session, start, end = session_window(now)
    return bool(session == "regular" and start <= stamp < end and
                0 <= (now - stamp).total_seconds() <= MAX_QUOTE_AGE_SECONDS)


def apply_us_quote_metadata(quote, info, now=None):
    """Select the active session price and retain stale fallback data as explicitly stale."""
    now = now or datetime.now(timezone.utc)
    session, start, end = session_window(now)
    prefix = {"premarket": "preMarket", "postmarket": "postMarket"}.get(session, "regularMarket")
    price = number(info.get(prefix + "Price"))
    stamp = quote_time(info.get(prefix + "Time"))
    selected_session = session
    # An extended-hours price from an earlier session (e.g. yesterday's
    # after-hours print shortly after today's close) is not this session's
    # quote; comparing it with today's reference would invent a session move.
    in_session = bool(stamp and start and end and start <= stamp < end)
    if price is None or price <= 0 or stamp is None or (prefix != "regularMarket" and not in_session):
        prefix = "regularMarket"
        selected_session = "regular"
        price = number(info.get(prefix + "Price"))
        stamp = quote_time(info.get(prefix + "Time"))
    if price is not None and price > 0:
        quote.price = price
    else:
        # This timestamp cannot establish the age of a retained fallback price.
        stamp = None
    quote.quote_session = selected_session if stamp else "unknown"
    quote.provider_timestamp = stamp.isoformat() if stamp else None
    quote.fetched_at = now.isoformat()
    age = (now - stamp).total_seconds() if stamp else None
    quote.stale_seconds = int(max(0, age)) if age is not None else None
    valid = bool(stamp and start and end and start <= stamp < end and
                 selected_session == session and 0 <= age <= MAX_QUOTE_AGE_SECONDS)
    quote.is_stale = not valid
    if not valid:
        quote.data_quality = "partial"
        quote.missing_fields = list(dict.fromkeys([*(quote.missing_fields or []), "fresh_session_quote"]))
    reference = number(info.get("regularMarketPrice" if selected_session == "postmarket"
                                else "regularMarketPreviousClose"))
    if selected_session == "premarket":
        reference = number(info.get("regularMarketPrice"))
    if selected_session in {"premarket", "postmarket"}:
        # A fresh extended quote does not establish that regularMarketPrice is
        # the expected close. Check its own date/time, including early closes.
        reference_stamp = quote_time(info.get("regularMarketTime"))
        reference_date = get_effective_trading_date("us", current_time=now)
        reference_day = datetime.combine(reference_date, time(12), NEW_YORK)
        _, reference_close = get_market_session_bounds("us", reference_day)
        reference_valid = bool(
            reference is not None and reference > 0 and reference_stamp and reference_close
            and reference_stamp.astimezone(NEW_YORK).date() == reference_date
            and reference_stamp <= now
            and abs((reference_stamp - reference_close).total_seconds()) <= MAX_QUOTE_AGE_SECONDS
        )
        if not reference_valid:
            reference = None
            quote.data_quality = "partial"
            quote.missing_fields = list(dict.fromkeys([
                *(quote.missing_fields or []), "regular_session_reference_close",
            ]))
    if selected_session == "postmarket" and reference is not None:
        # After the close, `reference` is today's validated regular close; keep
        # the day's move beside the after-hours move so reports can show both.
        previous = number(info.get("regularMarketPreviousClose"))
        quote.regular_close = reference
        quote.regular_change_pct = (reference / previous - 1) * 100 if previous and previous > 0 else None
    quote.pre_close = reference
    quote.change_amount = price - reference if price and price > 0 and reference and reference > 0 else None
    quote.change_pct = quote.change_amount / reference * 100 if quote.change_amount is not None else None
    if selected_session in {"premarket", "postmarket"}:
        # Regular-session OHLC/volume must never be attributed to an extended-hours quote.
        quote.open_price = quote.high = quote.low = quote.volume = quote.amplitude = None
        quote.amount = quote.volume_ratio = quote.turnover_rate = None
    return quote
