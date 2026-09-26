"""Market-hours watch: deterministic move rules and news triage."""
from datetime import datetime, timedelta, timezone

from src.services.trade_desk.pulse import MarketPulse

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)  # 11:00 ET


class Provider:
    def __init__(self):
        self.quotes = {}

    def watchlist_quotes(self, tickers):
        return {ticker: quote for ticker, quote in self.quotes.items() if ticker in tickers}


def _pulse(provider, events, **kwargs):
    seen_keys = set()

    def emit(event_type, payload, key):
        if key in seen_keys:  # the repository deduplicates by key
            return None
        seen_keys.add(key)
        events.append((event_type, payload))
        return payload
    return MarketPulse(lambda: provider, emit, tickers=lambda: ["AAPL", "NVDA"], **kwargs)


def test_day_moves_alert_once_per_level_and_only_the_highest_crossed():
    provider, events = Provider(), []
    pulse = _pulse(provider, events)
    provider.quotes = {"AAPL": {"price": 105.5, "change_pct": 5.5}, "NVDA": {"price": 99.0, "change_pct": -1.0}}
    pulse.check_moves(NOW)
    assert [(e[1]["underlying"], e[1]["kind"]) for e in events] == [("AAPL", "day_move")]
    assert "past +5%" in events[0][1]["message"]  # a gap past 3% and 5% is one alert
    pulse.check_moves(NOW + timedelta(minutes=1))
    assert len(events) == 1
    provider.quotes["AAPL"] = {"price": 109, "change_pct": 9.0}
    pulse.check_moves(NOW + timedelta(minutes=2))
    day_moves = [e[1]["message"] for e in events if e[1]["kind"] == "day_move"]
    assert len(day_moves) == 2 and "past +8%" in day_moves[-1]


def test_the_watch_runs_only_in_the_regular_session():
    provider, events = Provider(), []
    provider.quotes = {"AAPL": {"price": 110, "change_pct": 10.0}}
    pulse = _pulse(provider, events, fetch_news=lambda ticker: [])
    pulse.tick(NOW, session="postmarket")
    assert events == []
    pulse.tick(NOW, session="regular")
    assert len(events) == 1
    pulse.stop()


