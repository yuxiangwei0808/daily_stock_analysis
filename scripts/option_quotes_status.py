#!/usr/bin/env python3
"""How much 0DTE quote data the recorder has written, and whether tests on it can start.

    python scripts/option_quotes_status.py                    # OPTION_RECORDER_DIR, else option_quotes/ by the database

Phase 6 of docs/trading-assistant-plan.md: tests on recorded option quotes wait for about 60 sessions per
underlying and are frozen before they run (docs/trading-methodology.md §3, §14). Read only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.trade_desk.backtest import option_quotes  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="")
    args = parser.parse_args()
    if args.dir:
        root = Path(args.dir)
    else:
        from src.config import setup_env
        setup_env()
        from src.services.trade_desk.option_recorder import directory
        root = directory()
    found = option_quotes.recorded(root)
    if not found:
        print(f"Nothing recorded yet in {root} (OPTION_RECORDER_ENABLED=true and a restart start it).")
        return
    size = sum(path.stat().st_size for path in root.glob("*/*.csv.gz"))
    print(f"{root}: {len(found)} underlyings, {size / 2**20:.1f} MB")
    for ticker, days in sorted(found.items(), key=lambda item: -len(item[1])):
        ready = "ready" if len(days) >= option_quotes.READY_SESSIONS else f"{option_quotes.READY_SESSIONS - len(days)} to go"
        print(f"  {ticker:6} {len(days):4} sessions  {days[0]} .. {days[-1]}  {ready}")


if __name__ == "__main__":
    main()
