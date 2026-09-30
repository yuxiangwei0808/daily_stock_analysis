"""NX and CD recomputed from the user's moomoo scripts, and the backtest that uses them."""
from src.services.trade_desk.backtest import indicators as ib
from src.services.trade_desk import moomoo_indicators as mi


def _bars(prices):
    return [{"date": f"2026-01-{i:03d}", "open": c, "high": c * 1.005, "low": c * 0.995, "close": c}
            for i, c in enumerate(prices)]


def _divergence_path():
    """Sharp fall, small bounce, slower lower low (weaker MACD), recovery."""
    p, prices = 100.0, [100.0] * 40
    for factor, days in ((0.97, 15), (1.02, 8), (0.99, 20), (1.012, 25)):
        for _ in range(days):
            p *= factor
            prices.append(p)
    return prices


def test_mylang_primitives():
    assert mi.ema([10, 20], 3) == [10.0, 15.0]  # seeded with the first value, then (2x + (n-1)y) / (n+1)
    assert mi.ref([1, 2, 3, 4], 1) == [None, 1, 2, 3]
    assert mi.ref([1, 2, 3, 4], [0, 1, 1, 3]) == [1, 1, 2, 1]
    assert mi.barslast([False, True, False, False, True]) == [None, 0.0, 1.0, 2.0, 0.0]
    assert mi.llv([5, 3, 4, 1], [1, 2, 2, 3]) == [5, 3, 3, 1]
    assert mi.hhv([5, 3, 4, 1], [None, 2, 1, 4]) == [None, 5, 4, 5]
    assert mi.count([True, False, True, True], 2) == [1, 1, 1, 2]


def test_nx_is_ema_of_highs_and_lows():
    bars = [{"high": 11.0, "low": 9.0, "close": 10.0}, {"high": 13.0, "low": 10.0, "close": 12.0}]
    tunnel = mi.nx(bars, n1=3, n2=5)
    assert tunnel["A"] == [11.0, 12.0] and tunnel["B"] == [9.0, 9.5]
    assert tunnel["A1"][1] == (2 * 13 + 4 * 11) / 6


def test_cd_marks_bullish_and_bearish_divergence():
    prices = _divergence_path()
    cd = mi.cd(_bars(prices))
    assert [i for i, flag in enumerate(cd["buy"]) if flag] == [83]  # first day of the recovery
    assert not any(cd["sell"])
    mirrored = mi.cd(_bars([10000 / p for p in prices]))  # higher high on weaker momentum
    assert any(mirrored["sell"]) and not any(mirrored["buy"])
    flat = mi.cd(_bars([100.0 + i * 0.1 for i in range(200)]))
    assert not any(flat["buy"]) and not any(flat["sell"])


def test_backtest_trades_enter_next_open_and_use_the_rule_exit():
    prices = _divergence_path()
    bars = _bars(prices)
    ind = ib.prepare(bars)
    assert ("cd_buy", "long") in ib.signals(ind, 83)
    trade = ib.trade(bars, ind, 83, "cd_buy", "long", "hold10", "X")
    assert trade.entry == bars[84]["open"] and trade.days == 10 and trade.exit == bars[93]["close"]
    rule = ib.trade(bars, ind, 83, "cd_buy", "long", "rule", "X")
    assert rule.reason in {"end", "time"}  # no 卖出 mark follows in this path


def test_run_pairs_every_trade_with_twins():
    bars = _bars(_divergence_path())
    other = _bars([100.0 + i * 0.2 for i in range(len(bars))])
    trades, dated, same = ib.run({"X": bars, "Y": other}, member=lambda ticker, day: True,
                                 start_date="2026-01-000", end_date="2026-01-999", warmup=50, draws=2)
    x_trades = [t for t in trades if t.ticker == "X"]
    assert {t.plan for t in x_trades} >= {"cd_buy/atr", "cd_buy/hold10", "cd_buy/rule"}
    x_dated = [t for t in dated if t.plan.startswith("date:cd_buy")]
    # Same-date twins: the other stock, entered on the signal's date with the same exit.
    assert x_dated and all(t.ticker == "Y" and t.signal_date == bars[83]["date"] for t in x_dated)
    assert all(t.plan.startswith("random:") for t in same) and len(same) == 2 * len(trades)


def test_same_date_twins_show_no_edge_on_random_walks():
    from src.services.trade_desk.backtest import swing as bt
    walks = bt.random_walk_bars(60, 700, seed=3)
    first = walks[next(iter(walks))][0]["date"]
    trades, dated, _ = ib.run(walks, member=lambda ticker, day: True, start_date=first, end_date="9999")
    for key in ("cd_buy/hold10", "nx_long/hold10"):
        a = [t.return_pct for t in trades if t.plan == key]
        b = [t.return_pct for t in dated if t.plan == "date:" + key]
        assert a and b and abs(sum(a) / len(a) - sum(b) / len(b)) < 0.6
