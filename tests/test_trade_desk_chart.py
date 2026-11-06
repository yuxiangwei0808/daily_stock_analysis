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
        self.pushed, self.watched, self.listeners = set(), [], None

    def kline(self, code, subtype, count, push=False):
        self.calls.append((code, subtype, count))
        if push:
            self.pushed.add((code, subtype))
        if isinstance(self.rows, Exception):
            raise self.rows
        return [dict(row) for row in self.rows.get(subtype, [])][-count:]

    def listen_bars(self, on_bar, on_quote):
        self.listeners = (on_bar, on_quote)

    def watch_pushes(self, code, subtypes):
        self.watched.append((code, list(subtypes)))

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
    assert fake.pushed == {("US.SPY", "K_15M")} and fake.listeners  # pushes go to the chart's handler from the start
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


def _clock(monkeypatch, start):
    now = [start]
    monkeypatch.setattr(ch, "utcnow", lambda: now[0])
    return now


def test_pushes_reach_the_stream_and_a_15_minute_close_asks_the_page_to_reload(monkeypatch):
    now = _clock(monkeypatch, datetime(2026, 11, 6, 10, 2, tzinfo=NY))
    monkeypatch.setattr(ch, "_session_open", lambda when: True)
    charts, fake = _charts({})
    on_bar, on_quote = None, None
    on_bar0 = {"code": "US.SPY", "k_type": "K_15M", "time_key": "2026-11-06 10:00:00", "open": 1, "high": 2, "low": 0.5,
               "close": 1.5, "volume": 7}
    stream = charts.stream("spy", "5m")
    on_bar, on_quote = fake.listeners
    assert fake.watched == [("US.SPY", ["QUOTE", "K_15M", "K_5M"])]
    on_bar(on_bar0)  # a 15-minute bar that ended before the page connected is already in its snapshot
    assert stream.poll() == []
    on_bar({**on_bar0, "k_type": "K_5M", "time_key": "2026-11-06 09:05:00"})  # outside the regular session
    on_bar({**on_bar0, "k_type": "K_15M", "time_key": "2026-11-06 10:15:00"})
    assert stream.poll() == []
    on_bar({**on_bar0, "k_type": "K_5M", "time_key": "2026-11-06 10:05:00", "close": 1.25, "volume": 9.0})
    on_quote({"code": "US.SPY", "last_price": 1.25, "data_date": "2026-11-06", "data_time": "10:01:59"})
    on_quote({"code": "US.SPY", "last_price": 1.3, "data_date": "2026-11-06", "data_time": "10:02:00"})
    bar = {"time": "2026-11-06T10:05:00-05:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.25, "volume": 9,
           "start": "2026-11-06T10:00:00-05:00", "partial": True}
    assert stream.poll() == [("bar", {"interval": "5m", "bar": bar}),
                             ("quote", {"price": 1.3, "at": "2026-11-06T10:02:00-05:00"})]
    assert stream.poll() == [] and list(charts.lags) == [0.0]  # the first quote is the last known state, not a delay
    health = charts.health()
    assert health["state"] == "ok" and "1 chart open, 4 bar and 2 quote pushes" in health["detail"]
    now[0] = datetime(2026, 11, 6, 10, 15, 2, tzinfo=NY)
    assert stream.poll() == []  # a moment for the bar to settle in OpenD
    now[0] += timedelta(seconds=2)
    assert stream.poll() == [("closed", {"bar_end": "2026-11-06T10:15:00-05:00"})] and stream.poll() == []
    assert charts.health()["state"] == "warn"  # no push for 13 minutes while the session is open
    stream.close()
    stream.close()
    assert charts.health()["state"] == "off" and charts._streams == {}


def test_a_close_is_signalled_although_the_next_bar_is_pushed_within_the_grace(monkeypatch):
    # In market hours (2026-11-06) OpenD pushed the next 15-minute bar's first trade about 0.4 s after each boundary.
    now = _clock(monkeypatch, datetime(2026, 11, 6, 10, 14, 50, tzinfo=NY))
    monkeypatch.setattr(ch, "_session_open", lambda when: True)
    charts, fake = _charts({})
    stream = charts.stream("spy", "15m")
    on_bar, _ = fake.listeners
    row = {"code": "US.SPY", "k_type": "K_15M", "time_key": "2026-11-06 10:15:00", "open": 1, "high": 2, "low": 0.5,
           "close": 1.5, "volume": 7}
    on_bar(row)
    assert [name for name, _ in stream.poll()] == ["bar"]
    now[0] = datetime(2026, 11, 6, 10, 15, 0, 400000, tzinfo=NY)
    on_bar({**row, "time_key": "2026-11-06 10:30:00", "volume": 1})
    assert [name for name, _ in stream.poll()] == ["bar"]
    now[0] += timedelta(seconds=3)
    assert stream.poll() == [("closed", {"bar_end": "2026-11-06T10:15:00-05:00"})]
    now[0] = datetime(2026, 11, 6, 10, 30, 3, tzinfo=NY)  # no trade since; the forming bar's close is still signalled
    assert stream.poll() == [("closed", {"bar_end": "2026-11-06T10:30:00-05:00"})] and stream.poll() == []


