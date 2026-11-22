#!/usr/bin/env python3
"""Clipper: live streams and videos → vertical Shorts / Reels.

Commands
--------
``ui [--open]``                 Everything, controlled from the local web dashboard (recommended).
``run``                         Monitor, record, detect, render and publish (no dashboard).
``vod URL…``                    Clip finished videos (YouTube, Twitch VODs, local files…).
``process FILE --at SECONDS``   Render one clip from a local video (testing).
``status`` / ``stats``          What's recording/queued; how posted clips are doing.
``auth-youtube``                One-time Google sign-in; stores an encrypted token.
``auth-facebook --user-token``  Exchange a user token for a Page token; stores it encrypted.
``gen-key``                     Print a new TOKEN_ENCRYPTION_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from clipper.config import Settings, get_settings
from clipper.modules.utils import setup_logging

log = logging.getLogger("clipper")


async def cmd_run(s: Settings, extra_urls: list[str] | None = None, publish_urls: bool = False,
                  url_category: str | None = None, publish: bool = True) -> int:
    """Headless daemon: watch every source, render, post."""
    from clipper.modules.daemon import Daemon, run_until_signal

    daemon = Daemon(s, publish=publish)
    await daemon.start(extra_urls, publish_urls, url_category)
    return await run_until_signal(daemon)


async def cmd_ui(s: Settings, port: int, host: str, publish: bool, open_browser: bool) -> int:
    """Daemon + local web dashboard."""
    import uvicorn

    from clipper.modules.daemon import Daemon, run_until_signal
    from clipper.modules.web import create_app, install_log_buffer

    install_log_buffer()
    daemon = Daemon(s, publish=publish)
    await daemon.start()
    app = create_app(daemon, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off"))
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]  # Daemon handles Ctrl+C
    url = f"http://127.0.0.1:{port}/"
    log.info("dashboard: %s", url)
    print(f"\n  Clipper dashboard → {url}\n  (Ctrl+C to stop)\n", flush=True)
    if open_browser:
        import webbrowser

        asyncio.get_running_loop().call_later(1.5, webbrowser.open, url)
    return await run_until_signal(daemon, server.serve)


async def cmd_process(s: Settings, src_file: Path, at: float, enqueue: bool) -> int:
    from clipper.modules.pipeline import ClipPipeline
    from clipper.modules.publisher import PublishQueue
    from clipper.modules.subtitles import Transcriber
    from clipper.modules.video_editor import ClipSource, VideoEditor

    duration = s.clip_pre_roll_s + s.clip_post_roll_s
    src = ClipSource.from_file(src_file, at - s.clip_pre_roll_s, duration)
    queue = PublishQueue(s.state_dir / "publisher.db") if enqueue else None
    pipeline = ClipPipeline(s, {}, VideoEditor(s), Transcriber(s), queue)
    name = f"manual_{src_file.stem}_{int(at)}_{int(time.time())}"
    workdir = s.work_dir / name
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        out = await pipeline.render(src, name, workdir, None)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(out)
    return 0


async def cmd_vod(s: Settings, urls: list[str], clips: int | None, publish: bool,
                  category: str | None) -> int:
    from clipper.modules.pipeline import ClipPipeline
    from clipper.modules.publisher import PublishQueue
    from clipper.modules.subtitles import Transcriber
    from clipper.modules.video_editor import VideoEditor

    queue = PublishQueue(s.state_dir / "publisher.db")
    pipeline = ClipPipeline(s, {}, VideoEditor(s), Transcriber(s), queue)
    out = await pipeline.process_urls(urls, clips_per_video=clips, publishable=publish, category=category)
    for path in out:
        print(path)
    log.info("%d clip(s) from %d URL(s)%s", len(out), len(urls),
             " — queued for publishing" if publish else " — render-only (pass --publish if you hold the rights)")
    return 0 if out else 1


def cmd_status(s: Settings) -> int:
    """Read-only snapshot: recordings per source, recent clips, queue, next slots, quota."""
    import sqlite3

    from clipper.modules.publisher import pacific_day

    def ago(ts: float) -> str:
        d = time.time() - ts
        return f"{d:.0f}s ago" if d < 120 else f"{d / 60:.0f} min ago" if d < 7200 else f"{d / 3600:.1f} h ago"

    from clipper.modules.utils import daemon_running

    print("== Daemon:", "RUNNING" if daemon_running(s.state_dir / "daemon.lock") else "not running")

    print("\n== Live sources")
    for src in s.all_sources():
        segs = sorted([f for ext in (".ts", ".mkv") for f in s.raw_dir_for(src).glob(f"*/*{ext}")],
                      key=lambda f: f.stat().st_mtime)
        if segs:
            last = segs[-1].stat().st_mtime
            state = "RECORDING" if time.time() - last < 90 else "idle"
            print(f"  {src.slug:<22} {state:<10} last data {ago(last):<12} {len(segs)} segment(s)  "
                  f"publish={'yes' if src.permission_confirmed else 'no'}  {src.source_url}")
        else:
            print(f"  {src.slug:<22} {'no data':<10} {'':<22} publish={'yes' if src.permission_confirmed else 'no'}  "
                  f"{src.source_url}")

    db_path = s.state_dir / "publisher.db"
    if not db_path.exists():
        print("\n(no publish database yet)")
        return 0
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    print("\n== Recent clips (newest first)")
    rows = db.execute(
        "SELECT c.id, c.created_at, c.source_id, c.hook, c.score, c.path, "
        "group_concat(j.platform || ':' || j.status || COALESCE(':' || j.remote_id, ''), '  ') AS jobs "
        "FROM clips c LEFT JOIN jobs j ON j.clip_id = c.id GROUP BY c.id ORDER BY c.created_at DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']:<3} {ago(r['created_at']):<12} {r['source_id'] or '-':<20} score {r['score']:<5.1f} "
              f"{(r['hook'] or '')[:30]:<31} {r['jobs'] or 'render-only'}")

    print("\n== Publishing")
    now = time.time()
    keys = [r[0] for r in db.execute("SELECT DISTINCT platform FROM jobs ORDER BY platform").fetchall()]
    for platform in keys or list(s.publish_platforms):
        counts = dict(db.execute("SELECT status, count(*) FROM jobs WHERE platform=? GROUP BY status",
                                 (platform,)).fetchall())
        today = db.execute("SELECT count(*) FROM jobs WHERE platform=? AND status='posted' AND posted_at>=?",
                           (platform, now - 86400)).fetchone()[0]
        nxt = db.execute("SELECT next_eligible_at FROM platform_state WHERE platform=?", (platform,)).fetchone()
        slot = "now" if not nxt or nxt[0] <= now else time.strftime("%H:%M", time.localtime(nxt[0]))
        print(f"  {platform:<9} pending {counts.get('pending', 0)}  posted(24h) {today}/{s.max_posts_per_day}  "
              f"failed {counts.get('failed', 0)}  next slot {slot}")
    day = pacific_day()
    used = {k: v for k, v in db.execute("SELECT platform, units FROM quota_usage WHERE day=?", (day,)).fetchall()}
    print(f"\n== YouTube quota today ({day} PT): {used.get('youtube', 0)}/{s.youtube_daily_quota} units "
          f"(chat reading {used.get('youtube_chat', 0)}/{s.youtube_chat_daily_budget})")
    return 0


async def cmd_queue(s: Settings) -> int:
    from clipper.modules.publisher import PublishQueue

    for row in await PublishQueue(s.state_dir / "publisher.db").summary():
        print(json.dumps(row, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows terminals often use a legacy code page that has no "→" or emoji: print what fits
    # instead of crashing with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(prog="clipper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    try:
        from importlib.metadata import version

        parser.add_argument("--version", action="version", version=f"clipper {version('clipper')}")
    except Exception:  # running from a plain checkout without installing  # noqa: BLE001
        pass
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="monitor all SOURCES (live) and optionally clip extra URLs")
    p_run.add_argument("--url", action="append", default=[], help="non-live URL to clip too (repeatable)")
    p_run.add_argument("--publish-urls", action="store_true", help="queue --url clips for publishing")
    p_run.add_argument("--url-category", help="topic for --url clips (hashtags), e.g. Space")
    p_run.add_argument("--no-publish", action="store_true", help="render only; never post in this session")
    p_ui = sub.add_parser("ui", help="run everything with the local web dashboard")
    p_ui.add_argument("--port", type=int, default=8787)
    p_ui.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you know why")
    p_ui.add_argument("--no-publish", action="store_true", help="render only; never post in this session")
    p_ui.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p_vod = sub.add_parser("vod", help="clip one or more non-live video URLs (YouTube, Twitch VOD, ...)")
    p_vod.add_argument("urls", nargs="+")
    p_vod.add_argument("--clips", type=int, help="clips per video (default VOD_CLIPS_PER_VIDEO)")
    p_vod.add_argument("--category", help="topic used for hashtags, e.g. Space")
    p_vod.add_argument("--publish", action="store_true",
                       help="queue for publishing; only if you own the videos or have the rights")
    p = sub.add_parser("process", help="render one clip from a local file")
    p.add_argument("file", type=Path)
    p.add_argument("--at", type=float, required=True, help="highlight time in seconds")
    p.add_argument("--enqueue", action="store_true", help="also queue it for publishing")
    p_yt = sub.add_parser("auth-youtube", help="authorise YouTube uploads")
    p_yt.add_argument("--port", type=int, default=8765)
    p_yt.add_argument("--no-browser", action="store_true", help="print the consent URL instead of opening it")
    p_fb = sub.add_parser("auth-facebook", help="store a Facebook Page token")
    tok = p_fb.add_mutually_exclusive_group(required=True)
    tok.add_argument("--user-token", help="user token from Graph API Explorer (visible in shell history!)")
    tok.add_argument("--user-token-stdin", action="store_true",
                     help="read the user token from stdin, e.g. `pbpaste | clipper auth-facebook --user-token-stdin`")
    sub.add_parser("gen-key", help="generate TOKEN_ENCRYPTION_KEY")
    sub.add_parser("queue", help="show recent publishing jobs (raw JSON)")
    sub.add_parser("status", help="what is recording, rendered, queued and posted right now")
    sub.add_parser("stats", help="views / likes / comments of everything posted (and reach problems)")
    args = parser.parse_args(argv)

    if args.command == "gen-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file if args.command in {"run", "ui", "vod", "process"} else None)
    settings.ensure_dirs()

    match args.command:
        case "run":
            return asyncio.run(cmd_run(settings, args.url, args.publish_urls, args.url_category,
                                       publish=not args.no_publish))
        case "ui":
            code = asyncio.run(cmd_ui(settings, args.port, args.host, not args.no_publish, args.open))
            from clipper.modules.daemon import RESTART_EXIT_CODE

            if code == RESTART_EXIT_CODE:  # "Restart" from the dashboard: same command, new code
                argv_again = [a for a in sys.argv[1:] if a != "--open"]
                print("\n  Restarting Clipper with the updated code…\n", flush=True)
                # as a module: works from a checkout and for the installed `clipper` command
                # (whose launcher is an .exe on Windows, which Python can't run as a script)
                if os.name == "nt":  # no real exec on Windows: run the new copy and pass its exit code on
                    return subprocess.call([sys.executable, "-m", "clipper", *argv_again])
                os.execv(sys.executable, [sys.executable, "-m", "clipper", *argv_again])
            return code
        case "vod":
            return asyncio.run(cmd_vod(settings, args.urls, args.clips, args.publish, args.category))
        case "process":
            return asyncio.run(cmd_process(settings, args.file, args.at, args.enqueue))
        case "status":
            return cmd_status(settings)
        case "stats":
            from clipper.modules.stats import print_report

            return print_report(settings)
        case "queue":
            return asyncio.run(cmd_queue(settings))
        case "auth-youtube":
            from clipper.modules.publisher import youtube_authorize

            youtube_authorize(settings, port=args.port, open_browser=not args.no_browser)
            return 0
        case "auth-facebook":
            from clipper.modules.publisher import facebook_authorize

            token = sys.stdin.read() if args.user_token_stdin else args.user_token
            asyncio.run(facebook_authorize(settings, token))
            return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
