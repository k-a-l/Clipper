"""Runtime controller: owns every long-running component and lets them change while running.

``clipper run`` and ``clipper ui`` both drive a :class:`Daemon`. The web
dashboard calls its methods to add/remove live sources, toggle per-source publishing,
start video (VOD) jobs, pause posting and act on clips, all without a restart.

Runtime-managed sources persist in ``storage/state/sources.json``. On first start that
file is seeded from ``SOURCES`` / the single-source settings in ``.env``; afterwards the
file is the source of truth (edit sources in the dashboard, or delete the file to reseed).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipper.config import FACEBOOK_MAX_SECONDS, ClipStyle, RenderOptions, Settings, SourceConfig, account_platform
from clipper.modules import library as lib
from clipper.modules.accounts import AccountRegistry, token_store
from clipper.modules.audio_highlights import AudioSpikeWatcher
from clipper.modules.chat_analyzer import ChatAnalyzer, ClipEvent
from clipper.modules.pipeline import ClipPipeline
from clipper.modules.discover import Discover
from clipper.modules.publisher import (AuthError, PlatformLimits, Publisher, PublishQueue, build_uploaders, make_uploader,
                               pacific_day)
from clipper.modules.utils import pot_helper_up, EncryptedTokenStore, TokenStoreError
from clipper.modules.stream_recorder import StreamRecorder
from clipper.modules import yt_auth
from clipper.modules.subtitles import Transcriber
from clipper.modules.video_editor import VideoEditor

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 75  # main.py re-launches the dashboard when it sees this
_CODE = Path(__file__).resolve().parent.parent


def code_signature() -> str:
    """Changes whenever the app's code changes on disk (to tell the operator to restart)."""
    files = [*sorted((_CODE / "modules").glob("*.py")), _CODE / "config.py", _CODE / "main.py",
             _CODE / "modules" / "static" / "index.html"]
    parts = []
    for f in files:
        try:
            st = f.stat()
            parts.append(f"{f.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            continue
    import hashlib

    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


REMOTE_URLS = {"youtube": "https://youtube.com/shorts/{}", "facebook": "https://www.facebook.com/reel/{}"}


# ------------------------------------------------------------------- log buffer
class LogBuffer(logging.Handler):
    """Keeps the last N formatted records and fans new ones out to SSE subscribers."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__(level=logging.INFO)
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.seq = 0
        self.loop: asyncio.AbstractEventLoop | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seq += 1
            item = {"seq": self.seq, "ts": record.created, "level": record.levelname.upper(),
                    "logger": record.name.removeprefix("modules."), "msg": record.getMessage()}
            self.records.append(item)
            if self.loop is not None:
                for q in list(self.subscribers):
                    self.loop.call_soon_threadsafe(_offer, q, item)
        except Exception:  # logging must never raise
            self.handleError(record)


def _offer(q: asyncio.Queue[dict[str, Any]], item: dict[str, Any]) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        q.put_nowait(item)


# ------------------------------------------------------------------- runtime types
@dataclass
class SourceRuntime:
    config: SourceConfig
    recorder: StreamRecorder
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    added_at: float = field(default_factory=time.time)


@dataclass
class VodJob:
    id: str
    url: str
    clips_requested: int
    publish: bool
    category: str | None
    style: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"  # queued|downloading|analysing|rendering|done|failed
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    total: int = 0
    done: int = 0
    clips: list[str] = field(default_factory=list)
    skipped: int = 0
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None


def lib_size(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


class Daemon:
    def __init__(self, settings: Settings, *, publish: bool = True) -> None:
        self.s = settings
        self.publish_enabled = publish
        self.started_at = time.time()
        self.queue = PublishQueue(settings.state_dir / "publisher.db")
        self.events: asyncio.Queue[ClipEvent] = asyncio.Queue(maxsize=500)
        self.recorders: dict[str, StreamRecorder] = {}
        self.sources: dict[str, SourceRuntime] = {}
        self.pipeline = ClipPipeline(settings, self.recorders, VideoEditor(settings), Transcriber(settings), self.queue)
        self.publisher: Publisher | None = None
        self.vod_jobs: dict[str, VodJob] = {}
        self._vod_sem = asyncio.Semaphore(settings.vod_concurrency)
        self._core: list[asyncio.Task[Any]] = []
        self._sources_file = settings.state_dir / "sources.json"
        self.rerenders: dict[int, dict[str, Any]] = {}  # clip_id → {status, error, started}
        self.pipeline.on_new_clip = self._wake_publisher
        self.discover = Discover(settings)
        self._runtime_file = settings.state_dir / "runtime.json"
        self.connect_state: dict[str, Any] = {}
        self.connect_url: str | None = None  # Google sign-in link while one is waiting
        self.accounts = AccountRegistry(settings)
        self.pipeline.accounts = self.accounts
        self.move = lib.MoveState()
        self._move_lock = asyncio.Lock()
        self._fb_sessions: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._drives: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._apply_library_setting()
        self.build = code_signature()
        self.restart_requested = False
        self.stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------- credentials
    def _cred_store(self) -> EncryptedTokenStore | None:
        key = self.s.token_encryption_key.get_secret_value() if self.s.token_encryption_key else None
        try:
            return EncryptedTokenStore(self.s.secrets_dir / "app_credentials.enc", key)
        except TokenStoreError:
            return None

    def _load_app_credentials(self) -> None:
        """App secrets saved from the dashboard (encrypted) fill anything missing from .env."""
        from pydantic import SecretStr

        store = self._cred_store()
        if store is None or not store.exists():
            return
        try:
            data = store.load()
        except TokenStoreError as exc:
            log.error("cannot read saved app credentials: %s", exc)
            return
        if data.get("facebook_app_secret") and not self.s.facebook_app_secret:
            self.s.facebook_app_secret = SecretStr(data["facebook_app_secret"])
        if data.get("twitch_client_id") and not self.s.twitch_client_id:
            self.s.twitch_client_id = data["twitch_client_id"]
        if data.get("twitch_client_secret") and not self.s.twitch_client_secret:
            self.s.twitch_client_secret = SecretStr(data["twitch_client_secret"])

    def _save_app_credential(self, **values: str) -> None:
        store = self._cred_store()
        if store is None:
            raise ValueError("TOKEN_ENCRYPTION_KEY is not set, so secrets can't be stored safely")
        data = store.load() if store.exists() else {}
        data.update({k: v for k, v in values.items() if v})
        store.save(data)

    # --------------------------------------------------------------- accounts
    async def add_youtube_channel(self) -> dict[str, Any]:
        """Google sign-in in the browser; the channel you pick is added (or reconnected if known)."""
        from clipper.modules.publisher import youtube_channel, youtube_consent

        if self.connect_state.get("youtube") == "waiting":
            raise ValueError("a YouTube sign-in is already waiting in your browser")
        self.connect_state["youtube"] = "waiting"
        try:
            await self._resolve_accounts()  # know the main channel's id before deciding the new key
            self.connect_url = None

            def got_url(url: str) -> None:
                self.connect_url = url

            info = await asyncio.to_thread(youtube_consent, self.s, 8765, True, got_url, 600)
            channel_id, title = await asyncio.to_thread(youtube_channel, info)
            await self.queue.add_quota("youtube", pacific_day(), 1)
            acct = self.accounts.upsert("youtube", channel_id, title)
            token_store(self.s, acct.key).save(info)
            await self._attach(acct.key)
            self.connect_state["youtube"] = f"done: {title}"
            log.info("YouTube channel %r connected as %s", title, acct.key)
            return self.accounts.describe(acct.key)
        except Exception as exc:
            self.connect_state["youtube"] = f"failed: {str(exc)[:200]}"
            raise
        finally:
            self.connect_url = None

    async def facebook_find_pages(self, user_token: str, app_secret: str | None) -> dict[str, Any]:
        """List the Pages a Facebook login manages so the operator can pick which to post to."""
        from pydantic import SecretStr

        from clipper.modules.publisher import facebook_pages

        if app_secret:
            self.s.facebook_app_secret = SecretStr(app_secret.strip())
            self._save_app_credential(facebook_app_secret=app_secret.strip())
        pages = await facebook_pages(self.s, user_token)
        if not pages:
            raise ValueError("this login doesn't manage any Pages (or pages_show_list wasn't granted)")
        nonce = uuid.uuid4().hex
        now = time.time()
        self._fb_sessions = {k: v for k, v in self._fb_sessions.items() if v[0] > now}
        self._fb_sessions[nonce] = (now + 900, {p["id"]: p for p in pages})
        known = {a.ext_id: a for a in self.accounts.all() if a.platform == "facebook"}
        return {"session": nonce, "long_lived": all(p["long_lived"] for p in pages), "pages": [
            {"id": p["id"], "name": p["name"], "can_post": p["can_post"], "connected": p["id"] in known,
             "permanent": p["long_lived"],
             "suggested": p["id"] in known or p["id"] == (self.s.facebook_page_id or "")} for p in pages]}

    async def facebook_add_pages(self, session: str, page_ids: list[str]) -> list[dict[str, Any]]:
        entry = self._fb_sessions.get(session)
        if entry is None or entry[0] < time.time():
            raise ValueError("that Page list expired; find your Pages again")
        pages = entry[1]
        unknown = [i for i in page_ids if i not in pages]
        if unknown or not page_ids:
            raise ValueError("pick at least one of the listed Pages")
        added = []
        for pid in page_ids:
            p = pages[pid]
            acct = self.accounts.upsert("facebook", pid, p["name"])
            token_store(self.s, acct.key).save({"page_access_token": p["access_token"], "page_name": p["name"],
                                                "obtained_at": time.time(), "long_lived": p["long_lived"],
                                                "expires_at": p.get("expires_at")})
            try:
                await self._attach(acct.key)
            except AuthError as exc:
                log.error("Facebook Page %r connected but its token was rejected: %s", p["name"], exc)
            added.append(self.accounts.describe(acct.key))
            log.info("Facebook Page %r connected as %s (%s)", p["name"], acct.key,
                     "permanent token" if p["long_lived"] else "temporary token")
        self._fb_sessions.pop(session, None)
        return added

    async def _attach(self, key: str) -> None:
        """Start (or restart) posting to an account after its token changed."""
        if self.publisher is None:
            return
        acct = self.accounts.get(key)
        if acct is None or acct.platform not in self.s.publish_platforms:
            return
        up = make_uploader(self.s, self.queue, key, acct.ext_id or None)
        await self.publisher.add_uploader(up)
        self._apply_limits()

    def remove_account(self, key: str) -> dict[str, Any]:
        """Disconnect a channel/Page: its token is deleted and its unposted jobs are dropped."""
        if self.accounts.get(key) is None:
            raise KeyError(key)
        if self.publisher is not None:
            self.publisher.remove_uploader(key)
        with self._db() as db:
            n = db.execute("UPDATE jobs SET status='expired', error='account removed', updated_at=? "
                           "WHERE platform=? AND status IN ('pending','scheduled','failed')",
                           (time.time(), key)).rowcount
        acct = self.accounts.remove(key)
        self._write_runtime()
        log.info("%s removed (%d unposted job(s) dropped)", acct.label, n)
        return {"removed": key, "dropped_jobs": n}

    def set_account_default(self, key: str, default: bool) -> dict[str, Any]:
        if self.accounts.get(key) is None:
            raise KeyError(key)
        self.accounts.set_default(key, default)
        return self.accounts.describe(key)

    async def _resolve_accounts(self) -> None:
        """Fill in channel ids/names for accounts connected before they were tracked (1 unit each)."""
        from clipper.modules.publisher import youtube_channel

        changed = False
        for acct in self.accounts.all():
            if acct.ext_id and acct.name:
                continue
            store = token_store(self.s, acct.key)
            try:
                if acct.platform == "youtube":
                    from clipper.modules.publisher import youtube_credentials

                    creds = await asyncio.to_thread(youtube_credentials, self.s, store)
                    channel_id, title = await asyncio.to_thread(youtube_channel, json.loads(creds.to_json()))
                    await self.queue.add_quota("youtube", pacific_day(), 1)
                    acct.ext_id, acct.name = channel_id, title
                else:
                    acct.name = acct.name or str(store.load().get("page_name") or "")
                    acct.ext_id = acct.ext_id or (acct.key.partition(":")[2] or str(self.s.facebook_page_id or ""))
                changed = True
            except Exception as exc:  # broken/expired token: shown as paused; resolved after reconnect
                log.debug("can't resolve %s yet: %s", acct.key, exc)
        if changed:
            self.accounts.save()

    async def connect_twitch(self, client_id: str, client_secret: str) -> None:
        """Verify the Twitch app with a real API call first; only then keep and store it."""
        from pydantic import SecretStr

        old = (self.s.twitch_client_id, self.s.twitch_client_secret)
        self.s.twitch_client_id = client_id.strip()
        self.s.twitch_client_secret = SecretStr(client_secret.strip())
        candidate = Discover(self.s)
        try:
            await candidate.find("twitch_live", refresh=True)
        except Exception:
            self.s.twitch_client_id, self.s.twitch_client_secret = old
            raise
        self.discover = candidate
        self._save_app_credential(twitch_client_id=client_id.strip(), twitch_client_secret=client_secret.strip())

    def connections(self) -> dict[str, Any]:
        paused = self.publisher.paused if self.publisher else {}
        live = {u.key for u in self.publisher.uploaders} if self.publisher else set()
        accounts = []
        for a in self.accounts.all():
            info: dict[str, Any] = {}
            try:
                store = token_store(self.s, a.key)
                if a.platform == "facebook" and store.exists():
                    data = store.load()
                    info = {"long_lived": bool(data.get("long_lived")), "since": data.get("obtained_at"),
                            "expires_at": data.get("expires_at")}
                connected = store.exists()
            except TokenStoreError:
                connected = False
            accounts.append(self.accounts.describe(a.key) | info | {
                "connected": connected, "active": a.key in live, "paused": paused.get(a.key)})
        return {
            "accounts": accounts,
            "facebook_app_secret_saved": bool(self.s.facebook_app_secret),
            "youtube_signin": self.connect_state.get("youtube"),
            "youtube_signin_url": self.connect_url if self.connect_state.get("youtube") == "waiting" else None,
            "twitch": {"connected": self.discover.twitch_ready},
            "youtube_cookies": yt_auth.status(self.s) | {
                "browsers": yt_auth.installed_browsers(),
                "needed_by": [rt.config.name for rt in self.sources.values() if rt.recorder.needs_signin],
            },
        }

    # ------------------------------------------- YouTube sign-in for reading streams
    def _after_youtube_signin(self) -> None:
        for rt in self.sources.values():
            if yt_auth.is_youtube(rt.recorder.url):
                rt.recorder.recheck_now()

    async def youtube_cookies_import(self, browser: str) -> dict[str, Any]:
        n = await yt_auth.import_from_browser(self.s, browser)
        ok = await asyncio.to_thread(self._verify_quiet)
        self._after_youtube_signin()
        log.info("YouTube sign-in imported from %s (%d cookies, verified=%s)", browser, n, ok)
        return {"cookies": n, "verified": ok}

    async def youtube_cookies_paste(self, text: str) -> dict[str, Any]:
        n = yt_auth.save_pasted(self.s, text)
        ok = await asyncio.to_thread(self._verify_quiet)
        self._after_youtube_signin()
        log.info("YouTube sign-in saved from pasted cookies.txt (%d cookies, verified=%s)", n, ok)
        return {"cookies": n, "verified": ok}

    async def youtube_cookies_verify(self) -> dict[str, Any]:
        ok = await asyncio.to_thread(self._verify_quiet)
        if ok:
            self._after_youtube_signin()
        return {"verified": ok}

    def youtube_cookies_remove(self) -> None:
        yt_auth.sign_out(self.s)

    def _verify_quiet(self) -> bool | None:
        try:
            return yt_auth.verify(self.s)
        except Exception as exc:  # network trouble: unknown, not "signed out"
            log.warning("couldn't check the YouTube sign-in: %s", exc)
            return None

    # ------------------------------------------------------- runtime settings
    def _read_runtime(self) -> dict[str, Any]:
        if not self._runtime_file.is_file():
            return {}
        try:
            data = json.loads(self._runtime_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError) as exc:
            log.error("cannot read %s: %s", self._runtime_file, exc)
            return {}

    def _write_runtime(self, **changes: Any) -> None:
        data = self._read_runtime()
        if self.publisher is not None:
            data["limits"] = {k: vars(v) for k, v in self.publisher.limits.items()
                              if self.accounts.get(k) is not None}
        data.update(changes)
        tmp = self._runtime_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._runtime_file)

    def _apply_limits(self) -> None:
        if self.publisher is None:
            return
        for key, lim in self._read_runtime().get("limits", {}).items():
            if key in self.publisher.limits:
                try:
                    self.publisher.limits[key] = PlatformLimits(**lim)
                except TypeError:
                    pass

    def set_platform_limits(self, platform: str, *, max_per_day: int, min_spacing_minutes: int,
                            enabled: bool) -> PlatformLimits:
        if self.publisher is None or platform not in self.publisher.limits:
            raise KeyError(platform)
        if not 1 <= max_per_day <= 20:
            raise ValueError("posts per day must be between 1 and 20")
        if not 10 <= min_spacing_minutes <= 1440:
            raise ValueError("spacing must be between 10 minutes and 24 hours")
        lim = PlatformLimits(max_per_day, min_spacing_minutes, enabled)
        self.publisher.limits[platform] = lim
        self._write_runtime()
        log.info("%s limits: %d/day, %d min apart, %s", platform, max_per_day, min_spacing_minutes,
                 "on" if enabled else "off")
        self._wake_publisher()
        return lim

    # ---------------------------------------------------------------- library
    def _apply_library_setting(self) -> None:
        saved = self._read_runtime().get("library_dir")
        if saved and not self.s.library_dir:
            self.s.library_dir = Path(saved)

    def _library_roots(self) -> list[Path]:
        roots = [self.s.library_root, self.s.default_library, self.s.rendered_dir]
        roots += [Path(p) for p in self._read_runtime().get("previous_libraries", [])]
        return list(dict.fromkeys(roots))

    def _ensure_library(self) -> bool:
        """Create the library folder if its drive is there (a missing folder on a present drive)."""
        root = self.s.library_root
        if root.is_dir():
            return os.access(root, os.W_OK)
        if lib.offline(root):
            return False
        try:
            root.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    def drives(self, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or time.time() - self._drives[0] > 15:
            self._drives = (time.time(), lib.drives())
        return self._drives[1]

    def storage(self) -> dict[str, Any]:
        root = self.s.library_root
        online = self.s.library_online()
        with self._db() as db:
            paths = [Path(r[0]) for r in db.execute("SELECT path FROM clips").fetchall()]
        outside = waiting_drive = 0
        for path in paths:
            if root in path.parents:
                if not online and lib.offline(path):
                    waiting_drive += 1
                continue
            if path.is_file():
                outside += 1
            elif lib.offline(path):
                waiting_drive += 1
        return {
            "root": str(root), "default": str(self.s.default_library.resolve()),
            "is_default": root == self.s.default_library, "online": online,
            "offline": lib.offline(root), "drive": str(lib.mount_root(root) or ""),
            "active": str(self.s.active_library.resolve()),
            "usage": lib.usage(root if online else self.s.default_library),
            "to_move": outside, "waiting_for_drive": waiting_drive,
            "move": self.move.to_dict(), "drives": self.drives(),
        }

    async def set_library(self, raw: str) -> dict[str, Any]:
        """Choose a new library folder and move every clip and source video there."""
        target = Path(os.path.expanduser(raw.strip())).absolute()
        if not raw.strip() or not target.is_absolute():
            raise ValueError("choose a folder")
        storage = self.s.storage_dir.resolve()
        if any(p in (target, *target.parents) for p in (storage / "raw_streams", storage / "secrets",
                                                         storage / "state", storage / "work")):
            raise ValueError("pick a folder outside Clipper's working folders")
        if lib.offline(target):
            raise ValueError(f"{lib.mount_root(target)} isn't connected")
        parent = next((p for p in (target, *target.parents) if p.exists()), None)
        if parent is None or not os.access(parent, os.W_OK):
            raise ValueError("that location isn't writable (is the drive read-only, e.g. NTFS on a Mac?)")
        if self.move.status == "moving":
            raise ValueError("a move is already running")
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".clipper-write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"can't write there: {exc.strerror or exc}") from exc
        need = await asyncio.to_thread(self._bytes_to_move, target)
        free = (lib.usage(target) or {}).get("free", 0)
        same_drive = lib.mount_root(target) == lib.mount_root(self.s.library_root)
        if not same_drive and need > free - 200 * 1024 * 1024:
            raise ValueError(f"not enough space there: the clips need {lib_size(need)} plus 200 MB to spare, "
                             f"{lib_size(free)} is free")
        old = self.s.library_root
        prev = [p for p in self._read_runtime().get("previous_libraries", []) if Path(p) != target]
        if old != target:
            prev.append(str(old))
        self.s.library_dir = None if target == self.s.default_library.absolute() else target
        self._write_runtime(library_dir=str(target), previous_libraries=prev[-10:])
        log.info("clip library location → %s (moving %.1f GB)", target, need / 1e9)
        asyncio.create_task(self.consolidate(explicit=True), name="library-move")
        return self.storage()

    def _bytes_to_move(self, target: Path) -> int:
        total = 0
        with self._db() as db:
            for (path,) in db.execute("SELECT path FROM clips").fetchall():
                video = Path(path)
                if target in video.parents or not video.is_file():
                    continue
                total += sum(f.stat().st_size for f in lib.sidecars(video) if f.is_file())
        for d in self._source_dirs():
            if d.is_dir() and d != target / lib.SOURCES:
                total += lib.folder_size(d)
        return total

    def _source_dirs(self) -> list[Path]:
        """Folders that may hold downloaded source videos (old locations included)."""
        dirs = [self.s.storage_dir / "vod"] + [r / lib.SOURCES for r in self._library_roots()]
        return list(dict.fromkeys(dirs))

    async def consolidate(self, explicit: bool = False) -> None:
        """Bring every clip and source video into the current library, one safe step at a time."""
        if not self._ensure_library():
            return
        async with self._move_lock:
            root = self.s.library_root
            with self._db() as db:
                rows = [dict(r) for r in db.execute(
                    "SELECT id, path, created_at, hook, display_name, source_id FROM clips").fetchall()]
            todo = [r for r in rows if root not in Path(r["path"]).parents and Path(r["path"]).is_file()]
            vod_busy = any(j.status not in ("done", "failed") for j in self.vod_jobs.values())
            fresh = time.time() - 600
            sources = [] if vod_busy else [  # never touch a download in progress
                f for d in self._source_dirs() if d.is_dir() and d != root / lib.SOURCES
                for f in d.iterdir() if f.is_file() and not f.name.startswith(".")
                and not f.name.endswith((".part", ".ytdl", ".temp")) and f.stat().st_mtime < fresh]
            if not todo and not sources:
                if explicit:
                    self.move = lib.MoveState(status="done", target=str(root), started=time.time(),
                                              finished=time.time())
                return
            st = self.move = lib.MoveState(status="moving", target=str(root), total=len(todo) + len(sources),
                                           started=time.time())
            log.info("library: moving %d clip(s) and %d source video(s) into %s", len(todo), len(sources), root)
            for f in sources:  # source videos first: clips reference them for re-cutting
                st.current = f.name
                dest = root / lib.SOURCES / f.name
                try:
                    size = f.stat().st_size
                    if dest.exists() and dest.stat().st_size == size:
                        await asyncio.to_thread(f.unlink)  # already there (earlier interrupted move)
                    else:
                        await asyncio.to_thread(lib.move_file, f, dest)
                    await asyncio.to_thread(self._repoint_media, f, dest)
                    st.bytes_done += size
                    st.done += 1
                except OSError as exc:
                    st.errors.append(f"{f.name}: {exc}")
                    log.error("library: couldn't move %s: %s", f, exc)
            for r in todo:
                clip_id, video = r["id"], Path(r["path"])
                busy = (self.publisher is not None and clip_id in self.publisher.active) or \
                    (self.rerenders.get(clip_id, {}).get("status") == "working")
                if busy:
                    st.skipped += 1
                    continue
                st.current = r["hook"] or video.name
                if self.publisher is not None:
                    self.publisher.locked.add(clip_id)
                try:
                    if lib.is_clip_folder(video) and any(p in video.parents for p in self._library_roots()):
                        rel = next(video.parent.relative_to(p / lib.CLIPS) for p in self._library_roots()
                                   if (p / lib.CLIPS) in video.parents)
                        dest_dir = lib.unique_dir((root / lib.CLIPS / rel).parent, rel.name)
                    else:  # loose files from before folders existed
                        dest_dir = lib.unique_dir(root / lib.CLIPS / lib.safe_name(r["display_name"] or r["source_id"]
                                                                                    or "Earlier clips"),
                                                  lib.clip_dir_name(r["created_at"], r["hook"] or video.stem))
                    size = sum(f.stat().st_size for f in lib.sidecars(video) if f.is_file())
                    new_video = await asyncio.to_thread(lib.move_clip_files, video, dest_dir)
                    with self._db() as db:
                        db.execute("UPDATE clips SET path=?, clip_key=CASE WHEN clip_key='' THEN ? ELSE clip_key END "
                                   "WHERE id=?", (str(new_video), video.stem, clip_id))
                    await asyncio.to_thread(self._write_notes, clip_id)
                    st.bytes_done += size
                    st.done += 1
                except (OSError, StopIteration, ValueError) as exc:
                    st.errors.append(f"{r['hook'] or video.name}: {exc}")
                    log.error("library: couldn't move clip %d: %s", clip_id, exc)
                finally:
                    if self.publisher is not None:
                        self.publisher.locked.discard(clip_id)
            for old in self._library_roots():
                if old != root:
                    for sub in (old / lib.CLIPS, old / lib.SOURCES):
                        await asyncio.to_thread(self._prune_empty, sub)
            st.status = "failed" if st.errors and not st.done else "done"
            st.current, st.finished = "", time.time()
            log.info("library: moved %d item(s) into %s%s", st.done, root,
                     f", {len(st.errors)} problem(s)" if st.errors else "")

    @staticmethod
    def _prune_empty(path: Path) -> None:
        if not path.is_dir():
            return
        for d in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            lib.remove_empty_dirs(d)
        lib.remove_empty_dirs(path)

    def _repoint_media(self, old: Path, new: Path) -> None:
        """Clips remember which source video they came from (for re-cuts): follow the move."""
        old_abs = Path(os.path.abspath(old))
        with self._db() as db:
            for r in db.execute("SELECT id, media FROM clips WHERE media LIKE ?", (f"%{old.name}%",)).fetchall():
                try:
                    media = json.loads(r["media"] or "{}")
                except ValueError:
                    continue
                if media.get("kind") == "file" and Path(os.path.abspath(media.get("path", ""))) == old_abs:
                    media["path"] = str(new)
                    db.execute("UPDATE clips SET media=? WHERE id=?", (json.dumps(media), r["id"]))

    def _write_notes(self, clip_id: int) -> None:
        """(Re)write post.txt / info.json for a clip, including where it has been posted."""
        with self._db() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
            if row is None:
                return
            posts = [{"account": self.accounts.describe(j["platform"]).get("label", j["platform"]),
                      "key": j["platform"], "status": j["status"], "posted_at": j["posted_at"],
                      "url": REMOTE_URLS[account_platform(j["platform"])].format(j["remote_id"]) if j["remote_id"] else None}
                     for j in db.execute("SELECT platform, status, posted_at, remote_id FROM jobs WHERE clip_id=?",
                                         (clip_id,)).fetchall()]
        lib.write_sidecars(Path(row["path"]), dict(row), posts)

    async def _library_loop(self) -> None:
        """Every minute: pull in clips made while the drive was away, and anything left behind."""
        await asyncio.sleep(5)
        was_online = None
        while True:
            try:
                online = self._ensure_library()
                if online != was_online and was_online is not None:
                    log.warning("clip library %s: %s", "back online" if online else "drive not connected",
                                self.s.library_root)
                was_online = online
                if online:
                    await self.consolidate()
            except Exception:
                log.exception("library check failed")
            await asyncio.sleep(60)

    # ------------------------------------------------------------ reconcile
    async def _reconcile_loop(self) -> None:
        """Clips deleted from disk quietly leave the queue/schedule (they are not failures)."""
        while True:
            try:
                with self._db() as db:
                    rows = db.execute("SELECT j.id, c.path FROM jobs j JOIN clips c ON c.id=j.clip_id "
                                      "WHERE j.status IN ('pending','scheduled','failed')").fetchall()
                    gone = [r["id"] for r in rows
                            if not Path(r["path"]).is_file() and not lib.offline(Path(r["path"]))]
                    if gone:
                        db.execute(f"UPDATE jobs SET status='expired', error='file removed', updated_at=? "
                                   f"WHERE id IN ({','.join('?' * len(gone))})", (time.time(), *gone))
                        log.info("%d post(s) dropped because their clip file was removed", len(gone))
            except Exception:
                log.exception("reconcile failed")
            await asyncio.sleep(60)

    def reveal_clip(self, clip_id: int) -> str:
        """Show the clip in Finder (macOS) / the file manager, for manual uploads."""
        import subprocess
        import sys

        with self._db() as db:
            row = db.execute("SELECT path FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row is None:
            raise KeyError(clip_id)
        path = Path(row["path"]).resolve()
        if not path.is_file():
            if lib.offline(path):
                raise FileNotFoundError(f"the drive {lib.mount_root(path)} isn't connected")
            raise FileNotFoundError("the clip file was removed")
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        elif os.name == "nt":
            subprocess.run(["explorer", f"/select,{path}"], check=False)
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=False)
        return str(path)

    def request_restart(self) -> None:
        """Stop cleanly and start again with the code now on disk (only when nothing is mid-way)."""
        if self.publisher is not None and self.publisher.active:
            raise ValueError("an upload is in progress; restart once it finishes (a minute or two)")
        if self.move.status == "moving":
            raise ValueError("clips are being moved between drives; restart when that finishes")
        if any(r.get("status") == "working" for r in self.rerenders.values()):
            raise ValueError("a clip is being re-rendered; restart when it's done")
        if self.stop_event is None:
            raise ValueError("restart isn't available in this mode")
        log.info("restarting to load the updated code…")
        self.restart_requested = True
        self.stop_event.set()

    def _on_posted(self, clip_id: int) -> None:
        asyncio.get_running_loop().run_in_executor(None, self._write_notes, clip_id)

    def _wake_publisher(self) -> None:
        if self.publisher is not None:
            self.publisher.wake.set()

    # ------------------------------------------------------------- lifecycle
    def _acquire_lock(self) -> None:
        """One daemon per storage dir: two publishers would race the cadence limits."""
        from clipper.modules.utils import try_lock

        self._lock_fh = open(self.s.state_dir / "daemon.lock", "a+")  # noqa: SIM115  (held for process life)
        if not try_lock(self._lock_fh):
            self._lock_fh.seek(0)
            other = self._lock_fh.read().strip() or "another process"
            raise SystemExit(f"Clipper is already running ({other}). Stop it first (Ctrl+C in its window), "
                             f"or use its dashboard.") from None
        self._lock_fh.seek(0)
        self._lock_fh.truncate()
        self._lock_fh.write(f"pid {os.getpid()}")
        self._lock_fh.flush()

    async def start(self, extra_urls: list[str] | None = None, publish_urls: bool = False,
                    url_category: str | None = None) -> None:
        self._acquire_lock()
        self._load_app_credentials()
        for cfg in self._load_sources():
            await self.add_source(cfg, persist=False)
        self._persist()
        self._core.append(self._spawn(self.pipeline.run(self.events), "pipeline"))
        self._core.append(self._spawn(self.pipeline.janitor(), "rendered-janitor"))
        self._core.append(self._spawn(self._reconcile_loop(), "reconcile"))
        self._core.append(self._spawn(self._library_loop(), "library"))
        if self.publish_enabled:
            self.publisher = Publisher(self.s, self.queue, build_uploaders(self.s, self.queue, self.accounts))
            self.publisher.on_posted = self._on_posted
            self._apply_limits()
            self._core.append(self._spawn(self.publisher.run(), "publisher"))
            if not self.publisher.uploaders:
                log.warning("no channel or Page connected: clips are saved but not posted (add one in Connections)")
            asyncio.create_task(self._resolve_accounts(), name="resolve-accounts")
        else:
            log.info("publishing disabled for this session (--no-publish)")
        if extra_urls:
            self.add_vods(extra_urls, clips=None, category=url_category, publish=publish_urls)
        log.info("clipper running: %d live source(s)", len(self.sources))

    async def stop(self) -> None:
        for slug in list(self.sources):
            await self._stop_source(slug)
        for t in self._core:
            t.cancel()
        await asyncio.gather(*self._core, return_exceptions=True)

    def crashed(self) -> asyncio.Task[Any] | None:
        """A core task that died unexpectedly (callers fail fast on this)."""
        return next((t for t in self._core if t.done() and not t.cancelled()), None)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        return asyncio.create_task(coro, name=name)

    # --------------------------------------------------------------- sources
    def _load_sources(self) -> list[SourceConfig]:
        if self._sources_file.is_file():
            try:
                raw = json.loads(self._sources_file.read_text(encoding="utf-8"))
                return [SourceConfig.model_validate(item) for item in raw]
            except (ValueError, OSError) as exc:
                log.error("cannot read %s (%s); falling back to .env sources", self._sources_file, exc)
        try:
            return self.s.all_sources()
        except ValueError:
            return []

    def _persist(self) -> None:
        data = [rt.config.model_dump(exclude_none=True) for rt in self.sources.values()]
        tmp = self._sources_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._sources_file)

    async def add_source(self, cfg: SourceConfig, persist: bool = True) -> SourceRuntime:
        if cfg.slug in self.sources:
            raise ValueError(f"{cfg.slug} is already being watched")
        rec = StreamRecorder(self.s, cfg)
        rt = SourceRuntime(cfg, rec)
        self.sources[cfg.slug] = rt
        self.recorders[cfg.slug] = rec
        if not cfg.paused:
            self._start_tasks(rt)
        log.info("source %s added → %s (signals: %s, publish: %s%s)", cfg.slug, cfg.source_url,
                 "+".join(cfg.signals), "yes" if cfg.permission_confirmed else "no", ", PAUSED" if cfg.paused else "")
        if persist:
            self._persist()
        return rt

    def _start_tasks(self, rt: SourceRuntime) -> None:
        cfg, rec = rt.config, rt.recorder
        rec.live = None
        rec.on_not_live = lambda r=rt: self._clip_not_live(r)
        rt.tasks.append(self._spawn(rec.run(), f"{cfg.slug}:recorder"))
        if "chat" in cfg.signals:
            def recording(r: StreamRecorder = rec) -> bool:
                return r.is_recording
            rt.tasks.append(self._spawn(ChatAnalyzer(self.s, self.events, recording, self.queue, cfg).run(),
                                        f"{cfg.slug}:chat"))
        if "audio" in cfg.signals:
            rt.tasks.append(self._spawn(AudioSpikeWatcher(self.s, cfg, rec, self.events).run(), f"{cfg.slug}:audio"))

    async def set_paused(self, slug: str, paused: bool) -> None:
        """Pause: stop recording/clipping this stream (finalising the current segment) but keep it.
        Resume: start watching again."""
        rt = self.sources[slug]
        if rt.config.paused == paused:
            return
        rt.config.paused = paused
        if paused:
            for t in rt.tasks:
                t.cancel()
            await asyncio.gather(*rt.tasks, return_exceptions=True)
            rt.tasks.clear()
        else:
            self._start_tasks(rt)
        self._persist()
        log.info("source %s %s", slug, "paused" if paused else "resumed")

    async def set_all_paused(self, paused: bool) -> None:
        for slug in list(self.sources):
            await self.set_paused(slug, paused)

    async def _stop_source(self, slug: str) -> None:
        rt = self.sources.pop(slug)
        self.recorders.pop(slug, None)
        for t in rt.tasks:
            t.cancel()
        await asyncio.gather(*rt.tasks, return_exceptions=True)  # recorder finalises its segment

    async def remove_source(self, slug: str) -> None:
        if slug not in self.sources:
            raise KeyError(slug)
        await self._stop_source(slug)
        self._persist()
        log.info("source %s removed", slug)

    async def update_source(self, slug: str, changes: dict[str, Any]) -> SourceConfig:
        """Edit a source while running. Style changes apply to the next clip; signal changes
        restart that source's watchers (a few seconds of recording gap)."""
        rt = self.sources[slug]
        data = rt.config.model_dump()
        for key in ("display_name", "category", "signals", "permission_confirmed"):
            if key in changes:
                data[key] = changes[key]
        if "style" in changes:
            data["style"] = ClipStyle.model_validate({**data["style"], **changes["style"]}).model_dump()
        new = SourceConfig.model_validate(data)
        if new.slug != slug:
            raise ValueError("the stream link itself can't be changed; remove and add it instead")
        if set(new.signals) != set(rt.config.signals):
            await self._stop_source(slug)
            await self.add_source(new, persist=False)
        else:
            rt.config = new
            rt.recorder.source = new
        self._persist()
        log.info("source %s updated", slug)
        return new

    def set_source_publish(self, slug: str, publish: bool) -> None:
        rt = self.sources[slug]
        rt.config.permission_confirmed = publish
        self._persist()
        log.info("source %s publishing %s", slug, "enabled" if publish else "disabled")

    # ----------------------------------------------------- ended streams / VODs
    _VIDEO_URL = re.compile(r"(youtube\.com/(watch\?|live/|shorts/)|youtu\.be/|twitch\.tv/videos/)")

    def _ended_file(self) -> Path:
        return self.s.state_dir / "ended_clipped.json"

    def _ended(self) -> dict[str, Any]:
        try:
            return json.loads(self._ended_file().read_text())
        except (OSError, ValueError):
            return {}

    def _clip_not_live(self, rt: SourceRuntime) -> None:
        """A watched link answered "not live": if it is a single video (an ended live stream or a normal
        upload), clip it like a pasted video link, once. Channels (twitch.tv/name) keep waiting."""
        url = rt.config.source_url
        if not self.s.clip_ended_streams or not self._VIDEO_URL.search(url):
            return
        state = self._ended().get(url) or {}
        if state.get("done") or any(j.url == url and j.status not in ("done", "failed") for j in self.vod_jobs.values()):
            return
        if time.time() < float(state.get("retry_at") or 0):
            return
        style = rt.config.style
        log.info("[%s] not live: clipping the video itself (%s)", rt.config.slug, url)
        job = VodJob(id=uuid.uuid4().hex[:8], url=url, clips_requested=self.s.vod_clips_per_video,
                     publish=rt.config.permission_confirmed, category=rt.config.category, style=style.model_dump())
        self.vod_jobs[job.id] = job

        async def run() -> None:
            await self._run_vod(job)
            data = self._ended()
            prev = data.get(url) or {}
            if job.status == "done":
                data[url] = {"done": True, "at": time.time(), "clips": len(job.clips)}
            else:  # e.g. YouTube still processing the ended stream: try again later (10, 20, 40… min)
                tries = int(prev.get("tries") or 0) + 1
                data[url] = {"tries": tries, "retry_at": time.time() + min(6 * 3600, 600 * 2 ** (tries - 1)),
                             "error": job.error}
            try:
                self._ended_file().write_text(json.dumps(data, indent=1))
            except OSError as exc:
                log.warning("could not save %s: %s", self._ended_file().name, exc)

        asyncio.create_task(run(), name=f"vod:{job.id}")

    # ------------------------------------------------------------------ VODs
    def add_vods(self, urls: list[str], *, clips: int | None, category: str | None, publish: bool,
                 style: ClipStyle | None = None) -> list[VodJob]:
        jobs = []
        style = style or ClipStyle()
        for url in urls:
            job = VodJob(id=uuid.uuid4().hex[:8], url=url, clips_requested=clips or self.s.vod_clips_per_video,
                         publish=publish, category=category, style=style.model_dump())
            self.vod_jobs[job.id] = job
            asyncio.create_task(self._run_vod(job), name=f"vod:{job.id}")
            jobs.append(job)
        return jobs

    async def _run_vod(self, job: VodJob) -> None:
        def on_stage(stage: str, info: dict[str, Any]) -> None:
            job.status = stage
            job.title = info.get("title", job.title)
            job.uploader = info.get("uploader", job.uploader)
            job.duration = info.get("duration", job.duration)
            job.total = info.get("total", job.total)
            job.done = info.get("done", job.done)
            job.skipped = info.get("skipped", job.skipped)

        async with self._vod_sem:
            try:
                paths = await self.pipeline.process_vod(job.url, clips_per_video=job.clips_requested,
                                                        publishable=job.publish, category=job.category,
                                                        style=ClipStyle.model_validate(job.style),
                                                        on_stage=on_stage)
                job.clips = [p.parent.name if p.name == lib.VIDEO else p.name for p in paths]
                job.status = "done"
                if not paths:
                    job.error = ("these moments were already clipped earlier" if job.skipped
                                 else "no highlight moments found")
            except Exception as exc:
                job.status, job.error = "failed", str(exc)[:300]
                log.error("video job %s failed: %s", job.url, exc)
            finally:
                job.finished = time.time()

    # ------------------------------------------------------------ clip actions
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.s.state_dir / "publisher.db", isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def cancel_clip(self, clip_id: int) -> int:
        with self._db() as db:
            return db.execute("UPDATE jobs SET status='expired', error='cancelled from dashboard', updated_at=? "
                              "WHERE clip_id=? AND status='pending'", (time.time(), clip_id)).rowcount

    def retry_clip(self, clip_id: int) -> int:
        with self._db() as db:
            return db.execute("UPDATE jobs SET status='pending', attempts=0, next_attempt_at=0, error=NULL, "
                              "updated_at=? WHERE clip_id=? AND status IN ('failed','expired')",
                              (time.time(), clip_id)).rowcount

    def publish_clip(self, clip_id: int) -> int:
        """Queue a local-only clip for every platform (operator asserted the rights in the UI)."""
        with self._db() as db:
            if db.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone() is None:
                raise KeyError(clip_id)
            n = 0
            for platform in self.accounts.defaults():
                n += db.execute("INSERT OR IGNORE INTO jobs(clip_id, platform, updated_at) VALUES (?,?,?)",
                                (clip_id, platform, time.time())).rowcount
            return n

    def _check_schedule(self, clip_id: int, platform: str, at: float, confirm_rights: bool) -> None:
        with self._db() as db:
            clip = db.execute("SELECT source_id, duration, path FROM clips WHERE id=?", (clip_id,)).fetchone()
            if clip is None:
                raise KeyError(clip_id)
            if not Path(clip["path"]).is_file():
                if lib.offline(Path(clip["path"])):
                    raise ValueError("connect the drive this clip is on first")
                raise ValueError("the clip's video file was deleted")
            acct = self.accounts.get(platform)
            if acct is None:
                raise ValueError("that channel/Page isn't connected any more")
            if self.publisher is not None and self.publisher.uploader(platform) is None:
                raise ValueError(f"{acct.label} isn't connected; reconnect it under Connections")
            if acct.platform == "facebook" and (clip["duration"] or 0) > FACEBOOK_MAX_SECONDS + 0.5:
                raise ValueError(f"Facebook Reels must be {FACEBOOK_MAX_SECONDS:.0f}s or shorter; "
                                 f"this clip is {clip['duration']:.0f}s. Re-cut it first.")
            src = self.sources.get(clip["source_id"] or "")
            if not confirm_rights and not (src and src.config.permission_confirmed):
                raise PermissionError("confirm you own this content or have permission to repost it")
            # hard daily cap around the requested time (posted + other scheduled in a 24 h window)
            n = db.execute(
                "SELECT count(*) FROM jobs WHERE platform=? AND clip_id<>? AND ("
                "(status='posted' AND posted_at BETWEEN ? AND ?) OR "
                "(status='scheduled' AND scheduled_at BETWEEN ? AND ?))",
                (platform, clip_id, at - 86400, at + 86400, at - 86400, at + 86400)).fetchone()[0]
            cap = self.publisher.limit(platform).max_per_day if self.publisher else self.s.max_posts_per_day
            if n >= cap * 2:  # ±24 h window ≈ two daily windows
                raise ValueError(f"{acct.label}: that time is too crowded (daily limit {cap}); pick another day")

    async def schedule_clip(self, clip_id: int, platforms: list[str], at: float | None,
                            confirm_rights: bool = False) -> list[str]:
        """Post at ``at`` (unix time), or as soon as possible when ``at`` is None ("Post now")."""
        when = max(time.time(), at or 0)
        for platform in platforms:
            self._check_schedule(clip_id, platform, when, confirm_rights)
        for platform in platforms:
            await self.queue.schedule(clip_id, platform, when)
        self._wake_publisher()
        log.info("clip %d %s on %s", clip_id, "posting now" if at is None else
                 f"scheduled for {time.strftime('%Y-%m-%d %H:%M', time.localtime(when))}", ", ".join(platforms))
        return platforms

    async def unschedule_clip(self, clip_id: int, platform: str) -> int:
        return await self.queue.unschedule(clip_id, platform)

    def rerender_clip(self, clip_id: int, opts: RenderOptions, shift: float, length: float | None) -> None:
        cur = self.rerenders.get(clip_id)
        if cur and cur["status"] == "working":
            raise ValueError("this clip is already being re-rendered")
        with self._db() as db:
            busy = db.execute("SELECT 1 FROM jobs WHERE clip_id=? AND status='uploading'", (clip_id,)).fetchone()
        if busy:
            raise ValueError("this clip is uploading right now; try again in a minute")
        state = {"status": "working", "error": "", "started": time.time()}
        self.rerenders[clip_id] = state

        async def run() -> None:
            try:
                await self.pipeline.rerender(clip_id, opts, shift=shift, length=length)
                state["status"] = "done"
            except Exception as exc:
                state["status"], state["error"] = "failed", str(exc)[:300]
                log.error("re-render of clip %d failed: %s", clip_id, exc)
            state["finished"] = time.time()

        asyncio.create_task(run(), name=f"rerender:{clip_id}")

    async def facecam_scan(self, *, slug: str | None = None, clip_id: int | None = None, url: str | None = None,
                           detect: bool = True) -> dict[str, Any]:
        """Frames for the webcam picker plus an auto-detected webcam box (None when unsure)."""
        import shutil

        from clipper.modules import facecam

        for old in self.s.rendered_dir.glob("facecam_*.jpg"):
            if old.stat().st_mtime < time.time() - 900:
                old.unlink(missing_ok=True)
        tag = uuid.uuid4().hex[:10]
        tmp = self.s.work_dir / f"facecam_{tag}"
        try:
            if url:
                if not url.startswith(("http://", "https://")):
                    raise ValueError("paste a full video or stream link")
                paths = await facecam.frames_from_url(self.s, url, tmp)
            else:
                rt = self.sources[slug] if slug else None
                try:
                    paths = await self.pipeline.sample_frames(tmp, clip_id=clip_id, recorder=rt.recorder if rt else None)
                except ValueError:
                    if rt is None:
                        raise
                    # nothing recorded yet: look at the live stream itself
                    try:
                        paths = await facecam.frames_from_url(self.s, rt.config.source_url, tmp)
                    except Exception as exc:
                        log.info("facecam: live frames unavailable for %s: %s", slug, exc)
                        raise ValueError("this stream isn't live right now, so there's nothing to look at yet. "
                                         "Pick a corner, or try again when it's live.") from exc
            if not paths:
                raise ValueError("couldn't read any frames from that source")
            guess = await asyncio.to_thread(facecam.detect_files, paths) if detect else None
            urls = []
            for i, p in enumerate(paths):
                dest = self.s.rendered_dir / f"facecam_{tag}_{i:02d}.jpg"
                shutil.move(str(p), dest)
                urls.append(f"/media/{dest.name}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if guess:
            log.info("facecam auto-detect: x=%.2f y=%.2f w=%.2f h=%.2f (%s, %.0f%%)", guess.x, guess.y, guess.w,
                     guess.h, guess.method, guess.confidence * 100)
        return {"frames": urls, "guess": guess.to_dict() if guess else None}

    async def preview(self, opts: RenderOptions, *, slug: str | None = None, clip_id: int | None = None) -> str:
        """Render one composed frame and return its /media URL."""
        for old in self.s.rendered_dir.glob("preview_*.jpg"):  # keep only the latest few minutes
            if old.stat().st_mtime < time.time() - 600:
                old.unlink(missing_ok=True)
        out = self.s.rendered_dir / f"preview_{uuid.uuid4().hex[:10]}.jpg"
        recorder = self.sources[slug].recorder if slug else None
        await self.pipeline.preview(opts, out, clip_id=clip_id, recorder=recorder)
        return f"/media/{out.name}"

    def delete_clip(self, clip_id: int) -> None:
        """Delete the local files and stop any pending posts (published posts are untouched)."""
        with self._db() as db:
            row = db.execute("SELECT path FROM clips WHERE id=?", (clip_id,)).fetchone()
            if row is None:
                raise KeyError(clip_id)
            db.execute("UPDATE jobs SET status='expired', error='file removed' "
                       "WHERE clip_id=? AND status IN ('pending','scheduled','failed')", (clip_id,))
        lib.delete_clip_files(Path(row["path"]), self._library_roots())

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        sources = []
        for slug, rt in self.sources.items():
            segs = [p for ext in (".ts", ".mkv") for p in rt.recorder.raw_dir.glob(f"*/*{ext}")]
            last = max((f.stat().st_mtime for f in segs), default=None)
            if rt.config.paused:
                state = "paused"
            elif rt.recorder.is_recording:
                state = "recording"
            elif rt.recorder.live is None:
                state = "checking"
            elif rt.recorder.check_error:
                state = "blocked"  # we don't know: the check itself failed (not "not live")
            else:
                state = "offline"
            sources.append({
                "slug": slug, "platform": rt.config.platform, "url": rt.config.source_url,
                "name": rt.config.name, "category": rt.config.category, "signals": rt.config.signals,
                "publish": rt.config.permission_confirmed, "state": state,
                "style": rt.config.style.model_dump(),
                "last_data": last, "segments": len(segs), "last_check": rt.recorder.last_check or None,
                "check_error": rt.recorder.check_error, "retry_at": rt.recorder.retry_at or None,
                "added_at": rt.added_at,
            })

        clips: list[dict[str, Any]] = []
        platforms: list[dict[str, Any]] = []
        quota: dict[str, Any] = {}
        with self._db() as db:
            rows = db.execute(
                "SELECT id, path, created_at, score, duration, transcript, hook, source_id, source_url, "
                "display_name, render_opts, media FROM clips ORDER BY created_at DESC LIMIT 120").fetchall()
            jobs: dict[int, list[dict[str, Any]]] = {}
            if rows:
                ids = [r["id"] for r in rows]
                for j in db.execute(f"SELECT clip_id, platform, status, remote_id, error, posted_at, attempts, "
                                    f"next_attempt_at, scheduled_at FROM jobs "
                                    f"WHERE clip_id IN ({','.join('?' * len(ids))})", ids).fetchall():
                    jobs.setdefault(j["clip_id"], []).append({
                        "platform": j["platform"], "status": j["status"], "remote_id": j["remote_id"],
                        "error": j["error"], "posted_at": j["posted_at"], "attempts": j["attempts"],
                        "retry_at": j["next_attempt_at"] if j["status"] in ("pending", "scheduled") else None,
                        "scheduled_at": j["scheduled_at"] if j["status"] == "scheduled" else None,
                        "url": REMOTE_URLS[account_platform(j["platform"])].format(j["remote_id"]) if j["remote_id"] else None,
                    })
            for r in rows:
                path = Path(r["path"])
                exists = path.is_file()
                thumb = lib.thumb_of(path)
                media = json.loads(r["media"] or "{}")
                try:
                    opts = json.loads(r["render_opts"]) if r["render_opts"] else None
                except ValueError:
                    opts = None
                clips.append({
                    "render_opts": opts, "can_recut": bool(media),
                    "recut_kind": media.get("kind"), "rerender": self.rerenders.get(r["id"]),
                    "version": int(path.stat().st_mtime) if exists else 0,
                    "id": r["id"], "created": r["created_at"], "score": r["score"], "duration": r["duration"],
                    "hook": r["hook"], "transcript": r["transcript"], "source": r["source_id"],
                    "source_name": r["display_name"], "source_url": r["source_url"],
                    "video": f"/clip/{r['id']}/video" if exists else None,
                    "abs_path": str(path.resolve()) if exists else None,
                    "folder": str(path.parent.resolve()) if exists and lib.is_clip_folder(path) else None,
                    "offline": (not exists) and lib.offline(path),
                    "thumb": f"/clip/{r['id']}/thumb" if exists and thumb.is_file() else None,
                    "jobs": sorted(jobs.get(r["id"], []), key=lambda j: j["platform"]),
                })
            for acct in self.accounts.all():
                platform = acct.key
                counts = dict(db.execute("SELECT status, count(*) FROM jobs WHERE platform=? GROUP BY status",
                                         (platform,)).fetchall())
                posted_24h = db.execute("SELECT count(*) FROM jobs WHERE platform=? AND status='posted' "
                                        "AND posted_at>=?", (platform, now - 86400)).fetchone()[0]
                last = db.execute("SELECT max(posted_at) FROM jobs WHERE platform=? AND status='posted'",
                                  (platform,)).fetchone()[0]
                state = db.execute("SELECT next_eligible_at FROM platform_state WHERE platform=?",
                                   (platform,)).fetchone()
                lim = self.publisher.limit(platform) if self.publisher else PlatformLimits(
                    self.s.max_posts_per_day, self.s.min_post_spacing_minutes)
                next_slot = max(filter(None, [state[0] if state else None,
                                              (last + lim.min_spacing_minutes * 60) if last else None]),
                                default=None)
                platforms.append({
                    "name": platform, "key": platform, "platform": acct.platform, "label": acct.label,
                    "account": acct.name, "default": acct.default,
                    "active": bool(self.publisher and self.publisher.uploader(platform)),
                    "pending": counts.get("pending", 0), "failed": counts.get("failed", 0),
                    "posted_24h": posted_24h, "max_per_day": lim.max_per_day, "last_post": last,
                    "spacing_minutes": lim.min_spacing_minutes, "enabled": lim.enabled,
                    "next_slot": next_slot if next_slot and next_slot > now else None,
                    "paused_reason": (self.publisher.paused.get(platform) if self.publisher else None),
                })
            upcoming = [dict(r) for r in db.execute(
                "SELECT j.clip_id, j.platform, j.scheduled_at, c.hook FROM jobs j JOIN clips c ON c.id=j.clip_id "
                "WHERE j.status='scheduled' ORDER BY j.scheduled_at LIMIT 30").fetchall()]
            day = pacific_day()
            used = dict(db.execute("SELECT platform, units FROM quota_usage WHERE day=?", (day,)).fetchall())
            quota = {"day": day, "youtube": used.get("youtube", 0), "limit": self.s.youtube_daily_quota,
                     "chat": used.get("youtube_chat", 0), "chat_budget": self.s.youtube_chat_daily_budget}

        return {
            "now": now, "started_at": self.started_at, "antibot": pot_helper_up(),
            "build": {"id": self.build, "update_available": code_signature() != self.build},
            "publishing": {"enabled": bool(self.publisher and self.publisher.uploaders),
                           "session_disabled": not self.publish_enabled,
                           "hold": bool(self.publisher and self.publisher.hold),
                           "platforms": platforms, "quota": quota, "scheduled": upcoming,
                           "max_per_day": self.s.max_posts_per_day,
                           "spacing_minutes": self.s.min_post_spacing_minutes},
            "render": {"queued_events": self.events.qsize(), "workers": self.s.render_workers},
            "limits": {"facebook_max_seconds": FACEBOOK_MAX_SECONDS, "max_clip_seconds": 180,
                       "publish_platforms": self.accounts.defaults()},
            "accounts": [self.accounts.describe(a.key) for a in self.accounts.all()],
            "storage": self.storage(),
            "sources": sources,
            "connections": self.connections(),
            "vod_jobs": sorted((vars(j) | {} for j in self.vod_jobs.values()), key=lambda j: -j["started"])[:20],
            "clips": clips,
        }


async def run_until_signal(daemon: Daemon, extra: Callable[[], Coroutine[Any, Any, Any]] | None = None) -> int:
    """Run ``daemon`` (plus an optional extra coroutine such as the web server) until Ctrl+C/SIGTERM."""
    import signal

    stop = asyncio.Event()
    daemon.stop_event = stop
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    extra_task = asyncio.create_task(extra()) if extra else None
    exit_code = 0
    try:
        while not stop.is_set():
            if (crashed := daemon.crashed()) is not None:
                log.error("component %s stopped unexpectedly", crashed.get_name(),
                          exc_info=crashed.exception() if not crashed.cancelled() else None)
                exit_code = 1
                break
            if extra_task is not None and extra_task.done():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)
    finally:
        log.info("shutting down…")
        if extra_task is not None:
            extra_task.cancel()
            await asyncio.gather(extra_task, return_exceptions=True)
        await daemon.stop()
    return RESTART_EXIT_CODE if daemon.restart_requested else exit_code
