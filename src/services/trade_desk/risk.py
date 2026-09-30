"""Whole-portfolio risk from your broker holdings: delta, time decay, beta and index moves.

Read-only estimates for the Holdings page and the daily summary:

- Each option leg's implied volatility is backed out of its mark (Black-Scholes, European;
  American early exercise is ignored), then delta (share-equivalents) and the value change over
  one day at today's price (theta) follow from it. A leg whose IV cannot be solved (no mark, a
  mark below intrinsic) counts at its intrinsic delta with no theta.
- Beta to SPY and QQQ comes from a year of daily returns (at least 60 shared days; otherwise 1.0,
  flagged). Leveraged and inverse funds get their real, large or negative, betas this way.
- A scenario moves each underlying by beta × the index move and re-prices the options at the
  moved price with the same IV and time (an instant move): SPY ±3%, QQQ ±5%.

Nothing here trades; percentages of total assets are what the Discord summary shows.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
SCENARIOS = (("SPY", -3.0), ("SPY", 3.0), ("QQQ", -5.0), ("QQQ", 5.0))
MIN_BETA_DAYS = 60
_beta_cache: Dict[str, tuple] = {}  # "YYYY-MM-DD" -> betas for that day's tickers
_lock = threading.Lock()


def _years_to(expiry: Any, now: datetime) -> float:
    """Years until the 16:00 New York close on the expiry date."""
    day = expiry if not isinstance(expiry, str) else datetime.fromisoformat(expiry).date()
    close = datetime.combine(day, dtime(16, 0), tzinfo=_NEW_YORK)
    return max(0.0, (close - now).total_seconds() / (365.0 * 86400))


def _bs(spot: float, strike: float, years: float, vol: float, right: str, rate: float = 0.0) -> float:
    from .analytics import black_scholes_price
    return black_scholes_price(spot, strike, years, vol, rate, 0.0, right)


def implied_vol(price: float, spot: float, strike: float, years: float, right: str, rate: float = 0.0) -> Optional[float]:
    """Black-Scholes IV for a mark by bisection; None when no volatility reproduces it."""
    if not price or price <= 0 or spot <= 0 or years <= 0:
        return None
    low, high = 1e-4, 6.0
    if not _bs(spot, strike, years, low, right, rate) <= price <= _bs(spot, strike, years, high, right, rate):
        return None
    for _ in range(80):
        mid = (low + high) / 2
        if _bs(spot, strike, years, mid, right, rate) < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def leg_risk(leg: Dict[str, Any], spot: float, now: datetime) -> Dict[str, Any]:
    """One leg's IV, delta (per share of the underlying, signed by the leg's size) and theta per day ($)."""
    size = float(leg["qty"]) * 100
    years = _years_to(leg["expiry"], now)
    iv = implied_vol(leg.get("mark") or 0, spot, float(leg["strike"]), years, leg["right"]) if spot else None
    if iv is None:
        itm = spot > leg["strike"] if leg["right"] == "call" else spot < leg["strike"]
        delta = (1.0 if leg["right"] == "call" else -1.0) if itm else 0.0
        return {"iv": None, "delta": size * delta, "theta": None}
    bump = spot * 0.001
    delta = (_bs(spot + bump, leg["strike"], years, iv, leg["right"]) - _bs(spot - bump, leg["strike"], years, iv, leg["right"])) / (2 * bump)
    day = 1 / 365.0
    theta = (_bs(spot, leg["strike"], max(0.0, years - day), iv, leg["right"]) - _bs(spot, leg["strike"], years, iv, leg["right"]))
    return {"iv": iv, "delta": size * delta, "theta": size * theta}


def betas(bars: Dict[str, List[Dict[str, Any]]], tickers: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """Beta of each ticker's daily returns to SPY and QQQ; None without enough shared history."""
    def returns(rows):
        closes = {str(row["date"])[:10]: float(row["close"]) for row in rows or [] if row.get("close")}
        days = sorted(closes)
        return {day: closes[day] / closes[prev] - 1 for prev, day in zip(days, days[1:]) if closes[prev] > 0}
    index = {name: returns(bars.get(name)) for name in ("SPY", "QQQ")}
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for ticker in tickers:
        mine = returns(bars.get(ticker) or bars.get(ticker.replace("-", ".")))
        out[ticker] = {}
        for name, market in index.items():
            shared = [day for day in mine if day in market]
            if len(shared) < MIN_BETA_DAYS:
                out[ticker][name] = None
                continue
            xs, ys = [market[day] for day in shared], [mine[day] for day in shared]
            mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
            var = sum((x - mean_x) ** 2 for x in xs)
            out[ticker][name] = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var) if var > 0 else None
    return out


def _cached_betas(tickers: List[str], download: Callable[..., Dict[str, List[Dict[str, Any]]]], day: str) -> Dict[str, Dict[str, Optional[float]]]:
    """Betas for the day; only measured ones are kept, so a failed or partial download is retried
    (an assumed 1.0 would flip the sign of an inverse fund for the rest of the day)."""
    with _lock:
        known = dict(_beta_cache.get(day, ({}, {}))[1]) if day in _beta_cache else {}
    missing = [ticker for ticker in tickers if ticker not in known]
    if missing:
        bars = download(list(dict.fromkeys([*missing, "SPY", "QQQ"])), period="1y")
        measured = betas(bars, missing) if bars.get("SPY") and bars.get("QQQ") else {t: {} for t in missing}
        with _lock:
            kept = {ticker: value for ticker, value in measured.items() if value.get("SPY") is not None}
            _beta_cache.clear()
            _beta_cache[day] = (set(), {**known, **kept})
        known = {**known, **measured}
    return {ticker: known.get(ticker, {}) for ticker in tickers}


