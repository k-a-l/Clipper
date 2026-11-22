"""Module 5 — compliant YouTube Shorts & Facebook Reels publishing.

Safeguards implemented here
---------------------------
* **Permission gate** — nothing is published unless ``CHANNEL_PERMISSION_CONFIRMED``
  is true (you own the channel or have the rights holder's permission). Every
  upload carries attribution to the original stream.
* **Quota accounting** — YouTube Data API units are tracked per Pacific-time day
  (when Google resets quotas) in SQLite; uploads are deferred, not attempted,
  when the day's budget (minus a safety margin) would be exceeded.
* **Meta rate limits** — ``X-App-Usage`` / ``X-Business-Use-Case-Usage`` headers
  are inspected on every Graph call; the platform cools down above 85 % usage.
* **Backoff** — HTTP 429/5xx and transient network errors retry with exponential
  backoff + full jitter, honouring server-provided wait hints.
* **Cadence** — at most ``MAX_POSTS_PER_DAY`` (≤ 4) uploads per platform in any
  rolling 24 h, at least ``MIN_POST_SPACING_MINUTES`` apart plus random jitter.
  Only the highest-scoring clips get posted; stale ones expire.
* **Varied metadata** — titles are generated per clip and checked against
  previously used titles (see :mod:`modules.metadata`).
* **Token security** — OAuth tokens are stored Fernet-encrypted (key only in the
  environment), refreshed automatically, and an auth failure *pauses* the
  platform instead of retry-looping against the auth server.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import logging
import random
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo

import httpx

from clipper.config import FACEBOOK_MAX_SECONDS, Platform, Settings, account_platform
from clipper.modules.metadata import ClipContext, Metadata, MetadataGenerator, normalise_title
from clipper.modules.utils import EncryptedTokenStore, RetryableError, TokenStoreError, backoff_delay, retry_async

log = logging.getLogger(__name__)
T = TypeVar("T")
PACIFIC = ZoneInfo("America/Los_Angeles")


# ======================================================================= errors
class PublishError(Exception):
    """Base class for publishing failures."""


class TransientPublishError(PublishError):
    """Retry the job later with backoff."""


class PermanentPublishError(PublishError):
    """The job can never succeed as-is (e.g. video rejected)."""


class AuthError(PermanentPublishError):
    """Credentials are invalid/revoked: pause the platform until re-authorised."""


class DeferPublish(PublishError):
    """Not an error with the clip — retry no earlier than ``until`` (quota/rate limits)."""

    def __init__(self, message: str, until: float) -> None:
        super().__init__(message)
        self.until = until


def next_pacific_midnight(now: float | None = None) -> float:
    current = dt.datetime.fromtimestamp(now or time.time(), PACIFIC)
    tomorrow = (current + dt.timedelta(days=1)).date()
    return dt.datetime.combine(tomorrow, dt.time(0, 5), PACIFIC).timestamp()


def pacific_day(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(now or time.time(), PACIFIC).date().isoformat()


# ======================================================================== queue
@dataclass(frozen=True)
class Job:
    id: int
    clip_id: int
    platform: str  # account key: "youtube", "facebook:<page id>", …
    attempts: int
    path: Path
    score: float
    transcript: str
    keyword_hits: dict[str, int]
    metadata: Metadata | None
    hook: str = ""
    duration: float | None = None
    scheduled_at: float | None = None
    source_id: str = ""
    source_url: str = ""
    display_name: str = ""
    category: str = ""
    game: str = ""
    action: str = ""
    story: str = ""  # modules.story.Story JSON ("" = template metadata)

    @property
    def thumbnail(self) -> Path | None:
        """Thumbnail sidecar written next to the rendered clip, if any."""
        from clipper.modules.library import thumb_of

        thumb = thumb_of(self.path)
        return thumb if thumb.is_file() else None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    duration REAL,
    transcript TEXT NOT NULL DEFAULT '',
    keyword_hits TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|uploading|posted|failed|expired
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    title_key TEXT,
    metadata TEXT,
    remote_id TEXT,
    error TEXT,
    posted_at REAL,
    updated_at REAL,
    UNIQUE (clip_id, platform)
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(platform, status, next_attempt_at);
CREATE TABLE IF NOT EXISTS platform_state (
    platform TEXT PRIMARY KEY,
    next_eligible_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_usage (
    platform TEXT NOT NULL,
    day TEXT NOT NULL,
    units INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, day)
);
"""


