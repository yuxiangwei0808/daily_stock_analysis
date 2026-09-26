#!/usr/bin/env python3
"""Backtest the trade plans this system produces (see src/services/trade_desk/strategy_backtest.py).

    python scripts/backtest_trade_plans.py --start 2021-10-01 --split 2024-01-01 --out reports/backtests

Writes <out>/trade_plans_<today>.json and .md. Live parameters are used unchanged;
nothing here is tuned on the results. Needs network (Wikipedia, Yahoo Finance).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.trade_desk import strategy_backtest as bt  # noqa: E402

logger = logging.getLogger("backtest")
ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE",
        "XLC", "SMH", "XBI", "KRE", "TLT", "IEF", "GLD", "SLV", "USO", "UNG", "EEM", "EFA", "FXI", "HYG", "LQD"]


def _tables(title):
    import pandas as pd
    request = Request(f"https://en.wikipedia.org/wiki/{title}", headers={"User-Agent": "daily-stock-analysis-backtest/1.0"})
    with urlopen(request, timeout=30) as response:
        return pd.read_html(StringIO(response.read().decode("utf-8")))


def sp500_membership():
    """Today's list plus dated changes. Since 2026-08 Wikipedia keeps the changes on their own page."""
    import pandas as pd
    tables = _tables("List_of_S%26P_500_companies")
    current = [str(t).replace(".", "-") for t in tables[0]["Symbol"].dropna()]

    def flat(table):
        table.columns = [" ".join(str(part) for part in col).strip() if isinstance(col, tuple) else str(col)
                         for col in table.columns]
        return table

    candidates = [flat(t) for t in tables[1:] + _tables("Historical_components_of_the_S%26P_500")]
    changes_table = next(t for t in candidates if any("removed" in c.lower() for c in t.columns))
    date_col = next(c for c in changes_table.columns if "date" in c.lower())
    added_col = next(c for c in changes_table.columns if "added" in c.lower() and "ticker" in c.lower())
    removed_col = next(c for c in changes_table.columns if "removed" in c.lower() and "ticker" in c.lower())
    changes = []
    for _, row in changes_table.iterrows():
        try:
            day = pd.to_datetime(row[date_col]).date().isoformat()
        except Exception:
            continue
        added = str(row[added_col]).strip().replace(".", "-") if str(row[added_col]) != "nan" else ""
        removed = str(row[removed_col]).strip().replace(".", "-") if str(row[removed_col]) != "nan" else ""
        changes.append({"date": day, "added": added, "removed": removed})
    return bt.membership_from_changes(current, changes), current, changes


