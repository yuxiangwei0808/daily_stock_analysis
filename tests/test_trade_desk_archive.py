from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.v1.endpoints.trade_desk import _advice_page
from src.services.trade_desk import service
from src.services.trade_desk.models import TradeAdviceRequest

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)


def _job(created, status="completed", expiries=(), job_id="a"):
    legs = [{"expiry": e} for e in expiries]
    return {"id": job_id, "status": status, "created_at": created.isoformat(),
            "candidates": [{"legs": legs}] if expiries else []}


def test_archive_reasons():
    assert service.archive_reason(_job(NOW - timedelta(days=30), status="running"), NOW) == ""
    assert service.archive_reason(_job(NOW - timedelta(hours=1), status="stale", expiries=["2026-10-02T20:00:00Z"]), NOW) == "stale"
    assert service.archive_reason(_job(NOW - timedelta(days=2), expiries=["2026-09-25T20:00:00Z"]), NOW) == "expired"
    # A job with one leg still open is current.
    assert service.archive_reason(_job(NOW - timedelta(days=2), expiries=["2026-09-25T20:00:00Z", "2026-10-02T20:00:00Z"]), NOW) == ""
    # Empty results stay for the rest of their New York day.
    assert service.archive_reason(_job(NOW - timedelta(hours=1)), NOW) == ""
    assert service.archive_reason(_job(NOW - timedelta(days=1)), NOW) == "no_result"
    assert service.archive_reason(_job(NOW - timedelta(days=8), expiries=["2026-12-18T21:00:00Z"]), NOW) == "old"


class _Repo:
    def __init__(self, jobs):
        self.jobs = sorted(jobs, key=lambda j: j["created_at"], reverse=True)
        self.calls = 0

    def advice_list(self, limit, offset=0):
        self.calls += 1
        return self.jobs[offset:offset + limit]

    def advice_count(self):
        return len(self.jobs)


def test_advice_page_splits_active_and_archive(monkeypatch):
    monkeypatch.setattr("api.v1.endpoints.trade_desk.utcnow", lambda: NOW)
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    jobs = [_job(NOW - timedelta(hours=i), expiries=["2026-10-02T20:00:00Z"], job_id=f"live{i}") for i in range(3)]
    jobs += [_job(NOW - timedelta(days=20 + i), expiries=["2026-09-01T20:00:00Z"], job_id=f"old{i}") for i in range(500)]
    repo = _Repo(jobs)
    active = _advice_page(repo, 100, "active", batch=50)
    assert [j["id"] for j in active["items"]] == ["live0", "live1", "live2"]
    assert active["counts"] == {"active": 3, "archive": 500}
    assert repo.calls == 1  # stops once past the active window
    archive = _advice_page(_Repo(jobs), 10, "archive", batch=50)
    assert len(archive["items"]) == 10 and all(j["archived"] == "expired" for j in archive["items"])


def test_empty_result_says_why():
    request = TradeAdviceRequest(ticker="SOXS", message="entry and stop for a day call?")
    snapshot = SimpleNamespace(options=[], warnings=["expiry_unverified_or_unavailable", "spot_from_valid_bid_ask_midpoint"])
    text = service._no_candidates_reason(snapshot, request)
    assert "no quotable SOXS options" in text and "expiry_unverified_or_unavailable" in text
    assert "spot_from" not in text and "model was not asked" in text
    snapshot = SimpleNamespace(options=[1, 2, 3], warnings=[])
    assert "3 SOXS option quotes came back" in service._no_candidates_reason(snapshot, TradeAdviceRequest(ticker="SOXS"))
