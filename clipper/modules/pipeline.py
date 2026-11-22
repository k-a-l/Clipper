"""Clip pipeline: events/URLs → extract → transcribe → hook → render → thumbnail → enqueue.

Handles many sources at once:

* **Live**: one shared event queue fed by every source's chat analyser and audio
  watcher; ``render_workers`` tasks consume it in parallel. Events from the same
  source within ``clip_cooldown_s`` are merged (chat + audio often fire for the
  same moment). Each source can cap its clips per day.
* **VOD / non-live URLs**: each URL is downloaded with yt-dlp, scanned for the
  loudest-relative moments, and its top N moments are clipped.

Every clip is rendered with :class:`~config.RenderOptions` chosen from its source's
:class:`~config.ClipStyle` (fixed layout, or cycling / random through a list), and the
clip remembers *how to re-create itself* (``media`` + ``render_opts``) so the operator
can re-cut (move start, change length) or restyle it later from the dashboard.

Only clips from sources with ``permission_confirmed`` *and* ``auto_post`` are queued
automatically; everything else lands in the library to post now / schedule later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from clipper.config import MAX_CLIP_SECONDS, ClipStyle, RenderOptions, Settings, SourceConfig
from clipper.modules.audio_highlights import loudness_series, top_moments
from clipper.modules.chat_analyzer import ClipEvent
from clipper.modules.library import VIDEO, clip_dir_name, safe_name, thumb_of, transcript_of, unique_dir, write_sidecars
from clipper.modules.metadata import ClipContext, MetadataGenerator
from clipper.modules.publisher import PublishQueue
from clipper.modules.story import Story, write_story
from clipper.modules.stream_recorder import StreamRecorder, segments_in
from clipper.modules.subtitles import Transcriber, Word, words_to_text, write_ass
from clipper.modules.thumbnail import ThumbnailMaker
from clipper.modules.utils import ffprobe_duration
from clipper.modules.video_editor import ClipSource, Effects, VideoEditor
from clipper.modules.voiceover import sfx, strip_tags, synthesize

log = logging.getLogger(__name__)
LEAD_RATIO = 13 / 23  # where the highlight sits inside a clip (default 13 s before, 10 s after)


@dataclass(frozen=True)
class ClipOrigin:
    """Who/what a clip came from: drives attribution, metadata, style and publish permission."""

    id: str
    url: str
    name: str
    category: str | None
    publishable: bool
    style: ClipStyle = field(default_factory=lambda: ClipStyle())
    game: str | None = None  # detected game, e.g. "GTA 6"
    title: str | None = None  # stream / video title (context for the AI story)

    @classmethod
    def from_source(cls, src: SourceConfig, meta: dict[str, str] | None = None) -> ClipOrigin:
        from clipper.modules.games import detect_game

        meta = meta or {}
        g = detect_game(meta.get("category"), meta.get("title"), src.category)
        return cls(src.slug, src.source_url, src.name, src.category, src.permission_confirmed, src.style,
                   g.name if g else None, meta.get("title") or None)


def window_for(settings: Settings, style: ClipStyle, rng: random.Random | None = None,
               longest: bool = False) -> tuple[float, float]:
    """(seconds before, seconds after) the highlight for this style.

    ``random_length``: a fresh random total between ``random_clip_min_s`` and the style's length
    (``clip_seconds``, else ``random_clip_max_s``) on every call; ``longest`` returns the top of the range."""
    if style.random_length:
        hi = min(MAX_CLIP_SECONDS, style.clip_seconds or settings.random_clip_max_s)
        lo = min(settings.random_clip_min_s, hi)
        total = hi if longest else round((rng or random).uniform(lo, hi))
        before = round(total * LEAD_RATIO, 1)
        return before, total - before
    if style.clip_seconds:
        total = min(MAX_CLIP_SECONDS, style.clip_seconds)
        before = round(total * LEAD_RATIO, 1)
        return before, total - before
    return settings.clip_pre_roll_s, settings.clip_post_roll_s


def pick_render(style: ClipStyle, index: int, rng: random.Random | None = None) -> RenderOptions:
    """Render options for the ``index``-th clip of a source (layout rotation applied)."""
    opts = style.render.model_copy(deep=True)
    layouts = style.layouts or [opts.layout]
    if style.layout_mode == "cycle":
        opts.layout = layouts[index % len(layouts)]
    elif style.layout_mode == "random":
        opts.layout = (rng or random.Random()).choice(layouts)
    return opts


def source_from_media(settings: Settings, media: dict[str, Any], workdir: Path,
                      shift: float = 0.0, length: float | None = None) -> ClipSource:
    """Rebuild a ClipSource from a clip's stored ``media`` (optionally moved/re-sized)."""
    start = float(media["start"]) + shift
    duration = float(length if length is not None else media["duration"])
    duration = max(5.0, min(MAX_CLIP_SECONDS, duration))
    if media["kind"] == "file":
        path = Path(media["path"])
        if not path.is_file():
            raise FileNotFoundError("the original video file is gone (storage/vod was cleaned)")
        return ClipSource.from_file(path, max(0.0, start), duration)
    segs = segments_in(Path(media["raw_dir"]), start, start + duration)
    if not segs:
        raise FileNotFoundError(f"the live recording has expired (raw segments are kept "
                                f"{settings.raw_retention_hours:g} h)")
    real_start = max(start, segs[0].start)
    return ClipSource.from_segments(segs, real_start, min(start + duration, segs[-1].end) - real_start, workdir)


