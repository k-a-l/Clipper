"""Module 1 — live stream monitoring and lossless recording (Twitch via Streamlink; YouTube via yt-dlp + FFmpeg).

Design
------
* Every ``live_check_interval_s`` the channel is probed via Streamlink's Python API.
* While live, ``streamlink --stdout`` is piped into ``ffmpeg -c copy`` using the
  *segment* muxer. Media is never re-encoded; ffmpeg only re-muxes into
  fixed-length MPEG-TS files named by their UTC wall-clock start time::

      storage/raw_streams/<channel>/<session-id>/20261118T203015.ts

  Wall-clock filenames make it trivial to map a chat-spike timestamp onto the
  recording, and MPEG-TS stays readable while it is still being written and
  survives abrupt termination (no moov atom to lose).
* A watchdog restarts the capture if no bytes arrive for ``stall_timeout_s``;
  crashes and stream restarts reconnect with jittered exponential backoff,
  starting a fresh session directory each time.
* When a session ends every segment is ``ffprobe``-verified; unreadable or
  empty files are quarantined so later clip extraction never trips on them.
* A janitor deletes raw segments older than ``raw_retention_hours``.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
import random
import os
import shutil
import time
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipper.config import Settings, SourceConfig
from clipper.modules import yt_auth
from clipper.modules.utils import backoff_delay, ffprobe_duration

log = logging.getLogger(__name__)

SEGMENT_TIME_FORMAT = "%Y%m%dT%H%M%S"
# H.264 streams are segmented as MPEG-TS; VP9/AV1 (YouTube 1440p/4K) can't go in MPEG-TS, so those
# sessions are written as Matroska (equally readable while still being written).
SEGMENT_EXTS = (".ts", ".mkv")


def _segment_files(folder: Path, pattern: str = "*") -> list[Path]:
    return [p for ext in SEGMENT_EXTS for p in folder.glob(pattern + ext)]


@dataclass(frozen=True)
class Segment:
    """One recorded MPEG-TS file and the wall-clock span it covers."""

    path: Path
    start: float  # unix epoch seconds (from filename, UTC)
    end: float  # unix epoch seconds (file mtime == last write)

    @property
    def session(self) -> str:
        return self.path.parent.name


def _parse_segment_start(path: Path) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(path.stem, SEGMENT_TIME_FORMAT)))
    except ValueError:
        return None


def list_segments(raw_dir: Path) -> list[Segment]:
    """All segments under a source's raw directory, oldest first."""
    segments: list[Segment] = []
    for path in _segment_files(raw_dir, "*/*"):
        start = _parse_segment_start(path)
        if start is None:
            continue
        try:
            end = path.stat().st_mtime
        except FileNotFoundError:
            continue
        segments.append(Segment(path=path, start=start, end=max(end, start)))
    segments.sort(key=lambda seg: seg.start)
    return segments


def segments_in(raw_dir: Path, start: float, end: float) -> list[Segment]:
    """Contiguous segments (same capture session) overlapping ``[start, end]``."""
    overlapping = [seg for seg in list_segments(raw_dir) if seg.end >= start and seg.start <= end]
    if not overlapping:
        return []
    target = max(overlapping, key=lambda seg: seg.start).session  # the session covering the spike
    return [seg for seg in overlapping if seg.session == target]


