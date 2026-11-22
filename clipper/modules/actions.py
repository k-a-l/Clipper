"""What happens in a clip (racing, police chase, fight, shootout…), so the title can say it:
"GTA 6 Police Chase 🚔: Wait for it 👀".

Two signals:

1. **Frames (OpenCLIP, MIT, https://github.com/mlfoundations/open_clip).** Five frames around the
   highlight are compared with short descriptions of each action. Runs on the Mac GPU (MPS). Optional:
   without torch/open_clip only the transcript is used.
2. **Words.** What the streamer and chat say ("cops", "race", "he's shooting") backs up or replaces
   the frame guess.

An action is only reported when the evidence is clear; otherwise the title stays as it was.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Action:
    key: str
    label: str  # in titles: "GTA 6 Police Chase"
    emoji: str
    prompts: tuple[str, ...]  # what it looks like
    words: tuple[str, ...]  # what people say (regex, lower case)
    tags: tuple[str, ...]  # generic hashtags; "{G}" = the game's main tag without '#' (GTA6 → #GTA6Racing)


ACTIONS: tuple[Action, ...] = (
    Action("police_chase", "Police Chase", "🚔",
           ("police cars chasing a car in a video game", "a high speed police pursuit with sirens in a video game"),
           (r"\bcops?\b", r"\bpolice\b", r"\bwanted\b", r"\bpursuit\b", r"\bchase\b", r"\b5 stars?\b"),
           ("#{G}PoliceChase", "#PoliceChase", "#CarChase")),
    Action("racing", "Racing", "🏎️",
           ("supercars racing on a road in a video game", "a street race at high speed in a video game"),
           (r"\brac(?:e|ing)\b", r"\bdrift", r"\bsupercar\b", r"\bfinish line\b"),
           ("#{G}Racing", "#Racing", "#StreetRace")),
    Action("fight", "Fight", "👊",
           ("two characters fist fighting in a video game", "a melee brawl punching someone in a video game"),
           (r"\bfight", r"\bpunch", r"\bknock(?:ed)? (?:him|her|out)\b", r"\bbrawl\b"),
           ("#{G}Fight", "#Fight", "#GamingFights")),
    Action("shootout", "Shootout", "🔫",
           ("a gunfight with characters shooting guns in a video game", "third person shooter aiming a rifle"),
           (r"\bshoot", r"\bshot\b", r"\bgun\b", r"\bheadshot\b", r"\bsniper\b", r"\bkill(?:ed)?\b"),
           ("#{G}Shootout", "#Shootout", "#Headshot")),
    Action("crash", "Crash", "💥",
           ("a car crash wreck in a video game", "a vehicle flipping over after a collision in a video game"),
           (r"\bcrash", r"\bwreck", r"\bflipped\b"), ("#{G}Crash", "#CarCrash")),
    Action("explosion", "Explosion", "💣",
           ("a huge explosion with fire and smoke in a video game",),
           (r"\bexplo", r"\bblew up\b", r"\bboom\b", r"\bsticky\b", r"\brpg\b"), ("#{G}Explosion", "#Explosion")),
    Action("flying", "Flying", "✈️",
           ("flying a plane in the sky in a video game", "a helicopter flying over a city in a video game"),
           (r"\bplane\b", r"\bhelicopter\b", r"\bheli\b", r"\bjet\b", r"\bparachute\b"),
           ("#{G}Flying", "#Pilot")),
    Action("stunt", "Stunt", "🏍️",
           ("a motorcycle stunt jump in the air in a video game", "a car doing a huge ramp jump in a video game"),
           (r"\bstunt\b", r"\bjump(?:ed)?\b", r"\bramp\b", r"\bwheelie\b"), ("#{G}Stunts", "#Stunts")),
    Action("heist", "Heist", "💰",
           ("masked robbers robbing a bank in a video game", "a heist with robbers and money bags in a video game"),
           (r"\bheist\b", r"\brob(?:bing|bery)?\b", r"\bbank\b", r"\bvault\b"), ("#{G}Heist", "#Heist")),
)
# what the frame might show instead (these never become titles)
_OTHER = ("a streamer talking to a webcam", "a video game menu or map screen", "a character walking in a city",
          "a black loading screen", "people talking in a video game cutscene")

_lock = threading.Lock()
_model: Any = None


def available() -> bool:
    return find_spec("open_clip") is not None and find_spec("torch") is not None


def _load() -> Any:
    global _model
    with _lock:
        if _model is None:
            import open_clip
            import torch

            dev = "mps" if torch.backends.mps.is_available() else "cpu"
            model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
            model = model.to(dev).eval()
            tok = open_clip.get_tokenizer("ViT-B-32")
            texts = [p for a in ACTIONS for p in a.prompts] + list(_OTHER)
            with torch.no_grad():
                t = model.encode_text(tok(texts).to(dev))
                t = t / t.norm(dim=-1, keepdim=True)
            owners = [a.key for a in ACTIONS for _ in a.prompts] + ["_other"] * len(_OTHER)
            _model = (model, prep, dev, t, owners)
    return _model


CROPS = {  # where the gameplay sits in the rendered 9:16 frame, per layout
    "center": "crop=iw:iw*0.75:0:(ih-iw*0.75)/2", "fit": "crop=iw:iw*0.5625:0:(ih-iw*0.5625)/2",
    "full": "crop=iw:ih:0:0", "cam_top": "crop=iw:ih*0.58:0:ih*0.42", "cam_bottom": "crop=iw:ih*0.58:0:0",
}


def frames(ffmpeg: str, video: Path, at: float | None, n: int = 5, layout: str = "center") -> list[Any]:
    """`n` frames spread around `at` (default: the middle), gameplay area only (per layout)."""
    from PIL import Image

    try:
        probe = str(Path(ffmpeg).with_name("ffprobe")) if "/" in ffmpeg else "ffprobe"
        dur = float(subprocess.run([probe, "-v", "error", "-show_entries",
                                    "format=duration", "-of", "csv=p=0", str(video)],
                                   capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        dur = 0
    if dur <= 0:
        return []
    centre = at if at is not None and 0 < at < dur else dur * 0.55
    out = []
    for i in range(n):
        t = min(dur - 0.1, max(0.0, centre - 4 + i * 8 / max(1, n - 1)))
        r = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", CROPS.get(layout, CROPS["center"]) + ",scale=448:-2", "-f", "image2pipe",
                            "-vcodec", "png", "-"], capture_output=True, timeout=30)
        if r.stdout:
            import io

            out.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))
    return out


def from_frames(imgs: list[Any]) -> dict[str, float]:
    """Probability per action key (and '_other') averaged over frames."""
    if not imgs or not available():
        return {}
    import torch

    model, prep, dev, t, owners = _load()
    with torch.no_grad():
        x = torch.stack([prep(i) for i in imgs]).to(dev)
        f = model.encode_image(x)
        f = f / f.norm(dim=-1, keepdim=True)
        probs = (100 * f @ t.T).softmax(dim=-1).mean(0).cpu().tolist()
    agg: dict[str, float] = {}
    for k, p in zip(owners, probs):
        agg[k] = agg.get(k, 0.0) + p
    return agg


def from_words(text: str) -> dict[str, int]:
    low = (text or "").lower()
    return {a.key: sum(len(re.findall(w, low)) for w in a.words) for a in ACTIONS}


def detect(ffmpeg: str, video: Path, transcript: str = "", at: float | None = None,
           layout: str = "center") -> str | None:
    """The clip's action key, or None when nothing is clear."""
    words = from_words(transcript)
    try:
        vis = from_frames(frames(ffmpeg, video, at, layout=layout))
    except Exception:  # never lose a clip over the AI
        log.exception("action detection failed")
        vis = {}
    best, score = None, 0.0
    for a in ACTIONS:
        s = vis.get(a.key, 0.0) + min(3, words.get(a.key, 0)) * 0.12
        if s > score:
            best, score = a.key, s
    if best is None:
        return None
    other = vis.get("_other", 0.0)
    clear = score >= 0.32 and (not vis or score > other * 0.8) or words.get(best, 0) >= 3
    log.info("action: %s (%.2f, other %.2f, words %s)", best, score, other, words.get(best, 0))
    return best if clear else None


def get(key: str | None) -> Action | None:
    return next((a for a in ACTIONS if a.key == key), None)
