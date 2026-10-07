"""Portfolio risk: implied volatility from marks, delta and theta, beta, and index-move scenarios."""
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from src.services.trade_desk import risk as r

NOW = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)
EXPIRY = (NOW + timedelta(days=16)).date().isoformat()


def _bars():
    random.seed(1)
    days = [(date(2025, 9, 1) + timedelta(days=i)).isoformat() for i in range(260)]
    series = {"SPY": [100.0], "QQQ": [100.0], "NVDA": [100.0], "USO": [100.0]}
    for _ in days[1:]:
        move = random.gauss(0, 0.01)
        series["SPY"].append(series["SPY"][-1] * (1 + move))
        series["QQQ"].append(series["QQQ"][-1] * (1 + 1.2 * move))
        series["NVDA"].append(series["NVDA"][-1] * (1 + 2 * move + random.gauss(0, 0.005)))
        series["USO"].append(series["USO"][-1] * (1 + random.gauss(0, 0.02)))
    return {name: [{"date": d, "close": c} for d, c in zip(days, closes)] for name, closes in series.items()}


def test_implied_volatility_round_trips_and_refuses_impossible_marks():
    years = r._years_to(EXPIRY, NOW)
    price = r._bs(100, 100, years, 0.42, "put")
    assert r.implied_vol(price, 100, 100, years, "put") == pytest.approx(0.42, abs=1e-4)
    assert r.implied_vol(0.5, 120, 100, years, "call") is None  # below intrinsic
    assert r.implied_vol(None, 100, 100, years, "call") is None


def test_greeks_scenarios_and_the_discord_line(monkeypatch):
    r._beta_cache.clear()
    view = {"total_assets": 50000, "stocks": [{"ticker": "NVDA", "qty": 30, "price": 182.0}],
            "options": [{"underlying": "USO", "expiry": EXPIRY, "expired": False, "underlying_price": 82.1,
                         "legs": [{"right": "call", "strike": 80, "qty": 3, "mark": 3.6},
                                  {"right": "call", "strike": 85, "qty": -3, "mark": 1.2}]},
                        {"underlying": "OLD", "expiry": "2026-09-01", "expired": True, "underlying_price": 5.0, "legs": []}]}
    out = r.portfolio_risk(view, download=lambda tickers, period="1y": _bars(), now=NOW)
    rows = {row["ticker"]: row for row in out["rows"]}
    assert set(rows) == {"NVDA", "USO"}  # expired rows are left out
    assert rows["NVDA"]["shares_equiv"] == 30 and rows["NVDA"]["beta_spy"] == pytest.approx(2.0, abs=0.15)
    assert 0 < rows["USO"]["shares_equiv"] < 300 and rows["USO"]["theta_per_day"] < 0  # a bull call spread
    spy_down = {item["key"]: item for item in out["scenarios"]}["SPY-3"]
    nvda_part = 30 * 182.0 * rows["NVDA"]["beta_spy"] * -0.03
    assert spy_down["pnl"] == pytest.approx(nvda_part, abs=40)  # USO barely follows SPY here
    assert spy_down["pct"] == pytest.approx(spy_down["pnl"] / 500, abs=0.01)
    line = r.summary_line(out)
    assert line.startswith("Risk (estimate): if SPY -3% ≈ -0.") and "$" not in line  # percentages only


def test_missing_mark_leaves_exposure_and_portfolio_totals_unavailable():
    r._beta_cache.clear()
    view = {"total_assets": 0, "stocks": [], "options": [{"underlying": "ZZZ", "expiry": EXPIRY, "expired": False,
                                                         "underlying_price": 50.0,
                                                         "legs": [{"right": "put", "strike": 60, "qty": 2, "mark": None}]}]}
    out = r.portfolio_risk(view, download=lambda tickers, period="1y": {}, now=NOW)
    [row] = out["rows"]
    assert row["beta_assumed"] and row["shares_equiv"] is None and row["theta_partial"]
    assert out["totals"]["delta_dollars"] is None and out["totals"]["theta_per_day"] is None
    moves = {item["key"]: item["pnl"] for item in out["scenarios"]}
    assert moves["SPY-3"] is None and not out["complete"]
    assert all(item["pct"] is None for item in out["scenarios"])  # no account total, no percentages


def test_a_failed_beta_download_is_retried_not_kept_for_the_day():
    r._beta_cache.clear()
    view = {"total_assets": 100000, "stocks": [{"ticker": "NVDA", "qty": 10, "price": 100.0}], "options": []}
    calls = []

    def download(tickers, period="1y"):
        calls.append(list(tickers))
        return {} if len(calls) == 1 else _bars()
    first = r.portfolio_risk(view, download=download, now=NOW)
    assert first["rows"][0]["beta_assumed"]
    second = r.portfolio_risk(view, download=download, now=NOW)
    assert not second["rows"][0]["beta_assumed"] and len(calls) == 2
    r.portfolio_risk(view, download=download, now=NOW)
    assert len(calls) == 2  # measured betas are kept for the day


@pytest.mark.parametrize("spot,mark,spot_fresh,mark_fresh", [
    (None, 5, True, True), (100, 5, False, True), (100, 5, True, False), (100, None, True, True)])
def test_partial_risk_never_reports_missing_options_as_zero(spot, mark, spot_fresh, mark_fresh):
    view = {"total_assets": 100000, "stocks": [{"ticker": "GOOD", "qty": 10, "price": 100}],
            "options": [{"underlying": "AAA", "expiry": EXPIRY, "underlying_price": spot,
                         "underlying_fresh": spot_fresh,
                         "legs": [{"right": "call", "strike": 100, "qty": 10, "mark": mark, "mark_fresh": mark_fresh}]}]}
    out = r.portfolio_risk(view, download=lambda *a, **kw: {}, now=NOW)
    assert out["unavailable_tickers"] == ["AAA"]
    assert out["totals"]["delta_dollars"] is None
    assert all(item["pnl"] is None and item["pct"] is None for item in out["scenarios"])
    assert "unavailable" in r.summary_line(out) and "+0.0%" not in r.summary_line(out)
    assert next(row for row in out["rows"] if row["ticker"] == "GOOD")["delta_dollars"] == 1000


def test_stale_stock_mark_is_not_used_for_portfolio_totals():
    out = r.portfolio_risk({"stocks": [{"ticker": "AAA", "qty": 100, "price": 10, "price_fresh": False}]},
                           download=lambda *a, **kw: {}, now=NOW)
    assert out["totals"]["delta_dollars"] is None and out["scenarios"][0]["pnl"] is None
