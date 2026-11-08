"""The trading assistant's session engine (assistant.py): levels, contracts, positions and alerts."""
import base64
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
        for k in range(getattr(self, "n_bars", 20)):  # 09:31 .. 09:50 one-minute bars, and one from yesterday
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
                            "pre_low_price": 732.43, **getattr(self, "extra", {})})
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
    seen = a.seq
    for minute, price in ((10, 739.8), (11, 740.2), (12, 739.75), (13, 740.3)):  # within 0.05% of the level: no crossing
        charts.push_quote("US.SPY", price, at(10, minute))
        a.check(at(10, minute))
    assert not [e for e in a.events_after(seen) if str(e.get("level")).startswith("prior_high")]
    charts.push_quote("US.SPY", 739.6, at(10, 14))
    a.check(at(10, 14))
    assert [e["title"] for e in a.events_after(seen) if str(e.get("level")).startswith("prior_high")] == ["SPY below Prior day high (Nov 6) 740"]


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
    held["pnl_pct"] = 15.0
    clock["mono"] += asst.POSITION_SECONDS
    a.io(at(9, 52))
    assert a.events_after(0)[-1]["title"] == "SPY 11/09 735C +15.0% on cost"
    assert "1.2% of the account; its best this session" in a.events_after(0)[-1]["detail"]
    seen = a.seq
    # a 0DTE option swings: a new high (by 10 points, or a quarter of the last), a new low below cost, half its gain given back
    for minute, pnl in ((53, 4.0), (54, 24.0), (55, 26.0), (56, 12.0), (57, 10.0), (58, -11.0), (59, -3.0)):
        held["pnl_pct"] = pnl
        clock["mono"] += asst.POSITION_SECONDS
        a.io(at(9, minute))
    assert [(e["title"], e["detail"].split("; ")[1]) for e in a.events_after(seen)] == [
        ("SPY 11/09 735C +26.0% on cost", "its best this session"),
        ("SPY 11/09 735C +12.0% on cost", "it gave back half of its best this session (+26%)"),
        ("SPY 11/09 735C -11.0% on cost", "its worst this session")]
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


class FakeRepo:
    def __init__(self):
        self.rows = {}

    def chart_trades(self, ticker=None, statuses=None, limit=5000):
        rows = [dict(r) for r in self.rows.values() if (ticker is None or r["ticker"] == ticker)
                and (statuses is None or r["status"] in statuses)]
        return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)[:limit]

    def save_chart_trades(self, records):
        for rec in records:
            self.rows[rec["id"]] = dict(rec)

    def setting(self, key, default=None):
        return getattr(self, "settings", {}).get(key, default)

    def set_setting(self, key, value):
        self.settings = {**getattr(self, "settings", {}), key: value}

    def delete_chart_trade(self, record_id, statuses):
        if record_id in self.rows and self.rows[record_id]["status"] in statuses:
            del self.rows[record_id]
            return True
        return False


def _day_bars(path):
    return [{"time": (at(9, 31) + timedelta(minutes=k)).isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 100,
             "partial": False} for k, (o, h, l, c) in enumerate(path)]


def test_a_confirmed_plan_is_watched_alerted_and_graded_at_the_close():
    repo = FakeRepo()
    a, charts, _ = _assistant(SimpleNamespace(repo=repo))
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50))
    with pytest.raises(ValueError, match="Start the assistant on QQQ"):
        a.add_plan("QQQ", {"direction": "long", "stop": 1, "target": 2}, now=at(9, 50))
    charts.push_quote("US.SPY", 736.0, at(9, 50, 1))
    a.check(at(9, 50, 1))
    with pytest.raises(ValueError, match="stop goes below"):
        a.add_plan("SPY", {"direction": "long", "stop": 737, "target": 740}, now=at(9, 50, 2))
    plan = a.add_plan("SPY", {"direction": "long", "trigger": {"kind": "above", "price": 737}, "stop": 735.5, "target": 739,
                              "time_stop": "15:30"}, text="long over 737", now=at(9, 50, 2))
    assert plan["status"] == "waiting" and repo.rows[plan["id"]]["text"] == "long over 737"
    assert a.events_after(0)[-1]["title"] == "Plan watched: SPY long above 737"
    charts.push_quote("US.SPY", 737.2, at(10, 0))
    a.check(at(10, 0))
    assert a.events_after(0)[-1]["title"] == "Your plan triggered: SPY long at 737.2"
    a.io(at(10, 0, 1))
    assert repo.rows[plan["id"]]["status"] == "open"  # saved by the io pass
    charts.push_quote("US.SPY", 739.1, at(11, 0))
    a.check(at(11, 0))
    assert a.events_after(0)[-1]["title"] == "Your plan on SPY reached its target 739"
    other = a.add_plan("SPY", {"direction": "short", "trigger": {"kind": "below", "price": 730}, "stop": 733, "target": 725},
                       now=at(11, 1))
    assert a.remove_plan(other["id"], at(11, 2)) and other["id"] not in repo.rows
    assert a.events_after(0)[-1]["title"] == "Plan removed on SPY: short below 730"  # which plan, for the models too
    assert a.state()["tickers"][0]["plans"][0]["id"] == plan["id"]
    # The day's bars: up 0.01 a minute, about 737.2 at 10:00, so the 10:00 entry reaches 739 around 12:40.
    a._tickers["SPY"].bars = _day_bars([(736.9 + 0.01 * k, 736.9 + 0.01 * k + 0.2, 736.9 + 0.01 * k - 0.1,
                                         736.9 + 0.01 * k + 0.005) for k in range(390)])
    a.stop("the session closed", at(16, 15))
    graded = repo.rows[plan["id"]]
    assert graded["status"] == "closed" and graded["how"] == "target" and graded["month"] == "2026-11-09"
    assert graded["R"] > 0 and graded["twin_R"] is not None


