"""Standalone Kokoro TTS program: text → 24 kHz WAV.

Runs in its own virtualenv (``storage/tools/tts-venv``), invoked by
``modules/voiceover.py`` as a subprocess. It's kept out of Clipper's process because
kokoro-onnx depends on phonemizer / espeak-ng (GPL-3.0), and the project rule is that
copyleft code runs only as a separate program (like FFmpeg).

    tts-venv/bin/python tools/tts_kokoro.py --model-dir DIR --voice am_michael \
        --speed 1.1 --out vo.wav "Text to speak"

Prints the duration in seconds on stdout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True, type=Path)
    ap.add_argument("--voice", default="am_michael")
    ap.add_argument("--speed", type=float, default=1.1)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("text")
    a = ap.parse_args()

    import soundfile as sf
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(a.model_dir / "kokoro-v1.0.onnx"), str(a.model_dir / "voices-v1.0.bin"))
    lang = "en-gb" if a.voice[:1] == "b" else "en-us"
    samples, rate = kokoro.create(a.text, voice=a.voice, speed=a.speed, lang=lang)
    sf.write(str(a.out), samples, rate)
    print(f"{len(samples) / rate:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
