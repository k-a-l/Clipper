"""Non-live (VOD) URL download via yt-dlp.

yt-dlp (Unlicense, https://github.com/yt-dlp/yt-dlp) supports YouTube, Twitch
VODs/clips and ~1800 other sites. Since late 2025 YouTube downloads require a
JavaScript runtime for signature challenges; we pass the configured runtime
(``JS_RUNTIME``, default ``node``) and use the ``yt-dlp[default]`` extra, which
bundles the challenge solver (yt-dlp-ejs).

Downloads take the highest resolution available (``VOD_MAX_HEIGHT``, default 4320 = no practical
cap: a 4K source downscaled to the 1440x2560 output is the sharpest, a 1080p one upscaled looks soft) and merged to MP4 with the
same FFmpeg the pipeline uses. Files are cached by video id.

Long videos and ended live streams (``live_status == "post_live"``, often 10+ hours) are never
downloaded whole: :func:`download_audio` fetches the small audio-only track to find the loud
moments, then :func:`download_section` fetches just those few minutes of video.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from clipper.config import Settings
from clipper.modules import yt_auth
from clipper.modules.utils import run_cmd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VodInfo:
    path: Path
    slug: str
    title: str
    uploader: str
    webpage_url: str
    duration: float
    tags: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    description: str = ""


# yt-dlp runs in a *subprocess*: importing it in-process monkey-patches urllib3 globally,
# which breaks Streamlink's live checks (found in end-to-end testing, 2026-11-19). A child
# process also isolates hangs/crashes and can be killed on timeout.
_INFO_TMPL = "%(.{id,title,uploader,channel,webpage_url,duration,is_live,live_status,tags,categories,description})j"


def _format(max_h: int) -> str:
    """Best video up to ``max_h`` + best audio (MP4/M4A preferred at equal height, VP9/AV1 for 1440p+)."""
    # SDR first: an HDR (10-bit PQ/HLG) source rendered into the SDR clip comes out washed out
    sdr = f"[height<={max_h}][dynamic_range=SDR]"
    return (f"bv*{sdr}+ba[ext=m4a]/bv*{sdr}+ba/bv*[height<={max_h}]+ba[ext=m4a]/bv*[height<={max_h}]+ba/"
            f"b[height<={max_h}]/b")


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts", ".flv"}


def local_path(url: str) -> Path | None:
    """A video file on this Mac (``/path/file.mp4``, ``~/…``, ``file://…``), else None."""
    from urllib.parse import unquote, urlparse

    raw = url.strip().strip("'\"")
    if raw.startswith("file://"):
        raw = unquote(urlparse(raw).path)
    if not (raw.startswith(("/", "~")) or raw[1:3] == ":\\"):
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() and path.suffix.lower() in VIDEO_EXTS else None


async def local_info(s: Settings, path: Path) -> VodInfo:
    """VodInfo for your own video file (uploaded in the dashboard or a path on this Mac)."""
    from clipper.modules.utils import ffprobe_duration

    duration = await ffprobe_duration(s.ffprobe_bin, path, timeout=60)
    if not duration:
        raise RuntimeError(f"{path.name} isn't a playable video")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "", path.stem).lower()[:24] or "video"
    title = re.sub(r"[_-]+", " ", re.sub(r"^upload_\d+_", "", path.stem)).strip()
    log.info("local video: %s (%.0fs)", path.name, duration)
    return VodInfo(path=path.resolve(), slug=f"file_{stem}", title=title, uploader=s.my_channel_name or "",
                   webpage_url=path.resolve().as_uri(), duration=duration)


async def download(s: Settings, url: str, timeout: float = 3600) -> VodInfo:
    """Download (or reuse a cached copy of) a non-live video via the yt-dlp CLI. Local files are used in place."""
    if (path := local_path(url)) is not None:
        return await local_info(s, path)
    s.vod_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-progress", "--no-warnings",
        "--js-runtimes", s.js_runtime, *yt_auth.ytdlp_args(s), "-f", _format(s.vod_max_height), "--merge-output-format", "mp4",
        "-o", str(s.vod_dir / "%(extractor_key)s_%(id)s.%(ext)s"),
        "--retries", "5", "--fragment-retries", "10", "-N", "4",
        "--no-simulate", "-O", _INFO_TMPL, "-O", "after_move:filepath",
        "--match-filter", "!is_live", url,
    ]
    if "/" in s.ffmpeg_bin:
        cmd[3:3] = ["--ffmpeg-location", str(Path(s.ffmpeg_bin).parent)]
    log.info("downloading VOD %s", url)
    res = await run_cmd(cmd, timeout=timeout)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"yt-dlp produced no file for {url} (live stream? use SOURCES): {res.stderr[-300:]}")
    info = json.loads(lines[0])
    path = Path(lines[-1])
    if not path.is_file():
        raise RuntimeError(f"download finished but {path} is missing")
    vod = _vod_info(info, path, url)
    log.info("VOD ready: %s (%.0fs, %s)", vod.path.name, vod.duration, vod.uploader)
    return vod


