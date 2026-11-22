"""Module 4 — faster-whisper transcription and kinetic ASS subtitle generation.

Words are transcribed with word-level timestamps, grouped into short 3-4 word
"punch" lines, and emitted as Advanced SubStation Alpha events that:

* use a bold font with a 3 px black outline, centred horizontally in the lower
  third of the 1080x1920 canvas (over the blurred background, not the gameplay);
* pop in with a quick scale animation (``\\t`` transform) and fade;
* highlight each word as it is spoken with karaoke timing (``{\\k<cs>}``) —
  upcoming words use ``SecondaryColour``, spoken words switch to ``PrimaryColour``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pysubs2

from clipper.config import Settings
from clipper.modules.metadata import strip_emoji

if TYPE_CHECKING:  # pragma: no cover
    from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


# ----------------------------------------------------------------- transcriber
class Transcriber:
    """Lazy-loading faster-whisper wrapper; inference runs in a worker thread."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._model: WhisperModel | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> WhisperModel:
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading whisper model %s (%s/%s)", self.s.whisper_model,
                     self.s.whisper_device, self.s.whisper_compute_type)
            self._model = WhisperModel(
                self.s.whisper_model, device=self.s.whisper_device,
                compute_type=self.s.whisper_compute_type,
                download_root=str(self.s.storage_dir / "models"),
            )
        return self._model

    @staticmethod
    def _read_wav(path: Path) -> np.ndarray:
        """Load 16 kHz mono PCM16 WAV as float32 samples in [-1, 1].

        Passing samples (not a path) bypasses faster-whisper's PyAV decoding,
        which breaks on PyAV 19 (``av.open(metadata_errors=...)`` was removed).
        Our audio is always pre-extracted by FFmpeg, so stdlib ``wave`` suffices.
        """
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getnchannels() != 1 or wf.getframerate() != 16000:
                raise ValueError(f"expected 16 kHz mono s16 WAV, got {wf.getparams()}")
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
        return pcm.astype(np.float32) / 32768.0

    def _transcribe_sync(self, audio: Path) -> list[Word]:
        model = self._load()
        segments, _info = model.transcribe(
            self._read_wav(audio), language="en", word_timestamps=True, vad_filter=True,
            beam_size=5, condition_on_previous_text=False,
        )
        words: list[Word] = []
        for seg in segments:
            for w in seg.words or []:
                text = w.word.strip()
                if text and w.end > w.start:
                    words.append(Word(text=text, start=float(w.start), end=float(w.end)))
        return words

    async def transcribe(self, audio: Path) -> list[Word]:
        """Transcribe a 16 kHz mono WAV, returning word-timed tokens."""
        async with self._lock:  # one model instance, one inference at a time
            return await asyncio.to_thread(self._transcribe_sync, audio)


# -------------------------------------------------------------------- grouping
def group_words(
    words: list[Word], max_words: int = 4, max_gap: float = 0.7, max_chars: int = 26
) -> list[list[Word]]:
    """Split words into short caption lines.

    A new line starts when the line is full, the pause before the next word is
    long, the line would get too wide, or the previous word ended a sentence.
    """
    lines: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if current:
            prev = current[-1]
            width = sum(len(w.text) + 1 for w in current) + len(word.text)
            if (
                len(current) >= max_words
                or word.start - prev.end > max_gap
                or width > max_chars
                or prev.text[-1:] in ".?!"
            ):
                lines.append(current)
                current = []
        current.append(word)
    if current:
        lines.append(current)
    return lines


# ------------------------------------------------------------------------- ASS
def hex_to_color(color: str, alpha: int = 0) -> pysubs2.Color:
    """``#RRGGBB`` → :class:`pysubs2.Color` (alpha 0 = opaque, 255 = transparent)."""
    raw = color.lstrip("#")
    return pysubs2.Color(int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), alpha)


def _escape(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).replace("\n", " ")


# Pop-in: start at 80% scale, overshoot to 108%, settle at 100%, with a short fade.
_POP_IN = r"{\fad(40,60)\fscx80\fscy80\t(0,90,\fscx108\fscy108)\t(90,160,\fscx100\fscy100)}"


