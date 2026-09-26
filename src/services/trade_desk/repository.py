"""Additive Trade Desk persistence, transactions, event replay, and worker lease."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import Column, Integer, String, Text, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base

from .models import identity, utcnow

Base = declarative_base()


class ConversationRecord(Base):
    __tablename__ = "trade_desk_conversations"
    id = Column(String(64), primary_key=True)
    created_at = Column(String(40), nullable=False)


class AdviceRecord(Base):
    __tablename__ = "trade_desk_advice"
    id = Column(String(64), primary_key=True)
    conversation_id = Column(String(64), nullable=False, index=True)
    status = Column(String(24), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False, index=True)


class PlanRecord(Base):
    __tablename__ = "trade_desk_plans"
    id = Column(String(64), primary_key=True)
    payload = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(String(40), nullable=False, index=True)


class TrackedIdeaRecord(Base):
    """A trade idea or breakout alert followed forward to its stop, target or time exit."""
    __tablename__ = "trade_desk_tracked_ideas"
    id = Column(String(120), primary_key=True)
    status = Column(String(16), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False, index=True)


class FillRecord(Base):
    __tablename__ = "trade_desk_fills"
    id = Column(String(64), primary_key=True)
    plan_id = Column(String(64), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False, index=True)


class EventRecord(Base):
    __tablename__ = "trade_desk_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False)
    dedup_key = Column(String(200), nullable=True, unique=True)
    payload = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False, index=True)


class SettingsRecord(Base):
    __tablename__ = "trade_desk_settings"
    id = Column(String(64), primary_key=True)
    payload = Column(Text, nullable=False)


class LeaseRecord(Base):
    __tablename__ = "trade_desk_leases"
    id = Column(String(64), primary_key=True)
    owner = Column(String(64), nullable=False)
    expires_at = Column(String(40), nullable=False)


def encoded(value: Any) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, default=str)


class TradeDeskRepository:
    def __init__(self, db=None):
        if db is None:
            from src.storage import get_db
            db = get_db()
        self.db = db
        Base.metadata.create_all(db._engine)

    @staticmethod
    def _event(session, event_type, payload, dedup_key=None):
        event = EventRecord(event_type=event_type, payload=encoded(payload),
                            dedup_key=dedup_key, created_at=utcnow().isoformat())
        session.add(event)
        session.flush()
        return {"id": event.id, "event_type": event_type, "payload": payload,
                "created_at": event.created_at,
                "plan_id": payload.get("plan_id"), "advice_id": payload.get("advice_id")}

    def event(self, event_type, payload, dedup_key=None):
        try:
            with self.db.session_scope() as session:
                return self._event(session, event_type, payload, dedup_key)
        except IntegrityError:
            if dedup_key:
                return None
            raise

    def events(self, after=0, limit=100, newest=False, since=None, types=None):
        """Events after an id; ``since`` (ISO UTC) and ``types`` filter in SQL."""
        with self.db.get_session() as session:
            query = select(EventRecord).where(EventRecord.id > after)
            if since:
                query = query.where(EventRecord.created_at >= since)
            if types:
                query = query.where(EventRecord.event_type.in_(list(types)))
            query = query.order_by(EventRecord.id.desc() if newest else EventRecord.id)
            rows = session.execute(query.limit(limit)).scalars().all()
            result = []
            for r in rows:
                payload = json.loads(r.payload)
                result.append({"id": r.id, "event_type": r.event_type, "created_at": r.created_at,
                               "payload": payload, "plan_id": payload.get("plan_id"),
                               "advice_id": payload.get("advice_id")})
            return result

    def research_evidence(self, ticker, source_report_id=None):
        """Read stock research as attributed context, never as option probabilities."""
        evidence = []
        if hasattr(self.db, "get_analysis_history"):
            reports = self.db.get_analysis_history(code=ticker, days=3, limit=2)
            if source_report_id is not None:
                source = self.db.get_analysis_history_by_id(source_report_id)
                if source and source.code.upper() == ticker.upper():
                    reports = [source] + [row for row in reports if row.id != source.id]
            for row in reports[:3]:
                evidence.append({"kind": "report", "report_id": row.id, "ticker": row.code,
                    "created_at": str(row.created_at), "summary": (row.analysis_summary or "")[:3000],
                    "operation_advice": row.operation_advice, "stock_score": row.sentiment_score,
                    "meaning": "Existing stock research; stock score is not an options win probability."})
        from sqlalchemy import inspect
        if inspect(self.db._engine).has_table("decision_signals"):
            from src.storage import DecisionSignalRecord
            with self.db.get_session() as session:
                rows = session.execute(select(DecisionSignalRecord).where(
                    DecisionSignalRecord.stock_code == ticker,
                    DecisionSignalRecord.status == "active").order_by(
                    DecisionSignalRecord.created_at.desc()).limit(3)).scalars().all()
                for row in rows:
                    evidence.append({"kind": "signal", "signal_id": row.id,
                        "source_report_id": row.source_report_id, "action": row.action,
                        "reason": (row.reason or "")[:2000], "created_at": str(row.created_at),
                        "expires_at": str(row.expires_at) if row.expires_at else None,
                        "meaning": "Existing stock decision signal; not an options probability or execution instruction."})
        return evidence

    def create_advice(self, request, source="manual"):
        now = utcnow().isoformat()
        advice_id = identity()
        parent_id = request.get("parent_advice_id")
        with self.db.session_scope() as session:
            parent = session.get(AdviceRecord, parent_id) if parent_id else None
            if parent_id and parent is None:
                raise ValueError("Previous advice does not exist")
            conversation_id = parent.conversation_id if parent else identity()
            if not parent:
                session.add(ConversationRecord(id=conversation_id, created_at=now))
            payload = {"id": advice_id, "conversation_id": conversation_id,
                       "parent_advice_id": parent_id, "request": request,
                       "status": "queued", "source": source, "candidates": [],
                       "assessment": "Preparing comparison", "explanation": "",
                       "created_at": now, "updated_at": now, "error": None}
            session.add(AdviceRecord(id=advice_id, conversation_id=conversation_id,
                                    status="queued", payload=encoded(payload), created_at=now))
            self._event(session, "advice_queued", {"advice_id": advice_id})
            return payload

    def advice(self, advice_id):
        with self.db.get_session() as session:
            row = session.get(AdviceRecord, advice_id)
            return json.loads(row.payload) if row else None

    def advice_list(self, limit=100):
        with self.db.get_session() as session:
            rows = session.execute(select(AdviceRecord).order_by(
                AdviceRecord.created_at.desc()).limit(limit)).scalars().all()
            return [json.loads(row.payload) for row in rows]

    def update_advice(self, advice_id, changes, unless_cancelled=True, only_statuses=None):
        with self.db.session_scope() as session:
            # Acquire the write lock before reading to serialize cancel/completion.
            where = [AdviceRecord.id == advice_id]
            if unless_cancelled:
                where.append(AdviceRecord.status != "cancelled")
            if only_statuses:
                where.append(AdviceRecord.status.in_(list(only_statuses)))
            result = session.execute(update(AdviceRecord).where(*where).values(
                status=AdviceRecord.status))
            if result.rowcount != 1:
                return None
            row = session.get(AdviceRecord, advice_id)
            payload = json.loads(row.payload)
            payload.update(changes, updated_at=utcnow().isoformat())
            row.status = payload["status"]
            row.payload = encoded(payload)
            self._event(session, "advice_" + payload["status"], {"advice_id": advice_id})
            return payload

    def recover_jobs(self, exclude=()):
        for job in self.advice_list(1000):
            if job["status"] in {"queued", "running"} and job["id"] not in exclude:
                self.update_advice(job["id"], {"status": "failed", "error":
                    "The advice worker restarted; request a fresh comparison."})

    def add_plan(self, payload):
        with self.db.session_scope() as session:
            session.add(PlanRecord(id=payload["id"], payload=encoded(payload),
                                   created_at=payload["created_at"]))
            self._event(session, "plan_created", {"plan_id": payload["id"],
                                                  "advice_id": payload["advice_id"]})
        return payload

    def plan(self, plan_id):
        with self.db.get_session() as session:
            row = session.get(PlanRecord, plan_id)
            return json.loads(row.payload) if row else None

    def plans(self, limit=250):
        with self.db.get_session() as session:
            rows = session.execute(select(PlanRecord).order_by(
                PlanRecord.created_at.desc()).limit(limit)).scalars().all()
            return [json.loads(row.payload) for row in rows]

    def update_plan(self, plan_id, changes):
        with self.db.session_scope() as session:
            result = session.execute(update(PlanRecord).where(PlanRecord.id == plan_id).values(
                revision=PlanRecord.revision + 1))
            if not result.rowcount:
                raise ValueError("Plan does not exist")
            row = session.get(PlanRecord, plan_id)
            payload = json.loads(row.payload)
            payload.update(changes, updated_at=utcnow().isoformat())
            row.payload = encoded(payload)
            self._event(session, "plan_updated", {"plan_id": plan_id, "changes": changes})
            return payload

    def fills(self, plan_id):
        with self.db.get_session() as session:
            rows = session.execute(select(FillRecord).where(FillRecord.plan_id == plan_id)
                                   .order_by(FillRecord.created_at, FillRecord.id)).scalars().all()
            from .ledger import sort_fills
            return sort_fills(json.loads(row.payload) for row in rows)

    def add_fills(self, plan_id, fills, validate=None):
        with self.db.session_scope() as session:
            session.execute(update(PlanRecord).where(PlanRecord.id == plan_id).values(
                revision=PlanRecord.revision + 1))
            row = session.get(PlanRecord, plan_id)
            if row is None:
                raise ValueError("Plan does not exist")
            plan = json.loads(row.payload)
            prior = [json.loads(r.payload) for r in session.execute(select(FillRecord).where(
                FillRecord.plan_id == plan_id).order_by(FillRecord.created_at, FillRecord.id)).scalars()]
            if validate:
                validate(plan, prior, fills)
            next_sequence = max((item.get("sequence", 0) for item in prior), default=0)
            fills = [dict(item, sequence=next_sequence + index + 1) for index, item in enumerate(fills)]
            for fill in fills:
                session.add(FillRecord(id=fill["id"], plan_id=plan_id,
                                       payload=encoded(fill), created_at=fill["filled_at"]))
                self._event(session, "fill_recorded", {"plan_id": plan_id, "fill": fill})
            from .ledger import position_from_fills
            if any(fill["intent"] in {"exercise", "assignment"} for fill in fills):
                plan["status"] = "reconciliation_required"
            status = position_from_fills(plan, prior + fills)["status"]
            plan.update(status=status, updated_at=utcnow().isoformat())
            row.payload = encoded(plan)

    def reconcile_plan(self, plan_id, notes):
        from .ledger import position_from_fills
        with self.db.session_scope() as session:
            result = session.execute(update(PlanRecord).where(PlanRecord.id == plan_id).values(
                revision=PlanRecord.revision + 1))
            if not result.rowcount:
                raise ValueError("Plan does not exist")
            row = session.get(PlanRecord, plan_id)
            plan = json.loads(row.payload)
            fills = [json.loads(item.payload) for item in session.execute(
                select(FillRecord).where(FillRecord.plan_id == plan_id)).scalars()]
            plan.update(reconciled_at=utcnow().isoformat(), status="open", notes=notes)
            status = position_from_fills(plan, fills)["status"]
            if status == "reconciliation_required":
                raise ValueError("Record all resulting shares and resolve expired option legs before reconciliation")
            plan.update(status=status, updated_at=utcnow().isoformat())
            row.payload = encoded(plan)
            self._event(session, "reconciled", {"plan_id": plan_id, "notes": notes})

    def preferences(self):
        defaults = {"discord_enabled": False}
        with self.db.get_session() as session:
            row = session.get(SettingsRecord, "preferences")
            if row:
                defaults.update(json.loads(row.payload))
        return defaults

    def set_preferences(self, changes):
        prefs = self.preferences()
        prefs.update(changes)
        with self.db.session_scope() as session:
            session.merge(SettingsRecord(id="preferences", payload=encoded(prefs)))
        return prefs

    def setting(self, key, default=None):
        with self.db.get_session() as session:
            row = session.get(SettingsRecord, key)
            return json.loads(row.payload) if row else default

    def set_setting(self, key, value):
        with self.db.session_scope() as session:
            session.merge(SettingsRecord(id=key, payload=encoded(value)))
        return value

    def track_idea(self, record_id, payload):
        """Store a new tracked idea; an existing id is left untouched (False)."""
        try:
            with self.db.session_scope() as session:
                if session.get(TrackedIdeaRecord, record_id) is not None:
                    return False
                session.add(TrackedIdeaRecord(id=record_id, status="open", payload=encoded(payload),
                                              created_at=utcnow().isoformat()))
            return True
        except IntegrityError:
            return False

    def tracked_ideas(self, status=None, since=None, limit=5000):
        with self.db.get_session() as session:
            query = select(TrackedIdeaRecord)
            if status:
                query = query.where(TrackedIdeaRecord.status == status)
            if since:
                query = query.where(TrackedIdeaRecord.created_at >= since)
            rows = session.execute(query.order_by(TrackedIdeaRecord.created_at.desc()).limit(limit)).scalars().all()
            return [{"id": row.id, "status": row.status, "created_at": row.created_at, **json.loads(row.payload)}
                    for row in rows]

    def update_tracked_idea(self, record_id, payload, status):
        with self.db.session_scope() as session:
            row = session.get(TrackedIdeaRecord, record_id)
            if row is not None:
                row.payload, row.status = encoded(payload), status

    def lease(self, owner, seconds=45):
        now = utcnow()
        expiry = (now + timedelta(seconds=seconds)).isoformat()
        with self.db.session_scope() as session:
            result = session.execute(update(LeaseRecord).where(
                LeaseRecord.id == "worker", or_(LeaseRecord.owner == owner,
                                                LeaseRecord.expires_at < now.isoformat())
            ).values(owner=owner, expires_at=expiry))
            if result.rowcount:
                return True
        try:
            with self.db.session_scope() as session:
                session.add(LeaseRecord(id="worker", owner=owner, expires_at=expiry))
            return True
        except IntegrityError:
            return False

    def release(self, owner):
        with self.db.session_scope() as session:
            session.execute(update(LeaseRecord).where(and_(LeaseRecord.id == "worker",
                LeaseRecord.owner == owner)).values(expires_at=utcnow().isoformat()))
