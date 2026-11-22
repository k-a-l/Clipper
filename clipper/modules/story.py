"""AI "story" for a clip: hook, spoken intro, title, description (local LLM via Ollama).

Why this exists
---------------
A bare re-upload of a stream moment with a template hook ("Wait for it 👀") is what
YouTube's reused/inauthentic-content rules and Meta's unoriginal-content rules demote,
and it gives a viewer no reason to stop scrolling in the first second. The story adds
the context a viewer from the US/UK who has never seen this streamer needs: *who* this
is, *what* situation they're in and *why* the next seconds matter, spoken over the
start of the clip and written as a specific, searchable title.

Grounding: the model only sees the transcript (with timestamps), the source title and
game/streamer names, and is told never to invent events. Output is a JSON object
validated against a schema (Ollama structured outputs) and re-checked here; anything
off falls back to the template hooks in ``modules.metadata``.

Ollama (MIT, https://github.com/ollama/ollama) runs locally over HTTP; no data leaves
the Mac. Model is configurable (``OLLAMA_MODEL``, default ``qwen3:8b``, Apache-2.0).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from clipper.config import Settings
from clipper.modules.subtitles import Word

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Story:
    hook: str  # on-screen, ≤ 7 words, no emoji
    voiceover: str  # spoken in the first ~3 s after the teaser
    title: str  # ≤ 70 chars incl. one emoji max
    description: str  # 1-2 sentences
    hashtags: list[str] = field(default_factory=list)  # topical, without #Shorts
    question: str = ""  # one genuine question for the comments (no engagement bait)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> Story | None:
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return cls(**{k: d[k] for k in ("hook", "voiceover", "title", "description")},
                       hashtags=list(d.get("hashtags") or []), question=str(d.get("question") or ""))
        except (ValueError, KeyError, TypeError):
            return None


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "voiceover": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "question": {"type": "string"},
    },
    "required": ["hook", "voiceover", "title", "description", "hashtags", "question"],
}

_SYSTEM = """You are the editor of a fast-growing YouTube Shorts / Facebook Reels clip channel for viewers in the US and UK.
You write the words around a short vertical clip so a stranger scrolling past understands it in one second and wants to keep watching.

GROUNDING (most important):
- The transcript is messy speech-to-text from a livestream, with several people talking. Use ONLY what it clearly shows plus the streamer/game/title inputs.
- Never invent names, relationships (daughter, girlfriend…), prices, numbers or outcomes. Never state who someone is unless the inputs say so.
- If the transcript is too unclear to know what happens, write about the situation or the reaction ("Kai Cenat's chat can't believe what he just said") instead of guessing.

STYLE: casual American English like a top clip channel. Concrete, short words. Tension or curiosity, never spoilers. No emoji or hashtags in hook/voiceover.

FIELDS:
- hook: 3-7 words on screen, written as a STATEMENT (no question mark). Specific to THIS clip. Never generic ("Wait for it", "Watch till the end", "Nobody saw this coming", "You won't believe").
- voiceover: ONE sentence, 8-16 words, spoken in the first 3 seconds: who + situation + tension. Write it to be performed, with natural emotion. You MAY add at most one sound tag where it genuinely fits the mood: [chuckle] or [laugh] for something funny, [gasp] for something shocking, [sigh] for something frustrating or awkward (e.g. "[chuckle] Zack thought this would be easy, but the cops had other plans."). Do not add a tag to serious or neutral moments.
- title: under 70 characters; starts with the searchable name (streamer and/or game); says what the clip is about; max one emoji at the end.
- description: 1-2 sentences of context for a newcomer. No links.
- hashtags: 3-5 specific tags without # (streamer, game, topic). Never viral/fyp/shorts/trending.
- question: one genuine question viewers can answer from the clip (an opinion is fine). Never ask to like/comment/share/subscribe/tag.

GOOD EXAMPLES (style only — do not copy facts):
{"hook": "He tried to bribe the cop", "voiceover": "Zack got pulled over in GTA RP and decided to talk his way out of it.", "title": "Asmongold Tries to Bribe a Cop in GTA RP 🚔", "description": "Asmongold gets stopped by police on a GTA roleplay server and tries the worst possible excuse.", "hashtags": ["Asmongold", "GTARP", "GTA5"], "question": "Would that excuse have worked on you?"}
{"hook": "The ISS crew heard this live", "voiceover": "Astronauts on the space station were mid-spacewalk when mission control called in.", "title": "ISS Spacewalk: Mission Control Calls Mid-Task 🚀", "description": "Live NASA radio from a spacewalk outside the International Space Station.", "hashtags": ["NASA", "ISS", "Spacewalk"], "question": "Would you ever do a spacewalk?"}

