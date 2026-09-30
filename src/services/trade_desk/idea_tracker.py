"""Forward track record for trade ideas and breakout alerts (research only; nothing is traded).

The backtest (``backtest.swing``) can replay the deterministic rules but not the
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

``verdict`` records follow the stock reports' calls the same way (one per US stock
and day, the first report of the day, at its price): bullish (buy/add/hold), watch,
bearish (reduce/sell/avoid). They have no stop or target, so they settle on the
close 5 and 10 sessions later against SPY, and the summary splits them by the NX
slow tunnel at the report (above / inside / below). The after-close job reads the
report history itself, so older reports are picked up too.

``social`` records are the evening social scan's most-discussed names and
``influencer`` records the picks of the YouTube channels you follow
(``src/services/social_scan.py``, ``src/services/youtube_picks.py``). They enter at
the close of their signal day (read from the bars; no look-ahead) and settle like
the report calls, 5, 10 and 20 sessions later against SPY.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import trend

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
MAX_DAYS = 15
COST_BPS = 5.0
WINDOW_DAYS = 90
VERDICT_HORIZONS = (5, 10)
HORIZONS = {"verdict": VERDICT_HORIZONS, "social": (5, 10, 20), "influencer": (5, 10, 20)}
VERDICT_DAYS = 45  # how far back the after-close job reads the report history
NO_DATA_DAYS = 30  # a record still without bars this long after its signal closes as "no_data"
BULLISH_ACTIONS, BEARISH_ACTIONS = {"buy", "add", "hold"}, {"reduce", "sell", "avoid"}
VERDICT_GROUPS = (("bullish", "Bullish calls"), ("watch", "Watch"), ("bearish", "Bearish calls"))
NX_SLOW_STATES = ("above", "inside", "below")
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


def verdict_record(report: Any) -> Optional[tuple]:
    """(record id, record) for one stored stock report, or None (not a US stock call, no price)."""
    import json
    import re
    code = str(getattr(report, "code", "") or "").upper()
    if getattr(report, "report_type", "") == "market_review" or not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", code):
        return None
    try:
        raw = json.loads(getattr(report, "raw_result", None) or "{}")
    except (TypeError, ValueError):
        return None
    action = str(raw.get("action") or "").lower()
    group = "bullish" if action in BULLISH_ACTIONS else "bearish" if action in BEARISH_ACTIONS else (
        "watch" if action == "watch" else None)
    position = ((raw.get("dashboard") or {}).get("data_perspective") or {}).get("price_position") or {}
    price = position.get("current_price") or raw.get("current_price")
    created = getattr(report, "created_at", None)
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if group is None or price <= 0 or created is None:
        return None
    # Reports store naive local time; read it as the server's local time and take the New York date.
    day = (created.astimezone(_NEW_YORK).date().isoformat() if hasattr(created, "astimezone")
           else str(created)[:10])
    direction = {"bullish": "long", "bearish": "short"}.get(group, "none")
    return f"verdict:{day}:{code}", {
        "kind": "verdict", "verdict": action, "group": group, "ticker": code, "direction": direction,
        "signal_day": day, "entry": round(price, 4), "score": getattr(report, "sentiment_score", None),
        "report_id": getattr(report, "id", None)}


def recent_reports(db: Any, days: int = VERDICT_DAYS, known: Optional[set] = None) -> List[Any]:
    """Stock reports of the last ``days`` whose day's call is not tracked yet.

    Reads ids, codes and times first and loads ``raw_result`` only for the new days,
    instead of whole report rows (news and context snapshots included).
    """
    import re
    from types import SimpleNamespace

    from sqlalchemy import select

    from src.storage import AnalysisHistory
    known = known or set()
    cutoff = datetime.now() - timedelta(days=days)  # created_at is naive local time
    with db.get_session() as session:
        heads = session.execute(select(AnalysisHistory.id, AnalysisHistory.code, AnalysisHistory.created_at)
                                .where(AnalysisHistory.created_at >= cutoff,
                                       AnalysisHistory.report_type != "market_review")).all()
        wanted = []
        for report_id, code, created in heads:
            code = str(code or "").upper()
            if not created or not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", code):
                continue
            if f"verdict:{created.astimezone(_NEW_YORK).date().isoformat()}:{code}" not in known:
                wanted.append(report_id)
        rows = []
        for start in range(0, len(wanted), 500):
            rows += session.execute(select(AnalysisHistory.id, AnalysisHistory.code, AnalysisHistory.report_type,
                                           AnalysisHistory.sentiment_score, AnalysisHistory.created_at,
                                           AnalysisHistory.raw_result)
                                    .where(AnalysisHistory.id.in_(wanted[start:start + 500]))).all()
    return [SimpleNamespace(**row._mapping) for row in rows]


def sync_verdicts(repo: Any, reports: List[Any]) -> int:
    """Track every stored report's call not tracked yet; the first report of a day wins. Returns how many were new."""
    added = 0
    ordered = sorted(reports, key=lambda report: str(getattr(report, "created_at", "")))
    for report in ordered:
        made = verdict_record(report)
        if made and repo.track_idea(*made):
            added += 1
    return added


