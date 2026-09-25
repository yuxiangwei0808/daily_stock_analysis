"""Swing trade opportunities: trend rules, model review merge, delivery and live breakouts."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.trade_desk import opportunities as opp
from src.services.trade_desk import earnings, trend

NY_MIDDAY = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)  # 12:30 New York


def _bars(n=80, start=100.0, step=0.5, volume=1_000_000, last_volume=None, jump=0.0):
    rows, price = [], start
    for i in range(n):
        price += step
        close = price + (jump if i == n - 1 else 0)
        rows.append({"date": (date(2026, 5, 1) + timedelta(days=i)).isoformat(), "open": close - 0.2,
                     "high": close + 0.5, "low": close - 0.5, "close": close,
                     "volume": last_volume if (i == n - 1 and last_volume) else volume})
    return rows


def test_trend_rules_find_a_strong_breakout_in_either_direction():
    up = trend.score_bars(_bars(last_volume=2_000_000, jump=2))
    assert up["direction"] == "long" and up["strength"] >= trend.STRONG
    assert up["stop"] < up["price"] < up["targets"][0] < up["targets"][1]
    assert any("broke above 20-day high" in note for note in up["notes"])
    down = trend.score_bars(_bars(start=200, step=-0.5, last_volume=2_000_000, jump=-2))
    assert down["direction"] == "short" and down["strength"] >= trend.STRONG
    assert down["targets"][1] < down["targets"][0] < down["price"] < down["stop"]


def test_trend_rules_skip_short_history_illiquid_and_penny_stocks():
    assert trend.score_bars(_bars(n=40)) is None
    assert trend.score_bars(_bars(volume=10_000)) is None  # about $1.5M a day
    assert trend.score_bars(_bars(start=1, step=0.01, volume=50_000_000)) is None  # under $5


def test_partial_day_volume_is_scaled_by_the_intraday_profile():
    assert trend.volume_fraction(datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)) == 0.13  # 10:00 NY
    assert trend.volume_fraction(datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc)) == 1.0  # after the close
    full = trend.score_bars(_bars(last_volume=400_000), partial_fraction=1.0)
    early = trend.score_bars(_bars(last_volume=400_000), partial_fraction=0.2)
    assert early["volume_ratio"] == 2.0 and full["volume_ratio"] == 0.4


def _setup(direction="long", strength=80, price=100.0):
    sign = 1 if direction == "long" else -1
    return {"direction": direction, "strength": strength, "notes": [], "extended": False, "trigger": price,
            "stop": price - sign * 3, "targets": [price + sign * 6, price + sign * 9], "price": price, "ma20": 98,
            "ma50": 95, "ma20_slope_pct": 1, "ma50_slope_pct": 1, "atr": 2, "atr_pct": 2, "high20": price,
            "low20": price - 10, "momentum20_pct": 8, "volume_ratio": 1.6, "avg_volume": 1e6, "dollar_volume_m": 100}


def test_candidates_are_strong_trends_plus_watchlist_names_their_report_backs():
    scores = {"AAA": _setup(strength=90), "BBB": _setup(strength=60), "CCC": _setup("short", 58),
              "DDD": _setup(strength=50), "SPY": _setup(strength=95), "EEE": _setup(strength=72)}
    reports = {"BBB": {"score": 80}, "CCC": {"score": 20}, "DDD": {"score": 90}}
    picked = opp.select_candidates(scores, ["BBB", "CCC", "DDD", "EEE"], reports, 10)
    assert [item["ticker"] for item in picked] == ["AAA", "EEE", "BBB", "CCC"]
    assert picked[1]["source"] == "watchlist" and picked[0]["source"] == "scan"


def test_review_keeps_confident_ideas_in_the_rule_direction_and_sane_levels():
    candidates = [{"ticker": t, "source": "scan", **_setup(d)} for t, d in
                  (("AAA", "long"), ("BBB", "long"), ("CCC", "short"), ("DDD", "long"))]
    review = [{"ticker": "AAA", "direction": "long", "conviction": "medium", "stop": 105, "targets": [110],
               "thesis": "Clean  breakout."},
              {"ticker": "BBB", "direction": "short", "conviction": "high"},  # flipped: rejected
              {"ticker": "CCC", "direction": "short", "conviction": "high", "stop": 104, "targets": [90, 85]},
              {"ticker": "DDD", "direction": "long", "conviction": "low"}]
    ideas = opp.merge_review(candidates, review)
    assert [(i["ticker"], i["conviction"]) for i in ideas] == [("CCC", "high"), ("AAA", "medium")]
    assert ideas[1]["stop"] == 97.0 and ideas[1]["targets"] == [110.0]  # stop above price fell back
    assert ideas[1]["thesis"] == "Clean breakout."


def test_ways_to_act_are_labelled_and_options_need_high_conviction():
    long_medium = {"ticker": "AAA", "direction": "long", "conviction": "medium"}
    assert opp.expressions(long_medium, options_follow=False) == ["buy shares"]
    short_high = {"ticker": "SPY", "direction": "short", "conviction": "high"}
    ways = opp.expressions(short_high, options_follow=True)
    assert ways[0] == "sell or trim if you hold it" and "loss unbounded" in ways[1]
    assert ways[2] == "inverse ETF SH" and ways[3].startswith("puts / put debit spread")
    assert "options comparison follows" in ways[3]


def _row(i, code, score, created):
    return SimpleNamespace(id=i, code=code, sentiment_score=score, operation_advice="Buy",
                           analysis_summary=f"{code} summary", created_at=created)


class FakeService:
    def __init__(self):
        self.rows, self.jobs, self.submitted, self.settings = [], {}, [], {}
        self.repo = SimpleNamespace(db=SimpleNamespace(get_analysis_history=lambda days, limit: list(self.rows)),
                                    advice=lambda job_id: self.jobs.get(job_id),
                                    setting=lambda key, default=None: self.settings.get(key, default),
                                    set_setting=lambda key, value: self.settings.__setitem__(key, value))

    def submit(self, request, source):
        job = {"id": f"job-{request.ticker}", "status": "running", "request": {"ticker": request.ticker}}
        self.jobs[job["id"]] = job
        self.submitted.append((request.ticker, request.direction, request.horizon, source))
        return job


def _runner(service, sent, clock, review, earnings_date=lambda ticker, day: None):
    strong = _bars(last_volume=2_000_000, jump=2)
    return opp.OpportunityRunner(
        service, lambda t, p, k: sent.append((t, p, k)) or {"id": len(sent)}, watchlist=lambda: ["NVDA", "JPM"],
        now=lambda: clock["now"],
        universe=lambda: ["AAPL", "XOM"], bars=lambda tickers: {t: strong for t in tickers},
        news=lambda ticker: [{"published_at": "2026-09-25T12:00:00+00:00", "title": f"{ticker} wins", "source": "X"}],
        review=review, earnings_date=earnings_date,
        schedule_times=lambda: [opp._naive(NY_MIDDAY - timedelta(minutes=20)).strftime("%H:%M")])


def _run_batch(runner, service, clock, first_id, regular=True):
    local = opp._naive(clock["now"])
    service.rows += [_row(first_id + i, code, 70, local - timedelta(minutes=5))
                     for i, code in enumerate(["NVDA", "JPM", "AAPL", "MSFT", "AMD"])]
    clock["now"] += timedelta(minutes=1)
    runner.tick(regular)
    runner._scan.result(timeout=10)
    clock["now"] += timedelta(minutes=1)
    runner.tick(regular)


def test_a_finished_run_sends_opportunities_then_options_for_high_conviction():
    clock, service, sent, prompts = {"now": NY_MIDDAY}, FakeService(), [], []

    def review(prompt):
        prompts.append(prompt)
        return [{"ticker": "NVDA", "direction": "long", "conviction": "high", "entry": "buy 140-141",
                 "stop": 135, "targets": [150, 156], "horizon": "1-3 weeks", "thesis": "Breakout on demand."},
                {"ticker": "AAPL", "direction": "long", "conviction": "medium", "thesis": "Steady trend."}]

    service.rows.append(_row(1, "OLD", 90, opp._naive(clock["now"]) - timedelta(hours=3)))
    runner = _runner(service, sent, clock, review)
    runner.tick(True)  # the first look records where the history stands; older runs are not re-announced
    _run_batch(runner, service, clock, 10)
    [(event_type, payload, key)] = sent
    assert event_type == "trade_opportunities" and key.startswith("opportunities:2026-09-25 ")
    message = payload["message"]
    assert message.startswith("🎯 **Trade opportunities**")
    assert "🟢 **NVDA** · LONG · high conviction" in message and "🟢 **AAPL** · LONG · medium conviction" in message
    assert "calls / call debit spread (defined risk) (options comparison follows)" in message
    assert "NVDA wins" in prompts[0] and "NVDA summary" in prompts[0]
    assert service.submitted == [("NVDA", "bullish", "swing", "opportunity")]
    service.jobs["job-NVDA"].update(status="completed", assessment="wait", explanation="Spreads too wide.")
    clock["now"] += timedelta(minutes=1)
    runner.tick(True)
    assert sent[-1][0] == "options_ideas" and sent[-1][1]["message"] == "**NVDA** (long · high) — wait\n> Spreads too wide."

    # The next run finds the same ideas: nothing new, no message and no options rerun.
    _run_batch(runner, service, clock, 30)
    assert len(sent) == 2 and len(service.submitted) == 1


def test_after_the_close_ideas_are_sent_without_options_and_rejections_stay_quiet():
    clock, service, sent = {"now": NY_MIDDAY}, FakeService(), []
    runner = _runner(service, sent, clock, lambda prompt: [
        {"ticker": "NVDA", "direction": "long", "conviction": "high"}])
    runner.tick(False)
    _run_batch(runner, service, clock, 10, regular=False)
    assert sent[0][0] == "trade_opportunities" and "options comparison follows" not in sent[0][1]["message"]
    assert service.submitted == []

    clock2, service2, sent2 = {"now": NY_MIDDAY}, FakeService(), []
    quiet = _runner(service2, sent2, clock2, lambda prompt: [{"ticker": "NVDA", "direction": "none"}])
    quiet.tick(True)
    _run_batch(quiet, service2, clock2, 10)
    assert sent2 == []


class FakeProvider:
    def __init__(self, quotes):
        self.quotes = quotes

    def watchlist_quotes(self, codes):
        return {code: self.quotes[code] for code in codes if code in self.quotes}


def test_breakouts_alert_once_on_volume_and_cap_scan_names():
    history = _bars(n=80, step=0.5)  # dates end before 2026-09-25; high20 is about 140.5
    quotes = {"NVDA": {"price": 150.0, "volume": 900_000, "change_pct": 3.0},   # pace 2.0x at 12:30
              "JPM": {"price": 150.0, "volume": 300_000, "change_pct": 1.0}}     # pace ~0.7x: no alert
    provider = FakeProvider(quotes)
    sent, tick = [], {"clock": 0.0}
    watch = opp.BreakoutWatch(lambda: provider, lambda t, p, k: sent.append((t, p, k)) or {"id": 1},
                              watchlist=lambda: ["NVDA", "JPM"], bars=lambda tickers: {t: history for t in tickers},
                              clock=lambda: tick["clock"], earnings_date=lambda ticker, day: None)
    watch.tick(NY_MIDDAY, "regular")  # loads the day's levels in the background
    watch._loading.result(timeout=10)
    watch.tick(NY_MIDDAY, "regular")
    [(event_type, payload, key)] = sent
    assert event_type == "breakout" and payload["kind"] == "breakout" and key == "breakout:2026-09-25:NVDA:up"
    assert "broke above its 20-day high" in payload["message"] and "2.0x normal volume pace" in payload["message"]
    tick["clock"] += opp.QUOTE_SECONDS
    watch.tick(NY_MIDDAY + timedelta(minutes=1), "regular")
    assert len(sent) == 1  # once per day

    scan_names = {f"S{i}": history for i in range(opp.SCAN_BREAKOUTS_PER_DAY + 2)}
    for name in scan_names:
        quotes[name] = {"price": 150.0, "volume": 900_000, "change_pct": 2.0}
    watch.add(scan_names, date(2026, 9, 25))
    tick["clock"] += opp.QUOTE_SECONDS
    watch.tick(NY_MIDDAY + timedelta(minutes=2), "regular")
    assert len(sent) == 1 + opp.SCAN_BREAKOUTS_PER_DAY


def test_breakout_levels_exclude_the_current_session():
    rows = _bars(n=60) + [{"date": "2026-09-25", "open": 1, "high": 999, "low": 1, "close": 500, "volume": 1}]
    levels = opp.breakout_levels(rows, date(2026, 9, 25))
    assert levels["high20"] < 200 and levels["avg_volume"] == 1_000_000


def test_credit_and_unbounded_options_are_described_plainly():
    job = {"request": {"ticker": "SPY"}, "status": "completed", "assessment": "compare", "explanation": "",
           "candidates": [{"strategy": "short_put", "legs": [{"side": "sell", "right": "put", "strike": 700,
                                                              "expiry": "2026-10-16"}],
                           "payoff": {"entry_debit": -250, "max_loss": None, "max_gain": 250, "breakevens": [697.5]}}]}
    lines = opp.format_options(job, "short · high")
    assert lines[0] == "**SPY** (short · high) — short_put: sell 700P 10/16"
    assert lines[1] == "   credit $250.00 · max loss unbounded · max gain $250.00 · breakeven 697.5"


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TRADE_OPPORTUNITIES_ENABLED", raising=False)
    assert not opp.enabled()
    monkeypatch.setenv("TRADE_OPPORTUNITIES_MAX", "abc")
    assert opp.max_ideas() == 5


def test_a_restart_still_processes_the_last_hour_but_never_sends_twice():
    clock, service, sent = {"now": NY_MIDDAY}, FakeService(), []
    local = opp._naive(clock["now"])
    service.rows += [_row(i, code, 70, local - timedelta(minutes=20))
                     for i, code in enumerate(["NVDA", "JPM", "AAPL", "MSFT", "AMD"], start=1)]
    keys = set()

    def emit(event_type, payload, key):
        if key in keys:
            return None  # the repository's dedup key
        keys.add(key)
        sent.append((event_type, payload, key))
        return {"id": len(sent)}

    review = lambda prompt: [{"ticker": "NVDA", "direction": "long", "conviction": "high"}]  # noqa: E731
    runner = _runner(service, [], clock, review)
    runner.emit = emit
    runner.tick(True)
    runner._scan.result(timeout=10)
    clock["now"] += timedelta(minutes=1)
    runner.tick(True)
    # A restart with the saved state skips the published run entirely.
    restarted = _runner(service, [], clock, review)
    restarted.emit = emit
    clock["now"] += timedelta(minutes=1)
    restarted.tick(True)
    assert restarted._scan is None
    # Without saved state (a crash before it was written) the run is scanned again,
    # but the scheduled slot's dedup key stops a second message.
    service.settings.clear()
    again = _runner(service, [], clock, review)
    again.emit = emit
    again.tick(True)
    again._scan.result(timeout=10)
    clock["now"] += timedelta(minutes=1)
    again.tick(True)
    assert len(sent) == 1 and sent[0][2].startswith("opportunities:2026-09-25 ") and len(service.submitted) == 1


def test_leveraged_and_inverse_funds_are_never_suggested_as_shorts_or_options():
    assert opp.geared_fund("Direxion Daily Semiconductor Bear 3X Shares")
    assert opp.geared_fund("ProShares UltraPro Short QQQ") and opp.geared_fund("Direxion Daily TSLA Bull 2X Shares")
    assert not opp.geared_fund("NVIDIA Corporation") and not opp.geared_fund("SPDR S&P 500 ETF Trust")
    assert not opp.geared_fund("Vanguard Long-Term Treasury ETF") and not opp.geared_fund("iShares Short Term Bond ETF")
    assert opp.geared_fund("ProShares Short S&P500")
    assert opp.geared_fund("Direxion Daily Semiconductor Be")  # report names are cut to ~31 characters
    assert not opp.geared_fund("iShares Short Treasury Bond ETF")
    assert not opp.geared_fund("PIMCO Enhanced Short Maturity Active ETF")
    idea = {"ticker": "SOXS", "name": "Direxion Daily Semiconductor Bear 3X Shares", "direction": "short",
            "conviction": "high"}
    assert opp.expressions(idea, options_follow=False) == [
        "sell or trim if you hold it (leveraged/inverse ETF: shorting it is not advised)"]

    history = _bars(n=80, start=200, step=-0.5)
    quotes = {"SOXS": {"price": 150.0, "volume": 900_000, "change_pct": -4.0,
                       "name": "Direxion Daily Semiconductor Bear 3X Shares"}}
    sent = []
    watch = opp.BreakoutWatch(lambda: FakeProvider(quotes), lambda t, p, k: sent.append(p) or {"id": 1},
                              watchlist=lambda: ["SOXS"], bars=lambda tickers: {t: history for t in tickers},
                              clock=lambda: 0.0, earnings_date=lambda ticker, day: None)
    watch.tick(NY_MIDDAY, "regular")
    watch._loading.result(timeout=10)
    watch.tick(NY_MIDDAY, "regular")
    [payload] = sent
    assert payload["kind"] == "breakdown" and "targets" not in payload["message"]
    assert "shorting it is not advised" in payload["message"] and "short shares" not in payload["message"]


def test_earnings_notes_warn_inside_the_hold_and_mention_the_month_ahead():
    today = date(2026, 9, 25)
    assert earnings.note(date(2026, 10, 2), today).startswith("⚠️ Earnings Oct 2 (in 7 days) — inside the hold")
    assert earnings.note(date(2026, 9, 26), today).startswith("⚠️ Earnings Sep 26 (tomorrow)")
    assert earnings.note(date(2026, 10, 20), today) == "Earnings Oct 20 (in 25 days)"
    assert earnings.note(date(2026, 12, 1), today) == "" and earnings.note(None, today) == ""


def test_earnings_lookup_uses_the_earliest_upcoming_date_and_caches(monkeypatch):
    import yfinance as yf
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            calls.append(symbol)
            self.calendar = {"Earnings Date": [date(2026, 10, 30), date(2026, 10, 28)]}

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    earnings._cache.clear()
    assert earnings.next_earnings("BRK.B", date(2026, 9, 25)) == date(2026, 10, 28)
    assert earnings.next_earnings("BRK.B", date(2026, 9, 25)) == date(2026, 10, 28)
    assert calls == ["BRK-B"]
    monkeypatch.setattr(yf, "Ticker", lambda symbol: SimpleNamespace(calendar={}))  # funds publish none
    assert earnings.next_earnings("SPY", date(2026, 9, 25)) is None


def test_earnings_inside_the_hold_cap_conviction_and_skip_options():
    clock, service, sent, prompts = {"now": NY_MIDDAY}, FakeService(), [], []
    dates = {"NVDA": date(2026, 10, 1), "AAPL": date(2026, 10, 20)}

    def review(prompt):
        prompts.append(prompt)
        return [{"ticker": t, "direction": "long", "conviction": "high"} for t in ("NVDA", "AAPL")]

    runner = _runner(service, sent, clock, review, earnings_date=lambda ticker, day: dates.get(ticker))
    runner.tick(True)
    _run_batch(runner, service, clock, 10)
    message = sent[0][1]["message"]
    assert '"next_earnings": "2026-10-01 (in 6 days)"' in prompts[0]
    assert '"next_earnings": "none published"' in prompts[0]
    assert "🟢 **NVDA** · LONG · medium conviction" in message  # capped from high
    assert "⚠️ Earnings Oct 1 (in 6 days) — inside the hold" in message
    assert "🟢 **AAPL** · LONG · high conviction" in message and "Earnings Oct 20 (in 25 days)" in message
    assert [ticker for ticker, *_ in service.submitted] == ["AAPL"]


def test_breakout_alerts_carry_the_earnings_warning():
    history = _bars(n=80, step=0.5)
    quotes = {"NVDA": {"price": 150.0, "volume": 900_000, "change_pct": 3.0, "name": "NVIDIA Corporation"}}
    sent = []
    watch = opp.BreakoutWatch(lambda: FakeProvider(quotes), lambda t, p, k: sent.append(p) or {"id": 1},
                              watchlist=lambda: ["NVDA"], bars=lambda tickers: {t: history for t in tickers},
                              clock=lambda: 0.0, earnings_date=lambda ticker, day: date(2026, 9, 29))
    watch.tick(NY_MIDDAY, "regular")
    watch._loading.result(timeout=10)
    watch.tick(NY_MIDDAY, "regular")
    assert "⚠️ Earnings Sep 29 (in 4 days) — inside the hold" in sent[0]["message"]


def test_one_off_analyses_and_the_market_row_do_not_start_a_scan():
    clock, service, sent = {"now": NY_MIDDAY}, FakeService(), []
    runner = _runner(service, sent, clock, lambda prompt: [])
    runner._schedule_times = lambda: ["09:40"]  # nothing scheduled near midday
    runner.tick(True)
    _row_time = opp._naive(clock["now"]) - timedelta(minutes=5)
    service.rows += [_row(10 + i, code, 70, _row_time) for i, code in enumerate(["NVDA", "JPM", "AAPL", "MSFT", "AMD"])]
    clock["now"] += timedelta(minutes=1)
    runner.tick(True)
    assert runner._scan is None and sent == []
    runner._schedule_times = lambda: [opp._naive(NY_MIDDAY - timedelta(minutes=20)).strftime("%H:%M")]
    service.rows += [_row(20 + i, "MARKET", 50, _row_time) for i in range(5)]
    clock["now"] += timedelta(minutes=1)
    runner.tick(True)
    assert runner._scan is None  # five market-review rows are not a stock batch


def test_published_state_survives_a_restart():
    clock, service, sent = {"now": NY_MIDDAY}, FakeService(), []
    review = lambda prompt: [{"ticker": "AAPL", "direction": "long", "conviction": "medium"}]  # noqa: E731
    runner = _runner(service, sent, clock, review)
    runner.tick(True)
    _run_batch(runner, service, clock, 10)
    assert len(sent) == 1 and service.settings["opportunity_state"]["announced"]["AAPL"] == "long:medium"
    restarted = _runner(service, sent, clock, review)
    clock["now"] += timedelta(minutes=1)
    restarted.tick(True)
    assert restarted._scan is None and restarted._announced == {"AAPL": "long:medium"}


def _watch(history, quotes, sent, watchlist=("NVDA",)):
    return opp.BreakoutWatch(lambda: FakeProvider(quotes), lambda t, p, k: sent.append(p) or {"id": len(sent)},
                             watchlist=lambda: list(watchlist), bars=lambda tickers: {t: history for t in tickers},
                             clock=lambda: 0.0, earnings_date=lambda ticker, day: None)


def test_breakouts_need_a_real_margin_and_reuse_the_ideas_levels():
    history = _bars(n=80, step=0.5)  # high20 about 140.5, ATR about 1
    levels = opp.breakout_levels(history, date(2026, 9, 25))
    quotes = {"NVDA": {"price": levels["high20"] + 0.05, "volume": 900_000, "change_pct": 1.0}}
    sent = []
    watch = _watch(history, quotes, sent)
    watch.tick(NY_MIDDAY, "regular")
    watch._loading.result(timeout=10)
    watch.tick(NY_MIDDAY, "regular")
    assert sent == []  # a few cents above the high is not a breakout
    quotes["NVDA"]["price"] = 150.0
    watch._context = {"NVDA": {"direction": "long", "stop": 138.0, "targets": [160.0, 170.0], "text": "Trend review: long"}}
    watch._next = 0.0
    watch.tick(NY_MIDDAY + timedelta(minutes=1), "regular")
    [payload] = sent
    assert "Idea levels: stop 138.00 · targets 160.00 / 170.00" in payload["message"]
    assert "Trend review: long" in payload["message"]


def test_a_breakout_against_the_review_says_so_and_notes_reset_daily():
    history = _bars(n=80, start=200, step=-0.5)
    sent = []
    watch = _watch(history, {"NVDA": {"price": 150.0, "volume": 900_000, "change_pct": -3.0}}, sent)
    watch.tick(NY_MIDDAY, "regular")
    watch._loading.result(timeout=10)
    watch._context = {"NVDA": {"direction": "long", "stop": 1.0, "targets": [2.0], "text": "Trend review: long"}}
    watch.tick(NY_MIDDAY, "regular")
    assert "Today's trend review was long; this move goes against it." in sent[0]["message"]
    assert "Idea levels" not in sent[0]["message"]
    watch.tick(NY_MIDDAY + timedelta(days=3), "regular")  # the next session
    assert watch._context == {}


def test_a_failed_level_load_is_retried():
    calls = {"n": 0}

    def bars(tickers):
        calls["n"] += 1
        return {} if calls["n"] == 1 else {t: _bars(n=80, step=0.5) for t in tickers}

    tick = {"clock": 0.0}
    watch = opp.BreakoutWatch(lambda: FakeProvider({}), lambda *a: None, watchlist=lambda: ["NVDA"], bars=bars,
                              clock=lambda: tick["clock"], earnings_date=lambda ticker, day: None)
    watch.tick(NY_MIDDAY, "regular")
    watch._loading.exception(timeout=10)
    watch.tick(NY_MIDDAY, "regular")
    assert watch._levels == {} and watch._retry_at == opp.LEVELS_RETRY_SECONDS
    tick["clock"] = opp.LEVELS_RETRY_SECONDS + 1
    watch.tick(NY_MIDDAY, "regular")
    watch._loading.result(timeout=10)
    watch.tick(NY_MIDDAY, "regular")
    assert "NVDA" in watch._levels
