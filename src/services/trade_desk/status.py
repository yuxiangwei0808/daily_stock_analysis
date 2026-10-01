"""System status: one place to see that the server, quotes, scheduled reports and background jobs work.

``build(service)`` returns every component with a state (``ok``, ``warn``, ``error`` or ``off``
when not enabled) and a one-line detail; the web Status page shows it and the worker's watchdog
(``StatusWatch``) posts to Discord when a component stays in error, plus a short summary a
couple of minutes after each start.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parents[3]
ORDER = {"error": 0, "warn": 1, "ok": 2, "off": 3}
ICONS = {"ok": "🟢", "warn": "🟡", "error": "🔴", "off": "⚪"}


def code_version(root: Path = _ROOT) -> Optional[str]:
    """The checked-out commit (short), read from .git without running git; None outside a checkout."""
    try:
        git = root / ".git"
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head[:8]
        ref = head.split(" ", 1)[1].strip()
        if (git / ref).exists():
            return (git / ref).read_text().strip()[:8]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:8]
    except OSError:
        return None
    return None


def _utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()  # naive values are the server's local time


def _ago(value: Any, now: datetime) -> str:
    when = _utc(value)
    if when is None:
        return "never"
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


_probe_pool = None
_probe: Optional[Any] = None
PROBE_TIMEOUT = 5.0


def _opend(service: Any) -> Dict[str, Any]:
    """The OpenD check, bounded in time: the moomoo SDK retries a lost connection forever, so the
    probe runs on its own thread and a probe that has not answered in PROBE_TIMEOUT reads as failing
    (and is not started again while it is still stuck)."""
    global _probe_pool, _probe
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    if _probe_pool is None:
        _probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="status-opend")
    if _probe is None or _probe.done():
        _probe = _probe_pool.submit(lambda: service.health()["live"])
    try:
        live = _probe.result(timeout=PROBE_TIMEOUT)
    except FutureTimeout:
        return {"state": "error", "detail": "not answering (the check has waited over 5 seconds)"}
    except Exception as exc:
        return {"state": "error", "detail": type(exc).__name__}
    message = str(live.get("message") or ("connected" if live.get("available") else "unavailable"))
    if live.get("available") or live.get("status") == "ready":
        return {"state": "ok", "detail": message}
    if live.get("status") == "degraded" and live.get("connected"):
        # OpenD's global state carries no quote-rights fields, so a healthy connection reads "degraded";
        # a denial or a logged-out quote server would have made it "blocked". Logged in is working.
        if live.get("quote_logged_in") is True:
            return {"state": "ok", "detail": "connected and logged in to quotes (OpenD does not report quote rights)"}
        return {"state": "warn", "detail": message}  # worth a look, not an outage (the watchdog posts failures)
    return {"state": "error", "detail": message}


def _part(worker: Any, name: str, now: datetime) -> Dict[str, Any]:
    """State of one worker part from its last tick: an error since, or when it last ran cleanly."""
    status = (getattr(worker, "part_status", {}) or {}).get(name) or {}
    if status.get("error"):
        return {"state": "error", "detail": f"{status['error']} since {_ago(status.get('error_at'), now)}",
                "since": status.get("error_at")}
    return {"state": "ok", "detail": f"checked {_ago(status.get('ok_at'), now)}" if status.get("ok_at") else "starting"}


def build(service: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    worker = getattr(service, "worker", None)
    components: List[Dict[str, Any]] = []

    def add(key: str, label: str, state: str, detail: str, since: Optional[str] = None):
        components.append({"key": key, "label": label, "state": state, "detail": detail, "since": since})

    running_version, disk_version = getattr(service, "version", None), code_version()
    if running_version and disk_version and running_version != disk_version:
        add("version", "Code version", "warn", f"running {running_version}; {disk_version} is on disk — restart to load it")
    else:
        add("version", "Code version", "ok", f"{running_version or 'unknown'}, started {_ago(getattr(service, 'started_at', None), now)}")

    add("opend", "moomoo OpenD quotes", **_opend(service))

    scheduler = getattr(service, "scheduler_status", None)
    if scheduler is None:
        add("scheduler", "Scheduled reports", "off", "not managed by this server")
    else:
        try:
            info = scheduler()
            last_error, last_ok = info.get("last_error"), info.get("last_success_at")
            failing = bool(last_error) and (_utc(info.get("last_run_at")) or now) >= (_utc(last_ok) or datetime.min.replace(tzinfo=timezone.utc))
            parts = []
            if info.get("running"):
                parts.append("a run is in progress")
            if info.get("next_run_at"):
                parts.append(f"next {_utc(info['next_run_at']).astimezone(_NEW_YORK):%a %H:%M} NY")
            parts.append(f"last success {_ago(last_ok, now)}")
            state = "error" if failing else "ok"
            if failing:
                parts.append(f"last run failed: {str(last_error)[:120]}")
            else:
                # A run that sent its brief with stocks missing still "succeeded": say how many failed.
                counts = info.get("last_counts") or {}
                analyzed, requested = counts.get("analyzed", 0), counts.get("requested", 0)
                if requested and analyzed < requested:
                    parts.append(f"last run analyzed {analyzed} of {requested} stocks; the rest failed "
                                 "(see the scheduled log)")
                    state = "error" if analyzed * 2 < requested else "warn"
            add("scheduler", "Scheduled reports", "off" if not info.get("enabled") else state,
                "; ".join(parts) if info.get("enabled") else "scheduling is off")
        except Exception as exc:
            add("scheduler", "Scheduled reports", "error", type(exc).__name__)

    if worker is None:
        add("worker", "Alert monitor", "off", "not started")
    else:
        info = worker.status()
        if not getattr(service, "enabled", True):
            info = {**info, "running": None}
        tick_age = (now - _utc(info.get("last_tick"))).total_seconds() if _utc(info.get("last_tick")) else None
        if info.get("running") is None:
            add("worker", "Alert monitor", "off", "Trade Desk is disabled")
        elif not info.get("running"):
            add("worker", "Alert monitor", "error", "stopped")
        elif not info.get("leader"):
            add("worker", "Alert monitor", "warn", "another server holds the monitor lease")
        elif info.get("error"):
            add("worker", "Alert monitor", "error", f"last pass failed: {info['error']}")
        elif tick_age is not None and tick_age > 120:
            add("worker", "Alert monitor", "error", f"no pass for {int(tick_age // 60)} min")
        else:
            add("worker", "Alert monitor", "ok", f"last pass {_ago(info.get('last_tick'), now)}")
        add("plans", "Plan monitoring", **_part(worker, "plans", now))

        holdings = getattr(worker, "_holdings", None)
        if holdings is None:
            add("holdings", "Broker holdings", "off", "TRADE_DESK_BROKER_ACCOUNT not set")
        else:
            synced = (service.holdings.raw() or {}).get("synced_at")
            if holdings.errors.get("sync"):
                add("holdings", "Broker holdings", "error",
                    f"sync failing ({holdings.errors['sync']}) since {_ago(holdings.error_at.get('sync'), now)}; "
                    f"last good sync {_ago(synced, now)}", holdings.error_at.get("sync"))
            else:
                add("holdings", "Broker holdings", **{**_part(worker, "holdings", now),
                                                      "detail": f"synced {_ago(synced, now)}"})
        for key, label, attr in (("pulse", "Market pulse", "_pulse"), ("opportunities", "Trade opportunities", "_opportunities"),
                                 ("breakouts", "Breakout watch", "_breakouts")):
            part = getattr(worker, attr, None)
            if part is None:
                if key != "breakouts":
                    add(key, label, "off", "not enabled")
                continue
            state = _part(worker, key, now)
            background = getattr(part, "last_error", None)  # work the tick hands to a background thread
            if state["state"] == "ok" and background:
                state = {"state": "error", "detail": f"last background run failed ({background})"}
            add(key, label, **state)
        other = {name: error for name, error in ((holdings.errors if holdings is not None else {}) or {}).items()
                 if name != "sync"}
        if other:
            add("holdings_tasks", "Holdings background tasks", "warn",
                ", ".join(f"{name} failing ({error})" for name, error in sorted(other.items())))

        tracker = getattr(worker, "_tracker", None)
        if tracker is None:
            add("tracker", "Idea tracker", "off", "not enabled")
        else:
            error = getattr(tracker, "last_error", None)
            done = getattr(tracker, "_done_day", None)
            add("tracker", "Idea tracker", "warn" if error else "ok",
                (f"last run failed ({error}); retrying" if error else
                 f"settled {done.isoformat()}" if done else "settles after 16:30 NY on trading days"))

        youtube = getattr(worker, "_youtube", None)
        if youtube is None:
            add("youtube", "YouTube picks", "off", "YOUTUBE_CHANNELS not set")
        else:
            error = getattr(youtube, "last_error", None)
            detail = (f"last pass {_ago(youtube.last_pass_at, now)}, {youtube.last_pass_added} new picks"
                      if getattr(youtube, "last_pass_at", None) else "first pass running")
            if youtube.captions_paused():
                detail += "; YouTube is rate-limiting captions, waiting"
            add("youtube", "YouTube picks", "error" if error else "warn" if youtube.captions_paused() else "ok",
                f"last pass failed ({error})" if error else detail)

        social = getattr(worker, "_social", None)
        if social is None:
            add("social", "Social scan", "off", "SOCIAL_SCAN_ENABLED is off")
        else:
            error = getattr(social, "last_error", None)
            done = getattr(social, "_done_day", None)
            add("social", "Social scan", "warn" if error else "ok",
                f"last digest failed ({error}); retrying" if error else
                f"digest sent {done.isoformat()}" if done else "digest at 16:20 NY on trading days")

    add("discord", "Discord delivery", **_discord(service, now))
    worst = min((item["state"] for item in components), key=lambda state: ORDER[state], default="ok")
    return {"checked_at": now.isoformat(), "overall": "ok" if worst == "off" else worst, "components": components}


def _discord(service: Any, now: datetime) -> Dict[str, Any]:
    from src.config import get_config
    from . import discord_routes
    config = get_config()
    configured = bool(getattr(config, "discord_webhook_url", None) or discord_routes.webhooks() or (
        getattr(config, "discord_bot_token", None) and getattr(config, "discord_main_channel_id", None)))
    if not configured or not service.repo.preferences().get("discord_enabled"):
        return {"state": "off", "detail": "not configured" if not configured else "switched off in Trade Desk"}
    rows = service.repo.events(limit=500, newest=True, types=["discord_delivery"],
                               since=(now - timedelta(days=1)).isoformat())
    good = next((row for row in rows if row["payload"].get("success")), None)
    failed = {row["payload"].get("batch_of") or row["payload"].get("event_id") for row in rows
              if not row["payload"].get("success") and row["payload"].get("attempt", 0) >= 3}
    detail = f"last message {_ago(good['created_at'], now) if good else 'over a day ago'}"
    if failed:
        detail += f"; {len(failed)} gave up after 3 tries in the last day"
    newest = rows[0] if rows else None
    state = "error" if newest and not newest["payload"].get("success") and newest["payload"].get("attempt", 0) >= 3 else (
        "warn" if failed else "ok")
    return {"state": state, "detail": detail}


class StatusWatch:
    """Posts ``system_status`` to Discord: a summary after start, and a component that stays in error."""

    CHECK_SECONDS = 300
    STARTUP_DELAY = timedelta(minutes=2)

    def __init__(self, service: Any, emit: Callable[[str, Dict[str, Any], str], Any],
                 build_status: Callable[..., Dict[str, Any]] = build, clock: Optional[Callable[[], float]] = None):
        import time
        self.service = service
        self._emit = emit
        self._build = build_status
        self._clock = clock or time.monotonic
        self._next = 0.0
        self._failing: Dict[str, int] = {}
        self._reported_day: Dict[str, str] = {}  # component -> the day its failure was last posted
        self._reported_start = False

    def tick(self, now: datetime) -> None:
        started = _utc(getattr(self.service, "started_at", None)) or now
        if self._clock() < self._next or now - started < self.STARTUP_DELAY:
            return
        self._next = self._clock() + self.CHECK_SECONDS
        status = self._build(self.service, now)
        if not self._reported_start:
            problems = [item for item in status["components"] if item["state"] in ("error", "warn")]
            lines = [f"{'✅' if not problems else '⚠️'} **Server started** · {getattr(self.service, 'version', None) or 'unknown version'}"
                     + (" · all systems working" if not problems else f" · {len(problems)} need attention")]
            lines += [f"{ICONS[item['state']]} {item['label']}: {item['detail']}" for item in problems]
            self._emit("system_status", {"underlying": "", "message": "\n".join(lines)},
                       f"status-start:{getattr(self.service, 'started_at', '')}")
            self._reported_start = True  # only once stored: a locked database retries at the next check
        day = now.astimezone(_NEW_YORK).date().isoformat()
        for item in status["components"]:
            if item["state"] != "error":
                self._failing.pop(item["key"], None)
                continue
            self._failing[item["key"]] = self._failing.get(item["key"], 0) + 1
            # Still failing five minutes later (not a blip); posted again each day it lasts.
            if self._failing[item["key"]] >= 2 and self._reported_day.get(item["key"]) != day:
                self._emit("system_status", {"underlying": "", "message": f"🔴 **{item['label']}** · {item['detail']}"},
                           f"status:{item['key']}:{day}")
                self._reported_day[item["key"]] = day
