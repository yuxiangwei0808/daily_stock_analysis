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

The summary grades fairly (``_split_ideas``, ``peer_baseline``). A scan relists a live idea every slot,
so each idea counts once; and it counts only with all ``MAX_DAYS`` sessions behind it, because a stop
closes within days while a winner can take weeks. The yardstick is the average universe stock entered
the same day, same direction, same stop/target rule (or over the same sessions for report calls and
picks), not SPY: in a rotation the index and the average stock can move in opposite directions.
Report calls also keep ``model_call``, the model's own call before any guard, so a guard's effect can
be measured instead of assumed.
"""
from __future__ import annotations

import bisect
import logging
import math
import os
import re
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
ENTRY_AT_CLOSE = {"social", "influencer"}  # entered at the signal day's close, read from the bars
HALTED_SESSIONS = 5  # a ticker this many sessions behind SPY's latest bar has stopped trading
VERDICT_DAYS = 45  # how far back the after-close job reads the report history
NO_DATA_DAYS = 30  # a record still without bars this long after its signal closes as "no_data"
BULLISH_ACTIONS, BEARISH_ACTIONS = {"buy", "add", "hold"}, {"reduce", "sell", "avoid"}
VERDICT_GROUPS = (("bullish", "Bullish calls"), ("watch", "Watch"), ("bearish", "Bearish calls"))
NX_SLOW_STATES = ("above", "inside", "below")
NX_GROUPS = (("agree", "NX agrees"), ("neutral", "NX neutral"), ("against", "NX against"))
GROUPS = (("high", "Approved · high"), ("medium", "Approved · medium"), ("rejected", "Rejected by the review"),
          ("breakout", "Breakout alerts"))
APPROVED = {"high", "medium"}
PEERS_SETTING = "idea_peer_baselines"  # signal day -> what the average universe stock did from that day
PEER_HORIZONS = (5, 10, 20)
PEER_MIN_STOCKS = 200  # fewer than this is not "the average stock"
PEER_PERIOD = "1y"
PEER_LOOKBACK_DAYS = 300  # older signal days fall outside that download (with the ATR's history before them)
REVIEW_MIN_DAYS = 10  # days with both approved and rejected finished ideas before the review is judged
_MODEL_DECISION = re.compile(r'"decision_type"\s*:\s*"(buy|hold|sell)"', re.I)


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
        "report_id": getattr(report, "id", None), **_report_fields(raw)}


def _report_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The model's own call before any guard (from its stored answer) and whether the ticker is a geared fund."""
    from .opportunities import geared_fund
    found = _MODEL_DECISION.search(str(raw.get("raw_response") or ""))
    return {"model_call": found.group(1).lower() if found else None, "geared": geared_fund(str(raw.get("name") or ""))}


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


def fill_model_calls(repo: Any) -> int:
    """Report calls tracked before ``model_call`` existed read it from their stored report, once."""
    import json

    from sqlalchemy import select

    from src.storage import AnalysisHistory
    missing = [r for r in repo.tracked_ideas(limit=1_000_000) if r.get("kind") == "verdict" and "model_call" not in r]
    ids = [r["report_id"] for r in missing if r.get("report_id")]
    answers: Dict[Any, Dict[str, Any]] = {}
    if ids:
        with repo.db.get_session() as session:
            for start in range(0, len(ids), 500):
                for row in session.execute(select(AnalysisHistory.id, AnalysisHistory.raw_result)
                                           .where(AnalysisHistory.id.in_(ids[start:start + 500]))).all():
                    try:
                        answers[row.id] = json.loads(row.raw_result or "{}")
                    except (TypeError, ValueError):
                        continue
    updates = [(r["id"], {**{key: value for key, value in r.items() if key not in {"id", "status", "created_at"}},
                          **_report_fields(answers.get(r.get("report_id")) or {})}) for r in missing]
    if updates:
        repo.update_tracked_ideas(updates)
    return len(updates)


def _aligned_returns(bars, market, start_day, end_day):
    """Raw stock/SPY returns over the exact same closing-price window."""
    def change(rows):
        closes = {str(bar["date"])[:10]: bar.get("close") for bar in rows}
        start, end = closes.get(start_day), closes.get(end_day)
        return round((end / start - 1) * 100, 3) if start and end and start > 0 and end > 0 else None
    stock, spy = change(bars), change(market)
    return (stock, spy) if stock is not None and spy is not None else (None, None)


