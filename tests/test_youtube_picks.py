"""YouTube picks: channel config, caption choice, strict pick extraction, entry timing and the scan job."""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.services import youtube_picks as yp
from src.services.trade_desk import idea_tracker as it
from src.services.trade_desk.repository import TradeDeskRepository

KEVIN = "UCUvvj5lwue7PspotMDjk5UA"
NOW = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(engine)

    @contextmanager
    def transaction():
        with sessions() as session:
            with session.begin():
                yield session

    yield TradeDeskRepository(SimpleNamespace(_engine=engine, get_session=sessions, session_scope=transaction))
    engine.dispose()


def _weekday(day):
    return day.weekday() < 5


def test_channels_need_ids_not_handles(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CHANNELS", f"Meet Kevin={KEVIN}, @MeetKevin ,美投讲美股=UCBUH38E0ngqvmTqdchWunwQ,")
    assert yp.channels() == [("Meet Kevin", KEVIN), ("美投讲美股", "UCBUH38E0ngqvmTqdchWunwQ")]
    monkeypatch.setenv("YOUTUBE_CHANNELS", "")
    assert not yp.enabled()


def test_captions_prefer_the_videos_own_language_and_uploaded_tracks():
    tracks = [{"languageCode": "ar", "kind": "asr"}, {"languageCode": "en", "kind": "asr"}, {"languageCode": "en-US"}]
    assert yp._pick_track(tracks) == {"languageCode": "en-US"}
    assert yp._pick_track([{"languageCode": "ar", "kind": "asr"}, {"languageCode": "en", "kind": "asr"}])["languageCode"] == "en"
    assert yp._pick_track([{"languageCode": "zh-Hans"}, {"languageCode": "yue"}])["languageCode"] == "zh-Hans"
    assert yp._pick_track([{"languageCode": "fr", "kind": "asr"}]) is None


def test_picks_are_validated_and_capped():
    answer = {"picks": [
        {"ticker": "$nvda", "stance": "bullish", "conviction": "high", "horizon": "long", "reason": "AI  demand  keeps growing"},
        {"ticker": "NVDA", "stance": "bearish"},  # a duplicate
        {"ticker": "brk-b", "stance": "bearish", "conviction": "extreme", "horizon": "forever"},
        {"ticker": "BITCOIN1", "stance": "bullish"}, {"ticker": "TSLA", "stance": "neutral"}, "garbage",
    ]}
    video = {"video_id": "v1", "title": "Buy this", "description": "", "published": NOW}
    prompts = []
    picks = yp.extract_picks("Meet Kevin", video, "transcript text", lambda prompt: prompts.append(prompt) or json.dumps(answer))
    assert picks == [
        {"ticker": "NVDA", "stance": "bullish", "conviction": "high", "horizon": "long", "reason": "AI demand keeps growing"},
        {"ticker": "BRK.B", "stance": "bearish", "conviction": None, "horizon": "unspecified", "reason": ""},
    ]
    assert "Transcript:\ntranscript text" in prompts[0] and "Title: Buy this" in prompts[0]
    with pytest.raises(ValueError):  # a title alone is never read for a call
        yp.extract_picks("x", {**video, "description": "desc"}, "", lambda prompt: '{"picks": []}')
    with pytest.raises(ValueError):
        yp.extract_picks("x", video, "t", lambda prompt: "I cannot help with that")


def test_the_entry_is_the_first_close_after_publication():
    assert yp.signal_day(datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc), _weekday) == date(2026, 9, 29)  # 10:00 NY
    assert yp.signal_day(datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc), _weekday) == date(2026, 9, 30)  # 17:00 NY
    assert yp.signal_day(datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc), _weekday) == date(2026, 10, 5)  # Saturday
    assert yp.signal_day(datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc), _weekday) == date(2026, 10, 5)  # Friday 16:00


def _video(video_id, hours_ago, title="t"):
    return {"video_id": video_id, "title": title, "description": "d", "published": NOW - timedelta(hours=hours_ago)}


def test_the_scan_records_picks_once_and_retries_a_failed_video(repo):
    feed = [_video("new", 2, "new"), _video("fails", 30, "fails"), _video("spoken", 3, "spoken"),
            _video("silent", 4, "silent"), _video("old", 24 * 40, "old")]
    answers = {"new": [{"ticker": "MU", "stance": "bullish", "reason": "memory"}],
               "spoken": [{"ticker": "TSM", "stance": "bullish"}]}
    generated, transcribed = [], []

    def generate(prompt):
        video_id = prompt.split("Title: ")[1].split("\n")[0]
        generated.append(video_id)
        if video_id == "fails":
            raise RuntimeError("model down")
        return json.dumps({"picks": answers.get(video_id, [])})

    def captions(video_id):
        if video_id in {"spoken", "silent"}:
            return ""  # no captions
        return "words"

    def transcribe(video_id, should_stop):
        transcribed.append(video_id)
        return "spoken words" if video_id == "spoken" else ""
    job = yp.YouTubeScanJob(repo, channel_list=[("Meet Kevin", KEVIN)], read_feed=lambda cid: feed,
                            read_captions=captions, transcribe=transcribe, generate=generate, trading_day=_weekday)
    assert job.run(NOW) == 2
    rows = {row["ticker"]: row for row in repo.tracked_ideas()}
    assert rows["MU"]["id"] == "influencer:new:MU" and rows["MU"]["channel"] == "Meet Kevin"
    assert rows["MU"]["signal_day"] == "2026-09-30" and rows["MU"]["entry"] is None and rows["MU"]["source"] == "captions"
    assert rows["TSM"]["source"] == "audio" and {pick["ticker"] for pick in job.today_picks} == {"MU", "TSM"}
    assert "silent" not in generated  # no transcript: the title is never sent to the model
    assert set(repo.setting(yp.PROCESSED_KEY)) == {"new", "spoken"}  # failures retried; the old video never read
    generated.clear()
    transcribed.clear()
    job.run(NOW)
    assert generated == ["fails"] and transcribed == ["silent"]
    job.run(NOW)  # the third pass without a transcript drops the video
    assert "silent" in repo.setting(yp.PROCESSED_KEY)


