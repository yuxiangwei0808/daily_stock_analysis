"""The 0DTE quote recorder (option_recorder.py): near-the-money quotes every 15 seconds, to gzip files."""
import csv
import gzip
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.services.trade_desk import option_recorder as rec

NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 9)


class FakeMoomoo:
    def __init__(self, spots, no_options=(), failing=()):
        self.spots, self.no_options, self.failing = dict(spots), set(no_options), set(failing)
        self.chain_calls, self.snapshots, self.closed = [], [], False

    def nearest_chain(self, code):
        self.chain_calls.append(code)
        if code in self.failing:
            raise RuntimeError("chain timeout")
        if code in self.no_options:
            return None, []
        chain = [{"code": f"{code}-{right[0].upper()}{strike:g}", "right": right, "strike": strike}
                 for strike in [self.spots[code] + k for k in range(-20, 21)] for right in ("call", "put")]
        return DAY, chain

    def snapshot_rows(self, codes):
        self.snapshots.append(list(codes))
        rows = []
        for code in codes:
            if code in self.spots:
                rows.append({"code": code, "last_price": np.float64(self.spots[code]), "bid_price": self.spots[code] - 0.01,
                             "ask_price": self.spots[code] + 0.01, "volume": 1000, "update_time": "2026-11-09 09:30:00"})
            else:
                rows.append({"code": code, "bid_price": np.float64(1.25), "ask_price": 1.3, "bid_vol": 10, "ask_vol": 12,
                             "last_price": 1.27, "volume": 50, "option_open_interest": 900,
                             "option_implied_volatility": 21.5, "option_delta": 0.5, "option_gamma": "N/A",
                             "option_theta": float("nan"), "option_vega": 0.01, "update_time": "2026-11-09 09:29:59"})
        return rows

    def close(self):
        self.closed = True

    def quiet_sdk_log(self):
        self.quiet = True


def _at(hour, minute, second=0):
    return datetime(2026, 11, 9, hour, minute, second, tzinfo=NY)


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    def bounds(day):
        if day.weekday() >= 5:
            return None, None
        return datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY), datetime(day.year, day.month, day.day, 16, tzinfo=NY)
    monkeypatch.setattr(rec, "session_bounds", bounds)


def _recorder(tmp_path, fake, names=("SPY", "QQQ"), leader=lambda: True):
    service = SimpleNamespace(provider_factory=lambda mode: fake)
    return rec.OptionRecorder(service, leader=leader, underlyings=lambda: list(names), root=tmp_path, pause=0)


def _read(path):
    with gzip.open(path, "rt", newline="") as handle:
        return list(csv.reader(handle))


def test_it_reads_chains_before_the_open_and_records_near_the_money_quotes_from_the_open(tmp_path):
    fake = FakeMoomoo({"US.SPY": 735.0, "US.QQQ": 640.0})
    recorder = _recorder(tmp_path, fake)
    recorder.tick(_at(9, 15))
    assert fake.chain_calls == [] and recorder._provider is None  # before PREPARE: no connection
    recorder.tick(_at(9, 22))
    assert fake.chain_calls == ["US.SPY", "US.QQQ"] and fake.snapshots == []  # chains only
    assert fake.quiet  # the SDK's debug log is off before any request
    recorder.tick(_at(9, 30))
    recorder.tick(_at(9, 30, 15))
    assert fake.chain_calls == ["US.SPY", "US.QQQ"]  # once a day
    rows = _read(tmp_path / "2026-11-09" / "SPY.csv.gz")
    assert rows[0] == list(rec.COLUMNS)
    body = rows[1:]
    assert len(body) == 2 * (1 + 2 * rec.STRIKES * 2)  # two snapshots: the stock and 24 strikes of calls and puts
    stock = body[0]
    assert stock[1:4] == ["US.SPY", "S", ""] and stock[rec.COLUMNS.index("last")] == "735.0"
    strikes = sorted({float(row[3]) for row in body if row[2] in ("C", "P")})
    assert len(strikes) == 2 * rec.STRIKES and min(strikes) >= 723 and max(strikes) <= 747
    option = next(row for row in body if row[2] == "C")
    cell = dict(zip(rec.COLUMNS, option))
    assert cell["expiry"] == "2026-11-09" and cell["bid"] == "1.25" and cell["iv"] == "21.5"
    assert cell["gamma"] == "" and cell["theta"] == ""  # N/A and NaN are empty, numpy floats plain
    assert float(cell["ts"]) == _at(9, 30).timestamp()
    assert recorder.health(_at(9, 30, 20))["state"] == "ok"
    assert "2 underlyings" in recorder.health(_at(9, 30, 20))["detail"]


