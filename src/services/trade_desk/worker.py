"""Independent quote monitoring and persisted, selective Discord delivery."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import TradeAdviceRequest, identity, utcnow

logger = logging.getLogger(__name__)


_EVENT_LABELS = {"opportunity": "Opportunity", "price_trigger": "Price trigger", "invalidation": "Invalidated",
                 "target": "Target reached", "time_exit": "Time to exit", "data_outage": "Data outage",
                 "position_reconciliation": "Reconcile position", "monitor_capacity": "Monitor capacity"}


class TradeDeskWorker:
    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self.owner = identity()
        self._stop = threading.Event()
        self._thread = None
        self._scan_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trade-discovery")
        self._scan_future = None
        self._next_scan = 0.0
        self._last_tick = None
        self._error = None
        self._leader = False
        self._unmonitored_count = 0
        from . import pulse
        self._pulse = (pulse.MarketPulse(lambda: service.provider("live"), self._emit)
                       if pulse.enabled() else None)
        from . import opportunities
        self._breakouts = self._opportunities = None
        if opportunities.enabled():
            self._breakouts = opportunities.BreakoutWatch(lambda: service.provider("live"), self._emit,
                                                          watchlist=pulse.watch_tickers)
            self._opportunities = opportunities.OpportunityRunner(
                service, self._emit, watchlist=pulse.watch_tickers, breakouts=self._breakouts)

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
                "unmonitored_count": self._unmonitored_count, "plan_limit": 20}

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._scan_pool.shutdown(wait=False, cancel_futures=True)
        for part in (self._pulse, self._breakouts, self._opportunities):
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
        if self._pulse is not None:
            try:
                self._pulse.tick(now)
            except Exception as exc:  # the watch must never stop plan monitoring
                logger.warning("Market pulse check failed: %s", type(exc).__name__)
        if self._opportunities is not None:
            try:
                self._breakouts.tick(now, session_window(now)[0])
            except Exception as exc:  # optional; plan monitoring continues
                logger.warning("Breakout watch failed: %s", type(exc).__name__)
            try:
                self._opportunities.tick(regular_session)
            except Exception as exc:  # optional; plan monitoring continues
                logger.warning("Trade opportunities failed: %s", type(exc).__name__)
        self._proactive()
        self._deliver()

    def _proactive(self):
        from data_provider.us_session import session_window
        from .quality import candidate_quotes_fresh, quote_age_ok, snapshot_fresh
        prefs = self.repo.preferences()
        phase = session_window()[0]
        # Proactive discovery, model runs and idea delivery are limited to the
        # regular session: options quotes are live then, and each run costs a model call.
        if not prefs["proactive_enabled"] or phase != "regular":
            return
        now = utcnow()
        day = now.astimezone(ZoneInfo("America/New_York")).date()
        events = self.repo.events(limit=5000, newest=True)
        opportunities = [e for e in events if e["event_type"] == "opportunity" and
            datetime.fromisoformat(e["created_at"]).astimezone(ZoneInfo("America/New_York")).date() == day]
        for job in self.repo.advice_list(50):
            if job.get("source") != "proactive" or job["status"] != "completed" or job.get("llm_status") != "ready":
                continue
            if job.get("assessment") == "wait":
                continue
            if any(e["payload"].get("advice_id") == job["id"] for e in opportunities):
                continue
            symbol = job["request"]["ticker"]
            if len(opportunities) >= prefs["opportunity_daily_limit"]:
                break
            if any(e["payload"].get("underlying") == symbol and now - datetime.fromisoformat(e["created_at"])
                   < timedelta(minutes=prefs["cooldown_minutes"]) for e in opportunities):
                continue
            candidates = [c for c in job["candidates"] if c["evidence_confidence"] == "high"
                          and c["entry_conditions"] and c["invalidation"] and c["exit_conditions"]]
            if not candidates:
                continue
            if now - datetime.fromisoformat(job["updated_at"]) > timedelta(seconds=30):
                continue
            snapshot = self.service._latest.get(("live", symbol))
            if not snapshot or not snapshot_fresh(snapshot) or not snapshot.source_verified:
                continue
            quotes = {q.contract_id: q for q in snapshot.options}
            eligible = []
            for candidate in candidates:
                from .models import QuoteSnapshot
                saved = job.get("snapshots", {}).get(candidate["snapshot_id"], job.get("snapshot"))
                if not saved or not candidate_quotes_fresh(QuoteSnapshot.model_validate(saved), candidate, now):
                    continue
                if not candidate_quotes_fresh(snapshot, candidate, now):
                    continue
                options = [quotes.get(leg["contract_id"]) for leg in candidate["legs"] if leg["right"] != "stock"]
                if all(q and q.bid > 0 and q.ask >= q.bid and
                       (q.ask - q.bid) / ((q.ask + q.bid) / 2) <= 0.10 and
                       quote_age_ok(q.quoted_at, now) and
                       ((q.volume or 0) >= 50 or (q.open_interest or 0) >= 100) for q in options):
                    eligible.append(candidate)
            # Provider connectivity metadata is provenance, not a thesis catalyst.
            thesis_evidence = [item for item in snapshot.evidence if item.get("kind") in {"news", "report", "signal", "market_scan"}]
            if not eligible or not thesis_evidence:
                continue
            event = self._emit("opportunity", {"advice_id": job["id"], "underlying": symbol,
                "message": job["explanation"][:1200], "candidate_ids": [c["id"] for c in eligible],
                "confidence_meaning": "Strength of evidence; not a measured win probability"}, f"opportunity:{job['id']}")
            if event:
                opportunities.append(event)
        if len(opportunities) >= prefs["opportunity_daily_limit"]:
            return
        if self._scan_future is None and time.monotonic() >= self._next_scan:
            self._next_scan = time.monotonic() + 900
            self._scan_future = self._scan_pool.submit(self._discover)
        if self._scan_future and self._scan_future.done():
            try:
                symbols = self._scan_future.result()
                existing = self.repo.advice_list(100)
                for symbol in symbols[:10]:
                    if any(j["request"]["ticker"] == symbol and j.get("source") == "proactive" and
                           now - datetime.fromisoformat(j["created_at"]) < timedelta(hours=1) for j in existing):
                        continue
                    # One symbol's failure must not end the whole discovery cycle.
                    try:
                        # One model request per discovery cycle; no calls on every quote update.
                        request = TradeAdviceRequest(ticker=symbol, data_mode="live", horizon="both",
                            message="Assess whether a fresh, well-supported opportunity exists. Wait if evidence is weak.")
                        snapshot = self.service.snapshot(request)
                    except Exception as exc:
                        logger.info("Trade Desk discovery skipped %s: %s", symbol, type(exc).__name__)
                        continue
                    if snapshot_fresh(snapshot) and snapshot.source_verified:
                        self.service.submit(request, source="proactive")
                        break
            except Exception as exc:
                logger.warning("Trade Desk proactive discovery unavailable: %s", type(exc).__name__)
            finally:
                self._scan_future = None

    def _discover(self):
        from src.config import get_config
        from src.services.us_market_scan import collect_us_market_scan
        watchlist = list(getattr(get_config(), "stock_list", []))
        scan = collect_us_market_scan(watchlist)
        rows = [row for key in ("gainers", "losers") for row in scan.get(key, [])]
        movers = [row["code"] for row in rows]
        # The scanner is discovery evidence only. Prices for comparisons and
        # simulated executions still come exclusively from the quote provider.
        import math
        now = utcnow()
        evidence = {}
        for row in rows:
            metrics = {}
            for field in ("price", "change_pct", "amount", "volume"):
                value = row.get(field)
                metrics[field] = float(value) if value is not None and math.isfinite(float(value)) else None
            evidence[row["code"]] = (now, {"kind": "market_scan", "ticker": row["code"],
                "source": "Existing US market scanner (Yahoo research data; may be delayed)",
                "as_of": str(row.get("provider_timestamp") or scan.get("as_of") or "unknown"),
                "session": scan.get("session"), "metrics": metrics,
                "meaning": "Observed session move; catalyst and continuation unverified. Not an executable quote."})
        with self.service._lock:
            self.service._discovery_evidence = evidence
        # The configured watchlist may include A-share/HK codes; options discovery is US-only.
        watchlist = [code.strip().upper() for code in watchlist
                     if re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", str(code).strip().upper())]
        symbols = []
        for index in range(max(len(watchlist), len(movers))):
            symbols.extend(group[index] for group in (watchlist, movers) if index < len(group))
        return list(dict.fromkeys(symbols))

    def _deliver(self):
        if not self.repo.preferences()["discord_enabled"]:
            return
        from src.config import get_config
        from src.notification import NotificationService
        config = get_config()
        if not (getattr(config, "discord_webhook_url", None) or
                (getattr(config, "discord_bot_token", None) and getattr(config, "discord_main_channel_id", None))):
            return
        events = self.repo.events(limit=2000, newest=True)
        deliveries = {}
        for event in events:
            if event["event_type"] in {"discord_attempt", "discord_delivery"}:
                deliveries.setdefault(event["payload"].get("event_id"), event)
        wanted = {"opportunity", "price_trigger", "invalidation", "target", "time_exit", "data_outage", "position_reconciliation", "monitor_capacity",
                  "market_move", "market_news", "options_ideas", "trade_opportunities", "breakout"}
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
            if event["event_type"] == "trade_opportunities":
                content = message  # carries its own header
            elif event["event_type"] == "options_ideas":
                content = f"🧭 **Options ideas** · for today's high-conviction trades\n{message}"
                if base:
                    content += f"\n{base}/trade-desk"
            elif event["event_type"] == "breakout":
                up = payload.get("kind") == "breakout"
                content = f"{'🚀' if up else '🔻'} **{ticker}** · {'Breakout' if up else 'Breakdown'}\n{message}"
            elif event["event_type"] in {"market_move", "market_news"}:
                if event["event_type"] == "market_news":
                    icon, label = "📰", "News"
                else:
                    moved_up = (payload.get("change_pct") or 0) >= 0 if payload.get("kind") == "day_move" \
                        else " moved +" in message or " up " in message
                    icon, label = ("📈" if moved_up else "📉"), ("Fast move" if payload.get("kind") == "fast_move" else "Big move")
                content = f"{icon} **{ticker}** · {label}\n{message}"
            else:
                label = _EVENT_LABELS.get(event["event_type"], event["event_type"].replace("_", " ").capitalize())
                content = f"🧭 **Trade Desk · {label}** · {ticker}\n{message}\n{link}"
            if payload.get("data_mode") == "replay":
                content = "SYNTHETIC REPLAY / PAPER ONLY\n" + content
            try:
                success = bool(NotificationService().send_to_discord(content))
                diagnostic = "delivered" if success else "Discord delivery failed"
            except Exception as exc:
                success, diagnostic = False, type(exc).__name__
            self.repo.event("discord_delivery", {"event_id": event["id"], "attempt": attempt + 1,
                            "success": success, "diagnostic": diagnostic})
