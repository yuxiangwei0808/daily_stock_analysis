"""Swing trade opportunities: strong trends to go long or short, with live breakouts.

Enabled with ``TRADE_OPPORTUNITIES_ENABLED=true``; runs inside the Trade Desk
worker (leader only). Nothing is traded; every idea is research.

1. After each scheduled stock-report run (5+ US reports, settled 3 minutes),
   daily bars for the watchlist plus the US scan universe
   (``SCREENING_US_UNIVERSE``, S&P 500 by default) are scored by fixed trend
   rules (``trend.score_bars``). Strong setups, plus watchlist names whose
   report strongly agrees with a developing trend, go to the routine model in
   one call with recent bars, headlines, the report summary and the SPY/QQQ
   regime. It confirms or rejects each one with a conviction, levels and thesis.
2. Medium/high-conviction ideas are sent as one Discord message with labelled
   ways to act: long = buy shares; short = sell if held / short shares (margin,
   borrow, unbounded loss) / an inverse ETF where one exists. High conviction
   adds calls or puts; during the regular session a Trade Desk options
   comparison follows for new high-conviction names.
3. During the regular session, the watchlist and near-breakout names are
   quoted every minute; crossing the prior 20-day high (above MA50) or low
   (below MA50) on 1.3x normal volume pace alerts once per day.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import earnings, trend
from .models import TradeAdviceRequest

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
_US_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
MIN_BATCH = 5  # a scheduled run, not a one-off analysis
SETTLE = timedelta(minutes=3)
WINDOW = timedelta(minutes=60)
JOB_TIMEOUT = timedelta(minutes=30)
CHECK_EVERY = timedelta(seconds=60)  # the worker ticks every 5 s; reports change slowly
REPORT_AGREES = 55  # trend strength that, with a strong report, still earns a review
QUOTE_SECONDS = 60
BREAKOUT_PACE = 1.3
NEAR_TRIGGER = 0.02
NEAR_STRENGTH = 60
SCAN_BREAKOUTS_PER_DAY = 5
BREAKOUT_MARGIN_ATR = 0.15  # beyond the level by this much, not a one-cent poke
LEVELS_RETRY_SECONDS = 300
BREAKOUT_PACE_EARLY = 2.0
EARLY_UNTIL = dtime(10, 30)
BREAKOUT_COOLDOWN_DAYS = 7  # about five sessions per ticker and direction
# Funds tracking the same index move together: one breakout alert per group and day.
_EQUIVALENT = [{"SPY", "VOO", "IVV", "SPLG", "SH", "SSO", "SDS", "UPRO", "SPXU", "SPXL", "SPXS"},
               {"QQQ", "QQQM", "QLD", "QID", "TQQQ", "SQQQ", "PSQ"},
               {"SOXX", "SMH", "SOXL", "SOXS", "USD", "SSG"},
               {"IWM", "TNA", "TZA", "RWM", "UWM", "TWM"},
               {"DIA", "DOG", "DDM", "DXD", "UDOW", "SDOW"}]


def _breakout_group(ticker: str, name: str) -> str:
    for group in _EQUIVALENT:
        if ticker in group:
            return min(group)
    if geared_fund(name):
        # Single-stock funds ("Direxion Daily TSLA Bull 2X") group with their stock.
        for word in re.findall(r"\b[A-Z]{2,5}\b", name or ""):
            if word not in {"ETF", "ETN", "USD", "SHARES", "DAILY", "BULL", "BEAR", "LONG", "SHORT"}:
                return word
    return ticker  # names outside the watchlist; watchlist breakouts are not capped
OPENING_GRACE = timedelta(minutes=15)
REGIME = ("SPY", "QQQ")
# Broad inverse ETFs; single stocks are shorted directly or with puts.
INVERSE_ETFS = {"SPY": "SH", "VOO": "SH", "IVV": "SH", "QQQ": "PSQ", "IWM": "RWM", "DIA": "DOG",
                "SMH": "SOXS (3x)", "SOXX": "SOXS (3x)", "XLF": "SEF", "TLT": "TBF"}


# Fund families that only issue daily-reset leveraged/inverse funds (report names are
# often cut to ~31 characters, e.g. "Direxion Daily Semiconductor Be").
_GEARED_FAMILY = re.compile(r"^(direxion daily|proshares (ultra|short)|tradr\b|t-rex\b|defiance (daily|leveraged)|"
                            r"leverage shares|microsectors|axs (\d|short)|graniteshares (\d|short|long)|"
                            r"tuttle capital short|yieldmax short)", re.I)
_MULTIPLE = re.compile(r"(?<![\w.])-?[1-9](\.\d+)?x\b", re.I)
_FUND_WORDS = re.compile(r"\b(daily|etf|shares|fund|trust|etn)\b", re.I)
_INVERSE_WORDS = re.compile(r"\b(bear|bull|inverse)\b", re.I)


def geared_fund(name: str) -> bool:
    """Leveraged or inverse ETFs: daily-reset funds that decay and are not shorted."""
    name = name or ""
    if _GEARED_FAMILY.search(name.strip()):
        return True
    return bool(_FUND_WORDS.search(name) and (_MULTIPLE.search(name) or _INVERSE_WORDS.search(name)))


def enabled() -> bool:
    return os.getenv("TRADE_OPPORTUNITIES_ENABLED", "false").strip().lower() == "true"


def max_ideas() -> int:
    try:
        return max(1, min(10, int(os.getenv("TRADE_OPPORTUNITIES_MAX", "5") or 5)))
    except ValueError:
        return 5


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _session_day(now: datetime) -> date:
    return now.astimezone(_NEW_YORK).date()


# -- options comparison lines (Trade Desk jobs) --------------------------------
def _legs(candidate: Dict[str, Any]) -> str:
    parts = []
    for leg in candidate.get("legs") or []:
        side = "buy" if leg.get("side") == "buy" else "sell"
        if leg.get("right") == "stock":
            parts.append(f"{side} {leg.get('quantity', '')} shares")
            continue
        expiry = str(leg.get("expiry") or "")[5:10].replace("-", "/")
        strike = leg.get("strike")
        strike_text = f"{float(strike):g}" if strike is not None else "?"
        parts.append(f"{side} {strike_text}{'C' if leg.get('right') == 'call' else 'P'} {expiry}".strip())
    return " / ".join(parts)


def format_options(job: Dict[str, Any], label: str) -> List[str]:
    """Short lines per stock: the best options trade (or wait) and the reason."""
    ticker = job["request"]["ticker"]
    explanation = " ".join(str(job.get("explanation") or "").split())
    reason = (re.split(r"(?<=[.!?。])\s", explanation, maxsplit=1)[0] if explanation else "")[:240]
    candidates = job.get("candidates") or []
    if job.get("status") not in {"completed", "stale"}:
        return [f"**{ticker}** ({label}) — no comparison: {str(job.get('error') or job.get('status'))[:120]}"]
    if job.get("assessment") != "compare" or not candidates:
        return [f"**{ticker}** ({label}) — wait", *([f"> {reason}"] if reason else [])]
    best = candidates[0]
    payoff = best.get("payoff") or {}
    debit = payoff.get("entry_debit")
    cost = (f"debit {_money(debit)}" if (debit or 0) >= 0 else f"credit {_money(-debit)}") if debit is not None else ""
    probability = (best.get("probability") or {}).get("probability_of_profit")
    facts = [cost, f"max loss {_money(payoff.get('max_loss'))}" if payoff.get("max_loss") is not None else "max loss unbounded",
             f"max gain {_money(payoff.get('max_gain'))}" if payoff.get("max_gain") is not None else "max gain unbounded",
             "breakeven " + ", ".join(f"{float(b):g}" for b in (payoff.get("breakevens") or [])[:2]) if payoff.get("breakevens") else "",
             f"P(profit) {probability:.0%}" if isinstance(probability, (int, float)) else ""]
    stale = " (prices moved; re-check)" if job.get("status") == "stale" else ""
    return [f"**{ticker}** ({label}) — {best.get('title', best.get('strategy'))}: {_legs(best)}{stale}",
            "   " + " · ".join(fact for fact in facts if fact),
            *([f"> {reason}"] if reason else [])]


# -- candidates and model review ------------------------------------------------
REPORT_BULLISH, REPORT_BEARISH = 65, 35  # report scores cluster near 50; beyond these they lean clearly


def _quality(setup: Dict[str, Any]) -> float:
    """A continuous tie-breaker among equal strengths: volume and ATR-scaled momentum."""
    momentum = abs(setup.get("momentum20_pct") or 0) / max(setup.get("atr_pct") or 0, 0.5)
    return min(setup.get("volume_ratio") or 0, 3.0) + momentum / 5


def select_candidates(scores: Dict[str, Dict[str, Any]], watchlist: List[str], reports: Dict[str, Dict[str, Any]],
                      limit: int) -> List[Dict[str, Any]]:
    """Strong, not stretched trends; half the slots are kept for watchlist/held names.

    Stretched setups (more than 3 ATR past MA20) are chasing and are left out; watchlist
    names also qualify at strength REPORT_AGREES when their report leans the same way.
    """
    watch = set(watchlist)
    picked = []
    for ticker, setup in scores.items():
        if (ticker in REGIME and ticker not in watch) or setup.get("extended"):
            continue
        report = reports.get(ticker) or {}
        score = report.get("score")
        agrees = isinstance(score, (int, float)) and (
            (setup["direction"] == "long" and score >= REPORT_BULLISH)
            or (setup["direction"] == "short" and score <= REPORT_BEARISH))
        if setup["strength"] >= trend.STRONG or (ticker in watch and agrees and setup["strength"] >= REPORT_AGREES):
            picked.append({"ticker": ticker, "source": "watchlist" if ticker in watch else "scan",
                           "name": report.get("name", ""), **setup})
    rank = lambda item: (item["strength"], _quality(item))  # noqa: E731
    own = sorted((item for item in picked if item["source"] == "watchlist"), key=rank, reverse=True)
    scan = sorted((item for item in picked if item["source"] == "scan"), key=rank, reverse=True)
    reserved = own[:max(1, limit // 2)]
    rest = sorted(own[len(reserved):] + scan, key=rank, reverse=True)
    return sorted(reserved + rest[:limit - len(reserved)], key=rank, reverse=True)


def regime_text(scores: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    for ticker in REGIME:
        setup = scores.get(ticker)
        if setup:
            side = "above" if setup["price"] >= setup["ma50"] else "below"
            parts.append(f"{ticker} {side} MA50 ({setup['momentum20_pct']:+.1f}% 20d)")
    return ", ".join(parts)


_REVIEW_RULES = """You review swing-trade setups (holding days to a few weeks) for a US retail trader.
Each candidate passed fixed trend rules: MA20/MA50 alignment and slope, 20-day breakout, momentum, volume.
For each, decide whether the trend is worth acting on now, using the bars, headlines, stock report and market regime.
- direction: "long", "short" or "none" (reject). Keep the rule direction or reject; never flip it.
- conviction: "high" only when trend, catalyst/news and regime all agree and risk/reward is at least 2:1;
  "medium" when the trend is clean but something is missing; "low" otherwise.
