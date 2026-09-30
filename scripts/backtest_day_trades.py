#!/usr/bin/env python3
"""Backtest day-trading rules on intraday bars (see src/services/trade_desk/backtest/intraday.py).

    python scripts/backtest_day_trades.py --out reports/backtests

5-minute bars cover Yahoo's last 60 days (S&P 100, the STOCK_LIST watchlist, ETFs);
hourly bars cover about two years for the intraday-momentum rule. Rules are fixed in
the module; nothing is tuned on the results. Needs network (Wikipedia, Yahoo Finance).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.trade_desk.backtest import intraday as ib  # noqa: E402
from src.services.trade_desk import trend  # noqa: E402

logger = logging.getLogger("day-backtest")
ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU",
        "SMH", "XBI", "KRE", "TLT", "GLD", "SLV", "USO", "EEM", "TQQQ", "SQQQ", "SOXL", "SOXS"]


def sp100():
    import pandas as pd
    request = Request("https://en.wikipedia.org/wiki/S%26P_100", headers={"User-Agent": "daily-stock-analysis-backtest/1.0"})
    with urlopen(request, timeout=30) as response:
        tables = pd.read_html(StringIO(response.read().decode("utf-8")))
    table = next(t for t in tables if "Symbol" in t.columns)
    return [str(s).replace(".", "-") for s in table["Symbol"].dropna()]


def watchlist():
    """US tickers from STOCK_LIST (environment first, then .env)."""
    raw = os.environ.get("STOCK_LIST", "")
    env = Path(__file__).resolve().parents[1] / ".env"
    if not raw and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("STOCK_LIST="):
                raw = line.split("=", 1)[1]
    return [t.strip().upper() for t in raw.split(",") if t.strip().isalpha()]


def intraday(tickers, period, interval, chunk=50):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                data = yf.download(part, period=period, interval=interval, group_by="ticker", auto_adjust=False,
                                   prepost=False, progress=False, threads=True, timeout=30)
                break
            except Exception as exc:
                logger.warning("download failed (%s), retrying", type(exc).__name__)
                time.sleep(5 * (attempt + 1))
        else:
            continue
        for symbol in part:
            try:
                frame = data[symbol] if len(part) > 1 else data
                frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
            except (KeyError, TypeError):
                continue
            bars = [ib.to_bar(idx.to_pydatetime(), r["Open"], r["High"], r["Low"], r["Close"], r["Volume"] or 0)
                    for idx, r in frame.iterrows() if float(r["Low"]) > 0]
            days = ib.sessions(bars)
            if len(days) >= 5:
                out[symbol] = days
        logger.info("%s bars: %d/%d chunks, %d tickers", interval, i // chunk + 1, math.ceil(len(tickers) / chunk), len(out))
    return out


def compare(trades):
    """Per strategy: the rule, its random twins, and the day-clustered difference."""
    groups = defaultdict(lambda: {"rule": [], "twin": []})
    for trade in trades:
        name = trade.strategy.split(":")[-1]
        groups[name]["twin" if trade.strategy.startswith("random:") else "rule"].append(trade)
    out = {}
    for name in ib.STRATEGIES:
        rule, twin = groups[name]["rule"], groups[name]["twin"]
        if not rule:
            continue
        # One number per day (rule minus twin, averaged over that day's trades): same-day trades are correlated.
        by_day = defaultdict(lambda: {"rule": [], "twin": []})
        for t in rule:
            by_day[t.day]["rule"].append(t.return_pct)
        for t in twin:
            by_day[t.signal_day]["twin"].append(t.return_pct)
        diffs = [sum(v["rule"]) / len(v["rule"]) - sum(v["twin"]) / len(v["twin"])
                 for v in by_day.values() if v["rule"] and v["twin"]]
        mean = sum(diffs) / len(diffs) if diffs else 0.0
        sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)) if len(diffs) > 1 else 0.0
        by_side = {side: ib.summarize([t for t in rule if t.direction == side]) for side in ("long", "short")}
        out[name] = {"rule": ib.summarize(rule), "twin": ib.summarize(twin), "by_side": by_side,
                     "vs_twin_pct": mean, "vs_twin_t_by_day": mean / (sd / math.sqrt(len(diffs))) if sd else 0.0,
                     "days": len(diffs)}
    return out


def fmt(row):
    rule, twin = row["rule"], row["twin"]
    return (f"| {rule['trades']} | {rule['win_rate']:.0f}% | {rule['avg_gross_pct']:+.3f}% | {rule['avg_net_pct']:+.3f}% "
            f"| {twin.get('avg_net_pct', 0):+.3f}% | {row['vs_twin_pct']:+.3f}% | {row['vs_twin_t_by_day']:+.1f} |")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/backtests")
    parser.add_argument("--limit", type=int, default=0, help="first N tickers only (smoke runs)")
    args = parser.parse_args()
    from src.utils.yfinance_cache import use_memory_tz_cache
    use_memory_tz_cache()

    groups = {"S&P 100": sp100(), "Watchlist": watchlist(), "ETFs": ETFS}
    universe = sorted({t for tickers in groups.values() for t in tickers})
    if args.limit:
        universe = universe[:args.limit]
    five = intraday(universe, "60d", "5m")
    daily = trend.download_bars(list(five), period="1y")
    levels = {t: {d: ib.levels_for(daily.get(t) or [], d) for d in days} for t, days in five.items()}
    hourly = intraday(sorted(set(groups["S&P 100"]) | set(ETFS)) if not args.limit else universe, "730d", "1h")

    trades = ib.run(five, levels)
    first = min(d for days in five.values() for d in days)
    last = max(d for days in five.values() for d in days)
    results = {"generated": datetime.now().isoformat(timespec="seconds"), "cost_bps": ib.COST_BPS,
               "five_minute": {"from": first, "to": last, "tickers": len(five), "groups": {}},
               "hourly_momentum": {}}
    for label, tickers in [("All", list(five)), *groups.items()]:
        members = set(tickers)
        results["five_minute"]["groups"][label] = compare([t for t in trades if t.ticker in members])
    hourly_trades = ib.run(hourly, momentum_only=True)
    hours = [d for days in hourly.values() for d in days]
    results["hourly_momentum"] = {"from": min(hours) if hours else None, "to": max(hours) if hours else None,
                                  "tickers": len(hourly)}
    for label, members in (("S&P 100", set(groups["S&P 100"])), ("ETFs", set(ETFS))):
        results["hourly_momentum"][label] = compare([t for t in hourly_trades if t.ticker in members])

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"day_trades_{date.today().isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(results, indent=2, default=str))
    lines = [f"# Day-trading backtest ({date.today().isoformat()})", "",
             f"5-minute bars {first} → {last}, {len(five)} tickers; {ib.COST_BPS:g} bps per side. "
             "Twins: same ticker, side, entry time and exit rule on random other days. "
             "t is day-clustered (one rule-minus-twin number per day).", ""]
    header = "| Strategy | Trades | Win | Gross | Net | Twin net | vs twin | t (days) |\n|---|---|---|---|---|---|---|---|"
    for label, table in results["five_minute"]["groups"].items():
        lines += [f"## {label}", "", header]
        lines += [f"| {name} " + fmt(row) for name, row in table.items()]
        lines.append("")
    lines += [f"## Hourly bars: first hour → last half hour ({results['hourly_momentum']['from']} → "
              f"{results['hourly_momentum']['to']})", ""]
    for label in ("S&P 100", "ETFs"):
        lines += [f"### {label}", "", header]
        lines += [f"| {name} " + fmt(row) for name, row in results["hourly_momentum"][label].items()]
        lines.append("")
    stem.with_suffix(".md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
