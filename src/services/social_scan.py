"""Social media scan: which stocks retail traders are talking about (free public sources; context only).

Sources, all free and keyless, cached for ``CACHE_SECONDS``:

- ApeWisdom: mentions across the stock subreddits over 24 hours, with the rank and count a day earlier.
- Stocktwits: the trending list, with Stocktwits' own one-paragraph summary of why a name is trending,
  and per ticker the bullish/bearish tags on the latest 30 messages.
- Tradestie: WallStreetBets' 50 most-commented tickers with a sentiment tag.

X has no free API, and Reddit's own JSON refuses this kind of client, so neither is read directly.

Retail attention is context, not an edge: heavily discussed stocks have tended to do worse
afterwards, not better. Reports and Trade Desk answers get it as background (the prompt says
it must not change a score or an action); the evening digest lists the most-discussed names,
and each of them is followed forward in the idea tracker (kind ``social``) to test it here.
Enable with ``SOCIAL_SCAN_ENABLED=true``; US tickers only.
"""
from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

CACHE_SECONDS = 900
FAILURE_SECONDS = 120  # a source that failed is not asked again for two minutes
TOP = 20  # a ticker "is hot" on a source when it ranks in that source's top 20
HOT_LIMIT = 10
# Index funds are always discussed; they say nothing about a stock and are not tracked.
BROAD_FUNDS = {"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "SPX", "NDX", "VIX", "TQQQ", "SQQQ", "SPXL", "SPXS"}
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")

_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("SOCIAL_SCAN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _get_json(url: str) -> Any:
    import requests
    response = requests.get(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}, timeout=10)
    response.raise_for_status()
    return response.json()


def _cached(key: str, loader: Callable[[], Any]) -> Any:
    """``loader()`` at most once per ``CACHE_SECONDS``; after a failure, None for ``FAILURE_SECONDS``."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now < hit[0]:
            return hit[1]
    try:
        value, ttl = loader(), CACHE_SECONDS
    except Exception as exc:  # one source down never blocks the others
        logger.info("Social scan source %s unavailable: %s", key.split(":")[0], type(exc).__name__)
        value, ttl = None, FAILURE_SECONDS
    with _lock:
        _cache[key] = (now + ttl, value)
    return value


def _reddit() -> Dict[str, Dict[str, Any]]:
    rows = _get_json("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1").get("results") or []
    out = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if _TICKER.match(ticker) and ticker not in out:
            out[ticker] = {"rank": int(row.get("rank") or len(out) + 1), "mentions": int(row.get("mentions") or 0),
                           "mentions_before": _int(row.get("mentions_24h_ago")),
                           "rank_before": _int(row.get("rank_24h_ago")),
                           "name": html.unescape(str(row.get("name") or ""))}
    return out


def _stocktwits_trending() -> Dict[str, Dict[str, Any]]:
    rows = _get_json("https://api.stocktwits.com/api/2/trending/symbols.json").get("symbols") or []
    out = {}
    for index, row in enumerate(rows):
        ticker = str(row.get("symbol") or "").upper()
        if (not _TICKER.match(ticker) or ticker.endswith(".X") or str(row.get("region") or "US") != "US"
                or str(row.get("instrument_class") or "Stock") == "Crypto"):
            continue  # crypto (BTC.X) and foreign listings
        summary = " ".join(str((row.get("trends") or {}).get("summary") or "").split())
        out.setdefault(ticker, {"rank": index + 1, "summary": summary[:400], "name": str(row.get("title") or "")})
    return out


def _wsb() -> Dict[str, Dict[str, Any]]:
    rows = _get_json("https://tradestie.com/api/v1/apps/reddit")
    out = {}
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        ticker = str(row.get("ticker") or "").upper()
        if _TICKER.match(ticker):
            out.setdefault(ticker, {"rank": index + 1, "comments": _int(row.get("no_of_comments")),
                                    "sentiment": str(row.get("sentiment") or "").lower() or None})
    return out


def _stocktwits_messages(ticker: str) -> Optional[Dict[str, int]]:
    rows = _get_json(f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json").get("messages") or []
    tags = [((row.get("entities") or {}).get("sentiment") or {}).get("basic") for row in rows]
    return {"messages": len(rows), "bullish": tags.count("Bullish"), "bearish": tags.count("Bearish")}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sources() -> Dict[str, Optional[Dict[str, Dict[str, Any]]]]:
    """Each source's table (None when it is unavailable)."""
    return {"reddit": _cached("reddit", _reddit), "stocktwits": _cached("stocktwits", _stocktwits_trending),
            "wsb": _cached("wsb", _wsb)}


def ticker_context(ticker: str) -> Optional[Dict[str, Any]]:
    """What the three sources say about one ticker, or None when none of them could be read."""
    symbol = str(ticker or "").strip().upper().replace("-", ".")
    if not _TICKER.match(symbol):
        return None
    tables = sources()
    messages = _cached(f"messages:{symbol}", lambda: _stocktwits_messages(symbol))
    if all(table is None for table in tables.values()) and messages is None:
        return None
    return {"ticker": symbol, "as_of": time.strftime("%Y-%m-%d %H:%M", time.gmtime()) + " UTC",
            **{name: (table or {}).get(symbol) for name, table in tables.items()},
            "unavailable": [name for name, table in tables.items() if table is None],
            "messages": messages}


def hot_list(limit: int = HOT_LIMIT) -> List[Dict[str, Any]]:
    """The most-discussed stocks now: on the most sources' top 20 first, then by Reddit mentions."""
    tables = {name: table or {} for name, table in sources().items()}
    tickers = {ticker for table in tables.values() for ticker, row in table.items() if row["rank"] <= TOP}
    rows = []
    for ticker in tickers - BROAD_FUNDS:
        entry = {name: table.get(ticker) for name, table in tables.items()}
        on = [name for name, row in entry.items() if row and row["rank"] <= TOP]
        rows.append({"ticker": ticker, **entry, "sources": on,
                     "name": next((row.get("name") for row in entry.values() if row and row.get("name")), "")})
    # Then the best rank on any source: with Reddit's counts missing, alphabetical order must not decide.
    rows.sort(key=lambda row: (-len(row["sources"]), -((row["reddit"] or {}).get("mentions") or 0),
                               min(row[name]["rank"] for name in row["sources"]), row["ticker"]))
    return rows[:limit]


def flag(ticker: str) -> str:
    """A line for a trade idea when the name is among a source's most discussed; "" otherwise."""
    symbol = str(ticker or "").strip().upper().replace("-", ".")
    tables = sources()
    bits = [f"{label} #{row['rank']}" for name, label in (("reddit", "Reddit"), ("wsb", "WSB"), ("stocktwits", "Stocktwits"))
            for row in [(tables.get(name) or {}).get(symbol)] if row and row["rank"] <= TOP]
    return ("🔥 Much discussed: " + " · ".join(bits) + " (crowded; context only, not a signal)") if bits else ""


# -- wording ---------------------------------------------------------------------------
def _change(reddit: Dict[str, Any]) -> Optional[float]:
    before = reddit.get("mentions_before")
    return (reddit["mentions"] / before - 1) * 100 if before else None


def summary_line(context: Dict[str, Any], language: str = "en") -> str:
    """One line on the ticker's social attention, e.g. "Reddit #3 by mentions (325, +81% vs a day earlier); ..."."""
    zh = language in ("zh", "zh-CN", "zh_CN")
    parts = []
    reddit, trending, wsb, messages = (context.get("reddit"), context.get("stocktwits"), context.get("wsb"),
                                       context.get("messages"))
    if reddit:
        change = _change(reddit)
        if zh:
            parts.append(f"Reddit 提及第 {reddit['rank']} 名（{reddit['mentions']} 次"
                         + (f"，较前一日 {change:+.0f}%" if change is not None else "") + "）")
        else:
            parts.append(f"Reddit #{reddit['rank']} by mentions ({reddit['mentions']}"
                         + (f", {change:+.0f}% vs a day earlier" if change is not None else "") + ")")
    if wsb:
        mood = {"bullish": "偏多", "bearish": "偏空"}.get(wsb.get("sentiment") or "", "") if zh else (wsb.get("sentiment") or "")
        parts.append(f"WSB 评论第 {wsb['rank']} 名" + (f"（{mood}）" if mood else "") if zh else
                     f"WSB #{wsb['rank']} by comments" + (f" ({mood})" if mood else ""))
    if trending:
        parts.append(f"Stocktwits 热门第 {trending['rank']} 名" if zh else f"Stocktwits trending #{trending['rank']}")
    if messages and messages["bullish"] + messages["bearish"]:
        parts.append(f"Stocktwits 最近 {messages['messages']} 条中 {messages['bullish']} 条看多、{messages['bearish']} 条看空" if zh
                     else f"latest {messages['messages']} Stocktwits posts: {messages['bullish']} bullish, "
                          f"{messages['bearish']} bearish")
    if not (reddit or wsb or trending):
        parts.insert(0, "不在 Reddit、WSB、Stocktwits 热议榜前列" if zh else
                     "not among the most-discussed names on Reddit, WSB or Stocktwits")
    text = ("；" if zh else "; ").join(parts)
    if trending and trending.get("summary"):
        text += (f"。Stocktwits 摘要：{trending['summary']}" if zh else f". Stocktwits says: {trending['summary']}")
    return text


def prompt_section(context: Optional[Dict[str, Any]]) -> str:
    """Prompt block for the report model; empty without data."""
    if not context:
        return ""
    return f"""
### 社交媒体热度（Reddit / WSB / Stocktwits，免费公开数据，仅作背景）
- {summary_line(context, "zh")}
- 数据时间 {context['as_of']}{"；不可用：" + "、".join(context["unavailable"]) if context.get("unavailable") else ""}。
> 散户关注度只是背景，不是信号：它对之后走势的预测力尚无定论，系统正在跟踪检验。可以用它提示拥挤、情绪过热或消息驱动的波动风险；不得据此调整评分或买卖结论，也不要把帖子观点当作事实。
"""


def attach(result: Any, context: Optional[Dict[str, Any]]) -> None:
    """Store the context on the result's dashboard (data_perspective.social_scan), outside the model's answer."""
    if not context or result is None:
        return
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        dashboard = {}
        result.dashboard = dashboard
    perspective = dashboard.get("data_perspective")
    if not isinstance(perspective, dict):
        perspective = {}
        dashboard["data_perspective"] = perspective
    perspective["social_scan"] = context


def report_lines(perspective: Any, language: str = "zh") -> List[str]:
    """The social line for a report's data section; empty without data."""
    context = perspective.get("social_scan") if isinstance(perspective, dict) else None
    if not isinstance(context, dict):
        return []
    try:
        english = language not in ("zh", "zh-CN", "zh_CN")
        label = "Social attention" if english else "社交媒体热度"
        return [f"**{label}**: {summary_line(context, 'en' if english else 'zh')}", ""]
    except (KeyError, TypeError, ValueError):
        return []


def format_digest(hot: List[Dict[str, Any]], picks: List[Dict[str, Any]], day: date) -> str:
    """The evening Discord digest: the most-discussed names and today's YouTube picks."""
    lines = [f"📣 **Social scan** · {day:%b} {day.day} after the close (context, not signals)"]
    for row in hot:
        bits = []
        if row.get("reddit"):
            change = _change(row["reddit"])
            bits.append(f"Reddit #{row['reddit']['rank']} ({row['reddit']['mentions']}"
                        + (f", {change:+.0f}% d/d" if change is not None else "") + ")")
        if row.get("wsb"):
            bits.append(f"WSB #{row['wsb']['rank']}" + (f" {row['wsb']['sentiment']}" if row["wsb"].get("sentiment") else ""))
        if row.get("stocktwits"):
            bits.append(f"Stocktwits #{row['stocktwits']['rank']}")
        lines.append(f"• **{row['ticker']}** " + " · ".join(bits))
    if picks:
        lines.append("📺 **YouTube picks today**")
        for pick in picks[:15]:
            lines.append(f"• {pick['channel']}: {'🟢' if pick['group'] == 'bullish' else '🔴'} **{pick['ticker']}** "
                         f"{pick['group']}" + (f" — {pick['reason']}" if pick.get("reason") else ""))
    lines.append("Context, not a signal: whether heavily discussed names lead or lag is being tested in the weekly record.")
    return "\n".join(lines)


def records(hot: List[Dict[str, Any]], day: date) -> List[tuple]:
    """(id, record) for each hot name, followed from ``day``'s close (idea tracker kind ``social``);
    the digest passes the session after the one it was chosen in."""
    out = []
    for position, row in enumerate(hot, 1):
        out.append((f"social:{day.isoformat()}:{row['ticker']}", {
            "kind": "social", "group": "hot", "verdict": "social", "ticker": row["ticker"], "direction": "long",
            "signal_day": day.isoformat(), "entry": None, "rank": position, "sources": row["sources"],
            "reddit_change_pct": round(_change(row["reddit"]), 1) if row.get("reddit") and _change(row["reddit"]) is not None
            else None}))
    return out


class DigestJob:
    """After the close on trading days: the most-discussed names and the day's YouTube picks go to
    Discord (``social_digest``), and the names are tracked from the day's close. Worker leader only."""

    AT = (16, 20)
    RETRY_MINUTES = 15

    def __init__(self, repo: Any, emit: Callable[[str, Dict[str, Any], str], Any], *,
                 today_picks: Callable[[], List[Dict[str, Any]]] = lambda: [],
                 hot: Callable[[], List[Dict[str, Any]]] = hot_list,
                 trading_day: Optional[Callable[[date], bool]] = None):
        from concurrent.futures import ThreadPoolExecutor
        self.repo = repo
        self._emit = emit
        self._today_picks = today_picks
        self._hot = hot
        self._trading_day = trading_day
        self._done_day: Optional[date] = None
        self._retry_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="social-digest")
        self._task = None

    def stop(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def tick(self, now: datetime) -> None:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo("America/New_York"))
        day = local.date()
        trading = self._trading_day or _trading_day
        if self._done_day == day or (local.hour, local.minute) < self.AT or not trading(day):
            return
        if (self._task is not None and not self._task.done()) or (self._retry_at is not None and now < self._retry_at):
            return
        self._task = self._pool.submit(self.run, day, now)

    def run(self, day: date, now: datetime) -> None:
        from datetime import timedelta
        try:
            if self._already_sent(day):
                self._done_day, self._retry_at, self.last_error = day, None, None
                return  # sent (and its names tracked) before a restart
            answered = {name: table is not None for name, table in sources().items()} if self._hot is hot_list else {}
            hot = self._hot()
            if not hot:
                raise LookupError("no social source answered")
            if all(answered.values()):
                # Tracked first (the ids are idempotent), so a failure here retries before anything is sent.
                # Chosen after the close: they enter at the next session's close (no look-ahead).
                entry_day = _next_trading_day(day, self._trading_day or _trading_day)
                added = sum(bool(self.repo.track_idea(*item)) for item in records(hot, entry_day))
                logger.info("Social scan: %d names tracked", added)
            else:  # a partial list is not the most-discussed names: sent as context, not tracked
                logger.info("Social scan: %s unavailable; the digest is not tracked today",
                            ", ".join(name for name, ok in answered.items() if not ok))
            self._emit("social_digest", {"underlying": "", "day": day.isoformat(),
                                         "message": format_digest(hot, self._today_picks(), day)},
                       f"social-digest:{day.isoformat()}")
            self._done_day, self._retry_at, self.last_error = day, None, None
        except Exception as exc:  # retried shortly; the digest is keyed per day
            self.last_error = type(exc).__name__
            logger.warning("Social digest failed: %s", type(exc).__name__)
            self._retry_at = now + timedelta(minutes=self.RETRY_MINUTES)


    def _already_sent(self, day: date) -> bool:
        from datetime import timedelta
        since = (datetime.combine(day, datetime.min.time()) - timedelta(days=1)).isoformat()
        return any(event["payload"].get("day") == day.isoformat()
                   for event in self.repo.events(limit=10, newest=True, types=["social_digest"], since=since))


def _trading_day(day: date) -> bool:
    from src.services.trade_desk.holdings import _trading_day as trading
    return trading(day)


def _next_trading_day(day: date, trading: Callable[[date], bool]) -> date:
    from datetime import timedelta
    following = day + timedelta(days=1)
    while not trading(following):
        following += timedelta(days=1)
    return following
