# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""US equity snapshot via yfinance.

US snapshot provider for the screening L1 pipeline. Fetches a configurable
equity universe and returns the standard snapshot DataFrame schema.

HK is not supported yet: there is no HK universe source or ticker
configuration path, so ``market="hk"`` is rejected at the pipeline level
rather than silently screening the US pool.
"""

import logging
import math
import os
from datetime import datetime, timezone
from io import StringIO
from urllib.request import Request, urlopen

from data_provider.us_session import NEW_YORK, MAX_QUOTE_AGE_SECONDS, session_window, us_daily_history_end
import pandas as pd

logger = logging.getLogger(__name__)

_SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

_DEFAULT_US_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B",
    "AVGO", "JPM", "LLY", "V", "MA", "UNH", "XOM", "COST", "HD", "PG",
    "JNJ", "ABBV", "WMT", "NFLX", "BAC", "KO", "CRM", "CVX", "MRK",
    "PEP", "AMD", "TMO", "LIN", "ACN", "CSCO", "MCD", "ABT", "ADBE",
    "WFC", "GE", "DHR", "TXN", "PM", "ISRG", "MS", "NEE", "INTU",
    "DIS", "QCOM", "CAT", "NOW",
]


def fetch_us_universe(source: str = "auto") -> list[str]:
    """Return a list of US equity tickers.

    Sources:
        sp500   — scrape S&P 500 from Wikipedia
        env     — read SCREENING_US_TICKERS (comma-separated)
        default — hardcoded top-50 US large-caps
        auto    — try explicit env → sp500 → default
    """
    src = source.lower()
    if src == "auto":
        for s in ("env", "sp500", "default"):
            try:
                tickers = fetch_us_universe(s)
                if tickers:
                    logger.info("US universe from %s: %d tickers", s, len(tickers))
                    return tickers
            except Exception as e:
                logger.debug("US universe source %s failed: %s", s, e)
        return list(_DEFAULT_US_UNIVERSE)

    if src == "sp500":
        return _fetch_sp500_tickers()
    elif src == "env":
        raw = os.getenv("SCREENING_US_TICKERS", "").strip()
        if not raw:
            raise ValueError("SCREENING_US_TICKERS not set")
        return list(dict.fromkeys(t.strip().upper().replace(".", "-") for t in raw.split(",") if t.strip()))
    elif src == "default":
        return list(_DEFAULT_US_UNIVERSE)
    else:
        raise ValueError(f"Unknown US universe source: {source}")


def _fetch_sp500_tickers() -> list[str]:
    request = Request(_SP500_WIKI_URL, headers={"User-Agent": "daily-stock-analysis/1.0"})
    with urlopen(request, timeout=10) as response:
        tables = pd.read_html(StringIO(response.read().decode("utf-8")))
    for tbl in tables:
        if "Symbol" in tbl.columns:
            return sorted(tbl["Symbol"].dropna().str.strip().str.replace(".", "-", regex=False).tolist())
    raise RuntimeError("Could not find Symbol column in S&P 500 Wikipedia table")


def _ticker_frame(data, ticker):
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values("Ticker"):
            return pd.DataFrame()
        data = data.xs(ticker, axis=1, level="Ticker")
    return data.dropna(subset=["Close"]).copy()


def _session_row(ticker, intraday, daily, now, session, start, end):
    """Build one row only from a fresh bar in the requested session."""
    if intraday.empty or intraday.index.tz is None:
        return None
    intraday.index = intraday.index.tz_convert(NEW_YORK)
    bars = intraday[(intraday.index >= start) & (intraday.index < end) & (intraday.index <= now)]
    if bars.empty:
        return None
    stamp = bars.index[-1]
    age = (now - stamp).total_seconds()
    if age < 0 or age > MAX_QUOTE_AGE_SECONDS:
        return None
    price = float(bars.iloc[-1]["Close"])
    if not math.isfinite(price) or price <= 0:
        return None
    # Pre/open moves use the prior regular close; after-hours moves use today's close.
    reference = None
    day_change_pct = day_amount = None
    if session == "postmarket":
        from src.core.trading_calendar import get_market_session_bounds
        opening, closing = get_market_session_bounds("us", now)
        regular = intraday[(intraday.index >= opening) & (intraday.index < closing)]
        if not regular.empty and (closing - regular.index[-1]).total_seconds() <= 10 * 60:
            reference = float(regular.iloc[-1]["Close"])
            # The day's regular-session move beside the after-hours one (today's close vs the prior close).
            earlier = daily[[d.date() < now.date() for d in daily.index]] if not daily.empty else daily
            previous = float(earlier.iloc[-1]["Close"]) if not earlier.empty else None
            if previous and math.isfinite(previous) and previous > 0:
                day_change_pct = round((reference / previous - 1) * 100, 4)
            regular_volume = regular["Volume"].fillna(0).clip(lower=0)
            day_amount = float((regular_volume * regular["Close"]).sum())
    else:
        from datetime import datetime as _datetime, time as _time
        from src.core.trading_calendar import get_effective_trading_date, get_market_session_bounds
        reference_date = get_effective_trading_date("us", current_time=now)
        previous = daily[[d.date() == reference_date for d in daily.index]] if not daily.empty else daily
        if not previous.empty:
            reference = float(previous.iloc[-1]["Close"])
        else:
            # Yahoo's daily series occasionally omits a session its 5-minute bars
            # contain. Use that session's last regular bar, under the same
            # 10-minute-to-close rule as the after-hours reference above.
            opening, closing = get_market_session_bounds(
                "us", _datetime.combine(reference_date, _time(12), NEW_YORK))
            if opening is not None and closing is not None:
                regular = intraday[(intraday.index >= opening) & (intraday.index < closing)]
                if not regular.empty and (closing - regular.index[-1]).total_seconds() <= 10 * 60:
                    reference = float(regular.iloc[-1]["Close"])
    if reference is None or not math.isfinite(reference) or reference <= 0:
        return None
    volume = bars["Volume"].fillna(0).clip(lower=0)
    change_pct = round((price / reference - 1) * 100, 4)
    return {
        "day_change_pct": change_pct if session == "regular" else day_change_pct,
        "day_amount": day_amount,
        "code": ticker.replace("-", "."), "name": ticker.replace("-", "."), "price": price,
        "change_pct": change_pct,
        "amount": float((volume * bars["Close"]).sum()),  # estimated session dollar volume
        "volume": int(volume.sum()), "total_mv": None, "circ_mv": None,
        "pe_ratio": None, "pb_ratio": None, "volume_ratio": None,
        "turnover_rate": None, "industry": "", "quote_session": session,
        "provider_timestamp": stamp.isoformat(), "is_stale": False,
        "reference_price": reference,
    }


def fetch_us_snapshot(tickers=None, *, universe_source=None, max_workers=8, now=None):
    """Fresh five-minute session bars, never an old daily close presented as live.

    Amount is estimated USD traded within this session. Relative volume is left
    unavailable: partial-session volume is not comparable to full-day averages.
    """
    import yfinance as yf

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("US snapshot time must include a timezone")
    now = now.astimezone(NEW_YORK)
    session, start, end = session_window(now)
    source = universe_source or os.getenv("SCREENING_US_UNIVERSE", "auto")
    symbols = list(dict.fromkeys(str(t).strip().upper().replace(".", "-") for t in
                               (tickers if tickers is not None else fetch_us_universe(source)) if str(t).strip()))
    columns = ["code", "name", "price", "change_pct", "amount", "volume", "total_mv", "circ_mv",
               "pe_ratio", "pb_ratio", "volume_ratio", "turnover_rate", "industry", "quote_session",
               "provider_timestamp", "is_stale", "reference_price", "day_change_pct", "day_amount"]
    attrs = {"snapshot_source": "yfinance_5m", "session": session, "as_of": now.isoformat(),
             "universe_source": source, "requested_count": len(symbols), "source_errors": [],
             "coverage_note": "Configured US universe only; not whole-exchange breadth. "
                              "Five-minute bars may be delayed. Amount is estimated session USD volume."}
    if session == "closed" or not symbols:
        frame = pd.DataFrame(columns=columns)
        frame.attrs.update(attrs, available_count=0, excluded_count=len(symbols))
        frame.attrs["source_errors"] = ["No active supported US session or empty universe"]
        return frame
    kwargs = dict(group_by="ticker", auto_adjust=False, progress=False,
                  threads=max(1, min(max_workers, 8)), timeout=10)
    intraday = yf.download(symbols, period="5d", interval="5m", prepost=True, **kwargs)
    daily = yf.download(symbols, period="1mo", interval="1d", **kwargs)
    rows = []
    for ticker in symbols:
        try:
            row = _session_row(ticker, _ticker_frame(intraday, ticker), _ticker_frame(daily, ticker),
                               now, session, start, end)
            if row:
                rows.append(row)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("US session data unavailable for %s: %s", ticker, exc)
    frame = pd.DataFrame(rows, columns=columns)
    frame.attrs.update(attrs, available_count=len(rows), excluded_count=len(symbols) - len(rows))
    frame.attrs["source_errors"] = [
        f"US {session}: {len(rows)}/{len(symbols)} fresh symbols; "
        f"{len(symbols) - len(rows)} missing/stale/baseline-unavailable. {attrs['coverage_note']}"
    ]
    if session in {"premarket", "postmarket"}:
        missing_volume = int(frame["amount"].fillna(0).le(0).sum())
        if missing_volume:
            frame.attrs["source_errors"].append(
                f"US {session}: volume unavailable/unverified for {missing_volume}/{len(frame)} fresh symbols. "
                "Liquidity-qualified screening excludes these prices; zero reported volume does not verify liquidity."
            )
    return frame


def fetch_daily_history_yfinance(
    ticker: str,
    *,
    lookback_days: int = 120,
) -> pd.DataFrame:
    """Fetch daily OHLCV history for a US ticker via yfinance.

    Returns a DataFrame with columns: date, open, high, low, close, volume
    matching the schema expected by the daily enrichment logic.
    """
    import yfinance as yf

    end = pd.Timestamp(us_daily_history_end())
    start = end - pd.Timedelta(days=max(lookback_days * 2, 180))
    hist = yf.download(
        ticker.replace(".", "-"),
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
    )
    if hist is None or hist.empty:
        raise RuntimeError(f"yfinance daily history empty for {ticker}")

    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.droplevel("Ticker")

    hist = hist.tail(max(lookback_days, 30)).copy()
    hist = hist.rename(columns={
        "Open": "开盘", "High": "最高", "Low": "最低",
        "Close": "收盘", "Volume": "成交量",
    })
    hist.index.name = "日期"
    hist = hist.reset_index()
    return hist
