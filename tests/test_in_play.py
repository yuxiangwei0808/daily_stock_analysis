"""Today's in-play list (in_play.py): the backtested score on moomoo history, once a trading day."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.services.trade_desk import in_play, option_recorder

NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 9)


def _history(gap, first_range, sessions=15):
    """15-minute rows: ``sessions`` quiet days (range 2) before DAY, then DAY's first bar."""
    rows, day, made, price = [], DAY - timedelta(days=sessions * 2), 0, 100.0
    while made < sessions:
        if day.weekday() < 5 and day < DAY:
            for k in range(26):
                end = datetime(day.year, day.month, day.day, 9, 45) + timedelta(minutes=15 * k)
                rows.append({"time_key": end.strftime("%Y-%m-%d %H:%M:%S"), "open": price, "high": price + 1, "low": price - 1,
                             "close": price, "volume": 1000})
            made += 1
        day += timedelta(days=1)
    o = price + gap
    rows.append({"time_key": "2026-11-09 09:45:00", "open": o, "high": o + first_range / 2, "low": o - first_range / 2,
                 "close": o, "volume": 5000})
    return rows


def test_the_score_is_the_backtests():
    found = in_play.score(_history(gap=-3.0, first_range=2.0), DAY)
    assert found["T"] == 2.0 and found["S"] == pytest.approx((3 + 2) / 2) and found["gap_pct"] == -3.0
    assert in_play.score(_history(gap=0, first_range=1, sessions=5), DAY) is None  # no T yet
    assert in_play.score(_history(gap=0, first_range=1)[:-1], DAY) is None  # no first bar today


class FakeRepo:
    def __init__(self):
        self.settings = {}

    def setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def test_the_job_ranks_once_a_trading_day_after_0946(monkeypatch):
    monkeypatch.setattr(option_recorder, "session_bounds", lambda day: (
        (datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY), datetime(day.year, day.month, day.day, 16, tzinfo=NY))
        if day.weekday() < 5 else (None, None)))
    histories = {"US.AAA": _history(0.5, 1.0), "US.BBB": _history(4.0, 3.0), "US.CCC": _history(-1.0, 1.0)}
    provider = SimpleNamespace(history_bars=lambda code, start, end: histories.get(code) or (_ for _ in ()).throw(RuntimeError("x")),
                               close=lambda: None, quiet_sdk_log=lambda: None)
    repo = FakeRepo()
    job = in_play.InPlayJob(repo, lambda mode: provider, lambda: ["AAA", "BBB", "CCC", "NONE"], pause=0)
    started = []
    monkeypatch.setattr(job, "run", lambda day: started.append(day))
    job.tick(datetime(2026, 11, 9, 9, 40, tzinfo=NY))
    job.tick(datetime(2026, 11, 7, 10, 0, tzinfo=NY))  # a Saturday
    assert started == []
    job.tick(datetime(2026, 11, 9, 9, 46, tzinfo=NY))
    job._thread.join(timeout=5)
    job.tick(datetime(2026, 11, 9, 10, 0, tzinfo=NY))
    assert started == [DAY]  # once a day
    result = in_play.InPlayJob.run(job, DAY)
    assert [row["ticker"] for row in result["rows"]] == ["BBB", "CCC", "AAA"] and result["rows"][0]["rank"] == 1
    assert result["failed"] == ["NONE"] and result["verdict"] == "pass" and "not a trade rule" in result["label"]
    assert repo.settings["in_play"]["day"] == "2026-11-09"
    again = in_play.InPlayJob(repo, lambda mode: provider, lambda: [], pause=0)
    again.tick(datetime(2026, 11, 9, 11, 0, tzinfo=NY))
    assert again._thread is None  # already made today, before a restart


def test_the_universe_is_the_tests_names_then_yours():
    names = in_play.universe(["spy", "PLTR", "AAPL"], ["TSLA", "SOFI"])
    assert names[:4] == ["SPY", "QQQ", "IWM", "DIA"] and names.index("PLTR") > names.index("KO") and "SOFI" in names
    assert len(names) == len(set(names))
    assert len(in_play.universe([f"T{i}" for i in range(100)], [])) == in_play.MAX_STOCKS