class ClipPipeline:
    def __init__(
        self,
        settings: Settings,
        recorders: dict[str, StreamRecorder] | StreamRecorder,
        editor: VideoEditor,
        transcriber: Transcriber,
        queue: PublishQueue | None,
    ) -> None:
        self.s = settings
        if isinstance(recorders, StreamRecorder):
            recorders = {recorders.source.slug: recorders}
        self.recorders = recorders
        self.editor = editor
        self.transcriber = transcriber
        self.queue = queue
        self.on_new_clip: Callable[[], None] | None = None  # e.g. wake the publisher
        self.accounts: Any = None  # AccountRegistry: which channels/Pages a clip posts to
        self._last_event: dict[str, float] = {}
        self._rng = random.Random()

    # ----------------------------------------------------------------- live
    async def run(self, events: asyncio.Queue[ClipEvent]) -> None:
        """Start ``render_workers`` consumers on the shared event queue."""
        workers = [asyncio.create_task(self._worker(events, i), name=f"render-{i}")
                   for i in range(self.s.render_workers)]
        try:
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                w.cancel()

    async def _worker(self, events: asyncio.Queue[ClipEvent], idx: int) -> None:
        while True:
            event = await events.get()
            try:
                await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[worker %d] clip failed for %s at %.0f", idx, event.source_id, event.trigger_time)
            finally:
                events.task_done()

    async def _source_index(self, source_id: str) -> int:
        """How many clips this source already has (drives layout cycling across restarts)."""
        if self.queue is None:
            return 0
        return await self.queue.clips_since(source_id, 0)

    async def handle_event(self, event: ClipEvent) -> Path | None:
        source_id = event.source_id or next(iter(self.recorders))
        recorder = self.recorders.get(source_id)
        if recorder is None:
            log.warning("event for unknown source %r", source_id)
            return None
        style = recorder.source.style
        last = self._last_event.get(source_id, 0.0)
        if abs(event.trigger_time - last) < self.s.clip_cooldown_s:
            log.info("[%s] %s event merged with clip at %.0f", source_id, event.reason, last)
            return None
        if style.max_clips_per_day and self.queue is not None:
            made = await self.queue.clips_since(source_id, time.time() - 86_400)
            if made >= style.max_clips_per_day:
                log.info("[%s] daily clip limit reached (%d); skipping %s event", source_id, made, event.reason)
                return None
        self._last_event[source_id] = event.trigger_time

        before, after = window_for(self.s, style, self._rng)
        log.info("[%s] clip length %.0fs", source_id, before + after)
        window_start = event.trigger_time - before
        window_end = event.trigger_time + after
        wait = window_end + 5 - time.time()
        if wait > 0:  # let the post-roll be recorded
            await asyncio.sleep(wait)

        segments = recorder.segments_for_window(window_start, window_end)
        if not segments:
            log.warning("[%s] no recorded segments cover the event window; skipping", source_id)
            return None
        start = max(window_start, segments[0].start)
        duration = min(window_end, segments[-1].end) - start
        if duration < self.s.clip_min_duration_s:
            log.warning("[%s] only %.1fs of footage available; skipping", source_id, duration)
            return None

        name = f"{source_id}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(event.trigger_time))}"
        workdir = self.s.work_dir / name
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            src = ClipSource.from_segments(segments, start, duration, workdir)
            opts = pick_render(style, await self._source_index(source_id), self._rng)
            media = {"kind": "live", "raw_dir": str(recorder.raw_dir), "start": start, "duration": duration,
                     "highlight": event.trigger_time - start}
            return await self.render(src, name, workdir, event, highlight_at=event.trigger_time - start,
                                     origin=ClipOrigin.from_source(recorder.source, recorder.meta), opts=opts, media=media)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ VOD
    async def process_urls(self, urls: list[str], *, clips_per_video: int | None = None,
                           publishable: bool = False, category: str | None = None,
                           style: ClipStyle | None = None) -> list[Path]:
        """Clip several non-live URLs concurrently; returns all rendered clip paths."""
        sem = asyncio.Semaphore(self.s.vod_concurrency)

        async def one(url: str) -> list[Path]:
            async with sem:
                try:
                    return await self.process_vod(url, clips_per_video=clips_per_video, publishable=publishable,
                                                  category=category, style=style)
                except Exception:
                    log.exception("VOD failed: %s", url)
                    return []

        results = await asyncio.gather(*(one(u) for u in urls))
        return [p for clips in results for p in clips]

    async def process_vod(self, url: str, *, clips_per_video: int | None = None,
                          publishable: bool = False, category: str | None = None,
                          style: ClipStyle | None = None,
                          on_stage: Callable[[str, dict[str, Any]], None] | None = None) -> list[Path]:
        """Download → analyse → render top moments. ``on_stage(stage, info)`` reports progress."""
        from clipper.modules import vod

        def stage(name: str, **info: Any) -> None:
            if on_stage is not None:
                on_stage(name, info)

        style = style or ClipStyle()
        n = clips_per_video or self.s.vod_clips_per_video
        stage("downloading")
        # Long videos / ended live streams (often 10+ hours): fetch only the audio to find the
        # highlights, then only those sections of video (modules.vod.download_section).
        meta: dict[str, Any] = {}
        if vod.local_path(url) is None:
            try:
                meta = await vod.probe(self.s, url)
            except Exception as exc:  # noqa: BLE001 - the full download reports the real error
                log.info("probe failed for %s (%s); downloading the whole video", url, exc)
        if meta.get("live_status") == "is_live":
            raise RuntimeError("this video is live right now: add it as a live stream to record it")
        sections = bool(meta) and vod.needs_sections(self.s, meta)
        info = await vod.download_audio(self.s, url, meta) if sections else await vod.download(self.s, url)
        live_status = str(meta.get("live_status") or "")
        stage("analysing", title=info.title, uploader=info.uploader, duration=info.duration)
        media, duration = info.path, info.duration
        from clipper.modules.games import detect_game

        found_game = detect_game(None, info.title, " ".join(info.tags), " ".join(info.categories), info.description[:600])
        origin = ClipOrigin(id=info.slug, url=info.webpage_url, name=info.uploader, game=found_game.name if found_game else None,
                            category=category, publishable=publishable, style=style, title=info.title or None)
        before, after = window_for(self.s, style, longest=True)  # spacing between moments: the longest clip
        whole = False
        log.info("[%s] analysing %.0fs of audio in %r", info.slug, duration, info.title)
        if duration and duration <= before + after + 5:
            # Already a short highlight (e.g. a Twitch clip): use the whole thing as one clip.
            from clipper.modules.audio_highlights import Peak

            before, after = duration * LEAD_RATIO, duration * (1 - LEAD_RATIO)
            moments = [Peak(t=before, loudness=0.0, z=3.0)]
            whole = True
        else:
            series = await loudness_series(self.s.ffmpeg_bin, media)
            moments = top_moments(series, n, min_gap_s=max(self.s.audio_min_gap_s, before + after),
                                  duration=duration)
        log.info("[%s] %d highlight(s): %s", info.slug, len(moments),
                 ", ".join(f"{m.t:.0f}s (z {m.z})" for m in moments))

        stage("rendering", total=len(moments), done=0)
        render_sem = asyncio.Semaphore(self.s.render_workers)
        finished = 0
        skipped = 0
        base_index = await self._source_index(info.slug)

        async def clip(i: int, m: Any) -> Path | None:
            nonlocal finished, skipped
            pre, post = (before, after) if whole else window_for(self.s, style, self._rng)
            start = max(0.0, m.t - pre)
            length = min(pre + post, duration - start)
            name = f"{info.slug}_{int(m.t):05d}"
            if self.queue is not None and await self.queue.clip_exists(name):
                # Same video + same moment was clipped before: never re-render/re-post it.
                skipped += 1
                stage("rendering", total=len(moments), done=finished, skipped=skipped)
                log.info("[%s] moment at %.0fs already clipped (%s); skipping", info.slug, m.t, name)
                return None
            workdir = self.s.work_dir / name
            workdir.mkdir(parents=True, exist_ok=True)
            event = ClipEvent(trigger_time=time.time(), reason="audio", velocity=0, baseline_mean=0.0,
                              baseline_std=1.0, source_id=info.slug, audio_z=m.z)
            opts = pick_render(style, base_index + i, self._rng)
            async with render_sem:
                try:
                    clip_media, seek = media, start
                    if sections:  # only these seconds of the video, at full quality
                        clip_media, seek = await vod.download_section(
                            self.s, url, start, start + length, self.s.vod_dir / "sections" / f"{name}.mp4",
                            live_status=live_status)
                    media_ref = {"kind": "file", "path": str(clip_media), "start": seek, "duration": length,
                                 "highlight": m.t - start}
                    return await self.render(ClipSource.from_file(clip_media, seek, length), name, workdir, event,
                                             highlight_at=m.t - start, origin=origin, opts=opts, media=media_ref)
                finally:
                    finished += 1
                    stage("rendering", total=len(moments), done=finished, skipped=skipped)
                    shutil.rmtree(workdir, ignore_errors=True)

        rendered = await asyncio.gather(*(clip(i, m) for i, m in enumerate(moments)), return_exceptions=True)
        out: list[Path] = []
        for r in rendered:
            if isinstance(r, BaseException):
                log.error("[%s] clip render failed: %s", info.slug, r)
            elif r is not None:
                out.append(r)
        return out

    # --------------------------------------------------------------- render
    def _hook_for(self, origin: ClipOrigin, opts: RenderOptions, transcript: str,
                  keyword_hits: dict[str, int], story: Story | None = None) -> str:
        if opts.hook_text:
            return opts.hook_text.strip()
        if story is not None:
            return story.hook
        return MetadataGenerator().make_hook(ClipContext(
            channel=origin.id, display_name=origin.name, source_url=origin.url,
            category=origin.category, transcript=transcript, keyword_hits=keyword_hits, game=origin.game))

    async def _story_frames(self, src: ClipSource, workdir: Path, highlight_at: float | None) -> list[Path]:
        """4 frames spanning the build-up and the payoff, for the vision model."""
        if not self.s.ollama_vision_model:
            return []
        try:
            center = highlight_at if highlight_at is not None else src.duration / 2
            start = max(0.0, center - 8)
            window = replace(src, seek=src.seek + start, duration=min(12.0, src.duration - start))
            return await self.editor.raw_frames(window, 4, workdir / "story_frames", width=768)
        except Exception:
            log.exception("story frames failed")
            return []

    async def _tune_layout(self, src: ClipSource, opts: RenderOptions, workdir: Path,
                           check_streamer: bool = True) -> RenderOptions:
        """Webcam layouts only work when the streamer is on camera, and a tiny webcam stretched to the
        full width is a blur. ``check_streamer``: no webcam region, or no face inside it in this clip
        → "Full screen". Always: a small webcam box is widened so it's upscaled ≤ 3× (sharp)."""
        if opts.layout not in ("cam_top", "cam_bottom"):
            return opts
        from clipper.config import Facecam
        from clipper.modules import facecam

        if opts.facecam is None:
            log.info("no webcam region set → Full screen layout")
            return opts.model_copy(update={"layout": "full"})
        box = (opts.facecam.x, opts.facecam.y, opts.facecam.w, opts.facecam.h)
        if check_streamer:
            try:
                frames = await self.editor.raw_frames(src, 5, workdir / "cam_check", width=1280)
                seen, checked = await asyncio.to_thread(facecam.streamer_visible, frames, box)
            except Exception:  # can't tell: keep the chosen layout
                log.exception("streamer check failed")
                seen, checked = 1, 1
            if checked and seen < max(1, round(checked * 0.4)):
                log.info("streamer not on camera (face in %d/%d frames) → Full screen layout", seen, checked)
                return opts.model_copy(update={"layout": "full"})
        src_w, src_h = await self.editor.source_size(src)
        if src_w:
            wide = facecam.widen_for_sharpness(box, src_w, src_h, self.editor.dims(opts.quality)[0])
            if wide != box:
                log.info("webcam box %.0f px wide: widened to %.0f px so it stays sharp", box[2] * src_w, wide[2] * src_w)
                return opts.model_copy(update={"facecam": Facecam(x=wide[0], y=wide[1], w=wide[2], h=wide[3])})
        return opts

    async def _effects(self, src: ClipSource, workdir: Path, opts: RenderOptions, story: Story | None,
                       highlight_at: float | None) -> tuple[Effects | None, list[Word]]:
        """Story edit for this clip: teaser, voiceover, zoom/flash/SFX, progress bar.

        Returns the effects and the voiceover's words (timed on the *clip* timeline) so the
        captions can show what the narrator says instead of the streamer underneath it."""
        if not (opts.effects or (opts.voiceover and story)):
            return None, []
        s = self.s
        teaser = None
        if opts.effects and s.teaser_s > 0 and highlight_at is not None:
            t_start = max(0.0, min(highlight_at - 0.4, src.duration - s.teaser_s))
            # never push a clip past the Reels (90 s) or Shorts (180 s) limit it fitted before
            fits = all(not (src.duration <= lim < src.duration + s.teaser_s + 0.5) for lim in (90.0, 180.0))
            if t_start >= s.teaser_s + 4 and fits:  # only tease a payoff that is a few seconds away
                teaser = (t_start, s.teaser_s)
        lead = teaser[1] if teaser else 0.0
        vo_path, vo_dur, vo_words = None, 0.0, []
        if opts.voiceover and story and story.voiceover:
            path = workdir / "voiceover.wav"
            dur = await synthesize(s, story.voiceover, path, opts.voice, expressive=opts.expressive)
            if dur and dur < max(7.0, src.duration * 0.4):
                vo_path, vo_dur = path, dur
                tokens = strip_tags(story.voiceover).split()
                step = dur / len(tokens)
                vo_words = [Word(text=tok, start=0.15 + i * step, end=0.15 + (i + 1) * step)
                            for i, tok in enumerate(tokens)]
        fx = Effects(teaser=teaser,
                     highlight=(highlight_at + lead) if (opts.effects and highlight_at is not None) else None,
                     voiceover=vo_path, vo_at=lead + 0.15, vo_dur=vo_dur,
                     sfx=await sfx(s) if opts.effects else {},
                     progress=opts.effects and s.progress_bar, zoom=opts.effects)
        return fx, vo_words

    async def _render_media(self, src: ClipSource, out_path: Path, workdir: Path, opts: RenderOptions,
                            hook: str, words: list[Word], highlight_at: float | None,
                            story: Story | None = None) -> Path | None:
        """Burn captions/hook per ``opts``, render the video and its thumbnail. Returns the thumbnail."""
        fx, vo_words = await self._effects(src, workdir, opts, story, highlight_at)
        if vo_words:  # narrator's words replace the streamer's while the voiceover plays
            vo_end = vo_words[-1].end + 0.2
            words = sorted([w for w in words if not (w.start < vo_end and w.end > 0.0)] + vo_words,
                           key=lambda w: w.start)
        caption_words = words if opts.captions else []
        hook_overlay = hook if (opts.hook and self.s.hook_overlay) else None
        # Stacked layouts: keep text off the streamer's face (hook goes to the far edge from the
        # webcam panel) and put "middle" captions on the seam between the two panels.
        hook_pos = "bottom" if opts.layout == "cam_top" else "top"
        seam = None
        if opts.layout in ("cam_top", "cam_bottom") and opts.facecam is not None:
            cam = VideoEditor.cam_panel_h(opts, 1920)  # captions are laid out on a 1080x1920 canvas
            seam = cam if opts.layout == "cam_top" else 1920 - cam
        ass_path = (write_ass(caption_words, self.s, workdir / "captions.ass", src.duration,
                              hook=hook_overlay, position=opts.caption_position, hook_position=hook_pos,
                              seam_y=seam, offset=fx.lead if fx else 0.0)
                    if (caption_words or hook_overlay) else None)
        await self.editor.render_vertical(src, out_path, ass_path, opts, fx)
        if not self.s.thumbnails:
            return None
        try:
            center = highlight_at if highlight_at is not None else min(self.s.clip_pre_roll_s, src.duration / 2)
            # Clean frames (no captions / hook bar) around the spike → one strong text on the thumbnail.
            frames = await self.editor.layout_frames(src, center - 6.0, 9.0, workdir / "thumb_frames", opts=opts)
            return ThumbnailMaker(self.s).from_frames(frames, hook, thumb_of(out_path),
                                                      text_at="bottom" if opts.layout == "cam_top" else "top")
        except Exception:  # never lose the clip over a thumbnail
            log.exception("thumbnail generation failed")
            return None

    async def render(self, src: ClipSource, name: str, workdir: Path, event: ClipEvent | None,
                     highlight_at: float | None = None, origin: ClipOrigin | None = None,
                     opts: RenderOptions | None = None, media: dict[str, Any] | None = None) -> Path:
        """Transcribe, hook, render, thumbnail and register one clip (queued if auto-post applies)."""
        if origin is None:
            configured = self.s.all_sources()
            origin = (ClipOrigin.from_source(configured[0]) if configured else
                      ClipOrigin(id="local", url="", name=self.s.my_channel_name or "Clipper", category=None,
                                 publishable=False))
        opts = opts or origin.style.render
        words: list[Word] = []
        try:
            audio = await self.editor.extract_audio(src, workdir / "audio.wav")
            words = await self.transcriber.transcribe(audio)
        except Exception:  # captions are an enhancement — never lose the clip over them
            log.exception("transcription failed; rendering without captions")

        transcript = words_to_text(words)
        keyword_hits = event.keyword_hits if event else {}
        story = None
        if opts.story:
            story = await write_story(self.s, words=words, highlight_at=highlight_at, streamer=origin.name,
                                      game=origin.game, source_title=origin.title, category=origin.category,
                                      frames=await self._story_frames(src, workdir, highlight_at))
        hook = self._hook_for(origin, opts, transcript, keyword_hits, story)
        created = time.time()
        # one folder per clip: Clips/<source>/<date time hook>/video.mp4 (+ thumbnail, transcript, notes)
        folder = unique_dir(self.s.clips_dir / safe_name(origin.name or origin.id), clip_dir_name(created, hook))
        folder.mkdir(parents=True)
        out_path = folder / VIDEO
        try:
            opts = await self._tune_layout(src, opts, workdir)
            thumb = await self._render_media(src, out_path, workdir, opts, hook, words, highlight_at, story)
            transcript_of(out_path).write_text(transcript + "\n", encoding="utf-8")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)  # no half-made clip folders
            raise
        log.info("[%s] hook: %s | layout %s | thumbnail: %s | %s", origin.id, hook, opts.layout,
                 "yes" if thumb else "none", folder)
        if self.queue is not None:
            auto = origin.publishable and origin.style.auto_post
            platforms = self._targets(origin.style.platforms) if auto else []
            action = await self._action_for(out_path, transcript, highlight_at, opts.layout)
            source = {"id": origin.id, "url": origin.url, "name": origin.name, "category": origin.category or "",
                      "game": origin.game or "", "action": action or ""}
            duration = await ffprobe_duration(self.s.ffprobe_bin, out_path)
            clip_id = await self.queue.add_clip(
                out_path, score=event.score if event else 0.0, duration=duration,
                transcript=transcript, keyword_hits=keyword_hits, platforms=platforms, hook=hook,
                source=source, render_opts=opts.model_dump_json(), media=json.dumps(media or {}),
                clip_key=name, created=created, story=story.to_json() if story else "",
            )
            row = await self.queue.get_clip(clip_id)
            if row is not None:
                await asyncio.to_thread(write_sidecars, out_path, row)
            if platforms:
                log.info("[%s] clip %d queued for %s", origin.id, clip_id, ", ".join(platforms))
                if self.on_new_clip:
                    self.on_new_clip()
            else:
                why = "auto-post off" if origin.publishable else "source not permission_confirmed"
                log.info("[%s] clip %d saved to library (%s)", origin.id, clip_id, why)
        return out_path

    async def _action_for(self, video: Path, transcript: str, highlight_at: float | None, layout: str) -> str | None:
        """What happens in the clip (racing, police chase, fight…) for the title; None if unclear."""
        from clipper.modules import actions

        try:
            return await asyncio.to_thread(actions.detect, self.s.ffmpeg_bin, video, transcript, highlight_at, layout)
        except Exception:
            log.exception("action detection failed")
            return None

    def _targets(self, chosen: list[str] | None) -> list[str]:
        """Accounts a new clip posts to: the style's choice, else the default accounts (existing ones only)."""
        if self.accounts is None:
            return list(chosen or self.s.publish_platforms)
        keys = chosen if chosen else self.accounts.defaults()
        return [k for k in keys if self.accounts.get(k) is not None]

    async def rerender(self, clip_id: int, opts: RenderOptions, *, shift: float = 0.0,
                       length: float | None = None) -> Path:
        """Re-cut and/or restyle an existing clip in place (same file, same queue entry)."""
        if self.queue is None:
            raise RuntimeError("no queue")
        row = await self.queue.get_clip(clip_id)
        if row is None:
            raise KeyError(clip_id)
        media = json.loads(row.get("media") or "{}")
        if not media:
            raise ValueError("this clip was made before re-editing was supported; it can't be re-cut")
        out_path = Path(row["path"])
        workdir = self.s.work_dir / f"rerender_{clip_id}_{int(time.time())}"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            src = source_from_media(self.s, media, workdir, shift, length)
            words: list[Word] = []
            try:
                words = await self.transcriber.transcribe(await self.editor.extract_audio(src, workdir / "audio.wav"))
            except Exception:
                log.exception("transcription failed during re-render")
            transcript = words_to_text(words) or row.get("transcript", "")
            story = Story.from_json(row.get("story"))
            if story is None and opts.story and words:
                story = await write_story(self.s, words=words, highlight_at=None, streamer=row.get("display_name"),
                                          game=row.get("game") or None, source_title=None,
                                          category=row.get("category") or None)
            hook = opts.hook_text.strip() if opts.hook_text else (row.get("hook") or (story.hook if story else ""))
            highlight = max(0.0, min(src.duration, float(media.get("highlight", src.duration / 2)) - shift))
            await self._render_media(src, out_path, workdir, await self._tune_layout(src, opts, workdir, False),
                                     hook, words, highlight, story if opts.story else None)
            new_media = {**media, "start": float(media["start"]) + shift, "duration": src.duration,
                         "highlight": highlight}
            await self.queue.update_clip(clip_id, duration=await ffprobe_duration(self.s.ffprobe_bin, out_path),
                                         transcript=transcript, hook=hook, render_opts=opts.model_dump_json(),
                                         media=json.dumps(new_media), story=story.to_json() if story else "",
                                         action=await self._action_for(out_path, transcript, highlight, opts.layout) or "")
            transcript_of(out_path).write_text(transcript + "\n", encoding="utf-8")
            row = await self.queue.get_clip(clip_id)
            if row is not None:
                await asyncio.to_thread(write_sidecars, out_path, row)
            log.info("clip %d re-rendered (%s, %.0fs, captions %s)", clip_id, opts.layout, src.duration,
                     "on" if opts.captions else "off")
            return out_path
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def preview(self, opts: RenderOptions, out: Path, *, clip_id: int | None = None,
                      recorder: StreamRecorder | None = None) -> Path:
        """One composed frame for checking a layout/webcam region before rendering."""
        workdir = self.s.work_dir / f"preview_{int(time.time() * 1000)}"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            if clip_id is not None:
                row = await self.queue.get_clip(clip_id) if self.queue else None
                media = json.loads((row or {}).get("media") or "{}")
                if not media:
                    raise ValueError("no source media stored for this clip")
                src = source_from_media(self.s, media, workdir)
                at = float(media.get("highlight", src.duration / 2))
            elif recorder is not None:
                segs = recorder.recent_segments(max_age_s=self.s.raw_retention_hours * 3600)
                if not segs:
                    raise ValueError("nothing recorded from this stream yet; preview works once it has been live")
                seg = segs[-2] if len(segs) > 1 else segs[-1]  # a finished segment if possible
                src = ClipSource.from_file(seg.path, 0, max(1.0, seg.end - seg.start))
                at = min(20.0, src.duration / 2)
            else:
                raise ValueError("nothing to preview")
            return await self.editor.preview_frame(src, at, out, opts)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def sample_frames(self, out_dir: Path, *, n: int = 6, clip_id: int | None = None,
                            recorder: StreamRecorder | None = None) -> list[Path]:
        """Raw 16:9 frames from a clip's source or a stream's recent recording."""
        workdir = self.s.work_dir / f"frames_{int(time.time() * 1000)}"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            if clip_id is not None:
                row = await self.queue.get_clip(clip_id) if self.queue else None
                media = json.loads((row or {}).get("media") or "{}")
                if not media:
                    raise ValueError("no source media stored for this clip")
                src = source_from_media(self.s, media, workdir)
            elif recorder is not None:
                segs = recorder.recent_segments(max_age_s=self.s.raw_retention_hours * 3600)
                if len(segs) > 1:
                    segs = segs[:-1]  # skip the segment still being written
                segs = segs[-8:]
                if not segs:
                    raise ValueError("nothing recorded from this stream yet")
                src = ClipSource.from_segments(segs, segs[0].start, max(1.0, segs[-1].end - segs[0].start), workdir)
            else:
                raise ValueError("nothing to sample")
            return await self.editor.raw_frames(src, n, out_dir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def janitor(self) -> None:
        """Delete clips past retention (0 = keep forever); never ones still waiting to post."""
        import sqlite3

        from clipper.modules.library import delete_clip_files

        while True:
            if self.s.rendered_retention_days > 0:
                cutoff = time.time() - self.s.rendered_retention_days * 86_400
                try:
                    with sqlite3.connect(self.s.state_dir / "publisher.db") as db:
                        rows = db.execute(
                            "SELECT c.path FROM clips c WHERE c.created_at < ? AND NOT EXISTS (SELECT 1 FROM jobs j "
                            "WHERE j.clip_id=c.id AND j.status IN ('pending','scheduled','uploading'))",
                            (cutoff,)).fetchall()
                    roots = [self.s.library_root, self.s.default_library, self.s.rendered_dir]
                    for (path,) in rows:
                        video = Path(path)
                        if video.is_file():
                            await asyncio.to_thread(delete_clip_files, video, roots)
                except (sqlite3.Error, OSError) as exc:
                    log.warning("clip janitor: %s", exc)
            await asyncio.sleep(3600)
