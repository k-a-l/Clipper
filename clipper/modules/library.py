"""The clip library: one folder per clip, on this Mac or an external drive.

Layout (``<library>`` is chosen in the dashboard, default ``storage/library``)::

    <library>/
      Clips/<Source name>/<YYYY-MM-DD HH.MM> <hook>/
          video.mp4        the finished clip
          thumbnail.jpg    the thumbnail
          transcript.txt   what was said
          post.txt         ready-to-paste titles, descriptions and hashtags for manual uploads
          info.json        source, timing, style and where it was posted
      Source videos/       downloaded videos the clips were cut from (needed to re-cut)

Deleting a clip folder (in Finder or from the dashboard) cleanly removes that clip: the app
notices and drops its pending posts. A *missing drive* is different from a deleted file:
:func:`offline` tells the two apart so unplugging a drive never cancels anything.

Moves between locations go clip by clip (copy → verify sizes → switch the database entry → delete
the old copy), so an interrupted move never loses or duplicates a clip.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CLIPS = "Clips"
SOURCES = "Source videos"
VIDEO, THUMB, TRANSCRIPT, POST, INFO = "video.mp4", "thumbnail.jpg", "transcript.txt", "post.txt", "info.json"
_SYSTEM_VOLUMES = {"Recovery", "Preboot", "VM", "Update", "xarts", "iSCPreboot", "Hardware"}


# ------------------------------------------------------------------ file names
def safe_name(text: str, limit: int = 60) -> str:
    """A folder name that works on macOS, Windows-formatted drives (exFAT/NTFS) and Linux."""
    text = re.sub(r"[\x00-\x1f:/\\]+", " ", text or "")  # separators become spaces
    text = re.sub(r"[<>\"|?*#%]+", "", text)  # characters other drives reject simply go
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]+", "", text)  # emoji
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit].rstrip(" .") or "clip")


def unique_dir(parent: Path, name: str) -> Path:
    """``parent/name``, or ``parent/name (2)``… when taken. Not created."""
    cand, n = parent / name, 2
    while cand.exists():
        cand, n = parent / f"{name} ({n})", n + 1
    return cand


def clip_dir_name(created: float, hook: str) -> str:
    return safe_name(f"{time.strftime('%Y-%m-%d %H.%M', time.localtime(created))} {hook or 'clip'}", 70)


def is_clip_folder(video: Path) -> bool:
    return video.name == VIDEO


def thumb_of(video: Path) -> Path:
    return video.parent / THUMB if is_clip_folder(video) else video.with_suffix(".jpg")


def transcript_of(video: Path) -> Path:
    return video.parent / TRANSCRIPT if is_clip_folder(video) else video.with_suffix(".txt")


def sidecars(video: Path) -> list[Path]:
    """Every file belonging to a clip (existing or not)."""
    if is_clip_folder(video):
        return [video.parent / n for n in (VIDEO, THUMB, TRANSCRIPT, POST, INFO)]
    return [video, video.with_suffix(".jpg"), video.with_suffix(".txt")]


# ------------------------------------------------------------------ drives
def mount_root(path: Path) -> Path | None:
    """The drive folder a path lives on (``/Volumes/X``, ``/media/user/X``…); None for the system disk."""
    parts = Path(os.path.abspath(path)).parts
    if len(parts) >= 3 and parts[1] in ("Volumes", "mnt"):
        return Path(*parts[:3])
    if len(parts) >= 4 and parts[1] == "media":
        return Path(*parts[:4])
    if len(parts) >= 5 and parts[1:3] == ("run", "media"):
        return Path(*parts[:5])
    return None


def offline(path: Path) -> bool:
    """True when ``path`` is on a drive that isn't connected (so a missing file isn't a deletion)."""
    root = mount_root(path)
    if root is None:
        return False
    if sys.platform == "darwin" and root.is_symlink():  # /Volumes/Macintosh HD → /
        return False
    return not root.is_dir()


def usage(path: Path) -> dict[str, int] | None:
    try:
        u = shutil.disk_usage(path)
        return {"total": u.total, "free": u.free}
    except OSError:
        return None


def drives(home: Path | None = None) -> list[dict[str, Any]]:
    """This computer plus every mounted external drive, with free space (for the location picker)."""
    out: list[dict[str, Any]] = []
    home = home or Path.home()
    videos = home / ("Movies" if sys.platform == "darwin" else "Videos")
    mac = {"name": "This computer", "path": str(videos / "Clipper"), "internal": True}
    out.append(mac | (usage(home) or {}) | {"writable": os.access(home, os.W_OK)})
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates = [p for p in Path("/Volumes").iterdir()] if Path("/Volumes").is_dir() else []
    elif os.name == "nt":  # other drive letters (D:, E:, …), skipping the system drive
        sysdrive = os.environ.get("SystemDrive", "C:").upper()
        candidates = [Path(f"{c}:\\") for c in "DEFGHIJKLMNOPQRSTUVWXYZ"
                      if f"{c}:" != sysdrive and Path(f"{c}:\\").exists()]
    else:
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/mnt")):
            if base.is_dir():
                candidates += list(base.iterdir())
    for p in sorted(candidates):
        try:
            if p.resolve() == Path("/") or not p.is_dir() or p.name.startswith(".") or p.name in _SYSTEM_VOLUMES:
                continue  # the system disk alias, macOS helper volumes, or junk
        except OSError:
            continue
        info = usage(p)
        if info is None:
            continue
        out.append({"name": p.name, "path": str(p / "Clipper Clips"), "internal": False,
                    "writable": os.access(p, os.W_OK)} | info)
    return out


def folder_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*") if path.is_dir() else [path]:
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


# ------------------------------------------------------------------ safe moves
def _same_device(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def move_file(src: Path, dest: Path) -> None:
    """Move one file; across drives copy → fsync → verify size → delete the original."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _same_device(src.parent, dest.parent):
        os.replace(src, dest)
        return
    tmp = dest.with_name(f".{dest.name}.moving")
    shutil.copy2(src, tmp)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"copy of {src.name} is incomplete (drive full or removed?)")
    os.replace(tmp, dest)
    src.unlink()


