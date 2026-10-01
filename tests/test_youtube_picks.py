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


def test_an_early_close_day_counts_its_own_close():
    early = lambda day: yp.dtime(13, 0) if day == date(2026, 11, 27) else yp.dtime(16, 0)  # noqa: E731
    published = datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)  # 14:00 New York, after the 13:00 close
    assert yp.signal_day(published, _weekday, early) == date(2026, 11, 30)


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

    def captions(video_id, fetch=True):
        if video_id in {"spoken", "silent"}:
            return ""  # no captions
        return "words"

    def transcribe(video_id, should_stop):
        transcribed.append(video_id)
        return "spoken words" if video_id == "spoken" else ""
    job = yp.YouTubeScanJob(repo, channel_list=[("Meet Kevin", KEVIN)], read_feed=lambda cid: feed,
                            read_captions=captions, transcribe=transcribe, generate=generate, trading_day=_weekday)
    job._spacing = 0
    assert job.run(NOW) == 2
    rows = {row["ticker"]: row for row in repo.tracked_ideas()}
    assert rows["MU"]["id"] == "influencer:new:MU" and rows["MU"]["channel"] == "Meet Kevin"
    assert rows["MU"]["signal_day"] == "2026-09-30" and rows["MU"]["entry"] is None and rows["MU"]["source"] == "captions"
    assert rows["TSM"]["source"] == "audio" and {pick["ticker"] for pick in job.today_picks} == {"MU", "TSM"}
    assert "silent" not in generated  # no transcript: the title is never sent to the model
    assert set(repo.setting(yp.PROCESSED_KEY)) == {"new", "spoken", "old"}  # failures retried; the old one set aside unread
    generated.clear()
    transcribed.clear()
    job.run(NOW)
    assert generated == ["fails"] and transcribed == ["silent"]
    job.run(NOW)  # the third pass without a transcript drops the video
    assert "silent" in repo.setting(yp.PROCESSED_KEY)


def test_rate_limited_captions_wait_instead_of_transcribing(repo):
    feed = [_video("recent", 5, "recent"), _video("older", 24 * 10, "older")]
    clock = {"t": 0.0}
    asked, transcribed = [], []

    def captions(video_id, fetch=True):
        asked.append((video_id, fetch))
        raise yp.CaptionsBlocked("429" if fetch else "paused")

    def transcribe(video_id, should_stop):
        transcribed.append(video_id)
        return "spoken words"
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: feed, read_captions=captions,
                            transcribe=transcribe, generate=lambda prompt: '{"picks": [{"ticker": "MU", "stance": "bullish"}]}',
                            trading_day=_weekday, clock=lambda: clock["t"])
    assert job.run(NOW) == 0 and transcribed == []  # captions exist: no CPU spent on the audio
    assert asked == [("older", True), ("recent", False)]  # one refusal pauses caption downloads
    assert not repo.setting(yp.PROCESSED_KEY)
    clock["t"] += yp.CAPTION_PAUSE_SECONDS + 1
    asked.clear()
    job.run(NOW + timedelta(hours=7))
    assert asked[0] == ("older", True) and transcribed == []  # asked again after the pause, still waiting
    job.run(NOW + timedelta(days=1, hours=1))  # refused for a day: the audio is read, last week's videos only
    assert transcribed == ["recent"]
    assert set(repo.setting(yp.PROCESSED_KEY)) == {"recent", "older"}  # the older one is skipped, not transcribed
    [row] = repo.tracked_ideas()
    assert row["video_id"] == "recent" and row["source"] == "audio"


def test_a_stop_ends_the_pass_during_a_transcription(repo):
    feed = [_video("a", 2, "a"), _video("b", 3, "b")]

    def transcribe(video_id, should_stop):
        raise InterruptedError("stopping")
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: feed,
                            read_captions=lambda vid, fetch=True: "", transcribe=transcribe,
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


