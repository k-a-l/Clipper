"""Module 3 — FFmpeg clip extraction and 16:9 → 9:16 vertical formatting.

Layout (1080x1920)::

    ┌──────────────┐
    │  blurred bg  │  ← source scaled to *fill* 1080x1920, cropped, boxblur=25:5
    │┌────────────┐│
    ││ gameplay   ││  ← centre crop (default 4:3) scaled to 1080 wide, centred
    │└────────────┘│
    │  CAPTIONS    │  ← kinetic ASS subtitles burned in (lower third)
    └──────────────┘

FFmpeg commands are built with **typed-ffmpeg** (third-party; see
THIRD_PARTY_COMPONENTS.md), which validates filter/option names against FFmpeg's
schema and handles filter-graph escaping. The compiled argv is executed by our
own async runner so timeouts and task cancellation always kill the child.

Hardware encoders (NVENC → QSV → VideoToolbox) are probed once with a tiny test
encode and used when they actually work, otherwise ``libx264 -preset fast -crf 22``.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import ffmpeg  # typed-ffmpeg (import name "ffmpeg")

from clipper.config import RenderOptions, RenderQuality, Settings
from clipper.modules.stream_recorder import Segment
from clipper.modules.utils import CommandError, ffprobe_duration, run_cmd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClipSource:
    """Where a clip comes from: a media file (or concat listing) plus trim window.

    Seeking is input-side (``-ss`` before ``-i``). When transcoding, FFmpeg's
    default accurate seek decodes from the previous keyframe, drops frames up to
    the exact position and starts output timestamps at 0 — which keeps burned-in
    subtitle timing (relative to clip start) correct.
    """

    path: Path
    seek: float
    duration: float
    probe_path: Path  # a real media file for stream inspection
    concat: bool = False
    description: str = field(default="", compare=False)

    @classmethod
    def from_segments(
        cls, segments: list[Segment], window_start: float, duration: float, workdir: Path
    ) -> ClipSource:
        """Stitch recorded MPEG-TS segments via the concat demuxer."""
        if not segments:
            raise ValueError("no segments")
        listing = workdir / "segments.txt"
        listing.write_text(
            "".join(f"file '{seg.path.resolve().as_posix()}'\n" for seg in segments),
            encoding="utf-8",
        )
        offset = max(0.0, window_start - segments[0].start)
        return cls(
            path=listing.resolve(), seek=offset, duration=duration,
            probe_path=segments[0].path.resolve(), concat=True,
            description=f"{len(segments)} segment(s) @ +{offset:.1f}s",
        )

    @classmethod
    def from_file(cls, path: Path, start: float, duration: float) -> ClipSource:
        """Clip directly from a single local media file (manual mode / tests)."""
        path = path.resolve()
        return cls(path=path, seek=max(0.0, start), duration=duration, probe_path=path,
                   description=f"{path.name} @ {start:.1f}s")

    def open(self) -> ffmpeg.AVStream:
        """Return the typed-ffmpeg input node for this source."""
        if self.concat:
            return ffmpeg.input(self.path, f="concat", ss=self.seek, t=self.duration,
                                extra_options={"safe": 0})
        return ffmpeg.input(self.path, ss=self.seek, t=self.duration)


@dataclass(frozen=True)
class Effects:
    """The "story" edit on top of the layout (all times are on the *output* timeline).

    * ``teaser``: (start inside the clip, length): the payoff shown first as a flash-forward,
      then the clip plays from the beginning. Output = teaser + clip.
    * ``highlight``: when the payoff lands → punch-in zoom, white flash, impact sound.
    * ``voiceover`` / ``vo_at``: spoken intro mixed over the clip; the original audio is
      ducked to ~25 % underneath it.
    * ``sfx``: {"whoosh": wav, "hit": wav} (modules.voiceover.sfx).
    * ``progress``: thin bar along the top filling over the video (a retention cue).
    """

    teaser: tuple[float, float] | None = None
    highlight: float | None = None
    voiceover: Path | None = None
    vo_at: float = 0.0
    vo_dur: float = 0.0
    sfx: dict[str, Path] = field(default_factory=dict)
    progress: bool = True
    zoom: bool = True

    @property
    def lead(self) -> float:
        """Seconds added before the clip (captions and highlight shift by this)."""
        return self.teaser[1] if self.teaser else 0.0


class VideoEditor:
    """FFmpeg-based clip extraction and vertical rendering."""

    _CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_videotoolbox")

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._encoder: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ exec
    def _argv(self, node: Any) -> list[str]:
        return node.global_args(hide_banner=True, loglevel="error", y=True).compile(cmd=self.s.ffmpeg_bin)

    async def _run(self, node: Any, *, timeout: float, cwd: Path | None = None) -> None:
        await run_cmd(self._argv(node), timeout=timeout, cwd=cwd)

    # -------------------------------------------------------------- encoders
    async def encoder(self) -> str:
        """Return the encoder to use, probing hardware encoders once."""
        async with self._lock:
            if self._encoder is None:
                self._encoder = await self._detect_encoder()
                log.info("video encoder: %s", self._encoder)
            return self._encoder

    async def _detect_encoder(self) -> str:
        if self.s.video_encoder != "auto":
            return self.s.video_encoder
        for name in self._CANDIDATES:
            if name == "h264_videotoolbox" and sys.platform != "darwin":
                continue
            test = ffmpeg.output(
                ffmpeg.sources.testsrc2(size="1280x720", rate="30", duration=0.5),
                filename="-", f="null", extra_options={"c:v": name},
            )
            try:
                await self._run(test, timeout=30)
                return name
            except (CommandError, asyncio.TimeoutError, OSError):
                log.debug("encoder %s unavailable", name)
        return "libx264"

    def _encoder_options(self, name: str, quality: RenderQuality = "standard") -> dict[str, Any]:
        """Encoder settings; ``high``/``2k`` trade file size for detail (archive-grade masters)."""
        crf = self.s.x264_crf if quality == "standard" else min(self.s.x264_crf, 18)
        vt_rate = {"standard": ("16M", "24M"), "high": ("24M", "32M"), "2k": ("36M", "48M")}[quality]
        match name:
            case "h264_nvenc":
                return {"c:v": name, "preset": "p5", "rc": "vbr", "cq": crf, "b:v": 0, "pix_fmt": "yuv420p"}
            case "h264_qsv":
                return {"c:v": name, "preset": "medium", "global_quality": crf, "pix_fmt": "nv12"}
            case "h264_videotoolbox":
                return {"c:v": name, "b:v": vt_rate[0], "maxrate": vt_rate[1], "pix_fmt": "yuv420p"}
            case _:
                preset = self.s.x264_preset if quality == "standard" else "medium"
                return {"c:v": "libx264", "preset": preset, "crf": crf, "pix_fmt": "yuv420p"}

    # ---------------------------------------------------------------- graph
    async def has_audio(self, src: ClipSource) -> bool:
        try:
            info = await asyncio.to_thread(ffmpeg.probe, src.probe_path, cmd=self.s.ffprobe_bin, timeout=30)
        except (ffmpeg.FFMpegExecuteError, subprocess.TimeoutExpired, OSError, ValueError):
            return True  # let ffmpeg decide; worst case the render fails loudly
        return any(st.get("codec_type") == "audio" for st in info.get("streams", []))

    async def mean_volume(self, src: ClipSource) -> float | None:
        """Mean loudness (dB) of the clip window's audio; None if there is no audio at all."""
        args = [self.s.ffmpeg_bin, "-hide_banner", "-nostats"]
        if src.concat:
            args += ["-f", "concat", "-safe", "0"]
        args += ["-ss", f"{src.seek:.3f}", "-t", f"{src.duration:.3f}", "-i", str(src.path),
                 "-vn", "-af", "volumedetect", "-f", "null", "-"]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (OSError, asyncio.TimeoutError):
            return None
        import re

        m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err.decode(errors="replace"))
        if not m:
            return None
        return -200.0 if m.group(1) == "-inf" else float(m.group(1))

    async def source_size(self, src: ClipSource) -> tuple[int, int]:
        """Pixel (width, height) of the source video ((0, 0) if unknown)."""
        try:
            info = await asyncio.to_thread(ffmpeg.probe, src.probe_path, cmd=self.s.ffprobe_bin, timeout=30)
        except (ffmpeg.FFMpegExecuteError, subprocess.TimeoutExpired, OSError, ValueError):
            return 0, 0
        vs = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), {})
        return int(vs.get("width") or 0), int(vs.get("height") or 0)

    @staticmethod
    def cam_panel_h(opts: RenderOptions, h: int) -> int:
        """Height of the webcam panel: the webcam's own shape (a 16:9 webcam → a 16:9 panel, so no game
        spills into it and nothing is cropped off), kept between 30 % and 42 % of the frame."""
        fc = opts.facecam
        frac = 0.42
        if fc is not None and fc.h > 0:
            aspect = (fc.w * 16) / (fc.h * 9)  # pixel aspect of the webcam box in a 16:9 source
            frac = min(0.42, max(0.30, (h * 9 / 16) / aspect / h))
        return int(round(h * frac / 2) * 2)

    @staticmethod
    def dims(quality: RenderQuality) -> tuple[int, int]:
        return (1440, 2560) if quality == "2k" else (1080, 1920)

    async def content_box(self, src: ClipSource) -> tuple[int, int, int, int] | None:
        """Black bars baked into the stream (a 16:9 picture letterboxed inside 16:9, a 4:3 game…):
        FFmpeg ``cropdetect`` over the clip → (w, h, x, y) of the real picture, or None if no bars."""
        args = [self.s.ffmpeg_bin, "-hide_banner", "-nostats"]
        if src.concat:
            args += ["-f", "concat", "-safe", "0"]
        args += ["-ss", f"{src.seek:.3f}", "-t", f"{min(src.duration, 30):.3f}", "-i", str(src.path),
                 "-vf", "fps=2,cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except (OSError, asyncio.TimeoutError):
            return None
        import re

        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err.decode(errors="replace"))
        if not found:
            return None
        w, h, x, y = map(int, found[-1])  # reset=0: the box grows to cover everything that was ever lit
        info = await asyncio.to_thread(ffmpeg.probe, src.probe_path, cmd=self.s.ffprobe_bin, timeout=30)
        vs: dict[str, Any] = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        fw, fh = int(vs.get("width") or 0), int(vs.get("height") or 0)
        if not fw or not fh or w < fw * 0.4 or h < fh * 0.4:
            return None  # mostly black (fade, loading screen): don't trust it
        if w >= fw * 0.96 and h >= fh * 0.96:
            return None  # no real bars
        return (w, h, x, y)

    def _layout(self, inp: Any, opts: RenderOptions | None = None,
                box: tuple[int, int, int, int] | None = None) -> Any:
        """Compose the 9:16 frame for ``opts.layout`` (no text overlays).

        * center: blurred fill + centre crop (``foreground_aspect``, default 4:3)
        * fit: blurred fill + the whole 16:9 frame
        * full: centre 9:16 crop filling the screen
        * cam_top / cam_bottom: webcam panel (``opts.facecam``) stacked with a gameplay panel
        """
        opts = opts or RenderOptions()
        w, h = self.dims(opts.quality)
        v = inp.video.setpts(expr="PTS-STARTPTS")
        layout = opts.layout
        if box is not None and layout not in ("cam_top", "cam_bottom"):  # facecam boxes use full-frame coords
            v = v.crop(out_w=str(box[0]), out_h=str(box[1]), x=str(box[2]), y=str(box[3]))
        if layout in ("cam_top", "cam_bottom") and opts.facecam is None:
            log.warning("layout %s needs a facecam region; using full screen", layout)
            layout = "full"

        if layout == "full":
            # a 1080p source cropped to 9:16 is ~608 px wide → 2.4× upscale: sharpen what lanczos softens
            return (v.crop(out_w="min(iw,ih*9/16)", out_h="min(ih,iw*16/9)")
                    .scale(w=str(w), h=str(h), flags="lanczos")
                    .unsharp(luma_msize_x=5, luma_msize_y=5, luma_amount=0.6).setsar(sar="1"))

        if layout in ("cam_top", "cam_bottom"):
            fc = opts.facecam
            assert fc is not None
            cam_h = self.cam_panel_h(opts, h)
            game_h = h - cam_h
            parts = v.split(outputs=2)
            cam = (parts.video(0)
                   .crop(out_w=f"iw*{fc.w:.4f}", out_h=f"ih*{fc.h:.4f}", x=f"iw*{fc.x:.4f}", y=f"ih*{fc.y:.4f}")
                   .scale(w=str(w), h=str(cam_h), force_original_aspect_ratio="increase", flags="lanczos")
                   .crop(out_w=str(w), out_h=str(cam_h))
                   # webcams are the most upscaled part (and faces show softness first)
                   .unsharp(luma_msize_x=5, luma_msize_y=5, luma_amount=0.7))
            game_ar = w / game_h
            game = (parts.video(1)
                    .crop(out_w=f"min(iw,ih*{game_ar:.5f})", out_h=f"min(ih,iw/{game_ar:.5f})")
                    .scale(w=str(w), h=str(game_h), flags="lanczos")
                    .unsharp(luma_msize_x=5, luma_msize_y=5, luma_amount=0.4))
            top, bottom = (cam, game) if layout == "cam_top" else (game, cam)
            return ffmpeg.filters.vstack(top, bottom, inputs=2).setsar(sar="1")

        luma_radius, _, luma_power = self.s.background_blur.partition(":")
        parts = v.split(outputs=2)
        bg = (parts.video(0)
              .scale(w=str(w), h=str(h), force_original_aspect_ratio="increase")
              .crop(out_w=str(w), out_h=str(h))
              .boxblur(luma_radius=luma_radius, luma_power=int(luma_power or 2))
              .eq(brightness=-0.14, saturation=0.85))  # a quiet backdrop, so the sharp gameplay stands out
        fg_src = parts.video(1)
        if layout == "center":
            fg_src = fg_src.crop(out_w=f"min(iw,ih*{self.s.foreground_aspect:.6f})", out_h="ih")
        fg = (fg_src.scale(w=str(w), h="-2", flags="lanczos")
              .unsharp(luma_msize_x=5, luma_msize_y=5, luma_amount=0.45))  # recover detail lost to upscaling
        return bg.overlay(fg, x="(W-w)/2", y="(H-h)/2").setsar(sar="1")

    def build_render(self, src: ClipSource, out_path: Path, ass_filename: str | None,
                     encoder: str, with_audio: bool, opts: RenderOptions | None = None,
                     box: tuple[int, int, int, int] | None = None, fx: Effects | None = None) -> Any:
        """Layout (+ story effects) + optional FPS + burned captions/hook, encoded to MP4."""
        opts = opts or RenderOptions()
        w, h = self.dims(opts.quality)
        inp = src.open()
        video = self._layout(inp, opts, box)
        audio = inp.audio.asetpts(expr="PTS-STARTPTS") if with_audio else None
        total = src.duration
        if fx and fx.teaser:
            t_start, t_len = fx.teaser
            t_inp = replace(src, seek=src.seek + t_start, duration=t_len).open()
            t_video = self._layout(t_inp, opts, box).eq(saturation=1.25, contrast=1.06)
            if self.s.output_fps:
                t_video, video = t_video.fps(fps=str(self.s.output_fps)), video.fps(fps=str(self.s.output_fps))
            else:  # concat needs matching frame rates: take the source rate for both
                t_video, video = t_video.fps(fps="source_fps"), video.fps(fps="source_fps")
            if audio is not None:
                node = ffmpeg.filters.concat(t_video, t_inp.audio.asetpts(expr="PTS-STARTPTS"), video, audio,
                                             n=2, v=1, a=1)
                video, audio = node.video(0), node.audio(0)
            else:
                video = ffmpeg.filters.concat(t_video, video, n=2, v=1, a=0).video(0)
            total += t_len
        elif self.s.output_fps:
            video = video.fps(fps=str(self.s.output_fps))

        if fx and fx.zoom:
            lead = fx.lead
            hl = fx.highlight
            z = "1"
            if hl is not None:
                z = f"if(between(t,{hl:.3f},{hl + 1.3:.3f}),1.14,{z})"
            if lead:
                z = f"if(lt(t,{lead:.3f}),1.16-0.1*t/{lead:.3f},{z})"
            video = (video.scale(w=f"trunc(iw*({z})/2)*2", h=f"trunc(ih*({z})/2)*2", eval="frame", flags="lanczos")
                     .crop(out_w=str(w), out_h=str(h), x="(iw-ow)/2", y="(ih-oh)/2"))
            if hl is not None:
                video = video.drawbox(x="0", y="0", width="iw", height="ih", color="white@0.45",
                                      thickness="fill", enable=f"between(t,{hl:.3f},{hl + 0.07:.3f})")
            if lead:
                video = video.drawbox(x="0", y="0", width="iw", height="ih", color="white@0.6",
                                      thickness="fill", enable=f"between(t,{lead:.3f},{lead + 0.05:.3f})")
        if ass_filename:
            # Relative to ffmpeg's cwd → no path escaping inside the filter graph.
            fontsdir = self.s.fonts_dir.resolve()
            video = video.ass(filename=ass_filename, fontsdir=str(fontsdir)) if fontsdir.is_dir() \
                else video.ass(filename=ass_filename)
        if fx and fx.progress:
            bar_h = max(8, h // 200)
            bar = ffmpeg.sources.color(color="0xFFD400", size=f"{w}x{bar_h}", rate=30, duration=total + 1)
            video = video.overlay(bar, x=f"-W+W*t/{total:.3f}", y="0", shortest=True)
        video = video.setsar(sar="1")

        streams: list[Any] = [video]
        options: dict[str, Any] = {**self._encoder_options(encoder, opts.quality), "movflags": "+faststart"}
        if audio is not None:
            if self.s.loudnorm:
                audio = audio.loudnorm(I=-14, TP=-1.5, LRA=11).aresample(sample_rate=48000)
            if fx and (fx.voiceover or fx.sfx):
                mix: list[Any] = []
                if fx.voiceover and fx.vo_dur > 0:
                    a, b = fx.vo_at - 0.1, fx.vo_at + fx.vo_dur + 0.15
                    audio = audio.volume(volume=f"if(between(t,{a:.3f},{b:.3f}),0.25,1)", eval="frame")
                    ms = int(fx.vo_at * 1000)
                    mix.append(ffmpeg.input(fx.voiceover).audio.aresample(sample_rate=48000)
                               .adelay(delays=f"{ms}|{ms}", all=True).volume(volume="1.7"))
                if (whoosh := fx.sfx.get("whoosh")) and fx.lead:
                    ms = int(max(0.0, fx.lead - 0.3) * 1000)
                    mix.append(ffmpeg.input(whoosh).audio.adelay(delays=f"{ms}|{ms}", all=True).volume(volume="0.6"))
                if (hit := fx.sfx.get("hit")) and fx.highlight is not None:
                    ms = int(fx.highlight * 1000)
                    mix.append(ffmpeg.input(hit).audio.adelay(delays=f"{ms}|{ms}", all=True).volume(volume="0.8"))
                if mix:
                    audio = ffmpeg.filters.amix(audio, *mix, duration="first", normalize=False)
                    audio = audio.alimiter(limit=0.95)
            streams.append(audio)
            options |= {"c:a": "aac", "b:a": "192k" if opts.quality != "standard" else "160k", "ar": 48000, "ac": 2}
        return ffmpeg.output(*streams, filename=out_path.resolve(), t=total, extra_options=options)

    async def layout_frames(self, src: ClipSource, start: float, span: float, out_dir: Path,
                            fps: float = 2.0, opts: RenderOptions | None = None) -> list[Path]:
        """Clean 9:16 frames (no captions/hook) from ``[start, start+span]`` of the clip, for thumbnails."""
        start = max(0.0, min(start, src.duration - 1))
        window = replace(src, seek=src.seek + start, duration=min(span, src.duration - start))
        out_dir.mkdir(parents=True, exist_ok=True)
        box = await self.content_box(window)
        node = ffmpeg.output(self._layout(window.open(), opts, box).fps(fps=str(fps)),
                             filename=out_dir / "c_%03d.jpg", extra_options={"q:v": 2})
        await self._run(node, timeout=180)
        return sorted(out_dir.glob("c_*.jpg"))

    async def raw_frames(self, src: ClipSource, n: int, out_dir: Path, width: int = 1280) -> list[Path]:
        """``n`` evenly spaced, unmodified 16:9 frames (for the webcam picker / auto-detect)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        rate = max(n / max(src.duration, 1.0), 0.02)
        node = ffmpeg.output(src.open().video.fps(fps=f"{rate:.4f}").scale(w=str(width), h="-2"),
                             filename=out_dir / "f_%02d.jpg", extra_options={"frames:v": n, "q:v": 3})
        await self._run(node, timeout=120)
        return sorted(out_dir.glob("f_*.jpg"))

    async def preview_frame(self, src: ClipSource, at: float, out: Path, opts: RenderOptions) -> Path:
        """One composed frame at ``at`` seconds: lets the operator check a layout/facecam before rendering."""
        window = replace(src, seek=src.seek + max(0.0, min(at, src.duration - 0.5)), duration=1.0)
        box = await self.content_box(replace(src, seek=src.seek + max(0.0, at - 5), duration=10.0))
        node = ffmpeg.output(self._layout(window.open(), opts, box), filename=out.resolve(),
                             extra_options={"frames:v": 1, "q:v": 3})
        await self._run(node, timeout=60)
        return out

    # ---------------------------------------------------------------- render
    async def extract_audio(self, src: ClipSource, out_wav: Path) -> Path:
        """Extract 16 kHz mono PCM for Whisper."""
        node = ffmpeg.output(src.open().audio, filename=out_wav.resolve(),
                             ac=1, ar=16000, acodec="pcm_s16le")
        await self._run(node, timeout=180)
        return out_wav

    async def render_vertical(self, src: ClipSource, out_path: Path, ass_path: Path | None,
                              opts: RenderOptions | None = None, fx: Effects | None = None) -> Path:
        """Render the final 9:16 H.264/AAC MP4 (layout/quality from ``opts``) with captions burned in.

        Renders to a temporary file and swaps it in, so a re-render never leaves a half-written clip.
        """
        opts = opts or RenderOptions()
        tmp = out_path.with_name(f".{out_path.stem}.rendering.mp4")
        encoder = await self.encoder()
        with_audio = await self.has_audio(src)
        box = await self.content_box(src)
        if box:
            log.info("cropping baked-in black bars: picture is %dx%d at %d,%d", *box)
        cwd = ass_path.parent if ass_path else out_path.parent
        ass_name = ass_path.name if ass_path else None
        try:
            await self._run(self.build_render(src, tmp, ass_name, encoder, with_audio, opts, box, fx),
                            timeout=1800, cwd=cwd)
        except CommandError as exc:
            if encoder == "libx264":
                raise
            log.warning("%s render failed (%s); falling back to libx264", encoder, exc)
            self._encoder = "libx264"
            await self._run(self.build_render(src, tmp, ass_name, "libx264", with_audio, opts, box, fx),
                            timeout=3600, cwd=cwd)

        duration = await ffprobe_duration(self.s.ffprobe_bin, tmp)
        if duration is None or duration < 1:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"render produced an invalid file: {out_path}")
        if with_audio and fx is not None:
            # Never ship a silent clip: if the source has sound but the story mix came out silent,
            # render again with the plain original audio.
            src_db, out_db = await self.mean_volume(src), await self.mean_volume(ClipSource.from_file(tmp, 0, duration))
            if src_db is not None and src_db > -50 and (out_db is None or out_db < -60):
                log.warning("render came out silent (source %.0f dB, clip %s dB): re-rendering with the original audio",
                            src_db, "none" if out_db is None else f"{out_db:.0f}")
                await self._run(self.build_render(src, tmp, ass_name, self._encoder or "libx264", with_audio, opts, box,
                                                  None), timeout=1800, cwd=cwd)
        tmp.replace(out_path)
        log.info("rendered %s (%.1fs, %s, %s/%s)", out_path.name, duration, src.description, opts.layout, opts.quality)
        return out_path