- Penalize: a stretched move (chasing), news that contradicts the trend, a short against a strong market,
  thin reasons. earnings_inside_hold=true rules out "high" (gap risk); name it in risks. Otherwise do not treat
  later earnings as a reason to lower conviction. Aim for a first target at least 1.5x the risk to the stop.
- entry: a short instruction (e.g. "buy 224-226 or on a pullback to 218"); stop and targets as prices;
  the provided stop/targets are ATR-based starting points you may adjust.
- thesis: at most 35 words, concrete; invalidation: what would prove it wrong; risks: at most 20 words.
Write in English. Return only JSON:
{"ideas": [{"ticker": "...", "direction": "long|short|none", "conviction": "low|medium|high",
"entry": "...", "stop": 0.0, "targets": [0.0, 0.0], "horizon": "e.g. 1-3 weeks", "thesis": "...",
"invalidation": "...", "risks": "..."}]}"""


def build_prompt(candidates: List[Dict[str, Any]], regime: str, context: Dict[str, Dict[str, Any]], today: str) -> str:
    rows = []
    for item in candidates:
        extra = context.get(item["ticker"]) or {}
        rows.append({
            "ticker": item["ticker"], "name": item.get("name", ""), "source": item["source"], "rule_direction": item["direction"],
            "rule_strength": item["strength"], "rule_notes": item["notes"],
            "metrics": {key: item[key] for key in ("price", "ma20", "ma50", "ma20_slope_pct", "ma50_slope_pct",
                                                   "atr", "atr_pct", "high20", "low20", "momentum20_pct",
                                                   "volume_ratio", "dollar_volume_m")},
            "atr_levels": {"stop": item["stop"], "targets": item["targets"]},
            "bars_date_o_h_l_c_v": extra.get("bars", []),
            "headlines": extra.get("headlines", []),
            "report": extra.get("report"),
            "next_earnings": _earnings_text(item.get("earnings"), today),
            "earnings_inside_hold": bool(item.get("earnings") and (date.fromisoformat(item["earnings"])
                                          - date.fromisoformat(today)).days <= earnings.INSIDE_HOLD_DAYS),
        })
    return (f"{_REVIEW_RULES}\n\nDate: {today}. Market regime: {regime or 'unknown'}.\n"
            f"Candidates:\n{json.dumps(rows, ensure_ascii=False)}")


def _earnings_text(when: Optional[str], today: str) -> str:
    if not when:
        return "none published"
    return f"{when} (in {(date.fromisoformat(when) - date.fromisoformat(today)).days} days)"


def _review_with_llm(prompt: str) -> Optional[List[Dict[str, Any]]]:
    from src.agent.runner import try_parse_json
    from .advisor import _generation_backend

    backend, _backend_id = _generation_backend()
    result = backend.generate(prompt, {"temperature": 0.2, "max_output_tokens": 8192})
    data = try_parse_json(result.text or "") or {}
    ideas = data.get("ideas") if isinstance(data, dict) else None
    return [row for row in ideas if isinstance(row, dict)] if isinstance(ideas, list) else None


def _float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def merge_review(candidates: List[Dict[str, Any]], review: List[Dict[str, Any]],
                 today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Keep medium/high ideas in the rule direction; levels on the wrong side fall back to the ATR ones.

    Earnings inside the swing hold cap conviction at medium, which also skips options.
    """
    by_ticker = {str(row.get("ticker", "")).upper(): row for row in review if isinstance(row, dict)}
    ideas = []
    for item in candidates:
        row = by_ticker.get(item["ticker"])
        if not row or row.get("direction") != item["direction"] or row.get("conviction") not in {"medium", "high"}:
            continue
        try:
            ideas.append(_merged_idea(item, row, today))
        except Exception as exc:  # one malformed row drops only that idea
            logger.info("Review row for %s skipped: %s", item["ticker"], type(exc).__name__)
    ideas.sort(key=lambda idea: (idea["conviction"] == "high", idea["strength"]), reverse=True)
    return ideas


