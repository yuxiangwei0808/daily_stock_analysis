"""Daily portfolio summary for the broker holdings, after the close (percentages only on Discord).

Built from the post-close holdings snapshot, OpenD closing quotes and about
three months of daily bars (yfinance) for the held stocks and SPY:

- the account's day change (broker "today" P&L over the prior value),
  invested / cash / options shares of the account;
- the largest positions and the day's biggest movers;
- beta-weighted market exposure of the stock book (60-day beta to SPY;
  leveraged and inverse funds carry their own beta), options listed apart;
- the share in leveraged/inverse funds (daily reset: they decay when held);
- offsetting holdings (60-day daily-return correlation <= -0.6) and
  positions that move together (>= 0.85) above 5 % of the account each;
- option expiries within 5 trading days, earnings within 7 days, and your
  active / triggered alerts.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from . import earnings as earnings_mod

LOOKBACK = 60
OFFSET_CORR = -0.6
TOGETHER_CORR = 0.85
PAIR_MIN_WEIGHT = 5.0
CONCENTRATION = 25.0
EXPIRY_DAYS = 5
EARNINGS_DAYS = 7
TOP = 5


def _returns(bars: List[Dict[str, Any]]) -> Dict[str, float]:
    closes = [(bar["date"], bar["close"]) for bar in bars if bar.get("close")]
    return {day: close / prev - 1 for (_, prev), (day, close) in zip(closes, closes[1:]) if prev}


def _aligned(a: Dict[str, float], b: Dict[str, float]) -> tuple:
    days = sorted(set(a) & set(b))[-LOOKBACK:]
    return [a[d] for d in days], [b[d] for d in days]


def _corr(x: List[float], y: List[float]) -> Optional[float]:
    if len(x) < 20:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if not sx or not sy:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def _beta(x: List[float], market: List[float]) -> Optional[float]:
    if len(x) < 20:
        return None
    mx, mm = sum(x) / len(x), sum(market) / len(market)
    var = sum((v - mm) ** 2 for v in market)
    return sum((a - mx) * (b - mm) for a, b in zip(x, market)) / var if var else None


def build_summary(view: Dict[str, Any], raw: Dict[str, Any], bars: Dict[str, List[Dict[str, Any]]],
                  rules: List[Dict[str, Any]], today: date, *,
                  earnings_date: Callable[[str, date], Optional[date]] = earnings_mod.next_earnings,
                  geared: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
    from .opportunities import geared_fund
    geared = geared or geared_fund
    total = view.get("total_assets") or 0
    stocks, options = view["stocks"], view["options"]
    today_pl = sum(row.get("today_pl") or 0 for row in raw.get("positions") or [])
    prior = total - today_pl
    stock_value = sum(row["value"] or 0 for row in stocks)
    option_value = sum(row["value"] or 0 for row in options)
    share = (lambda value: value / total * 100) if total else (lambda value: None)

    market = _returns(bars.get("SPY") or [])
    returns = {row["ticker"]: _returns(bars.get(row["ticker"]) or []) for row in stocks}
    betas, beta_weight, covered, hedges = {}, 0.0, 0.0, []
    for row in stocks:
        x, m = _aligned(returns[row["ticker"]], market)
        beta = _beta(x, m)
        if beta is not None and row.get("weight_pct") is not None:
            betas[row["ticker"]] = beta
            contribution = (1 if row["qty"] > 0 else -1) * row["weight_pct"] / 100 * beta
            beta_weight += contribution
            covered += row["weight_pct"]
            if contribution <= -0.05:
                hedges.append({"ticker": row["ticker"], "contribution": round(contribution, 2)})
    geared_rows = [row for row in stocks if geared(row.get("name", ""))]
    pairs_offset, pairs_together = [], []
    big = [row for row in stocks if (row.get("weight_pct") or 0) >= PAIR_MIN_WEIGHT or geared(row.get("name", ""))]
    for i, a in enumerate(big):
        for b in big[i + 1:]:
            corr = _corr(*_aligned(returns[a["ticker"]], returns[b["ticker"]]))
            if corr is None:
                continue
            if corr <= OFFSET_CORR:
                pairs_offset.append((a["ticker"], b["ticker"], corr))
            elif corr >= TOGETHER_CORR and min(a.get("weight_pct") or 0, b.get("weight_pct") or 0) >= PAIR_MIN_WEIGHT:
                pairs_together.append((a["ticker"], b["ticker"], corr))
    movers = sorted((row for row in stocks if row.get("day_pct") is not None), key=lambda row: abs(row["day_pct"]),
                    reverse=True)[:3]
    upcoming = []
    for row in options:
        if row["days_left"] <= EXPIRY_DAYS:
            upcoming.append({"kind": "expiry", "ticker": row["underlying"], "days": row["days_left"],
                             "text": f"{row['underlying']} {row['expiry'][5:].replace('-', '/')} {row['label']} "
                                     f"expires in {row['days_left']} trading day{'s' if row['days_left'] != 1 else ''}"
                                     if row["days_left"] else f"{row['underlying']} {row['label']} expired today"})
    for ticker in dict.fromkeys([row["ticker"] for row in stocks if not geared(row.get("name", ""))]
                                + [row["underlying"] for row in options]):
        try:
            when = earnings_date(ticker, today)
        except Exception:
            when = None
        if when and (when - today).days <= EARNINGS_DAYS:
            upcoming.append({"kind": "earnings", "ticker": ticker, "days": (when - today).days,
                             "text": f"{ticker} earnings {when:%b} {when.day} (in {(when - today).days} days)"})
    top = sorted(stocks + options, key=lambda row: row.get("weight_pct") or 0, reverse=True)[:TOP]
    return {
        "date": today.isoformat(),
        "day_pct": today_pl / prior * 100 if prior > 0 else None,
        "invested_pct": share(stock_value), "options_pct": share(option_value),
        "cash_pct": share(raw.get("cash") or 0),
        "beta_exposure": beta_weight if covered else None, "beta_coverage_pct": covered,
        "geared_pct": sum(row.get("weight_pct") or 0 for row in geared_rows),
        "geared": [row["ticker"] for row in geared_rows],
        "hedges": hedges,
        "top": [{"label": f"{row['ticker']}" if "ticker" in row
                 else f"{row['underlying']} {row['expiry'][5:].replace('-', '/')} {row['label']}",
                 "weight_pct": row.get("weight_pct"), "pnl_pct": row.get("pnl_pct"), "day_pct": row.get("day_pct")}
                for row in top],
        "concentrated": [row["ticker"] for row in stocks if (row.get("weight_pct") or 0) >= CONCENTRATION],
        "movers": [{"ticker": row["ticker"], "day_pct": row["day_pct"], "weight_pct": row.get("weight_pct")}
                   for row in movers],
        "offsets": [{"a": a, "b": b, "corr": round(c, 2)} for a, b, c in pairs_offset],
        "together": [{"a": a, "b": b, "corr": round(c, 2)} for a, b, c in pairs_together],
        "betas": {ticker: round(beta, 2) for ticker, beta in betas.items()},
        "upcoming": sorted(upcoming, key=lambda item: item["days"]),
        "alerts_active": sum(1 for rule in rules if rule.get("status") == "active"),
        "alerts_triggered_today": sum(1 for rule in rules if _local_day(rule.get("triggered_at")) == today),
    }


def _local_day(stamp: Optional[str]) -> Optional[date]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(stamp).astimezone(ZoneInfo("America/New_York")).date() if stamp else None
    except ValueError:
        return None


def _pct(value: Optional[float], sign: bool = True) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:+.1f}%" if sign else f"{value:.1f}%"


def format_summary(summary: Dict[str, Any]) -> str:
    """Discord text: percentages and ratios only."""
    day = date.fromisoformat(summary["date"])
    lines = [f"📊 **Portfolio** · {day:%a %b} {day.day}",
             f"Account {_pct(summary['day_pct'])} today · stocks {_pct(summary['invested_pct'], False)} · "
             f"options {_pct(summary['options_pct'], False)} · cash {_pct(summary['cash_pct'], False)}"]
    if summary.get("beta_exposure") is not None:
        lines.append(f"Market exposure: a 1% SPY move ≈ {summary['beta_exposure']:+.1f}% on the account "
                     "(60-day betas of stocks/ETFs; options not included)")
    if summary["top"]:
        lines += ["", "**Largest**"] + [
            f"• {row['label']} {_pct(row['weight_pct'], False)} of account · {_pct(row['pnl_pct'])} on cost"
            + (f" · {_pct(row['day_pct'])} today" if row.get("day_pct") is not None else "") for row in summary["top"]]
    if summary["movers"]:
        lines.append("Movers today: " + ", ".join(f"{row['ticker']} {_pct(row['day_pct'])}" for row in summary["movers"]))
    notes = []
    if summary["concentrated"]:
        notes.append(f"⚠️ {', '.join(summary['concentrated'])} above 25% of the account")
    if summary["geared_pct"]:
        notes.append(f"Leveraged/inverse funds {_pct(summary['geared_pct'], False)} of the account "
                     f"({', '.join(summary['geared'])}) — daily reset, they lose value when held through chop")
    if summary.get("hedges"):
        notes.append("Hedging the book: " + ", ".join(
            f"{item['ticker']} {item['contribution'] * 100:+.0f}% of a 1% SPY move" for item in summary["hedges"]))
    notes += [f"{item['a']} and {item['b']} offset each other (correlation {item['corr']:+.2f})" for item in summary["offsets"]]
    notes += [f"{item['a']} and {item['b']} move together (correlation {item['corr']:+.2f})" for item in summary["together"]]
    if notes:
        lines += ["", "**Watch**"] + [f"• {note}" for note in notes]
    if summary["upcoming"]:
        lines += ["", "**Coming up**"] + [f"• {item['text']}" for item in summary["upcoming"]]
    lines += ["", f"Alerts: {summary['alerts_active']} active · {summary['alerts_triggered_today']} triggered today",
              "_Research only — nothing is traded._"]
    return "\n".join(lines)
