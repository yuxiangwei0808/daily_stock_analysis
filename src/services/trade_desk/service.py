"""Trade Desk orchestration. There is deliberately no broker order interface."""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .advisor import CodexTradeAdvisor, RoutineTradeAdvisor, build_panel, second_opinions
from .ledger import position_from_fills, validate_fills
from .models import PLAN_STRATEGY, TradeAdviceRequest, TradeFill, TradePlan, identity, utcnow
from .repository import TradeDeskRepository

logger = logging.getLogger(__name__)


MATERIAL_ENTRY_CHANGE = 0.05


def _material_entry_change(original, current):
    """Whether re-priced entry cost moved *against* the user by >5% of size.

    Size is the larger of the absolute entry debit/credit and the bounded
    maximum loss, so credit spreads and cheap wings are judged on the risk
    actually taken rather than on a single leg's relative tick. A cheaper
    entry (lower debit or larger credit) never invalidates a setup.
    """
    before, after = original.payoff, current.payoff
    size = max(abs(before.entry_debit), before.max_loss or 0.0)
    if size <= 0:
        return True
    return after.entry_debit - before.entry_debit > MATERIAL_ENTRY_CHANGE * size


# Model-authored fields survive re-pricing; the numbers are recalculated.
_NARRATIVE_FIELDS = ("evidence_confidence", "reasons", "entry_conditions", "invalidation",
                     "exit_conditions", "warnings")


