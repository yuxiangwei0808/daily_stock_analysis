"""A scheduled run cut off by a restart starts again when the server comes back soon enough."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.services import runtime_scheduler as rs


def test_latest_slot_is_the_last_time_already_passed():
    now = datetime(2026, 9, 25, 12, 30)
    assert rs._latest_slot(["09:40", "12:00", "16:10"], now) == "2026-09-25 12:00"
    assert rs._latest_slot(["09:40"], datetime(2026, 9, 25, 9, 0)) is None


def test_only_a_recent_interrupted_latest_slot_is_resumed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    service = SimpleNamespace()
    check = rs.RuntimeSchedulerService._interrupted_slot
    times = ["09:40", "12:00", "16:10"]
    now = datetime.now()
    slot = rs._latest_slot(times, now)
    if slot is None:  # before the first slot of the day: nothing to resume
        assert check(service, times) is False
        return
    started = now - timedelta(minutes=5)
    rs._write_run_record({"slot": slot, "status": "started", "started_at": started.isoformat(), "attempts": 1})
    assert check(service, times) is True
    rs._write_run_record({"slot": slot, "status": "finished", "started_at": started.isoformat(), "attempts": 1})
    assert check(service, times) is False
    rs._write_run_record({"slot": slot, "status": "started", "started_at": started.isoformat(), "attempts": 3})
    assert check(service, times) is False  # already retried twice
    old = now - timedelta(minutes=rs.SCHEDULE_CATCHUP_MINUTES + 5)
    rs._write_run_record({"slot": slot, "status": "started", "started_at": old.isoformat(), "attempts": 1})
    assert check(service, times) is False  # too late to be useful
    rs._write_run_record({"slot": "2000-01-01 09:40", "status": "started", "started_at": started.isoformat()})
    assert check(service, times) is False  # a different slot
