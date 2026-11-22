"""Thumbnail generation: pick the best frame near the highlight, add a bold hook.

Pipeline
--------
1. Extract candidate frames (2 fps) from the rendered 9:16 clip in a window
   around the chat-spike moment, which is the most representative part of the clip.
2. Score each frame with cheap image statistics (numpy):
   * **sharpness**: variance of a Laplacian (motion-blurred frames score low);
   * **exposure**: penalise frames that are too dark or blown out;
   * **colourfulness**: Hasler & Süsstrunk (2003) metric; vivid frames stand out
     in a feed.
3. Compose the winner with Pillow: mild contrast/saturation boost, a dark
   gradient behind the text area, and the hook in large outlined uppercase type.

The text is the same hook shown on the video and used in the title, so the
thumbnail never promises something the clip doesn't contain (YouTube penalises
misleading thumbnails).
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from clipper.config import Settings
from clipper.modules.metadata import strip_emoji
from clipper.modules.utils import run_cmd

log = logging.getLogger(__name__)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",      # Debian/Docker (fonts-dejavu-core)
    "C:/Windows/Fonts/impact.ttf",                                 # Windows
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",               # macOS
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
MAX_BYTES = 2 * 1024 * 1024  # YouTube custom-thumbnail limit


@dataclass(frozen=True)
class FrameScore:
    path: Path
    sharpness: float
    brightness: float
    colorfulness: float

    @property
    def score(self) -> float:
        exposure = 1.0 - min(1.0, abs(self.brightness - 0.5) / 0.4)  # 1 at mid-grey, 0 at extremes
        return np.log1p(self.sharpness) * 1.0 + self.colorfulness / 40.0 + exposure * 2.0


def score_frame(path: Path) -> FrameScore:
    """Compute sharpness / brightness / colourfulness for one image."""
    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB").resize((270, 480)), dtype=np.float32)
    gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    lap = (-4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:])
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colorfulness = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
    return FrameScore(path, float(lap.var()), float(gray.mean() / 255.0), colorfulness)


def find_font(preferred: str | None = None) -> str:
    for candidate in (preferred, *_FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("no bold TTF font found; set THUMBNAIL_FONT_PATH")


def compose(frame: Path, hook: str, out: Path, font_path: str, size: tuple[int, int] = (1080, 1920),
            text_at: str = "top") -> Path:
    """Render the final thumbnail JPEG (≤ 2 MB)."""
    with Image.open(frame) as src:
        img = src.convert("RGB").resize(size)
    # Auto-exposure: lift dark frames (night scenes) toward mid-grey, capped to avoid noise blow-up.
    mean = float(np.asarray(img.convert("L"), dtype=np.float32).mean()) / 255.0
    if mean < 0.35:
        img = ImageEnhance.Brightness(img).enhance(min(2.2, 0.42 / max(mean, 0.05)))
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.25)
    w, h = img.size

    # Dark vertical gradient behind the top text block for legibility.
    band_h = int(h * 0.36)
    alpha = np.linspace(200, 0, band_h, dtype=np.float32).astype(np.uint8)
    if text_at == "bottom":
        alpha = alpha[::-1]
    gradient = Image.fromarray(np.repeat(alpha[:, None], w, axis=1), mode="L")
    overlay = Image.new("RGB", (w, band_h), (0, 0, 0))
    img.paste(overlay, (0, h - band_h if text_at == "bottom" else 0), gradient)

    text = strip_emoji(hook).upper().strip()
    draw = ImageDraw.Draw(img)
    font_size = 132
    while True:  # shrink until the wrapped block fits ~88% width and 3 lines
        font = ImageFont.truetype(font_path, font_size)
        avg_char = font.getlength("M") * 0.62
        lines = textwrap.wrap(text, width=max(6, int(w * 0.88 / avg_char)))
        widest = max(font.getlength(line) for line in lines)
        if (len(lines) <= 3 and widest <= w * 0.9) or font_size <= 64:
            break
        font_size -= 6
    stroke = max(6, font_size // 11)
    line_h = int(font_size * 1.12)
    y = int(h * 0.07) if text_at != "bottom" else int(h * 0.93) - line_h * len(lines)
    for i, line in enumerate(lines):
        x = (w - font.getlength(line)) / 2
        fill = (255, 230, 0) if i == len(lines) - 1 else (255, 255, 255)  # last line in accent yellow
        draw.text((x, y), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += line_h

    quality = 90
    while True:
        img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        if out.stat().st_size <= MAX_BYTES or quality <= 60:
            break
        quality -= 8
    return out


class ThumbnailMaker:
    """Selects the best highlight frame from a rendered clip and composes a thumbnail."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.font = find_font(settings.thumbnail_font_path)

    def from_frames(self, frames: list[Path], hook: str, out: Path, text_at: str = "top") -> Path:
        """Score candidate frames, compose the best one with the hook text."""
        if not frames:
            raise RuntimeError("no candidate frames")
        scored = sorted((score_frame(f) for f in frames), key=lambda f: f.score, reverse=True)
        best = scored[0]
        log.info("thumbnail: best of %d frames = %s (sharp %.0f, bright %.2f, colour %.1f)",
                 len(scored), best.path.name, best.sharpness, best.brightness, best.colorfulness)
        return compose(best.path, hook, out, self.font, (self.s.output_width, self.s.output_height), text_at)

    async def from_video(self, video: Path, hook: str, center_s: float, out: Path,
                         span_s: float = 6.0, fps: float = 2.0) -> Path:
        """Thumbnail from an already-rendered clip (used to retrofit older uploads that
        have no burned-in hook bar). New clips use clean layout frames instead."""
        workdir = out.parent / f".{out.stem}_frames"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            await run_cmd([
                self.s.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{max(0.0, center_s - span_s):.2f}", "-t", f"{span_s * 1.5:.2f}", "-i", str(video),
                "-vf", f"fps={fps}", "-q:v", "2", str(workdir / "c_%03d.jpg"),
            ], timeout=120)
            return self.from_frames(sorted(workdir.glob("c_*.jpg")), hook, out)
        finally:
            for f in workdir.glob("*"):
                f.unlink(missing_ok=True)
            workdir.rmdir()
