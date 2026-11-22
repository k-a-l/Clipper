"""Per-clip hook, title, description, caption and hashtag generation.

Principles (platform guidance + what drives short-form ranking)
--------------------------------------------------------------
* **One hook everywhere.** A short, truthful line taken from what was actually said
  (or the chat's reaction) is burned onto the video, used as the thumbnail text
  and leads the title. Consistency builds click-through without misleading
  viewers; YouTube penalises misleading thumbnails/titles.
* **YouTube Shorts:** titles ≤ ~60 chars (mobile truncates), hook first, topic
  keyword included; description opens with a keyword-rich sentence, then a
  subscribe CTA, credit, and 3-5 hashtags (`#Shorts` + topic + keywords). YouTube
  shows the first hashtags above the title and ignores *all* if > 60, so keep few.
* **Facebook Reels:** caption = hook + one *genuine* question to invite replies.
  Meta demotes explicit engagement bait ("comment YES", "tag a friend", "share
  if…"), so the questions never instruct people to like/comment/tag/share.
* Titles are de-duplicated against previous uploads. Descriptions are one short line + hashtags.
"""

from __future__ import annotations

import random
from pathlib import Path
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

Mood = Literal["funny", "hype", "win", "confused"]
Platform = Literal["youtube", "facebook"]

_KEYWORD_MOOD: dict[str, Mood] = {"LMAO": "funny", "POG": "hype", "W": "win", "???": "confused"}

# Curiosity hooks that stay truthful for a chat-spike clip (the payoff is ~20 s in).
_HOOKS: dict[Mood, list[str]] = {
    "funny": ["Wait for the ending 😂", "Chat could not stop laughing", "Nobody was ready for this",
              "The timing on this 💀", "This had chat in tears"],
    "hype": ["Wait for it 👀", "Chat went absolutely crazy", "Nobody saw this coming",
             "Watch till the end 🔥", "This moment broke chat"],
    "win": ["Watch how this ends 🏆", "Chat said W", "This is how it's done",
            "Biggest W of the stream", "Wait for it… W"],
    "confused": ["Wait… what just happened? 🤔", "Nobody understood this", "Explain this one",
                 "Watch it twice 👀", "Chat was so confused"],
}
_NEUTRAL_HOOKS = ["Wait for it 👀", "Watch till the end", "You have to see this"]
_QUESTIONS: dict[Mood, list[str]] = {
    "funny": ["Who else laughed way too hard at this? 😂", "Funniest moment of the stream?",
              "Was it the timing or the reaction? 💀"],
    "hype": ["Did you see that coming? 👀", "Rate this moment 1-10 🔥", "Best moment of the stream?"],
    "win": ["Would you have pulled that off? 🏆", "Rate this play 1-10", "Clean or lucky? 🤔"],
    "confused": ["Can anyone explain what happened here? 🤔", "What did you see? 👀",
                 "How would you describe this one?"],
}
_YT_CTAS = ["👉 Subscribe for daily highlights", "👉 Subscribe for more moments like this",
            "🔔 Subscribe so you don't miss the next one"]
_MOOD_TAGS: dict[Mood, list[str]] = {
    "funny": ["#funny", "#funnymoments"], "hype": ["#viral", "#highlights"],
    "win": ["#clutch", "#highlights"], "confused": ["#wtf", "#funnymoments"],
}
_SOURCE_TAGS = {"twitch.tv": ["#twitch", "#twitchclips", "#gaming"], "youtube.com": ["#live"]}

_STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how i if in into is it its itself just let like me more most my no nor not now of off on once
only or other our out over own really right same she should so some such than that the their them then there
these they this those through to too under until up very was we well were what when where which while who why
will with would yeah yes you your yours okay ok oh uh um gonna wanna got get go going come know think see
look want need make say said thing things one two way also back still even much many
bro yo dude bruh nah man guys gotta lemme ain like lol lmao omg wait hey hello
god wow huh damn shit fuck fucking bitch ass hell holy dang gosh whoa woah yay hmm mhm uh-huh
what's that's it's i'm you're don't can't didn't there's here's let's he's she's
""".split())


@dataclass(frozen=True)
class ClipContext:
    channel: str
    display_name: str
    source_url: str
    category: str | None = None
    transcript: str = ""
    keyword_hits: dict[str, int] = field(default_factory=dict)
    hook: str = ""
    game: str | None = None  # detected game ("GTA 6"): hashtags, titles, tags
    action: str | None = None  # what happens (modules.actions key): "police_chase", "racing", "fight"…
    story: str = ""  # modules.story.Story JSON: AI-written title/description (preferred when present)

    @property
    def is_vod(self) -> bool:
        """Clips from non-live URLs (VOD pipeline ids start with ``vod_``)."""
        return self.channel.startswith("vod_")


@dataclass(frozen=True)
class Metadata:
    title: str
    description: str
    tags: list[str]


def _unique(tags: list[str | None]) -> list[str]:
    """Drop empties and case-insensitive duplicates (#viral / #Viral), keep order."""
    seen: set[str] = set()
    out = []
    for x in tags:
        if x and x != "#" and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def normalise_title(title: str) -> str:
    """Comparison key used to detect duplicate titles (ignores hashtags, emoji, punctuation)."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"#\w+", "", title.lower()))


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")


def strip_emoji(text: str) -> str:
    """Remove emoji for on-image text (video fonts render them as empty boxes)."""
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip()


def _hashtag(text: str) -> str | None:
    tag = re.sub(r"[^A-Za-z0-9]", "", text)
    return f"#{tag.lower()}" if len(tag) >= 2 else None


def keywords(transcript: str, k: int = 3) -> list[str]:
    """Top topic words from a short transcript (stopword-filtered frequency, acronyms boosted)."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", transcript)
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for w in words:
        key = w.lower().strip("'-")
        if key in _STOPWORDS or len(key) < 3:
            continue
        counts[key] += 2 if w.isupper() and len(w) <= 5 else 1  # acronyms (ISS, LSG) are topical
        display.setdefault(key, w if w.isupper() else key)
    return [display[w] for w, _ in counts.most_common(k)]


def _real_name(ctx: ClipContext) -> bool:
    from clipper.modules.story import clean_name

    return clean_name(ctx.display_name) is not None