def test_after_the_close_it_disconnects_and_a_restart_appends_to_the_days_file(tmp_path):
    fake = FakeMoomoo({"US.SPY": 735.0})
    recorder = _recorder(tmp_path, fake, names=("SPY",))
    recorder.tick(_at(15, 0))
    recorder.tick(_at(16, 14, 45))  # SPY options trade until 16:15
    recorder.tick(_at(16, 15))
    assert fake.closed and recorder._provider is None
    again = _recorder(tmp_path, FakeMoomoo({"US.SPY": 736.0}), names=("SPY",))  # a restart the same day
    again.tick(_at(15, 30))
    rows = _read(tmp_path / "2026-11-09" / "SPY.csv.gz")
    assert rows.count(list(rec.COLUMNS)) == 1 and len(rows) == 1 + 3 * (1 + 4 * rec.STRIKES)


def test_a_stock_without_options_is_asked_once_and_a_failed_chain_after_five_minutes(tmp_path):
    fake = FakeMoomoo({"US.SPY": 735.0, "US.ABC": 10.0, "US.XYZ": 50.0}, no_options={"US.ABC"}, failing={"US.XYZ"})
    recorder = _recorder(tmp_path, fake, names=("SPY", "ABC", "XYZ"))
    recorder.tick(_at(9, 31))
    recorder.tick(_at(9, 32))
    assert fake.chain_calls.count("US.ABC") == 1 and fake.chain_calls.count("US.XYZ") == 1
    health = recorder.health(_at(9, 32, 5))
    assert health["state"] == "warn" and "XYZ (RuntimeError)" in health["detail"]
    recorder.tick(_at(9, 36, 1))
    assert fake.chain_calls.count("US.XYZ") == 2 and fake.chain_calls.count("US.ABC") == 1
    assert (tmp_path / "2026-11-09" / "SPY.csv.gz").exists() and not (tmp_path / "2026-11-09" / "ABC.csv.gz").exists()


def test_only_the_lease_holder_records_and_status_says_when_it_stalls(tmp_path):
    fake = FakeMoomoo({"US.SPY": 735.0})
    follower = _recorder(tmp_path, fake, names=("SPY",), leader=lambda: False)
    follower.tick(_at(10, 0))
    assert fake.chain_calls == [] and not (tmp_path / "2026-11-09").exists()
    assert follower.health(_at(10, 0))["detail"] == "runs on the server holding the monitor lease"
    recorder = _recorder(tmp_path, fake, names=("SPY",))
    assert recorder.health(_at(9, 0))["state"] == "off"
    assert recorder.health(datetime(2026, 11, 7, 11, tzinfo=NY))["state"] == "off"  # a Saturday
    recorder.tick(_at(10, 0))
    assert recorder.health(_at(10, 2))["state"] == "error"  # nothing for two minutes in the session
    fresh = _recorder(tmp_path, FakeMoomoo({"US.SPY": 735.0}), names=("SPY",))
    assert fresh.health(_at(9, 30, 30)) == {"state": "warn", "detail": "no snapshot yet"}  # just opened


def test_default_underlyings_are_configured_then_held_options_then_the_journal(monkeypatch):
    monkeypatch.setenv("OPTION_RECORDER_TICKERS", "spy, QQQ ,SPY")
    holdings = SimpleNamespace(codes=lambda: ["US.NVDA", "US.TSLA261120C400000", "US.TSLA"],
                               journal=lambda: {"by_underlying": [{"ticker": "AMD"}, {"ticker": "TSLA"}]})
    recorder = rec.OptionRecorder(SimpleNamespace(holdings=holdings))
    assert recorder._default_underlyings() == ["SPY", "QQQ", "TSLA", "AMD"]
    monkeypatch.setenv("OPTION_RECORDER_TICKERS", ",".join(f"T{i}" for i in range(20)))
    assert len(recorder._default_underlyings()) == rec.MAX_UNDERLYINGS


def test_it_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("OPTION_RECORDER_ENABLED", raising=False)
    assert not rec.enabled()
    monkeypatch.setenv("OPTION_RECORDER_ENABLED", "true")
    assert rec.enabled()
    assert rec.near_money([{"code": "a", "right": "call", "strike": s} for s in (1, 2, 3, 4)], 2.4, strikes=1) == [
        {"code": "a", "right": "call", "strike": 2}, {"code": "a", "right": "call", "strike": 3}]