BAD (never do this): "Kai Cenat talks to his daughter" (relationship not in the inputs), "Wait for it 👀" (generic), "Kai Cenat on failing and waiting" (vague, no tension)."""

_BANNED = re.compile(r"\b(wait for it|watch till the end|nobody saw this coming|you won'?t believe|"
                     r"like and subscribe|smash that|comment below|tag a friend)\b", re.I)
_GENERIC_TAGS = {"viral", "fyp", "foryou", "foryoupage", "shorts", "reels", "trending", "explore", "viralvideo"}
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")


def clean_name(name: str | None) -> str | None:
    """Hide internal ids ("yt_qsyo5d-dvza", "vod_abc123") that leaked into titles before."""
    if not name or re.fullmatch(r"(yt|vod)_[\w-]+", name.strip()):
        return None
    return name.strip()


def _timed_transcript(words: list[Word], highlight_at: float | None, limit: int = 1800) -> str:
    """'[0:03] text …' lines every ~4 s, with the highlight marked, so the model knows what happens when."""
    lines: list[str] = []
    cur: list[str] = []
    t0 = None
    marked = highlight_at is None
    for w in words:
        if t0 is None:
            t0 = w.start
        if not marked and w.start >= highlight_at:  # type: ignore[operator]
            if cur:
                lines.append(f"[{int(t0) // 60}:{int(t0) % 60:02d}] {' '.join(cur)}")
                cur, t0 = [], w.start
            lines.append(">>> HIGHLIGHT (the loudest/most reacted moment) <<<")
            marked = True
        cur.append(w.text)
        if w.end - t0 >= 4:
            lines.append(f"[{int(t0) // 60}:{int(t0) % 60:02d}] {' '.join(cur)}")
            cur, t0 = [], None
    if cur and t0 is not None:
        lines.append(f"[{int(t0) // 60}:{int(t0) % 60:02d}] {' '.join(cur)}")
    return "\n".join(lines)[:limit] or "(no clear speech)"


def _fix(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip().strip('"').strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


_RELATIONS = frozenset("daughter son wife husband girlfriend boyfriend mother father mom dad brother sister "
                       "baby kid child children fiance fiancee ex cousin uncle aunt grandma grandpa".split())


@lru_cache(maxsize=1)
def _common_words() -> frozenset[str]:
    """Lower-case English words (system dictionary) — a capitalised token outside it is a *name*."""
    for path in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
        try:
            return frozenset(w for w in Path(path).read_text(errors="ignore").split() if w.islower())
        except OSError:
            continue
    return frozenset()


def _is_word(low: str, common: frozenset[str]) -> bool:
    """Dictionary word, allowing common inflections (tries → try, chased → chase, running → run)."""
    cands = {low, low.rstrip("s")}
    for suf, rep in (("ies", "y"), ("ied", "y"), ("iest", "y"), ("ier", "y"), ("es", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"),
                     ("er", ""), ("est", ""), ("ly", ""), ("'s", "")):
        if low.endswith(suf) and len(low) > len(suf) + 1:
            stem = low[: -len(suf)] + rep
            cands |= {stem, stem[:-1] if len(stem) > 3 and stem[-1] == stem[-2] else stem}
    return any(c in common for c in cands)


def ungrounded(text: str, source: str) -> list[str]:
    """Names, numbers and family words in ``text`` that never appear in ``source`` (made-up facts)."""
    src = source.lower()
    common = _common_words()
    bad = []
    for tok in re.findall(r"[A-Za-z][A-Za-z'’]+|\d[\d,.]*", text):
        low = re.sub(r"['’]s?$", "", tok.lower())
        if tok[0].isdigit():
            if tok.rstrip(".,") not in source:
                bad.append(tok)
        elif low in _RELATIONS or low.rstrip("s") in _RELATIONS:
            if not re.search(rf"\b{re.escape(low.rstrip('s'))}", src):
                bad.append(tok)
        elif tok[0].isupper() and common and not _is_word(low, common) and low not in src and len(low) > 2:
            bad.append(tok)
    return bad


def validate(raw: dict[str, Any], source: str = "") -> Story | None:
    """Enforce the rules the prompt asks for; reject output we can't trust (incl. made-up facts)."""
    hook = _EMOJI_RE.sub("", _fix(raw.get("hook", ""), 48)).strip()
    from clipper.modules.voiceover import EMOTION_TAGS, strip_tags

    vo = _EMOJI_RE.sub("", _fix(raw.get("voiceover", ""), 180)).strip()
    tags_seen = 0

    def keep(m: re.Match[str]) -> str:
        nonlocal tags_seen
        tag = m.group(1).lower()
        if tag in EMOTION_TAGS and tags_seen < 2:
            tags_seen += 1
            return f"[{tag}]"
        return ""

    vo = re.sub(r"\s{2,}", " ", re.sub(r"\[(\w+)\]", keep, vo)).strip()
    spoken = strip_tags(vo)
    title = _fix(raw.get("title", ""), 70)
    desc = _fix(raw.get("description", ""), 400)
    question = _fix(raw.get("question", ""), 120)
    hook = strip_tags(hook)
    if not (2 <= len(hook.split()) <= 8) or not (5 <= len(spoken.split()) <= 22) or len(title) < 10:
        return None
    if any(_BANNED.search(x) for x in (hook, spoken, title)):
        return None
    if source and (bad := ungrounded(" ".join((hook, spoken, title, desc)), source)):
        log.info("story: not in the clip: %s", ", ".join(dict.fromkeys(bad)))
        return None
    if re.search(r"\b(like|comment|share|subscribe|tag)\b", question, re.I):
        question = ""
    tags: list[str] = []
    for t in raw.get("hashtags") or []:
        t = re.sub(r"[^A-Za-z0-9]", "", str(t))
        if 2 <= len(t) <= 30 and t.lower() not in _GENERIC_TAGS and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    return Story(hook=hook, voiceover=vo, title=title, description=desc, hashtags=tags[:5], question=question)


