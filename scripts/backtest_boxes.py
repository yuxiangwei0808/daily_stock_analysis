#!/usr/bin/env python3
"""Backtest the box trades (src/services/trade_desk/boxes.py) on moomoo 15-minute bars.

    python scripts/backtest_boxes.py --out reports/backtests
    python scripts/backtest_boxes.py --tickers SPY,QQQ --offline   # cached bars only

Bars come from moomoo OpenD (read-only ``request_history_kline``, forward-adjusted, regular
session) and are cached in ``--cache`` as one CSV per stock; history goes back to 2018-10.
Each new stock uses one of the account's history allowances for 30 days (cached stocks use
none). Rules, controls and the pass bar are fixed in the modules and in
docs/trading-methodology.md; nothing is tuned on the results.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import zlib
from datetime import date
from multiprocessing import Pool
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_day_trades import watchlist  # noqa: E402
from src.services.trade_desk import boxes as bx  # noqa: E402
from src.services.trade_desk.backtest import boxes as bb  # noqa: E402
from src.services.trade_desk.idea_tracker import t_needed  # noqa: E402

logger = logging.getLogger("box-backtest")
ETFS = ["SPY", "QQQ", "IWM", "DIA"]
LARGE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
         "JPM", "BAC", "XOM", "CVX", "UNH", "LLY", "COST", "WMT", "HD", "KO"]
VARIANTS = [(impulse, tidy) for tidy in (False, True) for impulse in (1.0, 0.6)]
CONTROL_SEEDS = (1, 2)
COLUMNS = ["time_key", "open", "high", "low", "close", "volume"]


def download(tickers, start, end, cache: Path):
    """Fetch missing stocks from OpenD into ``cache`` (read-only quote requests)."""
    import pandas as pd
    from src.config import setup_env
    from src.services.trade_desk.providers import MoomooProvider
    setup_env()
    provider = MoomooProvider()
    sdk = provider._load_sdk()
    if hasattr(sdk, "logger"):
        sdk.logger.file_level = logging.INFO  # one debug line per request otherwise, in the home folder
    ctx = provider._ensure_context()  # the provider owns host, port and protocol encryption
    try:
        for ticker in tickers:
            pages, key = [], None
            while True:
                for _attempt in range(6):
                    ret, data, nxt = ctx.request_history_kline(
                        f"US.{ticker}", start=start, end=end, ktype=sdk.KLType.K_15M, autype=sdk.AuType.QFQ,
                        max_count=1000, page_req_key=key)
                    if ret == sdk.RET_OK:
                        break
                    message = str(data).lower()
                    # A timeout or the request-rate limit is retried: giving up mid-way would truncate the history.
                    if not any(word in message for word in ("timed out", "freq", "limit", "频率")):
                        raise RuntimeError(f"{ticker}: {str(data)[:160]}")
                    time.sleep(31)
                else:
                    raise RuntimeError(f"{ticker}: still failing after retries")
                pages.append(data[COLUMNS])
                time.sleep(0.6)
                key = nxt
                if key is None:
                    break
            frame = pd.concat(pages, ignore_index=True).drop_duplicates("time_key")
            frame.to_csv(cache / f"{ticker}.csv.gz", index=False)
            logger.info("%s: %d bars %s .. %s", ticker, len(frame), frame.time_key.iloc[0][:10], frame.time_key.iloc[-1][:10])
    finally:
        provider.close()


def load(path: Path, start: str, end: str) -> bx.Bars:
    import pandas as pd
    frame = pd.read_csv(path)
    frame = frame[(frame.time_key >= start) & (frame.time_key <= f"{end} 23:59:59")]
    return bx.Bars.from_rows(frame.to_dict("records"))


def one(job):
    ticker, path, start, end, control = job
    bars = load(Path(path), start, end)
    areas = bx.weekly_value_areas(bars)
    real, ctrl = [], []
    for impulse, tidy in VARIANTS:
        params = bx.Params(impulse_t=impulse, tidy=tidy)
        real += bb.run(bars, ticker, params, zlib.crc32(f"{ticker}|{impulse}|{tidy}".encode()), areas)
        if control and not tidy:
            for seed in CONTROL_SEEDS:
                ctrl += bb.run(bb.shuffled(bars, seed), ticker, params, seed)
    return real, ctrl


def by_period(trades, split):
    periods = {"A": [t for t in trades if t.entry_time < split], "B": [t for t in trades if t.entry_time >= split]}
    return {p: bb.summarize(rows) for p, rows in periods.items() if rows}


def fmt(x, digits=3):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{digits}f}"


def pct(x):
    return "n/a" if x is None else f"{x:.0f}%"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2020-11-05")
    parser.add_argument("--split", default="2023-11-05")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--tickers", default="", help="comma list; default: index ETFs, 20 large caps, STOCK_LIST")
    parser.add_argument("--cache", default="reports/backtests/bars_15m")
    parser.add_argument("--out", default="reports/backtests")
    parser.add_argument("--offline", action="store_true", help="use cached bars only")
    parser.add_argument("--no-control", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or \
        sorted(set(ETFS + LARGE + watchlist()))
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    missing = [t for t in tickers if not (cache / f"{t}.csv.gz").exists()]
    if missing and args.offline:
        logger.info("No cached bars, skipped: %s", ", ".join(missing))
        tickers = [t for t in tickers if t not in missing]
    elif missing:
        logger.info("Downloading %d stocks from OpenD (one history allowance each)", len(missing))
        download(missing, args.start, args.end, cache)
    jobs = [(t, str(cache / f"{t}.csv.gz"), args.start, args.end, not args.no_control) for t in tickers]
    with Pool(max(1, args.workers)) as pool:
        results = pool.map(one, jobs)
    real = [t for rows, _ in results for t in rows]
    ctrl = [t for _, rows in results for t in rows]

    report = {"start": args.start, "split": args.split, "end": args.end, "tickers": len(tickers), "variants": []}
    lines = [f"# Box trades: {len(tickers)} stocks, {args.start} → {args.end} (period B from {args.split})", ""]
    rng = np.random.default_rng(5)
    for impulse, tidy in VARIANTS:
        for kind in ("break_retest", "fade"):
            rows = [t for t in real if t.impulse_t == impulse and t.tidy == tidy and t.kind == kind]
            stats = by_period(rows, args.split)
            needed = max((t_needed(s["months"]) or 99) for s in stats.values()) if stats else 99
            entry = {"impulse_t": impulse, "tidy": tidy, "kind": kind, "periods": stats,
                     "pass": bb.passes(stats, needed), "t_needed": needed}
            if not tidy:
                entry["control"] = bb.summarize([t for t in ctrl if t.impulse_t == impulse and t.kind == kind])
            with_levels = [t for t in rows if len(t.levels) == 4]
            conf = [t for t in with_levels if bx.confirmed(t.span, t.levels, t.T)]
            unconf = [t for t in with_levels if not bx.confirmed(t.span, t.levels, t.T)]
            va = {"share_confirmed": len(conf) / len(with_levels) * 100 if with_levels else None,
                  "confirmed": by_period(conf, args.split), "unconfirmed": by_period(unconf, args.split)}
            for period in ("A", "B"):
                part = [t for t in with_levels if (t.entry_time < args.split) == (period == "A")]
                c = va["confirmed"].get(period, {})
                u = va["unconfirmed"].get(period, {})
                gap = (c["edge"] - u["edge"]) if c.get("edge") is not None and u.get("edge") is not None else None
                draws = [g for g in bb.placebo_gaps(part, rng) if not math.isnan(g)]
                va[f"gap_{period}"] = gap
                va[f"placebo_pct_{period}"] = (sum(g < gap for g in draws) / len(draws) * 100
                                               if draws and gap is not None else None)
            va["pass"] = bb.passes(va["confirmed"], needed) and all(
                (va.get(f"placebo_pct_{p}") or 0) >= 95 for p in ("A", "B"))
            entry["value_area"] = va
            report["variants"].append(entry)

            name = f"{kind.replace('_', ' ')}, impulse >= {impulse} T, {'tidy' if tidy else 'normal'} boxes"
            lines.append(f"## {name}: {'PASS' if entry['pass'] else 'no edge shown'}")
            lines.append("| Period | Trades | Mean R | Win % | Twin R | Edge | t (edge) |")
            lines.append("|---|---|---|---|---|---|---|")
            for p, s in stats.items():
                lines.append(f"| {p} | {s['trades']} | {fmt(s['mean_R'])} | {s['win_pct']:.0f} | {fmt(s['twin_R'])} "
                             f"| {fmt(s['edge'])} | {fmt(s['t_edge'], 2)} |")
            if "control" in entry:
                c = entry["control"]
                lines.append(f"Shuffled-data control: edge {fmt(c['edge'])} (t {fmt(c['t_edge'], 2)}), {c['trades']} trades")
            lines.append(f"Value area: {pct(va['share_confirmed'])} of trades confirmed; confirmed − unconfirmed edge "
                         f"A {fmt(va['gap_A'])} (beats {pct(va['placebo_pct_A'])} of placebo draws), "
                         f"B {fmt(va['gap_B'])} ({pct(va['placebo_pct_B'])}): "
                         f"{'PASS' if va['pass'] else 'no edge shown'}")
            lines.append("")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"boxes_{args.end}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=1, default=str))
    stem.with_suffix(".md").write_text("\n".join(lines))
    print("\n".join(lines))
    logger.info("Wrote %s.json/.md", stem)


if __name__ == "__main__":
    main()
