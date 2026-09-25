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
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", lambda self, c: sent.append(c) or True)
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
    monkeypatch.setattr("src.notification.NotificationService.send_to_discord", lambda self, c: sent.append(c) or True)
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