def settle_verdict(record: Dict[str, Any], bars: List[Dict[str, Any]], market: List[Dict[str, Any]],
                   today: date, horizons: tuple = VERDICT_HORIZONS, halted: bool = False) -> Optional[Dict[str, Any]]:
    """Returns after each horizon (stock and SPY); "closed" once the last one is in.

    A record of a kind that enters at the close (social, YouTube) reads that close from the same
    download every time, so a later split rescaling the history cannot skew it. ``halted`` closes a
    record whose prices stopped: the horizons not reached take the last price.
    """
    path = [bar for bar in bars if record["signal_day"] < bar["date"] <= today.isoformat()]
    if not path:
        return None
    update: Dict[str, Any] = {}
    entry = None if record.get("kind") in ENTRY_AT_CLOSE else record.get("entry")
    if entry is None:
        signal_bar = [bar for bar in bars if bar["date"] == record["signal_day"]]
        if not signal_bar:
            return None  # no close on the signal day (a gap in the data): no fair entry
        entry = update["entry"] = round(float(signal_bar[0]["close"]), 4)
    entry = float(entry)
    last = max(horizons)
    update.update({"days": min(len(path), last),
                   "mark_return_pct": round((path[min(len(path), last) - 1]["close"] / entry - 1) * 100, 3)})
    for horizon in horizons:
        if len(path) < horizon and not halted:
            continue
        end = path[min(horizon, len(path)) - 1]
        update[f"return_{horizon}d_pct"] = round((end["close"] / entry - 1) * 100, 3)
        stock_ret, spy_ret = _aligned_returns(bars, market, record["signal_day"], end["date"])
        update[f"benchmark_return_{horizon}d_pct"] = stock_ret
        update[f"spy_{horizon}d_pct"] = spy_ret
    if len(path) >= last or halted:
        update.update(status="closed", return_pct=update[f"return_{last}d_pct"], reason="time" if not halted else "halted",
                      exit_day=path[min(last, len(path)) - 1]["date"], spy_return_pct=update.get(f"spy_{last}d_pct"),
                      benchmark_return_pct=update.get(f"benchmark_return_{last}d_pct"))
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
    stock_ret, spy_ret = _aligned_returns(bars, market, record["signal_day"], exit_day or last["date"])
    update = {"days": min(len(path), MAX_DAYS), "return_pct": round(ret, 3),
              "r": round(ret / risk, 3) if risk else None, "spy_return_pct": round(spy_ret, 3) if spy_ret is not None else None,
              "mark": round(mark, 4), "benchmark_return_pct": stock_ret}
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
    legacy = [r for r in repo.tracked_ideas(status="closed", since=(today - timedelta(days=90)).isoformat())
              if "benchmark_return_pct" not in r and r.get("exit_day") and r.get("return_pct") is not None]
    if not open_records and not legacy:
        return 0
    history = bars(list(dict.fromkeys(["SPY", *(record["ticker"] for record in [*open_records, *legacy])])))

    def bars_for(source, ticker):  # download_bars keys use dots (BRK.B) whatever the request used
        return source.get(ticker) or source.get(str(ticker).replace("-", "."))

    market, closed = history.get("SPY") or [], 0
    for record in legacy:
        stock_bars = bars_for(history, record["ticker"]) or []
        if record.get("kind") in HORIZONS:
            calculated = settle_verdict(record, stock_bars, market, today, HORIZONS[record["kind"]],
                                        halted=record.get("reason") == "halted") or {}
            update = {key: value for key, value in calculated.items() if key.startswith(("benchmark_", "spy_"))}
        else:
            stock_ret, spy_ret = _aligned_returns(stock_bars, market, record["signal_day"], record["exit_day"])
            update = {"benchmark_return_pct": stock_ret, "spy_return_pct": spy_ret}
        if update.get("benchmark_return_pct") is not None:
            payload = {key: value for key, value in record.items() if key not in {"id", "status", "created_at"}}
            repo.update_tracked_idea(record["id"], {**payload, **update}, "closed")

    if not market:  # a record closed now would have no vs-SPY figures for good; NX states are still filled
        logger.info("Idea tracker: no SPY bars today; settling waits for the next run")
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
        stock_bars = bars_for(history, record["ticker"]) or []
        halted = _halted(stock_bars, market, record["signal_day"], today)
        priced, ref_update = _split_safe(record, stock_bars)
        if not market:
            update = None
        elif kind in HORIZONS:
            update = settle_verdict(priced, stock_bars, market, today, HORIZONS[kind], halted=halted)
        else:
            update = settle(priced, stock_bars, market, today)
            if update is not None and halted and update.get("status") != "closed":
                update.update(status="closed", reason="halted", exit=update.get("mark"),
                              exit_day=stock_bars[-1]["date"])
        if update is not None:
            update.update(ref_update)
            if kind not in ENTRY_AT_CLOSE:
                update.pop("entry", None)  # the stored alert price stays as it was printed
        nx_update = {}
        entry = record.get("entry") or (update or {}).get("entry")
        if "nx" not in record and entry and bars_for(long_history, record["ticker"]):
            nx_update["nx"] = nx_snapshot(bars_for(long_history, record["ticker"]), record["signal_day"],
                                          float(entry), record["direction"])
            if nx_update["nx"] and record["direction"] == "none":
                nx_update["nx"]["alignment"] = None  # "watch" takes no side
        if (update is None and market  # with no SPY either, the download failed: nothing is "no data"
                and (today - date.fromisoformat(str(record["signal_day"])[:10])).days > NO_DATA_DAYS):
            update = {"status": "closed", "reason": "no_data"}  # delisted or unknown: stop re-downloading it
        if update is None:
            # The signal day's close is kept now, before a split can rescale tomorrow's download.
            if nx_update or ref_update:
                payload = {key: value for key, value in record.items() if key not in {"id", "status", "created_at"}}
                repo.update_tracked_idea(record["id"], {**payload, **nx_update, **ref_update}, record["status"])
            continue
        update.update(nx_update)
        status = update.pop("status", "open")
        payload = {key: value for key, value in record.items() if key not in {"id", "status", "created_at"}}
        repo.update_tracked_idea(record["id"], {**payload, **update}, status)
        closed += status == "closed"
    return closed