class TradeDeskService:
    def __init__(self, repo=None, provider_factory=None, advisor=None, routine_advisor=None):
        if provider_factory is None:
            from .providers import build_provider
            provider_factory = build_provider
        self.repo = repo or TradeDeskRepository()
        self.provider_factory = provider_factory
        self.advisor = advisor or CodexTradeAdvisor()
        self.routine_advisor = routine_advisor or RoutineTradeAdvisor()
        self.providers = {}
        self._lock = threading.RLock()
        self._jobs = {}
        self._latest = {}
        self._discovery_evidence = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trade-advice")
        self.worker = None
        self.enabled = os.getenv("TRADE_DESK_ENABLED", "true").lower() == "true"
        from .holdings import Holdings
        self.holdings = Holdings(self)

    def provider(self, mode):
        with self._lock:
            if mode not in self.providers:
                self.providers[mode] = self.provider_factory(mode)
            return self.providers[mode]

    def snapshot(self, request, required_contracts=()):
        provider = self.provider(request.data_mode)
        # A user's own plan legs are quoted alongside the calculated candidates.
        required = list(dict.fromkeys([*required_contracts, *request.plan_contracts()]))
        snapshot = provider.snapshot(request.ticker, expiry=request.expiry,
                                     required_contracts=required).model_copy(deep=True)
        if snapshot.underlying != request.ticker or snapshot.mode != request.data_mode:
            raise ValueError("Provider snapshot does not match the requested stock and data mode")
        with self._lock:
            scan = self._discovery_evidence.get(request.ticker)
        if scan and (utcnow() - scan[0]).total_seconds() <= 1800:
            snapshot.evidence.append(dict(scan[1]))
        # Stored reports/news are context only; their dates remain visible.
        try:
            for news in self.repo.db.get_recent_news(request.ticker, days=3, limit=5):
                snapshot.evidence.append({"kind": "news", "title": getattr(news, "title", ""),
                    "url": getattr(news, "url", ""), "published_at": str(getattr(news, "published_date", "")),
                    "source": getattr(news, "source", ""), "verified_catalyst": False})
        except Exception as exc:
            logger.debug("Trade Desk stored news unavailable: %s", type(exc).__name__)
        try:
            snapshot.evidence.extend(self.repo.research_evidence(request.ticker, request.source_report_id))
        except Exception as exc:
            logger.debug("Trade Desk report evidence unavailable: %s", type(exc).__name__)
        with self._lock:
            self._latest[(snapshot.mode, snapshot.underlying)] = snapshot
            for expiry in {quote.expiry.date() for quote in snapshot.options}:
                self._latest[(snapshot.mode, snapshot.underlying, expiry)] = snapshot
        return snapshot

    def health(self):
        from src.config import get_config
        config = get_config()
        def provider_health(mode):
            try:
                result = self.provider(mode).health()
                result.setdefault("available", bool(result.get("ok", False)))
                return result
            except Exception as exc:
                logger.warning("Trade Desk provider health: %s", type(exc).__name__)
                return {"available": False, "code": "provider_error", "message": "Provider health check failed"}
        discord = bool(getattr(config, "discord_webhook_url", None) or (
            getattr(config, "discord_bot_token", None) and getattr(config, "discord_main_channel_id", None)))
        with self._lock:
            observed = list(self._latest.values())
        return {"enabled": self.enabled, "live": provider_health("live"), "replay": provider_health("replay"),
                "worker": self.worker.status() if self.worker else {"running": False},
                "discord_configured": discord,
                "capabilities": {"ledgers": ["paper", "manual_live"], "broker_order_execution": False,
                    "single_expiry": True, "standard_us_stock_etf_options": True,
                    "adjusted_contracts": False, "cash_settled_index_options": False,
                    "calendars_and_diagonals": False, "replay_is_synthetic": True},
                "authenticated_quotes_verified": any(s.mode == "live" and s.source_verified and s.options
                                                      for s in observed)}

    def submit(self, request, source="manual"):
        if not self.enabled:
            raise ValueError("Trade Desk is disabled")
        with self._lock:
            self._jobs = {key: value for key, value in self._jobs.items() if not value[1].done()}
            if sum(not item[1].done() for item in self._jobs.values()) >= 8:
                raise ValueError("Advice queue is full; wait for a running request or cancel it")
            job = self.repo.create_advice(request.model_dump(mode="json"), source=source)
            cancel = threading.Event()
            future = self._pool.submit(self._run_advice, job["id"], cancel)
            self._jobs[job["id"]] = (cancel, future)
        return job

    def cancel(self, advice_id):
        job = self.repo.advice(advice_id)
        if not job:
            raise ValueError("Advice does not exist")
        if job["status"] not in {"queued", "running"}:
            return job
        with self._lock:
            running = self._jobs.get(advice_id)
            if running:
                running[0].set()
                running[1].cancel()
        # A job that finished meanwhile keeps its result instead of becoming cancelled.
        return (self.repo.update_advice(advice_id, {"status": "cancelled"}, only_statuses={"queued", "running"})
                or self.repo.advice(advice_id))

    def _fresh_news(self, request):
        """Latest headlines from FREE_NEWS_SOURCES, fetched when the user asks (not for automatic scans)."""
        from src.config import get_config
        from src.services.free_news import ticker_news
        config = get_config()
        sources = getattr(config, "free_news_sources", None)
        if not sources or request.data_mode != "live":
            return []
        try:
            items = ticker_news(request.ticker, sources, days=3, limit=12,
                                finnhub_key=getattr(config, "finnhub_api_key", None))
        except Exception as exc:  # news is context; its absence is stated, not fatal
            logger.info("Trade Desk fresh news unavailable: %s", type(exc).__name__)
            return []
        return [{"kind": "news", "title": item["title"], "url": item["url"], "source": item["source"],
                 "published_at": item["published_at"], "summary": item["summary"], "feed": item["feed"],
                 "about_ticker": item["related"], "verified_catalyst": False} for item in items]

    def _refresh_candidate(self, candidate, request, original_spot, *, require_unchanged=True):
        """Re-price a live candidate's own contracts from fresh quotes.

        Returns ``(candidate, snapshot)``; the candidate is None when a leg
        cannot be priced, or (with ``require_unchanged``) when the setup moved
        against its assessment: the underlying moved >1% or the entry cost rose
        by >5% of the position's size.
        """
        from .analytics import reprice_candidate
        from .quality import candidate_quotes_fresh, snapshot_fresh
        expiry = next(leg.expiry.date() for leg in candidate.legs if leg.expiry)
        effective = request.model_copy(update={"expiry": expiry, "strategies": [candidate.strategy],
                                               "horizon": candidate.horizon})
        current = self.snapshot(effective, required_contracts=[
            leg.contract_id for leg in candidate.legs if leg.right != "stock"])
        if not snapshot_fresh(current):
            raise ValueError("Fresh verified quotes are required for recalculation")
        match = reprice_candidate(candidate, current, effective)
        if match is None or not candidate_quotes_fresh(current, match):
            return None, current
        if require_unchanged and (abs(current.spot / original_spot - 1) > 0.01
                                  or _material_entry_change(candidate, match)):
            return None, current
        return match.model_copy(update={name: getattr(candidate, name) for name in _NARRATIVE_FIELDS}), current

    def _history(self, job):
        related = [item for item in reversed(self.repo.advice_list())
                   if item["conversation_id"] == job["conversation_id"] and item["id"] != job["id"]]
        history = []
        for item in related[-4:]:
            history.extend([{"role": "user", "content": item["request"].get("message") or
                             f"Compare {item['request']['ticker']} strategies"},
                            {"role": "assistant", "content": item.get("explanation", "")}])
        return history

    def _run_advice(self, advice_id, cancel):
        from .analytics import build_candidates, price_plan
        from .quality import candidate_quotes_fresh, snapshot_fresh
        if cancel.is_set():
            return
        job = self.repo.update_advice(advice_id, {"status": "running"})
        if not job:
            return
        request = TradeAdviceRequest.model_validate(job["request"])
        try:
            snapshot = self.snapshot(request)
            if snapshot.mode == "live" and not snapshot_fresh(snapshot):
                raise ValueError("Live quotes are stale or unverified; no current comparison can be produced")
            if job.get("source") != "proactive":
                seen = {item.get("title") for item in snapshot.evidence}
                snapshot.evidence.extend(item for item in self._fresh_news(request) if item["title"] not in seen)
            candidates = build_candidates(snapshot, request)
            plan_error = ""
            if request.plan_legs and not any(c.strategy == PLAN_STRATEGY for c in candidates):
                plan_error = price_plan(snapshot, request)[1] or "Your plan could not be priced."
            changes = {"plan_error": plan_error,
                       "candidates": [c.model_dump(mode="json") for c in candidates],
                       "snapshot": snapshot.model_dump(mode="json"), "provider": snapshot.provider,
                       "snapshots": {snapshot.id: snapshot.model_dump(mode="json")},
                       "assessment": "wait", "llm_status": "unavailable", "triggers": {}}
            if not candidates:
                changes.update(status="completed", explanation="No eligible contracts match this request. "
                               "Check expiry, strategy filters, quote quality and available data.")
                self.repo.update_advice(advice_id, changes)
                return
            def compare(effective, required_contracts=()):
                new_snapshot = self.snapshot(effective, required_contracts=required_contracts)
                if new_snapshot.mode == "live" and not snapshot_fresh(new_snapshot):
                    raise ValueError("Fresh verified quotes are required for recalculation")
                return new_snapshot, build_candidates(new_snapshot, effective)
            from src.config import get_config
            config = get_config()
            # Model tiers: automatic scans use the routine model; user requests use
            # Codex plus independent second opinions from SECOND_OPINION_BACKENDS.
            # Automatic scans and report-driven ideas are routine work.
            proactive = job.get("source") in {"proactive", "report", "opportunity"}
            advisor = self.routine_advisor if proactive and config.targeted_generation_backend else self.advisor
            panel_backends = [] if proactive else [
                item for item in config.second_opinion_backends if item != "codex_cli"]
            panel_future = None
            if panel_backends:
                panel_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trade-panel")
                panel_future = panel_pool.submit(second_opinions, request, snapshot, candidates,
                                                 panel_backends, config)
                panel_pool.shutdown(wait=False)
            narrative = None
            pool = {candidate.id: candidate for candidate in candidates}
            try:
                narrative, pool, contexts = advisor.explain(
                    request, snapshot, candidates, cancel=cancel, history=self._history(job), compare=compare)
                selected = []
                snapshots = {}
                originals = {}
                triggers = {}
                stale = False
                for selection in narrative.selections:
                    candidate = pool[selection.candidate_id]
                    context = contexts[candidate.id]
                    original = context["snapshot"]
                    originals[original.id] = original.model_dump(mode="json")
                    current = original
                    if original.mode == "live":
                        # Re-price the selected contracts themselves; rebuilding by
                        # strategy could pick other strikes after a tiny spot move.
                        try:
                            match, current = self._refresh_candidate(candidate, context["request"], original.spot)
                        except Exception as exc:  # the explanation stands; its prices are unconfirmed
                            logger.info("Trade Desk selection re-pricing unavailable: %s", type(exc).__name__)
                            match = None
                        invalidated = selection.invalidation_price is not None and (
                            current.spot <= selection.invalidation_price if selection.trigger_direction == "above"
                            else current.spot >= selection.invalidation_price)
                        if (match is None or invalidated
                                or (selection.exit_at is not None and selection.exit_at <= utcnow())):
                            stale = True
                        else:
                            candidate = match
                    candidate = candidate.model_copy(update={
                        "evidence_confidence": selection.confidence,
                        "reasons": [selection.reason], "entry_conditions": selection.entry_conditions,
                        "invalidation": selection.invalidation, "exit_conditions": selection.exit_conditions})
                    # Replay and unsupported evidence cannot earn a proactive high-confidence label.
                    if current.mode == "replay":
                        candidate.evidence_confidence = "low"
                    elif candidate.evidence_confidence == "high" and not any(
                            item.get("kind") in {"news", "report", "signal", "market_scan"}
                            for item in current.evidence):
                        candidate.evidence_confidence = "low"
                        candidate.warnings.append("Supporting catalyst or research evidence is missing; quote provenance alone is insufficient.")
                    selected.append(candidate.model_dump(mode="json"))
                    snapshots[current.id] = current.model_dump(mode="json")
                    triggers[candidate.id] = selection.model_dump(mode="json", exclude={
                        "candidate_id", "reason", "confidence", "entry_conditions", "invalidation", "exit_conditions"})
                changes.update(candidates=selected or changes["candidates"],
                               assessment=narrative.assessment, explanation=narrative.explanation,
                               llm_status="ready", triggers=triggers, usage=contexts.get("_usage"),
                               tool_calls=contexts.get("_tool_calls", []),
                               effective_requests={item["id"]: contexts[item["id"]]["request"].model_dump(mode="json")
                                                   for item in selected},
                               snapshot=next(iter(snapshots.values()), changes["snapshot"]),
                               snapshots={**originals, **snapshots} or changes["snapshots"], source_snapshots=originals,
                               status="stale" if stale else "completed")
            except Exception as exc:
                if cancel.is_set():
                    return
                logger.warning("Trade Desk advice explanation failed: %s", type(exc).__name__)
                changes.update(status="completed", llm_error=str(exc)[:500],
                    explanation="Calculated strategy comparisons are available. The model could not produce a "
                                "validated explanation; no AI recommendation or high-confidence opportunity was issued.")
            if panel_future is not None:
                try:
                    changes["panel"] = build_panel(narrative, pool, panel_future.result(), config)
                except Exception as exc:  # advisory only
                    logger.warning("Trade Desk second opinions unavailable: %s", type(exc).__name__)
            if request.data_mode == "live" and changes.get("status") == "completed":
                # The model call can outlast the 30-second quote window; re-price
                # aged candidates, and call the comparison stale only if one changed.
                from .models import QuoteSnapshot, StrategyCandidate
                refreshed = []
                assessed = set(changes.get("triggers") or {})  # candidates the model selected
                for item in changes["candidates"]:
                    saved = QuoteSnapshot.model_validate(changes["snapshots"][item["snapshot_id"]])
                    if candidate_quotes_fresh(saved, item):
                        refreshed.append(item)
                        continue
                    effective = TradeAdviceRequest.model_validate(
                        (changes.get("effective_requests") or {}).get(item["id"], job["request"]))
                    try:
                        # Unselected candidates carry no model reasoning about their
                        # prices, so current numbers simply replace the old ones.
                        match, current = self._refresh_candidate(
                            StrategyCandidate.model_validate(item), effective, saved.spot,
                            require_unchanged=item["id"] in assessed)
                    except Exception as exc:  # the analysis stands; only its prices are unconfirmed
                        logger.info("Trade Desk re-pricing unavailable: %s", type(exc).__name__)
                        match = None
                    if match is None:
                        changes["status"] = "stale"
                        refreshed.append(item)
                        continue
                    changes["snapshots"][current.id] = current.model_dump(mode="json")
                    refreshed.append(match.model_dump(mode="json"))
                changes["candidates"] = refreshed
            if not cancel.is_set():
                self.repo.update_advice(advice_id, changes)
        except Exception as exc:
            if not cancel.is_set():
                logger.warning("Trade Desk advice failed: %s", type(exc).__name__)
                self.repo.update_advice(advice_id, {"status": "failed", "error": str(exc)[:500],
                                                  "assessment": "wait"})

    def create_plan(self, advice_id, candidate_id, ledger="paper", **overrides):
        job = self.repo.advice(advice_id)
        if not job or job["status"] != "completed":
            raise ValueError("Select a candidate from a completed, non-stale comparison")
        candidate = next((c for c in job["candidates"] if c["id"] == candidate_id), None)
        if candidate is None:
            raise ValueError("Candidate is not part of the selected advice")
        mode = job["request"]["data_mode"]
        if ledger == "manual_live" and mode != "live":
            raise ValueError("Replay candidates cannot create manual-live plans")
        effective = job.get("effective_requests", {}).get(candidate_id, job["request"])
        if mode == "live":
            from .models import QuoteSnapshot, StrategyCandidate
            from .quality import candidate_quotes_fresh
            saved = job.get("snapshots", {}).get(candidate["snapshot_id"], job.get("snapshot"))
            if not saved:
                raise ValueError("This comparison has no saved quotes; request a refreshed version")
            saved = QuoteSnapshot.model_validate(saved)
            if not candidate_quotes_fresh(saved, candidate):
                # Reading a comparison takes longer than the 30-second quote
                # window: re-price the same contracts now instead of refusing.
                match, _ = self._refresh_candidate(StrategyCandidate.model_validate(candidate),
                                                   TradeAdviceRequest.model_validate(effective), saved.spot)
                if match is None:
                    raise ValueError("Prices or the underlying have moved materially since this comparison; "
                                     "request a refreshed version before selecting a live-data plan")
                candidate = match.model_dump(mode="json")
        defaults = {k: v for k, v in job.get("triggers", {}).get(candidate_id, {}).items() if v is not None}
        defaults.update(overrides)
        owned_shares = effective.get("existing_shares", 0) if any(
            leg["right"] == "stock" and leg.get("existing") for leg in candidate["legs"]) else None
        plan = TradePlan(advice_id=advice_id, candidate=candidate, ledger=ledger,
                         data_mode=mode, existing_share_quantity=owned_shares, **defaults)
        return self.repo.add_plan(plan.model_dump(mode="json"))

    def update_plan(self, plan_id, changes):
        plan = self.repo.plan(plan_id)
        if not plan:
            raise ValueError("Plan does not exist")
        # Validate monitoring fields, but status transitions cannot fabricate closed holdings.
        allowed = {"monitoring", "notes", "trigger_price", "trigger_direction", "invalidation_price",
                   "target_price", "exit_at", "status"}
        if set(changes) - allowed:
            raise ValueError("Unsupported plan change")
        if "status" in changes and changes["status"] not in {"invalidated", "archived"}:
            raise ValueError("Position status is derived from recorded fills")
        candidate = TradePlan.model_validate({**plan, **changes})
        checked = candidate.model_dump(mode="json")
        return self.repo.update_plan(plan_id, {key: checked[key] for key in changes})

    def record_fill(self, plan_id, payload):
        plan = self.repo.plan(plan_id)
        if not plan or plan["ledger"] != "manual_live":
            raise ValueError("Manual fills require a manual-live plan")
        fill = TradeFill(plan_id=plan_id, **payload)
        self.repo.add_fills(plan_id, [fill.model_dump(mode="json")], validate=validate_fills)
        return self.position(plan_id)

    def paper_fill(self, plan_id, quantity=1, intent="open", limit_price=None):
        from .quality import quote_age_ok, quote_matches_leg, snapshot_fresh
        plan = self.repo.plan(plan_id)
        if not plan or plan["ledger"] != "paper":
            raise ValueError("Paper fills require a paper plan")
        if quantity < 1 or intent not in {"open", "close"}:
            raise ValueError("Invalid paper order")
        legs = plan["candidate"]["legs"]
        expiry = next(datetime.fromisoformat(leg["expiry"]).date() for leg in legs if leg.get("expiry"))
        snapshot = self.snapshot(TradeAdviceRequest(ticker=plan["candidate"]["underlying"],
                              data_mode=plan["data_mode"], expiry=expiry), required_contracts=[
                                  leg["contract_id"] for leg in legs if leg["right"] != "stock"])
        if not snapshot_fresh(snapshot):
            raise ValueError("Fresh bid/ask quotes are required for paper execution")
        quotes = {item.contract_id: item for item in snapshot.options}
        prior_position = self.position(plan_id)
        balances = {item["contract_id"]: item["signed_quantity"] for item in prior_position["legs"]}
        if intent == "open" and plan["candidate"]["strategy"] == "covered_call":
            owned_stock = next((leg for leg in legs if leg["right"] == "stock" and leg.get("existing")), None)
            if owned_stock:
                calls = [leg for leg in legs if leg["right"] == "call" and leg["side"] == "sell"]
                required_shares = sum((leg["quantity"] * quantity + max(0, -balances.get(leg["contract_id"], 0)))
                                      * leg["multiplier"] for leg in calls)
                if required_shares > balances.get(owned_stock["contract_id"], 0):
                    raise ValueError("The recorded existing shares do not cover this paper call quantity")
        fills, net_debit = [], 0.0
        original_job = self.repo.advice(plan["advice_id"])
        fee = float(original_job["request"].get("fee_per_contract", 0.65))
        now = utcnow()
        for leg in legs:
            # Explicitly owned shares were never bought by this plan, so a
            # simulated close must not sell them either.
            if leg.get("existing"):
                continue
            code = leg["contract_id"]
            units = leg["quantity"] * quantity
            side = leg["side"]
            if intent == "close":
                balance = balances.get(code, 0)
                if not balance:
                    continue
                units = min(units, abs(balance))
                side = "sell" if balance > 0 else "buy"
            if leg["right"] == "stock":
                price = snapshot.ask if side == "buy" else snapshot.bid
                size = None
            else:
                quote = quotes.get(code)
                if not quote_matches_leg(snapshot, quote, leg):
                    raise ValueError("A required contract is missing or its metadata does not match the plan")
                if quote.expiry <= now:
                    raise ValueError("A required contract is expired")
                if not quote_age_ok(quote.quoted_at, now) or quote.bid <= 0 or quote.ask < quote.bid:
                    raise ValueError("A required option quote is stale or crossed")
                price = quote.ask if side == "buy" else quote.bid
                size = quote.ask_size if side == "buy" else quote.bid_size
            if price is None or price <= 0:
                raise ValueError("Executable bid/ask is unavailable")
            if size is not None and units > size:
                raise ValueError("Paper quantity exceeds displayed size; reduce it")
            net_debit += price * units * leg["multiplier"] * (1 if side == "buy" else -1)
            fills.append(TradeFill(plan_id=plan_id, contract_id=code, side=side,
                quantity=units, price=price, fees=fee * units if leg["right"] != "stock" else 0,
                filled_at=now, intent=intent, note="Simulated at ask for buys/bid for sells; "
                "simultaneous leg availability assumed, not a broker combo fill guarantee.").model_dump(mode="json"))
        if not fills:
            raise ValueError("No position is available to close")
        if limit_price is not None and net_debit > limit_price:
            raise ValueError("The net debit limit is not executable at the quoted bid/ask")
        self.repo.add_fills(plan_id, fills, validate=validate_fills)
        return self.position(plan_id)

    def paper_settle(self, plan_id, underlying_price):
        """Close expired paper option legs at intrinsic value.

        Paper plans cannot record broker exercise/assignment, so without this an
        expired simulated position could never leave reconciliation. Share
        delivery is not simulated: in-the-money legs settle at intrinsic value.
        """
        plan = self.repo.plan(plan_id)
        if not plan or plan["ledger"] != "paper":
            raise ValueError("Expiration settlement applies to paper plans")
        price = float(underlying_price)
        if not price > 0:
            raise ValueError("Enter the underlying price at expiration")
        now = utcnow()
        fills = []
        for leg in self.position(plan_id)["legs"]:
            if leg["right"] == "stock" or not leg.get("expiry") or datetime.fromisoformat(leg["expiry"]) > now:
                continue
            strike = float(leg["strike"])
            intrinsic = max(price - strike, 0.0) if leg["right"] == "call" else max(strike - price, 0.0)
            fills.append(TradeFill(plan_id=plan_id, contract_id=leg["contract_id"],
                side="sell" if leg["signed_quantity"] > 0 else "buy", quantity=abs(leg["signed_quantity"]),
                price=round(intrinsic, 4), fees=0, filled_at=now, intent="close",
                note=f"Paper expiration settlement at intrinsic value for underlying {price:g}; "
                     "share delivery is not simulated.").model_dump(mode="json"))
        if not fills:
            raise ValueError("No expired paper option legs remain to settle")
        self.repo.add_fills(plan_id, fills, validate=validate_fills)
        return self.position(plan_id)

    def position(self, plan_id):
        plan = self.repo.plan(plan_id)
        if not plan:
            raise ValueError("Plan does not exist")
        expiry = next((datetime.fromisoformat(leg["expiry"]).date()
                       for leg in plan["candidate"]["legs"] if leg.get("expiry")), None)
        with self._lock:
            snapshot = self._latest.get((plan["data_mode"], plan["candidate"]["underlying"], expiry))
            if snapshot is None:
                snapshot = self._latest.get((plan["data_mode"], plan["candidate"]["underlying"]))
        return position_from_fills(plan, self.repo.fills(plan_id), snapshot)

    def positions(self):
        return [self.position(plan["id"]) for plan in self.repo.plans(limit=None)]

    def reconcile(self, plan_id, notes):
        if not notes.strip():
            raise ValueError("Provide the broker reconciliation details")
        self.repo.reconcile_plan(plan_id, notes)
        return self.position(plan_id)

    def outcomes(self):
        positions = self.positions()
        result = {}
        # Synthetic replay fills are reported separately so they never blend
        # into paper results simulated against live quotes.
        buckets = {"paper": ("paper", "live"), "paper_replay": ("paper", "replay"),
                   "manual_live": ("manual_live", "live")}
        for name, (ledger, mode) in buckets.items():
            closed = [p for p in positions if p["ledger"] == ledger and p["plan"].get("data_mode") == mode
                      and p["status"] == "closed"]
            result[name] = {"closed_trades": len(closed),
                "realized_pnl": sum(p["realized_pnl"] for p in closed),
                "win_rate": sum(p["realized_pnl"] > 0 for p in closed) / len(closed) if closed else None,
                "note": "Synthetic replay data; not market results." if mode == "replay" else
                        "Recorded positions only; paper fills are simulations, not real executions."}
        return result

    def stop(self):
        if self.worker:
            self.worker.stop()
        with self._lock:
            for cancel, _ in self._jobs.values():
                cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for provider in self.providers.values():
            provider.close()
