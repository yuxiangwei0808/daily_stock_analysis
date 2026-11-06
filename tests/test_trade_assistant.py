"""The trading assistant's session engine (assistant.py): levels, contracts, positions and alerts."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.services.trade_desk import assistant as asst
from src.services.trade_desk import holdings, position_plans

NY = ZoneInfo("America/New_York")


def at(hour, minute, second=0):
    return datetime(2026, 11, 9, hour, minute, second, tzinfo=NY)


FLAG = {"setup": "2026-11-06T10:00:00-05:00", "kind": "fade", "box": "range", "direction": "short", "price": 741.0,
        "stop": 743.0, "target": 737.0, "time": None, "grade": {"label": "No edge shown in backtest (t -1.78 / -0.90 vs random timing)"}}


class FakeProvider:
    def __init__(self):
        self.snapshots = []

    def kline(self, code, subtype, count, push=False):
        bars = []
        for k in range(20):  # 09:31 .. 09:50 one-minute bars, and one from yesterday
            end = at(9, 31) + timedelta(minutes=k)
            bars.append({"time": end.isoformat(), "open": 735 + k * 0.1, "high": 735.5 + k * 0.1, "low": 734.5 + k * 0.1,
                         "close": 735.2 + k * 0.1, "volume": 1000, "partial": end > at(9, 50)})
        return [{**bars[0], "time": (at(15, 59) - timedelta(days=3)).isoformat()}] + bars

    def snapshot_rows(self, codes):
        self.snapshots.append(list(codes))
        out = []
        for code in codes:
            if code == "US.SPY":
                out.append({"code": code, "last_price": 736.0, "prev_close_price": 733.0, "pre_high_price": 738.03,
                            "pre_low_price": 732.43})
            else:
                strike = float(code.split("-")[-1][1:])
                out.append({"code": code, "bid_price": 1.0, "ask_price": 1.1, "option_implied_volatility": 20.0,
                            "option_delta": 0.5, "volume": 100, "option_open_interest": 500, "strike": strike})
        return out

    def nearest_chain(self, code, min_days=0):
        self.min_days = min_days
        return date(2026, 11, 9), [{"code": f"{code}-{r[0].upper()}{s}", "right": r, "strike": float(s)}
                                    for s in range(720, 751) for r in ("call", "put")]


class FakeStream:
    def __init__(self):
        self.closed, self.kept = False, True

    def touch(self, now=None):
        return self.kept

    def close(self):
        self.closed = True


class FakeCharts:
    def __init__(self):
        self.provider = FakeProvider()
        self.bars, self.quotes, self.streams, self.flags, self.loads = {}, {}, {}, [], 0
        self.seq = 0

    def stream(self, ticker, interval):
        self.streams[ticker] = FakeStream()
        return self.streams[ticker]

    def pushed_bar(self, code, kind):
        return self.bars.get((code, kind))

    def pushed_quote(self, code):
        return self.quotes.get(code)

    def push_quote(self, code, price, when):
        self.seq += 1
        self.quotes[code] = {"price": price, "at": when.isoformat(), "received": when, "seq": self.seq}

    def push_bar(self, code, kind, end, **values):
        self.seq += 1
        self.bars[(code, kind)] = {"time": end.isoformat(), "open": 736, "high": 737, "low": 735, "close": 736,
                                   "volume": 500, **values, "seq": self.seq}

    def chart(self, ticker, interval, now=None):
        self.loads += 1
        return {"levels": [{"kind": "prior_high", "price": 740.0, "label": "Prior day high (Nov 6)"},
                           {"kind": "prior_low", "price": 730.0, "label": "Prior day low (Nov 6)"},
                           {"kind": "stop", "price": 728.0, "position_key": "SPY", "label": "SPY shares stop"}],
                "flags": list(self.flags)}

    def _connection(self):
        return self.provider


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    def bounds(day):
        if day.weekday() >= 5:
            return None, None
        return datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY), datetime(day.year, day.month, day.day, 16, tzinfo=NY)
    monkeypatch.setattr(asst, "_session_bounds", bounds)
    monkeypatch.setattr(holdings, "enabled", lambda: False)


def _assistant(service=None):
    clock = {"mono": 0.0}
    charts = FakeCharts()
    a = asst.Assistant(service or SimpleNamespace(), charts, clock=lambda: at(9, 50), monotonic=lambda: clock["mono"])
    return a, charts, clock


def test_start_checks_the_request_and_nothing_runs_before_it():
    a, charts, _ = _assistant()
    assert a.state()["running"] is False and charts.streams == {}
    with pytest.raises(ValueError):
        a.start([], threads=False)
    with pytest.raises(ValueError):
        a.start(["SPY", "QQQ", "NVDA", "TSLA"], threads=False)
    with pytest.raises(ValueError):
        a.start(["SPY"], "weekly", threads=False)
    state = a.start(["spy", "SPY"], "0dte", at(9, 50), threads=False)
    assert state["running"] and [t["ticker"] for t in state["tickers"]] == ["SPY"] and "SPY" in charts.streams
    assert state["events"][0]["title"] == "Watching SPY"
    a.stop("done", at(9, 51))
    assert charts.streams["SPY"].closed and a.state()["stopped"]["reason"] == "done"


def test_levels_contracts_and_crossings():
    a, charts, clock = _assistant()
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50, 1))
    state = a.state(at(9, 50, 1))
    spy = state["tickers"][0]
    kinds = {level["kind"] for level in spy["levels"]}
    assert {"prior_high", "prior_low", "stop", "pm_high", "pm_low", "or5_high", "or15_low", "vwap"} <= kinds
    pm = next(level for level in spy["levels"] if level["kind"] == "pm_high")
    assert pm["price"] == 738.03 and pm["verdict"] == "not tested" and pm["verdict_label"].startswith("Pre-market high and low")
    assert next(level for level in spy["levels"] if level["kind"] == "stop")["verdict_label"] == "Your level"
    assert spy["change_pct"] == pytest.approx(0.41, abs=0.01)
    rows = spy["contracts"]["rows"]
    assert len(rows) == 2 * 2 * asst.PANEL_STRIKES and {r["strike"] for r in rows} == {731.0 + k for k in range(10)}
    call = next(r for r in rows if r["right"] == "call" and r["strike"] == 736.0)
    assert call["mid"] == 1.05 and call["spread_pct"] == pytest.approx(9.5, abs=0.1)
    assert call["breakeven"] == pytest.approx(737.1) and call["breakeven_move_pct"] == pytest.approx(0.15, abs=0.01)
    assert call["theta_per_min"] < 0 and call["minutes_left"] == pytest.approx(370 - 1 / 60, abs=0.1)
    targets = {level["id"]: level for level in call["at_levels"]}
    assert "stop:SPY" in targets and targets["stop:SPY"]["now"] < call["mid"]  # the call is worth less at your stop
    assert len(charts.provider.snapshots[-1]) == 2 * 2 * asst.PANEL_STRIKES  # one snapshot for the panel
    charts.push_quote("US.SPY", 739.0, at(9, 50, 2))
    a.check(at(9, 50, 2))  # the first trade only sets the sides
    assert [e["kind"] for e in a.events_after(0)] == ["session"]
    charts.push_quote("US.SPY", 740.5, at(9, 50, 3))
    a.check(at(9, 50, 3))
    alert = a.events_after(1)[-1]
    assert alert["kind"] == "level" and alert["title"] == "SPY above Prior day high (Nov 6) 740"
    assert "not tested" in alert["detail"]
    charts.push_quote("US.SPY", 739.5, at(9, 51))
    a.check(at(9, 51))
    charts.push_quote("US.SPY", 740.6, at(9, 52))
    a.check(at(9, 52))
    assert [e["title"] for e in a.events_after(alert["seq"])] == ["SPY below Prior day high (Nov 6) 740"]  # once per direction in 15 min


def test_a_15_minute_close_reloads_the_chart_and_a_new_flag_alerts():
    a, charts, _ = _assistant()
    a.start(["SPY"], "intraday", at(9, 50), threads=False)
    a.io(at(9, 50, 1))
    assert charts.loads == 1
    charts.push_bar("US.SPY", "K_15M", at(10, 0))
    a.check(at(9, 59))
    charts.push_bar("US.SPY", "K_15M", at(10, 15))  # the next bar's first trade, inside the wait
    a.check(at(10, 0, 1))
    a.io(at(10, 0, 2))
    assert charts.loads == 1  # not yet: the bar settles for three seconds
    charts.flags = [{**FLAG, "state": "armed"}]
    a.check(at(10, 0, 3))
    a.io(at(10, 0, 3))
    assert charts.loads == 2
    titles = [e["title"] for e in a.events_after(0)]
    assert "SPY fade short armed: a limit the next 15-minute bar fills" in titles
    assert a.state()["tickers"][0]["flags"][0]["state"] == "armed"
    a.io(at(10, 0, 5))
    assert charts.loads == 2  # one reload per close


def test_positions_alert_when_they_appear_close_or_step_and_before_a_0dte_close(monkeypatch):
    monkeypatch.setattr(holdings, "enabled", lambda: True)
    rows = []
    monkeypatch.setattr(position_plans, "panel_rows", lambda view, plans, rules: list(rows))
    live = {"view": None}
    store = SimpleNamespace(view=lambda live=False: {"positions": []}, plans=lambda: {"plans": {}}, rules=lambda: [])
    shadow = SimpleNamespace(live_view=lambda now: live["view"])
    a, charts, clock = _assistant(SimpleNamespace(holdings=store, worker=SimpleNamespace(_shadow_watch=shadow)))
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50))
    held = {"key": "SPY|C735", "ticker": "SPY", "type": "option", "label": "SPY 11/09 735C", "pnl_pct": 4.0,
            "weight_pct": 1.2, "days_left": 0}
    rows.append(held)
    clock["mono"] += asst.POSITION_SECONDS
    live["view"] = {"positions": ["x"]}
    a.io(at(9, 51))
    assert [e["title"] for e in a.events_after(1)] == ["New position: SPY 11/09 735C"]
    assert a.state()["tickers"][0]["positions"][0]["source"] == "live feed"
    held["pnl_pct"] = 12.5
    clock["mono"] += asst.POSITION_SECONDS
    a.io(at(9, 52))
    assert a.events_after(0)[-1]["title"] == "SPY 11/09 735C +12.5% on cost"
    assert "1.2% of the account" in a.events_after(0)[-1]["detail"]
    a.check(at(15, 31))
    a.check(at(15, 44))
    a.check(at(15, 46))
    assert [e["title"] for e in a.events_after(0) if e["kind"] == "close_soon"] == ["30 minutes to the close", "15 minutes to the close"]
    rows.clear()
    live["view"] = None
    clock["mono"] += asst.POSITION_SECONDS
    a.io(at(15, 50))
    assert a.events_after(0)[-1]["title"] == "Position closed: SPY 11/09 735C"
    assert a.state()["tickers"][0]["positions"] == []


def test_the_session_ends_after_the_close_unless_started_after_it_and_streams_carry_alerts():
    a, charts, _ = _assistant()
    a.start(["SPY"], "swing", at(15, 0), threads=False)
    a.io(at(15, 0))
    assert charts.provider.min_days == asst.SWING_DAYS
    stream = a.stream()
    charts.push_quote("US.SPY", 735.0, at(15, 1))
    a.check(at(15, 1))
    out = stream.poll(at(15, 1))
    assert [name for name, _ in out] == ["state"] and out[0][1]["running"]
    a.check(at(16, 15))
    assert not a.running and a.stopped["reason"] == "the session closed"
    names = [name for name, _ in stream.poll(at(16, 15))]
    assert names == ["alert", "state"]
    evening, _, _ = _assistant()
    evening.start(["SPY"], "0dte", at(19, 0), threads=False)
    evening.check(at(19, 5))
    assert evening.running  # started after the close: runs until you stop it
    assert evening.health(at(19, 5))["state"] == "ok"


def test_health_warns_when_trades_stop_in_the_session():
    a, charts, _ = _assistant()
    assert a.health(at(10, 0))["state"] == "off"
    a.start(["SPY"], "0dte", at(10, 0), threads=False)
    assert a.health(at(10, 2))["state"] == "warn"
    charts.push_quote("US.SPY", 736.0, at(10, 2))
    a.check(at(10, 2))
    assert a.health(at(10, 2, 30))["state"] == "ok"


def test_the_api_starts_shows_and_stops_a_session():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints.trade_desk import router
    a, charts, _ = _assistant()
    a.start = lambda tickers, horizon: asst.Assistant.start(a, tickers, horizon, at(9, 50), threads=False)
    app = FastAPI()
    app.state.trade_desk_service = SimpleNamespace(assistant=a)
    app.include_router(router, prefix="/api/v1/trade-desk")
    with TestClient(app) as client:
        assert client.get("/api/v1/trade-desk/assistant").json()["running"] is False
        assert client.post("/api/v1/trade-desk/assistant/start", json={"tickers": ["SPY", "QQQ", "NVDA", "TSLA"]}).status_code == 422
        assert client.post("/api/v1/trade-desk/assistant/start", json={"tickers": ["SPY"], "horizon": "weekly"}).status_code == 422
        assert client.post("/api/v1/trade-desk/assistant/start", json={"tickers": ["S PY"]}).status_code == 422
        started = client.post("/api/v1/trade-desk/assistant/start", json={"tickers": ["SPY"], "horizon": "0dte"})
        assert started.status_code == 200 and started.json()["running"] and started.json()["horizon"] == "0dte"
        stopped = client.post("/api/v1/trade-desk/assistant/stop").json()
        assert stopped["running"] is False and stopped["stopped"]["reason"] == "stopped from the page"