def _session_days(first: str, today: date) -> List[str]:
    """Trading days from ``first`` through ``today``, as ISO dates."""
    cursor, days = date.fromisoformat(first), []
    while cursor <= today:
        if _trading_day(cursor):
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def peer_baseline(day: str, histories: List[Dict[str, Any]], today: date) -> Dict[str, Any]:
    """What the average universe stock did from ``day``'s close: ``long`` / ``short`` under the ideas' rule
    (the 1.5 / 3 ATR stop and target, the session limit, the same settlement and costs) once every session
    is in, and ``h5`` ... ``h20`` close to close. ``histories`` holds {"bars", "dates"} per stock."""
    from . import levels
    from .backtest.swing import STOP_ATR, TARGET_ATR
    trades: Dict[str, List[float]] = {"long": [], "short": []}
    moves: Dict[int, List[float]] = {horizon: [] for horizon in PEER_HORIZONS}
    for item in histories:
        bars, position = item["bars"], bisect.bisect_right(item["dates"], day)
        if position - 2 < levels.ATR_PERIOD or (date.fromisoformat(day) - date.fromisoformat(item["dates"][position - 1])).days > 5:
            continue  # too little history, or a stock not trading then
        close = float(bars[position - 1]["close"])
        path = [bar for bar in bars[position:position + max(MAX_DAYS, *PEER_HORIZONS)] if bar["date"] <= today.isoformat()]
        if close <= 0:
            continue
        for horizon in PEER_HORIZONS:
            if len(path) >= horizon:
                moves[horizon].append((path[horizon - 1]["close"] / close - 1) * 100)
        atr = levels.atr(bars, position - 2)  # bars before the signal day, like the live levels
        if len(path) < MAX_DAYS or atr <= 0:
            continue
        for direction, sign in (("long", 1), ("short", -1)):
            record = {"signal_day": day, "direction": direction, "entry": close,
                      "stop": close - sign * STOP_ATR * atr, "target": close + sign * TARGET_ATR * atr}
            settled = settle(record, path[:MAX_DAYS], [], today)
            if settled and settled.get("status") == "closed":
                trades[direction].append(settled["return_pct"])
    out: Dict[str, Any] = {}
    for key, values in [*trades.items(), *((f"h{horizon}", values) for horizon, values in moves.items())]:
        if len(values) >= PEER_MIN_STOCKS:
            out[key] = round(sum(values) / len(values), 3)
    return out


def update_peer_baselines(repo: Any, today: date, bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]],
                          universe: Callable[[], List[str]]) -> int:
    """Measure every tracked signal day that has reached a new horizon; returns how many days changed."""
    stored = dict(repo.setting(PEERS_SETTING, {}) or {})
    days = sorted({str(r["signal_day"])[:10] for r in repo.tracked_ideas(limit=1_000_000) if r.get("signal_day")})
    if not days:
        return 0
    sessions = _session_days(days[0], today)

    def outstanding(day: str) -> bool:
        passed = len(sessions) - bisect.bisect_right(sessions, day)
        keys = [f"h{horizon}" for horizon in PEER_HORIZONS if passed >= horizon]
        keys += ["long", "short"] if passed >= MAX_DAYS else []
        return any(key not in stored.get(day, {}) for key in keys)

    oldest = (today - timedelta(days=PEER_LOOKBACK_DAYS)).isoformat()
    wanted = [day for day in days if day >= oldest and outstanding(day)]
    tickers = universe() if wanted else []
    if not tickers:
        return 0
    download = bars(tickers)
    rows = [download.get(str(ticker).replace("-", ".")) for ticker in tickers]
    histories = [{"bars": row, "dates": [bar["date"] for bar in row]} for row in rows if row]
    changed = 0
    for day in wanted:
        measured = peer_baseline(day, histories, today)
        if any(stored.get(day, {}).get(key) != value for key, value in measured.items()):
            stored[day] = {**stored.get(day, {}), **measured}
            changed += 1
    if changed:
        repo.set_setting(PEERS_SETTING, stored)
    return changed