def _merged_idea(item: Dict[str, Any], row: Dict[str, Any], today: Optional[date]) -> Dict[str, Any]:
    sign = 1 if item["direction"] == "long" else -1
    price = item["price"]
    stop = _float(row.get("stop"))
    if stop is None or sign * (price - stop) <= 0:
        stop = item["stop"]
    raw_targets = row.get("targets")
    raw_targets = raw_targets if isinstance(raw_targets, (list, tuple)) else [raw_targets] if raw_targets else []
    targets = [value for value in (_float(t) for t in raw_targets[:2])
               if value is not None and sign * (value - price) > 0] or item["targets"]
    conviction = row["conviction"]
    when = date.fromisoformat(item["earnings"]) if item.get("earnings") else None
    inside = today is not None and when is not None and (when - today).days <= earnings.INSIDE_HOLD_DAYS
    if inside and conviction == "high":
        conviction = "medium"
    return {**item, "conviction": conviction, "stop": round(stop, 2),
            "earnings_note": earnings.note(when, today) if today is not None else "",
            "targets": [round(t, 2) for t in targets],
            **{key: " ".join(str(row.get(key) or "").split())[:300]
               for key in ("entry", "horizon", "thesis", "invalidation", "risks")}}


def expressions(idea: Dict[str, Any], *, options_follow: bool) -> List[str]:
    """Labelled ways to act on an idea; options only at high conviction.

    ``held_note`` is None when holdings are unknown, "" when not held.
    """
    high = idea["conviction"] == "high"
    options_note = " (options comparison follows)" if options_follow else ""
    long = idea["direction"] == "long"
    # held_side: None = holdings unknown, "" = not held, else the net side of what is held.
    side = idea.get("held_side", "long" if idea.get("held_note") else idea.get("held_note"))
    known = side is not None
    aligned = side == idea["direction"]
    opposed = side in {"long", "short"} and not aligned
    if geared_fund(idea.get("name", "")):
        if long:
            return [("already held — keep it short-term" if aligned else "buy shares")
                    + " (leveraged/inverse ETF: short-term, small size)"]
        if opposed or not known:
            return [("sell or trim your position" if opposed else "sell or trim if you hold it")
                    + " (leveraged/inverse ETF: shorting it is not advised)"]
        return ["stay out (leveraged/inverse ETF: shorting it is not advised)"]
    if long:
        first = ("already held — hold, or add small" if aligned else
                 "your position leans the other way — review it" if opposed else "buy shares")
        ways = [first]
        if high:
            ways.append(f"calls / call debit spread (defined risk){options_note}")
        return ways
    if aligned:
        return ["already positioned for a drop — hold, with the stop"]
    if opposed:
        ways = ["sell or trim your position"]
        if high:
            ways.append(f"or hedge it with puts / a put debit spread (defined risk){options_note}")
        return ways
    ways = ([] if known else ["sell or trim if you hold it"]) + [
        "short shares (needs margin + borrow; loss unbounded — use the stop)"]
    if idea["ticker"] in INVERSE_ETFS:
        ways.append(f"inverse ETF {INVERSE_ETFS[idea['ticker']]}")
    if high:
        ways.append(f"puts / put debit spread (defined risk){options_note}")
    return ways