def download(tickers, start, end, chunk=80):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                data = yf.download(part, start=start, end=end, interval="1d", group_by="ticker", auto_adjust=True,
                                   progress=False, threads=True, timeout=30)
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
            rows = [{"date": idx.date().isoformat(), "open": float(r["Open"]), "high": float(r["High"]),
                     "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"] or 0)}
                    for idx, r in frame.iterrows() if float(r["Close"]) > 0 and float(r["Low"]) > 0]
            if len(rows) >= 150:
                out[symbol] = rows
        logger.info("bars: %d/%d chunks, %d tickers with data", i // chunk + 1, math.ceil(len(tickers) / chunk), len(out))
    return out


def fmt(stats):
    if not stats.get("trades"):
        return "no trades"
    pf = stats["profit_factor"]
    return (f"{stats['trades']} trades · win {stats['win_rate']:.1f}% · avg {stats['avg_return_pct']:+.2f}% "
            f"(t {stats['t_stat']:+.1f}) · avg R {stats['avg_r']:+.2f} · PF {pf:.2f} · {stats['avg_days']:.1f} days"
            if pf is not None else f"{stats['trades']} trades")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-10-01")
    parser.add_argument("--split", default="2024-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--out", default="reports/backtests")
    parser.add_argument("--limit", type=int, default=0, help="first N tickers only (smoke runs)")
    parser.add_argument("--ambiguous", choices=["stop", "nearest"], default="stop",
                        help="fill when one day touches both stop and target")
    args = parser.parse_args()
    bt.AMBIGUOUS = args.ambiguous
    from src.utils.yfinance_cache import use_memory_tz_cache
    use_memory_tz_cache()

    member, current, changes = sp500_membership()
    universe = [t for t in member.tickers if any(member(t, d) for d in (args.start, args.split, args.end))
                or any(args.start <= c["date"] <= args.end and t in (c["added"], c["removed"]) for c in changes)]
    removed_in_window = sorted({c["removed"] for c in changes if args.start <= c["date"] <= args.end and c["removed"]})
    if args.limit:
        universe = universe[:args.limit]
    fetch_start = (date.fromisoformat(args.start) - timedelta(days=260)).isoformat()
    bars = download(sorted(set(universe) | {"SPY"}), fetch_start, args.end)
    market = bars.get("SPY")
    stocks = {t: rows for t, rows in bars.items() if t in universe}
    coverage = {"universe": len(universe), "with_bars": len(stocks),
                "removed_in_window": len(removed_in_window),
                "removed_with_bars": sum(1 for t in removed_in_window if t in stocks)}
    logger.info("coverage %s", coverage)
    etf_bars = download(ETFS, fetch_start, args.end)
    always = lambda ticker, day: True  # noqa: E731

    results = {"generated": datetime.now().isoformat(timespec="seconds"), "params": {
        "ambiguous_fill": bt.AMBIGUOUS, "stop_atr": bt.STOP_ATR, "target_atr": bt.TARGET_ATR, "max_days": bt.MAX_DAYS, "cost_bps": bt.COST_BPS,
        "start": args.start, "split": args.split, "end": args.end}, "coverage": coverage, "periods": {}}
    # Signals are generated once over the whole range and split by signal date.
    universes = {}
    for universe_name, universe_bars, is_member in (("sp500_point_in_time", stocks, member),
                                                     ("etfs", etf_bars, always)):
        trades = bt.run_plans(universe_bars, member=is_member, market=market, start_date=args.start,
                              end_date=args.end, progress=lambda msg, n=universe_name: logger.info("  %s %s", n, msg))
        twins = bt.random_baseline(trades, universe_bars, member=is_member)
        universes[universe_name] = (universe_bars, trades, twins)
        logger.info("%s: %d trades, %d random twins", universe_name, len(trades), len(twins))
    for label, lo, hi in (("before_split", args.start, args.split), ("after_split", args.split, args.end),
                          ("full", args.start, args.end)):
        section = {}
        for universe_name, (universe_bars, all_trades, all_twins) in universes.items():
            trades = [t for t in all_trades if lo <= t.signal_date < hi or (label == "full")]
            twins = [t for t in all_twins if lo <= t.signal_date < hi or (label == "full")]
            block = {}
            for plan in bt.PLANS:
                plan_trades = [t for t in trades if t.plan == plan]
                entry = {"all": bt.summarize(plan_trades),
                         "long": bt.summarize([t for t in plan_trades if t.direction == "long"]),
                         "short": bt.summarize([t for t in plan_trades if t.direction == "short"]),
                         "random_twin": bt.summarize([t for t in twins if t.plan == f"random:{plan}"]),
                         "random_twin_long": bt.summarize([t for t in twins if t.plan == f"random:{plan}"
                                                           and t.direction == "long"]),
                         "random_twin_short": bt.summarize([t for t in twins if t.plan == f"random:{plan}"
                                                            and t.direction == "short"])}
                sim = bt.portfolio(plan_trades)
                entry["portfolio"] = {k: v for k, v in sim.items() if k != "curve"}
                entry["curve"] = sim["curve"][::5]
                if plan == "swing_trend" and universe_name == "sp500_point_in_time":
                    opts = {"atm_option": [], "debit_spread": [], "shares": []}
                    for trade in plan_trades:
                        try:
                            variants = bt.option_variants(trade, universe_bars[trade.ticker])
                        except (StopIteration, ValueError, ZeroDivisionError):
                            continue
                        if len(variants) == 2:  # compare like with like
                            opts["shares"].append(trade.return_pct)
                            for key, value in variants.items():
                                opts[key].append(value)
                    entry["options"] = {key: {"n": len(v), "avg_return_pct": sum(v) / len(v) if v else None,
                                              "median_return_pct": sorted(v)[len(v) // 2] if v else None,
                                              "win_rate": sum(1 for x in v if x > 0) / len(v) * 100 if v else None}
                                        for key, v in opts.items()}
                block[plan] = entry
            section[universe_name] = block
        if market:
            spy = [b for b in market if lo <= b["date"] <= hi]
            if len(spy) > 1:
                years = (date.fromisoformat(spy[-1]["date"]) - date.fromisoformat(spy[0]["date"])).days / 365.25
                section["spy_buy_and_hold_cagr_pct"] = ((spy[-1]["close"] / spy[0]["close"]) ** (1 / years) - 1) * 100
        results["periods"][label] = section
    # Control: the same plans on random walks, where no edge exists (checks for look-ahead/fill bugs).
    walks = bt.random_walk_bars(150, 1300)
    control_trades = bt.run_plans(walks, member=always)
    results["random_walk_control"] = {plan: bt.summarize([t for t in control_trades if t.plan == plan])
                                      for plan in ("swing_trend", "breakout")}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"trade_plans_{date.today().isoformat()}_{args.ambiguous}"
    stem.with_suffix(".json").write_text(json.dumps(results, indent=1, default=str))
    lines = [f"# Trade plan backtest ({results['generated']})", "",
             f"Parameters: {results['params']}", f"Coverage: {coverage}", ""]
    for label, section in results["periods"].items():
        lines.append(f"## {label}  (SPY buy-and-hold CAGR {section.get('spy_buy_and_hold_cagr_pct', float('nan')):.1f}%)")
        for universe_name in ("sp500_point_in_time", "etfs"):
            for plan, entry in section[universe_name].items():  # noqa: B007
                lines.append(f"- **{universe_name} / {plan}**: {fmt(entry['all'])}")
                lines.append(f"  - long: {fmt(entry['long'])}; short: {fmt(entry['short'])}")
                lines.append(f"  - random twin: {fmt(entry['random_twin'])}")
                lines.append(f"  - random twin long: {fmt(entry['random_twin_long'])}; "
                             f"short: {fmt(entry['random_twin_short'])}")
                p = entry["portfolio"]
                lines.append(f"  - portfolio (1% risk, 10 max): CAGR {p['cagr_pct']:.1f}%, max DD {p['max_drawdown_pct']:.1f}%, "
                             f"{p['taken']} taken")
                if entry.get("options"):
                    lines.append(f"  - options: {entry['options']}")
        lines.append("")
    lines.append("## Random-walk control (no edge exists: costs plus the conservative same-day stop-first fill; "
                 "compare each plan with its random twin, which carries the same bias)")
    for plan, stats in results["random_walk_control"].items():
        lines.append(f"- {plan}: {fmt(stats)}")
    stem.with_suffix(".md").write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\nWritten {stem}.json/.md")


if __name__ == "__main__":
    main()