def _us_universe() -> List[str]:
    from src.services.screening.snapshot_us import fetch_us_universe
    return fetch_us_universe(os.getenv("SCREENING_US_UNIVERSE", "auto"))


def _split_ideas(rows: List[Dict[str, Any]], today: date) -> tuple:
    """(finished, running) trade records: each idea once, finished only with all its sessions behind it.

    A repeat of the same ticker, direction and family (approved / rejected / breakout) within MAX_DAYS
    sessions of the idea's first record is the same idea. A record closed without a result is dropped.
    """
    rows = [r for r in rows if r.get("signal_day")]
    if not rows:
        return [], []
    ordered = sorted(rows, key=lambda r: (str(r.get("signal_day")), str(r.get("slot") or ""), str(r.get("created_at") or "")))
    sessions = _session_days(str(ordered[0]["signal_day"])[:10], today)
    first_seen: Dict[tuple, int] = {}
    finished, running = [], []
    for record in ordered:
        position = bisect.bisect_right(sessions, str(record["signal_day"])[:10])
        family = "approved" if record.get("verdict") in APPROVED else record.get("verdict")
        key = (record.get("ticker"), record.get("direction"), family)
        if key in first_seen and position - first_seen[key] <= MAX_DAYS:
            continue
        first_seen[key] = position
        if record["status"] == "closed" and record.get("return_pct") is None:
            continue
        done = record["status"] == "closed" and len(sessions) - position >= MAX_DAYS
        (finished if done else running).append(record)
    return finished, running