def settle_verdict(record: Dict[str, Any], bars: List[Dict[str, Any]], market: List[Dict[str, Any]],
                   today: date, horizons: tuple = VERDICT_HORIZONS) -> Optional[Dict[str, Any]]:
    """Returns after each horizon (stock and SPY); "closed" once the last one is in.

    A record without an entry price enters at the close of its signal day.
    """
    path = [bar for bar in bars if record["signal_day"] < bar["date"] <= today.isoformat()]
    if not path:
        return None
    update: Dict[str, Any] = {}
    entry = record.get("entry")
    if entry is None:
        signal_bar = [bar for bar in bars if bar["date"] == record["signal_day"]]
        if not signal_bar:
            return None  # no close on the signal day (a gap in the data): no fair entry
        entry = update["entry"] = round(float(signal_bar[0]["close"]), 4)
    entry = float(entry)
    before = [bar for bar in market if bar["date"] <= record["signal_day"]]
    last = max(horizons)
    update.update({"days": min(len(path), last),
                   "mark_return_pct": round((path[min(len(path), last) - 1]["close"] / entry - 1) * 100, 3)})
    for horizon in horizons:
        if len(path) < horizon:
            continue
        end = path[horizon - 1]
        update[f"return_{horizon}d_pct"] = round((end["close"] / entry - 1) * 100, 3)
        spy = [bar for bar in market if record["signal_day"] < bar["date"] <= end["date"]]
        if spy and before:
            update[f"spy_{horizon}d_pct"] = round((spy[-1]["close"] / before[-1]["close"] - 1) * 100, 3)
    if len(path) >= last:
        update.update(status="closed", return_pct=update[f"return_{last}d_pct"], reason="time",
                      exit_day=path[last - 1]["date"], spy_return_pct=update.get(f"spy_{last}d_pct"))
    return update


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


def nx_snapshot(bars: List[Dict[str, Any]], signal_day: str, price: float, direction: str) -> Optional[Dict[str, Any]]:
    """NX tunnels from the bars before ``signal_day``, read at the alert price; None without enough history."""
    from .moomoo_indicators import NX_MIN_BARS, nx, tunnel_state, tunnel_structure
    history = [bar for bar in bars if str(bar["date"])[:10] < signal_day]
    if len(history) < NX_MIN_BARS or not price:
        return None
    lines = nx(history)
    fast = tunnel_state(price, lines["A"][-1], lines["B"][-1])
    slow = tunnel_state(price, lines["A1"][-1], lines["B1"][-1])
    structure = tunnel_structure(lines)
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

    def bars_for(source, ticker):  # download_bars keys use dots (BRK.B) whatever the request used
        return source.get(ticker) or source.get(str(ticker).replace("-", "."))

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
        kind = record.get("kind")
        if kind in HORIZONS:
            update = settle_verdict(record, bars_for(history, record["ticker"]) or [], market, today, HORIZONS[kind])
        else:
            update = settle(record, bars_for(history, record["ticker"]) or [], market, today)
        nx_update = {}
        entry = record.get("entry") or (update or {}).get("entry")
        if "nx" not in record and entry and bars_for(long_history, record["ticker"]):
            nx_update["nx"] = nx_snapshot(bars_for(long_history, record["ticker"]), record["signal_day"],
                                          float(entry), record["direction"])
            if nx_update["nx"] and record["direction"] == "none":
                nx_update["nx"]["alignment"] = None  # "watch" takes no side
        if update is None and (today - date.fromisoformat(str(record["signal_day"])[:10])).days > NO_DATA_DAYS:
            update = {"status": "closed", "reason": "no_data"}  # delisted or unknown: stop re-downloading it
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
    # created_at is stored in UTC ISO text; compare like with like.
    since = ((now or datetime.now(_NEW_YORK)) - timedelta(days=window_days)).astimezone(timezone.utc).isoformat()
    everything = repo.tracked_ideas(since=since, limit=1_000_000)
    verdicts = [r for r in everything if r.get("kind") == "verdict"]
    records = [r for r in everything if r.get("kind") in (None, "idea", "breakout")]
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
    from src.services.youtube_picks import channel_stats
    return {"window_days": window_days, "groups": groups, "by_nx": by_nx, "recent": recent,
            "verdicts": verdict_stats(verdicts),
            "social": social_stats([r for r in everything if r.get("kind") == "social"]),
            "scoreboard": scoreboard(everything),
            "influencers": channel_stats([r for r in everything if r.get("kind") == "influencer"])}