def _vod_info(info: dict, path: Path, url: str) -> VodInfo:
    slug = "vod_" + re.sub(r"[^A-Za-z0-9_-]+", "", str(info.get("id", "x"))).lower()[:32]
    return VodInfo(
        path=path, slug=slug, title=str(info.get("title") or ""),
        uploader=str(info.get("uploader") or info.get("channel") or "the creator"),
        webpage_url=str(info.get("webpage_url") or url), duration=float(info.get("duration") or 0),
        tags=tuple(str(t) for t in (info.get("tags") or [])[:30]),
        categories=tuple(str(t) for t in (info.get("categories") or [])),
        description=str(info.get("description") or "")[:2000],
    )


def _ytdlp(s: Settings) -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-progress", "--no-warnings",
           "--js-runtimes", s.js_runtime, *yt_auth.ytdlp_args(s)]
    if "/" in s.ffmpeg_bin:
        cmd += ["--ffmpeg-location", str(Path(s.ffmpeg_bin).parent)]
    return cmd


async def probe(s: Settings, url: str) -> dict:
    """Metadata only (no download): duration, live_status ("is_live", "post_live", "was_live", …)."""
    res = await run_cmd([*_ytdlp(s), "--skip-download", "-O", _INFO_TMPL, url], timeout=120)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"no information for {url}: {res.stderr[-300:]}")
    return json.loads(lines[-1])


def needs_sections(s: Settings, info: dict) -> bool:
    """Too long to download whole (or an ended live stream whose full file can be many GB)."""
    return info.get("live_status") == "post_live" or float(info.get("duration") or 0) > s.vod_full_download_max_s


async def download_audio(s: Settings, url: str, info: dict, timeout: float = 3600) -> VodInfo:
    """Audio-only track (~30 MB per hour) for highlight analysis of a long video."""
    s.vod_dir.mkdir(parents=True, exist_ok=True)
    out = s.vod_dir / "audio"
    out.mkdir(exist_ok=True)
    log.info("downloading audio of %s (%.0f min) to find highlights", url, float(info.get("duration") or 0) / 60)
    res = await run_cmd([*_ytdlp(s), "-f", "139/ba[abr<=100]/ba", "--retries", "5", "--fragment-retries", "10",
                         "-N", "16", "-o", str(out / "%(extractor_key)s_%(id)s.%(ext)s"),
                         "--no-simulate", "-O", "after_move:filepath", url], timeout=timeout)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    path = Path(lines[-1]) if lines else None
    if path is None or not path.is_file():
        raise RuntimeError(f"audio download failed for {url}: {res.stderr[-300:]}")
    return _vod_info(info, path, url)


_FORMATS_CACHE: dict[str, tuple[float, dict]] = {}


async def _formats(s: Settings, url: str) -> dict:
    """Full yt-dlp info (formats with signed fragment URLs), cached ~1 h (the URLs expire after ~6 h)."""
    import time

    hit = _FORMATS_CACHE.get(url)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    res = await run_cmd([*_ytdlp(s), "-J", url], timeout=180)
    info = json.loads(res.stdout)
    _FORMATS_CACHE[url] = (time.time(), info)
    return info


def _pick(formats: list[dict], kind: str, max_h: int) -> dict | None:
    if kind == "video":
        vids = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
                and (f.get("height") or 0) <= max_h and f.get("fragments")]
        return max(vids, key=lambda f: (f.get("dynamic_range") in (None, "SDR"), f.get("height") or 0,
                                        f.get("fps") or 0, f.get("tbr") or 0), default=None)
    auds = [f for f in formats if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
            and f.get("fragments")]
    return max(auds, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or f.get("tbr") or 0), default=None)