def _stock_calls(everything: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Report calls on stocks: a geared fund's move is not comparable to the average stock's or the watchlist's."""
    return [r for r in everything if r.get("kind") == "verdict" and not r.get("geared")]


def _trade_vs_peers(record: Dict[str, Any], peers: Dict[str, Any]) -> Optional[float]:
    """A trade's result minus the average stock entered the same day, same direction and rule."""
    peer = (peers.get(str(record.get("signal_day"))[:10]) or {}).get(record.get("direction"))
    value = record.get("return_pct")
    return value - peer if value is not None and peer is not None else None


def _horizon_vs_peers(record: Dict[str, Any], peers: Dict[str, Any], horizon: int) -> Optional[float]:
    """The stock's close-to-close move minus the average stock's over the same sessions."""
    peer = (peers.get(str(record.get("signal_day"))[:10]) or {}).get(f"h{horizon}")
    stock = record.get(f"benchmark_return_{horizon}d_pct")
    return stock - peer if stock is not None and peer is not None else None


def _month_t(values: List[tuple]) -> tuple:
    """(mean, month-clustered t, months) of (signal_day, value) pairs."""
    by_month: Dict[str, List[float]] = {}
    for day, value in values:
        by_month.setdefault(str(day)[:7], []).append(value)
    means = [sum(v) / len(v) for v in by_month.values()]
    mean = sum(value for _, value in values) / len(values) if values else None
    t = None
    if len(means) >= 2:
        centre = sum(means) / len(means)
        spread = math.sqrt(sum((m - centre) ** 2 for m in means) / (len(means) - 1))
        t = centre / (spread / math.sqrt(len(means))) if spread > 0 else None
    return mean, t, len(means)


def _halted(bars: List[Dict[str, Any]], market: List[Dict[str, Any]], signal_day: str, today: date) -> bool:
    """The ticker's prices stopped (a halt, a delisting) while SPY's went on for HALTED_SESSIONS sessions."""
    if not bars or not market or bars[-1]["date"] <= signal_day:
        return False
    return sum(1 for bar in market if bars[-1]["date"] < bar["date"] <= today.isoformat()) >= HALTED_SESSIONS


def _split_safe(record: Dict[str, Any], bars: List[Dict[str, Any]]) -> tuple:
    """(record priced on today's download, fields to store). A split or dividend adjusts the whole
    downloaded history, so a price stored earlier no longer matches it: the signal-day close is kept as
    ``ref_close`` at the first settle, and later settles scale the stored levels by how that close moved."""
    if record.get("kind") in ENTRY_AT_CLOSE:
        return record, {}  # read from the bars each time
    close = next((float(bar["close"]) for bar in bars if bar["date"] == record["signal_day"]), None)
    if close is None or close <= 0:
        return record, {}
    reference = record.get("ref_close")
    if not reference:
        return record, {"ref_close": round(close, 6)}
    scale = close / float(reference)
    if abs(scale - 1) < 1e-6:
        return record, {}
    priced = dict(record)
    for key in ("entry", "stop", "target"):
        if priced.get(key) is not None:
            priced[key] = float(priced[key]) * scale
    return priced, {}


def track_record(repo: Any, now: Optional[datetime] = None, window_days: int = WINDOW_DAYS) -> Dict[str, Any]:
    # created_at is stored in UTC ISO text; compare like with like.
    since = ((now or datetime.now(_NEW_YORK)) - timedelta(days=window_days)).astimezone(timezone.utc).isoformat()
    everything = repo.tracked_ideas(since=since, limit=1_000_000)
    peers = repo.setting(PEERS_SETTING, {}) or {}
    today = _day(now)
    verdicts = _stock_calls(everything)
    records = [r for r in everything if r.get("kind") in (None, "idea", "breakout")]
    finished, running = _split_ideas(records, today)

    def average(values):
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None

    groups = {}
    for key, label in GROUPS:
        done = [r for r in finished if r.get("verdict") == key]
        live = [r for r in running if r.get("verdict") == key]
        returns = [r["return_pct"] for r in done]
        groups[key] = {
            "label": label, "open": len(live), "closed": len(done),
            "win_rate": sum(1 for x in returns if x > 0) / len(returns) * 100 if returns else None,
            "avg_return_pct": average(returns),
            "avg_r": (sum(r["r"] for r in done if r.get("r") is not None) / len(done)) if done else None,
            "avg_vs_spy_pct": average([_excess(r, "return_pct", "spy_return_pct") for r in done]),
            "avg_vs_peers_pct": average([_trade_vs_peers(r, peers) for r in done]),
            "running_avg_pct": average([r.get("return_pct") for r in live]),
            "long": sum(1 for r in [*done, *live] if r["direction"] == "long"),
            "short": sum(1 for r in [*done, *live] if r["direction"] == "short"),
        }
    by_nx = {}
    for key, label in NX_GROUPS:
        rows = [r for r in finished if (r.get("nx") or {}).get("alignment") == key]
        returns = [r["return_pct"] for r in rows]
        by_nx[key] = {"label": label, "closed": len(rows),
                      "open": sum(1 for r in running if (r.get("nx") or {}).get("alignment") == key),
                      "win_rate": sum(1 for x in returns if x > 0) / len(returns) * 100 if returns else None,
                      "avg_return_pct": average(returns)}
    recent = [{**{key: r.get(key) for key in ("ticker", "direction", "verdict", "signal_day", "status", "reason",
                                              "return_pct", "r", "spy_return_pct", "days", "kind")},
               "nx_alignment": (r.get("nx") or {}).get("alignment")} for r in records[:60]]
    from src.services.youtube_picks import channel_stats
    return {"window_days": window_days, "groups": groups, "by_nx": by_nx, "recent": recent,
            "review_value": review_value(finished),
            "verdicts": verdict_stats(verdicts, peers),
            "model_calls": model_call_stats(verdicts),
            "social": social_stats([r for r in everything if r.get("kind") == "social"], peers),
            "scoreboard": scoreboard(everything, peers, today),
            "influencers": channel_stats([r for r in everything if r.get("kind") == "influencer"], peers)}


def review_value(finished: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Approved minus rejected finished ideas of the same signal day and direction, month-clustered; the
    comparison that tells whether the review adds anything, free of which days each group happened to fall on."""
    days: Dict[tuple, Dict[str, List[float]]] = {}
    for record in finished:
        side = "approved" if record.get("verdict") in APPROVED else "rejected" if record.get("verdict") == "rejected" else None
        if side:
            cell = days.setdefault((str(record["signal_day"])[:10], record.get("direction")), {"approved": [], "rejected": []})
            cell[side].append(record["return_pct"])
    gaps = [(day, sum(cell["approved"]) / len(cell["approved"]) - sum(cell["rejected"]) / len(cell["rejected"]))
            for (day, _direction), cell in days.items() if cell["approved"] and cell["rejected"]]
    mean, t, months = _month_t(gaps)
    needed = t_needed(months)
    verdict = ("too_early" if len(gaps) < REVIEW_MIN_DAYS else
               "helps" if t is not None and needed and t >= needed and (mean or 0) > 0 else
               "hurts" if t is not None and needed and t <= -needed and (mean or 0) < 0 else "no_difference")
    return {"days": len(gaps), "gap_pct": round(mean, 3) if mean is not None else None,
            "t": round(t, 2) if t is not None else None, "t_needed": needed, "months": months, "verdict": verdict}


SCOREBOARD_MIN = 30  # closed records before a source gets a verdict
# Two-sided 5% critical t by degrees of freedom (months - 1): with a handful of months, |t| >= 2 would
# call a source "ahead" or "behind" on noise far more often than 1 time in 20.
_T_CRITICAL = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
               12: 2.18, 15: 2.13, 20: 2.09, 30: 2.04}


def t_needed(months: int) -> Optional[float]:
    """The |t| a month-clustered mean needs over ``months`` months (None under 2 months)."""
    if months < 2:
        return None
    df = months - 1
    return next((_T_CRITICAL[key] for key in sorted(_T_CRITICAL) if key >= df), 1.96)


def _excess(record: Dict[str, Any], stock_key: str, spy_key: str) -> Optional[float]:
    """Return vs SPY in the call's direction: a short or bearish call gains when it lags SPY."""
    stock, spy = record.get("benchmark_" + stock_key), record.get(spy_key)
    if stock is None or spy is None:
        return None
    return (stock - spy) * (-1 if record.get("direction") == "short" else 1)


def scoreboard(everything: List[Dict[str, Any]], peers: Optional[Dict[str, Any]] = None,
               today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Every tracked source side by side: finished count, average result vs same-day stocks in the call's
    direction, a month-clustered t, and a verdict ("too_early" under SCOREBOARD_MIN finished, else "ahead" /
    "behind" when |t| reaches the 5% critical value for that many months, else "no_difference")."""
    peers = peers or {}
    today = today or _day()
    sources = []

    def add(key: str, label: str, done: List[Dict[str, Any]], still_open: int, compare: Callable[[Dict[str, Any]], Optional[float]],
            stock_key: str, spy_key: str, horizon: str):
        values = [(r, x) for r in done if (x := compare(r)) is not None]
        mean, t, months = _month_t([(r.get("signal_day", ""), x) for r, x in values])
        needed = t_needed(months)
        verdict = ("too_early" if len(values) < SCOREBOARD_MIN or needed is None else
                   "ahead" if t is not None and t >= needed and (mean or 0) > 0 else
                   "behind" if t is not None and t <= -needed and (mean or 0) < 0 else "no_difference")
        vs_spy = [x for r, _ in values if (x := _excess(r, stock_key, spy_key)) is not None]
        sources.append({"key": key, "label": label, "horizon": horizon, "closed": len(values), "open": still_open,
                        "avg_vs_peers_pct": round(mean, 3) if mean is not None else None,
                        "avg_vs_spy_pct": round(sum(vs_spy) / len(vs_spy), 3) if vs_spy else None,
                        "t": round(t, 2) if t is not None else None, "t_needed": needed, "months": months,
                        "verdict": verdict})

    trades = [r for r in everything if r.get("kind") in (None, "idea", "breakout")]
    finished, running = _split_ideas(trades, today)  # split once: an idea relisted at another conviction stays one idea

    def add_trades(key: str, label: str, chosen: Callable[[Dict[str, Any]], bool]):
        add(key, label, [r for r in finished if chosen(r)], sum(1 for r in running if chosen(r)),
            lambda r: _trade_vs_peers(r, peers), "return_pct", "spy_return_pct", f"to stop or target, ≤{MAX_DAYS} sessions")

    def add_horizon(key: str, label: str, rows: List[Dict[str, Any]], horizon: int, sign: Optional[int] = None):
        def compare(record):
            value = _horizon_vs_peers(record, peers, horizon)
            side = sign if sign is not None else (-1 if record.get("direction") == "short" else 1)
            return None if value is None else side * value
        add(key, label, [r for r in rows if r["status"] == "closed"], sum(1 for r in rows if r["status"] == "open"),
            compare, f"return_{horizon}d_pct", f"spy_{horizon}d_pct", f"{horizon} sessions")

    for verdict, label in (("high", "Trade ideas · high conviction"), ("medium", "Trade ideas · medium conviction"),
                           ("rejected", "Candidates the review rejected")):
        add_trades(f"idea:{verdict}", label, lambda r, v=verdict: r.get("verdict") == v and r.get("kind") in (None, "idea"))
    add_trades("breakout", "Breakout alerts", lambda r: r.get("kind") == "breakout")
    for alignment, label in (("agree", "Ideas & breakouts · NX agrees"), ("against", "Ideas & breakouts · NX against")):
        add_trades(f"nx:{alignment}", label, lambda r, a=alignment: (r.get("nx") or {}).get("alignment") == a)
    verdicts = _stock_calls(everything)
    for group, label in (("bullish", "Report calls · bullish"), ("bearish", "Report calls · bearish")):
        add_horizon(f"verdict:{group}", label, [r for r in verdicts if r.get("group") == group], 10)
    buys = [r for r in verdicts if r.get("model_call") == "buy"]
    add_horizon("model:buy_shown", "Model's Buy calls · shown as Buy", [r for r in buys if r.get("verdict") in ("buy", "add")], 10, 1)
    add_horizon("model:buy_softened", "Model's Buy calls · softened by a guard", [r for r in buys if r.get("verdict") not in ("buy", "add")], 10, 1)
    add_horizon("model:sell", "Model's Sell calls", [r for r in verdicts if r.get("model_call") == "sell"], 10, -1)
    add_horizon("social", "Most discussed on social media", [r for r in everything if r.get("kind") == "social"], 20)
    influencer = [r for r in everything if r.get("kind") == "influencer"]
    for channel in sorted({r.get("channel") or "?" for r in influencer}):
        add_horizon(f"youtube:{channel}", f"YouTube · {channel}", [r for r in influencer if (r.get("channel") or "?") == channel], 20)
    return [item for item in sources if item["closed"] or item["open"]]


def social_stats(rows: List[Dict[str, Any]], peers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The social scan's names: how many, and their average return, vs SPY and vs same-day stocks after 5/10/20 sessions."""
    peers = peers or {}
    def average(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None
    out: Dict[str, Any] = {"names": len(rows), "closed": sum(1 for r in rows if r["status"] == "closed"
                                                              and r.get("return_20d_pct") is not None)}
    for horizon in (5, 10, 20):
        done = [r for r in rows if r.get(f"benchmark_return_{horizon}d_pct") is not None and r.get(f"spy_{horizon}d_pct") is not None]
        out[f"{horizon}d"] = {"count": len(done), "avg_pct": average([r[f"return_{horizon}d_pct"] for r in done]),
                              "vs_spy_pct": average([r[f"benchmark_return_{horizon}d_pct"] - r[f"spy_{horizon}d_pct"] for r in done]),
                              "vs_peers_pct": average([_horizon_vs_peers(r, peers, horizon) for r in done])}
    return out


def verdict_stats(verdicts: List[Dict[str, Any]], peers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Report calls by group and NX slow-tunnel state: closed count, 5- and 10-session return, 10-session vs SPY and same-day stocks."""
    peers = peers or {}
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
                            "avg_10d_vs_spy_pct": average([r["benchmark_return_10d_pct"] - r["spy_10d_pct"] for r in closed
                                                           if r.get("benchmark_return_10d_pct") is not None
                                                           and r.get("spy_10d_pct") is not None]),
                            "avg_10d_vs_peers_pct": average([_horizon_vs_peers(r, peers, 10) for r in closed])}
        out[group] = {"label": label, "by_nx": cells}
    return out


def model_call_stats(verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The model's own call before any guard, 10 sessions later against the rest of the watchlist that day:
    the test of whether the call says anything about the stock, apart from how the watchlist as a whole did."""
    counted = [r for r in verdicts if r["status"] == "closed" and r.get("return_10d_pct") is not None]
    by_day: Dict[str, List[float]] = {}
    for record in counted:
        by_day.setdefault(record["signal_day"], []).append(record["return_10d_pct"])
    out: Dict[str, Any] = {}
    for call, label in (("buy", "Buy"), ("hold", "Hold"), ("sell", "Sell")):
        rows = [r for r in counted if r.get("model_call") == call]
        mean, t, months = _month_t([(r["signal_day"], r["return_10d_pct"] - sum(by_day[r["signal_day"]]) / len(by_day[r["signal_day"]]))
                                    for r in rows])
        out[call] = {"label": label, "closed": len(rows),
                     "open": sum(1 for r in verdicts if r["status"] == "open" and r.get("model_call") == call),
                     "shown": sum(1 for r in rows if r.get("verdict") in ("buy", "add")),
                     "avg_10d_pct": sum(r["return_10d_pct"] for r in rows) / len(rows) if rows else None,
                     "avg_10d_vs_watchlist_pct": mean, "t": round(t, 2) if t is not None else None, "months": months}
    return out


def _pct(value: Optional[float], sign: bool = True) -> str:
    if value is None:
        return "–"
    return f"{value:+.2f}%" if sign else f"{value:.0f}%"


def format_track_record(stats: Dict[str, Any]) -> str:
    lines = [f"📒 **Idea track record** · last {stats['window_days']} days (research only; entry at the alert price, "
             f"{MAX_DAYS}-session limit, printed stop/target; each idea counts once, after all {MAX_DAYS} sessions)"]
    for key, _label in GROUPS:
        group = stats["groups"][key]
        if not group["closed"] and not group["open"]:
            continue
        lines.append(f"• {group['label']}: {group['closed']} finished · win {_pct(group['win_rate'], False)} · "
                     f"avg {_pct(group['avg_return_pct'])} · vs same-day stocks {_pct(group['avg_vs_peers_pct'])} · "
                     f"{group['open']} running ({_pct(group['running_avg_pct'])} so far)")
    review = stats.get("review_value") or {}
    if review.get("verdict") in (None, "too_early"):
        lines.append("Too few closed ideas yet to judge the review; this fills in over the coming weeks.")
    else:
        outcome = {"helps": "the review helps", "hurts": "the review hurts"}.get(review["verdict"], "not a clear difference yet")
        lines.append(f"Review value: on {review['days']} same-day comparisons, approved minus rejected is "
                     f"{review['gap_pct']:+.2f} points per idea: {outcome}.")
    by_nx = stats.get("by_nx") or {}
    if any(group["closed"] for group in by_nx.values()):
        parts = [f"{group['label']} {group['closed']} · avg {_pct(group['avg_return_pct'])}"
                 for key, group in by_nx.items() if group["closed"]]
        lines.append("By your NX slow tunnel (finished ideas and breakouts): " + " | ".join(parts)
                     + ("" if sum(group["closed"] for group in by_nx.values()) >= 20 else " — too few to judge yet"))
    verdicts = stats.get("verdicts") or {}
    closed_verdicts = sum(group["by_nx"]["all"]["closed"] for group in verdicts.values())
    if closed_verdicts:
        lines.append("Report calls, 10 sessions later vs same-day stocks, by where the price sat against your NX slow tunnel:")
        for group in verdicts.values():
            cells = group["by_nx"]
            parts = [f"{state} {cells[state]['closed']} · {_pct(cells[state]['avg_10d_vs_peers_pct'])}"
                     for state in NX_SLOW_STATES if cells[state]["closed"]]
            if parts:
                lines.append(f"• {group['label']}: " + " | ".join(parts))
        if closed_verdicts < 60:
            lines.append("Few closed calls so far; read this as a first look.")
    calls = stats.get("model_calls") or {}
    if any(row["closed"] for row in calls.values()):
        parts = [f"{row['label']} {row['closed']} · {_pct(row['avg_10d_vs_watchlist_pct'])}" for row in calls.values() if row["closed"]]
        lines.append("The model's own calls before any guard, 10 sessions later vs the rest of the watchlist: " + " | ".join(parts)
                     + f" ({calls['buy']['shown']} of {calls['buy']['closed']} Buys were shown as Buy)")
    board = stats.get("scoreboard") or []
    if board:
        judged = [item for item in board if item["verdict"] in ("ahead", "behind")]
        if judged:
            lines.append("What's working so far: " + "; ".join(
                f"{item['label']} {'ahead of' if item['verdict'] == 'ahead' else 'behind'} same-day stocks "
                f"({item['avg_vs_peers_pct']:+.2f}%, {item['closed']} finished)" for item in judged))
        else:
            lines.append(f"What's working: no source has {SCOREBOARD_MIN} finished records with a clear difference from same-day stocks yet.")
    social = stats.get("social") or {}
    if social.get("5d", {}).get("count"):
        parts = [f"{h} sessions {social[f'{h}d']['count']} · vs same-day stocks {_pct(social[f'{h}d']['vs_peers_pct'])}"
                 for h in (5, 10, 20) if social[f"{h}d"]["count"]]
        lines.append("Most-discussed names on social media (from the close they were listed): " + " | ".join(parts)
                     + ("" if social["5d"]["count"] >= 50 else " — too few to judge yet"))
    influencers = stats.get("influencers") or {}
    counted = [(name, row) for name, row in influencers.items() if row["counted"][5]]
    if counted:
        lines.append("YouTube picks, in the call's direction vs same-day stocks (5 / 10 / 20 sessions):")
        for name, row in sorted(counted, key=lambda item: -item[1]["picks"]):
            lines.append(f"• {name}: {row['picks']} picks ({row['bullish']} bullish, {row['bearish']} bearish) · "
                         + " / ".join(_pct(row["vs_peers"][h]) for h in (5, 10, 20))
                         + f" · {row['closed']} closed")
        if sum(row["closed"] for _name, row in counted) < 30:
            lines.append("Few closed picks so far; read this as a first look.")
    lines.append("Same-day stocks: the average S&P stock from that day's close, same direction, stop, target and limit "
                 "(same sessions for report calls and picks).")
    return "\n".join(lines)


class TrackerJob:
    """Settles open records after each close and posts the weekly summary (worker leader only)."""

    SETTLE_AFTER = (16, 30)

    def __init__(self, repo: Any, emit: Callable[[str, Dict[str, Any], str], Any], *,
                 bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                 nx_bars: Optional[Callable[[List[str]], Dict[str, List[Dict[str, Any]]]]] = None,
                 reports: Optional[Callable[[], List[Any]]] = None,
                 universe: Optional[Callable[[], List[str]]] = None):
        from concurrent.futures import ThreadPoolExecutor
        self.repo = repo
        self._emit = emit
        self._bars = bars
        self._nx_bars = nx_bars
        self._reports = reports or (lambda: recent_reports(repo.db, VERDICT_DAYS, repo.tracked_idea_ids("verdict:")))
        # Live: the screening universe with a year of bars; an injected ``bars`` (tests) measures nothing unless given one.
        live = bars is trend.download_bars
        self._peer_bars = (lambda tickers: trend.download_bars(tickers, period=PEER_PERIOD)) if live else bars
        self._universe = universe or (_us_universe if live else list)
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
                filled = fill_model_calls(self.repo)
                logger.info("Idea tracker: %d report calls added, %d older calls filled in", added, filled)
            except Exception as exc:  # the report calls are picked up tomorrow
                logger.warning("Idea tracker could not read report calls: %s", type(exc).__name__)
            closed = settle_open(self.repo, day, self._bars, self._nx_bars)
            logger.info("Idea tracker: %d records closed", closed)
            try:
                measured = update_peer_baselines(self.repo, day, self._peer_bars, self._universe)
                logger.info("Idea tracker: same-day stocks measured for %d signal days", measured)
            except Exception as exc:  # the summary uses the days already measured; the rest wait for the next run
                logger.warning("Idea tracker could not measure same-day stocks: %s", type(exc).__name__)
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