def test_a_stop_ends_the_pass_during_a_transcription(repo):
    feed = [_video("a", 2, "a"), _video("b", 3, "b")]

    def transcribe(video_id, should_stop):
        raise InterruptedError("stopping")
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: feed,
                            read_captions=lambda vid: "", transcribe=transcribe,
                            generate=lambda prompt: '{"picks": []}', trading_day=_weekday)
    assert job.run(NOW) == 0 and not repo.setting(yp.PROCESSED_KEY)


def test_the_audio_folder_is_deleted_after_use_and_after_a_failure(monkeypatch, tmp_path):
    import yt_dlp
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    folders = []

    class FakeDownloader:
        def __init__(self, options):
            self.template = options["outtmpl"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            path = self.template.replace("%(id)s", "vid").replace("%(ext)s", "m4a")
            folders.append(path.rsplit("/", 1)[0])
            open(path, "wb").write(b"audio")
            return {"ext": "m4a"}

    class FakeModel:
        def __init__(self, fail):
            self.fail = fail

        def transcribe(self, path, **kwargs):
            assert open(path, "rb").read() == b"audio"
            if self.fail:
                raise RuntimeError("decoder")
            return iter([SimpleNamespace(text="我看好"), SimpleNamespace(text="台积电")]), None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDownloader)
    monkeypatch.setattr(yp, "_whisper", lambda: FakeModel(False))
    assert yp.transcribe_audio("vid") == "我看好台积电"
    monkeypatch.setattr(yp, "_whisper", lambda: FakeModel(True))
    with pytest.raises(RuntimeError):
        yp.transcribe_audio("vid")
    assert len(folders) == 2 and not any(Path(folder).exists() for folder in folders)
    assert list(tmp_path.iterdir()) == []
    stale = tmp_path / (yp.AUDIO_PREFIX + "crashed")
    stale.mkdir()
    (stale / "vid.m4a").write_bytes(b"audio")
    import os
    os.utime(stale, (0, 0))
    yp.clear_stale_audio()
    assert not stale.exists()


def test_recent_picks_and_channel_results(repo):
    video = _video("v1", 5)
    for ticker, stance in (("MU", "bullish"), ("TSLA", "bearish")):
        repo.track_idea(*yp.record("Meet Kevin", KEVIN, video, {"ticker": ticker, "stance": stance, "reason": "r"},
                                   date(2026, 9, 30), "captions"))
    repo.track_idea(*yp.record("Tom Nash", "UCJwKCyEIFHwUOPQQ-4kC1Zw", _video("v2", 24 * 45),
                               {"ticker": "MU", "stance": "bullish"}, date(2026, 8, 17), "captions"))
    picks = yp.recent_picks(repo, "MU", now=NOW)
    assert [pick["channel"] for pick in picks] == ["Meet Kevin"]  # Tom Nash's call is older than 30 days
    assert yp.summary_line(picks, "en") == "Meet Kevin bullish (09-30)"
    assert yp.flag(repo, "MU").startswith("📺 YouTube: Meet Kevin bullish")
    assert "不得据此调整评分或买卖结论" in yp.prompt_section(picks)
    rows = [{"channel": "A", "group": "bullish", "direction": "long", "return_5d_pct": 3.0, "spy_5d_pct": 1.0,
             "return_20d_pct": 5.0, "spy_20d_pct": 1.0},
            {"channel": "A", "group": "bearish", "direction": "short", "return_5d_pct": 3.0, "spy_5d_pct": 1.0}]
    stats = yp.channel_stats(rows)["A"]
    assert stats["picks"] == 2 and stats["vs_spy"][5] == pytest.approx(0.0) and stats["vs_spy"][20] == pytest.approx(4.0)
    assert stats["closed"] == 1


def test_the_weekly_record_lists_each_channel(repo):
    repo.track_idea(*yp.record("Meet Kevin", KEVIN, _video("v1", 24 * 10), {"ticker": "MU", "stance": "bullish"},
                               date(2026, 9, 1), "captions"))
    days = [f"2026-09-{d:02d}" for d in range(1, 30)]
    bars = [{"date": day, "open": 100, "high": 100, "low": 100, "close": 100.0 + i, "volume": 1} for i, day in enumerate(days)]
    spy = [{**bar, "close": 100.0} for bar in bars]
    it.settle_open(repo, date(2026, 9, 29), bars=lambda tickers: {"MU": bars, "SPY": spy}, nx_bars=lambda tickers: {})
    stats = it.track_record(repo, now=NOW)
    assert stats["influencers"]["Meet Kevin"]["vs_spy"][5] == pytest.approx(5.0)
    text = it.format_track_record(stats)
    assert "• Meet Kevin: 1 picks (1 bullish, 0 bearish) · +5.00% / +10.00% / +20.00% · 1 closed" in text