async def _fetch_fragments(fmt: dict, first: int, last: int, out: Path) -> None:
    """Fragments ``first..last`` (inclusive) of a segmented DASH format, concatenated into ``out``."""
    import asyncio

    import httpx

    frags = fmt["fragments"]
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(headers=fmt.get("http_headers") or {}, timeout=60, follow_redirects=True) as client:
        async def one(i: int) -> bytes:
            async with sem:
                for attempt in range(4):
                    try:
                        frag = frags[i]
                        r = await client.get(frag.get("url") or (fmt.get("fragment_base_url") or "") + frag["path"])
                        r.raise_for_status()
                        return r.content
                    except httpx.HTTPError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(1.5 * (attempt + 1))
            return b""

        parts = await asyncio.gather(*(one(i) for i in range(first, last + 1)))
    out.write_bytes(b"".join(parts))


async def _section_from_fragments(s: Settings, url: str, start: float, end: float, out: Path) -> float:
    """Ended live stream: download only the 2-second DASH fragments covering ``[start, end]`` and
    merge them. Returns where ``start`` lies inside ``out`` (fragments start on 2 s boundaries)."""
    info = await _formats(s, url)
    formats = info.get("formats") or []
    video, audio = _pick(formats, "video", s.vod_max_height), _pick(formats, "audio", 0)
    if video is None or audio is None:
        raise RuntimeError("no segmented video/audio formats")
    total = float(info.get("duration") or 0)
    n = len(video["fragments"])
    frag_len = total / n if total and n else 2.0
    first = max(0, int(start // frag_len))
    last = min(n - 1, int(end // frag_len))
    tmp_v, tmp_a = out.with_suffix(".v.part"), out.with_suffix(".a.part")
    try:
        await _fetch_fragments(video, first, last, tmp_v)
        await _fetch_fragments(audio, first, min(last, len(audio["fragments"]) - 1), tmp_a)
        # the fragments keep the stream's absolute timestamps: the true start of what we fetched
        starts = []
        for part in (tmp_v, tmp_a):
            res = await run_cmd([s.ffprobe_bin, "-v", "error", "-show_entries", "stream=start_time", "-of", "csv=p=0",
                                 str(part)], timeout=60, check=False)
            starts += [float(x) for x in res.stdout.split() if re.fullmatch(r"-?\d+(\.\d+)?", x.strip(", "))]
        base = min(starts) if starts else first * frag_len
        # the fragments carry absolute timestamps; -c copy re-bases both streams to 0
        await run_cmd([s.ffmpeg_bin, "-hide_banner", "-loglevel", "fatal", "-y", "-i", str(tmp_v), "-i", str(tmp_a),
                       "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(out)],
                      timeout=300)
    finally:
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    log.info("section %.0f-%.0fs: %d fragments at %sp", start, end, last - first + 1, video.get("height"))
    return max(0.0, start - base)


async def download_section(s: Settings, url: str, start: float, end: float, out: Path,
                           live_status: str | None = None, timeout: float = 1200) -> tuple[Path, float]:
    """Just ``[start, end]`` seconds of the video (best quality up to VOD_MAX_HEIGHT) → ``out`` (.mp4).

    Returns ``(path, offset)``: ``start`` sits ``offset`` seconds into the file.

    * Ended live streams (``post_live``) are served as numbered 2 s fragments: only those are fetched.
    * Other videos: yt-dlp hands the cut to FFmpeg (input-side seek + stream copy), which writes an
      MP4 edit list so the file plays from exactly ``start`` (offset 0)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if live_status == "post_live":
        try:
            return out, await _section_from_fragments(s, url, start, end, out)
        except Exception as exc:  # noqa: BLE001 - fall back to yt-dlp's own cutter
            log.warning("fragment download failed (%s); trying yt-dlp sections", exc)
    await run_cmd([*_ytdlp(s), "-f", _format(s.vod_max_height), "--merge-output-format", "mp4",
                   "--download-sections", f"*{start:.2f}-{end:.2f}", "--retries", "5", "--force-overwrites",
                   "--fragment-retries", "10", "-o", str(out.with_suffix(".%(ext)s")), url], timeout=timeout)
    if not out.is_file():
        found = sorted(out.parent.glob(out.stem + ".*"))
        if not found:
            raise RuntimeError(f"section {start:.0f}-{end:.0f}s of {url} was not downloaded")
        found[0].rename(out)
    from clipper.modules.utils import ffprobe_duration

    got = await ffprobe_duration(s.ffprobe_bin, out)
    if not got or got < min(5.0, (end - start) * 0.5):
        out.unlink(missing_ok=True)
        raise RuntimeError(f"section {start:.0f}-{end:.0f}s of {url} came back empty")
    return out, 0.0