class PublishQueue:
    """Durable SQLite-backed job queue (WAL mode; all access serialised off-loop)."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(clips)")}
        for col in ("hook", "source_id", "source_url", "display_name", "category", "render_opts", "media",
                    "clip_key", "game", "action", "story"):
            if col not in cols:  # forward migrations
                self._conn.execute(f"ALTER TABLE clips ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        jcols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "scheduled_at" not in jcols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN scheduled_at REAL")
        if "manual" not in jcols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
        # Crash recovery: an interrupted upload goes back to the queue.
        self._conn.execute("UPDATE jobs SET status='pending' WHERE status='uploading'")
        self._lock = asyncio.Lock()

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ---------------------------------------------------------------- clips
    def _add_clip(self, path: Path, score: float, duration: float | None, transcript: str,
                  keyword_hits: dict[str, int], platforms: list[str], hook: str,
                  source: dict[str, str], render_opts: str, media: str, clip_key: str, created: float,
                  story: str = "") -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO clips(path, created_at, score, duration, transcript, keyword_hits, hook, "
                "source_id, source_url, display_name, category, render_opts, media, clip_key, game, action, story) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(path), created, score, duration, transcript, json.dumps(keyword_hits), hook,
                 source.get("id", ""), source.get("url", ""), source.get("name", ""), source.get("category", ""),
                 render_opts, media, clip_key, source.get("game", ""), source.get("action", ""), story),
            )
            clip_id = int(cur.lastrowid or 0)
            for platform in platforms:
                self._conn.execute(
                    "INSERT OR IGNORE INTO jobs(clip_id, platform, updated_at) VALUES (?,?,?)",
                    (clip_id, platform, time.time()),
                )
        return clip_id

    async def add_clip(self, path: Path, *, score: float, duration: float | None, transcript: str,
                       keyword_hits: dict[str, int], platforms: list[str], hook: str = "",
                       source: dict[str, str] | None = None, render_opts: str = "", media: str = "",
                       clip_key: str = "", created: float | None = None, story: str = "") -> int:
        """Register a rendered clip and enqueue one job per account key (none if ``platforms`` is empty).

        ``source`` = {"id", "url", "name", "category"} for attribution and metadata. ``clip_key``
        identifies the moment (source + time) so the same moment is never clipped twice.
        """
        return await self._call(self._add_clip, path, score, duration, transcript, keyword_hits,
                                platforms, hook, source or {}, render_opts, media, clip_key,
                                created if created is not None else time.time(), story)

    def _update_clip(self, clip_id: int, fields: dict[str, Any]) -> None:
        allowed = {"duration", "transcript", "hook", "render_opts", "media", "action", "story"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        with self._conn:
            self._conn.execute(f"UPDATE clips SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                               (*[fields[c] for c in cols], clip_id))
            # a re-render invalidates generated metadata for jobs that haven't posted yet
            self._conn.execute("UPDATE jobs SET metadata=NULL, title_key=NULL WHERE clip_id=? "
                               "AND status IN ('pending','scheduled','failed')", (clip_id,))

    async def update_clip(self, clip_id: int, **fields: Any) -> None:
        await self._call(self._update_clip, clip_id, fields)

    def _schedule(self, clip_id: int, platform: str, at: float) -> str:
        """Create or move a manual job for ``platform`` to post at ``at``. Returns the new status."""
        with self._conn:
            row = self._conn.execute("SELECT id, status FROM jobs WHERE clip_id=? AND platform=?",
                                     (clip_id, platform)).fetchone()
            if row is not None and row["status"] in ("posted", "uploading"):
                raise ValueError(f"already {row['status']} on {platform}")
            if row is None:
                self._conn.execute("INSERT INTO jobs(clip_id, platform, status, scheduled_at, manual, updated_at) "
                                   "VALUES (?,?, 'scheduled', ?, 1, ?)", (clip_id, platform, at, time.time()))
            else:
                self._conn.execute("UPDATE jobs SET status='scheduled', scheduled_at=?, manual=1, attempts=0, "
                                   "next_attempt_at=0, error=NULL, updated_at=? WHERE id=?",
                                   (at, time.time(), row["id"]))
        return "scheduled"

    async def schedule(self, clip_id: int, platform: str, at: float) -> str:
        return await self._call(self._schedule, clip_id, platform, at)

    def _unschedule(self, clip_id: int, platform: str) -> int:
        with self._conn:
            return self._conn.execute(
                "UPDATE jobs SET status='expired', error='schedule cancelled', scheduled_at=NULL, updated_at=? "
                "WHERE clip_id=? AND platform=? AND status IN ('scheduled','pending')",
                (time.time(), clip_id, platform)).rowcount

    async def unschedule(self, clip_id: int, platform: str) -> int:
        return await self._call(self._unschedule, clip_id, platform)

    def _due_scheduled(self, platform: str, now: float) -> Job | None:
        row = self._conn.execute(
            "SELECT j.id FROM jobs j WHERE j.platform=? AND j.status='scheduled' AND j.scheduled_at<=? "
            "AND j.next_attempt_at<=? ORDER BY j.scheduled_at LIMIT 1", (platform, now, now)).fetchone()
        return self._job_by_id(row[0]) if row else None

    async def due_scheduled(self, platform: str, now: float) -> Job | None:
        return await self._call(self._due_scheduled, platform, now)

    def _oldest_post_since(self, platform: str, since: float) -> float | None:
        v = self._scalar("SELECT MIN(posted_at) FROM jobs WHERE platform=? AND status='posted' AND posted_at>=?",
                         (platform, since))
        return float(v) if v is not None else None

    async def oldest_post_since(self, platform: str, since: float) -> float | None:
        return await self._call(self._oldest_post_since, platform, since)

    # ----------------------------------------------------------------- jobs
    _JOB_SELECT = (
        "SELECT j.id, j.clip_id, j.platform, j.attempts, j.metadata, j.scheduled_at, c.path, c.score, "
        "c.duration, c.transcript, c.keyword_hits, c.hook, c.source_id, c.source_url, c.display_name, c.category, c.game, c.action, c.story "
        "FROM jobs j JOIN clips c ON c.id = j.clip_id ")

    def _job_by_id(self, job_id: int) -> Job | None:
        row = self._conn.execute(self._JOB_SELECT + "WHERE j.id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def _next_job(self, platform: str, now: float) -> Job | None:
        row = self._conn.execute(
            self._JOB_SELECT + "WHERE j.platform=? AND j.status='pending' AND j.next_attempt_at<=? "
            "ORDER BY c.score DESC, c.created_at ASC LIMIT 1",
            (platform, now),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        meta = Metadata(**json.loads(row["metadata"])) if row["metadata"] else None
        return Job(
            id=row["id"], clip_id=row["clip_id"], platform=row["platform"], attempts=row["attempts"],
            path=Path(row["path"]), score=row["score"], transcript=row["transcript"],
            keyword_hits=json.loads(row["keyword_hits"]), metadata=meta, hook=row["hook"] or "",
            duration=row["duration"], scheduled_at=row["scheduled_at"],
            source_id=row["source_id"] or "", source_url=row["source_url"] or "",
            display_name=row["display_name"] or "", category=row["category"] or "", game=row["game"] or "",
            action=row["action"] or "", story=row["story"] or "",
        )

    async def next_job(self, platform: str, now: float) -> Job | None:
        return await self._call(self._next_job, platform, now)

    def _exec(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._conn:
            self._conn.execute(sql, params)

    async def save_metadata(self, job_id: int, meta: Metadata) -> None:
        await self._call(self._exec, "UPDATE jobs SET metadata=?, title_key=?, updated_at=? WHERE id=?",
                         (json.dumps(meta.__dict__), normalise_title(meta.title), time.time(), job_id))

    async def set_status(self, job_id: int, status: str, *, error: str | None = None,
                         remote_id: str | None = None, next_attempt_at: float | None = None,
                         attempts_delta: int = 0) -> None:
        now = time.time()
        await self._call(
            self._exec,
            "UPDATE jobs SET status=?, error=COALESCE(?, error), remote_id=COALESCE(?, remote_id), "
            "next_attempt_at=COALESCE(?, next_attempt_at), attempts=attempts+?, updated_at=?, "
            "posted_at=CASE WHEN ?='posted' THEN ? ELSE posted_at END WHERE id=?",
            (status, error, remote_id, next_attempt_at, attempts_delta, now, status, now, job_id),
        )

    async def expire_stale(self, max_age_s: float) -> None:
        await self._call(
            self._exec,
            "UPDATE jobs SET status='expired', updated_at=? WHERE status='pending' AND clip_id IN "
            "(SELECT id FROM clips WHERE created_at < ?)",
            (time.time(), time.time() - max_age_s),
        )

    def _scalar(self, sql: str, params: tuple[Any, ...]) -> Any:
        row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    async def posts_since(self, platform: str, since: float) -> int:
        return int(await self._call(
            self._scalar, "SELECT COUNT(*) FROM jobs WHERE platform=? AND status='posted' AND posted_at>=?",
            (platform, since)) or 0)

    async def clip_exists(self, key: str) -> bool:
        """True if this moment (``clip_key``) was already clipped (re-processing guard).

        Clips from before folders existed are matched by their old file name.
        """
        value = await self._call(self._scalar, "SELECT 1 FROM clips WHERE clip_key=? OR path LIKE ?",
                                 (key, f"%/{key}.mp4"))
        return value is not None

    async def clips_since(self, source_id: str, since: float) -> int:
        return int(await self._call(self._scalar, "SELECT count(*) FROM clips WHERE source_id=? AND created_at>=?",
                                    (source_id, since)) or 0)

    def _get_clip(self, clip_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return dict(row) if row else None

    async def get_clip(self, clip_id: int) -> dict[str, Any] | None:
        return await self._call(self._get_clip, clip_id)

    async def last_post_at(self, platform: str) -> float | None:
        value = await self._call(
            self._scalar, "SELECT MAX(posted_at) FROM jobs WHERE platform=? AND status='posted'", (platform,))
        return float(value) if value is not None else None

    async def used_titles(self, platform: str, limit: int = 500) -> set[str]:
        def q() -> set[str]:
            rows = self._conn.execute(
                "SELECT title_key FROM jobs WHERE platform=? AND title_key IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT ?", (platform, limit)).fetchall()
            return {r[0] for r in rows}
        return await self._call(q)

    # -------------------------------------------------------- platform state
    async def next_eligible(self, platform: str) -> float | None:
        value = await self._call(
            self._scalar, "SELECT next_eligible_at FROM platform_state WHERE platform=?", (platform,))
        return float(value) if value is not None else None

    async def set_next_eligible(self, platform: str, ts: float) -> None:
        await self._call(
            self._exec,
            "INSERT INTO platform_state(platform, next_eligible_at) VALUES (?,?) "
            "ON CONFLICT(platform) DO UPDATE SET next_eligible_at=excluded.next_eligible_at",
            (platform, ts))

    async def quota_used(self, platform: str, day: str) -> int:
        return int(await self._call(
            self._scalar, "SELECT units FROM quota_usage WHERE platform=? AND day=?", (platform, day)) or 0)

    async def add_quota(self, platform: str, day: str, units: int) -> None:
        await self._call(
            self._exec,
            "INSERT INTO quota_usage(platform, day, units) VALUES (?,?,?) "
            "ON CONFLICT(platform, day) DO UPDATE SET units=units+excluded.units",
            (platform, day, units))

    def _summary(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT j.id, j.platform, j.status, j.attempts, j.remote_id, j.error, c.score, c.path, "
            "datetime(c.created_at,'unixepoch') AS created FROM jobs j JOIN clips c ON c.id=j.clip_id "
            "ORDER BY j.id DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]

    async def summary(self) -> list[dict[str, Any]]:
        return await self._call(self._summary)


# ==================================================================== uploaders
class Uploader(Protocol):
    platform: Platform  # youtube | facebook
    key: str  # account key (see modules.accounts)

    async def preflight(self) -> None:
        """Validate credentials; raise :class:`AuthError` if unusable."""

    async def upload(self, video: Path, meta: Metadata) -> str:
        """Upload and publish; return the platform's video id."""

    async def set_thumbnail(self, remote_id: str, image: Path) -> None:
        """Attach a custom thumbnail to an uploaded video."""


