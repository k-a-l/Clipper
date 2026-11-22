"""AI voiceover (Kokoro-82M, Apache-2.0) and procedurally generated sound effects.

* **Voice**: ``tools/tts_kokoro.py`` runs in a separate virtualenv / process (see that file
  for why). A missing venv or model just means no voiceover: the clip still renders.
* **SFX**: a whoosh (rising filtered noise) and an impact (low sine thump + noise click)
  are synthesised once with FFmpeg's own sources, so there are no third-party audio files
  and no licensing questions.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from clipper.config import Settings
from clipper.modules.utils import CommandError, run_cmd

log = logging.getLogger(__name__)

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
_TOOL = _TOOLS / "tts_kokoro.py"
_EXPRESSIVE_TOOL = _TOOLS / "tts_chatterbox.py"

# Non-speech sounds the expressive engine performs (Chatterbox Turbo paralinguistic tags).
EMOTION_TAGS = ("laugh", "chuckle", "gasp", "sigh", "groan")
_TAG_RE = re.compile(r"\[(\w+)\]")
# A neutral ~10 s passage for the expressive engine to copy each voice from.
_REFERENCE_TEXT = ("Here's something you probably haven't seen before. It started like any other day, "
                   "but then things got a little strange, and honestly, nobody expected what came next.")


def strip_tags(text: str) -> str:
    """Text without [laugh]-style tags (captions, the plain narrator)."""
    return re.sub(r"\s{2,}", " ", _TAG_RE.sub("", text)).strip(" ,")


def tts_available(s: Settings) -> bool:
    return (s.tts_python.is_file() and (s.tts_model_dir / "kokoro-v1.0.onnx").is_file()
            and (s.tts_model_dir / "voices-v1.0.bin").is_file())


def expressive_available(s: Settings) -> bool:
    return s.emotion_python.is_file() and tts_available(s)


def _speakable(text: str, keep_tags: bool = False) -> str:
    """Drop emoji / hashtags / symbols the voice would read out oddly (and unknown [tags])."""
    text = re.sub(r"#\w+", "", text)
    text = _TAG_RE.sub(lambda m: f"[{m.group(1).lower()}]" if keep_tags and m.group(1).lower() in EMOTION_TAGS
                       else "", text)
    allowed = r"[^\w\s.,!?'’\-:;$%&\[\]]" if keep_tags else r"[^\w\s.,!?'’\-:;$%&]"
    return re.sub(r"\s{2,}", " ", re.sub(allowed, "", text)).strip()


async def _kokoro(s: Settings, text: str, out_wav: Path, kokoro_voice: str) -> float:
    res = await run_cmd([str(s.tts_python), str(_TOOL), "--model-dir", str(s.tts_model_dir.resolve()),
                         "--voice", kokoro_voice, "--speed", f"{s.tts_speed:.2f}",
                         "--out", str(out_wav.resolve()), text], timeout=180)
    return float(res.stdout.strip().splitlines()[-1])


async def _reference(s: Settings, kokoro_voice: str) -> Path:
    """The voice sample the expressive engine imitates (made once per voice with Kokoro)."""
    d = s.storage_dir / "assets" / "voices"
    d.mkdir(parents=True, exist_ok=True)
    ref = d / f"{kokoro_voice}.wav"
    if not ref.is_file():
        await _kokoro(s, _REFERENCE_TEXT, ref, kokoro_voice)
    return ref


async def synthesize(s: Settings, text: str, out_wav: Path, voice: str | None = None,
                     expressive: bool = False) -> float | None:
    """Speak ``text`` into ``out_wav`` in ``voice`` (male_us/female_us/male_uk/female_uk or a Kokoro id).

    ``expressive``: Chatterbox Turbo performs [laugh]/[chuckle]/[gasp]… tags and livelier delivery;
    falls back to Kokoro (tags dropped) when it isn't installed or fails. Returns the duration,
    or None when no TTS is available.
    """
    from clipper.config import VOICES

    kokoro_voice = VOICES.get(voice or "", voice or s.tts_voice)
    plain = _speakable(strip_tags(text))
    if not plain or not tts_available(s):
        if plain:
            log.info("voiceover skipped: Kokoro not installed (see README → Story edit)")
        return None
    if expressive and expressive_available(s):
        try:
            ref = await _reference(s, kokoro_voice)
            res = await run_cmd([str(s.emotion_python), str(_EXPRESSIVE_TOOL), "--ref", str(ref.resolve()),
                                 "--out", str(out_wav.resolve()), _speakable(text, keep_tags=True)], timeout=300)
            return float(res.stdout.strip().splitlines()[-1])
        except (CommandError, OSError, ValueError, IndexError, TimeoutError) as exc:
            log.warning("expressive voice failed (%s); using the plain narrator", str(exc)[-300:])
    try:
        return await _kokoro(s, plain, out_wav, kokoro_voice)
    except (CommandError, OSError, ValueError, IndexError, TimeoutError) as exc:
        log.warning("voiceover failed: %s", exc)
        return None


async def sfx(s: Settings) -> dict[str, Path]:
    """Paths of the generated sound effects (created on first use)."""
    d = s.storage_dir / "assets" / "sfx"
    d.mkdir(parents=True, exist_ok=True)
    recipes = {
        # 0.45 s of pink noise swept up through a band-pass, fading in then out
        "whoosh": ["-f", "lavfi", "-i", "anoisesrc=color=pink:duration=0.45:amplitude=0.6",
                   "-af", "highpass=f=300,lowpass=f=4000,afade=t=in:d=0.3,afade=t=out:st=0.3:d=0.15,volume=0.8"],
        # low 55 Hz thump with a fast exponential decay
        "hit": ["-f", "lavfi", "-i", "sine=frequency=55:duration=0.6",
                "-f", "lavfi", "-i", "anoisesrc=color=brown:duration=0.08:amplitude=0.9",
                "-filter_complex", "[0]afade=t=out:st=0.02:d=0.55:curve=exp,volume=2.2[a];"
                                   "[1]lowpass=f=1200,afade=t=out:d=0.08[b];[a][b]amix=inputs=2:normalize=0"],
    }
    out: dict[str, Path] = {}
    for name, args in recipes.items():
        path = d / f"{name}.wav"
        if not path.is_file():
            await run_cmd([s.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *args,
                           "-ac", "2", "-ar", "48000", str(path)], timeout=60)
        out[name] = path
    return out
