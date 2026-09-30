"""Ask Trade Desk from Discord: a ``/ask`` slash command on your own Discord bot.

The bot connects out to Discord (discord.py gateway), so the server needs no public URL. It
starts with the server when ``DISCORD_BOT_TOKEN`` and ``TRADE_DESK_DISCORD_ASK_USERS`` (the
Discord user ids allowed to ask; every question costs a model call) are both set.

``/ask ticker:NVDA question:covered call for next week?`` queues the same live question as the
Trade Desk page (source ``discord``) and replies when the answer is ready: the verdict, the
explanation (shortened) and a link to the full answer when ``TRADE_DESK_PUBLIC_URL`` is set.
Questions from Discord never use your broker position, so nothing about your holdings reaches
the channel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional, Set

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
TIMEOUT_SECONDS = 15 * 60
MAX_REPLY = 1800
_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
_DONE = {"completed", "stale", "failed", "cancelled"}


def allowed_users() -> Set[int]:
    return {int(part) for part in re.split(r"[,\s]+", os.getenv("TRADE_DESK_DISCORD_ASK_USERS", "")) if part.isdigit()}


def enabled() -> bool:
    return bool((os.getenv("DISCORD_BOT_TOKEN") or "").strip()) and bool(allowed_users())


def submit(service: Any, user_id: int, ticker: str, question: str, allowed: Set[int]) -> Any:
    """Queues the question; returns the job, or raises PermissionError / ValueError with a reply for the user."""
    if user_id not in allowed:
        raise PermissionError("You are not on this bot's list of people who can ask (TRADE_DESK_DISCORD_ASK_USERS).")
    symbol = str(ticker or "").strip().upper().lstrip("$")
    if not _TICKER.match(symbol):
        raise ValueError(f"{ticker!r} is not a US ticker.")
    from .models import TradeAdviceRequest
    request = TradeAdviceRequest(ticker=symbol, message=str(question or "").strip()[:1000], data_mode="live",
                                 use_holdings=False)
    return service.submit(request, source="discord")


def wait_for(service: Any, advice_id: str, *, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> Optional[dict]:
    """The finished job, or None after TIMEOUT_SECONDS."""
    deadline = clock() + TIMEOUT_SECONDS
    while clock() < deadline:
        job = service.repo.advice(advice_id)
        if job and job.get("status") in _DONE:
            return job
        sleep(POLL_SECONDS)
    return None


def format_answer(job: Optional[dict], ticker: str, base_url: str = "") -> str:
    """The reply: verdict, shortened explanation, link. Mentions are defused."""
    if job is None:
        return f"**{ticker}** · still working after {TIMEOUT_SECONDS // 60} minutes; the answer will be on the Trade Desk page."
    link = f"\n{base_url.rstrip('/')}/trade-desk?adviceId={job['id']}" if base_url else ""
    if job["status"] in ("failed", "cancelled"):
        return f"**{ticker}** · the question {job['status']}: {str(job.get('error') or '')[:300]}{link}"
    verdict = {"trade": "Trade", "wait": "Wait", "compare": "Compare"}.get(job.get("assessment") or "", "Answer")
    explanation = " ".join(str(job.get("explanation") or "No explanation was produced.").split())
    if len(explanation) > MAX_REPLY:
        explanation = explanation[:MAX_REPLY].rsplit(" ", 1)[0] + " …"
    count = len(job.get("candidates") or [])
    stale = " · prices moved since; refresh them on the page" if job["status"] == "stale" else ""
    text = f"🧭 **{ticker}** · {verdict} · {count} candidate{'s' if count != 1 else ''}{stale}\n{explanation}{link}"
    return text.replace("@", "@​")


class DiscordAskBot:
    """Runs the discord.py client on its own thread and event loop."""

    def __init__(self, service: Any, token: str, allowed: Set[int]):
        self.service = service
        self._token = token
        self._allowed = allowed
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="trade-desk-discord-ask", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None and self._client is not None:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def _run(self) -> None:
        import discord
        from discord import app_commands
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        client = discord.Client(intents=discord.Intents.default())
        tree = app_commands.CommandTree(client)
        self._client = client
        base_url = os.getenv("TRADE_DESK_PUBLIC_URL", "")

        @tree.command(name="ask", description="Ask Trade Desk about a US stock or its options")
        @app_commands.describe(ticker="US ticker, e.g. NVDA", question="What you want to know")
        async def ask(interaction: discord.Interaction, ticker: str, question: str = "") -> None:
            try:
                job = await asyncio.to_thread(submit, self.service, interaction.user.id, ticker, question, self._allowed)
            except (PermissionError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            symbol = job["request"]["ticker"]
            await interaction.response.send_message(f"Asked Trade Desk about **{symbol}**; answers take one to three minutes.")
            finished = await asyncio.to_thread(wait_for, self.service, job["id"])
            await interaction.followup.send(format_answer(finished, symbol, base_url))

        @client.event
        async def on_ready() -> None:
            for guild in client.guilds:  # guild commands appear at once; global ones can take an hour
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
            logger.info("Discord /ask ready in %d server(s)", len(client.guilds))

        try:
            self._loop.run_until_complete(client.start(self._token))
        except Exception as exc:  # a bad token or no network: Trade Desk works without it
            logger.warning("Discord /ask bot stopped: %s", type(exc).__name__)
        finally:
            self._loop.close()


def start(service: Any) -> Optional[DiscordAskBot]:
    """Starts the bot when configured; None otherwise."""
    if not enabled():
        return None
    bot = DiscordAskBot(service, os.getenv("DISCORD_BOT_TOKEN", "").strip(), allowed_users())
    bot.start()
    return bot