def _headline(title, minutes_ago=5):
    return {"title": title, "source": "Reuters", "url": "https://x",
            "published_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def test_news_alerts_only_new_material_headlines_after_warm_up():
    provider, events, rated = Provider(), [], []
    feed = {"AAPL": [_headline("Apple old story")], "NVDA": []}

    def rate(items):
        rated.append([item["title"] for item in items])
        return {item["id"]: {"score": 3 if "acquire" in item["title"] else 1, "why": "M&A"} for item in items}
    pulse = _pulse(provider, events, fetch_news=lambda ticker: feed[ticker], rate_news=rate)
    pulse._names.update({"AAPL": "Apple Inc.", "NVDA": "NVIDIA Corporation"})
    pulse.news_sweep(NOW)
    assert events == [] and rated == []  # the first look only learns what is already out
    feed["AAPL"] = [_headline("Apple old story"), _headline("Apple to acquire a chip maker"),
                    _headline("Five stocks to watch"), _headline("Stale scoop", minutes_ago=300)]
    pulse.news_sweep(NOW)
    assert rated == [["Apple to acquire a chip maker"]]  # headlines not about the stock are dropped
    assert [e[1]["title"] for e in events] == ["Apple to acquire a chip maker"]
    # A later price move cites the latest headline.
    provider.quotes = {"AAPL": {"price": 104, "change_pct": 4.0}}
    pulse.check_moves(NOW)
    assert events[-1][1]["message"].endswith("Latest news: Apple old story (Reuters)")


def test_keyword_rule_covers_an_unavailable_model():
    provider, events = Provider(), []
    feed = {"AAPL": [], "NVDA": []}
    pulse = _pulse(provider, events, fetch_news=lambda ticker: feed[ticker], rate_news=lambda items: None)
    pulse._names.update({"NVDA": "NVIDIA Corporation"})
    pulse.news_sweep(NOW)
    feed["NVDA"] = [_headline("Nvidia raises guidance after record quarter"), _headline("Why I like Nvidia")]
    pulse.news_sweep(NOW)
    assert [e[1]["title"] for e in events] == ["Nvidia raises guidance after record quarter"]


def test_market_signal_ignores_volatility_indices():
    from src.market_analyzer import MarketIndex, _directional_index_changes
    indices = [MarketIndex(code="SPX", name="S&P 500", change_pct=-0.02),
               MarketIndex(code="DJI", name="Dow Jones Industrial Average", change_pct=-0.31),
               MarketIndex(code="VIX", name="CBOE Volatility Index (VIX)", change_pct=3.30)]
    assert _directional_index_changes(indices) == [-0.02, -0.31]  # VIX rises when stocks fall


def test_headlines_must_name_the_stock():
    from src.services.trade_desk.pulse import mentions
    assert not mentions("One disappointing White Sox prospect", "SOXS", "Direxion Daily Semiconductor Bear 3X")
    assert not mentions("Why Bernstein targets $3,000 for SanDisk (SNDK)", "MSFT", "Microsoft Corporation")
    assert mentions("Microsoft beats estimates", "MSFT", "Microsoft Corporation")


def test_a_fading_move_does_not_alert_again_at_lower_levels():
    provider, events = Provider(), []
    pulse = _pulse(provider, events)
    for minute, change in enumerate([9.0, 6.0, 4.0]):
        provider.quotes = {"AAPL": {"price": 100 + change, "change_pct": change}}
        pulse.check_moves(NOW + timedelta(minutes=minute * 20))
    day_moves = [e[1]["message"] for e in events if e[1]["kind"] == "day_move"]
    assert len(day_moves) == 1 and "past +8%" in day_moves[0]


def test_old_days_are_pruned():
    provider, events = Provider(), []
    pulse = _pulse(provider, events)
    pulse._level_high[("2026-09-01", "AAPL", "+")] = 5.0
    provider.quotes = {"AAPL": {"price": 100.0, "change_pct": 0.1}}
    pulse.check_moves(NOW)
    assert pulse._level_high == {}


def test_leveraged_funds_scale_the_levels():
    provider, events = Provider(), []
    pulse = _pulse(provider, events)
    provider.quotes = {"AAPL": {"price": 104, "change_pct": 4.0, "name": "Direxion Daily AAPL Bull 2X Shares"}}
    pulse.check_moves(NOW)
    assert events == []  # 4% on a 2x fund is below its scaled 6% level


def test_small_moves_alert_only_for_what_you_hold():
    provider, events = Provider(), []
    pulse = _pulse(provider, events, held=lambda: ["NVDA"])
    provider.quotes = {"AAPL": {"price": 103.5, "change_pct": 3.5}, "NVDA": {"price": 103.5, "change_pct": 3.5}}
    pulse.check_moves(NOW)
    assert [e[1]["underlying"] for e in events] == ["NVDA"]
    provider.quotes["AAPL"] = {"price": 105.5, "change_pct": 5.5}
    pulse.check_moves(NOW + timedelta(minutes=1))
    assert [e[1]["underlying"] for e in events] == ["NVDA", "AAPL"]


def test_shared_quotes_replace_the_pulses_own_poll():
    class Silent(Provider):
        def watchlist_quotes(self, tickers):
            raise AssertionError("the shared snapshot should be used")
    events = []
    pulse = _pulse(Silent(), events)
    pulse.tick(NOW, session="regular", quotes=None, shared=True)  # not a quote minute
    pulse.tick(NOW, session="regular", quotes={"AAPL": {"price": 106, "change_pct": 6.0}, "XYZ": {"price": 1}},
               shared=True)
    assert [e[1]["underlying"] for e in events] == ["AAPL"]  # only its own tickers