def test_a_typed_plan_is_read_by_the_model_for_you_to_confirm(monkeypatch):
    from src.services.trade_desk import assistant_model
    seen = {}

    def fake_generate(prompt, system, **kwargs):
        seen["prompt"], seen["system"] = prompt, system
        return {"data": {"direction": "long", "trigger": {"kind": "close_above", "price": 738.03, "bar_minutes": 5},
                         "stop": 736, "target": 740, "time_stop": "15:30", "questions": ["Which strike?"]},
                "label": "Claude Code (claude-sonnet-5-5)", "seconds": 6.1}
    monkeypatch.setattr(assistant_model, "generate", fake_generate)
    a, charts, _ = _assistant()
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50))
    out = a.parse_plan("SPY", "calls if a 5 min bar closes over the pre-market high, stop 736, target 740, out by 3:30", at(9, 51))
    assert out["draft"]["trigger"] == {"kind": "close_above", "price": 738.03, "bar_minutes": 5}
    assert out["questions"] == ["Which strike?"] and out["model"].startswith("Claude Code")
    assert '"Pre-market high"' in seen["prompt"] and "Never invent a price" in seen["system"]
    assert a.events_after(0)[-1]["kind"] == "session"  # reading a plan watches nothing

    def failing(prompt, system, **kwargs):
        raise RuntimeError("no model answered: claude_code_cli usage limit; codex_cli timeout; litellm error")
    monkeypatch.setattr(assistant_model, "generate", failing)
    with pytest.raises(ValueError, match="No model answered"):
        a.parse_plan("SPY", "long", at(9, 52))


def test_after_the_close_open_plans_of_the_day_are_graded_and_older_ones_expire():
    repo = FakeRepo()
    repo.rows["you|SPY|plan|a"] = {"id": "you|SPY|plan|a", "ticker": "SPY", "status": "open", "source": "you", "kind": "plan",
                                   "direction": 1, "trigger": {"kind": "now", "price": None, "bar_minutes": None},
                                   "stop": 735.0, "target": 745.0, "time_stop": "15:00", "day": "2026-11-09",
                                   "entry_time": at(9, 45, 30).isoformat(), "price": 736.0, "created_at": at(9, 45).isoformat()}
    repo.rows["you|SPY|plan|old"] = {**repo.rows["you|SPY|plan|a"], "id": "you|SPY|plan|old", "day": "2026-11-06",
                                     "status": "waiting"}
    provider = SimpleNamespace(kline=lambda code, kind, count, push=False: [
        {**bar, "partial": False} for bar in _day_bars([(736, 736.5, 735.6, 736.2)] * 390)])
    assert asst.settle_open_plans(repo, provider, at(16, 20)) == 2
    assert repo.rows["you|SPY|plan|a"]["status"] == "closed" and repo.rows["you|SPY|plan|a"]["how"] == "time"
    assert repo.rows["you|SPY|plan|old"]["status"] == "expired"


def _model(answers):
    """A fake model chain: each call takes the next answer (a dict, or an exception to raise)."""
    calls = []

    def generate(prompt, system, **kwargs):
        calls.append({"prompt": prompt, "system": system, "images": kwargs.get("images"), "chain": kwargs.get("chain")})
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"data": answer, "label": "Claude Code (claude-sonnet-5-5)", "seconds": 5.0}
    return generate, calls


