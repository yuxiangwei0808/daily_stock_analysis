#!/usr/bin/env python3
"""Backtest the user's moomoo indicators NX and CD (see src/services/trade_desk/indicator_backtest.py).

    python scripts/backtest_indicators.py --start 2021-10-01 --split 2024-01-01 --out reports/backtests

Same universe, periods and costs as scripts/backtest_trade_plans.py. Also lists the
latest CD marks on --check tickers so they can be compared with the moomoo chart.
Needs network (Wikipedia, Yahoo Finance).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest_trade_plans as plans  # noqa: E402
from src.services.trade_desk import indicator_backtest as ib  # noqa: E402
from src.services.trade_desk import moomoo_indicators as mi  # noqa: E402
from src.services.trade_desk import strategy_backtest as bt  # noqa: E402

logger = logging.getLogger("indicator-backtest")


def compare(trades, twins):
    rule, twin = bt.summarize(trades), bt.summarize(twins)
    diff = (rule["avg_return_pct"] - twin["avg_return_pct"]) if rule.get("trades") and twin.get("trades") else None
    # Welch t for the difference of means.
    se = None
    if rule.get("trades", 0) > 1 and twin.get("trades", 0) > 1:
        def var(items):
            values = [t.return_pct for t in items]
            mean = sum(values) / len(values)
            return sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        se = math.sqrt(var(trades) / len(trades) + var(twins) / len(twins))
    # Marks cluster (several in a few days), so trades overlap: one rule-minus-twin number per signal month.
    months = {}
    for t in trades:
        months.setdefault(t.signal_date[:7], [[], []])[0].append(t.return_pct)
    for t in twins:
        months.setdefault(t.signal_date[:7], [[], []])[1].append(t.return_pct)
    diffs = [sum(a) / len(a) - sum(b) / len(b) for a, b in months.values() if a and b]
    t_month = None
    if len(diffs) > 2:
        mean = sum(diffs) / len(diffs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1))
        t_month = mean / (sd / math.sqrt(len(diffs))) if sd else None
    return {"rule": rule, "twin": twin, "vs_twin_pct": diff, "t_vs_twin": diff / se if diff is not None and se else None,
            "t_by_month": t_month, "months": len(diffs)}


def line(name, row, old=None):
    rule, twin = row["rule"], row["twin"]
    if not rule.get("trades"):
        return f"| {name} | 0 | | | | | | |"
    return (f"| {name} | {rule['trades']} | {rule['win_rate']:.0f}% | {rule['avg_return_pct']:+.2f}% | "
            f"{twin.get('avg_return_pct', float('nan')):+.2f}% | {row['vs_twin_pct']:+.2f}% | "
            f"{row['t_by_month'] if row['t_by_month'] is None else format(row['t_by_month'], '+.1f')} ({row['months']}) | "
            f"{rule['avg_days']:.0f} | {old['vs_twin_pct']:+.2f}% |")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-10-01")
    parser.add_argument("--split", default="2024-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--out", default="reports/backtests")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--check", default="NVDA,SOXS,GOOG,TSLA,SOFI,RKLB")
    args = parser.parse_args()
    from src.utils.yfinance_cache import use_memory_tz_cache
    use_memory_tz_cache()

    member, current, changes = plans.sp500_membership()
    universe = [t for t in member.tickers if any(member(t, d) for d in (args.start, args.split, args.end))
                or any(args.start <= c["date"] <= args.end and t in (c["added"], c["removed"]) for c in changes)]
    if args.limit:
        universe = universe[:args.limit]
    fetch_start = (date.fromisoformat(args.start) - timedelta(days=400)).isoformat()
    stocks = plans.download(sorted(set(universe)), fetch_start, args.end)
    etfs = plans.download(plans.ETFS, fetch_start, args.end)
    always = lambda ticker, day: True  # noqa: E731
    results = {"generated": datetime.now().isoformat(timespec="seconds"), "cost_bps": bt.COST_BPS,
               "start": args.start, "split": args.split, "end": args.end,
               "coverage": {"universe": len(universe), "with_bars": len(stocks), "etfs": len(etfs)}, "periods": {}}
    runs = {name: ib.run(bars, member=is_member, start_date=args.start, end_date=args.end)
            for name, bars, is_member in (("sp500_point_in_time", stocks, member), ("etfs", etfs, always))}
    # Control: random walks have no edge, so every row should be ~0 against same-date twins.
    walks = bt.random_walk_bars(300, 1300)
    first_day = walks[next(iter(walks))][0]["date"]
    runs["random_walk_control"] = ib.run(walks, member=always, start_date=first_day, end_date="9999")
    lines = [f"# NX / CD backtest ({results['generated']})", "",
             f"{args.start} → {args.end}, split {args.split}; {bt.COST_BPS:g} bps per side; "
             f"coverage {results['coverage']}. Baseline: same-date twins (other member stocks entered on the "
             "signal's date, same direction and exit). t is clustered by signal month. The last column is the "
             "earlier same-ticker/same-year twin, kept only to show its bias (see the random-walk control).", ""]
    header = ("| Plan / exit | Trades | Win | Avg net | Same-date twin avg | vs twin | t by month (months) | Days "
              "| vs same-ticker twin (biased) |\n|---|---|---|---|---|---|---|---|---|")
    for label, lo, hi in (("before_split", args.start, args.split), ("after_split", args.split, args.end),
                          ("full", args.start, args.end)):
        section = {}
        for universe_name, (trades, dated, same) in runs.items():
            if universe_name == "random_walk_control" and label != "full":
                continue
            block, old = {}, {}
            for plan in ib.PLANS:
                for exit_kind in ib.EXITS:
                    key = f"{plan}/{exit_kind}"
                    window = (lambda t: lo <= t.signal_date < hi) if universe_name != "random_walk_control" else (lambda t: True)
                    chosen = [t for t in trades if t.plan == key and window(t)]
                    block[key] = compare(chosen, [t for t in dated if t.plan == "date:" + key and window(t)])
                    old[key] = compare(chosen, [t for t in same if t.plan == "random:" + key and window(t)])
                    block[key]["same_ticker_vs_twin_pct"] = old[key]["vs_twin_pct"]
            section[universe_name] = block
            lines += [f"## {label} · {universe_name}", "", header]
            lines += [line(key, row, old[key]) for key, row in block.items()]
            lines.append("")
        results["periods"][label] = section

    # Latest CD marks for a manual check against the moomoo chart.
    check = [t.strip().upper() for t in args.check.split(",") if t.strip()]
    recent = plans.download(check, (date.fromisoformat(args.end) - timedelta(days=900)).isoformat(), args.end)
    marks = {}
    for ticker, bars in recent.items():
        cd = mi.cd(bars)
        marks[ticker] = [(bars[i]["date"], "抄底" if cd["buy"][i] else "卖出")
                         for i in range(len(bars)) if cd["buy"][i] or cd["sell"][i]][-6:]
    results["cd_marks_to_check"] = marks
    lines += ["## Latest CD marks (compare with the moomoo chart)", ""]
    lines += [f"- {ticker}: " + ", ".join(f"{day} {mark}" for day, mark in items) for ticker, items in marks.items()]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"indicators_nx_cd_{date.today().isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(results, indent=1, default=str))
    stem.with_suffix(".md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