class MetadataGenerator:
    """Builds hook-led, platform-specific, attributed metadata."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()

    def mood(self, ctx: ClipContext) -> Mood:
        scored = [(n, _KEYWORD_MOOD[k.upper()]) for k, n in ctx.keyword_hits.items()
                  if k.upper() in _KEYWORD_MOOD]
        return max(scored)[1] if scored else "hype"

    def quote(self, transcript: str) -> str | None:
        """Best short quotable sentence: 3-7 words, prefer exclamations/questions."""
        sentences = [s.strip(" ,-\"'") for s in re.split(r"(?<=[.!?])\s+", transcript) if s.strip()]
        candidates = [s for s in sentences if 3 <= len(s.split()) <= 7]
        if not candidates:
            return None
        # Exclamations first; a question only now and then (a feed of "…?" hooks reads as one template).
        exclaim = [s for s in candidates if s.endswith("!")]
        asks = [s for s in candidates if s.endswith("?")]
        if exclaim:
            pool = exclaim
        elif asks and self.rng.random() < 0.25:
            pool = asks
        else:
            pool = [s for s in candidates if not s.endswith("?")] or candidates
        q = self.rng.choice(pool).rstrip(".")
        return q[:1].upper() + q[1:]

    def make_hook(self, ctx: ClipContext) -> str:
        """Short on-screen hook (≤ 42 chars). Prefers a real quote, else a mood hook."""
        q = self.quote(ctx.transcript) if ctx.transcript else None
        # Only punchy quotes (exclamations/questions) beat a curiosity hook.
        if q and len(q) <= 42 and q.endswith("!") and self.rng.random() < 0.7:
            return f'"{q}"'
        if q and len(q) <= 42 and q.endswith("?") and self.rng.random() < 0.2:
            return f'"{q}"'
        hooks = _HOOKS[self.mood(ctx)]
        if not ctx.keyword_hits:  # no chat reaction recorded → never claim one
            hooks = [h for h in hooks if "chat" not in h.lower()] or _NEUTRAL_HOOKS
        statements = [h for h in hooks if "?" not in h]
        if statements and self.rng.random() < 0.85:
            hooks = statements
        return self.rng.choice(hooks)

    # ---------------------------------------------------------------- titles
    def _title(self, ctx: ClipContext, hook: str, platform: Platform) -> str:
        """Game + what happens first ("GTA 6 Police Chase 🚔: Wait for it 👀"): that is what people search."""
        from clipper.modules.actions import get as action_of

        limit = 60 if platform == "youtube" else 90
        act = action_of(ctx.action)
        topic = ctx.category or (keywords(ctx.transcript, 1) or [""])[0]
        lead = ""
        if ctx.game and act:
            lead = f"{ctx.game} {act.label} {act.emoji}"
        elif ctx.game:
            lead = ctx.game
        elif act:
            lead = f"{act.label} {act.emoji}"
        options: list[str] = []
        if lead:
            if len(lead) + len(hook) + 2 <= limit:
                options += [f"{lead}: {hook}"] * 3
            if len(lead) + len(ctx.display_name) + 3 <= limit:
                options.append(f"{lead} | {ctx.display_name}")
        else:
            options.append(hook)
            if topic and len(hook) + len(topic) + 3 <= limit:
                options.append(f"{hook} | {topic}")
            if len(hook) + len(ctx.display_name) + 3 <= limit:
                options.append(f"{hook} | {ctx.display_name}")
        if not options:
            options = [lead or hook]
        title = re.sub(r"\s+", " ", self.rng.choice(options)).strip()
        return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"

    trend_dir: Path | None = None  # where autocomplete trends are cached (None: no live trends)

    def _hashtags(self, ctx: ClipContext, platform: Platform, mood: Mood) -> list[str]:
        """Up to 15: game + action lead (shown above the title), #Shorts, the streamer, then the most-used
        tags for the game, live search trends and generic gaming tags. YouTube ignores *all* hashtags
        past 60 and treats unrelated ones as spam, so every tag here describes the clip."""
        from clipper.modules.actions import get as action_of
        from clipper.modules.games import popular_tags

        lead = "#Shorts" if platform == "youtube" else "#Reels"
        person = "#" + "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", ctx.display_name)) \
            if re.search(r"[A-Za-z]", ctx.display_name) else None
        act = action_of(ctx.action)
        if ctx.game:
            trending: list[str] = []
            if self.trend_dir is not None:
                from clipper.modules.trends import trending_hashtags

                trending = trending_hashtags(ctx.game, self.trend_dir)[0]
            tags = popular_tags(ctx.game, act.tags if act else (), tuple(trending))
            first, rest = tags[:1], tags[1:]
            act_tag = [x for x in rest if act and x in [a.replace("{G}", first[0].lstrip("#") if first else "")
                                                         for a in act.tags]][:1]
            ordered = [*first, *act_tag, lead, person, *rest]
            return _unique(ordered)[:15]
        topic = [_hashtag(ctx.category or "")]
        kw = [_hashtag(w) for w in keywords(ctx.transcript, 2)]
        acts = [a.replace("{G}", "") for a in act.tags[1:]] if act else []
        ordered = [lead, *topic, *acts, person, *kw, *_MOOD_TAGS[mood], "#Viral", "#Highlights", "#Streamer"]
        return _unique(ordered)[:12]

    def _from_story(self, ctx: ClipContext, platform: Platform, used_titles: set[str]) -> Metadata | None:
        """Title/description/hashtags written for this clip (modules.story); None → templates."""
        from clipper.modules.story import Story

        story = Story.from_json(ctx.story)
        if story is None or normalise_title(story.title) in used_titles:
            return None
        limit = 100 if platform == "youtube" else 255
        title = story.title if len(story.title) <= limit else story.title[: limit - 1] + "…"
        story_tags = [f"#{t}" for t in story.hashtags]
        game_tags: list[str] = []
        if ctx.game:
            from clipper.modules.games import popular_tags

            game_tags = popular_tags(ctx.game, (), ())[:4]
        lead = "#Shorts" if platform == "youtube" else "#Reels"
        # YouTube shows the first three hashtags above the title: the two most specific + #Shorts
        hashtags = _unique([*story_tags[:2], lead, *story_tags[2:], *game_tags])[:8]
        body = story.description
        if story.question and self.rng.random() < 0.35:  # a question under every post reads as a template
            body += f"\n\n{story.question}"
        description = f"{body}\n\n{' '.join(hashtags)}"
        tags = list(dict.fromkeys(t for t in [*story.hashtags, ctx.game, ctx.display_name if _real_name(ctx) else None,
                                              f"{ctx.game} highlights" if ctx.game else None, "shorts"] if t))
        while sum(len(t) + 1 for t in tags) > 450:
            tags.pop()
        return Metadata(title=title, description=description, tags=tags)

    def generate(self, ctx: ClipContext, platform: Platform, used_titles: set[str]) -> Metadata:
        """Generate metadata whose title does not collide with ``used_titles`` (normalised)."""
        if ctx.story and (meta := self._from_story(ctx, platform, used_titles)):
            return meta
        if not _real_name(ctx):  # never put an internal id ("yt_qsyo5d-dvza") in a title
            from dataclasses import replace

            ctx = replace(ctx, display_name=ctx.game or ctx.category or "Stream highlights")
        mood = self.mood(ctx)
        hook = ctx.hook or self.make_hook(ctx)
        title = self._title(ctx, hook, platform)
        for _ in range(20):
            if normalise_title(title) not in used_titles:
                break
            title = self._title(ctx, self.make_hook(ctx), platform)

        hashtags = self._hashtags(ctx, platform, mood)
        kw = keywords(ctx.transcript, 3)
        topic = ctx.category or (kw[0] if kw else "live")
        quote = self.quote(ctx.transcript)
        from clipper.modules.actions import get as action_of

        act = action_of(ctx.action)
        # one short line about the clip, then the hashtags (no source line, no boilerplate)
        if ctx.game and act:
            about = f"{ctx.display_name} in a {ctx.game} {act.label.lower()} {act.emoji}"
        elif ctx.game:
            about = f"{ctx.display_name} playing {ctx.game} 🎮"
        elif act:
            about = f"{ctx.display_name}: {act.label.lower()} {act.emoji}"
        else:
            about = f"{ctx.display_name}: {hook}"
        if quote and len(quote) <= 60 and quote.strip('"') not in title:
            about += f' — "{quote}"'
        if platform == "facebook":
            about = f"{title}\n{about}" if title not in about else about
        description = f"{about}\n\n{' '.join(hashtags)}"

        tags = list(dict.fromkeys(
            t for t in [ctx.game, f"{ctx.game} {act.label.lower()}" if ctx.game and act else None,
                        f"{ctx.display_name} {ctx.game}" if ctx.game else None,
                        f"{ctx.game} highlights" if ctx.game else None, *kw, ctx.category, ctx.display_name,
                        None if ctx.is_vod else f"{ctx.display_name} live",
                        f"{topic} highlights", "shorts", None if ctx.is_vod else "live highlights"] if t
        ))
        while sum(len(t) + 1 for t in tags) > 450:
            tags.pop()
        return Metadata(title=title, description=description, tags=tags)
