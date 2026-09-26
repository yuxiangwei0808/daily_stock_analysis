"""Independent quote monitoring and persisted, selective Discord delivery."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import TradeAdviceRequest, identity, utcnow

logger = logging.getLogger(__name__)


_EVENT_LABELS = {"price_trigger": "Price trigger", "invalidation": "Invalidated",
                 "target": "Target reached", "time_exit": "Time to exit", "data_outage": "Data outage",
                 "position_reconciliation": "Reconcile position", "monitor_capacity": "Monitor capacity"}


DISCORD_PART_LIMIT = 1900
SHARED_QUOTE_SECONDS = 60


def discord_parts(content, limit=DISCORD_PART_LIMIT):
    """Split at blank lines first (an idea stays whole), then at lines, then hard."""
    pieces = []  # (separator before the piece, text)
    for paragraph in content.split("\n\n"):
        if len(paragraph) <= limit:
            pieces.append(("\n\n", paragraph))
            continue
        for number, line in enumerate(paragraph.split("\n")):
            for start in range(0, max(len(line), 1), limit):
                pieces.append(("\n\n" if number == 0 and start == 0 else "\n", line[start:start + limit]))
    parts, current = [], ""
    for separator, text in pieces:
        candidate = f"{current}{separator}{text}" if current else text
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = text
    if current:
        parts.append(current)
    return parts


class TradeDeskWorker:
    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self.owner = identity()
        self._stop = threading.Event()
        self._thread = None
        self._last_tick = None
        self._error = None
        self._leader = False
        self._unmonitored_count = 0
        self._next_quotes = 0.0
        from . import holdings, opportunities, pulse
        # Broker holdings join the watchlist for every live watch and add a
        # "You hold" line to ideas and breakouts.
        self._holdings = (holdings.HoldingsMonitor(service.holdings, self._emit)
                          if holdings.enabled() else None)
        watched = self._watched if self._holdings is not None else pulse.watch_tickers
        held_note = self._held_note if self._holdings is not None else None
        self._pulse = (pulse.MarketPulse(lambda: service.provider("live"), self._emit, tickers=watched,
                                         held=self._held_tickers if self._holdings is not None else None)
                       if pulse.enabled() else None)
        self._breakouts = self._opportunities = self._tracker = None
        if opportunities.enabled():
            held_side = self._held_side if self._holdings is not None else None
            from .idea_tracker import TrackerJob, record_breakout
            self._breakouts = opportunities.BreakoutWatch(
                lambda: service.provider("live"), self._emit, watchlist=watched, held=held_note,
                history=self._breakout_history, held_side=held_side,
                track=lambda *args: record_breakout(self.repo, *args))
            self._tracker = TrackerJob(self.repo, self._emit)
            self._opportunities = opportunities.OpportunityRunner(
                service, self._emit, watchlist=watched, breakouts=self._breakouts, held=held_note,
                held_side=held_side)

    def _watched(self):
        """The watchlist plus held stocks and option underlyings."""
        from . import pulse
        try:
            held = self.service.holdings.tickers()
        except Exception:
            held = []
        return list(dict.fromkeys([*pulse.watch_tickers(), *held]))

    def _shared_quotes(self, session):
        """Quotes for every live watch, once a minute in the regular session; None otherwise."""
        import time
        if session != "regular" or time.monotonic() < self._next_quotes:
            return None
        self._next_quotes = time.monotonic() + SHARED_QUOTE_SECONDS
        codes = set()
        for part, getter in ((self._pulse, "_tickers"), (self._breakouts, "tickers"), (self._holdings, "codes")):
            if part is not None:
                try:
                    codes.update(getattr(part, getter)())
                except Exception as exc:
                    logger.info("Live watch tickers unavailable: %s", type(exc).__name__)
        if not codes:
            return {}
        try:
            provider = self.service.provider("live")
        except Exception as exc:
            logger.warning("Live quotes unavailable: %s", type(exc).__name__)
            return {}
        try:
            return provider.watchlist_quotes(sorted(codes))
        except Exception as exc:
            # Stocks and option contracts apart: a quota or permission problem with one kind
            # does not silence every live watch. An empty dict still lets date-based
            # holdings alerts (expiry, earnings) run on the last synced prices.
            logger.warning("Live quotes unavailable (%s); retrying stocks and options apart", type(exc).__name__)
            from .holdings import parse_code
            quotes = {}
            for kind in ("stock", "option"):
                part = sorted(code for code in codes if parse_code(code)["kind"] == kind)
                try:
                    quotes.update(provider.watchlist_quotes(part) if part else {})
                except Exception:
                    pass
            return quotes

    def _breakout_history(self):
        new_york = ZoneInfo("America/New_York")
        return [(event["payload"].get("underlying"), "up" if event["payload"].get("kind") == "breakout" else "down",
                 datetime.fromisoformat(event["created_at"]).astimezone(new_york).date())
                for event in self.repo.events(limit=500, newest=True, types=["breakout"],
                                              since=(utcnow() - timedelta(days=10)).isoformat())]

    def _held_tickers(self):
        """Held tickers, or an error (read by the pulse as "unknown") before the first sync."""
        if not self.service.holdings.raw().get("synced_at"):
            raise LookupError("holdings not synced yet")
        return self.service.holdings.tickers()

    def _held_side(self, ticker):
        """"long"/"short"/"mixed", "" when not held, None when holdings are unknown."""
        try:
            if not self.service.holdings.raw().get("synced_at"):
                return None
            return self.service.holdings.side(ticker)
        except Exception:
            return None

    def _held_note(self, ticker):
        """The "You hold" line, "" when not held, None when holdings are unknown."""
        try:
            if not self.service.holdings.raw().get("positions") and not self.service.holdings.raw().get("synced_at"):
                return None
            return self.service.holdings.note(ticker)
        except Exception:  # unknown: ideas keep the generic "if you hold it" wording
            return None

    def start(self):
        if self._thread or not self.service.enabled:
            return
        self._leader = self.repo.lease(self.owner)
        if self._leader:
            self.repo.recover_jobs()
        self._thread = threading.Thread(target=self._loop, name="trade-desk-monitor", daemon=True)
        self._thread.start()

    def status(self):
        return {"running": bool(self._thread and self._thread.is_alive()), "leader": self._leader,
                "last_tick": self._last_tick, "error": self._error,
                "unmonitored_count": self._unmonitored_count, "plan_limit": 20,
                "holdings_error": self._holdings.last_error if self._holdings is not None else None,
                "holdings_error_at": self._holdings.error_at.get("sync") if self._holdings is not None else None}

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        for part in (self._pulse, self._breakouts, self._opportunities, self._holdings, self._tracker):
            if part is not None:
                part.stop()
        self.repo.release(self.owner)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
                self._error = None
            except Exception as exc:
                self._error = type(exc).__name__
                logger.exception("Trade Desk monitoring failed")
            self._stop.wait(5)

    def _emit(self, event_type, payload, key):
        return self.repo.event(event_type, payload, dedup_key=key)

    def run_once(self):
        from data_provider.us_session import session_window
        from .quality import candidate_quotes_fresh
        was_leader = self._leader
        self._leader = self.repo.lease(self.owner)
        if not self._leader:
            return
        if not was_leader:
            # A crashed leader's lease can outlive a fast restart; recover its
            # interrupted jobs when leadership is gained, keeping this process's own.
            self.repo.recover_jobs(exclude=set(self.service._jobs))
        self._last_tick = utcnow().isoformat()
        now = utcnow()
        regular_session = session_window(now)[0] == "regular"

        def held(position):
            # Owned shares alone are coverage, not a position opened by the plan.
            return position["status"] in {"open", "reconciliation_required"}

        def expired_leg(leg):
            return bool(leg.get("expiry")) and datetime.fromisoformat(leg["expiry"]) <= now

        def market_key(position):
            # Held stock left after exercise/assignment needs no option expiry.
            legs = position["legs"] if held(position) else position["plan"]["candidate"]["legs"]
            expiry = next((datetime.fromisoformat(leg["expiry"]).date()
                           for leg in legs if leg["right"] != "stock" and leg.get("expiry")), None)
            return (position["plan"]["data_mode"], position["underlying"], expiry)

        def expired(plan):
            return any(leg.get("expiry") and datetime.fromisoformat(leg["expiry"]) <= now
                       for leg in plan["candidate"]["legs"])

        # An unfilled plan whose contracts have expired can no longer be entered.
        positions = [p for p in self.service.positions() if p["plan"].get("monitoring")
                     and p["status"] != "closed" and
                     (held(p) or (p["plan"].get("status") not in {"invalidated", "archived"}
                                  and not expired(p["plan"])))]
        self._unmonitored_count = max(0, len(positions) - 20)
        if self._unmonitored_count:
            self._emit("monitor_capacity", {"message": f"{self._unmonitored_count} plans exceed the active monitoring limit; open positions take priority."},
                       "capacity:" + utcnow().strftime("%Y%m%d%H"))
        positions.sort(key=lambda item: (not held(item), item["plan_id"]))
        collected = {}
        required_by_market = {}
        for position in positions[:20]:
            plan = position["plan"]
            key = market_key(position)
            legs = position["legs"] if held(position) else plan["candidate"]["legs"]
            required_by_market.setdefault(key, set()).update(
                leg["contract_id"] for leg in legs if leg["right"] != "stock")
        for position in positions[:20]:
            plan = position["plan"]
            if not plan.get("monitoring") or position["status"] == "closed":
                continue
            if plan.get("status") in {"invalidated", "archived"} and not held(position):
                continue
            if not self.repo.lease(self.owner):
                return
            if position["status"] == "reconciliation_required":
                self._emit("position_reconciliation", {"plan_id": plan["id"], "underlying": position["underlying"],
                    "message": "Check exercise/assignment and record resulting fills. Position has not been closed.",
                    "data_mode": plan["data_mode"], "ledger": plan["ledger"]}, f"reconcile:{plan['id']}")
            if plan.get("exit_at") and utcnow() >= datetime.fromisoformat(plan["exit_at"]):
                self._emit("time_exit", {"plan_id": plan["id"], "underlying": position["underlying"],
                    "message": "Planned exit time reached; confirm any real exit and record its fill.",
                    "data_mode": plan["data_mode"], "ledger": plan["ledger"]},
                    f"time-exit:{plan['id']}:{plan['exit_at']}")
            if held(position) and any(leg["right"] != "stock" and expired_leg(leg) for leg in position["legs"]):
                # Expired contracts cannot be quoted again; the reconciliation
                # alert above covers them instead of an hourly outage.
                continue
            required = {"legs": position["legs"]} if held(position) else plan["candidate"]
            key = market_key(position)
            expiry = key[2]
            contracts = sorted(required_by_market[key])
            try:
                if key not in collected:
                    collected[key] = self.service.snapshot(TradeAdviceRequest(
                        ticker=position["underlying"], data_mode=plan["data_mode"], expiry=expiry),
                        required_contracts=contracts)
                snapshot = collected[key]
                if not candidate_quotes_fresh(snapshot, required):
                    raise ValueError("Required stock or option quotes stale or unverified")
            except Exception:
                if plan["data_mode"] == "live" and not regular_session:
                    # Option quotes are not expected outside the regular
                    # session; hourly overnight/weekend outages would be noise.
                    continue
                self._emit("data_outage", {"plan_id": plan["id"], "underlying": position["underlying"],
                    "message": "Monitoring paused: fresh verified prices are unavailable; position status is unchanged.",
                    "data_mode": plan["data_mode"], "ledger": plan["ledger"]},
                    f"outage:{plan['id']}:{utcnow().strftime('%Y%m%d%H')}")
                continue
            if not self.repo.lease(self.owner):
                return
            above = plan.get("trigger_direction", "above") == "above"
            price = snapshot.spot
            checks = [
                ("price_trigger", plan.get("trigger_price"),
                 lambda level: price >= level if above else price <= level,
                 "Underlying price trigger reached; review the remaining entry conditions before any trade."),
                ("invalidation", plan.get("invalidation_price"),
                 lambda level: price <= level if above else price >= level,
                 "Underlying invalidation level reached. Review the plan or open position in moomoo."),
                ("target", plan.get("target_price"),
                 lambda level: price >= level if above else price <= level,
                 "Underlying target reached; this is an alert, not an executed exit."),
            ]
            for event_type, level, predicate, message in checks:
                if level is None or not predicate(level):
                    continue
                if event_type == "price_trigger" and held(position):
                    continue
                self._emit(event_type, {"plan_id": plan["id"], "underlying": position["underlying"],
                    "price": price, "level": level, "message": message,
                    "data_mode": plan["data_mode"], "ledger": plan["ledger"]},
                    f"{event_type}:{plan['id']}:{level}")
        # One OpenD snapshot a minute serves the market pulse, breakouts and holdings.
        quotes = self._shared_quotes(session_window(now)[0])
        if self._pulse is not None:
            try:
                self._pulse.tick(now, quotes=quotes, shared=True)
            except Exception as exc:  # the watch must never stop plan monitoring
                logger.warning("Market pulse check failed: %s", type(exc).__name__)
        if self._holdings is not None:
            try:
                self._holdings.tick(now, session_window(now)[0], quotes=quotes, shared=True)
            except Exception as exc:  # optional; plan monitoring continues
                logger.warning("Holdings monitor failed: %s", type(exc).__name__)
        if self._tracker is not None:
            try:
                self._tracker.tick(now)
            except Exception as exc:  # optional; plan monitoring continues
                logger.warning("Idea tracker failed: %s", type(exc).__name__)
        if self._opportunities is not None:
            try:
                self._breakouts.tick(now, session_window(now)[0], quotes=quotes, shared=True)
            except Exception as exc:  # optional; plan monitoring continues
                logger.warning("Breakout watch failed: %s", type(exc).__name__)
            try:
                self._opportunities.tick(regular_session)
            except Exception as exc:  # optional; plan monitoring continues
                logger.warning("Trade opportunities failed: %s", type(exc).__name__)
        self._deliver()

    def _deliver(self):
        if not self.repo.preferences()["discord_enabled"]:
            return
        from src.config import get_config
        from src.notification import NotificationService
        config = get_config()
        if not (getattr(config, "discord_webhook_url", None) or
                (getattr(config, "discord_bot_token", None) and getattr(config, "discord_main_channel_id", None))):
            return
        # Only recent rows matter: events older than 15 minutes are never delivered.
        events = self.repo.events(limit=2000, newest=True,
                                  since=(utcnow() - timedelta(minutes=40)).isoformat())
        deliveries, delivered_parts = {}, {}
        for event in events:
            if event["event_type"] in {"discord_attempt", "discord_delivery"}:
                deliveries.setdefault(event["payload"].get("event_id"), event)
            if event["event_type"] == "discord_delivery":
                # A crash after the claim leaves the claim newest; parts sent come from deliveries.
                delivered_parts.setdefault(event["payload"].get("event_id"), set(event["payload"].get("sent_parts", [])))
        wanted = {"price_trigger", "invalidation", "target", "time_exit", "data_outage", "position_reconciliation", "monitor_capacity",
                  "market_move", "market_news", "options_ideas", "trade_opportunities", "breakout",
                  "holding_alert", "portfolio_summary", "track_record"}
        for event in reversed(events):
            if event["event_type"] not in wanted:
                continue
            prior = deliveries.get(event["id"])
            attempt = prior["payload"]["attempt"] if prior else 0
            if prior and (prior["payload"].get("success") or attempt >= 3):
                continue
            if utcnow() - datetime.fromisoformat(event["created_at"]) > timedelta(minutes=15):
                continue
            if prior and utcnow() - datetime.fromisoformat(prior["created_at"]) < timedelta(seconds=60):
                continue
            claim = self.repo.event("discord_attempt", {"event_id": event["id"], "attempt": attempt + 1,
                "success": False, "diagnostic": "Delivery attempt started"},
                dedup_key=f"discord-attempt:{event['id']}:{attempt + 1}")
            if claim is None:
                continue
            payload = event["payload"]
            base = os.getenv("TRADE_DESK_PUBLIC_URL", "").rstrip("/")
            target = payload.get("plan_id") or payload.get("advice_id", "")
            query = "planId" if payload.get("plan_id") else "adviceId"
            if not base:
                link = "Open Trade Desk in your app"
            elif target:
                link = f"{base}/trade-desk?{query}={target}"
            else:
                link = f"{base}/trade-desk?view=positions"
            # Messages can quote model output built from news text; never let it
            # ping @everyone/@here or users in the channel.
            message = str(payload.get("message", "")).replace("@", "@\u200b")
            ticker = payload.get("underlying", "")
            if event["event_type"] in {"trade_opportunities", "portfolio_summary", "track_record"}:
                content = message  # carries its own header
            elif event["event_type"] == "options_ideas":
                content = f"🧭 **Options ideas** · for today's high-conviction trades\n{message}"
                if base:
                    content += f"\n{base}/trade-desk"
            elif event["event_type"] == "holding_alert":
                icon, label = {"rule": ("🛎️", "Your alert"), "expiry": ("⏳", "Expiry"),
                               "assignment": ("⚠️", "Assignment risk"), "profit": ("💰", "Profit"),
                               "loss": ("🩸", "Loss"), "earnings": ("📅", "Earnings"),
                               "trend": ("📉", "Trend break")}.get(payload.get("kind"), ("🛎️", "Holding"))
                content = f"{icon} **{ticker}** · {label}\n{message}"
            elif event["event_type"] == "breakout":
                up = payload.get("kind") == "breakout"
                content = f"{'🚀' if up else '🔻'} **{ticker}** · {'Breakout' if up else 'Breakdown'}\n{message}"
            elif event["event_type"] in {"market_move", "market_news"}:
                if event["event_type"] == "market_news":
                    icon, label = "📰", "News"
                else:
                    moved_up = (payload.get("change_pct") or 0) >= 0
                    icon, label = ("📈" if moved_up else "📉"), "Big move"
                content = f"{icon} **{ticker}** · {label}\n{message}"
            else:
                label = _EVENT_LABELS.get(event["event_type"], event["event_type"].replace("_", " ").capitalize())
                content = f"🧭 **Trade Desk · {label}** · {ticker}\n{message}\n{link}"
            if payload.get("data_mode") == "replay":
                content = "SYNTHETIC REPLAY / PAPER ONLY\n" + content
            # Each part is sent once: a retry only sends the parts that failed.
            parts = discord_parts(content)
            sent = set(delivered_parts.get(event["id"], set()))
            diagnostic = "delivered"
            for index, part in enumerate(parts):
                if index in sent:
                    continue
                try:
                    if NotificationService().send_to_discord(part):
                        sent.add(index)
                    else:
                        diagnostic = "Discord delivery failed"
                except Exception as exc:
                    diagnostic = type(exc).__name__
            success = len(sent) == len(parts)
            self.repo.event("discord_delivery", {"event_id": event["id"], "attempt": attempt + 1,
                            "success": success, "diagnostic": diagnostic if not success else "delivered",
                            "sent_parts": sorted(sent), "parts": len(parts)})