def _running(monkeypatch, answers, repo=None):
    from src.services.trade_desk import assistant_model
    generate, calls = _model(answers)
    monkeypatch.setattr(assistant_model, "generate", generate)
    a, charts, clock = _assistant(SimpleNamespace(repo=repo or FakeRepo()))
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50))
    charts.push_quote("US.SPY", 736.0, at(9, 50, 1))
    a.check(at(9, 50, 1))
    return a, charts, calls


def test_notes_come_on_events_at_most_every_few_minutes_and_at_once_for_a_plan(monkeypatch):
    a, charts, calls = _running(monkeypatch, [
        {"note": "SPY 736 is just under the pre-market high 738.03, an untested level.", "call": None},
        {"note": "Above the pre-market high 738.03 now.", "call": None},
        {"note": "Your plan is in at 737.2.", "call": None}])
    a.notes_step(at(9, 50, 2))  # the first look after the start
    note = a.state()["tickers"][0]["notes"][0]
    assert note["kind"] == "note" and "738.03" in note["text"] and note["model"].startswith("Claude Code")
    assert "first look" in calls[0]["prompt"] and "Never compute, round or invent a price" in calls[0]["system"]
    assert a.state()["model"]["calls_today"] == 1 and a.state()["model"]["by"] == [{"label": "Claude Code (claude-sonnet-5-5)", "calls": 1}]
    charts.push_quote("US.SPY", 738.5, at(9, 51))
    a.check(at(9, 51))
    a.notes_step(at(9, 51))
    assert len(calls) == 1  # a level crossed, but the last note is a minute old
    a.notes_step(at(9, 53, 30))
    assert len(calls) == 2 and "level: SPY above Pre-market high" in calls[1]["prompt"]
    a.add_plan("SPY", {"direction": "long", "trigger": {"kind": "above", "price": 739}, "stop": 737, "target": 742}, now=at(9, 54))
    a.notes_step(at(9, 54, 1))
    assert len(calls) == 2  # watching a plan asks for nothing
    charts.push_quote("US.SPY", 739.1, at(9, 54, 30))
    a.check(at(9, 54, 30))
    a.notes_step(at(9, 54, 31))
    assert len(calls) == 3  # it triggered: a note at once, inside the gap


def test_a_call_becomes_a_graded_assistant_call_and_an_invented_price_is_held_back(monkeypatch):
    a, charts, calls = _running(monkeypatch, [
        {"note": "Rejected at the pre-market high 738.03 twice.", "call": {"direction": "short", "trigger": {"kind": "now"},
                                                                       "stop": 738.03, "target": 735.0, "time_stop": "11:00",
                                                                       "why": "Two rejections at the level"}},
        {"note": "Watch 741.37 for a breakout.", "call": None}])
    a._tickers["SPY"].contracts = [{"code": "x", "right": "put", "strike": 735.0, "bid": 1.0, "ask": 1.1, "breakeven": 733.9}]
    a.notes_step(at(9, 50, 2))
    plans = a.state()["tickers"][0]["plans"]
    assert plans[0]["source"] == "assistant" and plans[0]["kind"] == "call" and plans[0]["status"] == "open"
    assert plans[0]["why"] == "Two rejections at the level" and plans[0]["model"].startswith("Claude Code")
    assert a.state()["tickers"][0]["notes"][0]["call"].startswith("short now")
    assert any(e["title"].startswith("Assistant call watched: SPY short now") for e in a.events_after(0))
    charts.push_quote("US.SPY", 739.0, at(10, 0))
    a.check(at(10, 0))
    a.notes_step(at(10, 0, 1))  # its stop: an urgent note, which quotes a price nobody showed
    held = a.state()["tickers"][0]["notes"][0]
    assert held["kind"] == "held" and "741.37" in held["text"] and "Watch" not in held["text"]


def test_questions_are_answered_from_the_state_and_the_daily_cap_holds(monkeypatch):
    monkeypatch.setenv("TRADE_ASSISTANT_DAILY_CALLS", "2")
    a, charts, calls = _running(monkeypatch, [
        {"answer": "At 736 you are 0.3% under the pre-market high 738.03; your stop is 728.", "note": "", "call": None},
        RuntimeError("no model answered: claude_code_cli usage limit; codex_cli timeout; litellm no key")])
    out = a.ask("SPY", "where is the risk?", at(9, 51))
    assert out["kind"] == "answer" and out["question"] == "where is the risk?" and "728" in out["text"]
    assert '"question": "where is the risk?"' in calls[0]["prompt"] and "The trader asks you a question" in calls[0]["system"]
    with pytest.raises(ValueError, match="No model answered"):
        a.ask("SPY", "and now?", at(9, 52))
    assert a.state()["model"]["failed"] == 1
    a._usage["calls"] = 2
    with pytest.raises(ValueError, match="2 model calls for today are used"):
        a.ask("SPY", "again?", at(9, 53))
    a.notes_step(at(9, 55))
    assert len(calls) == 2  # the cap stops notes too


