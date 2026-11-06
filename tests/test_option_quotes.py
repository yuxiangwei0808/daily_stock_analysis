"""Reading the recorder's files back (backtest/option_quotes.py)."""
from pathlib import Path

from src.services.trade_desk import option_recorder as rec
from src.services.trade_desk.backtest import option_quotes as oq


def test_files_written_by_the_recorder_read_back_with_fills_at_the_quote(tmp_path):
    files = rec._DayFiles(Path(tmp_path), __import__("datetime").date(2026, 11, 9))
    rows = []
    for k, (spot, bid, ask) in enumerate([(735.0, 1.0, 1.1), (736.2, 1.5, 1.6), (737.0, 2.0, 2.1)]):
        ts = f"{1794000000 + 15 * k:.3f}"
        rows.append([ts, "US.SPY", "S", "", "", "", "", "", "", repr(spot), "1000", "", "", "", "", "", "", "t"])
        for strike, right in ((736.0, "C"), (735.0, "C"), (735.0, "P")):
            rows.append([ts, f"US.SPY-{right}{strike:g}", right, repr(strike), "2026-11-09", repr(bid), repr(ask), "10", "12",
                         repr(bid), "5", "100", "20.5", "0.5", "", "", "", "t"])
    files.write("SPY", rows[:4])
    files.write("SPY", rows[4:])  # a second member, as after a restart
    assert oq.recorded(tmp_path) == {"SPY": ["2026-11-09"]}
    day = oq.load_day(tmp_path, "2026-11-09", "SPY")
    calls = day["US.SPY-C736"]
    assert [snap["ts"] for snap in calls] == [1794000000.0, 1794000015.0, 1794000030.0] and calls[0]["gamma"] is None
    assert oq.quote_at(calls, 1794000001, "buy") == 1.6 and oq.quote_at(calls, 1794000030, "sell") == 2.0
    assert oq.quote_at(calls, 1794000031, "sell") is None
    assert oq.nearest_contract(day, 1794000016, "C") == "US.SPY-C736"  # the stock was 736.2 then
    assert oq.nearest_contract(day, 1794000001, "P") == "US.SPY-P735"
    assert oq.nearest_contract(day, 1793999000, "C") is None
