"""Ask Trade Desk from Discord: who may ask, what is queued, and the reply."""
from types import SimpleNamespace

import pytest

from src.services.trade_desk import discord_ask as da


def test_only_listed_users_can_ask_and_holdings_are_never_used(monkeypatch):
    submitted = []
    service = SimpleNamespace(submit=lambda request, source: submitted.append((request, source)) or {"id": "a1"})
    with pytest.raises(PermissionError):
        da.submit(service, 99, "NVDA", "calls?", {42})
    with pytest.raises(ValueError):
        da.submit(service, 42, "not a ticker", "", {42})
    assert da.submit(service, 42, "$nvda", " covered call next week? ", {42}) == {"id": "a1"}
    [(request, source)] = submitted
    assert request.ticker == "NVDA" and request.message == "covered call next week?" and request.data_mode == "live"
    assert request.use_holdings is False and source == "discord"


def test_the_bot_starts_only_with_a_token_and_a_user_list(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TRADE_DESK_DISCORD_ASK_USERS", "123, 456")
    assert not da.enabled() and da.allowed_users() == {123, 456}
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("TRADE_DESK_DISCORD_ASK_USERS", "")
    assert not da.enabled() and da.start(SimpleNamespace()) is None


def test_waiting_ends_when_the_answer_is_done_or_times_out():
    states = iter([{"status": "queued"}, {"status": "running"}, {"id": "a1", "status": "completed"}])
    service = SimpleNamespace(repo=SimpleNamespace(advice=lambda advice_id: next(states)))
    slept = []
    assert da.wait_for(service, "a1", sleep=slept.append, clock=lambda: 0.0)["status"] == "completed"
    assert slept == [da.POLL_SECONDS, da.POLL_SECONDS]
    clock = iter([0.0, 0.0, da.TIMEOUT_SECONDS + 1])
    busy = SimpleNamespace(repo=SimpleNamespace(advice=lambda advice_id: {"status": "running"}))
    assert da.wait_for(busy, "a1", sleep=lambda s: None, clock=lambda: next(clock)) is None


def test_the_reply_carries_the_verdict_a_short_explanation_and_a_link():
    job = {"id": "a1", "status": "completed", "assessment": "trade", "explanation": "Buy the 150 call @everyone " + "x " * 2000,
           "candidates": [{}, {}]}
    text = da.format_answer(job, "NVDA", "https://desk.example/")
    assert text.startswith("🧭 **NVDA** · Trade · 2 candidates\nBuy the 150 call @\u200beveryone")
    assert text.endswith("\nhttps://desk.example/trade-desk?adviceId=a1") and len(text) < 2000
    stale = da.format_answer({**job, "status": "stale", "explanation": "ok"}, "NVDA")
    assert "prices moved since" in stale and "adviceId" not in stale
    assert "failed" in da.format_answer({"id": "a1", "status": "failed", "error": "quotes unavailable"}, "NVDA")
    assert "still working" in da.format_answer(None, "NVDA")


def test_a_reply_never_exceeds_discords_limit_even_with_many_mentions():
    job = {"id": "a1", "status": "stale", "assessment": "wait", "explanation": "@ " * 3000, "candidates": []}
    text = da.format_answer(job, "NVDA", "https://" + "x" * 150 + ".example")
    assert len(text) <= da.DISCORD_LIMIT and "@everyone" not in text


def test_stopping_ends_a_wait_and_a_bot_that_never_logged_in_stops_cleanly():
    import asyncio
    import threading
    stopping = threading.Event()
    stopping.set()
    busy = SimpleNamespace(repo=SimpleNamespace(advice=lambda advice_id: {"status": "running"}))
    assert da.wait_for(busy, "a1", stopping=stopping) is None
    bot = da.DiscordAskBot(SimpleNamespace(), "token", {1})
    bot._loop = asyncio.new_event_loop()
    bot._client = SimpleNamespace(close=lambda: None)
    bot._loop.close()  # the login failed and the loop was closed
    bot.stop()  # no RuntimeError