SCOREBOARD_MIN = 30  # closed records before a source gets a verdict


def _excess(record: Dict[str, Any], stock_key: str, spy_key: str) -> Optional[float]:
    """Return vs SPY in the call's direction: a short or bearish call gains when it lags SPY."""
    stock, spy = record.get(stock_key), record.get(spy_key)
    if stock is None or spy is None:
        return None
    if record.get("kind") in (None, "idea", "breakout"):  # return_pct already carries the trade's direction
        return stock - (1 if record.get("direction") == "long" else -1) * spy
    return (stock - spy) * (-1 if record.get("direction") == "short" else 1)


def scoreboard(everything: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every tracked source side by side: closed count, average result vs SPY in the call's direction,
    a month-clustered t, and a verdict ("too_early" under SCOREBOARD_MIN closed, else "ahead" /
    "behind" when |t| >= 2, else "no_difference")."""
    import math

    def closed(rows):
        return [r for r in rows if r["status"] == "closed"]

    sources = []

    def add(key: str, label: str, rows: List[Dict[str, Any]], stock_key: str, spy_key: str, horizon: str):
        done = [(r, _excess(r, stock_key, spy_key)) for r in closed(rows)]
        values = [(r, x) for r, x in done if x is not None]
        by_month: Dict[str, List[float]] = {}
        for record, value in values:
            by_month.setdefault(str(record.get("signal_day", ""))[:7], []).append(value)
        means = [sum(v) / len(v) for v in by_month.values()]
        mean = sum(x for _, x in values) / len(values) if values else None
        t = None
        if len(means) >= 2:
            centre = sum(means) / len(means)
            spread = math.sqrt(sum((m - centre) ** 2 for m in means) / (len(means) - 1))
            t = centre / (spread / math.sqrt(len(means))) if spread > 0 else None
        verdict = ("too_early" if len(values) < SCOREBOARD_MIN else
                   "ahead" if t is not None and t >= 2 and (mean or 0) > 0 else
                   "behind" if t is not None and t <= -2 and (mean or 0) < 0 else "no_difference")
        sources.append({"key": key, "label": label, "horizon": horizon, "closed": len(values),
                        "open": sum(1 for r in rows if r["status"] == "open"),
                        "avg_vs_spy_pct": round(mean, 3) if mean is not None else None,
                        "t": round(t, 2) if t is not None else None, "verdict": verdict})
    ideas = [r for r in everything if r.get("kind") in (None, "idea")]
    for verdict, label in (("high", "Trade ideas · high conviction"), ("medium", "Trade ideas · medium conviction"),
                           ("rejected", "Candidates the review rejected")):
        add(f"idea:{verdict}", label, [r for r in ideas if r.get("verdict") == verdict],
            "return_pct", "spy_return_pct", "to stop/target, ≤15 sessions")
    add("breakout", "Breakout alerts", [r for r in everything if r.get("kind") == "breakout"],
        "return_pct", "spy_return_pct", "to stop/target, ≤15 sessions")
    trades = [r for r in everything if r.get("kind") in (None, "idea", "breakout")]
    for alignment, label in (("agree", "Ideas & breakouts · NX agrees"), ("against", "Ideas & breakouts · NX against")):
        add(f"nx:{alignment}", label, [r for r in trades if (r.get("nx") or {}).get("alignment") == alignment],
            "return_pct", "spy_return_pct", "to stop/target, ≤15 sessions")
    verdicts = [r for r in everything if r.get("kind") == "verdict"]
    for group, label in (("bullish", "Report calls · bullish"), ("bearish", "Report calls · bearish")):
        add(f"verdict:{group}", label, [r for r in verdicts if r.get("group") == group],
            "return_10d_pct", "spy_10d_pct", "10 sessions")
    add("social", "Most discussed on social media", [r for r in everything if r.get("kind") == "social"],
        "return_20d_pct", "spy_20d_pct", "20 sessions")
    influencer = [r for r in everything if r.get("kind") == "influencer"]
    for channel in sorted({r.get("channel") or "?" for r in influencer}):
        add(f"youtube:{channel}", f"YouTube · {channel}", [r for r in influencer if (r.get("channel") or "?") == channel],
            "return_20d_pct", "spy_20d_pct", "20 sessions")
    return [item for item in sources if item["closed"] or item["open"]]


def social_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The social scan's names: how many, and their average return and return vs SPY after 5/10/20 sessions."""
    def average(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None
    out: Dict[str, Any] = {"names": len(rows), "closed": sum(1 for r in rows if r["status"] == "closed"
                                                              and r.get("return_20d_pct") is not None)}
    for horizon in (5, 10, 20):
        done = [r for r in rows if r.get(f"return_{horizon}d_pct") is not None and r.get(f"spy_{horizon}d_pct") is not None]
        out[f"{horizon}d"] = {"count": len(done), "avg_pct": average([r[f"return_{horizon}d_pct"] for r in done]),
                              "vs_spy_pct": average([r[f"return_{horizon}d_pct"] - r[f"spy_{horizon}d_pct"] for r in done])}
    return out


def verdict_stats(verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Report calls by group and NX slow-tunnel state: closed count, 5- and 10-session return, 10-session vs SPY."""
    def average(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None
    out: Dict[str, Any] = {}
    for group, label in VERDICT_GROUPS:
        rows = [r for r in verdicts if r.get("group") == group]
        cells = {}
        for state in (*NX_SLOW_STATES, "all"):
            chosen = [r for r in rows if state == "all" or (r.get("nx") or {}).get("slow") == state]
            closed = [r for r in chosen if r["status"] == "closed" and r.get("return_10d_pct") is not None]
            still_open = [r for r in chosen if r["status"] == "open"]
            cells[state] = {"closed": len(closed), "open": len(still_open),
                            "avg_5d_pct": average([r.get("return_5d_pct") for r in chosen]),
                            "avg_10d_pct": average([r.get("return_10d_pct") for r in closed]),
                            "avg_10d_vs_spy_pct": average([r["return_10d_pct"] - r["spy_10d_pct"] for r in closed
                                                           if r.get("return_10d_pct") is not None
                                                           and r.get("spy_10d_pct") is not None])}
        out[group] = {"label": label, "by_nx": cells}
    return out


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
    verdicts = stats.get("verdicts") or {}
    closed_verdicts = sum(group["by_nx"]["all"]["closed"] for group in verdicts.values())
    if closed_verdicts:
        lines.append("Report calls, 10 sessions later vs SPY, by where the price sat against your NX slow tunnel:")
        for group in verdicts.values():
            cells = group["by_nx"]
            parts = [f"{state} {cells[state]['closed']} · {_pct(cells[state]['avg_10d_vs_spy_pct'])}"
                     for state in NX_SLOW_STATES if cells[state]["closed"]]
            if parts:
                lines.append(f"• {group['label']}: " + " | ".join(parts))
        if closed_verdicts < 60:
            lines.append("Few closed calls so far; read this as a first look.")
    board = stats.get("scoreboard") or []
    if board:
        judged = [item for item in board if item["verdict"] in ("ahead", "behind")]
        if judged:
            lines.append("What's working so far: " + "; ".join(
                f"{item['label']} {'ahead of' if item['verdict'] == 'ahead' else 'behind'} SPY "
                f"({item['avg_vs_spy_pct']:+.2f}%, {item['closed']} closed)" for item in judged))
        else:
            lines.append(f"What's working: no source has {SCOREBOARD_MIN} closed records with a clear difference from SPY yet.")
    social = stats.get("social") or {}
    if social.get("5d", {}).get("count"):
        parts = [f"{h} sessions {social[f'{h}d']['count']} · vs SPY {_pct(social[f'{h}d']['vs_spy_pct'])}"
                 for h in (5, 10, 20) if social[f"{h}d"]["count"]]
        lines.append("Most-discussed names on social media (from the close they were listed): " + " | ".join(parts)
                     + ("" if social["5d"]["count"] >= 50 else " — too few to judge yet"))
    influencers = stats.get("influencers") or {}
    counted = [(name, row) for name, row in influencers.items() if row["counted"][5]]
    if counted:
        lines.append("YouTube picks, in the call's direction vs SPY (5 / 10 / 20 sessions):")
        for name, row in sorted(counted, key=lambda item: -item[1]["picks"]):
            lines.append(f"• {name}: {row['picks']} picks ({row['bullish']} bullish, {row['bearish']} bearish) · "
                         + " / ".join(_pct(row["vs_spy"][h]) for h in (5, 10, 20))
                         + f" · {row['closed']} closed")
        if sum(row["closed"] for _name, row in counted) < 30:
            lines.append("Few closed picks so far; read this as a first look.")
    return "\n".join(lines)


class TrackerJob:
    """Settles open records after each close and posts the weekly summary (worker leader only)."""

    SETTLE_AFTER = (16, 30)

    def __init__(self, repo: Any, emit: Callable[[str, Dict[str, Any], str], Any], *,
                 bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                 nx_bars: Optional[Callable[[List[str]], Dict[str, List[Dict[str, Any]]]]] = None,
                 reports: Optional[Callable[[], List[Any]]] = None):
        from concurrent.futures import ThreadPoolExecutor
        self.repo = repo
        self._emit = emit
        self._bars = bars
        self._nx_bars = nx_bars
        self._reports = reports or (lambda: recent_reports(repo.db, VERDICT_DAYS, repo.tracked_idea_ids("verdict:")))
        self._retry_after: Optional[datetime] = None
        self._done_day: Optional[date] = None
        self.last_error: Optional[str] = None
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
        if self._retry_after is not None and now < self._retry_after:
            return
        self._task = self._pool.submit(self._run, day, now)

    def _run(self, day: date, now: datetime) -> None:
        try:
            try:
                added = sync_verdicts(self.repo, self._reports())
                logger.info("Idea tracker: %d report calls added", added)
            except Exception as exc:  # the report calls are picked up tomorrow
                logger.warning("Idea tracker could not read report calls: %s", type(exc).__name__)
            closed = settle_open(self.repo, day, self._bars, self._nx_bars)
            logger.info("Idea tracker: %d records closed", closed)
            if _last_trading_day_of_week(day):
                stats = track_record(self.repo, now)
                verdicts = (stats.get("verdicts") or {}).values()
                if any(group["closed"] or group["open"] for group in stats["groups"].values()) or any(
                        group["by_nx"]["all"]["closed"] for group in verdicts) or (
                        (stats.get("social") or {}).get("5d", {}).get("count")) or any(
                        row["counted"][5] for row in (stats.get("influencers") or {}).values()):
                    iso = day.isocalendar()
                    self._emit("track_record", {"underlying": "", "message": format_track_record(stats)},
                               f"track-record:{iso[0]}-{iso[1]}")
            self._done_day, self._retry_after, self.last_error = day, None, None
        except Exception as exc:  # retried in 30 minutes (the weekly summary is keyed per week); records stay open
            self.last_error = type(exc).__name__
            logger.warning("Idea tracker failed: %s", type(exc).__name__)
            self._retry_after = now + timedelta(minutes=30)


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
