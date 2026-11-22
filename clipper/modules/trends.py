"""What people are searching right now: YouTube autocomplete for a game ("gta 6 trailer 3",
"gta 6 map"…), cached a day. Turns into extra tags and a couple of trending hashtags.

Uses Google's public suggest endpoint (no key, no personal data sent).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)
_URL = "https://suggestqueries.google.com/complete/search"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; clip-tool/1.0)"}
_BAD = re.compile(r"\b(?:download|free|apk|crack|mod menu|torrent|leak\w*|nude|porn|price|release|pc requirements|"
                  r"trailer\w*|news|countdown|date|reaction|announcement|delay\w*|launch|preorder|pre order)\b")


def suggestions(query: str, cache_dir: Path, max_age: float = 86400) -> list[str]:
    cache = cache_dir / "trends.json"
    data: dict[str, Any] = {}
    try:
        data = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
    except ValueError:
        data = {}
    hit = data.get(query.lower())
    if hit and time.time() - float(hit.get("at", 0)) < max_age:
        return list(hit.get("s", []))
    try:
        r = httpx.get(_URL, params={"client": "firefox", "ds": "yt", "q": query}, headers=_UA, timeout=6)
        out = [s for s in r.json()[1] if isinstance(s, str)]
    except (httpx.HTTPError, ValueError, IndexError, KeyError):
        log.debug("autocomplete failed for %r", query)
        return list(hit.get("s", [])) if hit else []
    out = [s for s in out if not _BAD.search(s.lower())][:10]
    data[query.lower()] = {"at": time.time(), "s": out}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return out


def trending_hashtags(game: str, cache_dir: Path, k: int = 3) -> tuple[list[str], list[str]]:
    """(hashtags, plain tags) from what people type after the game name, e.g. #GTA6Trailer3."""
    sugg = [s for s in suggestions(game.lower(), cache_dir) if not _BAD.search(s.lower())]
    base = re.sub(r"\s+", "", game)
    tags, plain = [], []
    for s in sugg:
        rest = s.lower().replace(game.lower(), "").strip()
        words = [w for w in re.findall(r"[a-z0-9]+", rest) if w not in {"the", "a", "in", "of", "and", "vs"}]
        if words in (["video"], ["game"]):
            continue
        if not words or len(words) > 2:
            plain.append(s)
            continue
        tags.append("#" + base + "".join(w[:1].upper() + w[1:] for w in words))
        plain.append(s)
    return tags[:k], plain[:8]