def test_caption_downloads_are_spaced_and_capped_per_pass(repo, monkeypatch):
    monkeypatch.setattr(yp, "CAPTIONS_PER_PASS", 2)
    feed = [_video(f"v{i}", i + 1, f"v{i}") for i in range(4)]
    asked, waits = [], []

    def captions(video_id, fetch=True):
        asked.append((video_id, fetch))
        if not fetch:
            raise yp.CaptionsBlocked("paused")
        return "words"
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: feed, read_captions=captions,
                            transcribe=lambda video_id, should_stop: "never", generate=lambda prompt: '{"picks": []}',
                            trading_day=_weekday)
    job._stopping.wait = lambda seconds: waits.append(seconds)
    job.run(NOW)
    assert [fetch for _, fetch in asked] == [True, True, False, False] and waits == [yp.CAPTION_SPACING_SECONDS] * 3
    assert len(repo.setting(yp.PROCESSED_KEY)) == 2  # the other two wait for the next pass, not for the audio
    job.run(NOW + timedelta(days=2))
    assert len(repo.setting(yp.PROCESSED_KEY)) == 4


def test_audio_downloads_use_the_venv_deno(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert yp._deno_path() is None
    (tmp_path / "deno").write_text("")
    assert yp._deno_path() == str(tmp_path / "deno")



def test_captions_tell_a_refusal_or_a_live_stream_from_no_captions(monkeypatch):
    import requests

    class Reply:
        def __init__(self, status=200, data=None, text=""):
            self.status_code, self._data, self.text = status, data or {}, text

        def json(self):
            return self._data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))
    track = {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"languageCode": "en", "baseUrl": "u"}]}}}
    cases = [
        (Reply(data={"playabilityStatus": {"status": "LOGIN_REQUIRED"}}), None, yp.CaptionsBlocked),
        (Reply(data={"playabilityStatus": {"status": "OK"}, "videoDetails": {"isUpcoming": True}}), None, yp.VideoNotReady),
        (Reply(data={"playabilityStatus": {"status": "LIVE_STREAM_OFFLINE"}}), None, yp.VideoNotReady),
        (Reply(data={"playabilityStatus": {"status": "OK"}, **track}), Reply(text="  "), yp.CaptionsBlocked),
    ]
    for player, body, error in cases:
        monkeypatch.setattr(requests, "post", lambda *a, **k: player)
        monkeypatch.setattr(requests, "get", lambda *a, **k: body)
        with pytest.raises(error):
            yp.captions("v")
    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply(data={"playabilityStatus": {"status": "OK"}}))
    assert yp.captions("v") == ""  # a clean answer with no tracks: really no captions
    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply(data={"playabilityStatus": {"status": "OK"}, **track}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: Reply(text="<p>buy &amp; hold</p>"))
    assert yp.captions("v") == "buy & hold"


def test_a_failing_model_keeps_the_transcript_and_gives_up_after_three_passes(repo):
    feed = [_video("v1", 5, "v1")]
    reads = []
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: feed,
                            read_captions=lambda vid, fetch=True: reads.append(vid) or "words",
                            generate=lambda prompt: (_ for _ in ()).throw(RuntimeError("model down")), trading_day=_weekday)
    job._spacing = 0
    for _ in range(3):
        job.run(NOW)
    assert reads == ["v1"]  # read once; the retries reuse the transcript
    assert "v1" in repo.setting(yp.PROCESSED_KEY)  # dropped after the third failure



def test_listed_videos_get_their_details_and_captioned_ones_are_never_transcribed(repo):
    listed = [{"video_id": v, "title": v, "published": None, "description": ""} for v in ("cap", "nocap", "live", "gone")]
    details = {
        "cap": {"published": NOW - timedelta(hours=3), "description": "d", "live_status": "not_live", "has_captions": True},
        "nocap": {"published": NOW - timedelta(hours=4), "description": "d", "live_status": "not_live", "has_captions": False},
        "live": {"published": NOW - timedelta(hours=1), "description": "", "live_status": "is_live", "has_captions": False},
    }
    looked_up, transcribed = [], []

    def read_details(video_id):
        looked_up.append(video_id)
        if video_id == "gone":
            raise RuntimeError("private video")
        return details[video_id]
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: listed, read_details=read_details,
                            read_captions=lambda vid, fetch=True: "",  # the video page lists no tracks (a block)
                            transcribe=lambda vid, should_stop: transcribed.append(vid) or "spoken words",
                            generate=lambda prompt: '{"picks": []}', trading_day=_weekday)
    job._spacing = 0
    job.run(NOW)
    assert transcribed == ["nocap"]  # "cap" has captions: it waits for them instead of costing CPU
    assert not job.captions_paused()  # one video's unlisted track does not hold back the other channels
    assert "live" not in repo.setting(yp.PROCESSED_KEY)  # read once it has aired
    looked_up.clear()
    job.run(NOW)
    assert looked_up == ["gone", "live"]  # final details are read once; a failed lookup and a live stream again
    details["live"] = {"published": NOW, "description": "", "live_status": "was_live", "has_captions": False}
    looked_up.clear()
    job.run(NOW + timedelta(hours=2))
    assert looked_up == ["gone", "live"] and transcribed == ["nocap", "live"]  # read once it has aired
    assert "live" in repo.setting(yp.PROCESSED_KEY)