def move_clip_files(video: Path, dest_dir: Path) -> Path:
    """Move a clip (folder layout or legacy loose files) into ``dest_dir``; return the new video path.

    The destination is complete before anything is deleted at the source.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if is_clip_folder(video):
        files = [f for f in video.parent.iterdir() if f.is_file() and not f.name.startswith(".")]
        names = {f: f.name for f in files}
    else:
        legacy = {video: VIDEO, video.with_suffix(".jpg"): THUMB, video.with_suffix(".txt"): TRANSCRIPT}
        names = {f: n for f, n in legacy.items() if f.is_file()}
    # copy everything first (cheap rename on the same drive), video last so a half-moved clip
    # still has its video at the old place
    order = sorted(names, key=lambda f: f.suffix == ".mp4")
    moved: list[tuple[Path, Path]] = []
    try:
        for f in order:
            target = dest_dir / names[f]
            move_file(f, target)
            moved.append((f, target))
    except Exception:
        for src, target in reversed(moved):  # roll back what already moved
            try:
                move_file(target, src)
            except OSError:
                log.error("rollback failed for %s (it is at %s)", src, target)
        raise
    if is_clip_folder(video):
        remove_empty_dirs(video.parent)
        remove_empty_dirs(video.parent.parent)  # the source folder, once its last clip has left
    return dest_dir / VIDEO


def remove_empty_dirs(path: Path, stop_at: Path | None = None) -> None:
    """Remove ``path`` and empty parents (ignores .DS_Store), never above ``stop_at``."""
    cur = path
    while True:
        if stop_at is not None and (cur == stop_at or stop_at not in cur.parents):
            return
        try:
            junk = [f for f in cur.iterdir() if f.name in (".DS_Store", "Thumbs.db")]
            if any(f for f in cur.iterdir() if f not in junk):
                return
            for f in junk:
                f.unlink(missing_ok=True)
            cur.rmdir()
        except OSError:
            return
        if stop_at is None:
            return
        cur = cur.parent


def delete_clip_files(video: Path, library_roots: list[Path]) -> None:
    """Delete a clip's files. Its own folder goes too (only when it's a clip folder in a library)."""
    folder = video.parent
    inside = any(root in folder.parents for root in library_roots)
    if is_clip_folder(video) and inside and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        source_dir = folder.parent
        remove_empty_dirs(source_dir, stop_at=source_dir.parent)
        return
    for f in sidecars(video):
        f.unlink(missing_ok=True)


# ------------------------------------------------------------------ sidecars
def write_sidecars(video: Path, row: dict[str, Any], posts: list[dict[str, Any]] | None = None) -> None:
    """post.txt (copy-paste metadata) + info.json next to a clip in folder layout."""
    if not is_clip_folder(video) or not video.parent.is_dir():
        return
    from clipper.modules.metadata import ClipContext, MetadataGenerator

    ctx = ClipContext(channel=row.get("source_id") or "clip", display_name=row.get("display_name") or "the creator",
                      source_url=row.get("source_url") or "", category=row.get("category") or None,
                      transcript=row.get("transcript") or "", hook=row.get("hook") or "",
                      game=row.get("game") or None, action=row.get("action") or None,
                      story=row.get("story") or "")
    gen = MetadataGenerator()
    yt, fb = gen.generate(ctx, "youtube", set()), gen.generate(ctx, "facebook", set())
    post = "\n".join([
        f"{row.get('hook') or 'Clip'}",
        f"Source: {ctx.display_name}  {ctx.source_url}".rstrip(),
        "",
        "=== YouTube Shorts ===",
        f"Title: {yt.title}",
        "Description:", yt.description,
        f"Tags: {', '.join(yt.tags)}",
        "",
        "=== Facebook Reels ===",
        "Caption:", fb.description,
        "",
        "(Suggestions; the app writes fresh wording when it posts for you.)",
        "",
    ])
    try:
        opts = json.loads(row.get("render_opts") or "null")
    except ValueError:
        opts = None
    info = {
        "clip_id": row.get("id"), "hook": row.get("hook"), "created": row.get("created_at"),
        "duration_s": row.get("duration"), "score": row.get("score"),
        "source": {"id": row.get("source_id"), "name": row.get("display_name"), "url": row.get("source_url"),
                   "category": row.get("category")},
        "style": opts, "posts": posts or [],
    }
    try:
        _atomic_write(video.parent / POST, post)
        _atomic_write(video.parent / INFO, json.dumps(info, indent=2, default=str))
    except OSError as exc:
        log.warning("couldn't write clip notes in %s: %s", video.parent, exc)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ move progress
@dataclass
class MoveState:
    status: str = "idle"  # idle|moving|done|failed
    target: str = ""
    total: int = 0
    done: int = 0
    skipped: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current: str = ""
    errors: list[str] = field(default_factory=list)
    started: float = 0.0
    finished: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))