def test_before_the_open_the_price_is_pre_market_and_plans_wait_for_the_session(monkeypatch):
    from datetime import timezone
    a, charts, clock = _assistant()
    # OpenD's last price is the prior close until the open; the pre-market trade is pre_price
    charts.provider.extra = {"last_price": 733.0, "pre_price": 736.4}
    a.start(["SPY"], "0dte", at(9, 20), threads=False)
    a.io(at(9, 20))
    spy = a.state(at(9, 20))["tickers"][0]
    assert spy["price"] == 736.4 and spy["price_at"] == "pre-market"
    charts.push_quote("US.SPY", 733.0, at(9, 21))  # a pushed quote before the open carries the prior close
    a.check(at(9, 21))
    assert a.state(at(9, 21))["tickers"][0]["price"] == 736.4 and [e["kind"] for e in a.events_after(0)] == ["session"]
    charts.provider.extra = {"last_price": 733.0, "pre_price": 737.0, "pre_high_price": 738.5}
    clock["mono"] += 10
    a.io(at(9, 25))
    spy = a.state(at(9, 25))["tickers"][0]
    assert spy["price"] == 737.0 and next(l for l in spy["levels"] if l["kind"] == "pm_high")["price"] == 738.5
    with pytest.raises(ValueError, match="Before the open a plan needs a trigger price"):
        a.add_plan("SPY", {"direction": "long", "trigger": {"kind": "now"}, "stop": 735, "target": 742}, now=at(9, 25))
    a.add_plan("SPY", {"direction": "long", "trigger": {"kind": "above", "price": 739}, "stop": 737.5, "target": 742}, now=at(9, 25))
    charts.push_quote("US.SPY", 739.5, at(9, 26))
    a.check(at(9, 26))
    assert a.state(at(9, 26))["tickers"][0]["plans"][0]["status"] == "waiting"  # plans are watched from the open
    opened = at(9, 30, 1).astimezone(timezone.utc)  # the server's clock is UTC
    charts.push_quote("US.SPY", 739.6, opened)
    a.check(opened)
    assert a.state(opened)["tickers"][0]["plans"][0]["status"] == "open"
    charts.provider.extra = {"last_price": 739.6, "pre_price": 737.0, "pre_high_price": 738.9}  # its last seconds set a new high
    clock["mono"] += 10
    a.io(at(9, 30, 10))
    spy = a.state(at(9, 30, 10))["tickers"][0]
    assert spy["price"] == 739.6 and next(l for l in spy["levels"] if l["kind"] == "pm_high")["price"] == 738.9
    charts.provider.extra = {"pre_high_price": 999.0}
    clock["mono"] += 30
    a.io(at(9, 30, 40))
    a.io(at(9, 31))  # read until 30 s after the open, then final
    clock["mono"] += 10
    a.io(at(9, 31, 10))
    assert next(l for l in a.state()["tickers"][0]["levels"] if l["kind"] == "pm_high")["price"] == 999.0
    charts.provider.extra = {"pre_high_price": 1.0}
    clock["mono"] += 10
    a.io(at(9, 31, 20))
    assert next(l for l in a.state()["tickers"][0]["levels"] if l["kind"] == "pm_high")["price"] == 999.0
    ctx = a._context(a._tickers["SPY"], opened, ["plan"])
    events = ctx["recent_events"]
    assert events[-1]["title"].startswith("Your plan triggered") and events[-1]["time"] == "09:30"  # New York, not UTC
    plan = ctx["plans"][0]  # in words: the bare status "open" read to the model like "not triggered yet"
    assert plan["state"] == "in the trade: triggered at 09:30, entry 739.6" and plan["entry_price"] == 739.6
    assert "status" not in plan and 739.6 in __import__("src.services.trade_desk.assistant_notes", fromlist=["x"]).known_prices(ctx)