def portfolio_risk(view: Dict[str, Any], *, download: Optional[Callable[..., Dict[str, List[Dict[str, Any]]]]] = None,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Per-underlying delta, theta and beta, and the P&L of each index scenario (dollars and % of the account)."""
    from . import trend
    now = now or datetime.now(timezone.utc)
    download = download or trend.download_bars
    rows: Dict[str, Dict[str, Any]] = {}

    def row(ticker: str, price: Optional[float]) -> Dict[str, Any]:
        return rows.setdefault(ticker, {"ticker": ticker, "price": price, "shares_equiv": 0.0, "theta_per_day": 0.0,
                                        "stock_qty": 0.0, "legs": [], "theta_known": True})
    for stock in view.get("stocks") or []:
        entry = row(stock["ticker"], stock.get("price"))
        entry["shares_equiv"] += float(stock["qty"])
        entry["stock_qty"] += float(stock["qty"])
    for position in view.get("options") or []:
        if position.get("expired"):
            continue
        spot = position.get("underlying_price")
        entry = row(position["underlying"], spot)
        entry["price"] = entry["price"] or spot
        for leg in position["legs"]:
            measured = leg_risk({**leg, "expiry": position["expiry"]}, spot or 0.0, now) if spot else {"iv": None, "delta": 0.0, "theta": None}
            entry["shares_equiv"] += measured["delta"]
            if measured["theta"] is None:
                entry["theta_known"] = False
            else:
                entry["theta_per_day"] += measured["theta"]
            entry["legs"].append({**leg, "expiry": position["expiry"], "iv": measured["iv"]})
    tickers = sorted(rows)
    try:
        beta = _cached_betas(tickers, download, now.astimezone(_NEW_YORK).date().isoformat()) if tickers else {}
    except Exception as exc:  # no history: beta 1.0, flagged
        logger.info("Portfolio betas unavailable: %s", type(exc).__name__)
        beta = {}
    total_assets = view.get("total_assets") or 0
    scenarios = {f"{name}{move:+g}": 0.0 for name, move in SCENARIOS}
    out_rows = []
    for ticker in tickers:
        entry = rows[ticker]
        price = entry["price"] or 0.0
        betas_here = beta.get(ticker) or {}
        for name, move in SCENARIOS:
            b = betas_here.get(name)
            moved = price * (1 + (1.0 if b is None else b) * move / 100)
            pnl = entry["stock_qty"] * (moved - price)
            for leg in entry["legs"]:
                years = _years_to(leg["expiry"], now)
                if leg["iv"] is not None and price > 0:
                    pnl += float(leg["qty"]) * 100 * (_bs(moved, leg["strike"], years, leg["iv"], leg["right"])
                                                      - _bs(price, leg["strike"], years, leg["iv"], leg["right"]))
                else:  # intrinsic change only
                    intrinsic = (lambda s: max(0.0, s - leg["strike"]) if leg["right"] == "call" else max(0.0, leg["strike"] - s))
                    pnl += float(leg["qty"]) * 100 * (intrinsic(moved) - intrinsic(price))
            scenarios[f"{name}{move:+g}"] += pnl
        out_rows.append({"ticker": ticker, "price": price or None, "shares_equiv": round(entry["shares_equiv"], 2),
                         "delta_dollars": round(entry["shares_equiv"] * price, 2) if price else None,
                         "theta_per_day": round(entry["theta_per_day"], 2) if entry["legs"] else 0.0,
                         "theta_partial": not entry["theta_known"],
                         "beta_spy": None if betas_here.get("SPY") is None else round(betas_here["SPY"], 2),
                         "beta_qqq": None if betas_here.get("QQQ") is None else round(betas_here["QQQ"], 2),
                         "beta_assumed": betas_here.get("SPY") is None})
    out_rows.sort(key=lambda item: -abs(item["delta_dollars"] or 0))
    theta = sum(item["theta_per_day"] for item in out_rows)
    beta_dollars = sum((item["delta_dollars"] or 0) * (item["beta_spy"] if item["beta_spy"] is not None else 1.0)
                       for item in out_rows)

    def pct(value: float) -> Optional[float]:
        return round(value / total_assets * 100, 2) if total_assets else None
    return {
        "as_of": now.isoformat(), "rows": out_rows,
        "totals": {"delta_dollars": round(sum(item["delta_dollars"] or 0 for item in out_rows), 2),
                   "spy_beta_dollars": round(beta_dollars, 2), "spy_beta_pct": pct(beta_dollars),
                   "theta_per_day": round(theta, 2), "theta_pct": pct(theta)},
        "scenarios": [{"key": f"{name}{move:+g}", "label": f"{name} {move:+g}%", "pnl": round(scenarios[f"{name}{move:+g}"], 2),
                       "pct": pct(scenarios[f"{name}{move:+g}"])} for name, move in SCENARIOS],
    }


def summary_line(risk: Dict[str, Any]) -> str:
    """Percent-only risk line for Discord: index moves and time decay as a share of the account."""
    parts = [f"{item['label']} ≈ {item['pct']:+.1f}%" for item in risk["scenarios"] if item["pct"] is not None
             and item["key"] in ("SPY-3", "QQQ-5")]
    theta = risk["totals"].get("theta_pct")
    if theta is not None and abs(theta) >= 0.005:
        parts.append(f"time decay ≈ {theta:+.2f}%/day")
    return ("Risk (estimate): if " + " · ".join(parts)) if parts else ""
