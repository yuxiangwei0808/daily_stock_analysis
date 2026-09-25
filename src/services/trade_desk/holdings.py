"""Broker holdings from one moomoo account (read only): monitoring, default alerts and your own rules.

Enabled with ``TRADE_DESK_BROKER_ACCOUNT`` (the account id or its last digits).
The worker (leader only) reads positions through OpenD every 10 minutes in the
regular session and once after the close; nothing is ever traded.

- Option positions are grouped per underlying and expiry (a vertical spread is
  one position). Defaults for short-term options: expiry warnings 2 and 1
  trading days before, at the open of expiration day and one hour before its
  close; assignment risk when a short leg is in the money within 2 trading
  days; profit at 75 % of a defined maximum (else +50 % / +100 % on cost); loss
  at -50 % on cost; earnings on or before expiry.
- Stock positions: a break of the prior 20-day low or a first close-to-price
  move below MA50 (once a day each), and earnings within 7 days.
- Rules you add: underlying price below/above X, N trading days to expiry,
  position P&L below/above X %; once (default) or once a day.

Discord messages carry percentages only (weight of the account, P&L on cost),
never share counts, cost or dollar amounts; the dashboard shows the details.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import earnings, trend
from .models import identity, utcnow

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
OPTION_CODE = re.compile(r"^(?:US\.)?([A-Z][A-Z0-9.]*?)(\d{6})([CP])(\d+)$")  # adjusted roots: TSLA1
SYNC_SECONDS = 600
QUOTE_SECONDS = 60
EXPIRY_WARN_DAYS = (2, 1)
MORNING = dtime(9, 45)
LAST_HOUR = dtime(15, 0)
ASSIGNMENT_DAYS = 2
PROFIT_OF_MAX = 75.0
PROFIT_ON_COST = (50.0, 100.0)
LOSS_ON_COST = -50.0
STOCK_EARNINGS_DAYS = 7
SUMMARY_AT = dtime(16, 15)  # after the post-close sync at 16:05
LEVELS_RETRY_SECONDS = 300
RULE_KINDS = {
    "price_below": "price at or below", "price_above": "price at or above",
    "days_to_expiry": "trading days to expiry at most", "pnl_below": "P&L at or below", "pnl_above": "P&L at or above",
}
MAX_RULES = 100


def account() -> str:
    return (os.getenv("TRADE_DESK_BROKER_ACCOUNT") or "").strip()


def enabled() -> bool:
    return bool(account())


def _local(now: datetime) -> datetime:
    return now.astimezone(_NEW_YORK)


def parse_code(code: str) -> Dict[str, Any]:
    text = code.strip().upper()
    bare = text[3:] if text.startswith("US.") else text
    match = OPTION_CODE.match(text)
    if not match:
        return {"kind": "stock", "ticker": bare}
    underlying, stamp, right, strike = match.groups()
    return {"kind": "option", "ticker": bare, "underlying": underlying,
            "expiry": date(2000 + int(stamp[:2]), int(stamp[2:4]), int(stamp[4:])),
            "right": "call" if right == "C" else "put", "strike": int(strike) / 1000}


CALENDAR_HORIZON = timedelta(days=300)  # the holiday calendar only covers about a year ahead


@lru_cache(maxsize=4096)
def _calendar_open(day: date) -> bool:
    from src.core.trading_calendar import is_market_open
    try:
        return bool(is_market_open("us", day))
    except Exception:
        return day.weekday() < 5


def _trading_day(day: date, today: Optional[date] = None) -> bool:
    if today is not None and day - today > CALENDAR_HORIZON:
        return day.weekday() < 5  # beyond the calendar: weekdays, without per-day warnings
    return _calendar_open(day)


def trading_days_until(expiry: date, today: date) -> int:
    """Trading sessions after today up to and including expiry (0 on expiration day)."""
    days, cursor = 0, today
    while cursor < expiry:
        cursor += timedelta(days=1)
        days += 1 if _trading_day(cursor, today) else 0
    return days


def _mark(quote: Optional[Dict[str, Any]], fallback: Optional[float]) -> Optional[float]:
    """Bid/ask mid; with no bid, half the ask. A last trade is used only without any quote."""
    if quote:
        bid, ask = quote.get("bid") or 0, quote.get("ask") or 0
        if ask > 0 and 0 <= bid <= ask:
            return (bid + ask) / 2
        if quote.get("price"):
            return quote["price"]
    return fallback


def _intrinsic(legs: List[Dict[str, Any]], spot: float) -> float:
    return sum(leg["qty"] * 100 * (max(0.0, spot - leg["strike"]) if leg["right"] == "call"
                                   else max(0.0, leg["strike"] - spot)) for leg in legs)


def _label(legs: List[Dict[str, Any]]) -> str:
    strikes = "/".join(f"{leg['strike']:g}" for leg in sorted(legs, key=lambda leg: leg["strike"]))
    rights = {leg["right"] for leg in legs}
    letter = "C" if rights == {"call"} else "P" if rights == {"put"} else ""
    if len(legs) == 1:
        leg = legs[0]
        return f"{'short ' if leg['qty'] < 0 else ''}{leg['strike']:g}{letter}"
    if len(legs) == 2 and len(rights) == 1 and legs[0]["qty"] == -legs[1]["qty"]:
        return f"{strikes}{letter} spread"
    return f"{strikes} {letter or 'options'} ({len(legs)} legs)".strip()


def build_view(raw: Dict[str, Any], quotes: Dict[str, Dict[str, Any]], today: date) -> Dict[str, Any]:
    """Stocks and option positions (legs grouped per underlying and expiry) with marks and P&L."""
    total = raw.get("total_assets") or 0
    stocks, groups = [], {}
    for row in raw.get("positions") or []:
        info = parse_code(row["code"])
        qty = float(row["qty"]) * (-1 if row.get("side") == "SHORT" and row["qty"] > 0 else 1)
        if info["kind"] == "stock":
            quote = quotes.get(info["ticker"]) or {}
            price = quote.get("price") or row.get("price")
            prev_close = quote.get("prev_close")
            value = qty * price if price else row.get("market_value")
            cost = row.get("average_cost")
            stocks.append({"key": info["ticker"], "ticker": info["ticker"], "name": row.get("name", ""), "qty": qty,
                           "average_cost": cost, "price": price, "value": value,
                           "day_pct": (price / prev_close - 1) * 100 if price and prev_close else None,
                           "weight_pct": abs(value) / total * 100 if total and value else None,
                           "pnl_pct": ((price / cost - 1) * 100 * (1 if qty > 0 else -1)) if price and cost and cost > 0
                           else row.get("pl_pct")})
            continue
        mark = _mark(quotes.get(info["ticker"]), row.get("price"))
        groups.setdefault((info["underlying"], info["expiry"]), []).append(
            {**info, "code": info["ticker"], "name": row.get("name", ""), "qty": qty,
             "average_cost": row.get("average_cost"), "mark": mark,
             "prev_close": (quotes.get(info["ticker"]) or {}).get("prev_close")})
    options = []
    for (underlying, expiry), legs in sorted(groups.items()):
        cost = sum(leg["qty"] * (leg["average_cost"] or 0) * 100 for leg in legs)
        marks_known = all(leg["mark"] is not None for leg in legs)
        value = sum(leg["qty"] * leg["mark"] * 100 for leg in legs) if marks_known else None
        net_calls = sum(leg["qty"] for leg in legs if leg["right"] == "call")
        strikes = sorted({leg["strike"] for leg in legs})
        points = [0.0, *strikes, strikes[-1] * 3]
        # A cap exists only with short legs; long-only positions (a long put too) use on-cost levels.
        capped = net_calls <= 0 and any(leg["qty"] < 0 for leg in legs)
        max_value = max(_intrinsic(legs, spot) for spot in points) if capped else None
        pnl_pct = (value - cost) / abs(cost) * 100 if value is not None and cost else None
        previous = (sum(leg["qty"] * leg["prev_close"] * 100 for leg in legs)
                    if all(leg.get("prev_close") for leg in legs) else None)
        day_pct = (value - previous) / abs(previous) * 100 if value is not None and previous else None
        pct_of_max = ((value - cost) / (max_value - cost) * 100
                      if value is not None and max_value is not None and max_value > cost else None)
        spot = (quotes.get(underlying) or {}).get("price")
        # The contracts, not the size: adding to or trimming a position keeps its alerts.
        signature = hashlib.sha1("|".join(sorted(leg["code"] for leg in legs)).encode()).hexdigest()[:10]
        options.append({"key": f"{underlying} {expiry.isoformat()}", "underlying": underlying, "signature": signature,
                        "expired": expiry < today,
                        "expiry": expiry.isoformat(), "days_left": trading_days_until(expiry, today),
                        "label": _label(legs), "legs": legs, "cost": cost, "value": value, "max_value": max_value,
                        "pnl_pct": pnl_pct, "pct_of_max": pct_of_max, "underlying_price": spot, "day_pct": day_pct,
                        "weight_pct": abs(value) / total * 100 if total and value is not None else None})
    return {"account": raw.get("account"), "account_type": raw.get("account_type"),
            "synced_at": raw.get("synced_at"), "total_assets": total, "cash": raw.get("cash"),
            "stocks": stocks, "options": options}


def _pct(value: Optional[float]) -> str:
    return f"{value:+.1f}%" if isinstance(value, (int, float)) else "n/a"


def moneyness(position: Dict[str, Any]) -> str:
    """Each leg in or out of the money at the current underlying price."""
    spot = position.get("underlying_price")
    if not spot:
        return ""
    parts = []
    for leg in sorted(position["legs"], key=lambda leg: leg["strike"]):
        itm = spot > leg["strike"] if leg["right"] == "call" else spot < leg["strike"]
        parts.append(f"{'short' if leg['qty'] < 0 else 'long'} {leg['strike']:g}{'C' if leg['right'] == 'call' else 'P'} "
                     f"{'in' if itm else 'out of'} the money")
    return f"{position['underlying']} {spot:.2f}: " + ", ".join(parts)


def discord_safe(text: str) -> str:
    """Your note may mention amounts or sizes; Discord gets percentages only."""
    text = re.sub(r"[$€£¥]\s?[\d,.]+\s*[kKmM]?\b|\b[\d,.]+\s*(?:usd|dollars?)\b", "[amount]", text, flags=re.I)
    return re.sub(r"\b\d[\d,]*\s*(contracts?|shares?|lots?)\b", r"[n] \1", text, flags=re.I)


def describe_option(position: Dict[str, Any], with_days: bool = True) -> str:
    """Discord-safe summary: no quantities or amounts."""
    expiry = date.fromisoformat(position["expiry"])
    parts = [f"{position['underlying']} {expiry:%m/%d} {position['label']}", f"{_pct(position['pnl_pct'])} on cost"]
    if (position.get("pct_of_max") or 0) > 0:
        parts.append(f"{position['pct_of_max']:.0f}% of max profit")
    days = position["days_left"]
    if with_days or position.get("expired"):
        parts.append("expired" if position.get("expired") else "expires today" if days == 0
                     else f"{days} trading day{'s' if days != 1 else ''} left")
    return " · ".join(parts)


def describe_stock(position: Dict[str, Any]) -> str:
    weight = f"{position['weight_pct']:.1f}% of account" if position.get("weight_pct") is not None else ""
    return " · ".join(part for part in (f"{position['ticker']} shares", weight,
                                        f"{_pct(position['pnl_pct'])} vs avg cost") if part)


# -- rule text ------------------------------------------------------------------------
_NUMBER = r"\$?([-+]?\d+(?:\.\d+)?)"
_BELOW = re.compile(r"\b(loss|lose|loses|lost|losing|down|drawdown|below|under|drops?|falls?|lower|stop)\b|<|-\s*\d", re.I)
_PNL_WORDS = re.compile(r"\b(loss|lose|loses|lost|losing|profit|gain|gains|p&l|pnl|drawdown|position|spread|option|"
                        r"calls?|puts?|down|up|stop|return)\b|p&l", re.I)
_ABOVE = re.compile(r"\b(profit|gain|gains|up|above|over|rises?|reach(?:es)?|hits?|target|higher)\b|>|\+\s*\d", re.I)


def parse_rule_text(text: str) -> Optional[Dict[str, Any]]:
    """Common phrasings to a rule draft; None when the wording is ambiguous or not recognised."""
    lowered = " ".join(text.lower().split())
    for pattern in (rf"\b{_NUMBER}\s*(?:trading\s*)?days?\s*(?:left\s*)?(?:before|to|until|till|of|from)?\s*(?:the\s*)?expir",
                    rf"\bexpir\w*\s*(?:in|within|of|at)?\s*{_NUMBER}\s*(?:trading\s*)?days?\b"):
        match = re.search(pattern, lowered)
        if match:
            return {"kind": "days_to_expiry", "value": abs(float(match.group(1))), "note": text.strip()[:200]}
    percent = re.search(rf"{_NUMBER}\s*%", lowered)
    if percent:
        if re.search(r"\bof\s+(?:the\s+)?max", lowered):
            return None  # share of max profit is not an alert type
        if not _PNL_WORDS.search(lowered):
            return None  # "USO falls 5%" is a price move, not position P&L
        value = float(percent.group(1))
        below, above = bool(_BELOW.search(lowered)), bool(_ABOVE.search(lowered))
        if percent.group(1).startswith("-") or (below and not above):
            return {"kind": "pnl_below", "value": -abs(value), "note": text.strip()[:200]}
        if percent.group(1).startswith("+") or (above and not below):
            return {"kind": "pnl_above", "value": abs(value), "note": text.strip()[:200]}
        return None
    # A price: a number not followed by % or a period word ("50 day average").
    for kind, words in (("price_below", r"(?:drops?|falls?|below|under|stop(?:\s*loss)?|breaks? down|lower than|<=?)"),
                        ("price_above", r"(?:rises?|above|over|exceeds?|breaks? out|higher than|reach(?:es)?|hits?|"
                                        r"target|>=?)")):
        match = re.search(rf"(?:\b|(?=[<>])){words}\D{{0,25}}?{_NUMBER}(?![\d.]*\s*(?:%|-?\s*days?\b|/))", lowered)
        if match:
            return {"kind": kind, "value": abs(float(match.group(1))), "note": text.strip()[:200]}
    return None


def parse_rule_with_model(text: str, position: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Model fallback; it sees only the Discord-safe description of the position, never amounts."""
    from src.agent.runner import try_parse_json
    from .advisor import _generation_backend
    described = ("none (a ticker alert)" if not position else describe_option(position) if "expiry" in position
                 else describe_stock(position))
    prompt = ("Turn this alert request about one brokerage position into JSON. Kinds: price_below / price_above "
              "(underlying stock price), days_to_expiry (trading days left, options only), pnl_below / pnl_above "
              "(position P&L in percent on cost; below uses a negative number). Return only "
              '{"kind": "...", "value": <number>} or {"kind": null} if it is not one of these.\n'
              f"Position: {described}\nRequest: {text}")
    backend, _ = _generation_backend()
    data = try_parse_json(backend.generate(prompt, {"temperature": 0, "max_output_tokens": 512}).text or "") or {}
    if data.get("kind") in RULE_KINDS and isinstance(data.get("value"), (int, float)):
        return {"kind": data["kind"], "value": float(data["value"]), "note": text.strip()[:200]}
    return None