def test_minutes_the_pushes_missed_are_read_again_from_opend():
    a, charts, clock = _assistant()
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50))
    item = a._tickers["SPY"]
    assert item.bars[-1]["time"] == at(9, 50).isoformat() and not item.bars_stale
    charts.push_bar("US.SPY", "K_1M", at(9, 53), open=737, high=737.5, low=736.8, close=737.2, volume=900)
    a.check(at(9, 52, 30))  # 09:51 and 09:52 never arrived: the opening ranges and VWAP would rest on a hole
    assert item.bars_stale and [b["time"][11:16] for b in item.bars[-2:]] == ["09:50", "09:53"]
    charts.provider.n_bars = 23  # OpenD has them
    a.io(at(9, 52, 31))
    assert not item.bars_stale and [b["time"][11:16] for b in item.bars[-4:]] == ["09:50", "09:51", "09:52", "09:53"]


# -- shadow mode's models, the chart picture, the close ---------------------------------------------------
def test_notes_and_answers_carry_todays_chart_unless_switched_off(monkeypatch):
    a, charts, calls = _running(monkeypatch, [{"note": "SPY 736 sits under the pre-market high 738.03.", "call": None},
                                              {"answer": "At 736.", "note": "", "call": None},
                                              {"answer": "At 736.", "note": "", "call": None}])
    a.notes_step(at(9, 50, 2))
    images = calls[0]["images"]
    assert len(images) == 3 and all(image["mime_type"] == "image/png" for image in images)  # 1-, 5- and 15-minute
    assert base64.b64decode(images[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"
    monkeypatch.setenv("TRADE_ASSISTANT_CHART_FRAMES", "5m")
    a.ask("SPY", "where?", at(9, 51))
    assert len(calls[1]["images"]) == 1 and "never from the picture's axis" in calls[1]["system"]
    monkeypatch.setenv("TRADE_DESK_CHART_IMAGES", "false")
    a.ask("SPY", "and now?", at(9, 52))
    assert calls[2]["images"] is None


def test_after_the_close_the_session_keeps_the_days_levels():
    a, charts, _ = _assistant()
    a.start(["SPY"], "0dte", at(9, 50), threads=False)
    a.io(at(9, 50))
    item = a._tickers["SPY"]
    before = list(item.chart_levels)
    charts.chart = lambda ticker, interval, now=None: {"levels": [{"kind": "prior_close", "price": 736.0,
                                                                  "label": "Prior day close (Nov 9)"}], "flags": []}
    a._load_chart(item, at(16, 0, 5), alert=True)
    assert item.chart_levels == before  # the chart moved on to tomorrow, where today is the prior day
    a._load_chart(item, at(15, 45, 5), alert=True)
    assert item.chart_levels[0]["label"] == "Prior day close (Nov 9)"  # in the session it still reloads


def test_the_model_order_is_tried_in_turn_with_the_chart(monkeypatch):
    from src.services.trade_desk import assistant_model as am
    monkeypatch.setenv("TRADE_ASSISTANT_MODELS", "claude_code_cli:claude-sonnet-5-5:xhigh, codex_cli:gpt-6.1-sol:xhigh,"
                                                 "claude_code_cli:claude-opus-5-5:xhigh,litellm")
    entries = am.entries("assistant")
    assert [(e.backend, e.model, e.effort) for e in entries] == [
        ("claude_code_cli", "claude-sonnet-5-5", "xhigh"), ("codex_cli", "gpt-6.1-sol", "xhigh"),
        ("claude_code_cli", "claude-opus-5-5", "xhigh"), ("litellm", "", "")]
    seen = []

    class Backend:
        def __init__(self, entry):
            self.entry = entry

        def generate(self, prompt, config, system_prompt=None):
            seen.append((self.entry.model, config.get("images")))
            if self.entry.backend == "claude_code_cli":
                raise RuntimeError("usage limit")
            return SimpleNamespace(text='{"note": "ok"}', model="", usage={})

    analyzer_for = lambda entry: SimpleNamespace(_get_generation_backend=lambda backend_id: Backend(entry))
    image = {"mime_type": "image/png", "data": "QUJD"}
    out = am.generate("state", "rules", images=[image], analyzer_for=analyzer_for)
    assert out["data"] == {"note": "ok"} and out["label"] == "Codex (gpt-6.1-sol, xhigh)"
    assert seen == [("claude-sonnet-5-5", [image]), ("gpt-6.1-sol", [image])]
    assert out["tried"][0]["backend"] == "claude_code_cli" and out["tried"][0]["model"] == "claude-sonnet-5-5"
    assert am.describe("assistant").startswith("Claude Code (claude-sonnet-5-5, xhigh), then Codex (gpt-6.1-sol, xhigh), then ")

    for bad, words in (("gpt:x", "backend:model:effort"), ("codex_cli:gpt-6.1-sol:max", "effort for codex_cli"),
                       ("litellm:m:high", "effort for litellm")):
        monkeypatch.setenv("TRADE_ASSISTANT_MODELS", bad)
        with pytest.raises(ValueError, match=words):
            am.entries("assistant")
        with pytest.raises(RuntimeError, match="TRADE_ASSISTANT_MODELS"):
            am.generate("state", "rules", analyzer_for=analyzer_for)
    monkeypatch.delenv("TRADE_ASSISTANT_MODELS")
    monkeypatch.setenv("TRADE_ASSISTANT_BACKENDS", "codex_cli,litellm")
    assert [(e.backend, e.legacy) for e in am.entries("assistant")] == [("codex_cli", True), ("litellm", True)]
    assert not am.configured("questions")
    with pytest.raises(ValueError, match="TRADE_DESK_QUESTION_MODELS is not set"):
        am.entries("questions")


# -- the three tiers: code facts, the watcher, the analyst ----------------------------------------------------
WATCH = "claude_code_cli:claude-haiku-5-5:low,litellm"


def test_the_code_tier_hands_the_models_facts_from_completed_bars_only():
    from src.services.trade_desk import assistant_watch as aw
    provider = FakeProvider()
    bars = [b for b in provider.kline("US.SPY", "K_1M", 400) if b["time"] >= at(9, 31).isoformat()]  # 09:31 .. 09:50
    state = {"ticker": "SPY", "price": 737.0, "levels": [
        {"label": "Pre-market high", "price": 737.03, "verdict": "not tested"},
        {"label": "Far away", "price": 760.0, "verdict": "not tested"}]}
    found = aw.facts(state, bars, at(9, 49, 30), band=asst.LEVEL_BAND,
                     in_play={"rows": [{"ticker": "SPY", "rank": 2, "S": 2.1}, {"ticker": "QQQ", "rank": 1}]})
    assert found["last_completed_minute_ends"] == "09:49"  # the 09:50 bar was still forming
    assert found["in_play_today"]["rank"] == 2 and "not a direction" in found["in_play_today"]["verdict"]
    near = found["levels_near_price"]
    assert [level["label"] for level in near] == ["Pre-market high"]  # 760 is beyond 1.5 %
    assert near[0]["last_1m_close"] == "at" and near[0]["last_5m_close"] in ("at", "below")
    fives = aw.five_minute(aw.completed(bars, at(9, 49, 30)))
    assert [five["end"].strftime("%H:%M") for five in fives] == ["09:35", "09:40", "09:45"]  # 09:45-09:50 not complete


def test_the_watcher_reads_market_events_and_escalates_to_the_analyst(monkeypatch):
    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_MODELS", WATCH)
    a, charts, calls = _running(monkeypatch, [
        {"note": "SPY 736 sits under the pre-market high 738.03.", "call": None},                    # first look: analyst
        {"note": "SPY crossed 738.03 but no bar closed past it.", "escalate": False, "why": "No bar confirms it."},
        {"note": "SPY closed above 738.03 on rising volume.", "escalate": True, "why": "A 1-minute bar closed past it."},
        {"note": "Above the pre-market high 738.03; the plan's trigger 739 is close.", "call": None}])  # the analyst
    a.notes_step(at(9, 50, 2))
    assert calls[0]["chain"] is None and len(calls[0]["images"]) == 3 and '"facts"' in calls[0]["prompt"]
    charts.push_quote("US.SPY", 738.5, at(9, 51))
    a.check(at(9, 51))
    assert a._wants["SPY"] == [] and "level: SPY above Pre-market high 738.03" in a._watch_wants["SPY"]
    a.notes_step(at(9, 51, 1))
    assert len(calls) == 1  # a market event: the analyst's queue doesn't take it
    a.watch_step(at(9, 51, 1))
    assert calls[1]["chain"] == "watch" and len(calls[1]["images"]) == 1 and "first, fast reader" in calls[1]["system"]
    assert "level: SPY above Pre-market high 738.03" in calls[1]["prompt"] and '"analyst_last_note"' in calls[1]["prompt"]
    a.notes_step(at(9, 53, 30))
    assert len(calls) == 2  # not escalated: the analyst was not asked
    a._watch_wants["SPY"] = ["level: SPY above Pre-market high 738.03"]
    a.watch_step(at(9, 57))
    assert calls[2]["chain"] == "watch" and '"your_last_read"' in calls[2]["prompt"] and len(calls) == 3
    a.notes_step(at(9, 57, 1))  # the escalation waits in the analyst's queue, so the watcher never waits for it
    assert calls[3]["chain"] is None
    assert "watcher escalated: A 1-minute bar closed past it." in calls[3]["prompt"]
    notes = a.state()["tickers"][0]["notes"]
    assert notes[0]["tier"] == "analyst" and notes[1]["tier"] == "watcher" and notes[1]["escalate"] is True
    model = a.state()["model"]
    assert model["watch"]["calls_today"] == 2 and model["watch"]["escalated_today"] == 1 and model["calls_today"] == 2


def test_your_plan_events_skip_the_watcher_and_escalations_have_an_hourly_limit(monkeypatch):
    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_MODELS", WATCH)
    monkeypatch.setenv("TRADE_ASSISTANT_ANALYST_PER_HOUR", "1")
    a, charts, calls = _running(monkeypatch, [
        {"note": "First look at 736.", "call": None},
        {"note": "Your plan triggered at 739.1.", "call": None},
        {"note": "A break of 738.03.", "escalate": True, "why": "Closed past it."},
        {"note": "The analyst on 738.03.", "call": None},
        {"note": "Another break of 738.03.", "escalate": True, "why": "Again."}])
    a.notes_step(at(9, 50, 2))
    a.add_plan("SPY", {"direction": "long", "trigger": {"kind": "above", "price": 739}, "stop": 737, "target": 742}, now=at(9, 50, 3))
    charts.push_quote("US.SPY", 739.1, at(9, 50, 30))
    a.check(at(9, 50, 30))
    a.notes_step(at(9, 50, 31))
    assert calls[1]["chain"] is None and "plan" in calls[1]["prompt"]  # yours: straight to the analyst
    a._watch_wants["SPY"] = ["level: SPY above Pre-market high 738.03"]
    a.watch_step(at(9, 54))
    a.notes_step(at(9, 54, 1))
    assert [c["chain"] for c in calls[2:4]] == ["watch", None]
    a._watch_wants["SPY"] = ["level: SPY below Pre-market high 738.03"]
    a.watch_step(at(9, 58))
    a.notes_step(at(9, 58, 1))
    assert len(calls) == 5 and calls[4]["chain"] == "watch"  # escalated, but the hour's one analyst call is used
    assert "escalations this hour are used" in a.state()["tickers"][0]["notes"][0]["why"]


def test_a_watcher_that_fails_hands_over_and_an_invented_price_is_held(monkeypatch):
    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_MODELS", WATCH)
    a, charts, calls = _running(monkeypatch, [
        {"note": "First look.", "call": None},
        RuntimeError("no model answered: claude_code_cli usage limit; litellm error"),
        {"note": "The analyst instead, at 736.", "call": None},
        {"note": "A move to 741.37.", "escalate": True, "why": "Big."}])
    a.notes_step(at(9, 50, 2))
    a._watch_wants["SPY"] = ["flag: SPY fade short armed"]
    a.watch_step(at(9, 54))
    a.notes_step(at(9, 54, 1))
    assert [c["chain"] for c in calls[1:3]] == ["watch", None] and "watcher: unavailable" in calls[2]["prompt"]
    a._watch_wants["SPY"] = ["flag: SPY fade short watching"]
    a.watch_step(at(9, 58))
    a.notes_step(at(9, 58, 1))
    assert a.state()["tickers"][0]["notes"][0]["kind"] == "held" and len(calls) == 4  # nothing escalated from a held note


def test_the_watcher_reads_each_completed_minute_quietly_and_events_within_seconds(monkeypatch):
    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_MODELS", WATCH)
    a, charts, calls = _running(monkeypatch, [
        {"note": "SPY 736 sits under the pre-market high 738.03.", "call": None},                         # first look
        {"note": "SPY holds 736, a quiet drift.", "escalate": False, "why": "Nothing new."},             # 09:50 bar
        {"note": "The 09:51 bar closed above 738.03.", "escalate": True, "why": "A completed bar broke it."},
        {"note": "Above the pre-market high 738.03.", "call": None},                                     # the analyst
        {"note": "SPY at 736 again.", "escalate": False, "why": "Back inside."}])                        # the event
    a.notes_step(at(9, 50, 2))
    a.watch_step(at(9, 50, 3))
    assert calls[1]["chain"] == "watch" and "minute: the 1-minute bar to 09:50 closed" in calls[1]["prompt"]
    ticker = a.state()["tickers"][0]
    assert [note["tier"] for note in ticker["notes"]] == ["analyst"]  # not escalated: only the latest read changes
    assert ticker["watch"]["text"] == "SPY holds 736, a quiet drift." and ticker["watch"]["trigger"] == "minute"
    a.watch_step(at(9, 50, 30))
    assert len(calls) == 2  # that minute is read
    charts.push_bar("US.SPY", "K_1M", at(9, 51), close=738.5)
    a.check(at(9, 50, 31))
    charts.push_bar("US.SPY", "K_1M", at(9, 52), close=738.6)
    a.check(at(9, 51, 1))
    a.watch_step(at(9, 51, 2))
    assert "minute: the 1-minute bar to 09:51 closed" in calls[2]["prompt"] and '"your_last_read"' in calls[2]["prompt"]
    assert a.state()["tickers"][0]["notes"][0]["escalate"] is True  # an escalation goes to the notes too
    a.notes_step(at(9, 51, 3))
    assert calls[3]["chain"] is None and "watcher escalated: A completed bar broke it." in calls[3]["prompt"]
    a._watch_wants["SPY"] = ["level: SPY below Pre-market high 738.03"]
    a.watch_step(at(9, 51, 7))
    assert len(calls) == 4  # an event waits until 10 seconds after the last read
    a.watch_step(at(9, 51, 12))
    assert calls[4]["chain"] == "watch" and "level: SPY below" in calls[4]["prompt"]
    assert a.state()["tickers"][0]["notes"][0]["trigger"] == "event"  # an event's read is shown, escalated or not
    assert [d["trigger"] for d in a._watch_log] == ["minute", "minute", "event"]
    a._wants["SPY"] = ["watcher escalated: one"]  # still waiting for the analyst: a second escalation joins it
    a._hand_over(a._tickers["SPY"], ["watcher escalated: two"], at(9, 51, 13))
    assert a._wants["SPY"] == ["watcher escalated: one", "watcher escalated: two"] and len(a._analyst_times) == 1
    watch = a.state()["model"]["watch"]
    assert watch["calls_today"] == 3 and watch["every_minute"] is True and watch["event_seconds"] == 10 and watch["cap"] == 1500


def test_minute_reads_only_in_the_regular_session_and_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_MODELS", WATCH)
    a, charts, calls = _running(monkeypatch, [{"note": "First look.", "call": None}])
    a.notes_step(at(9, 50, 2))
    a.watch_step(at(16, 5))
    assert len(calls) == 1  # after the close: no minute reads
    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_EVERY_MINUTE", "false")
    a.watch_step(at(9, 50, 5))
    assert len(calls) == 1 and a.state()["model"]["watch"]["every_minute"] is False
    monkeypatch.delenv("TRADE_ASSISTANT_WATCH_MODELS")
    charts.push_quote("US.SPY", 738.5, at(9, 51))
    a.check(at(9, 51))
    assert "level: SPY above Pre-market high 738.03" in a._wants["SPY"] and not a._watch_wants  # no watcher: the analyst's


def test_watcher_decisions_are_graded_by_the_next_30_minutes_and_summarised_by_day(monkeypatch):
    from src.services.trade_desk import assistant_watch as aw
    bars = [{"time": (at(10, 0) + timedelta(minutes=k)).isoformat(), "open": 100, "high": 100 + (2 if k == 10 else 0.5),
             "low": 99.5, "close": 100, "volume": 10} for k in range(1, 61)]
    decisions = [{"ticker": "SPY", "at": at(10, 0, 30).isoformat(), "escalate": True, "price": 100.0},  # mid-minute
                 {"ticker": "SPY", "at": at(10, 40).isoformat(), "escalate": False, "price": 100.0},  # past the window end
                 {"ticker": "SPY", "at": at(10, 20).isoformat(), "escalate": False, "price": 100.0}]
    graded = aw.grade_decisions(decisions, bars)
    assert [(row["at"][11:16], row["range_pct"]) for row in graded] == [("10:00", 2.5), ("10:20", 1.0)]  # 10:40 runs past 11:00
    days = {f"2026-10-{d:02d}": [{"ticker": "SPY", "escalate": True, "range_pct": 1.0 + d * 0.01},
                                 {"ticker": "SPY", "escalate": False, "range_pct": 0.5}] for d in range(1, 6)}
    out = aw.summary(days)
    assert out["days"] == 5 and out["verdict"].startswith("too early: 5 of 30") and out["mean_difference_pct"] > 0

    monkeypatch.setenv("TRADE_ASSISTANT_WATCH_MODELS", WATCH)
    repo = FakeRepo()
    a, charts, calls = _running(monkeypatch, [{"note": "First look.", "call": None}], repo=repo)
    a._watch_log = [{"ticker": "SPY", "at": at(9, 31).isoformat(), "escalate": True, "price": 735.0}]
    a.stop("the session closed", at(16, 15))
    assert "2026-11-09" not in (repo.setting(aw.GRADES_SETTING) or {})  # 09:31 + 30 min is past the 20 bars held
