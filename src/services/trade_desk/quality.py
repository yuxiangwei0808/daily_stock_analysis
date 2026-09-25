"""Common provider-independent freshness checks for executable observations."""
from datetime import datetime

from .models import utcnow

MAX_QUOTE_AGE_SECONDS = 30
# OpenD and this host keep separate clocks; a just-received quote can carry a
# timestamp slightly after the local clock and must not be rejected as stale.
CLOCK_SKEW_SECONDS = 2.0


def quote_age_ok(quoted_at, now, max_age_seconds=MAX_QUOTE_AGE_SECONDS):
    return -CLOCK_SKEW_SECONDS <= (now - quoted_at).total_seconds() <= max_age_seconds


def snapshot_fresh(snapshot, now=None, max_age_seconds=MAX_QUOTE_AGE_SECONDS):
    now = now or utcnow()
    if now.tzinfo is None or snapshot.stale:
        return False
    if snapshot.mode == 'live' and not snapshot.source_verified:
        return False
    if not quote_age_ok(snapshot.quoted_at, now, max_age_seconds):
        return False
    if snapshot.bid is not None and snapshot.ask is not None:
        if snapshot.bid <= 0 or snapshot.ask < snapshot.bid:
            return False
    return snapshot.spot > 0


def quote_matches_leg(snapshot, quote, leg):
    """Contract IDs alone cannot establish a contract's economic identity."""
    if quote is None or quote.underlying != snapshot.underlying or not quote.standard or not quote.expiry_verified:
        return False
    expiry = leg.get("expiry")
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    return (quote.contract_id == leg.get("contract_id") and quote.right == leg.get("right")
            and quote.strike == leg.get("strike") and quote.expiry == expiry
            and quote.multiplier == leg.get("multiplier")
            and quote.exercise_style == leg.get("exercise_style", "american"))


def candidate_quotes_fresh(snapshot, candidate, now=None):
    """All legs needed by a proposal must have contemporaneous executable quotes."""
    now = now or utcnow()
    if not snapshot_fresh(snapshot, now=now):
        return False
    legs = candidate["legs"] if isinstance(candidate, dict) else [leg.model_dump() for leg in candidate.legs]
    underlying = candidate.get("underlying") if isinstance(candidate, dict) else candidate.underlying
    if underlying is not None and underlying != snapshot.underlying:
        return False
    quotes = {quote.contract_id: quote for quote in snapshot.options}
    for leg in legs:
        if leg["right"] == "stock":
            if (leg["contract_id"] != snapshot.underlying or leg.get("multiplier") != 1
                    or snapshot.bid is None or snapshot.ask is None or snapshot.bid <= 0 or snapshot.ask < snapshot.bid):
                return False
            continue
        quote = quotes.get(leg["contract_id"])
        if (not quote_matches_leg(snapshot, quote, leg) or quote.expiry <= now
                or not quote_age_ok(quote.quoted_at, now)
                or quote.bid <= 0 or quote.ask < quote.bid):
            return False
    return bool(legs)