class StreamRecorder:
    """Long-running daemon that keeps a lossless rolling recording of the channel."""

    def __init__(self, settings: Settings, source: SourceConfig | None = None) -> None:
        self.s = settings
        self.source = source or settings.all_sources()[0]
        self.url = self.source.source_url  # Twitch channel or YouTube live URL
        self.raw_dir = settings.raw_dir_for(self.source)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._recording = False
        self._session_dir: Path | None = None
        self.meta: dict[str, str] = {}  # title/author/category of the current broadcast
        self.live: bool | None = None  # result of the last live check (None = not checked yet)
        self.last_check: float = 0.0
        self._wake = asyncio.Event()
        self.needs_signin = False  # YouTube answered LOGIN_REQUIRED ("confirm you're not a bot")
        self.check_error: str | None = None  # why the last live check could not answer (rate limit…)
        self.retry_at: float = 0.0  # when the next check runs after a rate limit
        self._blocks = 0  # consecutive rate-limited checks (backoff exponent)
        self._session: Any = None  # streamlink.Streamlink, created lazily
        self._cookie_mtime: float | None = None  # YouTube cookie file version loaded into _session
        # Called (no args) after a check that answered "not live" cleanly: the daemon clips the
        # stream's recording / the video itself instead of waiting for it to go live.
        self.on_not_live: Callable[[], None] | None = None

    # ------------------------------------------------------------------ public
    def recheck_now(self) -> None:
        """Forget the block backoff and check again right away (e.g. after signing in)."""
        self._blocks, self.retry_at = 0, 0.0
        self._wake.set()

    @property
    def is_recording(self) -> bool:
        """True while a capture pipeline is actively writing segments."""
        return self._recording

    async def run(self) -> None:
        """Main loop: poll live status, record while live, reconnect on failure."""
        janitor = asyncio.create_task(self._janitor_loop(), name="raw-janitor")
        failures = 0
        try:
            while True:
                if not await self.check_live():
                    failures = 0
                    if self.check_error is None and self.on_not_live is not None:
                        try:
                            self.on_not_live()
                        except Exception:  # never stop watching because of the fallback
                            log.exception("[%s] not-live fallback failed", self.source.slug)
                    # rate-limited: wait it out (checking every 30 s only keeps the block going)
                    base: float = self.s.live_check_interval_s
                    if "youtu" in self.url:  # YouTube rate-limits page loads per address: check less often
                        base = max(base, self.s.youtube_live_check_interval_s)
                    base *= random.uniform(0.85, 1.15)  # jitter: no machine-regular rhythm
                    wait = max(base, self.retry_at - time.time())
                    self._wake.clear()
                    with contextlib.suppress(asyncio.TimeoutError):  # recheck_now() cuts the wait short
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    continue

                log.info("%s is live — starting capture", self.source.slug)
                started = time.monotonic()
                clean_exit = await self._record_session()
                # A session that ran for a while counts as healthy: reset backoff.
                failures = 0 if time.monotonic() - started > 300 else failures + 1
                if not clean_exit or failures:
                    delay = backoff_delay(max(failures, 1), base=2, cap=60)
                    log.info("capture ended; re-checking stream in %.1fs", delay)
                    await asyncio.sleep(delay)
        finally:
            janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor

    async def check_live(self) -> bool:
        """Return True if the channel currently has playable streams.

        Uses Streamlink's documented Python API in-process (no process spawn
        every 30 s). The capture itself still runs the Streamlink CLI as a child
        process so a hung download can always be killed and restarted.
        """
        from streamlink.exceptions import StreamlinkError

        def probe() -> bool:
            if self._session is None:
                from streamlink import Streamlink

                self._session = Streamlink()
            if yt_auth.is_youtube(self.url):  # signed-in cookies avoid "confirm you're not a bot"
                f = yt_auth.cookie_file(self.s)
                mtime = f.stat().st_mtime if f.is_file() else None
                if mtime != self._cookie_mtime:
                    self._session.http.cookies.clear()
                    yt_auth.load_into(self.s, self._session.http.cookies)
                    self._cookie_mtime = mtime
            _, plugin_cls, url = self._session.resolve_url(self.url)
            plugin = plugin_cls(self._session, url)
            streams = plugin.streams()
            if streams:  # title/category of the live broadcast: used to detect the game for hashtags
                with contextlib.suppress(Exception):
                    self.meta = {"title": plugin.get_title() or "", "author": plugin.get_author() or "",
                                 "category": plugin.get_category() or ""}
            return bool(streams)

        try:
            self.live = await asyncio.wait_for(asyncio.to_thread(probe), timeout=45)
            self.check_error, self._blocks, self.retry_at = None, 0, 0.0
            self.needs_signin = False
        except (asyncio.TimeoutError, StreamlinkError, OSError) as exc:
            text = str(exc)
            if "LOGIN_REQUIRED" in text or "not a bot" in text:
                self._blocks += 1
                wait = min(1800, 120 * 2 ** (self._blocks - 1))
                self.retry_at = time.time() + wait
                signed = yt_auth.status(self.s).get("signed_in")
                self.check_error = ("YouTube wants a fresh sign-in (saved sign-in expired): Connections → YouTube sign-in"
                                    if signed else "YouTube wants you to sign in: Connections → YouTube sign-in")
                self.needs_signin = True
                log.warning("[%s] YouTube asks to sign in (%s); next try in %d min", self.source.slug,
                            "cookies rejected" if signed else "no cookies saved", wait // 60)
            elif "429" in text or "Too Many Requests" in text:
                self._blocks += 1
                wait = min(1800, 120 * 2 ** (self._blocks - 1))  # 2, 4, 8, 16, 30 min
                self.retry_at = time.time() + wait
                site = "YouTube" if "youtu" in self.url else "the site"
                self.check_error = f"{site} is rate-limiting this computer (too many requests)"
                log.warning("[%s] live check rate-limited; next try in %d min", self.source.slug, wait // 60)
            else:
                self.check_error = f"live check failed: {text[:120]}"
                self.retry_at = 0.0
                log.warning("[%s] live check failed: %s", self.source.slug, exc)
            self.live = False
        self.last_check = time.time()
        return self.live

    def recent_segments(self, max_age_s: float) -> list[Segment]:
        """Segments whose last write is within ``max_age_s`` (for live audio analysis)."""
        cutoff = time.time() - max_age_s
        return [seg for seg in self._all_segments() if seg.end >= cutoff]

    def segments_for_window(self, start: float, end: float) -> list[Segment]:
        """Contiguous segments (same session) overlapping ``[start, end]``; never across a reconnect gap."""
        return segments_in(self.raw_dir, start, end)

    # ---------------------------------------------------------------- capture
    async def _record_session(self) -> bool:
        """Run one streamlink→ffmpeg capture until it exits. Returns True on clean EOF."""
        session_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        session_dir = self.raw_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = session_dir

        if yt_auth.is_youtube(self.url):
            direct = await self._youtube_hls()
            if direct is not None:
                return await self._record_direct(session_id, session_dir, *direct)

        read_fd, write_fd = os.pipe()
        streamlink_cmd = [
            self.s.streamlink_bin, "--stdout", "--loglevel", "warning",
            "--retry-open", "3", "--stream-segment-threads", "2",
            *(yt_auth.streamlink_args(self.s) if yt_auth.is_youtube(self.url) else []),
            *self.s.streamlink_extra_args, self.url, self.source.quality,
        ]
        ffmpeg_cmd = [
            self.s.ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
            "-map", "0", "-c", "copy",  # never re-encode on ingest
            "-f", "segment", "-segment_time", str(self.s.segment_duration_s),
            "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
            str(session_dir / f"{SEGMENT_TIME_FORMAT}.ts"),
        ]
        # ffmpeg's -strftime uses localtime; force UTC so filenames are unambiguous.
        ff_env = {**os.environ, "TZ": "UTC"}

        streamlink = ffmpeg = None
        try:
            streamlink = await asyncio.create_subprocess_exec(
                *streamlink_cmd, stdout=write_fd, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, stdin=read_fd, stderr=asyncio.subprocess.PIPE, env=ff_env,
            )
        finally:
            # Children hold their own copies; closing ours lets EOF propagate.
            os.close(read_fd)
            os.close(write_fd)

        self._recording = True
        log.info("recording session %s → %s", session_id, session_dir)
        tasks = [
            asyncio.create_task(self._drain(streamlink.stderr, "streamlink")),
            asyncio.create_task(self._drain(ffmpeg.stderr, "ffmpeg")),
        ]
        watchdog = asyncio.create_task(self._watchdog(session_dir))
        sl_wait = asyncio.create_task(streamlink.wait())
        ff_wait = asyncio.create_task(ffmpeg.wait())
        clean = False
        try:
            done, _ = await asyncio.wait(
                {sl_wait, ff_wait, watchdog}, return_when=asyncio.FIRST_COMPLETED
            )
            if watchdog in done:
                log.warning("no data for %ss — restarting capture", self.s.stall_timeout_s)
            elif sl_wait in done:
                clean = streamlink.returncode == 0
                log.info("streamlink exited (%s)", streamlink.returncode)
            else:
                log.warning("ffmpeg exited unexpectedly (%s)", ffmpeg.returncode)
        finally:
            self._recording = False
            watchdog.cancel()
            await self._terminate(streamlink, "streamlink")
            # With streamlink gone ffmpeg sees EOF and finalises the last segment.
            await self._terminate(ffmpeg, "ffmpeg", grace=15)
            for t in (*tasks, sl_wait, ff_wait):
                t.cancel()
            await asyncio.gather(*tasks, sl_wait, ff_wait, return_exceptions=True)
            await asyncio.shield(self._verify_session(session_dir))
        return clean

    async def _youtube_hls(self) -> tuple[list[str], str, str] | None:
        """Highest-quality live HLS URLs for a YouTube stream via yt-dlp: ([video, audio] urls, codec, label).

        Streamlink's YouTube client is only offered 360p for many streams now, while yt-dlp's clients
        get the full ladder (1080p60 / 1440p60 / 4K). None → fall back to Streamlink."""
        import json
        import sys

        from clipper.modules.utils import CommandError, run_cmd

        fmt = (f"bv*[height<={self.s.vod_max_height}][protocol^=m3u8]+ba[protocol^=m3u8]/"
               f"b[height<={self.s.vod_max_height}][protocol^=m3u8]/bv*+ba/b")
        cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", "--js-runtimes", self.s.js_runtime,
               *yt_auth.ytdlp_args(self.s), "-f", fmt,
               "-O", "%(.{url,vcodec,height,fps,requested_formats,http_headers})j", self.url]
        try:
            res = await run_cmd(cmd, timeout=120)
            info = json.loads(res.stdout.strip().splitlines()[-1])
        except (CommandError, OSError, ValueError, IndexError, asyncio.TimeoutError) as exc:
            log.warning("[%s] yt-dlp couldn't get the live stream (%s); using Streamlink", self.source.slug,
                        str(exc).strip().splitlines()[-1][:160] if str(exc).strip() else exc.__class__.__name__)
            return None
        parts = info.get("requested_formats") or [info]
        urls = [f["url"] for f in parts if f.get("url")]
        video = next((f for f in parts if f.get("vcodec") not in (None, "none")), info)
        if not urls:
            return None
        label = f"{video.get('height') or '?'}p{int(video.get('fps') or 0) or ''} {video.get('vcodec') or ''}".strip()
        return urls, str(video.get("vcodec") or ""), label

    async def _record_direct(self, session_id: str, session_dir: Path, urls: list[str], vcodec: str,
                             label: str) -> bool:
        """FFmpeg reads the live HLS playlists itself (no Streamlink pipe) and segments them losslessly."""
        ext = ".ts" if vcodec.startswith(("avc", "h264")) else ".mkv"
        fmt = "mpegts" if ext == ".ts" else "matroska"
        cmd = [self.s.ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-nostdin"]
        for u in urls:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
                    "-live_start_index", "-1", "-i", u]
        maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(urls) > 1 else ["-map", "0"]
        cmd += [*maps, "-c", "copy", "-f", "segment", "-segment_time", str(self.s.segment_duration_s),
                "-segment_format", fmt, "-reset_timestamps", "1", "-strftime", "1",
                str(session_dir / f"{SEGMENT_TIME_FORMAT}{ext}")]
        ffmpeg = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.PIPE, env={**os.environ, "TZ": "UTC"})
        self._recording = True
        log.info("recording session %s at %s (yt-dlp → ffmpeg) → %s", session_id, label, session_dir)
        drain = asyncio.create_task(self._drain(ffmpeg.stderr, "ffmpeg"))
        watchdog = asyncio.create_task(self._watchdog(session_dir))
        ff_wait = asyncio.create_task(ffmpeg.wait())
        clean = False
        try:
            done, _ = await asyncio.wait({ff_wait, watchdog}, return_when=asyncio.FIRST_COMPLETED)
            if watchdog in done:
                log.warning("no data for %ss — restarting capture", self.s.stall_timeout_s)
            else:
                clean = ffmpeg.returncode == 0
                log.info("ffmpeg exited (%s)", ffmpeg.returncode)
        finally:
            self._recording = False
            watchdog.cancel()
            await self._terminate(ffmpeg, "ffmpeg", grace=15)
            for task in (drain, ff_wait):
                task.cancel()
            await asyncio.gather(drain, ff_wait, return_exceptions=True)
            await asyncio.shield(self._verify_session(session_dir))
        return clean

    async def _watchdog(self, session_dir: Path) -> None:
        """Return (completing the task) once the newest segment stops growing."""
        last_state: tuple[int, int] = (-1, -1)
        last_change = time.monotonic()
        while True:
            await asyncio.sleep(10)
            files = sorted(_segment_files(session_dir))
            with contextlib.suppress(FileNotFoundError):
                state = (len(files), files[-1].stat().st_size if files else 0)
                if state != last_state:
                    last_state, last_change = state, time.monotonic()
                    continue
            if time.monotonic() - last_change > self.s.stall_timeout_s:
                return

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, name: str) -> None:
        """Forward a child's stderr to our logger (also prevents pipe back-pressure)."""
        if stream is None:
            return
        child_log = logging.getLogger(f"{__name__}.{name}")
        async for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if line:
                child_log.info(line)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process | None, name: str, grace: float = 5) -> None:
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            log.warning("%s did not exit in %.0fs; killing", name, grace)
            proc.kill()
            await proc.wait()

    # -------------------------------------------------------------- integrity
    async def _verify_session(self, session_dir: Path) -> None:
        """ffprobe every segment; quarantine unreadable / empty files."""
        quarantine = session_dir / "corrupt"
        bad = 0
        # Only the newest segments can be damaged by an interrupted capture; earlier ones were
        # closed cleanly by ffmpeg's segment muxer. Checking just the tail keeps shutdown fast
        # even after many hours of recording.
        for seg in sorted(_segment_files(session_dir))[-3:]:
            duration = await ffprobe_duration(self.s.ffprobe_bin, seg)
            if duration is None or duration < 0.5:
                quarantine.mkdir(exist_ok=True)
                shutil.move(str(seg), quarantine / seg.name)
                bad += 1
        if bad:
            log.warning("session %s: quarantined %d corrupt segment(s)", session_dir.name, bad)
        if not _segment_files(session_dir) and not quarantine.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    def _all_segments(self) -> list[Segment]:
        return list_segments(self.raw_dir)

    # ---------------------------------------------------------------- janitor
    async def _janitor_loop(self) -> None:
        """Delete raw segments past retention so the disk never fills up."""
        while True:
            try:
                cutoff = time.time() - self.s.raw_retention_hours * 3600
                removed = 0
                for path in _segment_files(self.raw_dir, "*/**/*"):
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                        removed += 1
                for session in self.raw_dir.iterdir():
                    if session.is_dir() and session != self._session_dir and not _segment_files(session, "**/*"):
                        shutil.rmtree(session, ignore_errors=True)
                if removed:
                    log.info("janitor removed %d expired raw segment(s)", removed)
            except OSError as exc:
                log.warning("janitor error: %s", exc)
            await asyncio.sleep(600)

