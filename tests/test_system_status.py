"""System status: component states for the Status page, and the Discord watchdog."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.trade_desk import status as st

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


def _service(**overrides):
    worker = SimpleNamespace(
        status=lambda: {"running": True, "leader": True, "last_tick": (NOW - timedelta(seconds=5)).isoformat(), "error": None},
        part_status={"plans": {"ok_at": NOW.isoformat(), "error": None},
                     "pulse": {"ok_at": (NOW - timedelta(minutes=9)).isoformat(), "error": "OperationalError",
                               "error_at": (NOW - timedelta(minutes=9)).isoformat()}},
        _holdings=SimpleNamespace(errors={}, error_at={}), _pulse=object(), _opportunities=None, _breakouts=None,
        _tracker=SimpleNamespace(last_error=None, _done_day=date(2026, 9, 29)),
        _youtube=SimpleNamespace(last_error=None, last_pass_at=(NOW - timedelta(hours=1)).isoformat(), last_pass_added=3,
                                 captions_paused=lambda: True),
        _social=None)
    service = SimpleNamespace(
        worker=worker, version="abc12345", started_at=(NOW - timedelta(hours=2)).isoformat(),
        health=lambda: {"live": {"available": True, "message": "moomoo OpenD, verified quotes"}},
        scheduler_status=lambda: {"enabled": True, "running": False, "next_run_at": "2026-09-30T12:00:00-04:00",
                                  "last_run_at": "2026-09-30T09:40:00-04:00", "last_success_at": "2026-09-30T09:52:00-04:00",
                                  "last_error": None},
        holdings=SimpleNamespace(raw=lambda: {"synced_at": (NOW - timedelta(minutes=4)).isoformat()}),
        repo=SimpleNamespace(preferences=lambda: {"discord_enabled": False}))
    for key, value in overrides.items():
        setattr(service, key, value)
    return service


def test_components_report_their_state_and_detail(monkeypatch):
    monkeypatch.setattr(st, "code_version", lambda root=None: "abc12345")
    report = st.build(_service(), NOW)
    states = {item["key"]: (item["state"], item["detail"]) for item in report["components"]}
    assert states["version"] == ("ok", "abc12345, started 2 h ago")
    assert states["opend"] == ("ok", "moomoo OpenD, verified quotes")
    assert states["scheduler"][0] == "ok" and "next Wed 12:00 NY" in states["scheduler"][1]
    assert states["worker"] == ("ok", "last pass 5s ago")
    assert states["pulse"] == ("error", "OperationalError since 9 min ago")
    assert states["holdings"] == ("ok", "synced 4 min ago")
    assert states["opportunities"][0] == "off" and states["social"][0] == "off"
    assert states["youtube"] == ("warn", "last pass 60 min ago, 3 new picks; YouTube is rate-limiting captions, waiting")
    assert states["tracker"] == ("ok", "settled 2026-09-29")
    assert states["discord"][0] == "off"
    assert report["overall"] == "error"


def test_new_code_on_disk_asks_for_a_restart_and_a_stalled_worker_is_an_error(monkeypatch):
    monkeypatch.setattr(st, "code_version", lambda root=None: "def67890")
    service = _service()
    service.worker.status = lambda: {"running": True, "leader": True, "last_tick": (NOW - timedelta(minutes=6)).isoformat()}
    service.scheduler_status = lambda: {"enabled": True, "last_run_at": "2026-09-30T09:40:00-04:00",
                                        "last_success_at": "2026-09-29T16:20:00-04:00", "last_error": "timeout after 3600s"}
    states = {item["key"]: (item["state"], item["detail"]) for item in st.build(service, NOW)["components"]}
    assert states["version"] == ("warn", "running abc12345; def67890 is on disk — restart to load it")
    assert states["worker"] == ("error", "no pass for 6 min")
    assert states["scheduler"][0] == "error" and "last run failed: timeout after 3600s" in states["scheduler"][1]


def test_git_version_is_read_from_the_checkout(tmp_path):
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text("0123456789abcdef\n")
    assert st.code_version(tmp_path) == "01234567"
    (tmp_path / ".git" / "refs" / "heads" / "main").unlink()
    (tmp_path / ".git" / "packed-refs").write_text("# pack\nfedcba9876543210 refs/heads/main\n")
    assert st.code_version(tmp_path) == "fedcba98"
    assert st.code_version(tmp_path / "missing") is None


def test_the_watchdog_reports_the_start_and_a_lasting_failure_once_a_day():
    sent = []
    clock = {"t": 0.0}
    reports = iter([
        {"components": [{"key": "opend", "label": "moomoo OpenD quotes", "state": "ok", "detail": "connected"},
                        {"key": "pulse", "label": "Market pulse", "state": "error", "detail": "OperationalError since 1 min ago"}]},
        {"components": [{"key": "pulse", "label": "Market pulse", "state": "error", "detail": "OperationalError since 6 min ago"}]},
        {"components": [{"key": "pulse", "label": "Market pulse", "state": "error", "detail": "OperationalError since 11 min ago"}]},
    ])
    service = SimpleNamespace(started_at=(NOW - timedelta(minutes=1)).isoformat(), version="abc12345")
    watch = st.StatusWatch(service, lambda kind, payload, key: sent.append((key, payload["message"])),
                           build_status=lambda svc, now: next(reports), clock=lambda: clock["t"])
    watch.tick(NOW)
    assert sent == []  # too soon after the start
    watch.tick(NOW + timedelta(minutes=2))
    assert sent == [(f"status-start:{service.started_at}",
                     "⚠️ **Server started** · abc12345 · 1 need attention\n🔴 Market pulse: OperationalError since 1 min ago")]
    watch.tick(NOW + timedelta(minutes=3))
    assert len(sent) == 1  # checks every five minutes
    clock["t"] += st.StatusWatch.CHECK_SECONDS
    watch.tick(NOW + timedelta(minutes=7))
    assert sent[-1] == ("status:pulse:2026-09-30", "🔴 **Market pulse** · OperationalError since 6 min ago")
    clock["t"] += st.StatusWatch.CHECK_SECONDS
    watch.tick(NOW + timedelta(minutes=12))
    assert len(sent) == 2  # reported once
