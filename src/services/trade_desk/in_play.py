"""Today's in-play list: the stocks likely to move most, by the rule that passed (docs/strategy-backtest.md).

The in-play range test passed as a range selector (``register.py``, ``in_play_range``): at 09:45 the top
stocks by S = (|open - prior close| + the first 15-minute bar's high - low) / T, T the median session range
of the 10 prior sessions, moved more over the rest of the day than the other stocks. It says what will move,
not which way, so the list chooses what to watch; it is not a trade signal.

After ``RUN_AT`` on trading days, on the server holding the monitor lease, a thread reads each stock's
15-minute history (read only, ``request_history_kline``; a new stock uses one history allowance for 30
days) and scores it like the backtest. The list, highest S first, is kept in ``trade_desk_settings``
(``in_play``) for the Chart tab, where the trading assistant can start on its top three.

Universe: SPY, QQQ, IWM, DIA and the 20 large caps of the test, then the watchlist's US stocks and what you
hold, at most ``MAX_STOCKS``.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import boxes as bx
from . import register

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
RUN_AT = dtime(9, 46)
SETTING = "in_play"
TOP = 5
MAX_STOCKS = 80
PAUSE = 0.6  # seconds between history requests: OpenD allows about 60 per 30 seconds
HISTORY_DAYS = 30  # calendar days of 15-minute bars: more than the 10 prior sessions T needs
ETFS = ("SPY", "QQQ", "IWM", "DIA")
LARGE = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
         "JPM", "BAC", "XOM", "CVX", "UNH", "LLY", "COST", "WMT", "HD", "KO")


def score(rows: List[Dict[str, Any]], day: date) -> Optional[Dict[str, float]]:
    """S and its parts for ``day`` from 15-minute rows (``time_key`` = bar end); None without a first bar or T."""
    if not rows:
        return None
    bars = bx.Bars.from_rows(rows)
    days = [str(d)[:10] for d in bars.days]
    if day.isoformat() not in days:
        return None
    s = days.index(day.isoformat())
    if s < 1:
        return None
    a = int(bars.s_first[s])
    T = float(bars.T[a])
    if not T > 0 or str(bars.start[a])[11:16] != "09:30":
        return None
    prior_close = float(bars.c[bars.s_last[s - 1]])
    gap, first = float(bars.o[a]) - prior_close, float(bars.h[a] - bars.l[a])
    return {"S": round((abs(gap) + first) / T, 3), "gap_pct": round(gap / prior_close * 100, 2),
            "first_range": round(first, 4), "T": round(T, 4), "open": float(bars.o[a]), "prior_close": prior_close}


def universe(watchlist: List[str], held: List[str]) -> List[str]:
    names = [*ETFS, *LARGE, *watchlist, *held]
    return list(dict.fromkeys(name.strip().upper() for name in names if name and name.strip()))[:MAX_STOCKS]


class InPlayJob:
    def __init__(self, repo: Any, provider_factory: Callable[[str], Any], names: Callable[[], List[str]], *,
                 leader: Optional[Callable[[], bool]] = None, pause: float = PAUSE):
        self.repo, self._factory, self._names = repo, provider_factory, names
        self._leader = leader or (lambda: True)
        self._pause = pause
        self._thread: Optional[threading.Thread] = None
        self._done: Optional[date] = None

    def tick(self, now: datetime) -> None:
        """Starts the day's scoring once, after ``RUN_AT`` in the session, on its own thread."""
        from .option_recorder import session_bounds
        local = now.astimezone(_NEW_YORK)
        if self._done == local.date() or not self._leader() or local.time() < RUN_AT:
            return
        opening, closing = session_bounds(local.date())
        if opening is None or now >= closing:
            return
        if (self.latest() or {}).get("day") == local.date().isoformat():
            self._done = local.date()  # made before a restart
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._done = local.date()
        self._thread = threading.Thread(target=self.run, args=(local.date(),), name="in-play", daemon=True)
        self._thread.start()

    def run(self, day: date) -> Dict[str, Any]:
        provider = self._factory("live")
        quiet = getattr(provider, "quiet_sdk_log", None)
        if callable(quiet):
            quiet()
        rows, failed = [], []
        start = (day - timedelta(days=HISTORY_DAYS)).isoformat()
        try:
            for name in self._names():
                try:
                    found = score(provider.history_bars(f"US.{name}", start, day.isoformat()), day)
                    if found is not None:
                        rows.append({"ticker": name, **found})
                except Exception as exc:  # a stock without history is left out
                    failed.append(name)
                    logger.info("In-play: %s skipped (%s)", name, type(exc).__name__)
                if self._pause:
                    time.sleep(self._pause)
        finally:
            provider.close()
        rows.sort(key=lambda row: -row["S"])
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        result = {"day": day.isoformat(), "made_at": datetime.now(_NEW_YORK).isoformat(), "top": TOP, "rows": rows,
                  "failed": failed, "verdict": register.get("in_play_range").verdict, "label": register.label("in_play_range")}
        self.repo.set_setting(SETTING, result)
        return result

    def latest(self) -> Optional[Dict[str, Any]]:
        return self.repo.setting(SETTING, None)

    def stop(self) -> None:
        pass  # the thread is a daemon and ends with the day's list
