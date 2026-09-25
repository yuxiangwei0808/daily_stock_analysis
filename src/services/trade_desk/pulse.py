"""Market-hours watch over the watchlist: deterministic move rules plus cheap news triage.

Enabled with ``MARKET_PULSE_ENABLED=true``; runs inside the Trade Desk worker
(leader only, regular session only) and emits ``market_move`` /
``market_news`` events that the worker delivers to Discord.

- Moves (every 60 s, one batched OpenD snapshot): the day change crossing a
  level in ``MARKET_PULSE_MOVE_LEVELS`` (default 3,5,8 %) alerts once per level
  per day; a move of ``MARKET_PULSE_FAST_MOVE_PCT`` (default 2 %) within 15
  minutes alerts at most every 30 minutes per stock.
- News (every 10 minutes, in the background): Google News for a rotating third
  of the watchlist; new headlines are rated for materiality by the routine
  generation backend in one call, with a keyword rule as fallback. Only
  high-materiality items alert.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")

_US_TICKER = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")
_MATERIAL_WORDS = re.compile(
    r"\b(earnings|guidance|outlook|forecast|beats?|miss(es|ed)?|downgrade[sd]?|upgrade[sd]?|acquir\w*|merger|"
    r"buyout|takeover|FDA|SEC|DOJ|FTC|antitrust|lawsuit|sued|probe|investigation|recall|halt\w*|bankrupt\w*|"
    r"offering|dilution|layoffs?|resign\w*|CEO|CFO|contract|partnership|stake|buyback|dividend|split)\b",
    re.IGNORECASE)
QUOTE_SECONDS = 60
NEWS_SECONDS = 600
FAST_WINDOW = timedelta(minutes=15)
FAST_COOLDOWN = timedelta(minutes=30)
NEWS_TICKERS_PER_SWEEP = 11
NEWS_MAX_AGE = timedelta(hours=2)
NEWS_DAILY_LIMIT = 20
OPENING_GRACE = timedelta(minutes=15)  # opening prints swing widely; day levels still alert
FAST_DAILY_LIMIT = 3
_LEVERAGE = [(re.compile(r"\b3x\b|ultrapro", re.I), 3.0),
             (re.compile(r"\b2x\b|\bproshares ultra(short)?\b", re.I), 2.0)]
_NAME_STOPWORDS = {"the", "inc", "corp", "corporation", "company", "group", "holdings", "trust", "fund",
                   "daily", "etf", "shares", "class", "ltd", "plc", "technologies", "spdr", "ishares"}


def leverage(name: str) -> float:
    """Leveraged ETFs move a multiple of their index, so thresholds scale with them."""
    for pattern, factor in _LEVERAGE:
        if pattern.search(name or ""):
            return factor
    return 1.0


def mentions(title: str, ticker: str, name: str = "") -> bool:
    """Whether a headline is about this stock: its ticker or the first distinctive word of its name."""
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", title):
        return True
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z&'-]+", name or "") if word.lower() not in _NAME_STOPWORDS]
    return bool(words) and len(words[0]) >= 4 and re.search(rf"\b{re.escape(words[0])}\b", title, re.I) is not None


def enabled() -> bool:
    return os.getenv("MARKET_PULSE_ENABLED", "false").strip().lower() == "true"


def _levels() -> List[float]:
    try:
        values = sorted({abs(float(item)) for item in os.getenv("MARKET_PULSE_MOVE_LEVELS", "3,5,8").split(",")
                         if item.strip()})
    except ValueError:
        values = [3.0, 5.0, 8.0]
    return [value for value in values if value > 0] or [3.0, 5.0, 8.0]


def _fast_pct() -> float:
    try:
        return abs(float(os.getenv("MARKET_PULSE_FAST_MOVE_PCT", "2")))
    except ValueError:
        return 2.0


def watch_tickers() -> List[str]:
    from src.config import get_config
    return [code.strip().upper() for code in (get_config().stock_list or []) if _US_TICKER.match(code.strip())]


def _rate_with_llm(items: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
    """Ask the routine model to rate headlines 0-3; None when it is unavailable."""
    from src.agent.runner import try_parse_json
    from .advisor import _generation_backend

    prompt = (
        "Rate how likely each headline is to move its stock's price materially today.\n"
        "3 = major (earnings/guidance surprise, M&A, regulatory action, trading halt, large contract, "
        "executive exit, offering); 2 = notable (analyst rating change, lawsuit, product news with numbers); "
        "1 = minor; 0 = noise, listicles, opinion, price recaps.\n"
        "Return only JSON: {\"ratings\": [{\"id\": <id>, \"score\": 0-3, \"why\": <at most 12 words>}]}\n"
        + json.dumps([{key: item[key] for key in ("id", "ticker", "title", "source")} for item in items],
                     ensure_ascii=False))
    try:
        backend, _backend_id = _generation_backend()
        result = backend.generate(prompt, {"temperature": 0, "max_output_tokens": 4096})
        data = try_parse_json(result.text or "") or {}
        ratings = {str(row.get("id")): row for row in data.get("ratings", []) if isinstance(row, dict)}
        return ratings or None
    except Exception as exc:  # the keyword rule covers an unavailable model
        logger.info("Market pulse news rating unavailable: %s", type(exc).__name__)
        return None


class MarketPulse:
    def __init__(self, provider: Callable[[], Any], emit: Callable[[str, Dict[str, Any], str], Any], *,
                 tickers: Callable[[], List[str]] = watch_tickers,
                 fetch_news: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
                 rate_news: Callable[[List[Dict[str, Any]]], Optional[Dict[str, Dict[str, Any]]]] = _rate_with_llm,
                 clock: Callable[[], float] = time.monotonic):
        self._provider = provider
        self._emit = emit
        self._tickers = tickers
        self._fetch_news = fetch_news or self._google_news
        self._rate_news = rate_news
        self._clock = clock
        self._next_quotes = 0.0
        self._next_news = 0.0
        self._history: Dict[str, deque] = {}
        self._fast_alerted: Dict[str, datetime] = {}
        self._fast_counts: Dict[tuple, int] = {}
        self._level_high: Dict[tuple, float] = {}  # (day, ticker, sign) -> highest level alerted
        self._names: Dict[str, str] = {}
        self._headlines: Dict[str, List[Dict[str, Any]]] = {}
        self._seen: set = set()
        self._warmed: set = set()
        self._news_cursor = 0
        self._news_alerts: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-pulse-news")
        self._news_future = None

    def stop(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _google_news(ticker: str) -> List[Dict[str, Any]]:
        from src.services.free_news import google_news
        return google_news(f"{ticker} stock", days=1, limit=8)

    def tick(self, now: Optional[datetime] = None, session: Optional[str] = None) -> None:
        from data_provider.us_session import session_window
        now = now or datetime.now(timezone.utc)
        if (session or session_window(now)[0]) != "regular":
            return
        clock = self._clock()
        if clock >= self._next_quotes:
            self._next_quotes = clock + QUOTE_SECONDS
            self.check_moves(now)
        if clock >= self._next_news and (self._news_future is None or self._news_future.done()):
            self._next_news = clock + NEWS_SECONDS
            self._news_future = self._pool.submit(self._safe_news_sweep, now)

    # -- moves ---------------------------------------------------------
    def check_moves(self, now: datetime) -> None:
        tickers = self._tickers()
        if not tickers:
            return
        quotes = self._provider().watchlist_quotes(tickers)
        local = now.astimezone(_NEW_YORK)
        day = local.date().isoformat()
        self._prune(day)
        opening = datetime.combine(local.date(), datetime.min.time().replace(hour=9, minute=30), _NEW_YORK)
        in_grace = local < opening + OPENING_GRACE
        levels, fast_pct = _levels(), _fast_pct()
        for ticker, quote in quotes.items():
            price, change = quote.get("price"), quote.get("change_pct")
            if not price:
                continue
            if quote.get("name"):
                self._names[ticker] = quote["name"]
            factor = leverage(self._names.get(ticker, ""))
            if change is not None:
                crossed = [level for level in levels if abs(change) >= level * factor]
                sign = "+" if change > 0 else "-"
                # A move giving back gains crosses lower levels again; only new highs alert.
                if crossed and crossed[-1] * factor > self._level_high.get((day, ticker, sign), 0):
                    level = crossed[-1] * factor
                    self._level_high[(day, ticker, sign)] = level
                    self._emit("market_move", {
                        "underlying": ticker, "kind": "day_move", "price": price, "change_pct": change,
                        "message": f"{ticker} {'up' if change > 0 else 'down'} {abs(change):.1f}% today "
                                   f"(past {sign}{level:g}%) at {price:.2f}.{self._reason(ticker)}"},
                        f"pulse-move:{day}:{ticker}:{sign}{level:g}")
            history = self._history.setdefault(ticker, deque())
            history.append((now, price))
            while history and now - history[0][0] > FAST_WINDOW:
                history.popleft()
            low = min(value for _, value in history)
            high = max(value for _, value in history)
            rise, fall = (price / low - 1) * 100, (price / high - 1) * 100
            move = rise if rise >= -fall else fall
            last = self._fast_alerted.get(ticker)
            count = self._fast_counts.get((day, ticker), 0)
            if (abs(move) >= fast_pct * factor and not in_grace and count < FAST_DAILY_LIMIT
                    and (last is None or now - last >= FAST_COOLDOWN)):
                self._fast_alerted[ticker] = now
                self._fast_counts[(day, ticker)] = count + 1
                self._emit("market_move", {
                    "underlying": ticker, "kind": "fast_move", "price": price, "change_pct": change,
                    "message": f"{ticker} moved {move:+.1f}% within 15 min to {price:.2f}"
                               + (f" (day {change:+.1f}%)" if change is not None else "") + f".{self._reason(ticker)}"},
                    f"pulse-fast:{ticker}:{now.strftime('%Y%m%d%H%M')}")

    def _prune(self, day: str) -> None:
        """Day-keyed state from earlier days and old headline hashes are dropped (weeks of uptime)."""
        if getattr(self, "_pruned_day", None) == day:
            return
        self._pruned_day = day
        self._level_high = {k: v for k, v in self._level_high.items() if k[0] == day}
        self._fast_counts = {k: v for k, v in self._fast_counts.items() if k[0] == day}
        self._news_alerts = {k: v for k, v in self._news_alerts.items() if k == day}
        if len(self._seen) > 20000:
            self._seen = set(list(self._seen)[-10000:])

    def _reason(self, ticker: str) -> str:
        with self._lock:
            recent = self._headlines.get(ticker) or []
        return f" Latest news: {recent[0]['title']} ({recent[0]['source']})" if recent else ""

    # -- news ----------------------------------------------------------
    def _safe_news_sweep(self, now: datetime) -> None:
        try:
            self.news_sweep(now)
        except Exception as exc:  # the next sweep retries; moves are unaffected
            logger.warning("Market pulse news sweep failed: %s", type(exc).__name__)

    def news_sweep(self, now: datetime) -> None:
        tickers = self._tickers()
        if not tickers:
            return
        start = self._news_cursor % len(tickers)
        batch = (tickers[start:] + tickers[:start])[:NEWS_TICKERS_PER_SWEEP]
        self._news_cursor = start + len(batch)
        fresh: List[Dict[str, Any]] = []
        for ticker in batch:
            try:
                items = self._fetch_news(ticker)
            except Exception as exc:
                logger.info("Market pulse news unavailable for %s: %s", ticker, type(exc).__name__)
                continue
            name = self._names.get(ticker, "")
            recent = [item for item in items if item.get("published_at")
                      and now - datetime.fromisoformat(item["published_at"]) <= NEWS_MAX_AGE
                      and mentions(item["title"], ticker, name)]
            with self._lock:
                self._headlines[ticker] = recent
            for item in recent:
                key = hashlib.sha1(f"{ticker}|{item['title'].lower()}".encode()).hexdigest()[:16]
                if key in self._seen:
                    continue
                self._seen.add(key)
                # The first look at a ticker after start only learns what is already out.
                if ticker in self._warmed:
                    fresh.append({"id": key, "ticker": ticker, "title": item["title"],
                                  "source": item.get("source", ""), "url": item.get("url", "")})
            self._warmed.add(ticker)
        if not fresh:
            return
        ratings = self._rate_news(fresh)
        day = now.astimezone(_NEW_YORK).date().isoformat()
        for item in fresh:
            if ratings is not None:
                rating = ratings.get(item["id"]) or {}
                try:
                    score = int(rating.get("score", 0))
                except (TypeError, ValueError):
                    score = 0
                why = str(rating.get("why") or "")[:120]
            else:
                score, why = (3, "keyword match") if _MATERIAL_WORDS.search(item["title"]) else (0, "")
            if score < 3 or self._news_alerts.get(day, 0) >= NEWS_DAILY_LIMIT:
                continue
            self._news_alerts[day] = self._news_alerts.get(day, 0) + 1
            self._emit("market_news", {
                "underlying": item["ticker"], "kind": "news", "title": item["title"], "url": item["url"],
                "message": f"{item['ticker']}: {item['title']} ({item['source']})"
                           + (f" — {why}" if why else "") + (f"\n{item['url']}" if item["url"] else "")},
                f"pulse-news:{item['id']}")