# -------------------------------------------------------------------- YouTube
# upload: publish Shorts. readonly: read live chat when the *source* is a YouTube stream.
# force-ssl: videos.update (fix titles/descriptions on live posts) — Google offers no narrower
# scope for editing; note it also permits deletion, so guard the token accordingly.
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
YOUTUBE_CHUNK_RETRIES = 8
YOUTUBE_THUMBNAIL_COST = 50
YOUTUBE_UPDATE_COST = 50
_YT_RETRY_STATUS = {429, 500, 502, 503, 504}
_YT_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}
_YT_DEFER_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}


def _youtube_client_config(s: Settings) -> dict[str, Any]:
    if not (s.youtube_client_id and s.youtube_client_secret):
        raise AuthError("YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET not configured")
    return {"installed": {
        "client_id": s.youtube_client_id.get_secret_value(),
        "client_secret": s.youtube_client_secret.get_secret_value(),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


def youtube_token_store(s: Settings) -> EncryptedTokenStore:
    """The main channel's token, or the first added channel's when there is no main one."""
    from clipper.modules.accounts import token_store

    main = token_store(s, "youtube")
    if not main.exists() and s.secrets_dir.is_dir():
        for f in sorted(s.secrets_dir.glob("youtube_*.enc")):
            if f.name != "youtube_token.enc":
                return token_store(s, f"youtube:{f.stem.partition('_')[2]}")
    return main


def youtube_consent(s: Settings, port: int = 8765, open_browser: bool = True,
                    on_url: Callable[[str], None] | None = None, timeout: float | None = None) -> dict[str, Any]:
    """Interactive Google consent (lets you pick the account/brand channel). Returns token data.

    ``on_url`` receives the sign-in link (so the dashboard can show it); ``timeout`` gives up
    if Google never redirects back (an abandoned sign-in must not block the next one).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(_youtube_client_config(s), YOUTUBE_SCOPES)
    if on_url is not None:
        build_url = flow.authorization_url

        def capture(**kwargs: Any) -> Any:
            url, state = build_url(**kwargs)
            on_url(url)
            return url, state

        flow.authorization_url = capture  # type: ignore[method-assign]
    try:
        creds = flow.run_local_server(port=port, open_browser=open_browser, access_type="offline",
                                      prompt="consent select_account", timeout_seconds=timeout,
                                      authorization_prompt_message="Open this URL to authorise: {url}")
    except Exception as exc:
        if type(exc).__name__ == "WSGITimeoutError":
            raise AuthError("the Google sign-in wasn't finished in time; start it again") from exc
        raise
    if not creds.refresh_token:
        raise AuthError("Google returned no refresh token; revoke app access and retry")
    return dict(json.loads(creds.to_json()))


def youtube_channel(info: dict[str, Any]) -> tuple[str, str]:
    """(channel id, channel title) for a token (1 quota unit). Blocking."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_info(info, YOUTUBE_SCOPES)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute(num_retries=2).get("items", [])
    if not items:
        raise AuthError("that Google account has no YouTube channel; create one or pick a brand channel")
    return str(items[0]["id"]), str(items[0]["snippet"].get("title", ""))


def youtube_authorize(s: Settings, port: int = 8765, open_browser: bool = True) -> None:
    """One-time interactive consent for the main channel (CLI). With ``open_browser=False`` the URL is printed."""
    from clipper.modules.accounts import token_store

    store = token_store(s, "youtube")
    store.save(youtube_consent(s, port, open_browser))
    log.info("YouTube token stored encrypted at %s", store.path)


def youtube_credentials(s: Settings, store: EncryptedTokenStore | None = None) -> Any:
    """Load the stored Google credentials, refreshing (and re-saving) if expired.

    Shared by the Shorts uploaders (one per channel, ``store``) and the YouTube live-chat
    reader / Discover (main channel). Blocking: call from a worker thread.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    store = store or youtube_token_store(s)
    try:
        info = store.load()
    except TokenStoreError as exc:
        raise AuthError(str(exc)) from exc
    creds = Credentials.from_authorized_user_info(info, YOUTUBE_SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as exc:  # revoked / expired refresh token
            raise AuthError(f"YouTube token refresh failed: {exc}") from exc
        store.save(json.loads(creds.to_json()))
        log.debug("YouTube access token refreshed")
    return creds


class YouTubeUploader:
    """YouTube Data API v3 resumable uploader with quota tracking."""

    platform: Platform = "youtube"

    def __init__(self, settings: Settings, queue: PublishQueue, key: str = "youtube") -> None:
        from clipper.modules.accounts import token_store

        self.s = settings
        self.queue = queue
        self.key = key
        self.store = token_store(settings, key)

    # ----------------------------------------------------------- credentials
    def _credentials(self) -> Any:
        return youtube_credentials(self.s, self.store)

    async def preflight(self) -> None:
        await asyncio.to_thread(self._credentials)
        log.info("YouTube credentials OK (%s)", self.key)

    async def _call(self, cost: int, fn: Callable[[Any], Any]) -> Any:
        """Run one quota-accounted YouTube API call in a worker thread."""
        from googleapiclient.discovery import build

        await self.queue.add_quota(self.platform, pacific_day(), cost)

        def run() -> Any:
            yt = build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)
            return fn(yt).execute(num_retries=3)

        return await asyncio.to_thread(run)

    async def set_thumbnail(self, remote_id: str, image: Path) -> None:
        """Custom thumbnail, 50 units (requires a phone-verified channel; 403 otherwise)."""
        from googleapiclient.http import MediaFileUpload

        await self._call(YOUTUBE_THUMBNAIL_COST, lambda yt: yt.thumbnails().set(
            videoId=remote_id, media_body=MediaFileUpload(str(image), mimetype="image/jpeg")))

    async def update_metadata(self, remote_id: str, meta: Metadata) -> None:
        """Replace title/description/tags of an existing upload (videos.update needs the full snippet)."""
        await self._call(YOUTUBE_UPDATE_COST, lambda yt: yt.videos().update(part="snippet", body={
            "id": remote_id,
            "snippet": {"title": meta.title, "description": meta.description, "tags": meta.tags,
                        "categoryId": self.s.youtube_category_id, "defaultLanguage": "en"},
        }))

    # ---------------------------------------------------------------- upload
    async def upload(self, video: Path, meta: Metadata) -> str:
        day = pacific_day()
        used = await self.queue.quota_used(self.platform, day)
        budget = int(self.s.youtube_daily_quota * (1 - self.s.youtube_quota_safety_margin))
        if used + self.s.youtube_upload_cost > budget:
            raise DeferPublish(f"YouTube quota budget reached ({used}/{budget})", next_pacific_midnight())
        # Units are charged per attempt, so account before calling the API.
        await self.queue.add_quota(self.platform, day, self.s.youtube_upload_cost)
        return await asyncio.to_thread(self._upload_sync, video, meta)

    def _upload_sync(self, video: Path, meta: Metadata) -> str:
        import httplib2
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        creds = self._credentials()
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "snippet": {
                "title": meta.title, "description": meta.description, "tags": meta.tags,
                "categoryId": self.s.youtube_category_id, "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": self.s.youtube_privacy_status,
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": False,
            },
        }
        media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        # next_chunk(num_retries=N) is google-api-python-client's built-in randomized
        # exponential backoff: it retries 5xx, 429, rate-limit 403s and connection
        # errors, resuming from the last acknowledged byte. We only classify what
        # is left once those retries are exhausted.
        response: dict[str, Any] | None = None
        while response is None:
            try:
                status, response = request.next_chunk(num_retries=YOUTUBE_CHUNK_RETRIES)
                if status:
                    log.debug("YouTube upload %d%%", int(status.progress() * 100))
            except HttpError as exc:
                code, reason = exc.resp.status, _yt_reason(exc)
                if reason in _YT_DEFER_REASONS:
                    raise DeferPublish(f"YouTube {reason}", next_pacific_midnight()) from exc
                if code == 401:
                    raise AuthError(f"YouTube 401: {reason}") from exc
                if code in _YT_RETRY_STATUS or reason in _YT_RETRY_REASONS:
                    raise TransientPublishError(f"YouTube HTTP {code} {reason} after retries") from exc
                raise PermanentPublishError(f"YouTube HTTP {code}: {reason or exc}") from exc
            except RefreshError as exc:
                raise AuthError(f"YouTube token refresh failed: {exc}") from exc
            except (OSError, httplib2.HttpLib2Error, TransportError) as exc:
                raise TransientPublishError(f"YouTube network error after retries: {exc!r}") from exc

        if response.get("status", {}).get("uploadStatus") in {"rejected", "failed"}:
            raise PermanentPublishError(f"YouTube rejected upload: {response.get('status')}")
        video_id = str(response["id"])
        log.info("YouTube upload complete: https://youtube.com/shorts/%s", video_id)
        return video_id


def _yt_reason(exc: Any) -> str:
    try:
        return str(json.loads(exc.content)["error"]["errors"][0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


# ------------------------------------------------------------------- Facebook
_FB_RATE_CODES = {4, 17, 32, 613}
_FB_AUTH_CODES = {10, 102, 190}


def facebook_token_store(s: Settings, key: str = "facebook") -> EncryptedTokenStore:
    from clipper.modules.accounts import token_store

    return token_store(s, key)


def _appsecret_proof(secret: str, token: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


async def facebook_authorize(s: Settings, user_token: str) -> None:
    """Derive a Page token from a user token and store it encrypted.

    With FACEBOOK_APP_SECRET set (production): the user token is first exchanged
    for a long-lived one, so the resulting Page token does not expire. Without
    it, the Page token inherits the short-lived user token's ~1 h lifetime — fine
    for a one-off post, and a warning is logged.
    """
    if not (s.facebook_app_id and s.facebook_page_id):
        raise AuthError("FACEBOOK_APP_ID / FACEBOOK_PAGE_ID required")
    secret = s.facebook_app_secret.get_secret_value() if s.facebook_app_secret else None
    base = f"https://graph.facebook.com/{s.facebook_graph_version}"
    user_token = user_token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", user_token):
        raise AuthError("that doesn't look like a Facebook access token (expected one unbroken token string)")
    async with httpx.AsyncClient(timeout=30) as client:
        if secret:
            r = await client.get(f"{base}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": s.facebook_app_id,
                "client_secret": secret, "fb_exchange_token": user_token})
            if r.is_error:
                raise AuthError(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
            user_token = r.json()["access_token"]
        params = {"fields": "access_token,name", "access_token": user_token}
        if secret:
            params["appsecret_proof"] = _appsecret_proof(secret, user_token)
        r = await client.get(f"{base}/{s.facebook_page_id}", params=params)
        if r.is_error:
            raise AuthError(f"Page token request failed: HTTP {r.status_code} {r.text[:200]}")
        page = r.json()
        if "access_token" not in page:
            raise AuthError("no Page token returned — does the user manage this Page and grant pages_show_list?")
        expires = await _token_expiry(client, base, page["access_token"], user_token, secret)
    permanent = expires == 0 if expires is not None else bool(secret)
    facebook_token_store(s).save({
        "page_access_token": page["access_token"], "page_name": page.get("name"),
        "obtained_at": time.time(), "long_lived": permanent, "expires_at": expires,
    })
    log.info("Facebook Page token for %r stored encrypted (%s)", page.get("name"),
             "never expires" if permanent else
             "TEMPORARY: extend the token in the Access Token Debugger or add the App secret, then re-run")
    print(f"Facebook Page {page.get('name')!r} connected: "
          f"{'token never expires' if permanent else 'temporary token (expires)'}")


async def facebook_pages(s: Settings, user_token: str) -> list[dict[str, Any]]:
    """Pages this user manages, each with its own Page token (non-expiring when the App secret is set).

    Returns ``[{"id", "name", "access_token", "can_post", "long_lived"}]``.
    """
    secret = s.facebook_app_secret.get_secret_value() if s.facebook_app_secret else None
    base = f"https://graph.facebook.com/{s.facebook_graph_version}"
    user_token = user_token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", user_token):
        raise AuthError("that doesn't look like a Facebook access token (expected one unbroken token string)")
    async with httpx.AsyncClient(timeout=30) as client:
        if secret:
            if not s.facebook_app_id:
                raise AuthError("FACEBOOK_APP_ID is not set")
            r = await client.get(f"{base}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": s.facebook_app_id,
                "client_secret": secret, "fb_exchange_token": user_token})
            if r.is_error:
                raise AuthError(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
            user_token = r.json()["access_token"]
        params: dict[str, Any] = {"fields": "id,name,access_token,tasks", "limit": 100, "access_token": user_token}
        if secret:
            params["appsecret_proof"] = _appsecret_proof(secret, user_token)
        pages: list[dict[str, Any]] = []
        url: str | None = f"{base}/me/accounts"
        while url and len(pages) < 500:
            r = await client.get(url, params=params)
            if r.is_error:
                raise AuthError(f"couldn't list your Pages: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            pages += body.get("data", [])
            url, params = body.get("paging", {}).get("next"), {}
        out = []
        for p in pages:
            if not p.get("access_token"):
                continue
            # Ask Facebook how long this Page token lasts (0 = never expires) instead of guessing.
            expires = await _token_expiry(client, base, p["access_token"], user_token, secret)
            out.append({"id": str(p["id"]), "name": p.get("name", ""), "access_token": p["access_token"],
                        "can_post": bool({"CREATE_CONTENT", "MANAGE"} & set(p.get("tasks") or ["CREATE_CONTENT"])),
                        "expires_at": expires,
                        "long_lived": expires == 0 if expires is not None else bool(secret)})
    return out


async def _token_expiry(client: httpx.AsyncClient, base: str, token: str, user_token: str,
                        secret: str | None) -> float | None:
    """Unix time the token expires, 0 if it never does, None if Facebook wouldn't say."""
    params = {"input_token": token, "access_token": user_token}
    if secret:
        params["appsecret_proof"] = _appsecret_proof(secret, user_token)
    try:
        r = await client.get(f"{base}/debug_token", params=params)
        data = r.json().get("data", {}) if r.is_success else {}
    except (httpx.HTTPError, ValueError):
        return None
    if "expires_at" not in data:
        return None
    return float(data.get("expires_at") or 0)


class FacebookReelsUploader:
    """Meta Graph API Reels publisher (start → binary upload → finish → poll)."""

    platform: Platform = "facebook"
    REQUIRED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}

    def __init__(self, settings: Settings, key: str = "facebook", page_id: str | None = None) -> None:
        page_id = page_id or (key.partition(":")[2] or settings.facebook_page_id)
        if not page_id:
            raise AuthError("no Facebook Page id for this account")
        self.s = settings
        self.key = key
        self.page_id = page_id
        self.base = f"https://graph.facebook.com/{settings.facebook_graph_version}"
        self.secret = settings.facebook_app_secret.get_secret_value() if settings.facebook_app_secret else None
        self.store = facebook_token_store(settings, key)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15))
        self._cooldown_until = 0.0

    def _token(self) -> str:
        try:
            return str(self.store.load()["page_access_token"])
        except (TokenStoreError, KeyError) as exc:
            raise AuthError(f"Facebook token unavailable: {exc}") from exc

    # ---------------------------------------------------------------- graph
    def _check_usage(self, headers: httpx.Headers) -> None:
        """Back off proactively when Meta reports high rate-limit utilisation."""
        peak, regain_min = 0.0, 0.0
        if raw := headers.get("x-app-usage"):
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                peak = max([peak, *map(float, json.loads(raw).values())])
        if raw := headers.get("x-business-use-case-usage"):
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                for entries in json.loads(raw).values():
                    for e in entries:
                        peak = max(peak, float(e.get("call_count", 0)), float(e.get("total_time", 0)),
                                   float(e.get("total_cputime", 0)))
                        regain_min = max(regain_min, float(e.get("estimated_time_to_regain_access", 0)))
        if peak >= 85:
            wait = max(regain_min * 60, 1800)
            self._cooldown_until = max(self._cooldown_until, time.time() + wait)
            log.warning("Meta rate-limit usage at %.0f%% — cooling down %.0f min", peak, wait / 60)

    async def _graph(self, method: str, path: str, params: dict[str, Any] | None = None,
                     *, app_token: bool = False) -> dict[str, Any]:
        if app_token:
            if not (self.s.facebook_app_id and self.secret):
                raise AuthError("app credentials missing")
            auth = {"access_token": f"{self.s.facebook_app_id}|{self.secret}"}
        else:
            auth = self._auth_params()
        payload = {**(params or {}), **auth}

        async def call() -> dict[str, Any]:
            try:
                if method == "GET":
                    r = await self._client.get(self.base + path, params=payload)
                else:
                    r = await self._client.post(self.base + path, data=payload)
            except httpx.TransportError as exc:
                raise RetryableError(f"network: {exc!r}") from exc
            self._check_usage(r.headers)
            return self._parse(r)

        return await retry_async(call, what=f"Graph {method} {path}", attempts=4, base=5, cap=120)

    def _parse(self, r: httpx.Response) -> dict[str, Any]:
        try:
            body: dict[str, Any] = r.json()
        except ValueError:
            body = {}
        if r.is_success and "error" not in body:
            return body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = int(err.get("code", 0) or 0)
        msg = f"HTTP {r.status_code} code={code} {err.get('message', r.text[:200])}"
        if r.status_code == 429 or code in _FB_RATE_CODES or 80000 <= code < 80100:
            retry_after = float(r.headers.get("retry-after", 0) or 0) or 300.0
            raise RetryableError(msg, retry_after=retry_after)
        if r.status_code >= 500 or err.get("is_transient"):
            raise RetryableError(msg)
        if code in _FB_AUTH_CODES or 200 <= code < 300:
            raise AuthError(msg)
        raise PermanentPublishError(msg)

    def _auth_params(self) -> dict[str, str]:
        token = self._token()
        auth = {"access_token": token}
        if self.secret:
            auth["appsecret_proof"] = _appsecret_proof(self.secret, token)
        return auth

    async def set_thumbnail(self, remote_id: str, image: Path) -> None:
        """Upload a preferred custom thumbnail for a published video/Reel."""
        data = await asyncio.to_thread(image.read_bytes)

        async def call() -> dict[str, Any]:
            try:
                r = await self._client.post(
                    f"{self.base}/{remote_id}/thumbnails",
                    data={"is_preferred": "true", **self._auth_params()},
                    files={"source": (image.name, data, "image/jpeg")}, timeout=120)
            except httpx.TransportError as exc:
                raise RetryableError(f"network: {exc!r}") from exc
            self._check_usage(r.headers)
            return self._parse(r)

        await retry_async(call, what="Reel thumbnail upload", attempts=3, base=5, cap=60)

    async def update_metadata(self, remote_id: str, meta: Metadata) -> None:
        """Replace the caption of an existing video/Reel."""
        await self._graph("POST", f"/{remote_id}", {"description": meta.description})

    # ---------------------------------------------------------- lifecycle
    async def preflight(self) -> None:
        token = self._token()
        if not (self.secret and self.s.facebook_app_id):
            # No app secret → can't use debug_token; a cheap /me call still proves the token works.
            await self._graph("GET", "/me", {"fields": "id,name"})
            log.info("Facebook credentials OK (Page %s)", self.page_id)
            return
        if self.secret and self.s.facebook_app_id:
            info = (await self._graph("GET", "/debug_token", {"input_token": token}, app_token=True)).get("data", {})
            if not info.get("is_valid"):
                raise AuthError(f"Facebook Page token invalid: {info.get('error')}")
            missing = self.REQUIRED_SCOPES - set(info.get("scopes", []))
            if missing:
                raise AuthError(f"Facebook token missing permissions: {sorted(missing)}")
        log.info("Facebook credentials OK (page %s)", self.page_id)

    async def upload(self, video: Path, meta: Metadata) -> str:
        if time.time() < self._cooldown_until:
            raise DeferPublish("Meta rate-limit cooldown", self._cooldown_until)
        try:
            start = await self._graph("POST", f"/{self.page_id}/video_reels", {"upload_phase": "start"})
            video_id = str(start["video_id"])
            upload_url = start.get("upload_url") or (
                f"https://rupload.facebook.com/video-upload/{self.s.facebook_graph_version}/{video_id}")
            await retry_async(lambda: self._transfer(upload_url, video), what="Reel binary upload",
                              attempts=5, base=5, cap=120)
            await self._graph("POST", f"/{self.page_id}/video_reels", {
                "upload_phase": "finish", "video_id": video_id,
                "video_state": self.s.facebook_video_state, "description": meta.description,
            })
            await self._wait_processed(video_id)
        except RetryableError as exc:
            raise TransientPublishError(str(exc)) from exc
        log.info("Facebook Reel published: video_id=%s (state=%s)", video_id, self.s.facebook_video_state)
        return video_id

    async def _transfer(self, url: str, video: Path) -> None:
        data = await asyncio.to_thread(video.read_bytes)
        headers = {"Authorization": f"OAuth {self._token()}", "offset": "0",
                   "file_size": str(len(data)), "Content-Type": "application/octet-stream"}
        try:
            r = await self._client.post(url, content=data, headers=headers, timeout=600)
        except httpx.TransportError as exc:
            raise RetryableError(f"network: {exc!r}") from exc
        body = self._parse(r)
        if not body.get("success", True):
            raise RetryableError(f"rupload not successful: {body}")

    async def _wait_processed(self, video_id: str, timeout: float = 900) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = (await self._graph("GET", f"/{video_id}", {"fields": "status"})).get("status", {})
            vstat = status.get("video_status")
            phases = [status.get(p, {}).get("status") for p in ("uploading_phase", "processing_phase", "publishing_phase")]
            if vstat == "error" or "error" in phases:
                raise PermanentPublishError(f"Reel processing failed: {status}")
            if vstat in {"ready", "published"} or phases[-1] == "complete":
                return
            if self.s.facebook_video_state == "DRAFT" and phases[1] == "complete":
                return  # drafts never enter the publishing phase
            await asyncio.sleep(15)
        log.warning("Reel %s still processing after %.0fs; assuming success", video_id, timeout)

    async def aclose(self) -> None:
        await self._client.aclose()