def _reward_risk(idea: Dict[str, Any]) -> Optional[float]:
    """First target's reward over the risk to the stop, from the last price."""
    try:
        risk = abs(idea["price"] - idea["stop"])
        return abs(idea["targets"][0] - idea["price"]) / risk if risk else None
    except (KeyError, IndexError, TypeError):
        return None


def format_message(ideas: List[Dict[str, Any]], repeats: List[Dict[str, Any]], regime: str, *,
                   options_follow: set, scanned: str = "") -> str:
    lines = ["🎯 **Trade opportunities** · swing, days to weeks"]
    lines += [line for line in (f"Market: {regime}" if regime else "", scanned) if line]
    for idea in ideas:
        long = idea["direction"] == "long"
        icon = "🟢" if long else "🔴"
        targets = " / ".join(_price(t) for t in idea["targets"])
        lines += ["",
                  f"{icon} **{idea['ticker']}**{' ' + idea['name'][:24] if idea.get('name') else ''} · "
                  f"{'LONG' if long else 'SHORT'} · {idea['conviction']} conviction · "
                  f"trend {idea['strength']} · {idea['source']}",
                  f"Price {_price(idea['price'])} · entry: {idea.get('entry') or 'near ' + _price(idea['price'])} · "
                  f"stop {_price(idea['stop'])} · targets {targets}" + (f" · {idea['horizon']}" if idea.get("horizon") else "")
                  + (f" · R:R {_reward_risk(idea):.1f}" if _reward_risk(idea) else "")]
        if idea.get("held_note"):
            lines.append(idea["held_note"])
        if idea.get("earnings_note"):
            lines.append(idea["earnings_note"])
        if idea.get("thesis"):
            lines.append(f"> {idea['thesis']}")
        if idea.get("risks"):
            lines.append(f"Risks: {idea['risks']}")
        if idea.get("invalidation"):
            lines.append(f"Wrong if: {idea['invalidation']}")
        lines.append("How: " + " · ".join(expressions(idea, options_follow=idea["ticker"] in options_follow)))
    if repeats:
        lines += ["", "Still on: " + ", ".join(
            f"{idea['ticker']} {'↑' if idea['direction'] == 'long' else '↓'} ({idea['conviction']})" for idea in repeats)]
    lines += ["", "_Research only — no orders are placed._"]
    return "\n".join(lines)


# -- breakout levels -------------------------------------------------------------
def breakout_levels(bars: List[Dict[str, Any]], day: date) -> Optional[Dict[str, float]]:
    """Prior 20-session high/low, MA50, ATR and average volume as of ``day`` (today's bar excluded)."""
    history = [bar for bar in bars if str(bar.get("date", ""))[:10] < day.isoformat()]
    if len(history) < 50:
        return None
    closes = [bar["close"] for bar in history]
    highs, lows = [bar["high"] for bar in history], [bar["low"] for bar in history]
    ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
              for i in range(len(history) - 14, len(history))]
    volumes = [volume if isinstance(volume, (int, float)) and math.isfinite(volume) else 0
               for volume in (bar.get("volume") for bar in history[-50:])]
    return {"high20": max(highs[-20:]), "low20": min(lows[-20:]), "ma50": sum(closes[-50:]) / 50,
            "atr": sum(ranges) / len(ranges), "avg_volume": sum(volumes) / len(volumes)}


