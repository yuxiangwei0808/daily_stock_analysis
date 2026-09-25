"""Options ideas for the strongest calls of each scheduled stock-report run.

Enabled with ``TRADE_DESK_REPORT_IDEAS`` (number of stocks per run, default 0 =
off). During the regular session, once a report batch has finished, the Trade
Desk compares options strategies for the reports with the strongest directional
view (routine model, live quotes, the report as evidence) and sends one Discord
summary. Nothing is traded; ideas are research only.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from .models import TradeAdviceRequest

logger = logging.getLogger(__name__)

_US_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
MIN_BATCH = 5  # a scheduled run, not a one-off analysis
SETTLE = timedelta(minutes=3)
WINDOW = timedelta(minutes=60)
JOB_TIMEOUT = timedelta(minutes=30)
CHECK_EVERY = timedelta(seconds=60)  # the worker ticks every 5 s; reports change slowly


def ideas_per_run() -> int:
    try:
        return max(0, min(10, int(os.getenv("TRADE_DESK_REPORT_IDEAS", "0") or 0)))
    except ValueError:
        return 0


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _legs(candidate: Dict[str, Any]) -> str:
    parts = []
    for leg in candidate.get("legs") or []:
        side = "buy" if leg.get("side") == "buy" else "sell"
        if leg.get("right") == "stock":
            parts.append(f"{side} {leg.get('quantity', '')} shares")
            continue
        expiry = str(leg.get("expiry") or "")[5:10].replace("-", "/")
        strike = leg.get("strike")
        strike_text = f"{float(strike):g}" if strike is not None else "?"
        parts.append(f"{side} {strike_text}{'C' if leg.get('right') == 'call' else 'P'} {expiry}".strip())
    return " / ".join(parts)


def format_idea(job: Dict[str, Any], report: Dict[str, Any]) -> List[str]:
    """Two short lines per stock: the trade (or wait) and the reason."""
    ticker = job["request"]["ticker"]
    view = f"{report.get('advice') or ''} · score {report.get('score')}".strip(" ·")
    explanation = " ".join(str(job.get("explanation") or "").split())
    reason = (re.split(r"(?<=[.!?。])\s", explanation, maxsplit=1)[0] if explanation else "")[:240]
    candidates = job.get("candidates") or []
    if job.get("status") not in {"completed", "stale"}:
        return [f"**{ticker}** ({view}) — no comparison: {str(job.get('error') or job.get('status'))[:120]}"]
    if job.get("assessment") != "compare" or not candidates:
        return [f"**{ticker}** ({view}) — wait", *([f"> {reason}"] if reason else [])]
    best = candidates[0]
    payoff = best.get("payoff") or {}
    debit = payoff.get("entry_debit")
    cost = (f"debit {_money(debit)}" if (debit or 0) >= 0 else f"credit {_money(-debit)}") if debit is not None else ""
    probability = (best.get("probability") or {}).get("probability_of_profit")
    facts = [cost, f"max loss {_money(payoff.get('max_loss'))}" if payoff.get("max_loss") is not None else "max loss unbounded",
             f"max gain {_money(payoff.get('max_gain'))}" if payoff.get("max_gain") is not None else "max gain unbounded",
             "breakeven " + ", ".join(f"{float(b):g}" for b in (payoff.get("breakevens") or [])[:2]) if payoff.get("breakevens") else "",
             f"P(profit) {probability:.0%}" if isinstance(probability, (int, float)) else ""]
    stale = " (prices moved; re-check)" if job.get("status") == "stale" else ""
    return [f"**{ticker}** ({view}) — {best.get('title', best.get('strategy'))}: {_legs(best)}{stale}",
            "   " + " · ".join(fact for fact in facts if fact),
            *([f"> {reason}"] if reason else [])]


class ReportIdeas:
    def __init__(self, service, emit: Callable[[str, Dict[str, Any], str], Any],
                 now: Callable[[], datetime] = datetime.now):
        self.service = service
        self.emit = emit
        self._now = now
        self._last_id: Optional[int] = None
        self._pending: Optional[Dict[str, Any]] = None
        self._next_check: Optional[datetime] = None

    def tick(self, regular_session: bool) -> None:
        limit = ideas_per_run()
        if limit <= 0:
            return
        now = self._now()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + CHECK_EVERY
        if self._pending is not None:
            self._finish()
            return
        rows = self.service.repo.db.get_analysis_history(days=1, limit=100)
        if self._last_id is None:  # a restart does not re-announce earlier runs
            self._last_id = max((row.id for row in rows), default=0)
            return
        batch = [row for row in rows if row.id > self._last_id and _US_TICKER.match(str(row.code or ""))
                 and row.created_at and now - row.created_at <= WINDOW]
        if len(batch) < MIN_BATCH or now - max(row.created_at for row in batch) < SETTLE:
            return
        self._last_id = max(row.id for row in batch)
        if not regular_session:
            return  # option quotes are not tradable outside the regular session
        seen, picks = set(), []
        for row in sorted(batch, key=lambda item: abs((item.sentiment_score or 50) - 50), reverse=True):
            if row.code not in seen:
                seen.add(row.code)
                picks.append(row)
        jobs = {}
        for row in picks[:limit]:
            score = row.sentiment_score or 50
            direction = "bullish" if score >= 60 else "bearish" if score <= 40 else "neutral"
            summary = " ".join(str(row.analysis_summary or "").split())[:400]
            try:
                request = TradeAdviceRequest(
                    ticker=row.code, data_mode="live", horizon="both", direction=direction, source_report_id=row.id,
                    message=f"Suggest options trades that fit today's stock report ({row.operation_advice}, "
                            f"score {score}): {summary} Recommend waiting if no candidate fits.")
                job = self.service.submit(request, source="report")
            except Exception as exc:
                logger.info("Report idea for %s not submitted: %s", row.code, type(exc).__name__)
                continue
            jobs[job["id"]] = {"advice": row.operation_advice, "score": score}
        if jobs:
            self._pending = {"jobs": jobs, "batch": self._last_id, "started": now}

    def _finish(self) -> None:
        pending = self._pending
        jobs = [self.service.repo.advice(job_id) for job_id in pending["jobs"]]
        done = [job for job in jobs if job and job["status"] not in {"queued", "running"}]
        if len(done) < len(jobs) and self._now() - pending["started"] < JOB_TIMEOUT:
            return
        self._pending = None
        lines = []
        for job in done:
            lines.extend(format_idea(job, pending["jobs"][job["id"]]))
        if lines:
            self.emit("options_ideas", {"underlying": "", "message": "\n".join(lines)},
                      f"options-ideas:{pending['batch']}")

