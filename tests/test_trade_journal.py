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


def test_an_option_that_expired_out_of_the_money_is_a_realized_loss():
    deals = [deal("QQQ260918C500000", "BUY", 1, 4.0, "2026-09-02 10:00:00"),
             deal("SPY260918P550000", "SELL_SHORT", 2, 3.0, "2026-09-01 10:00:00")]
    closes = {("QQQ", date(2026, 9, 18)): 490.0, ("SPY", date(2026, 9, 18)): 560.0}
    result = j.build(deals, [], TODAY, lambda ticker, day: closes.get((ticker, day)), current={})
    trips = {trip["ticker"]: trip for trip in result["best"] + result["worst"]}
    assert trips["QQQ"]["pnl"] == -400.0 and trips["QQQ"]["how"] == "expired worthless"  # the premium is lost
    assert trips["SPY"]["pnl"] == 600.0  # a short put that expired worthless keeps its premium
    assert result["unresolved"] == []


@pytest.mark.parametrize("underlying_close", [None, 510.0])
def test_in_the_money_or_unknown_expiries_require_reconciliation(underlying_close):
    deals = [deal("QQQ260918C500000", "BUY", 1, 4.0, "2026-09-02 10:00:00")]
    result = j.build(deals, [], TODAY, lambda ticker, day: underlying_close, current={})
    assert result["total"]["trades"] == 0 and result["total"]["total_pnl"] == 0
    assert result["total"]["win_rate"] is None
    assert result["unresolved"][0]["reason"] == "expiry_reconciliation"


def test_only_closed_option_quantities_count_as_realized():
    deals = [deal("QQQ260918C500000", "BUY", 2, 4, "2026-09-01 10:00:00"),
             deal("QQQ260918C500000", "SELL", 1, 5, "2026-09-10 10:00:00")]
    result = j.build(deals, [], TODAY, current={})
    assert result["total"]["trades"] == 1 and result["total"]["total_pnl"] == 100
    assert result["unresolved"][0]["qty"] == 1


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
    assert result["total"]["trades"] == 0
    assert result["unresolved"][0]["reason"] == "inventory_difference"
    # Shares delivered by an assignment (no fill) and sold afterwards: no fake short either.
    assigned = [deal("MU", "SELL", 100, 97.0, "2026-06-01 10:00:00")]
    assert j.build(assigned, [], TODAY, current={})["total"]["trades"] == 0
    # A short opened and covered in the window still counts.
    short = [deal("TSLA", "SELL_SHORT", 5, 300.0, "2026-09-03 10:00:00"), deal("TSLA", "BUY_BACK", 5, 280.0, "2026-09-04 10:00:00")]
    assert j.build(short, [], TODAY, current={})["by_type"][0]["label"] == "Short stock"


def test_a_later_assignment_does_not_take_the_place_of_an_earlier_known_purchase():
    # Jan buy, Feb sale, then a short put assigned in March (no fill) and those shares sold in June.
    deals = [deal("AAPL", "BUY", 100, 150.0, "2026-01-05 10:00:00"), deal("AAPL", "SELL", 100, 160.0, "2026-02-05 10:00:00"),
             deal("AAPL", "SELL", 100, 170.0, "2026-06-05 10:00:00")]
    result = j.build(deals, [], TODAY, current={})
    assert result["total"]["trades"] == 0  # the net difference cannot date an assignment
    assert result["unresolved"][0]["reason"] == "inventory_difference"


def test_fills_before_a_split_are_restated_in_todays_shares():
    deals = [deal("NVDA", "BUY", 10, 1000.0, "2026-05-01 10:00:00"), deal("NVDA", "SELL", 100, 110.0, "2026-07-01 10:00:00")]
    result = j.build(deals, [], TODAY, current={}, splits={"NVDA": [("2026-06-10", 10.0)]})
    assert result["total"]["trades"] == 1 and result["total"]["total_pnl"] == pytest.approx(1000.0)  # not -9,000
    assert result["unmatched_closes"] == 0


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


def test_unknown_opening_inventory_cannot_be_matched_after_new_purchases():
    deals = [deal("AAPL", "BUY", 100, 150, "2026-01-05 10:00:00"),
             deal("AAPL", "SELL", 100, 160, "2026-02-05 10:00:00")]
    result = j.build(deals, [], TODAY, current={"AAPL": 100})
    assert result["total"]["trades"] == 0  # FIFO would sell the unknown opening shares first
    assert result["unresolved"][0]["quantity_difference"] == 100
