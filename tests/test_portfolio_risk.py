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


@pytest.mark.parametrize("spot,mark,wide", [(None, 5, False), (100, None, False), (100, 5, True)])
def test_an_unpriceable_option_is_left_out_of_the_totals_and_named(spot, mark, wide):
    r._beta_cache.clear()
    view = {"total_assets": 100000, "stocks": [{"ticker": "GOOD", "qty": 10, "price": 100}],
            "options": [{"underlying": "AAA", "expiry": EXPIRY, "underlying_price": spot,
                         "legs": [{"right": "call", "strike": 100, "qty": 10, "mark": mark, "wide": wide}]}]}
    out = r.portfolio_risk(view, download=lambda *a, **kw: {}, now=NOW)
    assert out["unavailable_tickers"] == ["AAA"] and not out["complete"]
    assert out["totals"]["delta_dollars"] == 1000  # GOOD only: AAA is never counted as zero exposure
    line = r.summary_line(out)
    assert "excludes AAA" in line and "SPY -3%" in line
    assert next(row for row in out["rows"] if row["ticker"] == "AAA")["delta_dollars"] is None


def test_after_the_close_the_latest_prices_still_give_an_estimate():
    # Quotes are not "fresh" outside the regular session, but the closing price is the right input here.
    r._beta_cache.clear()
    out = r.portfolio_risk({"total_assets": 100000,
                            "stocks": [{"ticker": "AAA", "qty": 100, "price": 10, "price_fresh": False}]},
                           download=lambda *a, **kw: {}, now=NOW)
    assert out["complete"] and out["totals"]["delta_dollars"] == 1000
    assert r.summary_line(out).startswith("Risk (estimate): if SPY -3%")


from tests.test_broker_holdings import repo, store  # noqa: E402,F401


def test_stop_risk_is_the_loss_at_each_plan_stop_and_in_a_gap_through_it_as_percent_of_the_account(monkeypatch):
    from src.services.trade_desk import risk as rk
    from tests.test_broker_holdings import MIDDAY
    view = {"total_assets": 10000.0,
            "stocks": [{"key": "NVDA", "ticker": "NVDA", "qty": 5.0, "price": 200.0},
                       {"key": "SOXS", "ticker": "SOXS", "qty": 10.0, "price": 30.0},
                       {"key": "MU", "ticker": "MU", "qty": -4.0, "price": 100.0}],
            "options": [{"key": "USO 2026-10-16", "underlying": "USO", "underlying_price": 150.0, "expiry": "2026-10-16",
                         "legs": [{"qty": 3.0, "strike": 160.0, "right": "call", "mark": 3.4},
                                  {"qty": -3.0, "strike": 170.0, "right": "call", "mark": 1.6}]},
                        {"key": "XYZ 2026-10-16", "underlying": "XYZ", "underlying_price": 50.0, "expiry": "2026-10-16",
                         "legs": [{"qty": 1.0, "strike": 40.0, "right": "call", "mark": None}]}]}
    plan = lambda key, ticker, label, price, stop, exposure="long", weight=1.0: {  # noqa: E731
        "key": key, "ticker": ticker, "label": label, "price": price, "stop": stop, "exposure": exposure,
        "weight_pct": weight, "stop_source": "review", "stop_distance_pct": None}
    rows = [plan("NVDA", "NVDA", "NVDA shares", 200.0, 190.0), plan("SOXS", "SOXS", "SOXS shares", 30.0, 33.0),
            plan("MU", "MU", "MU short", 100.0, 105.0, "short"), plan("USO 2026-10-16", "USO", "USO spread", 150.0, 145.0),
            plan("XYZ 2026-10-16", "XYZ", "XYZ call", 50.0, 45.0), plan("OLD", "OLD", "gone", 1.0, 1.0)]
    rows.append({**plan("NVDA", "NVDA", "NVDA again", 200.0, None)})
    result = rk.stop_risk(view, rows, now=MIDDAY)
    by = {row["label"]: row for row in result["rows"]}
    assert (by["NVDA shares"]["at_stop_pct"], by["NVDA shares"]["at_gap_pct"]) == (-0.5, -1.0)  # 5 × −10, 5 × −20 of 10,000
    assert by["SOXS shares"]["past_stop"] and by["SOXS shares"]["at_stop_pct"] == 0.0  # selling now is the exit
    assert (by["MU short"]["at_stop_pct"], by["MU short"]["at_gap_pct"]) == (-0.2, -0.4)
    uso = by["USO spread"]
    years = rk._years_to("2026-10-16", MIDDAY)
    spread = lambda spot, ivs: sum(q * 100 * rk._bs(spot, k, years, iv, "call") for (q, k), iv in zip(((3, 160), (-3, 170)), ivs))  # noqa: E731
    ivs = [rk.implied_vol(3.4, 150, 160, years, "call"), rk.implied_vol(1.6, 150, 170, years, "call")]
    assert uso["at_stop_pct"] == round((spread(145, ivs) - spread(150, ivs)) / 100, 2) < 0
    assert by["XYZ call"]["unavailable"] == "option not priced" and by["NVDA again"]["unavailable"] == "no stop"
    assert "gone" not in by
    assert result["totals"]["at_stops_pct"] == round(-0.5 + 0 - 0.2 + uso["at_stop_pct"], 2)
    assert result["without_stop"] == ["NVDA again"] and result["unpriced"] == ["XYZ call"] and result["gap_multiple"] == 2.0
    assert rk.stop_risk(view, [], now=MIDDAY)["totals"] == {"at_stops_pct": None, "at_gaps_pct": None}


def test_the_holdings_stop_risk_uses_the_plans_and_view_of_the_holdings_page(store):  # noqa: F811
    from tests.test_broker_holdings import MIDDAY
    store.mutate_plans(lambda stored: {**stored, "plans": {"NVDA": {
        "key": "NVDA", "ticker": "NVDA", "type": "stock", "exposure": "long", "stop": 190.0, "stop_source": "review"}}})
    result = store.stop_risk(now=MIDDAY)
    nvda = next(row for row in result["rows"] if row["key"] == "NVDA")
    assert (nvda["at_stop_pct"], nvda["weight_pct"], nvda["stop_note"]) == (-0.5, 10.0, "review")
    assert set(result["without_stop"]) == {"SOXS shares", "USO 10/16 160/170C spread"}
