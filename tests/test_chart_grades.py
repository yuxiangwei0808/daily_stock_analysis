"""Forward grades on the chart: flags seen in time, your marks and boxes, the twins and the verdict."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.services.trade_desk import boxes as bx
from src.services.trade_desk import chart as ch
from src.services.trade_desk import chart_grades as cg
from src.services.trade_desk.backtest import boxes as bb
from tests.test_boxes import QUIET, SIDEWAYS, _bars, _impulse_day
from tests.test_broker_holdings import repo  # noqa: F401
from tests.test_trade_desk_chart import NY, FakeMoomoo, _rows

BREAK = [(102.9, 102.95, 102.5, 102.6), (102.6, 102.95, 102.55, 102.7), (102.7, 102.75, 101.0, 101.2),
         (101.2, 101.3, 99.9, 100.1)]  # the break, the retest (short at 102.9), and the fall to the base box


def _charts(repo, rows):  # noqa: F811
    fake = FakeMoomoo({"K_15M": rows})
    return ch.Charts(SimpleNamespace(holdings=None, repo=repo, provider_factory=lambda mode: fake)), fake


def _at(rows, k, minutes=1):
    return datetime.fromisoformat(rows[k]["time"]) + timedelta(minutes=minutes)


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    monkeypatch.delenv("TRADE_DESK_BROKER_ACCOUNT", raising=False)


def test_a_flag_seen_before_its_entry_is_followed_to_its_exit_and_graded_against_its_twins(repo, monkeypatch):  # noqa: F811
    bars = _bars([QUIET] * 11 + [_impulse_day(SIDEWAYS, BREAK)] + [QUIET] * 11)
    s = next(bx.setups(bars))
    rows = _rows(bars)
    open_ = [True]
    monkeypatch.setattr(ch, "_session_open", lambda now: open_[0])
    # The break bar is forming: the retest is "watching", and seen now.
    charts, fake = _charts(repo, [dict(r) for r in rows[:s.i + 10]])
    fake.rows["K_15M"][-1]["partial"] = True
    charts.chart("SPY", now=_at(rows, s.i + 8))
    seen = [r for r in repo.chart_trades() if r["kind"] == "break_retest"]
    assert [(r["status"], r["direction"], r["price"]) for r in seen] == [("waiting", -1, 102.9)]
    # After the session the trade has reached its target; the twins wait for their own five sessions.
    open_[0] = False
    fake.rows = {"K_15M": rows[:(12 + 3) * 26]}
    charts.chart("SPY", now=_at(rows, (12 + 3) * 26 - 1, 60))
    rec = next(r for r in repo.chart_trades() if r["kind"] == "break_retest")
    assert (rec["status"], rec["how"], rec["exit"]) == ("closed", "target", 100.0) and rec.get("twin_R") is None
    assert rec["R"] == pytest.approx((102.9 - 100.0 - bb.COST * 102.9) / 0.5, abs=1e-4)
    fake.rows = {"K_15M": rows}
    charts.chart("SPY", now=_at(rows, len(rows) - 1, 60))
    rec = next(r for r in repo.chart_trades() if r["kind"] == "break_retest")
    assert rec["twin_R"] is not None and rec["month"] == "2026-01"
    row = next(k for k in charts.grade_table()["kinds"] if k["kind"] == "break_retest")
    assert (row["graded"], row["verdict"], row["mean_R"]) == (1, "too_early", rec["R"])


def test_a_flag_seen_only_during_its_entry_bar_or_after_it_traded_does_not_count(repo, monkeypatch):  # noqa: F811
    bars = _bars([QUIET] * 11 + [_impulse_day(SIDEWAYS, BREAK)] + [QUIET] * 2)
    s = next(bx.setups(bars))
    rows = _rows(bars)
    monkeypatch.setattr(ch, "_session_open", lambda now: True)
    charts, fake = _charts(repo, [dict(r) for r in rows[:s.i + 11]])
    fake.rows["K_15M"][-1]["partial"] = True  # the retest bar is forming: it may have filled already
    charts.chart("SPY", now=_at(rows, s.i + 9))
    fake.rows = {"K_15M": rows}
    charts.chart("SPY", now=_at(rows, len(rows) - 1, 60))
    assert [r["status"] for r in repo.chart_trades() if r["kind"] == "break_retest"] == ["late"]

    other = cg.ChartGrades(repo)
    repo.save_chart_trades([{**r, "status": "deleted"} for r in repo.chart_trades()])
    charts2, _ = _charts(repo, rows)
    charts2.chart("QQQ", now=_at(rows, len(rows) - 1, 60))  # first seen with the trade already done
    assert not [r for r in repo.chart_trades(ticker="QQQ") if r["kind"] == "break_retest"]
    assert other.summary()["kinds"][0]["graded"] == 0


def test_a_mark_enters_at_the_next_bar_open_and_one_past_its_stop_is_void(repo, monkeypatch):  # noqa: F811
    bars = _bars([QUIET] * 11 + [[(100.6, 100.7, 100.5, 100.6)] * 26] + [QUIET] * 6)  # the next session opens higher
    rows = _rows(bars)
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, fake = _charts(repo, rows[:11 * 26])
    before_open = _at(rows, 11 * 26 - 1, 60)  # after the 11th session's close
    with pytest.raises(ValueError, match="stop goes below"):
        charts.add_mark("SPY", "long", 100.5, 101.0, now=before_open)
    mark = charts.add_mark("SPY", "long", 99.6, 101.5, note="test", now=before_open)
    assert (mark["status"], mark["direction"], mark["reference"]) == ("waiting", "long", 100.0)
    void = charts.add_mark("SPY", "short", 100.5, 99.0, now=before_open)  # the next bar opens at 100.6: past this stop
    fake.rows = {"K_15M": rows}
    body = charts.chart("SPY", now=_at(rows, len(rows) - 1, 60))
    yours = {item["id"]: item for item in body["yours"]}
    assert yours[mark["id"]]["entry_time"] == "2026-01-20T09:30:00-05:00" and yours[mark["id"]]["price"] == 100.6
    assert (yours[mark["id"]]["status"], yours[mark["id"]]["how"]) == ("closed", "stop")
    assert yours[void["id"]]["status"] == "void" and yours[void["id"]]["void"] == "opened past your stop"
    stored = next(r for r in repo.chart_trades() if r["id"] == mark["id"])
    assert stored["twin_R"] is not None  # twins at random opens of the same session, same distances
    assert not charts.remove_yours(mark["id"])  # a trade stays on the record


def test_a_kept_box_is_armed_while_the_price_is_away_and_trades_by_the_fade_rule(repo, monkeypatch):  # noqa: F811
    away = [(103.0, 103.1, 102.9, 103.0)] * 3 + [(103.0, 103.0, 100.1, 100.15)]
    bars = _bars([QUIET] * 11 + [_impulse_day(SIDEWAYS, away)] + [QUIET] * 8)
    rows = _rows(bars)
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, fake = _charts(repo, rows[:11 * 26])
    box = charts.add_box("SPY", 100.2, 99.8, now=_at(rows, 11 * 26 - 1, 60))
    removable = charts.add_box("SPY", 90, 80, now=_at(rows, 11 * 26 - 1, 60))
    assert charts.remove_yours(removable["id"]) and not charts.remove_yours("sys|SPY|x")
    fake.rows = {"K_15M": rows[:11 * 26 + 12]}  # above the box after the gap
    body = charts.chart("SPY", now=_at(rows, 11 * 26 + 11, 60))
    mine = next(item for item in body["yours"] if item["id"] == box["id"])
    assert (mine["status"], mine["armed"], mine["direction"], mine["price"], mine["stop"]) == ("waiting", True, "long", 100.2, 99.8)
    fake.rows = {"K_15M": rows}
    body = charts.chart("SPY", now=_at(rows, len(rows) - 1, 60))
    mine = next(item for item in body["yours"] if item["id"] == box["id"])
    assert (mine["status"], mine["price"], mine["target"], mine["how"]) == ("closed", 100.2, 101.0, "stop")
    with pytest.raises(ValueError):
        charts.add_box("SPY", 1.0, 2.0)


def test_a_box_no_price_returns_to_expires_after_five_sessions(repo, monkeypatch):  # noqa: F811
    rows = _rows(_bars([QUIET] * 18))
    monkeypatch.setattr(ch, "_session_open", lambda now: False)
    charts, fake = _charts(repo, rows)
    box = charts.add_box("SPY", 90.0, 89.0, now=_at(rows, 11 * 26 - 1, 60))
    body = charts.chart("SPY", now=_at(rows, len(rows) - 1, 60))
    assert next(item for item in body["yours"] if item["id"] == box["id"])["status"] == "expired"


def _graded(repo, edge, mean, n=30):  # noqa: F811
    repo.save_chart_trades([{"id": f"you|SPY|mark|{k}", "ticker": "SPY", "status": "closed", "source": "you", "kind": "mark",
                             "R": mean + (0.1 if k % 2 else -0.1), "twin_R": mean - edge + (0.1 if k % 2 else -0.1) * 0.5,
                             "month": f"2026-{1 + k % 6:02d}", "ret_pct": 0.1} for k in range(n)])


def test_a_kind_is_too_early_until_30_trades_then_passes_only_with_an_edge_that_made_money(repo):  # noqa: F811
    grades = cg.ChartGrades(repo)
    _graded(repo, edge=0.5, mean=0.3, n=29)
    assert next(k for k in grades.summary()["kinds"] if k["kind"] == "mark")["verdict"] == "too_early"
    _graded(repo, edge=0.5, mean=0.3)
    row = next(k for k in grades.summary()["kinds"] if k["kind"] == "mark")
    assert (row["graded"], row["months"], row["verdict"]) == (30, 6, "passes") and row["t"] >= row["t_needed"]
    _graded(repo, edge=0.5, mean=-0.2)
    assert next(k for k in grades.summary()["kinds"] if k["kind"] == "mark")["verdict"] == "no_edge"


def test_the_daily_pass_reads_the_charts_with_trades_still_running_once_after_the_close(repo, monkeypatch):  # noqa: F811
    charts, _ = _charts(repo, [])
    repo.save_chart_trades([{"id": "sys|SPY|0.6|1|x|fade|range", "ticker": "SPY", "status": "open", "source": "system",
                             "kind": "fade", "impulse_t": 0.6, "tidy": True},
                            {"id": "you|QQQ|mark|1", "ticker": "QQQ", "status": "closed", "source": "you", "kind": "mark"},
                            {"id": "you|IWM|mark|2", "ticker": "IWM", "status": "expired", "source": "you", "kind": "mark"}])
    read = []
    monkeypatch.setattr(charts, "chart", lambda ticker, interval, impulse_t, tidy: read.append((ticker, impulse_t, tidy)))
    charts.grade_due(datetime(2026, 11, 6, 16, 0, tzinfo=NY))  # before the close's pass
    charts.grade_due(datetime(2026, 11, 7, 17, 0, tzinfo=NY))  # Saturday
    assert read == []
    charts.grade_due(datetime(2026, 11, 6, 16, 30, tzinfo=NY))
    charts._grading.join(5)
    charts.grade_due(datetime(2026, 11, 6, 17, 30, tzinfo=NY))
    assert read == [("QQQ", 1.0, False), ("SPY", 0.6, True)]  # the mark still waits for its twins


def test_the_routes_record_marks_and_boxes_and_serve_the_grades(repo, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints.trade_desk import router
    rows = _rows(_bars([QUIET] * 12))
    charts, _ = _charts(repo, rows)
    app = FastAPI()
    app.state.trade_desk_service = SimpleNamespace(charts=charts)
    app.include_router(router, prefix="/api/v1/trade-desk")
    with TestClient(app) as client:
        made = client.post("/api/v1/trade-desk/chart/SPY/marks", json={"direction": "short", "stop": 101, "target": 98})
        assert made.status_code == 201 and made.json()["status"] == "waiting"
        assert client.post("/api/v1/trade-desk/chart/SPY/marks", json={"direction": "short", "stop": 99, "target": 98}).status_code == 422
        assert client.post("/api/v1/trade-desk/chart/SPY/marks", json={"direction": "up", "stop": 99, "target": 98}).status_code == 422
        box = client.post("/api/v1/trade-desk/chart/SPY/boxes", json={"top": 101, "bottom": 100.5})
        assert box.status_code == 201
        assert client.delete(f"/api/v1/trade-desk/chart-yours/{box.json()['id']}").status_code == 204
        assert client.delete("/api/v1/trade-desk/chart-yours/sys%7CSPY%7Cx").status_code == 409
        table = client.get("/api/v1/trade-desk/chart-grades").json()
        assert [k["label"] for k in table["kinds"]] == ["System: break and retest", "System: fade at a box", "Your marks", "Your boxes",
                                                   "Your plans", "Assistant calls", "Assistant trims and exits"]
        assert [k["unit"] for k in table["kinds"]][-2:] == ["R", "%"]
        assert table["recent"][0]["id"] == made.json()["id"] and table["min_trades"] == 30
