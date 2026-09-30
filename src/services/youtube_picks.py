"""Stock picks from the YouTube channels you follow (reference only; nothing is traded).

``YOUTUBE_CHANNELS`` lists the channels as ``Name=UC…`` entries (channel ids, not handles:
a handle can point at a clips or fan channel). Every ``POLL_SECONDS`` the channel feeds
(RSS, free) are read. Each new video's full spoken text goes to a model that lists only
the stocks the host explicitly recommends or warns against, as strict JSON. The text is
the video's captions (YouTube's own, from the Android player API); a video without
captions is transcribed locally when ``YOUTUBE_TRANSCRIBE_AUDIO`` is on (yt-dlp downloads
the audio into a temporary folder, faster-whisper transcribes it on the CPU, and the
folder is deleted right away). Titles and descriptions say too little to read a call
from, so a video with no transcript is skipped (retried on later passes, then dropped).
Captions that YouTube refuses to send (rate limit) are waited for, not replaced by the
audio, until they have been refused for a day; audio is read for the last week only.
The model is ``YOUTUBE_PICKS_BACKEND`` (default: the routine ``GENERATION_BACKEND``); a
low-cost LiteLLM model reads a video for a fraction of a cent.

Each pick is followed forward in the idea tracker (kind ``influencer``) from the first
close after the video was published (no look-ahead), 5, 10 and 20 sessions, against
SPY. The weekly track record shows each channel's results. Reports and Trade Desk
answers show a ticker's picks from the last ``RECENT_DAYS`` days as a reference.
"""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

POLL_SECONDS = 3 * 3600
BACKFILL_DAYS = 30  # the first run reads this far back, to seed the track record
RECENT_DAYS = 30
MAX_TRANSCRIPT_CHARS = 60000
MAX_PICKS_PER_VIDEO = 10
TRANSCRIPT_ATTEMPTS = 3  # passes a video without a transcript is retried before it is dropped
CAPTION_PAUSE_SECONDS = 6 * 3600  # after YouTube rate-limits caption downloads, stop asking for this long
CAPTION_SPACING_SECONDS = 10.0  # between caption downloads: bursts are what trip the rate limit
CAPTIONS_PER_PASS = 25  # the rest wait for the next pass (every 3 hours)
BLOCKED_AUDIO_AFTER = timedelta(days=1)  # captions refused this long: transcribe the audio instead
AUDIO_BACKFILL_DAYS = 7  # audio is transcribed only for videos of the last week (CPU time)
MAX_AUDIO_SECONDS = 2 * 3600  # longer videos (live streams) are not transcribed
AUDIO_PREFIX = "dsa-yt-audio-"
PROCESSED_KEY = "youtube_processed"
_NEW_YORK = ZoneInfo("America/New_York")
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
       "media": "http://search.yahoo.com/mrss/"}
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")
_ANDROID = {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 30, "hl": "en", "gl": "US"}


def channels() -> List[Tuple[str, str]]:
    """(name, channel id) from ``YOUTUBE_CHANNELS``; entries that are not channel ids are skipped."""
    out = []
    for entry in os.getenv("YOUTUBE_CHANNELS", "").split(","):
        name, _, channel_id = entry.strip().rpartition("=")
        channel_id = channel_id.strip()
        if _CHANNEL_ID.match(channel_id):
            out.append((name.strip() or channel_id, channel_id))
        elif entry.strip():
            logger.warning("YOUTUBE_CHANNELS: %r is not a channel id (UC…)", entry.strip())
    return out


def enabled() -> bool:
    return bool(channels())


def _get(url: str, **kwargs) -> Any:
    import requests
    response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, **kwargs)
    response.raise_for_status()
    return response


def feed(channel_id: str) -> List[Dict[str, Any]]:
    """The channel's latest videos (the feed lists about 15), newest first."""
    root = ET.fromstring(_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}").content)
    videos = []
    for entry in root.findall("a:entry", _NS):
        published = datetime.fromisoformat(entry.find("a:published", _NS).text.replace("Z", "+00:00"))
        description = entry.find("media:group/media:description", _NS)
        videos.append({"video_id": entry.find("yt:videoId", _NS).text, "title": entry.find("a:title", _NS).text or "",
                       "published": published.astimezone(timezone.utc),
                       "description": (description.text or "") if description is not None else ""})
    return videos


