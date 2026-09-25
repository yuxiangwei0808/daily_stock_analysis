"""Trade Desk Discord delivery: long messages go out in parts, and a retry sends only what failed."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