def test_a_sixth_chart_ends_the_oldest_stream_and_a_closed_page_is_released_after_ten_minutes(monkeypatch):
    start = datetime(2026, 11, 6, 11, tzinfo=NY)
    now = _clock(monkeypatch, start)
    monkeypatch.setattr(ch, "_session_open", lambda when: True)
    charts, fake = _charts({})
    streams = {}
    for k, ticker in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]):
        now[0] = start + timedelta(seconds=k)
        streams[ticker] = charts.stream(ticker, "15m")
    now[0] += ch.TOUCH
    assert streams["AAA"].due(now[0]) and not streams["AAA"].touch(now[0])  # the page shows it was closed for room
    assert streams["FFF"].touch(now[0])
    streams["BBB"].close()
    for later in (timedelta(0), timedelta(minutes=5), ch.IDLE - timedelta(seconds=10)):  # open pages keep theirs
        for ticker in ("CCC", "DDD", "EEE", "FFF"):
            assert streams[ticker].touch(now[0] + later)
    charts._upkeep([], now[0] + ch.IDLE - timedelta(seconds=5), connect=False)
    assert ("US.BBB", "QUOTE") in fake.kept[-1]  # kept a while in case the page comes back
    for ticker in ("CCC", "DDD", "EEE", "FFF"):
        assert streams[ticker].touch(now[0] + ch.IDLE)
    charts._upkeep([], now[0] + ch.IDLE + timedelta(seconds=5), connect=False)
    assert {code for code, _ in fake.kept[-1]} == {"US.CCC", "US.DDD", "US.EEE", "US.FFF"}
    charts.close()
    charts._upkeep([], now[0], connect=False)  # the sweep after closing opens no new connection
    assert fake.closed


def test_a_push_handler_error_shows_on_status():
    charts, fake = _charts({})
    charts._connection()
    on_bar, _ = fake.listeners
    on_bar({"code": "US.SPY", "k_type": "K_1M", "time_key": "bad", "open": 1, "high": 1, "low": 1, "close": 1})
    assert charts.health()["state"] == "off"  # an unreadable time is skipped, not an error
    on_bar(None)
    assert charts.health() == {"state": "error", "detail": "push handler failed: AttributeError"}
    charts.close()


def test_bar_and_quote_pushes_reach_the_chart_as_plain_rows():
    from tests.test_shadow_exits import _Context, _sdk
    from src.services.trade_desk.providers import MoomooProvider
    import logging
    rows = [{"code": "US.SPY", "k_type": "K_1M"}]
    sdk = _sdk(lambda content: (0, rows))

    class KlineBase:
        def parse_rsp_pb(self, content):
            return 0, rows

        def on_recv_rsp(self, content):
            raise AssertionError("the SDK builds a pandas table for every bar push")
    sdk.CurKlineHandlerBase = KlineBase
    ctx = _Context()
    provider = MoomooProvider(sdk=sdk, context=ctx)
    bars, quotes = [], []
    provider.listen_bars(bars.append, quotes.append)
    assert sdk.logger.file_level == logging.INFO
    bar_handler, quote_handler = ctx.handlers
    assert bar_handler.on_recv_rsp("bar") == (0, rows) and bars == rows
    assert quote_handler.on_recv_rsp("quote") == (0, rows) and quotes == rows
    provider.watch_pushes("US.SPY", ["QUOTE", "K_1M"])
    assert [(codes, kwargs["subscribe_push"]) for codes, _, kwargs in ctx.subscribe_calls] == [(["US.SPY"], True)] * 2


def test_the_stream_route_sends_events_and_ends_when_the_chart_is_released(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints import trade_desk as endpoints

    class Stream:
        def __init__(self):
            self.closed, self.polls = False, 0

        def due(self, now):
            return self.polls >= 2

        def touch(self, now):
            return False

        def poll(self, now):
            self.polls += 1
            return [("quote", {"price": 1.5, "at": None})] if self.polls == 1 else []

        def close(self):
            self.closed = True
    stream = Stream()
    opened = []

    def open_stream(ticker, interval):
        opened.append((ticker, interval))
        if interval == "2m":
            raise ValueError("bad")
        return stream
    monkeypatch.setattr(endpoints, "STREAM_POLL_SECONDS", 0.01)
    app = FastAPI()
    app.state.trade_desk_service = SimpleNamespace(charts=SimpleNamespace(stream=open_stream))
    app.include_router(endpoints.router, prefix="/api/v1/trade-desk")
    with TestClient(app) as client:
        assert client.get("/api/v1/trade-desk/chart/SPY/stream", params={"interval": "2h"}).status_code == 422
        body = client.get("/api/v1/trade-desk/chart/SPY/stream", params={"interval": "5m"}).text
    assert opened == [("SPY", "5m")]
    assert body.startswith("event: ready") and 'event: quote\ndata: {"price": 1.5, "at": null}' in body
    assert body.rstrip().endswith('event: evicted\ndata: {"reason": "more than five charts are open"}') and stream.closed