def _pick_track(tracks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Captions in the video's own language: English or Chinese, uploaded ones before automatic ones."""
    def language(track):
        return str(track.get("languageCode") or "").lower()
    for automatic in (False, True):
        for prefix in ("en", "zh"):
            for track in tracks:
                if language(track).startswith(prefix) and (track.get("kind") == "asr") == automatic:
                    return track
    return None


class CaptionsBlocked(Exception):
    """The video has captions, but they were not downloaded: YouTube refused (rate limit) or fetching is paused."""


def captions(video_id: str, fetch: bool = True) -> str:
    """The video's caption text, or "" when it has none. Raises CaptionsBlocked when it has captions
    that YouTube refuses to send (HTTP 429/403), or that ``fetch=False`` says not to ask for."""
    import requests
    response = requests.post("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", timeout=20,
                             json={"context": {"client": _ANDROID}, "videoId": video_id},
                             headers={"User-Agent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"})
    response.raise_for_status()
    tracks = ((((response.json().get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
               .get("captionTracks")) or [])
    track = _pick_track(tracks)
    if track is None:
        return ""
    if not fetch:
        raise CaptionsBlocked("paused")
    reply = requests.get(track["baseUrl"], timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    if reply.status_code in (403, 429):
        raise CaptionsBlocked(str(reply.status_code))
    reply.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", reply.text)
    return " ".join(html.unescape(text).split())


def transcribe_enabled() -> bool:
    return os.getenv("YOUTUBE_TRANSCRIBE_AUDIO", "false").strip().lower() in {"1", "true", "yes", "on"}


_whisper_lock = threading.Lock()
_whisper_model = None


def _whisper():
    """The local speech-to-text model, loaded on first use (about 1.6 GB on disk for large-v3-turbo)."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(os.getenv("YOUTUBE_WHISPER_MODEL", "large-v3-turbo").strip() or "large-v3-turbo",
                                          device="cpu", compute_type="int8",
                                          cpu_threads=int(os.getenv("YOUTUBE_WHISPER_THREADS", "8") or 8),
                                          download_root=os.getenv("YOUTUBE_WHISPER_DIR", "").strip() or None)
        return _whisper_model


def release_whisper() -> None:
    """Frees the model's memory between passes."""
    global _whisper_model
    with _whisper_lock:
        _whisper_model = None


def clear_stale_audio(max_age_seconds: float = 3600) -> None:
    """Removes audio folders a crash or kill left behind (normally each is deleted as soon as it is used)."""
    root = tempfile.gettempdir()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.startswith(AUDIO_PREFIX) and time.time() - os.path.getmtime(path) > max_age_seconds:
            shutil.rmtree(path, ignore_errors=True)


def _deno_path() -> Optional[str]:
    """The deno binary: on PATH, or next to this Python (``pip install deno`` puts it in the venv)."""
    import sys
    found = shutil.which("deno")
    if found:
        return found
    local = os.path.join(os.path.dirname(sys.executable), "deno")
    return local if os.path.exists(local) else None


def transcribe_audio(video_id: str, should_stop: Callable[[], bool] = lambda: False) -> str:
    """The video's speech as text: the audio goes to a temporary folder that is deleted on the way out."""
    import yt_dlp
    from yt_dlp.utils import match_filter_func
    with tempfile.TemporaryDirectory(prefix=AUDIO_PREFIX) as folder:
        options = {"format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
                   "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
                   "match_filter": match_filter_func(f"duration <= {MAX_AUDIO_SECONDS} & !is_live")}
        deno = _deno_path()
        if deno:  # YouTube's player challenges need a JavaScript runtime; without one formats go missing
            options["js_runtimes"] = {"deno": {"path": deno}}
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        files = [os.path.join(folder, name) for name in os.listdir(folder) if not name.endswith(".part")]
        if not files:
            return ""  # a live stream or a very long video
        segments, _info = _whisper().transcribe(files[0], vad_filter=True, beam_size=1)
        parts = []
        for segment in segments:  # decoded lazily: a server stop ends the work between segments
            if should_stop():
                raise InterruptedError("stopping")
            parts.append(segment.text)
        return "".join(parts).strip()


PROMPT = """You read the transcript of a stock-market YouTube video.
List only the stocks the host explicitly recommends buying or holding ("bullish") or explicitly says to sell, avoid or short ("bearish").
Skip stocks that are only mentioned, compared or used as examples; skip broad index funds discussed as market commentary; skip crypto.
Use US ticker symbols (Class B shares with a dot, e.g. BRK.B). The transcript may be automatic captions or Chinese; answer in English.
Return only JSON: {"picks": [{"ticker": "NVDA", "stance": "bullish" or "bearish", "conviction": "high" or "medium" or "low",
"horizon": "short" (days to weeks) or "long" (months or more) or "unspecified", "reason": "the host's reason, at most 20 words, your own words"}]}
Return {"picks": []} when the video makes no explicit call."""


def extract_picks(channel: str, video: Dict[str, Any], text: str,
                  generate: Optional[Callable[[str], str]] = None) -> List[Dict[str, Any]]:
    """The video's explicit calls, validated; [] when there are none. Raises when the model fails."""
    if not text:
        raise ValueError("no transcript")  # a title or description is not enough to read a call from
    prompt = f"{PROMPT}\n\nChannel: {channel}\nTitle: {video['title']}\nTranscript:\n{text[:MAX_TRANSCRIPT_CHARS]}"
    raw = (generate or _generate)(prompt)
    from src.agent.runner import try_parse_json
    data = try_parse_json(raw or "")
    if not isinstance(data, dict) or not isinstance(data.get("picks"), list):
        raise ValueError("the model did not return the picks JSON")
    picks, seen = [], set()
    for row in data["picks"]:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper().replace("-", ".").lstrip("$")
        stance = str(row.get("stance") or "").lower()
        if not _TICKER.match(ticker) or stance not in {"bullish", "bearish"} or ticker in seen:
            continue
        seen.add(ticker)
        picks.append({"ticker": ticker, "stance": stance,
                      "conviction": row.get("conviction") if row.get("conviction") in {"high", "medium", "low"} else None,
                      "horizon": row.get("horizon") if row.get("horizon") in {"short", "long"} else "unspecified",
                      "reason": " ".join(str(row.get("reason") or "").split())[:160]})
    return picks[:MAX_PICKS_PER_VIDEO]


def _generate(prompt: str) -> str:
    from src.services.trade_desk.advisor import _generation_backend
    backend, _backend_id = _generation_backend(os.getenv("YOUTUBE_PICKS_BACKEND", "").strip() or None)
    return backend.generate(prompt, {"temperature": 0, "max_output_tokens": 2048}).text or ""


def signal_day(published: datetime, trading_day: Callable[[date], bool]) -> date:
    """The first session whose close comes after the video was published: its close is the entry."""
    local = published.astimezone(_NEW_YORK)
    day = local.date()
    if local.time() >= dtime(16, 0) or not trading_day(day):
        day += timedelta(days=1)
        while not trading_day(day):
            day += timedelta(days=1)
    return day


def record(channel: str, channel_id: str, video: Dict[str, Any], pick: Dict[str, Any], day: date,
           source: str) -> tuple:
    """(id, record) for one pick (idea tracker kind ``influencer``)."""
    return (f"influencer:{video['video_id']}:{pick['ticker']}", {
        "kind": "influencer", "verdict": "influencer", "group": pick["stance"], "ticker": pick["ticker"],
        "direction": "long" if pick["stance"] == "bullish" else "short", "signal_day": day.isoformat(),
        "entry": None, "channel": channel, "channel_id": channel_id, "video_id": video["video_id"],
        "title": video["title"][:200], "published_at": video["published"].isoformat(),
        "conviction": pick.get("conviction"), "horizon": pick.get("horizon"), "reason": pick.get("reason", ""),
        "source": source})  # "captions" or "audio"


def recent_picks(repo: Any, ticker: str, days: int = RECENT_DAYS, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The ticker's picks from videos of the last ``days`` days, newest first."""
    symbol = str(ticker or "").strip().upper().replace("-", ".")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    rows = repo.tracked_ideas_like(f"influencer:%:{symbol}")
    rows = [row for row in rows if row.get("ticker") == symbol
            and datetime.fromisoformat(row["published_at"]) >= cutoff]
    return sorted(rows, key=lambda row: row["published_at"], reverse=True)


def flag(repo: Any, ticker: str, days: int = 14) -> str:
    """A line for a trade idea when a followed channel called the stock lately; "" otherwise."""
    picks = recent_picks(repo, ticker, days)
    return f"📺 YouTube: {summary_line(picks, 'en')}" if picks else ""


def summary_line(picks: List[Dict[str, Any]], language: str = "en") -> str:
    zh = language in ("zh", "zh-CN", "zh_CN")
    parts = []
    for pick in picks[:6]:
        when = pick["published_at"][5:10]
        stance = ({"bullish": "看多", "bearish": "看空"} if zh else {"bullish": "bullish", "bearish": "bearish"})[pick["group"]]
        parts.append(f"{pick['channel']} {stance}（{when}）" if zh else f"{pick['channel']} {stance} ({when})")
    return ("；" if zh else "; ").join(parts)


def prompt_section(picks: List[Dict[str, Any]]) -> str:
    if not picks:
        return ""
    reasons = "\n".join(f"- {pick['channel']}（{pick['published_at'][:10]}，{pick['group']}）：{pick.get('reason') or '未说明'}"
                        for pick in picks[:6])
    return f"""
### 用户关注的 YouTube 博主近 {RECENT_DAYS} 天的观点（仅作参考）
{reasons}
> 这些是博主的公开观点，由模型从视频字幕提炼，可能有误读；系统正在跟踪它们之后的表现，尚无证据表明其有预测力。可以提及、并与自己的分析对照；不得据此调整评分或买卖结论。
"""


def attach(result: Any, picks: List[Dict[str, Any]]) -> None:
    """Store the picks on the result's dashboard (data_perspective.youtube_picks)."""
    if not picks or result is None:
        return
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        dashboard = {}
        result.dashboard = dashboard
    perspective = dashboard.get("data_perspective")
    if not isinstance(perspective, dict):
        perspective = {}
        dashboard["data_perspective"] = perspective
    perspective["youtube_picks"] = [{key: pick.get(key) for key in ("channel", "group", "published_at", "reason",
                                                                    "video_id", "title")} for pick in picks[:6]]


def report_lines(perspective: Any, language: str = "zh") -> List[str]:
    picks = perspective.get("youtube_picks") if isinstance(perspective, dict) else None
    if not isinstance(picks, list) or not picks:
        return []
    try:
        english = language not in ("zh", "zh-CN", "zh_CN")
        label = f"YouTube picks, last {RECENT_DAYS} days" if english else f"YouTube 博主观点（近 {RECENT_DAYS} 天）"
        return [f"**{label}**: {summary_line(picks, 'en' if english else 'zh')}", ""]
    except (KeyError, TypeError, ValueError):
        return []


_repositories: Dict[int, Any] = {}


def for_report(ticker: str, db: Any = None) -> List[Dict[str, Any]]:
    """Recent picks for a report; [] when the scan is off or the store cannot be read."""
    if not enabled():
        return []
    try:
        from src.services.trade_desk.repository import TradeDeskRepository
        repo = _repositories.get(id(db))
        if repo is None:  # the repository checks its tables once, not on every report
            repo = _repositories[id(db)] = TradeDeskRepository(db)
        return recent_picks(repo, ticker)
    except Exception as exc:  # a missing reference never blocks a report
        logger.info("YouTube picks unavailable for %s: %s", ticker, type(exc).__name__)
        return []


class YouTubeScanJob:
    """Reads the channel feeds every few hours and records new videos' picks (worker leader only)."""

    def __init__(self, repo: Any, *, channel_list: Optional[List[Tuple[str, str]]] = None,
                 read_feed: Callable[[str], List[Dict[str, Any]]] = feed,
                 read_captions: Callable[[str], str] = captions,
                 transcribe: Optional[Callable[..., str]] = None,
                 generate: Optional[Callable[[str], str]] = None,
                 trading_day: Optional[Callable[[date], bool]] = None,
                 clock: Optional[Callable[[], float]] = None):
        from concurrent.futures import ThreadPoolExecutor
        self.repo = repo
        self._channels = channel_list
        self._feed = read_feed
        self._captions = read_captions
        # Local transcription of videos without captions, when YOUTUBE_TRANSCRIBE_AUDIO is on.
        self._transcribe = transcribe if transcribe is not None else (transcribe_audio if transcribe_enabled() else None)
        self._missing: Dict[str, int] = {}  # video -> passes without a transcript
        self._blocked_since: Dict[str, datetime] = {}  # video -> first time its captions were refused
        self._captions_paused_until = 0.0
        self._caption_fetches = 0  # this pass
        self._spacing = CAPTION_SPACING_SECONDS
        self._stopping = threading.Event()
        self._generate = generate
        self._trading_day = trading_day
        self._clock = clock or time.monotonic
        self._next = 0.0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="youtube-picks")
        self._task = None
        self.today_picks: List[Dict[str, Any]] = []  # for the evening digest
        self.last_pass_at: Optional[str] = None
        self.last_pass_added = 0
        self.last_error: Optional[str] = None

    def stop(self):
        self._stopping.set()  # a transcription in progress ends at its next segment; its folder is deleted
        self._pool.shutdown(wait=False, cancel_futures=True)

    def tick(self, now: datetime) -> None:
        if self._clock() < self._next or (self._task is not None and not self._task.done()):
            return
        self._next = self._clock() + POLL_SECONDS
        self._task = self._pool.submit(self._run_recorded, now)

    def _run_recorded(self, now: datetime) -> None:
        try:
            self.last_pass_added = self.run(now)
            self.last_pass_at, self.last_error = now.isoformat(), None
        except Exception as exc:  # the next pass retries
            self.last_error = type(exc).__name__
            logger.warning("YouTube scan failed: %s", type(exc).__name__)

    def captions_paused(self) -> bool:
        return self._clock() < self._captions_paused_until

    def run(self, now: datetime) -> int:
        """One pass over every channel; returns how many picks were recorded."""
        processed = dict(self.repo.setting(PROCESSED_KEY, {}) or {})
        cutoff = now - timedelta(days=BACKFILL_DAYS)
        trading = self._trading_day or _trading_day
        added = 0
        if self._transcribe is not None:
            try:
                clear_stale_audio()
            except OSError as exc:
                logger.info("Could not clear old audio folders: %s", type(exc).__name__)
        self._caption_fetches = 0
        try:
            added = self._pass(now, processed, cutoff, trading)
        finally:
            release_whisper()
        self.today_picks = [pick for pick in self.today_picks
                            if datetime.fromisoformat(pick["published_at"]) >= now - timedelta(days=1)]
        logger.info("YouTube scan: %d picks recorded", added)
        return added

    def _transcript(self, video: Dict[str, Any], now: datetime) -> Tuple[str, str]:
        """(text, source): "captions", "audio", "" (no transcript: retried, then dropped) or "skip" (too
        old to transcribe). Raises _Deferred when the captions exist but YouTube is refusing them for now."""
        video_id = video["video_id"]
        fetch = self._clock() >= self._captions_paused_until and self._caption_fetches < CAPTIONS_PER_PASS
        if fetch and self._caption_fetches:
            self._stopping.wait(self._spacing)  # a server stop ends the wait
        self._caption_fetches += int(fetch)
        try:
            text = self._captions(video_id, fetch=fetch)
            if text:
                self._blocked_since.pop(video_id, None)
                return text, "captions"
        except CaptionsBlocked as exc:
            if fetch:  # refused just now: stop asking for a while, YouTube lifts these blocks slowly
                self._captions_paused_until = self._clock() + CAPTION_PAUSE_SECONDS
                logger.warning("YouTube refused captions (%s); captioned videos wait %d hours", exc,
                               CAPTION_PAUSE_SECONDS // 3600)
            if not fetch and self._clock() >= self._captions_paused_until:
                raise _Deferred() from exc  # only this pass's quota is used up: the next pass reads it
            if now - self._blocked_since.setdefault(video_id, now) < BLOCKED_AUDIO_AFTER or self._transcribe is None:
                raise _Deferred() from exc
            # Refused for a day: the audio is the only way left to read the video.
        except Exception as exc:  # the video page or the network failed: try again on the next pass
            logger.info("YouTube captions unavailable for %s: %s", video_id, type(exc).__name__)
            raise _Deferred() from exc
        if self._transcribe is None:
            return "", ""
        if video["published"] < now - timedelta(days=AUDIO_BACKFILL_DAYS):
            return "", "skip"
        started = time.monotonic()
        text = self._transcribe(video_id, should_stop=self._stopping.is_set)
        if text:
            logger.info("YouTube audio for %s transcribed in %.0f s", video_id, time.monotonic() - started)
            return text, "audio"
        logger.info("YouTube video %s not transcribed (live, over two hours, or no speech)", video_id)
        return "", ""

    def _pass(self, now, processed, cutoff, trading) -> int:
        added = 0
        for name, channel_id in (self._channels if self._channels is not None else channels()):
            try:
                videos = self._feed(channel_id)
            except Exception as exc:  # the next pass retries this channel
                logger.info("YouTube feed unavailable for %s: %s", name, type(exc).__name__)
                continue
            for video in reversed(videos):  # oldest first
                if self._stopping.is_set():
                    return added
                if video["video_id"] in processed or video["published"] < cutoff:
                    continue
                try:
                    text, source = self._transcript(video, now)
                except InterruptedError:
                    return added
                except _Deferred:
                    continue  # its captions come on a later pass
                except Exception as exc:  # download or transcription failed: counted like a missing transcript
                    logger.warning("YouTube transcription failed for %s: %s", video["video_id"], type(exc).__name__)
                    text, source = "", ""
                if not text:
                    misses = self._missing[video["video_id"]] = self._missing.get(video["video_id"], 0) + 1
                    if misses >= TRANSCRIPT_ATTEMPTS or source == "skip":  # captions rarely arrive later; stop asking
                        processed[video["video_id"]] = video["published"].date().isoformat()
                        self.repo.set_setting(PROCESSED_KEY, _pruned(processed, now))
                        logger.info("YouTube video %s has no transcript; skipped", video["video_id"])
                    continue
                try:
                    picks = extract_picks(name, video, text, self._generate)
                except Exception as exc:  # the model failed: retried on the next pass
                    logger.warning("YouTube picks failed for %s: %s", video["video_id"], type(exc).__name__)
                    continue
                day = signal_day(video["published"], trading)
                for pick in picks:
                    record_id, payload = record(name, channel_id, video, pick, day, source)
                    if self.repo.track_idea(record_id, payload):
                        added += 1
                        if video["published"] >= now - timedelta(days=1):
                            self.today_picks.append(payload)
                processed[video["video_id"]] = video["published"].date().isoformat()
                # Saved after each video: a restart never pays for the same video twice.
                self.repo.set_setting(PROCESSED_KEY, _pruned(processed, now))
        return added


class _Deferred(Exception):
    """The video is left for a later pass without counting as a miss."""


def _pruned(processed: Dict[str, str], now: datetime) -> Dict[str, str]:
    keep = (now - timedelta(days=BACKFILL_DAYS + 30)).date().isoformat()
    return {video: day for video, day in processed.items() if day >= keep}


def _trading_day(day: date) -> bool:
    from src.services.trade_desk.holdings import _trading_day as trading
    return trading(day)


def channel_stats(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per channel: picks, closed picks, and the average 5/10/20-session return in the call's direction vs SPY."""
    def average(values):
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None
    out: Dict[str, Dict[str, Any]] = {}
    for row in records:
        stats = out.setdefault(row.get("channel") or "?", {"picks": 0, "bullish": 0, "bearish": 0, "edges": {}})
        stats["picks"] += 1
        stats[row["group"]] = stats.get(row["group"], 0) + 1
        sign = 1 if row.get("direction") == "long" else -1
        for horizon in (5, 10, 20):
            stock, spy = row.get(f"return_{horizon}d_pct"), row.get(f"spy_{horizon}d_pct")
            if stock is not None and spy is not None:
                stats["edges"].setdefault(horizon, []).append(sign * (stock - spy))
    for stats in out.values():
        stats["closed"] = len(stats["edges"].get(20, []))
        stats["vs_spy"] = {horizon: average(stats["edges"].get(horizon, [])) for horizon in (5, 10, 20)}
        stats["counted"] = {horizon: len(stats["edges"].get(horizon, [])) for horizon in (5, 10, 20)}
        del stats["edges"]
    return out
