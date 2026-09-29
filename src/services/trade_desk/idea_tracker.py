"""Forward track record for trade ideas and breakout alerts (research only; nothing is traded).

The backtest (``strategy_backtest``) can replay the deterministic rules but not the
model's review, so every scan's candidates are followed forward from the moment
they were published:

- ``idea``: 🎯 ideas the review approved (verdict ``high`` / ``medium``) and the
  candidates it rejected (``rejected``, with the rule's ATR levels). Comparing the
  two groups measures whether the review adds value.
- ``breakout``: breakout alerts, with the levels they printed.

Entry is the price in the message. From the next session on, daily bars settle
each record like the backtest: stop and target on highs/lows (a day touching
both counts as the stop, a gap fills at the open), otherwise the close after
``MAX_DAYS`` sessions; 5 bps per side. SPY over the same days is the benchmark.
After the close each trading day open records are settled; on the week's last
trading day a summary goes to Discord (``track_record``).

Each record also gets the user's NX tunnel state at the signal (``nx``): tunnels
from the daily bars before the signal day (no look-ahead) against the alert price.
The slow (89) tunnel sets ``alignment``: a long above it or a short below it
"agrees", the opposite is "against", inside it is "neutral". The summary splits
closed records by it, to see whether ideas that agree with NX do better.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import trend

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
MAX_DAYS = 15
COST_BPS = 5.0
WINDOW_DAYS = 90
NX_MIN_BARS = 250
NX_GROUPS = (("agree", "NX agrees"), ("neutral", "NX neutral"), ("against", "NX against"))
GROUPS = (("high", "Approved · high"), ("medium", "Approved · medium"), ("rejected", "Rejected by the review"),
          ("breakout", "Breakout alerts"))


def _day(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(_NEW_YORK)).astimezone(_NEW_YORK).date()


def record_scan(repo: Any, result: Dict[str, Any]) -> int:
    """Track every reviewed candidate of one scan; returns how many were new."""
    added = 0
    for item in result.get("tracked") or []:
        record = {"kind": "idea", "verdict": item["verdict"], "ticker": item["ticker"], "name": item.get("name", ""),
                  "direction": item["direction"], "signal_day": result["day"].isoformat()
                  if isinstance(result.get("day"), date) else str(result.get("day")),
                  "slot": result.get("slot"), "entry": item["price"], "stop": item["stop"],
                  "target": item["targets"][0], "strength": item.get("strength"), "source": item.get("source")}
        if repo.track_idea(f"idea:{result.get('slot')}:{item['ticker']}", record):
            added += 1
    return added


def record_breakout(repo: Any, ticker: str, direction: str, price: float, stop: float, target: float,
                    day: date) -> bool:
    return repo.track_idea(f"breakout:{day.isoformat()}:{ticker}:{direction}", {
        "kind": "breakout", "verdict": "breakout", "ticker": ticker, "direction": direction,
        "signal_day": day.isoformat(), "entry": price, "stop": stop, "target": target})


def settle(record: Dict[str, Any], bars: List[Dict[str, Any]], market: List[Dict[str, Any]],
           today: date) -> Optional[Dict[str, Any]]:
    """Updated fields for one open record (status "closed" once an exit is reached), None if unchanged."""
    path = [bar for bar in bars if bar["date"] > record["signal_day"] and bar["date"] <= today.isoformat()]
    if not path:
        return None
    sign = 1 if record["direction"] == "long" else -1
    entry, stop, target = float(record["entry"]), float(record["stop"]), float(record["target"])
    exit_price, reason, exit_day = None, "", ""
    for index, bar in enumerate(path[:MAX_DAYS]):
        opened, high, low = bar["open"], bar["high"], bar["low"]
        if (low <= stop) if sign > 0 else (high >= stop):
            exit_price = opened if ((opened <= stop) if sign > 0 else (opened >= stop)) else stop
            reason = "stop"
        elif (high >= target) if sign > 0 else (low <= target):
            exit_price = opened if ((opened >= target) if sign > 0 else (opened <= target)) else target
            reason = "target"
        elif index == MAX_DAYS - 1:
            exit_price, reason = bar["close"], "time"
        if reason:
            exit_day = bar["date"]
            break
    last = path[min(len(path), MAX_DAYS) - 1]
    mark = exit_price if reason else last["close"]
    ret = sign * (mark / entry - 1) * 100 - (2 * COST_BPS / 100 if reason else COST_BPS / 100)
    risk = abs(entry - stop) / entry * 100
    spy = [bar for bar in market if record["signal_day"] < bar["date"] <= (exit_day or last["date"])]
    before = [bar for bar in market if bar["date"] <= record["signal_day"]]
    spy_ret = (spy[-1]["close"] / before[-1]["close"] - 1) * 100 if spy and before else None
    update = {"days": min(len(path), MAX_DAYS), "return_pct": round(ret, 3),
              "r": round(ret / risk, 3) if risk else None, "spy_return_pct": round(spy_ret, 3) if spy_ret is not None else None,
              "mark": round(mark, 4)}
    if reason:
        update.update(status="closed", exit=round(exit_price, 4), exit_day=exit_day, reason=reason)
    return update


def _tunnel_state(price: float, top: float, bottom: float) -> str:
    return "above" if price > top else "below" if price < bottom else "inside"


def nx_snapshot(bars: List[Dict[str, Any]], signal_day: str, price: float, direction: str) -> Optional[Dict[str, Any]]:
    """NX tunnels from the bars before ``signal_day``, read at the alert price; None without enough history."""
    from .moomoo_indicators import nx
    history = [bar for bar in bars if str(bar["date"])[:10] < signal_day]
    if len(history) < NX_MIN_BARS or not price:
        return None
    lines = nx(history)
    fast = _tunnel_state(price, lines["A"][-1], lines["B"][-1])
    slow = _tunnel_state(price, lines["A1"][-1], lines["B1"][-1])
    if lines["B"][-1] > lines["A1"][-1]:
        structure = "fast_above_slow"
    elif lines["A"][-1] < lines["B1"][-1]:
        structure = "fast_below_slow"
    else:
        structure = "overlapping"
    alignment = "neutral" if slow == "inside" else (
        "agree" if (slow == "above") == (direction == "long") else "against")
    return {"fast": fast, "slow": slow, "structure": structure, "alignment": alignment,
            "fast_bottom": round(lines["B"][-1], 4), "slow_bottom": round(lines["B1"][-1], 4),
            "slow_top": round(lines["A1"][-1], 4)}


def settle_open(repo: Any, today: date,
                bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                nx_bars: Optional[Callable[[List[str]], Dict[str, List[Dict[str, Any]]]]] = None) -> int:
    """Settle every open record from daily bars (and fill its NX state once); returns how many closed."""
    open_records = repo.tracked_ideas(status="open")
    if not open_records:
        return 0
    history = bars(list(dict.fromkeys(["SPY", *(record["ticker"] for record in open_records)])))
    market, closed = history.get("SPY") or [], 0
    missing_nx = list(dict.fromkeys(record["ticker"] for record in open_records if "nx" not in record))
    long_history: Dict[str, List[Dict[str, Any]]] = {}
    if missing_nx:
        try:
            # Live: two years (the 89-bar tunnel needs it); an injected ``bars`` (tests) is reused.
            fetch = nx_bars or ((lambda tickers: trend.download_bars(tickers, period="2y"))
                                if bars is trend.download_bars else bars)
            long_history = fetch(missing_nx)
        except Exception as exc:  # NX is filled on a later day; settling goes on
            logger.info("Idea tracker NX bars unavailable: %s", type(exc).__name__)
    for record in open_records:
        nx_update = {}
        if "nx" not in record and record["ticker"] in long_history:
            nx_update["nx"] = nx_snapshot(long_history[record["ticker"]], record["signal_day"],
                                          float(record.get("entry") or 0), record["direction"])
        update = settle(record, history.get(record["ticker"]) or [], market, today)
        if update is None:
            if nx_update:
                payload = {key: value for key, value in record.items() if key not in {"id", "status", "created_at"}}
                repo.update_tracked_idea(record["id"], {**payload, **nx_update}, record["status"])
            continue
        update.update(nx_update)
        status = update.pop("status", "open")
        payload = {key: value for key, value in record.items() if key not in {"id", "status", "created_at"}}
        repo.update_tracked_idea(record["id"], {**payload, **update}, status)
        closed += status == "closed"
    return closed


def track_record(repo: Any, now: Optional[datetime] = None, window_days: int = WINDOW_DAYS) -> Dict[str, Any]:
    since = ((now or datetime.now(_NEW_YORK)) - timedelta(days=window_days)).isoformat()
    records = repo.tracked_ideas(since=since)
    groups = {}
    for key, label in GROUPS:
        rows = [r for r in records if r.get("verdict") == key]
        closed = [r for r in rows if r["status"] == "closed" and r.get("return_pct") is not None]
        returns = [r["return_pct"] for r in closed]
        spy = [r["spy_return_pct"] for r in closed if r.get("spy_return_pct") is not None]
        groups[key] = {
            "label": label, "open": sum(1 for r in rows if r["status"] == "open"), "closed": len(closed),
            "win_rate": sum(1 for x in returns if x > 0) / len(returns) * 100 if returns else None,
            "avg_return_pct": sum(returns) / len(returns) if returns else None,
            "avg_r": (sum(r["r"] for r in closed if r.get("r") is not None) / len(closed)) if closed else None,
            # SPY is long-only; shorts are compared with the market's move in their favour.
            "avg_vs_spy_pct": (sum(r["return_pct"] - (1 if r["direction"] == "long" else -1) * r["spy_return_pct"]
                                   for r in closed if r.get("spy_return_pct") is not None) / len(spy)) if spy else None,
            "long": sum(1 for r in rows if r["direction"] == "long"), "short": sum(1 for r in rows if r["direction"] == "short"),
        }
    by_nx = {}
    closed_all = [r for r in records if r["status"] == "closed" and r.get("return_pct") is not None]
    for key, label in NX_GROUPS:
        rows = [r for r in closed_all if (r.get("nx") or {}).get("alignment") == key]
        returns = [r["return_pct"] for r in rows]
        by_nx[key] = {"label": label, "closed": len(rows),
                      "open": sum(1 for r in records if r["status"] == "open" and (r.get("nx") or {}).get("alignment") == key),
                      "win_rate": sum(1 for x in returns if x > 0) / len(returns) * 100 if returns else None,
                      "avg_return_pct": sum(returns) / len(returns) if returns else None}
    recent = [{**{key: r.get(key) for key in ("ticker", "direction", "verdict", "signal_day", "status", "reason",
                                              "return_pct", "r", "spy_return_pct", "days", "kind")},
               "nx_alignment": (r.get("nx") or {}).get("alignment")} for r in records[:60]]
    return {"window_days": window_days, "groups": groups, "by_nx": by_nx, "recent": recent}


def _pct(value: Optional[float], sign: bool = True) -> str:
    if value is None:
        return "–"
    return f"{value:+.2f}%" if sign else f"{value:.0f}%"


def format_track_record(stats: Dict[str, Any]) -> str:
    lines = [f"📒 **Idea track record** · last {stats['window_days']} days (research only; entry at the alert price, "
             f"{MAX_DAYS}-session limit, printed stop/target)"]
    for key, _label in GROUPS:
        group = stats["groups"][key]
        if not group["closed"] and not group["open"]:
            continue
        lines.append(f"• {group['label']}: {group['closed']} closed · win {_pct(group['win_rate'], False)} · "
                     f"avg {_pct(group['avg_return_pct'])} · vs SPY {_pct(group['avg_vs_spy_pct'])} · "
                     f"{group['open']} open")
    approved = [stats["groups"][k] for k in ("high", "medium") if stats["groups"][k]["closed"]]
    rejected = stats["groups"]["rejected"]
    if approved and rejected["closed"] >= 10 and sum(g["closed"] for g in approved) >= 10:
        mean = sum(g["avg_return_pct"] * g["closed"] for g in approved) / sum(g["closed"] for g in approved)
        lines.append(f"Review value so far: approved {mean:+.2f}% vs rejected {rejected['avg_return_pct']:+.2f}% per idea.")
    else:
        lines.append("Too few closed ideas yet to judge the review; this fills in over the coming weeks.")
    by_nx = stats.get("by_nx") or {}
    if any(group["closed"] for group in by_nx.values()):
        parts = [f"{group['label']} {group['closed']} · avg {_pct(group['avg_return_pct'])}"
                 for key, group in by_nx.items() if group["closed"]]
        lines.append("By your NX slow tunnel (closed ideas and breakouts): " + " | ".join(parts)
                     + ("" if sum(group["closed"] for group in by_nx.values()) >= 20 else " — too few to judge yet"))
    return "\n".join(lines)


class TrackerJob:
    """Settles open records after each close and posts the weekly summary (worker leader only)."""

    SETTLE_AFTER = (16, 30)

    def __init__(self, repo: Any, emit: Callable[[str, Dict[str, Any], str], Any], *,
                 bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                 nx_bars: Optional[Callable[[List[str]], Dict[str, List[Dict[str, Any]]]]] = None):
        from concurrent.futures import ThreadPoolExecutor
        self.repo = repo
        self._emit = emit
        self._bars = bars
        self._nx_bars = nx_bars
        self._done_day: Optional[date] = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="idea-tracker")
        self._task = None

    def stop(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def tick(self, now: datetime) -> None:
        local = now.astimezone(_NEW_YORK)
        day = local.date()
        if self._done_day == day or (local.hour, local.minute) < self.SETTLE_AFTER or not _trading_day(day):
            return
        if self._task is not None and not self._task.done():
            return
        self._done_day = day
        self._task = self._pool.submit(self._run, day, now)

    def _run(self, day: date, now: datetime) -> None:
        try:
            closed = settle_open(self.repo, day, self._bars, self._nx_bars)
            logger.info("Idea tracker: %d records closed", closed)
            if _last_trading_day_of_week(day):
                stats = track_record(self.repo, now)
                if any(group["closed"] or group["open"] for group in stats["groups"].values()):
                    iso = day.isocalendar()
                    self._emit("track_record", {"underlying": "", "message": format_track_record(stats)},
                               f"track-record:{iso[0]}-{iso[1]}")
        except Exception as exc:  # retried tomorrow; open records stay open
            logger.warning("Idea tracker failed: %s", type(exc).__name__)


def _trading_day(day: date) -> bool:
    from .holdings import _trading_day as trading
    return trading(day)


def _last_trading_day_of_week(day: date) -> bool:
    following = day + timedelta(days=1)
    while following.weekday() < 5:
        if _trading_day(following):
            return False
        following += timedelta(days=1)
    return True
