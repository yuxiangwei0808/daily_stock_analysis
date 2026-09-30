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
    # A record for another slot means the latest slot never ran: see the pinned-clock test below.


def test_a_run_whose_worker_is_still_alive_is_not_started_again(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    times = ["00:00"]
    now = datetime.now()
    slot = rs._latest_slot(times, now)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        rs._write_run_record({"slot": slot, "status": "started", "attempts": 1, "pid": child.pid,
                              "started_at": (now - timedelta(minutes=1)).isoformat()})
        assert rs.RuntimeSchedulerService._interrupted_slot(SimpleNamespace(), times) is False
    finally:
        child.kill()
        child.wait()
    assert rs.RuntimeSchedulerService._interrupted_slot(SimpleNamespace(), times) is True


def test_the_worker_marks_its_own_run_finished(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    rs._write_run_record({"slot": "2026-09-25 16:10", "status": "started", "pid": os.getpid(),
                          "started_at": datetime.now().isoformat(), "attempts": 1})
    monkeypatch.setattr(rs.os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(rs, "_setup_child_logging", lambda: None)
    monkeypatch.setattr(rs.RuntimeSchedulerService, "__init__", lambda self, **kwargs: setattr(self, "_last_error", None))
    monkeypatch.setattr(rs.RuntimeSchedulerService, "_run_analysis_locked", lambda self, codes: True)
    sent = []
    rs._run_scheduled_analysis_process(SimpleNamespace(put=sent.append), None, {})
    assert rs._read_run_record()["status"] == "finished" and sent == [{"success": True, "error": None}]


def _pin_clock(monkeypatch, now):
    class Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)
    monkeypatch.setattr(rs, "datetime", Pinned)
    import src.scheduler as scheduler_module
    return scheduler_module


def test_a_slot_missed_while_the_server_was_down_starts_if_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    times = ["09:40", "12:00", "16:10"]
    rs._write_run_record({"slot": "2026-09-29 09:40", "status": "finished", "started_at": "2026-09-29T09:40:00"})
    _pin_clock(monkeypatch, datetime(2026, 9, 29, 12, 3))
    assert rs.RuntimeSchedulerService._interrupted_slot(SimpleNamespace(), times) is True  # 12:00 never ran
    _pin_clock(monkeypatch, datetime(2026, 9, 29, 13, 30))
    assert rs.RuntimeSchedulerService._interrupted_slot(SimpleNamespace(), times) is False  # too late now


def test_runs_are_labelled_only_within_the_slot_window():
    assert rs._slot_label(["09:40", "12:00"], datetime(2026, 9, 29, 12, 10)) == "12:00"
    assert rs._slot_label(["09:40", "12:00"], datetime(2026, 9, 29, 15, 0)) is None  # a late manual start
    assert "scheduled_slot" in rs.SCHEDULE_ARGS_OVERRIDE_KEYS  # reaches the child process


def test_the_child_process_keeps_the_scheduled_slot():
    service = rs.RuntimeSchedulerService(schedule_args_overrides={"scheduled_slot": "12:00", "unknown": 1})
    assert service._make_schedule_args().scheduled_slot == "12:00"


def test_a_pushed_run_is_not_caught_up_again(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    times = ["09:40", "12:00", "16:10"]
    rs._write_run_record({"slot": "2026-09-29 12:00", "status": "started", "pid": os.getpid(),
                          "started_at": "2026-09-29T12:00:05", "attempts": 1})
    rs.mark_run_pushed()
    assert rs._read_run_record()["status"] == "pushed"
    _pin_clock(monkeypatch, datetime(2026, 9, 29, 12, 20))
    assert rs.RuntimeSchedulerService._interrupted_slot(SimpleNamespace(), times) is False
    rs._write_run_record({"slot": "2026-09-29 12:00", "status": "started", "pid": os.getpid() + 1, "attempts": 1,
                          "started_at": "2026-09-29T12:00:05"})
    rs.mark_run_pushed()  # another process's run is left alone
    assert rs._read_run_record()["status"] == "started"
