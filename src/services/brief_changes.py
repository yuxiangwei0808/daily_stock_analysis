"""Changes-only briefs for later scheduled runs of the day (``BRIEF_CHANGES_ONLY_TIMES``).

In a listed slot, the pushed brief keeps only stocks whose call changed since
the day's earlier run: a different decision bucket (buy / sell / watch) or a
score move of at least ``SCORE_STEP`` points; stocks not analysed earlier today
count as changed. The saved report files stay complete.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCORE_STEP = 10


def _bucket(kind: str) -> str:
    return kind if kind in ("buy", "sell") else "watch"


def changes_only(results: List[Any], db: Any, config: Any, slot: Optional[str],
                 started_at: Optional[datetime]) -> Optional[Tuple[List[Any], str]]:
    """(changed results, header) for a scheduled run in a changes-only slot, else None (full brief).

    ``slot`` is the scheduled "HH:MM" (None for manual runs); ``started_at`` bounds the history
    compared against, so this run's own freshly saved rows are never its own baseline.
    """
    from src.report_language import infer_decision_type_from_advice

    slots = list(getattr(config, "brief_changes_only_times", None) or [])
    if not slot or slot not in slots or started_at is None or len(results) < 2:
        return None
    try:
        earlier = db.get_analysis_history(days=1, limit=1000)
    except Exception as exc:  # without history the full brief goes out
        logger.info("Changes-only brief unavailable: %s", type(exc).__name__)
        return None
    latest = {}
    for row in sorted(earlier, key=lambda item: item.id):
        if row.created_at and row.created_at < started_at and row.created_at.date() == started_at.date():
            latest[row.code] = row
    if not latest:
        return None  # the day's first run: nothing to compare with
    language = getattr(config, "report_language", "zh") or "zh"
    changed = []
    for result in results:
        before = latest.get(result.code)
        if before is None:
            changed.append(result)
            continue
        # Both sides through the same mapping, so wording differences are not "changes".
        now_kind = _bucket(infer_decision_type_from_advice(getattr(result, "operation_advice", ""), default="hold"))
        then_kind = _bucket(infer_decision_type_from_advice(before.operation_advice, default="hold"))
        if now_kind != then_kind or abs((result.sentiment_score or 50) - (before.sentiment_score or 50)) >= SCORE_STEP:
            changed.append(result)
    since = max(row.created_at for row in latest.values()).strftime("%H:%M")
    if language == "zh":
        header = (f"_{slot} 更新：仅列出自 {since} 以来结论变化的股票（{len(changed)}/{len(results)}）。_" if changed
                  else f"_{slot} 更新：自 {since} 以来 {len(results)} 只股票结论均无变化。_")
    else:
        header = (f"_{slot} update: only calls that changed since {since} ({len(changed)} of {len(results)})._" if changed
                  else f"_{slot} update: no call changed since {since} ({len(results)} stocks)._")
    return changed, header