class BreakoutWatch:
    """Minute quotes for the watchlist and near-breakout names; alerts on 20-day breakouts with volume."""

    def __init__(self, provider: Callable[[], Any], emit: Callable[[str, Dict[str, Any], str], Any], *,
                 watchlist: Callable[[], List[str]], bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]]
                 = trend.download_bars, clock: Callable[[], float] = None,
                 earnings_date: Callable[[str, date], Optional[date]] = earnings.next_earnings,
                 held: Optional[Callable[[str], str]] = None,
                 history: Optional[Callable[[], List[tuple]]] = None, confirm_checks: int = 2,
                 held_side: Optional[Callable[[str], Optional[str]]] = None):
        import time
        self._held_side = held_side
        self._confirm = max(1, confirm_checks)
        self._earnings_date = earnings_date
        self._held = held
        self._history = history  # past breakout alerts: (ticker, "up"|"down", date)
        self._recent: Dict[tuple, date] = {}
        self._pending: Dict[tuple, int] = {}  # (ticker, direction) -> consecutive checks beyond the level
        self._groups_alerted: set = set()
        self._provider = provider
        self._emit = emit
        self._watchlist = watchlist
        self._bars = bars
        self._clock = clock or time.monotonic
        self._day: Optional[date] = None
        self._levels: Dict[str, Dict[str, float]] = {}
        self._context: Dict[str, str] = {}
        self._extra: List[str] = []
        self._scan_alerts: Dict[date, int] = {}
        self._alerted: set = set()
        self._next = 0.0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="breakout-levels")
        self._loading = None
        self._retry_at: Optional[float] = None

    def stop(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def add(self, bars: Dict[str, List[Dict[str, Any]]], day: date,
            context: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """Levels for names from a scan, computed from bars already downloaded."""
        self._extra = list(bars)
        # Tagged with the scan's day: a pre-market review still rides on that session's alerts.
        self._context.update({ticker: {**note, "day": day.isoformat()} for ticker, note in (context or {}).items()})
        if self._day != day:
            return  # an after-close scan: the next session loads these names itself
        for ticker, rows in bars.items():
            levels = breakout_levels(rows, day)
            if levels:
                self._levels[ticker] = levels

    def _load(self, day: date, tickers: List[str]) -> Dict[str, Dict[str, float]]:
        bars = self._bars(tickers)
        return {ticker: levels for ticker, rows in bars.items() if (levels := breakout_levels(rows, day))}

    def tick(self, now: datetime, session: str) -> None:
        if session != "regular":
            return
        day = _session_day(now)
        clock = self._clock()
        if self._day != day or (self._retry_at is not None and clock >= self._retry_at and self._loading is None):
            if self._day != day:
                # Notes from an earlier day's review never ride on today's alerts.
                self._day, self._levels = day, {}
                self._context = {ticker: note for ticker, note in self._context.items()
                                 if note.get("day") == day.isoformat()}
                self._alerted, self._pending, self._groups_alerted = set(), {}, set()
                self._scan_alerts = {}
                self._load_recent(day)
            self._retry_at = None
            tickers = list(dict.fromkeys([*self._watchlist(), *self._extra]))
            self._loading = self._pool.submit(self._load, day, tickers)
            return
        if self._loading is not None:
            if not self._loading.done():
                return
            try:
                loaded = self._loading.result()
                self._levels = {**loaded, **self._levels}
                requested = len(set(self._watchlist()) | set(self._extra))
                if requested and len(loaded) < requested / 2:  # keep what loaded; try again for the rest
                    logger.warning("Breakout levels for only %d/%d tickers; retrying", len(loaded), requested)
                    self._retry_at = clock + LEVELS_RETRY_SECONDS
            except Exception as exc:  # retried in a few minutes; scans still add names
                logger.warning("Breakout levels unavailable: %s", exc)
                self._retry_at = clock + LEVELS_RETRY_SECONDS
            self._loading = None
        if clock < self._next or not self._levels:
            return
        self._next = clock + QUOTE_SECONDS
        local = now.astimezone(_NEW_YORK)
        if local < datetime.combine(day, datetime.min.time().replace(hour=9, minute=30), _NEW_YORK) + OPENING_GRACE:
            return
        quotes = self._provider().watchlist_quotes(list(self._levels))
        fraction = trend.volume_fraction(now)
        watchlist = set(self._watchlist())
        # Early prints may include pre-market volume in the snapshot: ask for more pace then.
        needed = BREAKOUT_PACE_EARLY if local.time() < EARLY_UNTIL else BREAKOUT_PACE
        beyond = set()
        # Watchlist (and held) names first, so a mirror fund you hold is the one that alerts.
        for ticker, quote in sorted(quotes.items(), key=lambda item: item[0] not in watchlist):
            levels, price, volume = self._levels.get(ticker), quote.get("price"), quote.get("volume")
            if not levels or not price or not volume or levels["avg_volume"] <= 0:
                continue
            pace = volume / (levels["avg_volume"] * fraction)
            margin = BREAKOUT_MARGIN_ATR * levels["atr"]
            up = price > levels["high20"] + margin and price > levels["ma50"]
            down = price < levels["low20"] - margin and price < levels["ma50"]
            if pace < needed or not (up or down):
                continue
            direction = "up" if up else "down"
            beyond.add((ticker, direction))
            # The move must hold for two consecutive checks: one-minute pokes reverse often.
            self._pending[(ticker, direction)] = self._pending.get((ticker, direction), 0) + 1
            if self._pending[(ticker, direction)] < self._confirm:
                continue
            key = f"breakout:{day.isoformat()}:{ticker}:{direction}"
            group = _breakout_group(ticker, quote.get("name", ""))
            last = self._recent.get((ticker, direction))
            if (key in self._alerted or group in self._groups_alerted
                    or (last is not None and (day - last).days < BREAKOUT_COOLDOWN_DAYS)
                    or (ticker not in watchlist and self._scan_alerts.get(day, 0) >= SCAN_BREAKOUTS_PER_DAY)):
                continue
            self._alerted.add(key)
            self._groups_alerted.add(group)
            self._recent[(ticker, direction)] = day
            sign, level = (1, levels["high20"]) if up else (-1, levels["low20"])
            stop, atr = price - sign * 1.5 * levels["atr"], levels["atr"]
            change = quote.get("change_pct")
            review = self._context.get(ticker) or {}
            same_way = review.get("direction") == ("long" if up else "short")
            note = (review.get("text", "") if same_way else
                    f"Today's trend review was {review['direction']}; this move goes against it." if review else "")
            geared = geared_fund(quote.get("name", ""))
            held_note = (self._held(ticker) if self._held is not None else "") or ""
            side = self._held_side(ticker) if self._held_side is not None else ("long" if held_note else "")
            aligned, opposed = side == ("long" if up else "short"), side in {"long", "short"} and side != (
                "long" if up else "short")
            if geared:  # daily-reset funds: ATR targets mean little and shorting them is not advised
                levels_line = f"Stop {stop:.2f} (1.5 ATR)"
                how = ("buy shares — leveraged/inverse ETF: short-term, small size" if up else
                       "sell/trim if held — leveraged/inverse ETF: shorting it is not advised")
                if aligned and up:
                    how = "already held — keep it short-term"
                elif opposed and not up:
                    how = "sell or trim your position — leveraged/inverse ETF"
                earnings_line = ""
            else:
                earnings_line = self._earnings_line(ticker, day)
                if same_way and review.get("stop") and review.get("targets"):
                    # The trend review's own levels, so the two messages agree.
                    levels_line = (f"Idea levels: stop {review['stop']:.2f} · targets "
                                   + " / ".join(f"{target:.2f}" for target in review["targets"]))
                else:
                    levels_line = (f"Swing levels: stop {stop:.2f} (1.5 ATR) · targets {price + sign * 3 * atr:.2f} / "
                                   f"{price + sign * 4.5 * atr:.2f}")
                how = ("buy shares with that stop" if up else
                       "sell/trim if held · short shares (margin + borrow; loss unbounded — use the stop)")
                if aligned:
                    how = ("already held — hold with that stop, or add small" if up else
                           "already positioned for a drop — hold, with the stop")
                elif opposed:
                    how = ("your position leans the other way — review it" if up else
                           "sell or trim your position, or tighten your stop")
            message = (f"{ticker} broke {'above its 20-day high' if up else 'below its 20-day low'} {level:.2f} "
                       f"at {price:.2f}" + (f" ({change:+.1f}% today)" if change is not None else "")
                       + f" on {pace:.1f}x normal volume pace.\n{levels_line}"
                       + (f"\n{held_note}" if held_note else "")
                       + (f"\n{earnings_line}" if earnings_line else "") + (f"\n{note}" if note else "") + f"\nHow: {how}.")
            event = self._emit("breakout", {"underlying": ticker, "kind": "breakout" if up else "breakdown",
                                            "price": price, "level": round(level, 2), "change_pct": change,
                                            "message": message}, key)
            if event is not None and ticker not in watchlist:
                self._scan_alerts[day] = self._scan_alerts.get(day, 0) + 1
        for pending in list(self._pending):
            if pending not in beyond:
                del self._pending[pending]  # back inside the level: start counting again

    def _earnings_line(self, ticker: str, day: date) -> str:
        """From the day's cache only; a miss is looked up in the background, never on the worker loop."""
        hit, when = earnings.peek(ticker, day)
        if not hit and self._earnings_date is earnings.next_earnings:
            self._pool.submit(earnings.next_earnings, ticker, day)
            return ""
        if not hit:
            try:
                when = self._earnings_date(ticker, day)
            except Exception:
                return ""
        return earnings.note(when, day)

    def _load_recent(self, day: date) -> None:
        """Breakouts already alerted in the last week, so a running trend is not re-announced daily."""
        self._recent = {}
        if self._history is None:
            return
        try:
            for ticker, direction, when in self._history():
                if (day - when).days < BREAKOUT_COOLDOWN_DAYS:
                    self._recent[(ticker, direction)] = max(when, self._recent.get((ticker, direction), when))
        except Exception as exc:  # the cooldown then starts from today
            logger.info("Breakout history unavailable: %s", type(exc).__name__)


# -- runner ------------------------------------------------------------------------
class OpportunityRunner:
    def __init__(self, service, emit: Callable[[str, Dict[str, Any], str], Any], *,
                 watchlist: Callable[[], List[str]], breakouts: Optional[BreakoutWatch] = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 universe: Optional[Callable[[], List[str]]] = None,
                 bars: Callable[[List[str]], Dict[str, List[Dict[str, Any]]]] = trend.download_bars,
                 news: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
                 review: Callable[[str], Optional[List[Dict[str, Any]]]] = _review_with_llm,
                 earnings_date: Callable[[str, date], Optional[date]] = earnings.next_earnings,
                 held: Optional[Callable[[str], str]] = None,
                 schedule_times: Optional[Callable[[], List[str]]] = None,
                 held_side: Optional[Callable[[str], Optional[str]]] = None):
        self.service = service
        self._held = held
        self._held_side = held_side
        self._schedule_times = schedule_times or _configured_schedule_times
        self._earnings_date = earnings_date
        self.emit = emit
        self.breakouts = breakouts
        self._watchlist = watchlist
        self._now = now
        self._universe = universe or self._scan_universe
        self._bars = bars
        self._news = news or self._headlines
        self._review = review
        self._last_id: Optional[int] = None
        self._next_check: Optional[datetime] = None
        self._scan = None
        self._pending: Optional[Dict[str, Any]] = None
        self._announced: Dict[str, str] = {}
        self._announced_day: Optional[date] = None
        self._universe_cache: tuple = (None, [])
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trade-opportunities")

    def stop(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    # data sources ----------------------------------------------------------
    def _scan_universe(self) -> List[str]:
        from src.services.screening.snapshot_us import fetch_us_universe
        day = _session_day(self._now())
        if self._universe_cache[0] != day:
            source = os.getenv("SCREENING_US_UNIVERSE", "auto")
            self._universe_cache = (day, [t.replace("-", ".") for t in fetch_us_universe(source)])
        return self._universe_cache[1]

    @staticmethod
    def _headlines(ticker: str) -> List[Dict[str, Any]]:
        from src.config import get_config
        from src.services.free_news import ticker_news
        config = get_config()
        sources = config.free_news_sources or ["google_news"]
        return ticker_news(ticker, sources, days=3, limit=5, finnhub_key=config.finnhub_api_key)

    # scheduling ------------------------------------------------------------
    def tick(self, regular_session: bool) -> None:
        now = self._now()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + CHECK_EVERY
        if self._scan is not None:
            if self._scan.done():
                scan, self._scan = self._scan, None
                try:
                    self._publish(scan.result(), regular_session)
                except Exception as exc:  # the next run scans again
                    logger.warning("Trade opportunities scan failed: %s", type(exc).__name__, exc_info=True)
            return
        if self._pending is not None:
            self._finish_options()
            return
        rows = self.service.repo.db.get_analysis_history(days=1, limit=200)
        if self._last_id is None:
            state = self._state()
            if state.get("day") == _session_day(now).isoformat():
                self._announced_day = _session_day(now)
                self._announced = dict(state.get("announced") or {})
            # The last published run survives restarts; without one, a run from the
            # last hour is still processed (its slot's dedup key stops a second send).
            self._last_id = state.get("last_id") or max(
                (row.id for row in rows if row.created_at and _naive(now) - row.created_at > WINDOW), default=0)
        batch = [row for row in rows if row.id > self._last_id and _US_TICKER.match(str(row.code or ""))
                 and str(row.code).upper() not in NOT_TICKERS
                 and row.created_at and _naive(now) - row.created_at <= WINDOW]
        if len(batch) < MIN_BATCH or _naive(now) - max(row.created_at for row in batch) < SETTLE:
            return
        self._last_id = max(row.id for row in batch)
        slot = self._slot(min(row.created_at for row in batch))
        if slot is None:
            return  # one-off analyses, not a scheduled run
        if slot in (self._state().get("published") or []):
            return  # a split batch or a catch-up run of a slot already sent
        reports = {}
        for row in sorted(batch, key=lambda item: item.id):
            reports[row.code] = {"id": row.id, "name": str(getattr(row, "name", "") or ""),
                                 "advice": row.operation_advice, "score": row.sentiment_score,
                                 "summary": " ".join(str(row.analysis_summary or "").split())[:400]}
        self._scan = self._pool.submit(self.scan, reports, self._last_id, slot)

    def _fill_names(self, candidates: List[Dict[str, Any]]) -> None:
        """Full names (display, leveraged-fund check) from one OpenD snapshot; report names are cut short."""
        try:
            quotes = self.service.provider("live").watchlist_quotes([item["ticker"] for item in candidates])
        except Exception as exc:  # names are a nicety; the scan goes on without them
            logger.info("Candidate names unavailable: %s", type(exc).__name__)
            return
        for item in candidates:
            name = (quotes.get(item["ticker"]) or {}).get("name")
            if name:
                item["name"] = name

    def _state(self) -> Dict[str, Any]:
        try:
            return self.service.repo.setting("opportunity_state", {}) or {}
        except Exception:
            return {}

    def _save_state(self, day: date, published: Optional[str] = None) -> None:
        try:
            state = self._state()
            slots = [slot for slot in (state.get("published") or []) if slot.startswith(day.isoformat())]
            if published and published not in slots:
                slots.append(published)
            self.service.repo.set_setting("opportunity_state", {"last_id": self._last_id, "day": day.isoformat(),
                                                                "announced": self._announced, "published": slots})
        except Exception as exc:  # restarts may then repeat a scan; the slot key still stops a second send
            logger.info("Opportunity state not saved: %s", type(exc).__name__)

    def _slot(self, first: datetime) -> Optional[str]:
        """The scheduled time this batch belongs to (its first report within an hour of it), else None."""
        for value in sorted(self._schedule_times(), reverse=True):
            try:
                hour, minute = (int(part) for part in value.split(":"))
            except ValueError:
                continue
            start = first.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if start <= first <= start + WINDOW + CATCHUP:  # a restarted run lands late
                return f"{first.date().isoformat()} {value}"
        return None

    # scan (background thread) ----------------------------------------------
    def scan(self, reports: Dict[str, Dict[str, Any]], batch_id: int, slot: str = "") -> Dict[str, Any]:
        from data_provider.us_session import session_window
        now = self._now()
        watchlist = [t for t in self._watchlist() if _US_TICKER.match(t)]
        tickers = list(dict.fromkeys([*REGIME, *watchlist, *self._universe()]))
        bars = self._bars(tickers)
        partial = trend.volume_fraction(now) if session_window(now)[0] == "regular" else 1.0
        today = _session_day(now).isoformat()
        scores = {}
        for ticker, rows in bars.items():
            fraction = partial if rows and rows[-1]["date"] == today else 1.0
            try:
                setup = trend.score_bars(rows, partial_fraction=fraction)
            except Exception as exc:  # one ticker's bad data never ends the scan
                logger.info("Trend score skipped %s: %s", ticker, type(exc).__name__)
                continue
            if setup:
                scores[ticker] = setup
        limit = max_ideas()
        candidates = select_candidates(scores, watchlist, reports, max(6, limit * 2))
        self._fill_names(candidates)
        regime = regime_text(scores)
        day = _session_day(now)
        dates = earnings.many([item["ticker"] for item in candidates if not geared_fund(item.get("name", ""))],
                              day, lookup=self._earnings_date)
        for item in candidates:
            when = dates.get(item["ticker"])
            item["earnings"] = when.isoformat() if when else None
        context = {}
        for item in candidates:
            try:
                headlines = [f"{h['published_at'][:10]} {h['title']} ({h.get('source', '')})"
                             for h in self._news(item["ticker"])[:5]]
            except Exception as exc:
                logger.info("Headlines unavailable for %s: %s", item["ticker"], type(exc).__name__)
                headlines = []
            report = reports.get(item["ticker"])
            context[item["ticker"]] = {"bars": trend.recent_bars(bars[item["ticker"]]), "headlines": headlines,
                                       "report": {k: report[k] for k in ("advice", "score", "summary")} if report else None}
        ideas: List[Dict[str, Any]] = []
        if candidates:
            review = self._review(build_prompt(candidates, regime, context, today))
            if review is None:
                logger.warning("Trade opportunities review returned no ideas; %d candidates skipped", len(candidates))
            ideas = merge_review(candidates, review or [], day)[:limit]
        # Watch near-breakout scan names live, besides the watchlist.
        near = {ticker: bars[ticker] for ticker, setup in scores.items()
                if ticker not in REGIME and setup["strength"] >= NEAR_STRENGTH
                and setup["high20"] > 0 and setup["low20"] > 0
                and min(abs(setup["price"] / setup["high20"] - 1), abs(setup["price"] / setup["low20"] - 1)) <= NEAR_TRIGGER}
        notes = {idea["ticker"]: {"direction": idea["direction"], "stop": idea["stop"], "targets": idea["targets"],
                                  "text": f"Trend review: {idea['direction']} · {idea['conviction']} conviction — "
                                          f"{idea.get('thesis', '')}"}
                 for idea in ideas}
        logger.info("Trade opportunities: %d scored, %d candidates, %d ideas", len(scores), len(candidates), len(ideas))
        return {"batch": batch_id, "slot": slot or str(batch_id), "ideas": ideas, "regime": regime, "day": _session_day(now),
                "watch_bars": {**{t: bars[t] for t in watchlist if t in bars}, **near}, "notes": notes,
                "scored": len(scores), "candidates": len(candidates)}

    # publishing (worker thread) -----------------------------------------------
    def _publish(self, result: Dict[str, Any], regular_session: bool) -> None:
        if self.breakouts is not None:
            self.breakouts.add(result["watch_bars"], result["day"], result["notes"])
        if self._announced_day != result["day"]:
            self._announced_day, self._announced = result["day"], {}
        fresh, repeats = [], []
        for idea in result["ideas"]:
            if self._held is not None:
                idea["held_note"] = self._held(idea["ticker"])  # None when holdings are unknown
            if self._held_side is not None:
                idea["held_side"] = self._held_side(idea["ticker"])
            previous = self._announced.get(idea["ticker"])
            (repeats if previous == f"{idea['direction']}:{idea['conviction']}" else fresh).append(idea)
        if not fresh:
            self._save_state(result["day"], published=result["slot"])
            return  # nothing new since the last run today
        follow = {idea["ticker"] for idea in fresh if idea["conviction"] == "high"
                  and not geared_fund(idea.get("name", ""))} if regular_session else set()
        scanned = (f"Scanned {result['scored']} stocks · {result['candidates']} strong trends reviewed · "
                   f"{len(result['ideas'])} passed")
        event = self.emit("trade_opportunities", {"underlying": "", "message": format_message(
            fresh, repeats, result["regime"], options_follow=follow, scanned=scanned)}, f"opportunities:{result['slot']}")
        if event is None:
            self._save_state(result["day"], published=result["slot"])
            return  # already sent before a restart; its ideas were recorded then
        for idea in result["ideas"]:
            self._announced[idea["ticker"]] = f"{idea['direction']}:{idea['conviction']}"
        jobs = {}
        for idea in fresh:
            if idea["ticker"] not in follow:
                continue
            bullish = idea["direction"] == "long"
            try:
                request = TradeAdviceRequest(
                    ticker=idea["ticker"], data_mode="live", horizon="swing",
                    direction="bullish" if bullish else "bearish",
                    message=(f"Swing {'long' if bullish else 'short'} idea, high conviction: {idea.get('thesis', '')} "
                             f"Entry {idea.get('entry', '')}; stop {idea['stop']}; targets "
                             f"{', '.join(str(t) for t in idea['targets'])}; horizon {idea.get('horizon') or '1-3 weeks'}. "
                             + (f"Next earnings {idea['earnings']}: weigh expiries after it (IV crush, gap risk). "
                                if idea.get("earnings") else "")
                             + "Suggest defined-risk options that fit this move. Recommend waiting if none fits."))
                job = self.service.submit(request, source="opportunity")
            except Exception as exc:
                logger.info("Options comparison for %s not submitted: %s", idea["ticker"], type(exc).__name__)
                continue
            jobs[job["id"]] = f"{'long' if bullish else 'short'} · high"
        self._save_state(result["day"], published=result["slot"])
        if jobs:
            self._pending = {"jobs": jobs, "batch": result["slot"], "started": self._now()}

    def _finish_options(self) -> None:
        pending = self._pending
        jobs = [self.service.repo.advice(job_id) for job_id in pending["jobs"]]
        done = [job for job in jobs if job and job["status"] not in {"queued", "running"}]
        if len(done) < len(jobs) and self._now() - pending["started"] < JOB_TIMEOUT:
            return
        self._pending = None
        lines = []
        for job in done:
            lines.extend(format_options(job, pending["jobs"][job["id"]]))
        if lines:
            self.emit("options_ideas", {"underlying": "", "message": "\n".join(lines)},
                      f"options-ideas:{pending['batch']}")


NOT_TICKERS = {"MARKET"}  # the market review row
CATCHUP = timedelta(minutes=60)  # the scheduler restarts an interrupted run up to an hour late


def _configured_schedule_times() -> List[str]:
    from src.config import get_config
    from src.scheduler import normalize_schedule_times
    config = get_config()
    return normalize_schedule_times(getattr(config, "schedule_times", None),
                                    fallback_time=getattr(config, "schedule_time", "18:00"))


def _naive(now: datetime) -> datetime:
    """Report rows carry naive local timestamps."""
    return now.astimezone().replace(tzinfo=None) if now.tzinfo else now