def build_subs(words: list[Word], settings: Settings, duration: float | None = None,
               hook: str | None = None, position: str = "bottom", hook_position: str = "top",
               seam_y: int | None = None, offset: float = 0.0) -> pysubs2.SSAFile:
    """Build a kinetic-karaoke subtitle document (file I/O and ASS syntax via pysubs2).

    ``seam_y`` (PlayRes px) = boundary between stacked panels; "middle" captions sit on it.
    ``offset`` shifts every caption later (the flash-forward teaser plays before the clip);
    the hook is on screen from the very first frame.
    """
    s = settings
    subs = pysubs2.SSAFile()
    subs.info.update({
        "PlayResX": str(s.output_width), "PlayResY": str(s.output_height),
        "WrapStyle": "2", "ScaledBorderAndShadow": "yes", "YCbCr Matrix": "TV.709",
    })
    subs.styles = {"Kinetic": pysubs2.SSAStyle(
        fontname=s.caption_font, fontsize=s.caption_font_size, bold=True,
        primarycolor=hex_to_color(s.caption_primary_color),     # spoken word
        secondarycolor=hex_to_color(s.caption_secondary_color),  # upcoming word
        outlinecolor=hex_to_color("#000000"), backcolor=hex_to_color("#000000", alpha=0x80),
        borderstyle=1, outline=s.caption_outline_px, shadow=1,
        alignment={"middle": pysubs2.Alignment.MIDDLE_CENTER, "top": pysubs2.Alignment.TOP_CENTER}.get(
            position, pysubs2.Alignment.BOTTOM_CENTER),
        marginl=60, marginr=60,
        # top: under a top hook bar; bottom: lower third (above a bottom hook bar)
        marginv={"middle": 0, "top": 470 if (hook and hook_position == "top") else 260}.get(
            position, 470 if (hook and hook_position == "bottom") else s.caption_margin_v),
    )}

    if hook and s.hook_overlay:
        # Top-of-frame hook bar: white bold text in a translucent black box (BorderStyle 3),
        # sitting in the blurred area above the gameplay so it never covers the action.
        subs.styles["Hook"] = pysubs2.SSAStyle(
            fontname=s.caption_font, fontsize=s.hook_font_size, bold=True,
            primarycolor=hex_to_color("#FFFFFF"), outlinecolor=hex_to_color("#000000", alpha=0x50),
            backcolor=hex_to_color("#000000", alpha=0x50), borderstyle=3, outline=18, shadow=0,
            alignment=pysubs2.Alignment.BOTTOM_CENTER if hook_position == "bottom" else pysubs2.Alignment.TOP_CENTER,
            marginl=70, marginr=70, marginv=260,
        )
        end_ms = pysubs2.make_time(s=duration + offset) if duration else pysubs2.make_time(s=600)
        subs.events.append(pysubs2.SSAEvent(
            start=0, end=end_ms, style="Hook",
            text=r"{\q0\fad(150,0)\fscx90\fscy90\t(0,180,\fscx100\fscy100)}" + _escape(strip_emoji(hook).upper()),
        ))

    # Heavy uppercase fonts are ~0.66 em per character: keep each line inside the frame.
    fit_chars = max(8, int((s.output_width - 140) / (s.caption_font_size * 0.66)))
    lines = group_words(words, max_words=s.caption_max_words, max_chars=fit_chars)
    for i, line in enumerate(lines):
        start = line[0].start
        natural_end = line[-1].end + 0.15
        end = min(natural_end, lines[i + 1][0].start) if i + 1 < len(lines) else natural_end
        if duration is not None:
            end = min(end, duration)
        end = max(end, start + 0.3)

        parts: list[str] = []
        for j, word in enumerate(line):
            nxt = line[j + 1].start if j + 1 < len(line) else end
            k_cs = max(1, round((nxt - word.start) * 100))  # karaoke duration, centiseconds
            text = _escape(word.text.upper() if s.caption_uppercase else word.text)
            parts.append(f"{{\\k{k_cs}}}{text}")
        pos = f"{{\\an5\\pos({s.output_width // 2},{seam_y})}}" if (position == "middle" and seam_y) else ""
        subs.events.append(pysubs2.SSAEvent(
            start=pysubs2.make_time(s=start + offset), end=pysubs2.make_time(s=end + offset),
            style="Kinetic", text=pos + _POP_IN + " ".join(parts),
        ))
    return subs


def build_ass(words: list[Word], settings: Settings, duration: float | None = None,
              hook: str | None = None, position: str = "bottom", hook_position: str = "top",
              seam_y: int | None = None) -> str:
    """Return the ASS document as a string."""
    return build_subs(words, settings, duration, hook, position, hook_position, seam_y).to_string("ass")


def write_ass(words: list[Word], settings: Settings, path: Path, duration: float | None = None,
              hook: str | None = None, position: str = "bottom", hook_position: str = "top",
              seam_y: int | None = None, offset: float = 0.0) -> Path:
    """Write the ASS file and return its path."""
    build_subs(words, settings, duration, hook, position, hook_position, seam_y, offset).save(
        str(path), format_="ass", encoding="utf-8")
    log.info("wrote %d caption words → %s", len(words), path.name)
    return path


def words_to_text(words: list[Word]) -> str:
    return " ".join(w.text for w in words).strip()


__all__ = ["Transcriber", "Word", "build_ass", "group_words", "write_ass", "words_to_text"]
