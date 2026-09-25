# -*- coding: utf-8 -*-
"""Free news sources that need no paid search plan.

- ``google_news``: Google News RSS search (no key). Aggregates headlines from
  Reuters, CNBC, MarketWatch, Yahoo Finance, Barchart and other outlets.
- ``yahoo_finance``: Yahoo Finance ticker news via yfinance (no key).
- ``finnhub``: Finnhub company news (free API key, ``FINNHUB_API_KEY``).

Enabled with ``FREE_NEWS_SOURCES``. Every source is best-effort: a failing
source returns nothing and never blocks analysis.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

FREE_NEWS_SOURCES = ("google_news", "yahoo_finance", "finnhub")
_GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
_FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; daily-stock-analysis news reader)"}
_TIMEOUT = 8
_CACHE_SECONDS = 600
_cache: Dict[tuple, tuple] = {}
_cache_lock = threading.Lock()


def parse_sources(value: Any) -> List[str]:
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [item for item in dict.fromkeys(str(v).strip().lower() for v in items) if item in FREE_NEWS_SOURCES]


def _item(title, url, source, published, summary, feed) -> Dict[str, Any]:
    return {"title": " ".join(str(title or "").split())[:300], "url": str(url or ""),
            "source": str(source or ""), "published_at": published.isoformat() if published else "",
            "summary": " ".join(re.sub(r"<[^>]+>", " ", str(summary or "")).split())[:500], "feed": feed}


def google_news(query: str, days: int = 3, limit: int = 10) -> List[Dict[str, Any]]:
    """Search Google News RSS; results are the newest matching headlines."""
    response = requests.get(_GOOGLE_NEWS_URL, headers=_HEADERS, timeout=_TIMEOUT, params={
        "q": f"{query} when:{max(1, int(days))}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    response.raise_for_status()
    items = []
    for node in ET.fromstring(response.content).findall("./channel/item"):
        source = node.findtext("source") or ""
        title = node.findtext("title") or ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        items.append(_item(title, node.findtext("link"), source, published, "", "google_news"))
    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items[:limit]


def yahoo_finance_news(ticker: str, limit: int = 10) -> List[Dict[str, Any]]:
    import yfinance as yf

    items = []
    for raw in yf.Ticker(ticker).get_news(count=limit) or []:
        content = raw.get("content") or raw
        published = None
        try:
            published = datetime.fromisoformat(str(content.get("pubDate")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
        url = ((content.get("canonicalUrl") or {}).get("url") or (content.get("clickThroughUrl") or {}).get("url"))
        items.append(_item(content.get("title"), url, (content.get("provider") or {}).get("displayName"),
                           published, content.get("summary"), "yahoo_finance"))
    return items


def finnhub_news(ticker: str, api_key: str, days: int = 3, limit: int = 10) -> List[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    response = requests.get(_FINNHUB_URL, headers=_HEADERS, timeout=_TIMEOUT, params={
        "symbol": ticker, "from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat(),
        "token": api_key})
    response.raise_for_status()
    items = [_item(raw.get("headline"), raw.get("url"), raw.get("source"),
                   datetime.fromtimestamp(raw["datetime"], timezone.utc) if raw.get("datetime") else None,
                   raw.get("summary"), "finnhub") for raw in response.json() or []]
    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items[:limit]


def _names(ticker: str) -> List[str]:
    try:
        from src.data.stock_mapping import STOCK_ENGLISH_NAME_MAP
        aliases = STOCK_ENGLISH_NAME_MAP.get(ticker.upper(), ())
    except Exception:
        aliases = ()
    return [ticker.upper(), *aliases]


def _dedupe(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, result = set(), []
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item["title"].lower()).strip()[:80]
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def ticker_news(ticker: str, sources: Iterable[str], *, days: int = 3, limit: int = 12,
                finnhub_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Recent news for a US ticker from the enabled free sources, newest first (cached 10 minutes)."""
    sources = [item for item in parse_sources(list(sources)) if item != "finnhub" or finnhub_key]
    if not sources:
        return []
    key = (ticker.upper(), tuple(sources), days, limit)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return list(cached[1])
    fetchers = {"google_news": lambda: google_news(f"{ticker} stock", days=days, limit=limit),
                "yahoo_finance": lambda: yahoo_finance_news(ticker, limit=limit),
                "finnhub": lambda: finnhub_news(ticker, finnhub_key, days=days, limit=limit)}

    def fetch(name):
        try:
            return fetchers[name]()
        except Exception as exc:  # best-effort: one source failing never blocks the others
            logger.info("Free news source %s unavailable for %s: %s", name, ticker, type(exc).__name__)
            return []

    with ThreadPoolExecutor(max_workers=len(sources), thread_name_prefix="free-news") as pool:
        batches = list(pool.map(fetch, sources))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    merged = [item for batch in batches for item in batch if not item["published_at"] or item["published_at"] >= cutoff]
    # Ticker feeds (Yahoo) mix in general market stories; items naming the
    # stock come first and are flagged, the rest remain as market context.
    names = _names(ticker)
    for item in merged:
        text = f"{item['title']} {item['summary']}"
        item["related"] = item["feed"] != "yahoo_finance" or any(
            re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text, re.IGNORECASE if len(name) > 5 else 0)
            for name in names)
    merged.sort(key=lambda item: (item["related"], item["published_at"]), reverse=True)
    result = _dedupe(merged)[:limit]
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return list(result)
