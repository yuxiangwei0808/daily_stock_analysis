#!/usr/bin/env python3
"""The in-play range test (docs/strategy-backtest.md): do stocks ranked at 09:45 move more the rest of the day?

    python scripts/backtest_in_play.py --out reports/backtests      # the cached bars of the box test

Rules, controls and the pass bar were frozen on 2026-11-06 before any real-data run and are in
src/services/trade_desk/backtest/in_play.py. It reads the 15-minute bars cached by
scripts/backtest_boxes.py (no OpenD request, no history allowance) and writes a JSON and a Markdown report.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_boxes import load  # noqa: E402
from src.services.trade_desk.backtest import in_play as ip  # noqa: E402

SECONDARY = (("top 3", {"top": 3}), ("gap alone", {"score": "gap"}), ("first bar's range alone", {"score": "range1"}),
             ("S times the first bar's volume ratio", {"score": "S_vol"}), ("up gaps", {"keep": "up"}),
             ("down gaps", {"keep": "down"}))


def fmt(x, digits=3):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{digits}f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2020-11-05")
    parser.add_argument("--split", default="2023-11-05")
    parser.add_argument("--end", default="2026-11-05")
    parser.add_argument("--cache", default="reports/backtests/bars_15m")
    parser.add_argument("--out", default="reports/backtests")
    args = parser.parse_args()
    paths = sorted(Path(args.cache).glob("*.csv*"))  # the box test caches CSV.gz
    if not paths:
        raise SystemExit(f"No cached bars in {args.cache}: run scripts/backtest_boxes.py first")
    rng = np.random.default_rng(ip.SEED)
    tables, walks = {}, {}
    for path in paths:
        bars = load(path, args.start, args.end)
        name = path.name.split(".")[0]
        tables[name] = ip.features(bars)
        walks[name] = ip.features(ip.random_walk(bars, rng))
    main_edges = ip.daily_edges(tables)
    result = {"frozen": "2026-11-06", "stocks": len(paths), "start": args.start, "split": args.split, "end": args.end,
              "main": ip.summary(main_edges, args.split), "weeks": ip.summary(main_edges, args.split, weeks=True)}
    result["passes"] = ip.passes(result["main"])
    picks = [row["Y"] for rows in tables.values() for row in rows.values()]
    result["typical_rest_of_day"] = {"median_Y": float(np.median(picks)) if picks else None}
    shuffled = ip.shuffled_scores(tables, rng)
    result["shuffled_scores"] = {"mean": float(np.mean(shuffled)), "sd": float(np.std(shuffled)), "draws": len(shuffled)}
    result["random_walk"] = ip.summary(ip.daily_edges(walks), args.split)
    result["secondary"] = {name: ip.summary(ip.daily_edges(tables, **kw), args.split) for name, kw in SECONDARY}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"in_play_{date.today().isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(result, indent=2, default=float))
    lines = [f"# In-play range test ({len(paths)} stocks, {args.start} → {args.end}, period B from {args.split})", "",
             "| Check | Period | Days | Edge (Y, in T) | t | Days with a positive edge |", "|---|---|---|---|---|---|"]
    rows = [("Main: top 5 by S, day clusters", result["main"]), ("Main, week clusters", result["weeks"]),
            ("Random walks", result["random_walk"]), *((f"Secondary: {name}", value) for name, value in result["secondary"].items())]
    for name, by_period in rows:
        for period, row in by_period.items():
            share = "—" if row["share_positive"] is None else f"{row['share_positive'] * 100:.0f}%"
            lines.append(f"| {name} | {period} | {row['days']} | {fmt(row['edge'])} | {fmt(row['t'], 2)} | {share} |")
    lines += ["", f"Scores shuffled among each day's stocks ({result['shuffled_scores']['draws']} times): mean edge "
              f"{fmt(result['shuffled_scores']['mean'])}, sd {result['shuffled_scores']['sd']:.3f}.",
              f"Median rest-of-day range: {result['typical_rest_of_day']['median_Y']:.3f} T.",
              "", f"Verdict: {'pass' if result['passes'] else 'no edge shown'} as a range selector (what to watch, not a trade)."]
    stem.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
