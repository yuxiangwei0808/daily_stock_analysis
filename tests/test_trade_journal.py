"""Trade journal from fills: FIFO round trips, expired options, and grouping by type, hold and signal."""
from datetime import date

import pytest

from src.services.trade_desk import journal as j

TODAY = date(2026, 9, 30)


def deal(code, side, qty, price, time, deal_id=None):
    return {"deal_id": deal_id or f"{code}-{time}-{side}", "code": f"US.{code}", "side": side, "qty": qty,
            "price": price, "time": time}


def test_fills_match_first_in_first_out_into_round_trips():
    deals = [deal("NVDA", "BUY", 10, 100.0, "2026-09-01 10:00:00"), deal("NVDA", "BUY", 10, 110.0, "2026-09-02 10:00:00"),
             deal("NVDA", "SELL", 15, 120.0, "2026-09-10 10:00:00"),
             deal("TSLA", "SELL_SHORT", 5, 300.0, "2026-09-03 10:00:00"), deal("TSLA", "BUY_BACK", 5, 280.0, "2026-09-03 15:00:00")]
    trips = j.round_trips(deals, TODAY)
    nvda = [trip for trip in trips if trip["ticker"] == "NVDA"]
    assert [(t["qty"], t["entry"], t["pnl"]) for t in nvda] == [(10, 100.0, 200.0), (5, 110.0, 50.0)]
    assert nvda[0]["hold_days"] == 9 and nvda[0]["view"] == "long"
    [tsla] = [trip for trip in trips if trip["ticker"] == "TSLA"]
    assert tsla["position"] == "short" and tsla["pnl"] == 100.0 and tsla["hold_days"] == 0 and tsla["view"] == "short"


def test_options_use_the_multiplier_and_expired_ones_settle_at_intrinsic():
    deals = [deal("SPY260918P550000", "SELL_SHORT", 2, 3.0, "2026-09-01 10:00:00"),  # short puts, expired worthless
             deal("QQQ260918C500000", "BUY", 1, 4.0, "2026-09-02 10:00:00"),  # long call, expired in the money
             deal("AMD261016C200000", "BUY", 1, 5.0, "2026-09-05 10:00:00")]  # still open: not counted
    closes = {("SPY", date(2026, 9, 18)): 600.0, ("QQQ", date(2026, 9, 18)): 510.0}
    trips = {t["ticker"]: t for t in j.round_trips(deals, TODAY, lambda ticker, day: closes.get((ticker, day)))}
    assert set(trips) == {"SPY", "QQQ"}
    assert trips["SPY"]["pnl"] == 600.0 and trips["SPY"]["view"] == "long" and trips["SPY"]["how"] == "expired"
    assert trips["QQQ"]["pnl"] == pytest.approx((10.0 - 4.0) * 100) and trips["QQQ"]["return_pct"] == 150.0
    unknown = j.round_trips(deals[1:2], TODAY, lambda ticker, day: None)
    assert unknown[0]["pnl"] == -400.0 and unknown[0]["how"] == "expired, assumed worthless"


def test_the_journal_groups_by_type_hold_and_whether_a_signal_agreed():
    deals = [deal("NVDA", "BUY", 10, 100.0, "2026-09-01 10:00:00"), deal("NVDA", "SELL", 10, 90.0, "2026-09-02 10:00:00"),
             deal("MU", "BUY", 10, 100.0, "2026-09-05 10:00:00"), deal("MU", "SELL", 10, 130.0, "2026-09-25 10:00:00"),
             deal("AMD261016P150000", "BUY", 1, 2.0, "2026-09-08 10:00:00"), deal("AMD261016P150000", "SELL", 1, 3.0, "2026-09-08 14:00:00")]
    signals = [{"ticker": "MU", "signal_day": "2026-09-03", "direction": "long", "kind": "idea",
                "created_at": "2026-09-03T15:00:00+00:00"},
               {"ticker": "AMD", "signal_day": "2026-09-07", "direction": "long", "kind": "verdict"},  # long puts go against it
               {"ticker": "NVDA", "signal_day": "2026-08-01", "direction": "long", "kind": "idea"}]  # too old to count
    result = j.build(deals, signals, TODAY)
    assert result["total"] == {"trades": 3, "total_pnl": 300.0, "win_rate": pytest.approx(66.7), "avg_pnl": pytest.approx(100.0),
                               "avg_return_pct": pytest.approx((-10 + 30 + 50) / 3, abs=0.01), "avg_hold_days": pytest.approx(7.0)}
    signal = {row["key"]: row for row in result["by_signal"]}
    assert signal["agreed"]["trades"] == 1 and signal["agreed"]["total_pnl"] == 300.0
    assert signal["against"]["trades"] == 1 and signal["none"]["trades"] == 1
    hold = {row["key"]: row["trades"] for row in result["by_hold"]}
    assert hold == {"same_day": 1, "days_1_5": 1, "days_6_20": 1, "over_20": 0}
    assert [row["label"] for row in result["by_type"]] == ["Long stock", "Long puts"]
    assert result["best"][0]["ticker"] == "MU" and result["worst"][0]["ticker"] == "NVDA"


def test_sales_of_shares_held_before_the_history_close_them_instead_of_opening_shorts():
    # The reviewer's case: 100 AAPL bought in 2024 and sold in January, then a new round trip.
    deals = [deal("AAPL", "SELL", 100, 200.0, "2026-01-10 10:00:00"), deal("AAPL", "BUY", 100, 180.0, "2026-03-02 10:00:00"),
             deal("AAPL", "SELL", 100, 190.0, "2026-05-01 10:00:00")]
    result = j.build(deals, [], TODAY, current={})  # nothing held today
    assert result["total"]["trades"] == 1 and result["total"]["total_pnl"] == 1000.0
    assert result["by_type"][0]["label"] == "Long stock" and result["unmatched_closes"] == 1
    # Shares delivered by an assignment (no fill) and sold afterwards: no fake short either.
    assigned = [deal("MU", "SELL", 100, 97.0, "2026-06-01 10:00:00")]
    assert j.build(assigned, [], TODAY, current={})["total"]["trades"] == 0
    # A short opened and covered in the window still counts.
    short = [deal("TSLA", "SELL_SHORT", 5, 300.0, "2026-09-03 10:00:00"), deal("TSLA", "BUY_BACK", 5, 280.0, "2026-09-04 10:00:00")]
    assert j.build(short, [], TODAY, current={})["by_type"][0]["label"] == "Short stock"


def test_a_signal_counts_only_if_it_existed_before_the_trade():
    trip = {"ticker": "MU", "opened": "2026-09-10T10:05:00", "view": "long"}
    after_close = {"ticker": "MU", "signal_day": "2026-09-10", "direction": "long", "kind": "social",
                   "created_at": "2026-09-10T20:25:00+00:00"}
    morning = {**after_close, "created_at": "2026-09-10T13:45:00+00:00"}  # 09:45 New York
    same_day_report = {"ticker": "MU", "signal_day": "2026-09-10", "direction": "long", "kind": "verdict",
                       "created_at": "2026-09-11T20:30:00+00:00"}
    assert j.signal_match(trip, [after_close]) == "none"
    assert j.signal_match(trip, [morning]) == "agreed"
    assert j.signal_match(trip, [same_day_report]) == "none"
    assert j.signal_match(trip, [{**same_day_report, "signal_day": "2026-09-09"}]) == "agreed"
