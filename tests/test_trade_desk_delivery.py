"""Trade Desk Discord delivery: long messages go out in parts, and a retry sends only what failed."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.notification  # noqa: F401  (imported before get_config is stubbed, so nothing binds the stub)
from src.services.trade_desk import worker as worker_module
from src.services.trade_desk.repository import TradeDeskRepository


@pytest.fixture
def repo():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(engine)

    @contextmanager
    def transaction():
        with sessions() as session:
            with session.begin():
                yield session

    yield TradeDeskRepository(SimpleNamespace(_engine=engine, get_session=sessions, session_scope=transaction))
    engine.dispose()


def test_parts_split_on_paragraphs_and_stay_under_the_limit():
    ideas = [f"🟢 **T{i}** · LONG\n" + "detail " * 60 for i in range(8)]
    text = "\n\n".join(["🎯 **Trade opportunities**", *ideas])
    parts = worker_module.discord_parts(text)
    assert len(parts) > 1 and all(len(part) <= worker_module.DISCORD_PART_LIMIT for part in parts)
    assert "\n\n".join(parts) == text
    assert all(part.startswith(("🎯", "🟢")) for part in parts)  # no idea is cut in half


def test_a_failed_part_is_retried_alone(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    repo.set_preferences({"discord_enabled": True})
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url="https://example.invalid"))
    sent, fail_once = [], {"armed": True}

    def send(self, content):
        if "PART-B" in content and fail_once["armed"]:
            fail_once["armed"] = False
            return False
        sent.append(content)
        return True

    monkeypatch.setattr("src.notification.NotificationService.__init__", lambda self: None)
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", send)
    service = SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda mode: None)
    desk = worker_module.TradeDeskWorker(service)
    message = "PART-A " + "a" * 1500 + "\n\n" + "PART-B " + "b" * 1500
    repo.event("trade_opportunities", {"underlying": "", "message": message}, "opportunities:1")
    desk._deliver()
    assert [part[:6] for part in sent] == ["PART-A"]
    later = worker_module.utcnow() + timedelta(seconds=61)
    monkeypatch.setattr(worker_module, "utcnow", lambda: later)
    desk._deliver()
    assert [part[:6] for part in sent] == ["PART-A", "PART-B"]  # part A was not posted twice
    desk._deliver()
    assert len(sent) == 2


def test_a_crash_after_the_claim_does_not_resend_delivered_parts(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    repo.set_preferences({"discord_enabled": True})
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url="https://example.invalid"))
    sent = []
    monkeypatch.setattr("src.notification.NotificationService.__init__", lambda self: None)
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", lambda self, c, **kw: sent.append(c) or True)
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda m: None))
    message = "PART-A " + "a" * 1500 + "\n\n" + "PART-B " + "b" * 1500
    event = repo.event("trade_opportunities", {"underlying": "", "message": message}, "opportunities:2")
    repo.event("discord_attempt", {"event_id": event["id"], "attempt": 1, "success": False}, "discord-attempt:x:1")
    repo.event("discord_delivery", {"event_id": event["id"], "attempt": 1, "success": False, "sent_parts": [0], "parts": 2})
    repo.event("discord_attempt", {"event_id": event["id"], "attempt": 2, "success": False}, "discord-attempt:x:2")
    later = worker_module.utcnow() + timedelta(seconds=61)
    monkeypatch.setattr(worker_module, "utcnow", lambda: later)
    desk._deliver()  # the newest record is a claim from a crashed attempt
    assert [part[:6] for part in sent] == ["PART-B"]


def test_events_filter_by_time_and_type_in_sql(repo):
    repo.event("breakout", {"underlying": "AAA"}, "b1")
    repo.event("market_move", {"underlying": "BBB"}, "m1")
    assert [e["event_type"] for e in repo.events(limit=10, types=["breakout"])] == ["breakout"]
    assert repo.events(limit=10, since="2999-01-01") == []


def test_mentions_in_messages_cannot_ping(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    repo.set_preferences({"discord_enabled": True})
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url="https://example.invalid"))
    sent = []
    monkeypatch.setattr("src.notification.NotificationService.__init__", lambda self: None)
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", lambda self, c, **kw: sent.append(c) or True)
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda m: None))
    repo.event("market_news", {"underlying": "AAA", "message": "@everyone look"}, "n1")
    desk._deliver()
    assert "@everyone" not in sent[0] and "@​everyone" in sent[0]


def test_one_unquotable_code_does_not_blank_the_batch():
    from src.services.trade_desk.providers import MoomooProvider, ProviderError

    class Stub(MoomooProvider):
        def __init__(self):
            pass

        def _call(self, method, codes):
            if "US.BAD" in codes:
                raise ProviderError("provider_error", "unknown code")
            return [{"code": code} for code in codes]

    assert [row["code"] for row in Stub()._snapshot_rows(["US.A", "US.BAD", "US.B", "US.C"])] == ["US.A", "US.B", "US.C"]


def test_one_shared_snapshot_serves_every_live_watch(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    provider = SimpleNamespace(watchlist_quotes=lambda codes: calls.append(codes) or {code: {"price": 1} for code in codes})
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None,
                                                         provider=lambda mode: provider))
    desk._pulse = SimpleNamespace(_tickers=lambda: ["AAA", "BBB"])
    desk._breakouts = SimpleNamespace(tickers=lambda: ["BBB", "CCC"])
    desk._holdings = SimpleNamespace(codes=lambda: ["USO261016C160000", "USO"])
    quotes = desk._shared_quotes("regular")
    assert calls == [["AAA", "BBB", "CCC", "USO", "USO261016C160000"]] and set(quotes) == set(calls[0])
    assert desk._shared_quotes("regular") is None  # once a minute
    assert desk._shared_quotes("postmarket") is None and len(calls) == 1


def test_a_failed_shared_snapshot_retries_stocks_and_options_apart(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)

    def quotes(codes):
        if any(code.startswith("USO2") for code in codes) and len(codes) > 1:
            raise RuntimeError("quota")
        if any(code.startswith("USO2") for code in codes):
            raise RuntimeError("no option rights")
        return {code: {"price": 1} for code in codes}
    provider = SimpleNamespace(watchlist_quotes=quotes)
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None,
                                                         provider=lambda mode: provider))
    desk._holdings = SimpleNamespace(codes=lambda: ["USO261016C160000", "USO", "NVDA"])
    assert set(desk._shared_quotes("regular")) == {"USO", "NVDA"}  # stocks survive the option failure


def test_pulse_treats_never_synced_holdings_as_unknown(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    holdings = SimpleNamespace(raw=lambda: {}, tickers=lambda: [])
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=holdings,
                                                         provider=lambda mode: None))
    with pytest.raises(LookupError):
        desk._held_tickers()  # the pulse then applies every level to every name


def test_a_slow_discord_does_not_hold_up_the_monitor(repo, monkeypatch):
    import threading
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda m: None))
    release, passes = threading.Event(), []
    desk._deliver = lambda: passes.append(1) or release.wait(5)
    desk._deliver_in_background()
    desk._deliver_in_background()  # a pass is still sending: no second one
    assert desk._delivery.is_alive() and len(passes) == 1
    release.set()
    desk._delivery.join(5)
    desk._deliver_in_background()
    desk._delivery.join(5)
    assert len(passes) == 2


def test_the_social_digest_goes_out_with_its_own_header(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    repo.set_preferences({"discord_enabled": True})
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url="https://example.invalid"))
    sent = []
    monkeypatch.setattr("src.notification.NotificationService.__init__", lambda self: None)
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", lambda self, c, **kw: sent.append(c) or True)
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda m: None))
    repo.event("social_digest", {"underlying": "", "message": "📣 **Social scan** · Sep 29"}, "social-digest:2026-09-29")
    desk._deliver()
    assert sent == ["📣 **Social scan** · Sep 29"]


def test_the_worker_starts_the_scans_only_when_configured(repo, monkeypatch):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    service = SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda m: None)
    desk = worker_module.TradeDeskWorker(service)
    assert desk._social is None and desk._youtube is None and desk._tracker is None
    monkeypatch.setenv("SOCIAL_SCAN_ENABLED", "true")
    monkeypatch.setenv("YOUTUBE_CHANNELS", "Meet Kevin=UCUvvj5lwue7PspotMDjk5UA")
    desk = worker_module.TradeDeskWorker(service)
    assert desk._social is not None and desk._youtube is not None
    assert desk._tracker is not None  # their records settle even with trade opportunities off
    for part in (desk._social, desk._youtube, desk._tracker):
        part.stop()


def _delivery_desk(repo, monkeypatch, sent, **env):
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    repo.set_preferences({"discord_enabled": True})
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url="https://example.invalid/main"))
    monkeypatch.setattr("src.notification.NotificationService.__init__", lambda self: None)
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", lambda self, c, **kw: sent.append(("main", c)) or True)
    monkeypatch.setattr("src.notification_sender.discord_sender.DiscordSender.send_to_discord",
                        lambda self, c, **kw: sent.append((self._discord_config["webhook_url"], c)) or True)
    return worker_module.TradeDeskWorker(SimpleNamespace(repo=repo, enabled=True, holdings=None, provider=lambda m: None))


def test_categories_go_to_their_own_channels_and_can_be_switched_off(repo, monkeypatch):
    sent = []
    desk = _delivery_desk(repo, monkeypatch, sent,
                          TRADE_DESK_DISCORD_WEBHOOKS="holdings=https://discord.test/h, ideas=https://discord.test/i,bogus=x")
    repo.event("holding_alert", {"underlying": "NVDA", "kind": "loss", "message": "Down 40% on cost"}, "h1")
    repo.event("breakout", {"underlying": "MU", "kind": "breakout", "message": "MU broke out"}, "b1")
    repo.event("social_digest", {"underlying": "", "message": "📣 digest"}, "d1")
    desk._deliver()
    assert [target for target, _ in sent] == ["https://discord.test/h", "https://discord.test/i", "main"]
    sent.clear()
    repo.set_preferences({"discord_categories": {"ideas": False}})
    repo.event("breakout", {"underlying": "AMD", "kind": "breakout", "message": "AMD broke out"}, "b2")
    repo.event("holding_alert", {"underlying": "NVDA", "kind": "profit", "message": "Up 50%"}, "h2")
    desk._deliver()
    assert [target for target, _ in sent] == ["https://discord.test/h"]  # ideas are off


def test_moves_on_names_you_do_not_hold_are_batched(repo, monkeypatch):
    sent = []
    desk = _delivery_desk(repo, monkeypatch, sent)
    repo.event("market_move", {"underlying": "NVDA", "change_pct": 4.2, "held": True, "message": "NVDA up 4.2% today"}, "m1")
    repo.event("market_move", {"underlying": "AMD", "change_pct": -5.1, "held": False, "message": "AMD down 5.1% today"}, "m2")
    repo.event("market_news", {"underlying": "TSLA", "held": False, "message": "TSLA: recall (Reuters)\nhttps://x"}, "n1")
    repo.event("market_move", {"underlying": "PLTR", "change_pct": 3.0, "held": None, "message": "PLTR up 3.0% today"}, "m3")
    desk._deliver()
    assert [c.split("\n")[1] for _, c in sent] == ["NVDA up 4.2% today", "PLTR up 3.0% today"]  # held or unknown: at once
    sent.clear()
    later = worker_module.utcnow() + timedelta(minutes=16)
    monkeypatch.setattr(worker_module, "utcnow", lambda: later)
    desk._deliver()
    [(_, batch)] = sent
    assert batch.startswith("📊 **Market moves & news** · names you don't hold")
    assert "📉 AMD down 5.1% today" in batch and "📰 TSLA: recall (Reuters)" in batch and "https://x" not in batch
    desk._deliver()
    assert len(sent) == 1  # sent once


def test_a_big_batch_goes_out_as_whole_messages_and_the_rest_waits(repo, monkeypatch):
    sent = []
    desk = _delivery_desk(repo, monkeypatch, sent)
    for i in range(40):
        repo.event("market_news", {"underlying": f"T{i}", "held": False, "message": f"T{i}: " + "headline words " * 12}, f"n{i}")
    later = worker_module.utcnow() + timedelta(minutes=16)
    monkeypatch.setattr(worker_module, "utcnow", lambda: later)
    desk._deliver()
    assert len(sent) == 1 and len(sent[0][1]) <= worker_module.DISCORD_PART_LIMIT
    first_batch = sent[0][1].count("📰")
    desk._deliver()  # the rest goes in the next message; nothing is repeated
    assert len(sent) == 2 and first_batch + sent[1][1].count("📰") <= 40
    assert not set(sent[0][1].split("\n")[1:]) & set(sent[1][1].split("\n")[1:])


def test_a_category_with_nowhere_to_go_is_skipped(repo, monkeypatch):
    sent = []
    desk = _delivery_desk(repo, monkeypatch, sent, TRADE_DESK_DISCORD_WEBHOOKS="holdings=https://discord.test/h")
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url=None))
    repo.event("breakout", {"underlying": "MU", "kind": "breakout", "message": "MU broke out"}, "b1")
    repo.event("holding_alert", {"underlying": "NVDA", "kind": "loss", "message": "Down 40%"}, "h1")
    desk._deliver()
    assert [target for target, _ in sent] == ["https://discord.test/h"]
    assert not [e for e in repo.events() if e["event_type"] == "discord_attempt" and e["payload"]["event_id"] != 2]
