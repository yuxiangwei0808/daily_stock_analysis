"""Backtest of the user's moomoo indicators NX and CD on daily bars (research only).

Rules were fixed before any result was seen. Signals use the day's close and
enter at the next open; costs are ``swing.COST_BPS`` per side.

- ``cd_buy``: long on CD's 抄底 mark.
- ``cd_buy_nx``: the same, only while the close is above NX's slow tunnel (B1).
- ``cd_sell``: short on CD's 卖出 mark (read as "expect a fall").
- ``nx_long``: the close crosses above the fast tunnel's top (A) while above the
  slow tunnel's top (A1).
- ``nx_short``: the close crosses below the fast tunnel's bottom (B) while below
  the slow tunnel's bottom (B1).

Each signal is traded with three exits:

- ``atr``: the system's plan levels — 1.5 ATR stop, 3 ATR target, 15 sessions.
- ``hold10``: the close ten sessions after entry.
- ``rule``: the indicator's own exit — CD longs until a 卖出 mark, CD shorts
  until a 抄底 mark, NX longs until a close below B, NX shorts until a close
  above A — acted on at the next open, 60 sessions at most.

Baseline: same-date twins — other member stocks entered on the signal's date with
the same direction and exit (an event-study control). Random days of the same
stock and year were tried first but select on the stock's realised path: on
random walks they show a fake +0.55% edge for CD buys and -0.60% for NX longs.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import levels
from .. import moomoo_indicators as mi
from . import swing as bt

PLANS = ("cd_buy", "cd_buy_nx", "cd_sell", "nx_long", "nx_short")
EXITS = ("atr", "hold10", "rule")
RULE_MAX_DAYS = 60
HOLD_DAYS = 10


def prepare(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    tunnel, cd = mi.nx(bars), mi.cd(bars)
    close = [float(bar["close"]) for bar in bars]
    return {"close": close, **tunnel, "buy": cd["buy"], "sell": cd["sell"]}


def signals(ind: Dict[str, Any], index: int) -> List[tuple]:
    """(plan, direction) pairs signalled at ``index``."""
    out = []
    close, a, b, a1, b1 = ind["close"], ind["A"], ind["B"], ind["A1"], ind["B1"]
    if ind["buy"][index]:
        out.append(("cd_buy", "long"))
        if close[index] > b1[index]:
            out.append(("cd_buy_nx", "long"))
    if ind["sell"][index]:
        out.append(("cd_sell", "short"))
    if index > 0:
        if close[index] > a[index] and close[index - 1] <= a[index - 1] and close[index] > a1[index]:
            out.append(("nx_long", "long"))
        if close[index] < b[index] and close[index - 1] >= b[index - 1] and close[index] < b1[index]:
            out.append(("nx_short", "short"))
    return out


def rule_exit(plan: str, ind: Dict[str, Any], index: int) -> bool:
    close = ind["close"]
    if plan in ("cd_buy", "cd_buy_nx"):
        return ind["sell"][index]
    if plan == "cd_sell":
        return ind["buy"][index]
    if plan == "nx_long":
        return close[index] < ind["B"][index]
    return close[index] > ind["A"][index]  # nx_short


def _atr(bars: Sequence[Dict[str, Any]], index: int) -> float:
    return levels.atr(bars, index)


def trade(bars, ind, index: int, plan: str, direction: str, exit_kind: str, ticker: str) -> Optional[bt.Trade]:
    """One trade signalled at ``index`` (entry at the next open) with the given exit."""
    label = f"{plan}/{exit_kind}"
    if exit_kind == "atr":
        return bt.simulate(bars, index, direction, _atr(bars, index), plan=label, ticker=ticker)
    entry_index = index + 1
    if entry_index >= len(bars):
        return None
    entry = float(bars[entry_index]["open"])
    if entry <= 0:
        return None
    result = bt.Trade(label, ticker, direction, bars[index]["date"], bars[entry_index]["date"], entry, entry, entry)
    if exit_kind == "hold10":
        last = min(len(bars) - 1, entry_index + HOLD_DAYS - 1)
        result.exit, result.reason = float(bars[last]["close"]), "time" if last == entry_index + HOLD_DAYS - 1 else "end"
        result.exit_date, result.days = bars[last]["date"], last - entry_index + 1
        return result
    last = min(len(bars) - 1, entry_index + RULE_MAX_DAYS - 1)
    for i in range(entry_index, last):
        if rule_exit(plan, ind, i):  # decided at this close, filled at the next open
            result.exit, result.reason = float(bars[i + 1]["open"]), "rule"
            result.exit_date, result.days = bars[i + 1]["date"], i + 1 - entry_index + 1
            return result
    result.exit, result.reason = float(bars[last]["close"]), "time" if last == entry_index + RULE_MAX_DAYS - 1 else "end"
    result.exit_date, result.days = bars[last]["date"], last - entry_index + 1
    return result


def run(bars_by_ticker: Dict[str, List[Dict[str, Any]]], *, member: Callable[[str, str], bool],
        start_date: str, end_date: str, warmup: int = 150, seed: int = 13, draws: int = 3):
    """All plan trades plus two baselines: (trades, same-date twins, same-ticker twins).

    Same-date twins (the primary baseline) enter other member stocks on the signal's
    date, same direction and exit. Same-ticker twins (random days of the same stock
    and year) are kept for comparison only: they inherit that stock-year's realised
    path, which the signals select on, so they are biased even on random walks.
    """
    rng = random.Random(seed)
    prepared = {ticker: prepare(bars) for ticker, bars in bars_by_ticker.items() if len(bars) > warmup + 2}
    index_of = {ticker: {bar["date"]: i for i, bar in enumerate(bars_by_ticker[ticker])} for ticker in prepared}
    on_date: Dict[str, List[str]] = {}
    for ticker in prepared:
        for day, i in index_of[ticker].items():
            if i >= warmup and start_date <= day < end_date and member(ticker, day):
                on_date.setdefault(day, []).append(ticker)
    trades: List[bt.Trade] = []
    date_twins: List[bt.Trade] = []
    ticker_twins: List[bt.Trade] = []
    for ticker, ind in prepared.items():
        bars = bars_by_ticker[ticker]
        by_year: Dict[str, List[int]] = {}
        for i in range(warmup, len(bars) - 1):
            day = bars[i]["date"]
            if start_date <= day < end_date and member(ticker, day):
                by_year.setdefault(day[:4], []).append(i)
        for year_days in by_year.values():
            for i in year_days:
                day = bars[i]["date"]
                others = [other for other in on_date.get(day, []) if other != ticker]
                for plan, direction in signals(ind, i):
                    for exit_kind in EXITS:
                        made = trade(bars, ind, i, plan, direction, exit_kind, ticker)
                        if made is None:
                            continue
                        trades.append(made)
                        for _ in range(draws):
                            if others:
                                other = rng.choice(others)
                                twin = trade(bars_by_ticker[other], prepared[other], index_of[other][day], plan,
                                             direction, exit_kind, other)
                                if twin is not None:
                                    twin.plan = "date:" + twin.plan
                                    date_twins.append(twin)
                            j = rng.choice(year_days)
                            twin = trade(bars, ind, j, plan, direction, exit_kind, ticker)
                            if twin is not None:
                                twin.plan = "random:" + twin.plan
                                ticker_twins.append(twin)
    return trades, date_twins, ticker_twins