async def _ollama(s: Settings, client: httpx.AsyncClient, body: dict[str, Any]) -> str:
    r = await client.post(f"{s.ollama_url.rstrip('/')}/api/chat", json=body)
    r.raise_for_status()
    return str(r.json()["message"]["content"])


_VISION_CACHE: dict[str, bool] = {}


async def _has_vision(s: Settings, client: httpx.AsyncClient) -> bool:
    model = s.ollama_vision_model
    if not model:
        return False
    if _VISION_CACHE.get(model):  # only "yes" is cached: a model pulled later is picked up without a restart
        return True
    try:
        r = await client.post(f"{s.ollama_url.rstrip('/')}/api/show", json={"model": model})
        ok = r.status_code == 200 and "vision" in (r.json().get("capabilities") or [])
    except (httpx.HTTPError, ValueError):
        return False
    if ok:
        _VISION_CACHE[model] = True
    else:
        log.info("story: vision model %s not installed (ollama pull %s); using the transcript only", model, model)
    return ok


def _grid(frames: list[Path], cell: int = 640) -> str:
    """Frames → one 2×2 JPEG (base64). Several separate images are rejected by some vision models."""
    import io

    from PIL import Image

    imgs = [Image.open(f).convert("RGB") for f in frames]
    h = max(1, round(cell * imgs[0].height / imgs[0].width))
    cols = 2 if len(imgs) > 1 else 1
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cell * cols, h * rows))
    for i, im in enumerate(imgs):
        sheet.paste(im.resize((cell, h)), ((i % cols) * cell, (i // cols) * h))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def describe_frames(s: Settings, client: httpx.AsyncClient, frames: list[Path]) -> str:
    """What the frames visibly show (vision model), so the story isn't guessed from messy speech."""
    if not frames or not await _has_vision(s, client):
        return ""
    images = [_grid(frames[:4])]
    body = {"model": s.ollama_vision_model, "stream": False, "think": False, "options": {"temperature": 0.2},
            "messages": [{"role": "user", "images": images, "content":
                          "This image is a 2x2 grid of frames from a short video clip, in reading order (top-left "
                          "first). In 2-3 plain sentences say "
                          "what is visibly happening: setting, game (only if obvious), what people or characters "
                          "do, and any on-screen text you can read. Do not guess who anyone is."}]}
    try:
        text = re.sub(r"\s+", " ", await _ollama(s, client, body)).strip()
    except (httpx.HTTPError, KeyError, ValueError):
        return ""
    log.info("story: frames show: %s", text[:300])
    return text[:800]


async def write_story(s: Settings, *, words: list[Word], highlight_at: float | None, streamer: str | None,
                      game: str | None, source_title: str | None, category: str | None,
                      frames: list[Path] | None = None) -> Story | None:
    """Ask the local LLM for a story; None if Ollama isn't reachable or the answer fails validation."""
    if not s.story_llm:
        return None
    transcript = _timed_transcript(words, highlight_at)
    try:
        async with httpx.AsyncClient(timeout=s.story_timeout_s) as client:
            seen = await describe_frames(s, client, frames or [])
            facts = [f"Streamer / creator: {clean_name(streamer) or 'unknown'}",
                     f"Game: {game or 'unknown / not a game'}",
                     f"Category: {category or 'unknown'}",
                     f"Source video/stream title: {source_title or 'unknown'}"]
            if seen:
                facts.append(f"What the video frames show: {seen}")
            facts += ["Clip transcript (timestamps from clip start; speech-to-text, may contain errors):", transcript]
            source = "\n".join(facts)
            body: dict[str, Any] = {
                "model": s.ollama_model, "stream": False, "think": s.story_think, "format": _SCHEMA,
                "options": {"temperature": 0.7, "num_ctx": 6144},
                "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": source}],
            }
            for attempt in range(3):
                content = await _ollama(s, client, body)
                try:
                    story = validate(json.loads(content), source)
                except ValueError:
                    story = None
                if story:
                    log.info("story: hook %r | vo %r | title %r", story.hook, story.voiceover, story.title)
                    return story
                log.info("story: answer %d rejected: %s", attempt + 1, content[:200])
                body["options"]["temperature"] = 0.9
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.info("story: Ollama unavailable (%s); using template hook", exc.__class__.__name__)
    return None