def validate_rule(rule: Dict[str, Any], position: Optional[Dict[str, Any]], *,
                  check_position: bool = True) -> Dict[str, Any]:
    kind, value = rule.get("kind"), rule.get("value")
    if kind not in RULE_KINDS:
        raise ValueError(f"Unknown alert type {kind!r}")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("Alert value must be a number") from None
    if kind.startswith("price") and value <= 0:
        raise ValueError("Price must be positive")
    if kind == "days_to_expiry":
        if check_position and (position is None or "expiry" not in position):
            raise ValueError("Days-to-expiry alerts apply to an option position")
        if not 0 <= value <= 60:
            raise ValueError("Days to expiry must be between 0 and 60")
    if check_position and kind.startswith("pnl") and position is None:
        raise ValueError("P&L alerts apply to a position")
    return {"kind": kind, "value": value}


# -- store and monitor ----------------------------------------------------------------
class Holdings:
    """Snapshot, live view and rules; shared by the API and the worker."""

    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self._lock = threading.Lock()

    def raw(self) -> Dict[str, Any]:
        return self.repo.setting("broker_holdings", {})

    def sync(self) -> Dict[str, Any]:
        data = self.service.provider("live").broker_positions(
            account(), os.getenv("TRADE_DESK_BROKER_SECURITY_FIRM", "FUTUINC") or "FUTUINC")
        data["synced_at"] = utcnow().isoformat()
        self.repo.set_setting("broker_holdings", data)
        return data

    def codes(self, raw: Optional[Dict[str, Any]] = None) -> List[str]:
        raw = raw if raw is not None else self.raw()
        codes = []
        for row in raw.get("positions") or []:
            info = parse_code(row["code"])
            codes += [info["ticker"]] + ([info["underlying"]] if info["kind"] == "option" else [])
        return list(dict.fromkeys(codes))

    def tickers(self) -> List[str]:
        """Held stocks and option underlyings."""
        return [code for code in self.codes() if parse_code(code)["kind"] == "stock"]

    def view(self, *, live: bool = True, now: Optional[datetime] = None) -> Dict[str, Any]:
        raw = self.raw()
        quotes = {}
        if live and raw.get("positions"):
            codes = self.codes(raw)
            try:
                provider = self.service.provider("live")
            except Exception as exc:  # broker prices from the last sync remain
                logger.info("Holdings quotes unavailable: %s", type(exc).__name__)
                provider = None
            if provider is not None:
                try:
                    quotes = provider.watchlist_quotes(codes)
                except Exception as exc:
                    # One unquotable code must not blank every price: retry stocks and options apart.
                    logger.info("Holdings quotes unavailable: %s", type(exc).__name__)
                    for part in ([c for c in codes if parse_code(c)["kind"] == "stock"],
                                 [c for c in codes if parse_code(c)["kind"] == "option"]):
                        try:
                            quotes.update(provider.watchlist_quotes(part) if part else {})
                        except Exception:
                            pass  # broker prices from the last sync remain
        return build_view(raw, quotes, _local(now or utcnow()).date())

    def note(self, ticker: str, view: Optional[Dict[str, Any]] = None) -> str:
        """One Discord-safe line about what you hold in this ticker, or ''."""
        view = view or self.view(live=False)
        parts = [describe_stock(row) for row in view["stocks"] if row["ticker"] == ticker]
        parts += [describe_option(row) for row in view["options"] if row["underlying"] == ticker]
        return "You hold: " + "; ".join(parts) if parts else ""

    def side(self, ticker: str, view: Optional[Dict[str, Any]] = None) -> str:
        """Net direction of what you hold in a ticker: "long", "short", "mixed", or "" when not held.

        Shares count by sign; options by their expiry payoff across the strikes (a bull call
        spread or short puts are long, long puts or a bear spread are short).
        """
        view = view or self.view(live=False)
        held, exposure = False, 0.0
        for row in view["stocks"]:
            if row["ticker"] == ticker:
                held, exposure = True, exposure + (1 if row["qty"] > 0 else -1)
        for row in view["options"]:
            if row["underlying"] != ticker or row.get("expired"):
                continue
            held = True
            strikes = [leg["strike"] for leg in row["legs"]]
            low, high = min(strikes) * 0.9, max(strikes) * 1.1
            slope = _intrinsic(row["legs"], high) - _intrinsic(row["legs"], low)
            exposure += 1 if slope > 0 else -1 if slope < 0 else 0
        if not held:
            return ""
        return "long" if exposure > 0 else "short" if exposure < 0 else "mixed"

    def weight(self, ticker: str, view: Optional[Dict[str, Any]] = None) -> Optional[float]:
        view = view or self.view(live=False)
        rows = [row for row in view["stocks"] if row["ticker"] == ticker]
        return rows[0]["weight_pct"] if rows else None

    def summary(self, now: Optional[datetime] = None, *,
                bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                earnings_date: Callable[[str, date], Optional[date]] = earnings.next_earnings) -> Dict[str, Any]:
        """Build (not send) the portfolio summary from the current snapshot and quotes."""
        from .portfolio import build_summary, format_summary
        now = now or utcnow()
        view = self.view(now=now)
        tickers = [row["ticker"] for row in view["stocks"]]
        history = bars(list(dict.fromkeys(["SPY", *tickers]))) if tickers else {}
        summary = build_summary(view, self.raw(), history, self.rules(), _local(now).date(),
                                earnings_date=earnings_date)
        summary["message"] = format_summary(summary)
        summary["built_at"] = utcnow().isoformat()
        self.repo.set_setting("portfolio_summary", summary)
        return summary

    def last_summary(self) -> Optional[Dict[str, Any]]:
        return self.repo.setting("portfolio_summary", None)

    # rules ----------------------------------------------------------------
    def rules(self) -> List[Dict[str, Any]]:
        return self.repo.setting("holding_rules", [])

    def _save_rules(self, rules: List[Dict[str, Any]]) -> None:
        self.repo.set_setting("holding_rules", rules)

    def position(self, key: Optional[str], view: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        view = view or self.view(live=False)
        return next((row for row in [*view["stocks"], *view["options"]] if row["key"] == key), None)

    def add_rule(self, body: Dict[str, Any]) -> Dict[str, Any]:
        view = self.view(live=False)
        position = self.position(body.get("position_key"), view)
        if body.get("position_key") and position is None:
            raise ValueError("That position is no longer held")
        ticker = (position or {}).get("underlying") or (position or {}).get("ticker") or str(body.get("ticker") or "").upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", ticker or ""):
            raise ValueError("Choose a held position or a US ticker")
        checked = validate_rule(body, position)
        rule = {"id": identity(), "ticker": ticker, "position_key": body.get("position_key") or None,
                "position_label": describe_option(position) if position and "expiry" in position
                else (f"{ticker} shares" if position else ""),
                **checked, "note": str(body.get("note") or "")[:200],
                "repeat": "daily" if body.get("repeat") == "daily" else "once",
                "status": "active", "arm": 0, "created_at": utcnow().isoformat(), "triggered_at": None}
        live = self.view()
        price = _prices(live).get(ticker)
        if price is None and rule["kind"].startswith("price"):
            try:  # a ticker you do not hold
                price = (self.service.provider("live").watchlist_quotes([ticker]).get(ticker) or {}).get("price")
            except Exception:
                price = None
        now_value = rule_fires(rule, self.position(rule["position_key"], live), price)
        warning = (f"This already holds (now {now_value}); it will fire at the next check in the session."
                   if now_value is not None else None)
        with self._lock:
            rules = self.rules()
            if len(rules) >= MAX_RULES:
                raise ValueError(f"At most {MAX_RULES} alerts")
            self._save_rules([*rules, rule])
        return {**rule, "warning": warning} if warning else rule

    def update_rule(self, rule_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            rules = self.rules()
            rule = next((item for item in rules if item["id"] == rule_id), None)
            if rule is None:
                raise KeyError(rule_id)
            rearm = False
            if "status" in changes:
                if changes["status"] not in {"active", "paused"}:
                    raise ValueError("Status must be active or paused")
                rearm = changes["status"] == "active" and rule["status"] != "active"
                rule["status"] = changes["status"]
            if "value" in changes:
                rule.update(validate_rule({**rule, "value": changes["value"]}, None, check_position=False))
                rearm = rearm or rule["status"] != "paused"  # a new level is a new alert, daily ones too
                if rule["status"] == "triggered":
                    rule["status"] = "active"
            if rearm:
                # A new arm number gives the alert a fresh dedup key, so it can send again.
                rule["arm"] = int(rule.get("arm") or 0) + 1
                rule["triggered_at"] = None
            if "note" in changes:
                rule["note"] = str(changes["note"] or "")[:200]
            self._save_rules(rules)
            return rule

    def delete_rule(self, rule_id: str) -> None:
        with self._lock:
            rules = self.rules()
            if not any(item["id"] == rule_id for item in rules):
                raise KeyError(rule_id)
            self._save_rules([item for item in rules if item["id"] != rule_id])

    def mark_triggered(self, rule_id: str, when: str, once: bool) -> None:
        with self._lock:
            rules = self.rules()
            for item in rules:
                if item["id"] == rule_id:
                    item["triggered_at"] = when
                    if once:
                        item["status"] = "triggered"
            self._save_rules(rules)


def _prices(view: Dict[str, Any]) -> Dict[str, float]:
    prices = {row["ticker"]: row["price"] for row in view["stocks"] if row.get("price")}
    prices.update({row["underlying"]: row["underlying_price"] for row in view["options"] if row.get("underlying_price")})
    return prices


def rule_fires(rule: Dict[str, Any], position: Optional[Dict[str, Any]], price: Optional[float]) -> Optional[str]:
    """The observed value when the rule's condition holds, else None."""
    kind, value = rule["kind"], rule["value"]
    if kind in {"price_below", "price_above"}:
        if price is None:
            return None
        hit = price <= value if kind == "price_below" else price >= value
        return f"{price:.2f}" if hit else None
    if position is None:
        return None
    if kind == "days_to_expiry":
        return f"{position['days_left']} trading days left" if position.get("days_left", 99) <= value else None
    pnl = position.get("pnl_pct")
    if pnl is None:
        return None
    hit = pnl <= value if kind == "pnl_below" else pnl >= value
    return f"{pnl:+.1f}% on cost" if hit else None


class HoldingsMonitor:
    def __init__(self, holdings: Holdings, emit: Callable[[str, Dict[str, Any], str], Any], *,
                 bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                 earnings_date: Callable[[str, date], Optional[date]] = earnings.next_earnings,
                 clock: Optional[Callable[[], float]] = None):
        import time
        self.holdings = holdings
        self._emit = emit
        self._bars = bars
        self._earnings_date = earnings_date
        self._clock = clock or time.monotonic
        self._next_sync = 0.0
        self._next_quotes = 0.0
        self._after_close_day: Optional[date] = None
        self._summary_day: Optional[date] = None
        self._levels_day: Optional[date] = None  # set once a load succeeds
        self._levels_for: Optional[date] = None
        self._levels_retry_at = 0.0
        self._levels: Dict[str, Dict[str, float]] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="holdings")
        self._tasks: Dict[str, Any] = {}
        self.errors: Dict[str, str] = {}
        self.error_at: Dict[str, str] = {}

    @property
    def last_error(self) -> Optional[str]:
        """The broker sync's error; other tasks succeeding does not hide it."""
        return self.errors.get("sync")

    def stop(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, name: str, function, *args) -> bool:
        """Queue a background task unless the same one is still pending; True when queued."""
        task = self._tasks.get(name)
        if task is None or task.done():
            self._tasks[name] = self._pool.submit(self._safe, name, function, *args)
            return True
        return False

    def _safe(self, name: str, function, *args):
        try:
            function(*args)
            self.errors.pop(name, None)
        except Exception as exc:  # the next tick retries; alerts keep using the last snapshot
            self.errors[name] = type(exc).__name__
            self.error_at[name] = utcnow().isoformat()
            logger.warning("Holdings %s failed: %s", name, type(exc).__name__)

    def _load_levels(self, day: date) -> None:
        tickers = self.holdings.tickers()
        levels = {}
        self._warm_earnings(day)
        from .opportunities import breakout_levels
        for ticker, rows in self._bars(tickers).items():
            found = breakout_levels(rows, day)
            if found:
                history = [row for row in rows if row["date"] < day.isoformat()]
                levels[ticker] = {**found, "last_close": history[-1]["close"]}
        stock_tickers = [row["ticker"] for row in self.holdings.view(live=False)["stocks"]]
        if stock_tickers and not levels:
            raise RuntimeError("no daily bars for held stocks")  # retried in a few minutes
        self._levels, self._levels_day = levels, day

    def _warm_earnings(self, day: date) -> None:
        tickers = [t for t in self.holdings.tickers() if not self._geared(t)]
        earnings.many(tickers, day, lookup=self._earnings_date)

    def tick(self, now: datetime, session: str) -> None:
        local = _local(now)
        day = local.date()
        clock = self._clock()
        if session == "regular":
            if clock >= self._next_sync:
                self._next_sync = clock + SYNC_SECONDS
                self._run("sync", self.holdings.sync)
                return
        elif local.time() >= dtime(16, 5) and self._after_close_day != day and _trading_day(day):
            if self._run("sync", self.holdings.sync):  # a still-running session sync is not the close
                self._after_close_day = day
            return
        elif (local.time() >= SUMMARY_AT and self._summary_day != day and _trading_day(day)
              and self._after_close_day == day and self._tasks.get("sync") is not None
              and self._tasks["sync"].done()):
            self._summary_day = day
            self._run("summary", self._daily_summary, now)
            return
        elif not self.holdings.raw():
            if clock >= self._next_sync:
                self._next_sync = clock + SYNC_SECONDS
                self._run("sync", self.holdings.sync)
            return
        if session != "regular" or clock < self._next_quotes:
            return
        if self._levels_day != day:
            if self._levels_for != day:
                self._levels_for, self._levels = day, {}  # yesterday's levels never judge today
            if clock >= self._levels_retry_at and self._run("levels", self._load_levels, day):
                self._levels_retry_at = clock + LEVELS_RETRY_SECONDS
        self._next_quotes = clock + QUOTE_SECONDS
        self.check(now)

    def _daily_summary(self, now: datetime) -> None:
        summary = self.holdings.summary(now, bars=self._bars, earnings_date=self._earnings_date)
        # The day's dedup key stops a second send after a restart.
        self._emit("portfolio_summary", {"underlying": "", "message": summary["message"]},
                   f"portfolio:{summary['date']}")

    # alerts ------------------------------------------------------------------
    def _alert(self, kind: str, ticker: str, message: str, key: str) -> Any:
        return self._emit("holding_alert", {"underlying": ticker, "kind": kind,
                                            "message": message + "\nReview in moomoo — nothing is traded automatically."},
                          key)

    def check(self, now: datetime) -> None:
        local = _local(now)
        day = local.date()
        view = self.holdings.view(now=now)
        prices = _prices(view)
        extra = sorted({rule["ticker"] for rule in self.holdings.rules()
                        if rule["status"] == "active" and rule["ticker"] not in prices})
        if extra:  # alerts on tickers you do not hold
            try:
                quotes = self.holdings.service.provider("live").watchlist_quotes(extra)
                prices.update({ticker: quote.get("price") for ticker, quote in quotes.items()})
            except Exception as exc:
                logger.info("Alert quotes unavailable: %s", type(exc).__name__)
        self._rules(view, prices, now)
        from src.core.trading_calendar import get_market_session_bounds
        try:
            closing = get_market_session_bounds("us", now)[1]
        except Exception:
            closing = None
        last_hour = (now >= closing - timedelta(hours=1)) if closing is not None else local.time() >= LAST_HOUR
        morning = local.time() >= MORNING
        for position in view["options"]:
            self._option_defaults(position, day, morning, last_hour)
        for position in view["stocks"]:
            self._stock_defaults(position, day, morning)

    def _rules(self, view, prices, now):
        day = _local(now).date().isoformat()
        for rule in self.holdings.rules():
            if rule["status"] != "active":
                continue
            if rule["repeat"] == "daily" and (rule.get("triggered_at") or "")[:10] == day:
                continue
            position = self.holdings.position(rule.get("position_key"), view)
            if rule.get("position_key") and position is None:
                continue  # the position was closed; the rule waits in the list
            if position is not None and position.get("expired"):
                continue  # expired contracts the broker still lists
            if rule["kind"] == "days_to_expiry" and _local(now).time() < MORNING:
                continue
            observed = rule_fires(rule, position, prices.get(rule["ticker"]))
            if observed is None:
                continue
            context = (describe_option(position) if position and "expiry" in position
                       else describe_stock(position) if position else "")
            message = (f"Your alert: {rule['ticker']} {RULE_KINDS[rule['kind']]} "
                       f"{rule['value']:g}{'%' if rule['kind'].startswith('pnl') else ''} — now {observed}."
                       + (f"\nPosition: {context}" if context else "")
                       + (f"\nNote: {discord_safe(rule['note'])}" if rule.get("note") else ""))
            key = f"rule:{rule['id']}:{rule.get('arm') or 0}:{day if rule['repeat'] == 'daily' else 'once'}"
            self._alert("rule", rule["ticker"], message, key)
            self.holdings.mark_triggered(rule["id"], utcnow().isoformat(), rule["repeat"] == "once")

    def _option_defaults(self, position, day, morning, last_hour):
        if position.get("expired"):
            return  # the broker still lists it after expiry; nothing left to warn about
        # The legs' signature keeps a rolled position on the same expiry from inheriting old alerts.
        key, ticker, days = f"{position['key']}:{position.get('signature', '')}", position["underlying"], position["days_left"]
        text = describe_option(position)
        brief = describe_option(position, with_days=False)
        where = moneyness(position)
        if morning and days in EXPIRY_WARN_DAYS:
            self._alert("expiry", ticker, f"Expires in {days} trading day{'s' if days != 1 else ''}: {brief}"
                        + (f"\n{where}" if where else ""), f"hold-expiry:{key}:{days}")
        if days == 0 and morning:
            self._alert("expiry", ticker, f"Expires today: {brief}" + (f"\n{where}" if where else ""),
                        f"hold-expiry:{key}:0")
        if days == 0 and last_hour:
            self._alert("expiry", ticker, f"One hour to the close on expiration day: {brief}"
                        + (f"\n{where}" if where else ""), f"hold-expiry:{key}:last")
        spot = position.get("underlying_price")
        if spot and days <= ASSIGNMENT_DAYS:
            for leg in position["legs"]:
                itm = spot > leg["strike"] if leg["right"] == "call" else spot < leg["strike"]
                if leg["qty"] < 0 and itm:
                    self._alert("assignment", ticker,
                                f"Assignment risk: short {leg['strike']:g}{'C' if leg['right'] == 'call' else 'P'} is in "
                                f"the money ({ticker} {spot:.2f}). {text}",
                                f"hold-assign:{key}:{leg['code']}:{day.isoformat()}")
        if position.get("pct_of_max") is not None:
            if position["pct_of_max"] >= PROFIT_OF_MAX:
                self._alert("profit", ticker, f"Profit target: {text}", f"hold-profit:{key}:{PROFIT_OF_MAX:g}")
        elif position.get("pnl_pct") is not None:
            for level in PROFIT_ON_COST:
                if position["pnl_pct"] >= level:
                    self._alert("profit", ticker, f"Up {level:g}% on cost: {text}", f"hold-profit:{key}:{level:g}")
        if position.get("pnl_pct") is not None and position["pnl_pct"] <= LOSS_ON_COST:
            self._alert("loss", ticker, f"Down {abs(LOSS_ON_COST):g}% on cost: {text}", f"hold-loss:{key}:{LOSS_ON_COST:g}")
        when = self._earnings(ticker, day)
        if when and when <= date.fromisoformat(position["expiry"]):
            self._alert("earnings", ticker, f"Earnings {when:%b} {when.day} fall before this expiry: {text}",
                        f"hold-earnings:{key}:{when.isoformat()}")

    def _stock_defaults(self, position, day, morning):
        ticker, price = position["ticker"], position.get("price")
        levels = self._levels.get(ticker)
        text = describe_stock(position)
        if levels and price and position["qty"] > 0:
            if price < levels["low20"]:
                self._alert("trend", ticker, f"Broke below its 20-day low {levels['low20']:.2f} at {price:.2f}: {text}",
                            f"hold-low20:{ticker}:{day.isoformat()}")
            if levels["last_close"] >= levels["ma50"] > price:
                self._alert("trend", ticker, f"Fell below its 50-day average {levels['ma50']:.2f} at {price:.2f}: {text}",
                            f"hold-ma50:{ticker}:{day.isoformat()}")
        if morning:
            when = self._earnings(ticker, day)
            if when and (when - day).days <= STOCK_EARNINGS_DAYS:
                self._alert("earnings", ticker, f"{earnings.note(when, day)}. {text}",
                            f"hold-earnings:{ticker}:{when.isoformat()}")

    def _geared(self, ticker: str) -> bool:
        from .opportunities import geared_fund
        names = [row.get("name", "") for row in self.holdings.raw().get("positions") or []
                 if parse_code(row["code"])["ticker"] == ticker]
        return bool(names) and geared_fund(names[0])

    def _earnings(self, ticker: str, day: date) -> Optional[date]:
        """Cached dates only: the monitor loop never waits on Yahoo; misses are warmed in the background."""
        if self._geared(ticker):
            return None
        if self._earnings_date is earnings.next_earnings:
            hit, when = earnings.peek(ticker, day)
            if not hit:
                self._run("earnings", self._warm_earnings, day)
            return when
        try:
            return self._earnings_date(ticker, day)
        except Exception:
            return None
