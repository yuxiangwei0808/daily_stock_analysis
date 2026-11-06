"""The Trade Desk chart (GET /chart/{ticker}): boxes, flags and levels from the tested rules, on a fake moomoo."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.services.trade_desk import boxes as bx
from src.services.trade_desk import chart as ch
from src.services.trade_desk.backtest import boxes as bb
from src.services.trade_desk.providers import ProviderError
from tests.test_boxes import _break_and_retest
from tests.test_broker_holdings import repo, store  # noqa: F401

NY = ZoneInfo("America/New_York")


def _rows(bars, partial_after=None):
    """moomoo-style rows: ``time`` is the bar's end; bars from ``partial_after`` on are still forming."""
    out = []
    for k in range(len(bars)):
        end = (bars.start[k] + np.timedelta64(15, "m")).astype("datetime64[s]").item().replace(tzinfo=NY)
        out.append({"time": end.isoformat(), "open": float(bars.o[k]), "high": float(bars.h[k]),
                    "low": float(bars.l[k]), "close": float(bars.c[k]), "volume": 1000,
                    "partial": partial_after is not None and k >= partial_after})
    return out


class FakeMoomoo:
    def __init__(self, rows):
        self.rows, self.calls, self.kept, self.closed = rows, [], [], False

    def kline(self, code, subtype, count):
        self.calls.append((code, subtype, count))
        if isinstance(self.rows, Exception):
            raise self.rows
        return [dict(row) for row in self.rows.get(subtype, [])][-count:]

    def keep_subscriptions(self, wanted):
        self.kept.append(set(wanted))

    def close(self):
        self.closed = True


def _charts(rows, holdings=None):
    fake = FakeMoomoo(rows)
    service = SimpleNamespace(holdings=holdings, provider_factory=lambda mode: fake)
    return ch.Charts(service), fake


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    monkeypatch.delenv("TRADE_DESK_BROKER_ACCOUNT", raising=False)


def test_a_break_shows_an_armed_retest_with_its_grade_and_the_forming_bar_is_ignored(monkeypatch):
    bars = _break_and_retest()
    s = next(bx.setups(bars))
    cut = s.i + 10  # the break closed; the retest bar is forming
    rows = _rows(bars, partial_after=cut)[:cut + 1]
    monkeypatch.setattr(ch, "_session_open", lambda now: True)
    charts, fake = _charts({"K_15M": rows})
    now = datetime.fromisoformat(rows[cut - 1]["time"]) + timedelta(minutes=1)
    body = charts.chart("spy", now=now)
    assert fake.calls == [("US.SPY", "K_15M", ch.MAX_BARS)] and fake.kept == [{("US.SPY", "K_15M")}]
    assert len(body["bars"]) == cut + 1 and body["bars"][-1]["partial"]
    assert body["bars"][0]["start"] == "2026-01-05T09:30:00-05:00"
    flag = body["flags"][0]
    assert (flag["state"], flag["kind"], flag["direction"]) == ("armed", "break_retest", "short")
    assert (flag["price"], flag["stop"], flag["target"], flag["reward_risk"]) == (102.9, 103.4, 100.0, 5.8)
    assert flag["grade"]["verdict"] == "rejected" and flag["grade"]["t"] == [-2.24, -2.90]
    box = next(b for b in body["boxes"] if b["setup"] == rows[s.i]["time"] and b["box"] == "range")
    assert (box["bottom"], box["top"], box["wick_top"], box["alive"]) == (102.9, 103.1, 103.2, True)
    assert box["from"] == ch._start(rows[s.i + 1], "15m") and box["to"] == rows[s.i + 8]["time"]
    levels = {level["kind"]: level["price"] for level in body["levels"]}
    assert (levels["prior_high"], levels["prior_low"], levels["prior_close"]) == (100.5, 99.5, 100.0)
    assert {"val", "vah"} <= set(levels) and body["typical_range"]["T"] == 1.0
    assert body["nx"]["A"][249] is None and body["nx"]["A"][250] is not None and not body["warnings"]


def test_after_the_session_the_trade_shows_closed_at_its_target_with_the_backtest_r(monkeypatch):
    bars = _break_and_retest()
    rows = _rows(bars)
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, _ = _charts({"K_15M": rows})
    body = charts.chart("SPY", now=datetime.fromisoformat(rows[-1]["time"]) + timedelta(hours=2))
    flag = next(f for f in body["flags"] if f["kind"] == "break_retest" and f["state"] != "watching")
    s = next(bx.setups(bars))
    entry = bx.break_retest(bars, s)
    expected = bb.simulate(bars, entry.bar, entry.price, entry.stop, entry.target, -1,
                           bars.session_end(entry.bar, 5))
    assert flag["state"] == "closed" and flag["result"]["how"] == "target"
    assert flag["result"]["R"] == round(expected["R"], 3) and flag["time"] == rows[entry.bar]["time"]
    assert not [f for f in body["flags"] if f["state"] == "armed" and f["setup"] == rows[s.i]["time"]]