# ==================================================================== scheduler
@dataclass
class PlatformLimits:
    """Per-platform posting pace (editable at runtime from the dashboard)."""

    max_per_day: int
    min_spacing_minutes: int
    enabled: bool = True


class Publisher:
    """Cadence-enforcing scheduler that drains the queue into each platform."""

    def __init__(self, settings: Settings, queue: PublishQueue, uploaders: list[Uploader]) -> None:
        self.s = settings
        self.queue = queue
        self.uploaders = uploaders
        self.meta = MetadataGenerator()
        self.meta.trend_dir = settings.state_dir  # live YouTube search trends for hashtags/tags (cached a day)
        self._paused: dict[str, str] = {}
        self.hold = False  # operator kill-switch: when True nothing is posted (jobs stay queued)
        self.limits: dict[str, PlatformLimits] = {
            up.key: PlatformLimits(settings.max_posts_per_day, settings.min_post_spacing_minutes)
            for up in uploaders}
        self.wake = asyncio.Event()  # set by "post now" / schedule changes to tick immediately
        self.active: set[int] = set()  # clip ids uploading right now (the library mover waits for them)
        self.locked: set[int] = set()  # clip ids being moved between drives (not posted meanwhile)
        self.on_posted: Callable[[int], None] | None = None  # clip id → refresh its info.json

    @property
    def paused(self) -> dict[str, str]:
        """Accounts paused by auth problems → reason."""
        return dict(self._paused)

    def limit(self, key: str) -> PlatformLimits:
        return self.limits.get(key) or PlatformLimits(self.s.max_posts_per_day, self.s.min_post_spacing_minutes)

    def uploader(self, key: str) -> Uploader | None:
        return next((u for u in self.uploaders if u.key == key), None)

    async def unpause(self, key: str) -> None:
        """Re-check credentials after a reconnect and resume the account (no restart needed)."""
        up = self.uploader(key)
        if up is None:
            raise KeyError(key)
        await up.preflight()  # raises AuthError if still broken
        self._paused.pop(key, None)
        log.info("%s publishing resumed", key)
        self.wake.set()

    async def add_uploader(self, up: Uploader) -> None:
        """Start posting to a newly connected account (or replace a reconnected one)."""
        self.uploaders = [u for u in self.uploaders if u.key != up.key] + [up]
        self.limits.setdefault(up.key, PlatformLimits(self.s.max_posts_per_day, self.s.min_post_spacing_minutes))
        try:
            await up.preflight()
            self._paused.pop(up.key, None)
        except AuthError as exc:
            self._pause(up.key, str(exc))
            raise
        finally:
            self.wake.set()

    def remove_uploader(self, key: str) -> None:
        self.uploaders = [u for u in self.uploaders if u.key != key]
        self._paused.pop(key, None)
        self.limits.pop(key, None)

    async def run(self) -> None:
        for up in list(self.uploaders):
            try:
                await up.preflight()
            except AuthError as exc:
                self._pause(up.key, str(exc))
            except Exception as exc:  # network down at startup: try again when posting
                log.warning("%s: couldn't check credentials yet (%s)", up.key, exc)
        while True:
            await self.queue.expire_stale(self.s.clip_max_queue_age_hours * 3600)
            for up in list(self.uploaders):
                if up.key not in self._paused and not self.hold and self.limit(up.key).enabled:
                    try:
                        await self._tick(up)
                    except Exception:  # never let one account kill the scheduler
                        log.exception("%s: scheduler tick failed", up.key)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=self.s.publish_poll_interval_s)
            self.wake.clear()

    def _pause(self, key: str, why: str) -> None:
        self._paused[key] = why
        log.error("%s publishing PAUSED: %s (reconnect it under Connections)", key, why)

    def _jitter(self) -> float:
        return random.uniform(0, self.s.post_jitter_max_minutes * 60)

    async def _tick(self, up: Uploader) -> None:
        now = time.time()
        lim = self.limit(up.key)
        daily_full = await self.queue.posts_since(up.key, now - 86_400) >= lim.max_per_day

        # 1) Operator-scheduled posts (incl. "post now") go at their time; spacing is the
        #    operator's call, but the hard daily cap still protects the account.
        due = await self.queue.due_scheduled(up.key, now)
        if due is not None:
            if daily_full:
                oldest = await self.queue.oldest_post_since(up.key, now - 86_400) or now
                retry = oldest + 86_400 + 60
                log.warning("%s: daily limit (%d) reached; scheduled job %d moved to %s", up.key,
                            lim.max_per_day, due.id,
                            dt.datetime.fromtimestamp(retry).isoformat(timespec="minutes"))
                await self.queue.set_status(due.id, "scheduled", next_attempt_at=retry,
                                            error=f"daily limit reached; will post at {time.strftime('%H:%M', time.localtime(retry))}")
                return
            await self._post(up, due, now)
            return

        # 2) Automatic cadence: best-scoring queued clip, spaced and capped.
        eligible = await self.queue.next_eligible(up.key)
        if eligible is None:  # first run: don't fire immediately on startup
            await self.queue.set_next_eligible(up.key, now + self._jitter())
            return
        if now < eligible:
            return
        last = await self.queue.last_post_at(up.key)  # spacing from real history, not just state
        if last is not None and now < last + lim.min_spacing_minutes * 60:
            return
        if daily_full:
            return
        job = await self.queue.next_job(up.key, now)
        if job is not None:
            await self._post(up, job, now)

    async def _post(self, up: Uploader, job: Job, now: float) -> None:
        from clipper.modules.library import offline

        retry_status = "scheduled" if job.scheduled_at is not None else "pending"
        if job.clip_id in self.locked:  # moving to another drive right now: next tick
            return
        if not job.path.is_file():
            if offline(job.path):  # its drive is unplugged: wait, this is not a deletion
                await self.queue.set_status(job.id, retry_status, next_attempt_at=now + 600,
                                            error="drive not connected; will post when it's back")
                return
            # deleted by the operator: drop it quietly, it isn't a failure
            await self.queue.set_status(job.id, "expired", error="file removed")
            return
        if up.platform == "facebook" and (job.duration or 0) > FACEBOOK_MAX_SECONDS + 0.5:
            await self.queue.set_status(job.id, "failed", error=f"Facebook Reels must be ≤ {FACEBOOK_MAX_SECONDS:.0f}s "
                                        f"(this clip is {job.duration:.0f}s); re-cut it shorter")
            return

        meta = job.metadata
        if meta is None:
            configured = self.s.all_sources()
            legacy = configured[0] if configured else None
            ctx = ClipContext(channel=job.source_id or (legacy.slug if legacy else "local"),
                              display_name=job.display_name or (legacy.name if legacy else ""),
                              source_url=job.source_url or (legacy.source_url if legacy else ""), hook=job.hook,
                              category=job.category or (legacy.category if legacy else None), transcript=job.transcript,
                              keyword_hits=job.keyword_hits, game=job.game or None,
                              action=job.action or None, story=job.story)
            meta = self.meta.generate(ctx, up.platform, await self.queue.used_titles(up.key))
            await self.queue.save_metadata(job.id, meta)

        log.info("%s: uploading job %d (%s, score %.1f) — %r", up.key, job.id,
                 "scheduled" if job.scheduled_at is not None else "auto", job.score, meta.title)
        await self.queue.set_status(job.id, "uploading")
        self.active.add(job.clip_id)
        try:
            remote_id = await up.upload(job.path, meta)
        except DeferPublish as exc:
            log.warning("%s: deferred until %s (%s)", up.key,
                        dt.datetime.fromtimestamp(exc.until).isoformat(timespec="minutes"), exc)
            await self.queue.set_status(job.id, retry_status, next_attempt_at=exc.until, error=str(exc))
            await self.queue.set_next_eligible(up.key, exc.until)
        except AuthError as exc:
            await self.queue.set_status(job.id, retry_status, error=str(exc))
            self._pause(up.key, str(exc))
        except PermanentPublishError as exc:
            log.error("%s: job %d failed permanently: %s", up.key, job.id, exc)
            await self.queue.set_status(job.id, "failed", error=str(exc), attempts_delta=1)
        except Exception as exc:  # TransientPublishError and anything unexpected
            attempts = job.attempts + 1
            if attempts >= self.s.max_publish_attempts:
                log.error("%s: job %d failed after %d attempts: %s", up.key, job.id, attempts, exc)
                await self.queue.set_status(job.id, "failed", error=str(exc), attempts_delta=1)
            else:
                retry_at = now + backoff_delay(attempts, base=120, cap=3600) + 60
                log.warning("%s: job %d attempt %d failed (%s); retrying later", up.key, job.id, attempts, exc)
                await self.queue.set_status(job.id, retry_status, error=str(exc), next_attempt_at=retry_at,
                                            attempts_delta=1)
        else:
            await self.queue.set_status(job.id, "posted", remote_id=remote_id, error="")
            if self.s.thumbnails and job.thumbnail is not None:
                try:
                    await up.set_thumbnail(remote_id, job.thumbnail)
                    log.info("%s: custom thumbnail set for %s", up.key, remote_id)
                except Exception as exc:  # the post is live; a thumbnail failure must not fail it
                    log.warning("%s: thumbnail not applied (%s)", up.key, exc)
            nxt = now + self.limit(up.key).min_spacing_minutes * 60 + self._jitter()
            await self.queue.set_next_eligible(up.key, nxt)
            log.info("%s: posted job %d; next automatic slot ≥ %s", up.key, job.id,
                     dt.datetime.fromtimestamp(nxt).isoformat(timespec="minutes"))
            if self.on_posted is not None:
                with contextlib.suppress(Exception):
                    self.on_posted(job.clip_id)
        finally:
            self.active.discard(job.clip_id)


def build_uploaders(settings: Settings, queue: PublishQueue, accounts: Any = None) -> list[Uploader]:
    """One uploader per connected account (``accounts``: an AccountRegistry), skipping broken ones."""
    from clipper.modules.accounts import AccountRegistry

    registry = accounts or AccountRegistry(settings)
    uploaders: list[Uploader] = []
    for acct in registry.all():
        if acct.platform not in settings.publish_platforms or not registry.connected(acct.key):
            continue
        try:
            uploaders.append(make_uploader(settings, queue, acct.key, acct.ext_id or None))
        except (AuthError, TokenStoreError) as exc:
            log.error("%s disabled: %s", acct.key, exc)
    return uploaders


def make_uploader(settings: Settings, queue: PublishQueue, key: str, ext_id: str | None = None) -> Uploader:
    if account_platform(key) == "youtube":
        return YouTubeUploader(settings, queue, key)
    return FacebookReelsUploader(settings, key, ext_id)