def test_no_channel_listed_is_an_error_the_status_page_shows(repo):
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: (_ for _ in ()).throw(LookupError("404")),
                            read_captions=lambda vid, fetch=True: "", generate=lambda prompt: "", trading_day=_weekday)
    job._run_recorded(NOW)
    assert job.last_error == "LookupError"


def test_audio_transcription_is_capped_per_pass(repo, monkeypatch):
    monkeypatch.setattr(yp, "AUDIO_PER_PASS", 2)
    feed = [_video(f"v{i}", i + 1, f"v{i}") for i in range(4)]
    transcribed = []
    job = yp.YouTubeScanJob(repo, channel_list=[("x", KEVIN)], read_feed=lambda cid: feed,
                            read_captions=lambda vid, fetch=True: "",
                            transcribe=lambda vid, should_stop: transcribed.append(vid) or "words",
                            generate=lambda prompt: '{"picks": []}', trading_day=_weekday)
    job._spacing = 0
    job.run(NOW)
    assert len(transcribed) == 2
    job.run(NOW)
    assert len(transcribed) == 4  # the rest on the next pass


def _fake_ytdlp(monkeypatch, info=None, error=None):
    import yt_dlp

    class FakeDownloader:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            if error:
                raise error
            return info
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDownloader)


@pytest.mark.parametrize("subtitles, automatic, expected", [
    ({"live_chat": [{}]}, {"ja-orig": [{}], "en": [{}]}, False),  # chat replay and a translation are not captions
    ({"en-US": [{}], "live_chat": [{}]}, {}, True),
    ({}, {"zh-Hans-orig": [{}]}, True),
    ({}, {}, False),
])
def test_video_details_counts_only_captions_the_job_can_read(monkeypatch, subtitles, automatic, expected):
    _fake_ytdlp(monkeypatch, {"live_status": "was_live", "release_timestamp": 1_790_000_000, "duration": 3600,
                              "title": "t", "subtitles": subtitles, "automatic_captions": automatic})
    details = yp.video_details("vid")
    assert details["has_captions"] is expected
    assert details["published"] == datetime.fromtimestamp(1_790_000_000 + 3600, timezone.utc)  # a stream counts from its end


def test_rss_publish_times_are_not_trusted(monkeypatch):
    xml = (b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
           b'xmlns:media="http://search.yahoo.com/mrss/"><entry><yt:videoId>abc</yt:videoId><title>Live at 8pm</title>'
           b'<published>2026-09-25T13:00:00+00:00</published><media:group><media:description>d</media:description>'
           b'</media:group></entry></feed>')
    monkeypatch.setattr(yp, "_get", lambda url, **kwargs: SimpleNamespace(content=xml))
    # A scheduled stream's feed time is when it was scheduled: the job reads the real time per video.
    assert yp.feed(KEVIN) == [{"video_id": "abc", "title": "Live at 8pm", "published": None, "description": "d"}]


def test_a_stop_during_the_audio_download_is_a_stop_not_a_failure(monkeypatch, tmp_path):
    from yt_dlp.utils import DownloadError
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    _fake_ytdlp(monkeypatch, error=DownloadError("interrupted"))  # how yt-dlp reports the hook's InterruptedError
    with pytest.raises(InterruptedError):
        yp.transcribe_audio("vid", should_stop=lambda: True)
    with pytest.raises(DownloadError):
        yp.transcribe_audio("vid")
    assert list(tmp_path.iterdir()) == []


def test_an_age_restricted_video_is_unreadable_not_a_block(monkeypatch):
    import requests
    reply = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {
        "playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm your age"}})
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: reply)
    assert yp.captions("vid") == ""
    reply.json = lambda: {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you're not a bot"}}
    with pytest.raises(yp.CaptionsBlocked, match="login_required"):
        yp.captions("vid")