def test_other_intervals_show_their_own_bars_with_the_15_minute_boxes(monkeypatch):
    bars = _break_and_retest()
    rows = _rows(bars)
    early = {**rows[0], "time": "2026-01-05T09:00:00-05:00"}  # pre-market: outside the regular session
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, fake = _charts({"K_15M": rows, "K_5M": [early, *rows[:3]]})
    body = charts.chart("SPY", "5m", now=datetime.fromisoformat(rows[-1]["time"]) + timedelta(hours=2))
    assert [c[1] for c in fake.calls] == ["K_15M", "K_5M"]
    assert fake.kept == [{("US.SPY", "K_15M"), ("US.SPY", "K_5M")}]
    assert [b["time"] for b in body["bars"]] == [r["time"] for r in rows[:3]]
    assert body["bars"][0]["start"] == "2026-01-05T09:40:00-05:00" and body["boxes"]
    assert body["warnings"] == ["nx_needs_250_bars"] and body["nx"]["A"] == [None] * 3


def test_the_chart_keeps_the_newest_five_tickers_and_releases_idle_ones(monkeypatch):
    rows = _rows(_break_and_retest())
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, fake = _charts({"K_15M": rows})
    start = datetime(2026, 1, 23, 18, tzinfo=NY)
    for k, ticker in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]):
        charts.chart(ticker, now=start + timedelta(seconds=k))
    assert {code for code, _ in fake.kept[-1]} == {"US.BBB", "US.CCC", "US.DDD", "US.EEE", "US.FFF"}
    charts.chart("GGG", now=start + ch.IDLE + timedelta(minutes=1))
    assert fake.kept[-1] == {("US.GGG", "K_15M")}
    charts.close()
    assert fake.closed


def test_your_stops_targets_and_price_alerts_are_lines_on_the_chart(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TRADE_DESK_BROKER_ACCOUNT", "1234")
    key = "USO 2026-10-16"
    store.mutate_plans(lambda stored: {**stored, "plans": {key: {
        "key": key, "ticker": "USO", "type": "option", "exposure": "long", "stop": 140.0, "target": 165.0,
        "stop_source": "review", "target_source": "review"}}})
    store.add_rule({"position_key": key, "kind": "price_below", "value": 145})  # becomes the plan's stop
    store.add_rule({"ticker": "USO", "kind": "price_above", "value": 158, "note": "take a look"})
    store.add_rule({"ticker": "NVDA", "kind": "price_above", "value": 250})
    store.add_rule({"position_key": key, "kind": "pnl_below", "value": -30})
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, _ = _charts({"K_15M": _rows(_break_and_retest())}, holdings=store)
    body = charts.chart("USO", now=datetime(2026, 1, 23, 18, tzinfo=NY))
    mine = [(m["kind"], m["price"], m["label"]) for m in body["levels"] if m["kind"] in ("stop", "target", "alert")]
    assert mine == [("stop", 145, "USO 10/16 160/170C spread stop (your alert)"),
                    ("target", 165.0, "USO 10/16 160/170C spread target (daily review)"),
                    ("alert", 158, "Your alert: price at or above 158 (take a look)")]


def test_the_route_checks_its_inputs_and_reports_moomoo_errors(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints.trade_desk import router
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, fake = _charts({"K_15M": _rows(_break_and_retest())})
    app = FastAPI()
    app.state.trade_desk_service = SimpleNamespace(charts=charts)
    app.include_router(router, prefix="/api/v1/trade-desk")
    with TestClient(app) as client:
        ok = client.get("/api/v1/trade-desk/chart/SPY", params={"impulse": 0.6, "tidy": True})
        assert ok.status_code == 200 and ok.json()["params"] == {"impulse_t": 0.6, "tidy": True}
        assert all(f["grade"]["verdict"] == ch.GRADES[(f["kind"], 0.6, True)][0] for f in ok.json()["flags"])
        assert client.get("/api/v1/trade-desk/chart/SPY", params={"interval": "2h"}).status_code == 422
        assert client.get("/api/v1/trade-desk/chart/SPY", params={"impulse": 0.8}).status_code == 422
        assert client.get("/api/v1/trade-desk/chart/S%20PY").status_code == 422
        fake.rows = ProviderError("subscription_quota", "OpenD subscription quota has 0 slots")
        failed = client.get("/api/v1/trade-desk/chart/SPY")
        assert failed.status_code == 503 and failed.json()["detail"]["code"] == "subscription_quota"
