"""Standalone expressive TTS program (Chatterbox Turbo, MIT): text with [laugh]/[chuckle]/[gasp]… → WAV.

Runs in its own virtualenv (``storage/tools/emotion-venv``; it pins torch 2.6 and
transformers versions that would clash with Clipper's), invoked by ``modules/voiceover.py``.

    emotion-venv/bin/python tools/tts_chatterbox.py --ref voice.wav --out vo.wav "Wait [gasp] he did WHAT?"

``--ref`` is a ~10 s sample of the voice to speak in (Clipper makes it with Kokoro, so the
male/female US/UK voices match the plain narrator). Prints the duration in seconds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("text")
    a = ap.parse_args()

    import soundfile as sf
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = ChatterboxTurboTTS.from_pretrained(device=device)
    wav = model.generate(a.text, audio_prompt_path=str(a.ref))
    samples = wav.squeeze(0).detach().cpu().numpy()
    sf.write(str(a.out), samples, model.sr)
    print(f"{len(samples) / model.sr:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
