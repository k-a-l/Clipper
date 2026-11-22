"""Local web dashboard (FastAPI) for the :class:`~modules.daemon.Daemon`.

Security model (single local operator):
* binds to 127.0.0.1 only by default;
* every request must carry a localhost ``Host`` header (blocks DNS-rebinding attacks);
* every state-changing request must carry ``X-Clipper-Token`` (a random per-session token
  embedded in the page). Browsers cannot add custom headers to cross-site requests without
  a CORS preflight, and no CORS is enabled, so other websites cannot drive the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from clipper.config import ACCOUNT_KEY, FACECAM_PRESETS, ClipStyle, RenderOptions, Signal, SourceConfig
from clipper.modules.daemon import Daemon

STATIC = Path(__file__).parent / "static"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.(mp4|jpg)$")
_TWITCH_NAME = re.compile(r"^[A-Za-z0-9_]{3,25}$")


# ------------------------------------------------------------------ request models
class AddSource(BaseModel):
    input: str = Field(..., description="Twitch channel/URL, YouTube live URL or @handle")
    display_name: str | None = None
    category: str | None = None
    signals: list[Signal] = Field(default_factory=lambda: list[Signal](["chat", "audio"]))
    publish: bool = False
    style: ClipStyle | None = None


class UpdateSource(BaseModel):
    display_name: str | None = None
    category: str | None = None
    signals: list[Signal] | None = None
    style: dict[str, Any] | None = None


AccountKey = Annotated[str, StringConstraints(pattern=ACCOUNT_KEY)]


class Schedule(BaseModel):
    platforms: list[AccountKey] = Field(min_length=1)
    at: float | None = None  # unix seconds; None = post now
    confirm_rights: bool = False


class Unschedule(BaseModel):
    platform: AccountKey


class Rerender(BaseModel):
    opts: RenderOptions
    shift: float = Field(default=0.0, ge=-120, le=120)
    length: float | None = Field(default=None, ge=10, le=180)


class Preview(BaseModel):
    opts: RenderOptions
    slug: str | None = None
    clip_id: int | None = None


class FacecamScan(BaseModel):
    slug: str | None = None
    clip_id: int | None = None
    url: str | None = Field(default=None, max_length=500)
    detect: bool = True


class Pause(BaseModel):
    paused: bool


class Limits(BaseModel):
    max_per_day: int = Field(ge=1, le=20)
    min_spacing_minutes: int = Field(ge=10, le=1440)
    enabled: bool = True


class FindPages(BaseModel):
    user_token: str = Field(min_length=20, max_length=1000)
    app_secret: str | None = Field(default=None, min_length=16, max_length=64)


class AddPages(BaseModel):
    session: str = Field(min_length=10, max_length=64)
    page_ids: list[Annotated[str, StringConstraints(pattern=r"^[0-9]{5,30}$")]] = Field(min_length=1, max_length=50)


class AccountDefault(BaseModel):
    default: bool


class Library(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


class ConnectTwitch(BaseModel):
    client_id: str = Field(min_length=10, max_length=64)
    client_secret: str = Field(min_length=10, max_length=64)


class YtCookiesImport(BaseModel):
    browser: Literal["chrome", "safari", "brave", "edge", "firefox", "chromium", "vivaldi", "opera"]


class YtCookiesPaste(BaseModel):
    text: Annotated[str, StringConstraints(min_length=10, max_length=500_000)]


class TogglePublish(BaseModel):
    publish: bool


class AddVods(BaseModel):
    urls: list[str]
    clips: int | None = Field(default=None, ge=1, le=50)
    category: str | None = None
    publish: bool = False
    style: ClipStyle | None = None


class Hold(BaseModel):
    hold: bool


def parse_source(req: AddSource) -> SourceConfig:
    """Turn whatever the operator pasted into a SourceConfig."""
    raw = req.input.strip()
    platform: Literal["twitch", "youtube"]
    channel = url = None
    if m := re.match(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?", raw):
        platform, channel = "twitch", m.group(1)
    elif re.match(r"^(?:https?://)?(?:www\.|m\.)?(youtube\.com|youtu\.be)/", raw):
        platform, url = "youtube", raw if raw.startswith("http") else f"https://{raw}"
    elif raw.startswith("@") and re.match(r"^@[\w.-]{3,}$", raw):
        platform, url = "youtube", f"https://www.youtube.com/{raw}/live"
    elif _TWITCH_NAME.match(raw):
        platform, channel = "twitch", raw
    else:
        raise ValueError("Paste a Twitch channel or link, a YouTube live link, or a YouTube @handle.")
    signals = [s for s in req.signals if s in ("chat", "audio")] or ["audio"]
    return SourceConfig(platform=platform, channel=channel, url=url,
                        display_name=(req.display_name or None), category=(req.category or None),
                        signals=signals, permission_confirmed=req.publish, style=req.style or ClipStyle())


# ------------------------------------------------------------------ app
def create_app(daemon: Daemon, port: int, token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}
    app = FastAPI(title="Clipper", docs_url=None, redoc_url=None, openapi_url=None)
    rendered = daemon.s.rendered_dir.resolve()

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
                request.headers.get("x-clipper-token", ""), token):
            return JSONResponse({"error": "missing or invalid session token; reload the page"}, status_code=403)
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def err(status: int, msg: str) -> HTTPException:
        return HTTPException(status_code=status, detail=msg)

    # The page is read once, so it always matches the code running behind it (after an update
    # the dashboard shows "Restart" instead of a new page talking to an old server).
    page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__CLIPPER_TOKEN__", token)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return page

    @app.post("/api/restart")
    async def restart() -> dict[str, Any]:
        try:
            daemon.request_restart()
        except ValueError as exc:
            raise err(409, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(daemon.snapshot)

    # ---- sources
    @app.post("/api/sources")
    async def add_source(body: AddSource) -> dict[str, Any]:
        try:
            cfg = parse_source(body)
            await daemon.add_source(cfg)
        except (ValueError, ValidationError) as exc:
            raise err(400, str(exc).splitlines()[0]) from exc
        return {"ok": True, "slug": cfg.slug}

    @app.delete("/api/sources/{slug}")
    async def remove_source(slug: str) -> dict[str, Any]:
        try:
            await daemon.remove_source(slug)
        except KeyError as exc:
            raise err(404, f"unknown source {slug}") from exc
        return {"ok": True}

    @app.patch("/api/sources/{slug}")
    async def update_source(slug: str, body: UpdateSource) -> dict[str, Any]:
        try:
            cfg = await daemon.update_source(slug, body.model_dump(exclude_none=True))
        except KeyError as exc:
            raise err(404, f"unknown source {slug}") from exc
        except (ValueError, ValidationError) as exc:
            raise err(400, str(exc).splitlines()[0]) from exc
        return {"ok": True, "source": cfg.model_dump()}

    @app.get("/api/options")
    async def options() -> dict[str, Any]:
        return {"facecam_presets": {k: dict(zip("xywh", v)) for k, v in FACECAM_PRESETS.items()},
                "layouts": ["center", "full", "fit", "cam_top", "cam_bottom"],
                "defaults": RenderOptions().model_dump(), "style_defaults": ClipStyle().model_dump()}

    @app.post("/api/preview")
    async def preview(body: Preview) -> dict[str, Any]:
        try:
            url = await daemon.preview(body.opts, slug=body.slug, clip_id=body.clip_id)
        except KeyError as exc:
            raise err(404, "unknown stream or clip") from exc
        except (ValueError, FileNotFoundError) as exc:
            raise err(400, str(exc)) from exc
        except Exception as exc:
            raise err(500, f"preview failed: {str(exc).splitlines()[0][:200]}") from exc
        return {"ok": True, "url": url}

    @app.post("/api/facecam")
    async def facecam_scan(body: FacecamScan) -> dict[str, Any]:
        """Frames for the webcam picker + auto-detected webcam box."""
        if not (body.slug or body.clip_id is not None or body.url):
            raise err(400, "pick a stream, clip or link")
        try:
            res = await daemon.facecam_scan(slug=body.slug, clip_id=body.clip_id, url=body.url, detect=body.detect)
        except KeyError as exc:
            raise err(404, "unknown stream or clip") from exc
        except (ValueError, FileNotFoundError) as exc:
            raise err(400, str(exc)) from exc
        except Exception as exc:
            raise err(500, f"couldn't read frames: {str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__}") from exc
        return {"ok": True, **res}

    @app.post("/api/clips/{clip_id}/schedule")
    async def schedule(clip_id: int, body: Schedule) -> dict[str, Any]:
        try:
            done = await daemon.schedule_clip(clip_id, list(body.platforms), body.at, body.confirm_rights)
        except KeyError as exc:
            raise err(404, f"unknown clip {clip_id}") from exc
        except PermissionError as exc:
            raise err(403, str(exc)) from exc
        except ValueError as exc:
            raise err(400, str(exc)) from exc
        return {"ok": True, "platforms": done}

    @app.post("/api/clips/{clip_id}/unschedule")
    async def unschedule(clip_id: int, body: Unschedule) -> dict[str, Any]:
        n = await daemon.unschedule_clip(clip_id, body.platform)
        return {"ok": True, "changed": n}

    @app.post("/api/clips/{clip_id}/rerender")
    async def rerender(clip_id: int, body: Rerender) -> dict[str, Any]:
        try:
            daemon.rerender_clip(clip_id, body.opts, body.shift, body.length)
        except ValueError as exc:
            raise err(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sources/pause-all")
    async def pause_all(body: Pause) -> dict[str, Any]:
        await daemon.set_all_paused(body.paused)
        return {"ok": True}

    @app.post("/api/sources/{slug}/pause")
    async def pause_source(slug: str, body: Pause) -> dict[str, Any]:
        try:
            await daemon.set_paused(slug, body.paused)
        except KeyError as exc:
            raise err(404, f"unknown source {slug}") from exc
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/reveal")
    async def reveal(clip_id: int) -> dict[str, Any]:
        try:
            path = await asyncio.to_thread(daemon.reveal_clip, clip_id)
        except KeyError as exc:
            raise err(404, f"unknown clip {clip_id}") from exc
        except FileNotFoundError as exc:
            raise err(410, str(exc)) from exc
        return {"ok": True, "path": path}

    @app.post("/api/platforms/{platform}")
    async def platform_limits(platform: str, body: Limits) -> dict[str, Any]:
        try:
            lim = daemon.set_platform_limits(platform, max_per_day=body.max_per_day,
                                             min_spacing_minutes=body.min_spacing_minutes, enabled=body.enabled)
        except KeyError as exc:
            raise err(409, f"{platform} isn't connected in this session") from exc
        except ValueError as exc:
            raise err(400, str(exc)) from exc
        return {"ok": True, "limits": vars(lim)}

    @app.get("/api/connections")
    async def connections() -> dict[str, Any]:
        return daemon.connections()

    # ---- accounts (YouTube channels, Facebook Pages)
    @app.post("/api/accounts/facebook/pages")
    async def facebook_pages(body: FindPages) -> dict[str, Any]:
        try:
            return {"ok": True, **await daemon.facebook_find_pages(body.user_token, body.app_secret)}
        except Exception as exc:  # surface the platform's message (token never echoed)
            raise err(400, str(exc).replace(body.user_token, "<token>")[:300]) from exc

    @app.post("/api/accounts/facebook/add")
    async def facebook_add(body: AddPages) -> dict[str, Any]:
        try:
            added = await daemon.facebook_add_pages(body.session, list(body.page_ids))
        except ValueError as exc:
            raise err(400, str(exc)) from exc
        return {"ok": True, "added": added}

    @app.post("/api/accounts/youtube/add")
    async def youtube_add() -> dict[str, Any]:
        async def run() -> None:
            try:
                await daemon.add_youtube_channel()
            except Exception as exc:
                logging.getLogger(__name__).error("YouTube sign-in failed: %s", exc)
        if daemon.connect_state.get("youtube") == "waiting":
            raise err(409, "a YouTube sign-in is already waiting in your browser")
        daemon.connect_state["youtube"] = "starting"
        asyncio.create_task(run())
        return {"ok": True, "message": "A Google sign-in page is opening in your browser. Pick the channel there."}

    @app.post("/api/accounts/{key}/default")
    async def account_default(key: str, body: AccountDefault) -> dict[str, Any]:
        try:
            return {"ok": True, "account": daemon.set_account_default(key, body.default)}
        except KeyError as exc:
            raise err(404, "unknown account") from exc

    @app.delete("/api/accounts/{key}")
    async def account_remove(key: str) -> dict[str, Any]:
        try:
            return {"ok": True, **daemon.remove_account(key)}
        except KeyError as exc:
            raise err(404, "unknown account") from exc

    # ---- storage
    @app.get("/api/storage")
    async def storage(refresh: bool = False) -> dict[str, Any]:
        if refresh:
            await asyncio.to_thread(daemon.drives, True)
        return await asyncio.to_thread(daemon.storage)

    @app.post("/api/storage")
    async def set_storage(body: Library) -> dict[str, Any]:
        try:
            return {"ok": True, **await daemon.set_library(body.path)}
        except ValueError as exc:
            raise err(400, str(exc)) from exc

    @app.post("/api/storage/open")
    async def open_storage() -> dict[str, Any]:
        import subprocess
        import sys

        root = daemon.s.active_library
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(root)], check=False)
        return {"ok": True, "path": str(root)}

    # ---- YouTube sign-in for reading streams ("Sign in to confirm you're not a bot")
    @app.post("/api/youtube-signin/open")
    async def yt_signin_open() -> dict[str, Any]:
        import subprocess
        import sys

        from clipper.modules.yt_auth import SIGNIN_URL
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", SIGNIN_URL], check=False)
        return {"ok": True, "url": SIGNIN_URL}

    @app.post("/api/youtube-signin/import")
    async def yt_signin_import(body: YtCookiesImport) -> dict[str, Any]:
        try:
            return {"ok": True, **await daemon.youtube_cookies_import(body.browser)}
        except Exception as exc:
            raise err(400, str(exc)[:300]) from exc

    @app.post("/api/youtube-signin/paste")
    async def yt_signin_paste(body: YtCookiesPaste) -> dict[str, Any]:
        try:
            return {"ok": True, **await daemon.youtube_cookies_paste(body.text)}
        except ValueError as exc:
            raise err(400, str(exc)[:300]) from exc

    @app.post("/api/youtube-signin/verify")
    async def yt_signin_verify() -> dict[str, Any]:
        return {"ok": True, **await daemon.youtube_cookies_verify()}

    @app.delete("/api/youtube-signin")
    async def yt_signin_remove() -> dict[str, Any]:
        daemon.youtube_cookies_remove()
        return {"ok": True}

    @app.post("/api/connect/twitch")
    async def connect_twitch(body: ConnectTwitch) -> dict[str, Any]:
        try:
            await daemon.connect_twitch(body.client_id, body.client_secret)
        except Exception as exc:
            raise err(400, str(exc)[:300]) from exc
        return {"ok": True}

    @app.get("/api/discover")
    async def discover(source: str, q: str | None = None, category: str | None = None, lang: str | None = "en",
                       refresh: bool = False, cache_only: bool = False) -> dict[str, Any]:
        from clipper.modules.discover import DiscoverError
        try:
            return await daemon.discover.find(source, query=q or None, category=category or None,
                                              language=lang or None, refresh=refresh, quota=daemon.queue,
                                              cache_only=cache_only)
        except DiscoverError as exc:
            raise err(400, str(exc)) from exc
        except Exception as exc:
            raise err(502, f"discovery failed: {str(exc).splitlines()[0][:200]}") from exc

    @app.post("/api/sources/{slug}/publish")
    async def source_publish(slug: str, body: TogglePublish) -> dict[str, Any]:
        try:
            daemon.set_source_publish(slug, body.publish)
        except KeyError as exc:
            raise err(404, f"unknown source {slug}") from exc
        return {"ok": True}

    # ---- videos
    @app.post("/api/vods")
    async def add_vods(body: AddVods) -> dict[str, Any]:
        from clipper.modules.vod import local_path

        urls = [u.strip() for u in body.urls if u.strip()]
        bad = [u for u in urls if not re.match(r"^https?://\S+$", u) and local_path(u) is None]
        if not urls or bad:
            raise err(400, f"Not a link or a video file on this computer: {bad[0]}" if bad
                      else "Add at least one video link or file.")
        jobs = daemon.add_vods(urls, clips=body.clips, category=body.category or None, publish=body.publish,
                               style=body.style)
        return {"ok": True, "jobs": [j.id for j in jobs]}

    @app.put("/api/upload")
    async def upload_video(request: Request, name: str) -> dict[str, Any]:
        """Your own video file, streamed straight to disk (raw body, no multipart). Returns its path,
        which is then clipped like a link."""
        import time as _time

        from clipper.modules.vod import VIDEO_EXTS

        suffix = Path(name).suffix.lower()
        if suffix not in VIDEO_EXTS:
            raise err(400, f"Not a video file: {name} (use {', '.join(sorted(VIDEO_EXTS))})")
        stem = re.sub(r"[^A-Za-z0-9 _-]+", "", Path(name).stem).strip()[:60] or "video"
        dest_dir = daemon.s.vod_dir / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"upload_{int(_time.time())}_{stem.replace(' ', '_')}{suffix}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        size = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 20 * 1024**3:
                        raise err(413, "Video is larger than 20 GB")
                    await asyncio.to_thread(fh.write, chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise err(400, "Empty file")
        tmp.replace(dest)
        logging.getLogger(__name__).info("uploaded %s (%.1f MB)", dest.name, size / 1e6)
        return {"ok": True, "path": str(dest.resolve())}

    # ---- clips
    @app.post("/api/clips/{clip_id}/{action:str}")
    async def clip_action(clip_id: int, action: Literal["cancel", "retry", "publish", "delete"]) -> dict[str, Any]:
        try:
            if action == "cancel":
                n = daemon.cancel_clip(clip_id)
            elif action == "retry":
                n = daemon.retry_clip(clip_id)
            elif action == "publish":
                n = daemon.publish_clip(clip_id)
            else:
                daemon.delete_clip(clip_id)
                n = 1
        except KeyError as exc:
            raise err(404, f"unknown clip {clip_id}") from exc
        return {"ok": True, "changed": n}

    # ---- publishing
    @app.post("/api/publishing/hold")
    async def hold(body: Hold) -> dict[str, Any]:
        if daemon.publisher is None:
            raise err(409, "Publishing is not running in this session.")
        daemon.publisher.hold = body.hold
        return {"ok": True, "hold": body.hold}

    # ---- logs
    buffer = daemon_log_buffer()

    @app.get("/api/logs")
    async def logs(after: int = 0) -> list[dict[str, Any]]:
        return [r for r in buffer.records if r["seq"] > after]

    @app.get("/api/logs/stream")
    async def log_stream(request: Request) -> StreamingResponse:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        buffer.subscribers.add(q)

        async def gen() -> Any:
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"id: {item['seq']}\ndata: {json.dumps(item)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                buffer.subscribers.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- clip files (wherever the library is)
    @app.get("/clip/{clip_id}/{kind}")
    async def clip_file(clip_id: int, kind: Literal["video", "thumb"]) -> FileResponse:
        from clipper.modules.library import thumb_of

        row = await daemon.queue.get_clip(clip_id)
        if row is None:
            raise err(404, "not found")
        video = Path(row["path"])
        path = video if kind == "video" else thumb_of(video)
        if not path.is_file():
            raise err(404, "not found")
        return FileResponse(path, media_type="video/mp4" if kind == "video" else "image/jpeg",
                            headers={"Cache-Control": "no-cache"})

    # ---- media (previews and picker frames)
    @app.get("/media/{name}")
    async def media(name: str) -> FileResponse:
        if not _SAFE_NAME.match(name):
            raise err(404, "not found")
        path = (rendered / name).resolve()
        if path.parent != rendered or not path.is_file():
            raise err(404, "not found")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    return app


_BUFFER = None


def daemon_log_buffer() -> Any:
    """The process-wide LogBuffer (installed by install_log_buffer)."""
    global _BUFFER
    if _BUFFER is None:
        _BUFFER = install_log_buffer()
    return _BUFFER


def install_log_buffer() -> Any:
    import logging

    from clipper.modules.daemon import LogBuffer

    global _BUFFER
    if _BUFFER is None:
        _BUFFER = LogBuffer()
        _BUFFER.loop = asyncio.get_running_loop()
        logging.getLogger().addHandler(_BUFFER)
    return _BUFFER
