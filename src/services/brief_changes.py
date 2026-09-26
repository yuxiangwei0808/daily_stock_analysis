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


def changes_only(results: List[Any], db: Any, config: Any, query_id: str,
                 now: Optional[datetime] = None) -> Optional[Tuple[List[Any], str]]:
    """(changed results, header) in a changes-only slot, else None (push the full brief)."""
    from src.report_language import infer_decision_type_from_advice
    from src.scheduler import current_slot
    from src.schemas.decision_action import display_decision_type_for_result

    slots = list(getattr(config, "brief_changes_only_times", None) or [])
    slot = current_slot(list(getattr(config, "schedule_times", None) or []), now) if slots else None
    if not slot or slot not in slots or len(results) < 2:
        return None
    try:
        earlier = db.get_analysis_history(days=1, limit=1000, exclude_query_id=query_id)
    except Exception as exc:  # without history the full brief goes out
        logger.info("Changes-only brief unavailable: %s", type(exc).__name__)
        return None
    today = (now or datetime.now()).date()
    latest = {}
    for row in sorted(earlier, key=lambda item: item.id):
        if row.created_at and row.created_at.date() == today:
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
        now_kind = _bucket(display_decision_type_for_result(result, report_language=language))
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
