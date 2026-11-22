"""Discover: what's worth clipping right now.

* **Twitch** (official Helix API, needs a free app from https://dev.twitch.tv/console/apps):
  - ``live``: live streams ranked by current viewers (optionally one game/category, language)
  - ``clips``: the most-viewed clips of the past few days from the top live streamers;
    these are moments the audience already proved are highlights, and Twitch clip URLs feed
    straight into the video pipeline (yt-dlp supports them)
  - ``vods``: most-viewed past broadcasts of those streamers this week
* **YouTube** (Data API v3 with the existing login): live videos ranked by view count.
  ``search.list`` costs 100 quota units, so results are cached and only refreshed on demand.

Every result carries a ``score`` used to order recommendations, plus ``action``:
``watch`` (add as a live source) or ``clip`` (send to the video pipeline).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from clipper.config import Settings

log = logging.getLogger(__name__)
HELIX = "https://api.twitch.tv/helix"
CACHE_S = 20 * 60


@dataclass
class Pick:
    kind: str  # twitch_live | twitch_clip | twitch_vod | youtube_live
    action: str  # watch | clip
    url: str
    title: str
    channel: str
    thumbnail: str | None
    viewers: int | None = None  # live concurrent viewers
    views: int | None = None  # total views (clips/VODs)
    duration_s: float | None = None
    created_at: float | None = None
    category: str | None = None
    language: str | None = None
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _helix_duration(s: str) -> float:
    """'1h2m3s' → seconds."""
    total, num = 0.0, ""
    for ch in s or "":
        if ch.isdigit():
            num += ch
        else:
            total += int(num or 0) * {"h": 3600, "m": 60, "s": 1}.get(ch, 0)
            num = ""
    return total


def velocity_score(views: int, created: float | None, now: float) -> float:
    """Views per hour since publish, log-scaled (favours fresh momentum over old totals)."""
    hours = max(1.0, (now - (created or now)) / 3600)
    return round(math.log10(1 + views / hours) * 10, 2)


class DiscoverError(RuntimeError):
    pass


class Discover:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._token: tuple[str, float] | None = None
        self._cache: dict[str, tuple[float, list[Pick]]] = {}
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- twitch
    @property
    def twitch_ready(self) -> bool:
        return bool(self.s.twitch_client_id and self.s.twitch_client_secret)

    async def _twitch_token(self, client: httpx.AsyncClient) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        if not self.twitch_ready:
            raise DiscoverError("Add your Twitch app Client ID and Secret under Connections to use Twitch discovery.")
        assert self.s.twitch_client_secret is not None
        r = await client.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": self.s.twitch_client_id, "client_secret": self.s.twitch_client_secret.get_secret_value(),
            "grant_type": "client_credentials"})
        if r.is_error:
            raise DiscoverError(f"Twitch rejected the app credentials (HTTP {r.status_code}). Check Client ID/Secret.")
        body = r.json()
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    async def _helix(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        token = await self._twitch_token(client)
        r = await client.get(f"{HELIX}{path}", params=params,
                             headers={"Client-Id": str(self.s.twitch_client_id), "Authorization": f"Bearer {token}"})
        if r.status_code == 401:  # token revoked/expired early: refresh once
            self._token = None
            token = await self._twitch_token(client)
            r = await client.get(f"{HELIX}{path}", params=params,
                                 headers={"Client-Id": str(self.s.twitch_client_id), "Authorization": f"Bearer {token}"})
        if r.status_code == 429:
            raise DiscoverError("Twitch rate limit reached; try again in a minute.")
        if r.is_error:
            raise DiscoverError(f"Twitch API error HTTP {r.status_code}: {r.text[:160]}")
        return list(r.json().get("data", []))

    async def _game_id(self, client: httpx.AsyncClient, name: str) -> str | None:
        rows = await self._helix(client, "/games", {"name": name})
        return rows[0]["id"] if rows else None

    async def twitch_live(self, category: str | None = None, language: str | None = "en",
                          limit: int = 24) -> list[Pick]:
        async with httpx.AsyncClient(timeout=20) as c:
            params: dict[str, Any] = {"first": min(100, limit)}
            if language:
                params["language"] = language
            if category:
                gid = await self._game_id(c, category)
                if gid is None:
                    raise DiscoverError(f"No Twitch category named “{category}”.")
                params["game_id"] = gid
            rows = await self._helix(c, "/streams", params)
        now = time.time()
        picks = []
        for r in rows:
            started = _iso(r.get("started_at"))
            hours_live = (now - (started or now)) / 3600
            picks.append(Pick(
                kind="twitch_live", action="watch", url=f"https://twitch.tv/{r['user_login']}",
                title=r.get("title") or "", channel=r.get("user_name") or r["user_login"],
                thumbnail=(r.get("thumbnail_url") or "").replace("{width}", "440").replace("{height}", "248") or None,
                viewers=int(r.get("viewer_count", 0)), created_at=started, category=r.get("game_name"),
                language=r.get("language"),
                # big audience = busy chat = more spikes; long sessions have more chances to clip
                score=round(math.log10(1 + int(r.get("viewer_count", 0))) * 10 + min(hours_live, 6), 2)))
        return sorted(picks, key=lambda p: -p.score)

    async def _top_broadcasters(self, c: httpx.AsyncClient, category: str | None, language: str | None,
                                n: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"first": n}
        if language:
            params["language"] = language
        if category:
            gid = await self._game_id(c, category)
            if gid:
                params["game_id"] = gid
        return await self._helix(c, "/streams", params)

    async def twitch_clips(self, category: str | None = None, language: str | None = "en",
                           days: int = 3, streamers: int = 8, per_streamer: int = 4) -> list[Pick]:
        """Most-viewed recent clips from today's biggest live streamers."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")
        async with httpx.AsyncClient(timeout=20) as c:
            tops = await self._top_broadcasters(c, category, language, streamers)
            batches = await asyncio.gather(*(self._helix(c, "/clips", {
                "broadcaster_id": s["user_id"], "started_at": since, "first": per_streamer}) for s in tops))
        now = time.time()
        picks = []
        for rows in batches:
            for r in rows:
                created = _iso(r.get("created_at"))
                views = int(r.get("view_count", 0))
                picks.append(Pick(
                    kind="twitch_clip", action="clip", url=r["url"], title=r.get("title") or "",
                    channel=r.get("broadcaster_name") or "", thumbnail=r.get("thumbnail_url"),
                    views=views, duration_s=float(r.get("duration", 0)), created_at=created,
                    language=r.get("language"), score=velocity_score(views, created, now)))
        return sorted(picks, key=lambda p: -p.score)

    async def twitch_vods(self, category: str | None = None, language: str | None = "en",
                          streamers: int = 8) -> list[Pick]:
        """Most-viewed past broadcasts this week from today's biggest live streamers."""
        async with httpx.AsyncClient(timeout=20) as c:
            tops = await self._top_broadcasters(c, category, language, streamers)
            batches = await asyncio.gather(*(self._helix(c, "/videos", {
                "user_id": s["user_id"], "type": "archive", "sort": "views", "period": "week", "first": 2})
                for s in tops))
        now = time.time()
        picks = []
        for rows in batches:
            for r in rows:
                created = _iso(r.get("created_at"))
                views = int(r.get("view_count", 0))
                picks.append(Pick(
                    kind="twitch_vod", action="clip", url=r["url"], title=r.get("title") or "",
                    channel=r.get("user_name") or "",
                    thumbnail=(r.get("thumbnail_url") or "").replace("%{width}", "440").replace("%{height}", "248") or None,
                    views=views, duration_s=_helix_duration(r.get("duration", "")), created_at=created,
                    language=r.get("language"), score=velocity_score(views, created, now)))
        return sorted(picks, key=lambda p: -p.score)

    # ---------------------------------------------------------------- youtube
    async def youtube_live(self, query: str | None, quota: Any) -> list[Pick]:
        """Live YouTube videos by view count. Costs 101 quota units (search 100 + videos 1)."""
        from clipper.modules.publisher import pacific_day, youtube_credentials

        def run() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
            from googleapiclient.discovery import build

            yt = build("youtube", "v3", credentials=youtube_credentials(self.s), cache_discovery=False)
            found = yt.search().list(part="snippet", eventType="live", type="video", order="viewCount",
                                     q=query or "", maxResults=25, relevanceLanguage="en",
                                     safeSearch="moderate").execute(num_retries=2).get("items", [])
            ids = [i["id"]["videoId"] for i in found]
            details = {}
            if ids:
                for v in yt.videos().list(part="liveStreamingDetails,statistics", id=",".join(ids)).execute(
                        num_retries=2).get("items", []):
                    details[v["id"]] = v
            return found, details

        if quota is not None:
            day = pacific_day()
            await quota.add_quota("youtube", day, 101)
        found, details = await asyncio.to_thread(run)
        picks = []
        for item in found:
            vid = item["id"]["videoId"]
            sn = item["snippet"]
            det = details.get(vid, {})
            viewers = int(det.get("liveStreamingDetails", {}).get("concurrentViewers", 0) or 0)
            started = _iso(det.get("liveStreamingDetails", {}).get("actualStartTime"))
            picks.append(Pick(
                kind="youtube_live", action="watch", url=f"https://www.youtube.com/watch?v={vid}",
                title=sn.get("title", ""), channel=sn.get("channelTitle", ""),
                thumbnail=(sn.get("thumbnails", {}).get("medium") or {}).get("url"),
                viewers=viewers, created_at=started,
                score=round(math.log10(1 + viewers) * 10, 2)))
        return sorted(picks, key=lambda p: -p.score)

    # ------------------------------------------------------------------ cache
    async def find(self, source: str, *, query: str | None = None, category: str | None = None,
                   language: str | None = "en", refresh: bool = False, quota: Any = None,
                   cache_only: bool = False) -> dict[str, Any]:
        key = f"{source}|{query or ''}|{category or ''}|{language or ''}"
        async with self._lock:
            hit = self._cache.get(key)
            if hit and not refresh and time.time() - hit[0] < CACHE_S:
                return {"items": [p.to_dict() for p in hit[1]], "fetched_at": hit[0], "cached": True}
            if cache_only:  # never spend API quota implicitly
                return {"items": [], "fetched_at": None, "cached": False, "empty_cache": True}
            if source == "twitch_live":
                picks = await self.twitch_live(category, language)
            elif source == "twitch_clips":
                picks = await self.twitch_clips(category, language)
            elif source == "twitch_vods":
                picks = await self.twitch_vods(category, language)
            elif source == "youtube_live":
                picks = await self.youtube_live(query or category, quota)
            else:
                raise DiscoverError(f"unknown discovery source {source}")
            self._cache[key] = (time.time(), picks)
            log.info("discover %s: %d result(s)", source, len(picks))
            return {"items": [p.to_dict() for p in picks], "fetched_at": time.time(), "cached": False}
