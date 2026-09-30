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
TIMEOUT_SECONDS = 14 * 60  # Discord's reply token lasts 15 minutes from the command
MAX_REPLY = 1800
DISCORD_LIMIT = 2000
_ZERO_WIDTH = "​"
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
             clock: Callable[[], float] = time.monotonic, timeout: float = TIMEOUT_SECONDS,
             stopping: Optional[threading.Event] = None) -> Optional[dict]:
    """The finished job, or None after ``timeout`` seconds or once ``stopping`` is set."""
    deadline = clock() + timeout
    while clock() < deadline and not (stopping is not None and stopping.is_set()):
        job = service.repo.advice(advice_id)
        if job and job.get("status") in _DONE:
            return job
        if stopping is not None:
            stopping.wait(POLL_SECONDS)
        else:
            sleep(POLL_SECONDS)
    return None


def format_answer(job: Optional[dict], ticker: str, base_url: str = "") -> str:
    """The reply: verdict, shortened explanation, link; mentions defused, always within Discord's 2000 characters."""
    if job is None:
        return f"**{ticker}** · still working after {TIMEOUT_SECONDS // 60} minutes; the answer will be on the Trade Desk page."
    link = f"\n{base_url.rstrip('/')}/trade-desk?adviceId={job['id']}" if base_url else ""
    if job["status"] in ("failed", "cancelled"):
        return f"**{ticker}** · the question {job['status']}: {str(job.get('error') or '')[:300]}{link}"
    verdict = {"trade": "Trade", "wait": "Wait", "compare": "Compare"}.get(job.get("assessment") or "", "Answer")
    # Defused before measuring: the zero-width spaces count toward the limit.
    explanation = " ".join(str(job.get("explanation") or "No explanation was produced.").split()).replace("@", "@" + _ZERO_WIDTH)
    count = len(job.get("candidates") or [])
    stale = " · prices moved since; refresh them on the page" if job["status"] == "stale" else ""
    head = f"🧭 **{ticker}** · {verdict} · {count} candidate{'s' if count != 1 else ''}{stale}\n"
    room = min(MAX_REPLY, DISCORD_LIMIT - len(head) - len(link) - 2)
    if len(explanation) > room:
        explanation = explanation[:room].rsplit(" ", 1)[0] + " …"
    return f"{head}{explanation}{link}"


class DiscordAskBot:
    """Runs the discord.py client on its own thread and event loop."""

    def __init__(self, service: Any, token: str, allowed: Set[int]):
        self.service = service
        self._token = token
        self._allowed = allowed
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()  # ends pending waits so a shutdown is not held up

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="trade-desk-discord-ask", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        loop, client = self._loop, self._client
        if loop is None or client is None or loop.is_closed():
            return  # never logged in (bad token, no network) or already stopped
        try:
            asyncio.run_coroutine_threadsafe(client.close(), loop)
        except RuntimeError:  # the loop closed in between
            pass

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
            if interaction.user.id not in self._allowed:  # answered at once, only to the asker
                await interaction.response.send_message(
                    "You are not on this bot's list of people who can ask (TRADE_DESK_DISCORD_ASK_USERS).", ephemeral=True)
                return
            # Discord wants a first response within 3 seconds; the database write can take longer.
            await interaction.response.defer(thinking=True)
            try:
                job = await asyncio.to_thread(submit, self.service, interaction.user.id, ticker, question, self._allowed)
            except Exception as exc:  # a bad ticker, a full queue, a locked database
                reason = str(exc) if isinstance(exc, (ValueError, PermissionError)) else type(exc).__name__
                await interaction.followup.send(f"Could not ask: {reason}")
                return
            symbol = job["request"]["ticker"]
            # The reply token lasts 15 minutes from the command, not from now.
            elapsed = (discord.utils.utcnow() - interaction.created_at).total_seconds()
            finished = await asyncio.to_thread(wait_for, self.service, job["id"],
                                               timeout=max(0.0, TIMEOUT_SECONDS - elapsed), stopping=self._stopping)
            if self._stopping.is_set():
                return
            text = format_answer(finished, symbol, base_url)
            try:
                await interaction.followup.send(text)
            except discord.HTTPException:  # the reply token expired: post in the channel instead
                if interaction.channel is not None:
                    await interaction.channel.send(text)

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
