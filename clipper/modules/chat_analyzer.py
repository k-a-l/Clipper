"""Module 2 — anonymous Twitch chat monitoring and highlight (spike) detection.

Connects to Twitch IRC over WebSocket as an anonymous ``justinfanNNNN`` user (read
only, no OAuth token) and feeds every chat message to :class:`SpikeDetector`.

Detection model
---------------
* **Velocity** — messages in the trailing ``chat_bin_s`` (10 s) window, evaluated
  every ``chat_eval_interval_s`` so spikes are caught quickly (a rolling bin).
* **Baseline** — non-overlapping 10 s bins over the previous ``chat_baseline_window_s``
  (5 min), *excluding* the bins overlapping the current window. Mean and standard
  deviation are computed over those bins (silent bins count as zero).
* **Velocity trigger** — ``velocity > mean + sigma * max(std, min_std)``, plus
  ``velocity >= chat_min_spike_messages`` and ``velocity >= mean * chat_min_spike_ratio``.
  The relative floor suppresses false positives from ordinary noise, since the
  window is re-evaluated every couple of seconds.
* **Keyword trigger** — share of messages in the window containing a hype keyword
  (``LMAO``, ``POG``, ``W``, ``???``) ≥ ``chat_keyword_density`` with at least
  ``chat_keyword_min_hits`` matching messages, and chat not quieter than baseline.
* A ``clip_cooldown_s`` cooldown after every trigger prevents overlapping clips.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from clipper.config import Settings, SourceConfig
from clipper.modules.utils import backoff_delay

log = logging.getLogger(__name__)

TriggerReason = Literal["velocity", "keywords", "velocity+keywords", "audio"]

_PRIVMSG_RE = re.compile(r"^(?:@\S+ )?:(?P<nick>[^!\s]+)!\S+ PRIVMSG #\S+ :(?P<msg>.*)$")


@dataclass(frozen=True)
class ClipEvent:
    """A detected highlight moment."""

    trigger_time: float  # unix epoch seconds
    reason: TriggerReason
    velocity: int
    baseline_mean: float
    baseline_std: float
    keyword_hits: dict[str, int] = field(default_factory=dict)
    sample_messages: tuple[str, ...] = ()
    source_id: str = ""  # SourceConfig.slug of the stream this happened on
    audio_z: float = 0.0  # loudness z-score for audio-triggered events

    @property
    def zscore(self) -> float:
        return (self.velocity - self.baseline_mean) / max(self.baseline_std, 1e-6)

    @property
    def score(self) -> float:
        """Ranking score used by the publisher to post the best clips first (comparable across signals)."""
        signal = 2.0 * self.audio_z if self.reason == "audio" else min(self.zscore, 50.0)
        return round(signal + 0.5 * sum(self.keyword_hits.values()), 3)


# --------------------------------------------------------------------- keywords
class KeywordMatcher:
    """Matches hype keywords with rules that suit chat slang.

    * Non-alphanumeric keywords (``???``) match as substrings.
    * Single-letter keywords (``W``) must be a whole token (avoids matching "WHAT").
    * Longer words match token prefixes, so ``POG`` matches ``POGGERS``/``PogChamp``
      and ``LMAO`` matches ``LMAOOO``.
    """

    def __init__(self, keywords: list[str]) -> None:
        self.keywords = [k.upper() for k in keywords if k.strip()]

    def match(self, message: str) -> set[str]:
        upper = message.upper()
        tokens = re.findall(r"[A-Z0-9]+", upper)
        hits: set[str] = set()
        for kw in self.keywords:
            if not kw.isalnum():
                if kw in upper:
                    hits.add(kw)
            elif len(kw) == 1:
                if kw in tokens:
                    hits.add(kw)
            elif any(tok.startswith(kw) for tok in tokens):
                hits.add(kw)
        return hits


# --------------------------------------------------------------------- detector
class SpikeDetector:
    """Pure, clock-injectable spike detector (no I/O — easy to unit test)."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.s = settings
        self.clock = clock
        self.bin_s = settings.chat_bin_s
        self.n_baseline_bins = max(1, settings.chat_baseline_window_s // settings.chat_bin_s)
        self.matcher = KeywordMatcher(settings.chat_keywords)
        self._window: deque[tuple[float, str, frozenset[str]]] = deque()
        self._bins: Counter[int] = Counter()
        self._first_bin: int | None = None
        self._last_trigger = -math.inf

    def add_message(self, text: str, ts: float | None = None) -> None:
        ts = self.clock() if ts is None else ts
        idx = int(ts // self.bin_s)
        if self._first_bin is None:
            self._first_bin = idx
        self._bins[idx] += 1
        self._window.append((ts, text, frozenset(self.matcher.match(text))))

    @property
    def velocity(self) -> int:
        """Messages currently in the rolling window."""
        self._prune(self.clock())
        return len(self._window)

    def baseline(self, now: float) -> tuple[float, float, int]:
        """Return ``(mean, std, n_bins)`` over closed bins preceding the current window."""
        last_closed = int((now - self.bin_s) // self.bin_s) - 1
        first = max(last_closed - self.n_baseline_bins + 1, self._first_bin or last_closed + 1)
        counts = [self._bins.get(i, 0) for i in range(first, last_closed + 1)]
        if not counts:
            return 0.0, 0.0, 0
        mean = sum(counts) / len(counts)
        var = sum((c - mean) ** 2 for c in counts) / len(counts)
        return mean, math.sqrt(var), len(counts)

    def evaluate(self, now: float | None = None) -> ClipEvent | None:
        """Check the current rolling window; return a :class:`ClipEvent` if it spiked."""
        now = self.clock() if now is None else now
        self._prune(now)

        if now - self._last_trigger < self.s.clip_cooldown_s:
            return None
        mean, std, n = self.baseline(now)
        if n < self.s.chat_min_baseline_bins:
            return None  # still warming up

        velocity = len(self._window)
        threshold = mean + self.s.chat_spike_sigma * max(std, self.s.chat_min_std)
        velocity_hit = (
            velocity > threshold
            and velocity >= self.s.chat_min_spike_messages
            and velocity >= mean * self.s.chat_min_spike_ratio
        )

        kw_counter: Counter[str] = Counter()
        kw_messages = 0
        for _, _, hits in self._window:
            if hits:
                kw_messages += 1
                kw_counter.update(hits)
        density = kw_messages / velocity if velocity else 0.0
        keyword_hit = (
            kw_messages >= self.s.chat_keyword_min_hits
            and density >= self.s.chat_keyword_density
            and velocity >= mean
        )

        if not (velocity_hit or keyword_hit):
            return None

        reason: TriggerReason = (
            "velocity+keywords" if velocity_hit and keyword_hit
            else "velocity" if velocity_hit else "keywords"
        )
        self._last_trigger = now
        samples = tuple(text for _, text, _ in list(self._window)[-8:])
        return ClipEvent(
            trigger_time=now, reason=reason, velocity=velocity,
            baseline_mean=round(mean, 2), baseline_std=round(std, 2),
            keyword_hits=dict(kw_counter), sample_messages=samples,
        )

    def _prune(self, now: float) -> None:
        horizon = now - self.bin_s
        while self._window and self._window[0][0] < horizon:
            self._window.popleft()
        oldest_keep = int(now // self.bin_s) - self.n_baseline_bins - 3
        for idx in [i for i in self._bins if i < oldest_keep]:
            del self._bins[idx]


# ------------------------------------------------------------------ IRC client
class TwitchChatClient:
    """Minimal, resilient read-only Twitch IRC-over-WebSocket client."""

    def __init__(self, settings: Settings, on_message: Callable[[str, str], None], channel: str) -> None:
        self.s = settings
        self.on_message = on_message
        self.channel = channel

    async def run(self) -> None:
        """Connect forever, reconnecting with jittered backoff on any failure."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        attempt = 0
        while True:
            connected_at = time.monotonic()
            try:
                async with connect(self.s.chat_ws_url, open_timeout=20, ping_interval=60) as ws:
                    await ws.send("CAP REQ :twitch.tv/commands")
                    await ws.send(f"NICK {self.s.chat_nick}")
                    await ws.send(f"JOIN #{self.channel}")
                    log.info("joined chat #%s anonymously as %s", self.channel, self.s.chat_nick)
                    async for frame in ws:
                        text = frame if isinstance(frame, str) else frame.decode(errors="replace")
                        for line in text.split("\r\n"):
                            if not line:
                                continue
                            if line.startswith("PING"):
                                await ws.send("PONG :tmi.twitch.tv")
                            elif " RECONNECT" in line[:80]:
                                log.info("server requested reconnect")
                                break
                            elif (m := _PRIVMSG_RE.match(line)) is not None:
                                self.on_message(m["nick"], m["msg"])
                        else:
                            continue
                        break  # RECONNECT
            except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
                log.warning("chat connection error: %s", exc)

            attempt = 0 if time.monotonic() - connected_at > 60 else attempt + 1
            delay = backoff_delay(max(attempt, 1), base=1, cap=60)
            log.info("reconnecting to chat in %.1fs", delay)
            await asyncio.sleep(delay)


# ------------------------------------------------------------ YouTube chat
class ChatUnavailable(Exception):
    """The stream has no readable live chat (disabled, members-only, or ended)."""


class QuotaLedger(Protocol):
    async def quota_used(self, platform: str, day: str) -> int: ...
    async def add_quota(self, platform: str, day: str, units: int) -> None: ...


_WATCH_ID_RE = re.compile(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})")
_CANONICAL_RE = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"')


class YouTubeChatClient:
    """Reads public YouTube live chat through the official Data API v3.

    Evaluated alternatives (see THIRD_PARTY_COMPONENTS.md): chat-downloader fails
    with "Unable to parse initial video data" and pytchat is archived, so the
    supported API is used despite its quota cost: ``liveChatMessages.list`` is
    5 units per call. Every call is charged to the shared ``youtube`` ledger
    (so uploads see the true remaining quota) and to a ``youtube_chat`` ledger
    capped at ``youtube_chat_daily_budget`` per Pacific day.
    """

    CALL_COST = 5
    _GONE = {"liveChatEnded", "liveChatNotFound", "liveChatDisabled", "forbidden"}

    def __init__(self, settings: Settings, on_message: Callable[[str, str], None],
                 quota: QuotaLedger | None, url: str | None = None) -> None:
        self.s = settings
        self.on_message = on_message
        self.quota = quota
        self.url = url or str(settings.youtube_source_url)

    async def resolve_video_id(self) -> str:
        """Accept watch/youtu.be URLs directly; resolve channel '/live' pages via their canonical link."""
        url = self.url
        if (m := _WATCH_ID_RE.search(url)) is not None:
            return m.group(1)
        import httpx

        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}) as c:
            page = (await c.get(url)).text
        if (m := _CANONICAL_RE.search(page)) is None:
            raise ChatUnavailable(f"no live video found at {url}")
        return m.group(1)

    async def _call(self, build: Callable[[Any], Any], cost: int) -> dict[str, Any]:
        from clipper.modules.publisher import pacific_day, youtube_credentials

        def run() -> dict[str, Any]:
            from googleapiclient.discovery import build as discovery_build

            yt = discovery_build("youtube", "v3", credentials=youtube_credentials(self.s), cache_discovery=False)
            return build(yt).execute(num_retries=3)  # client's built-in backoff for 5xx/429

        if self.quota is not None:
            day = pacific_day()
            await self.quota.add_quota("youtube", day, cost)
            await self.quota.add_quota("youtube_chat", day, cost)
        return await asyncio.to_thread(run)

    async def _budget_ok(self) -> bool:
        if self.quota is None:
            return True
        from clipper.modules.publisher import pacific_day

        return await self.quota.quota_used("youtube_chat", pacific_day()) + self.CALL_COST <= self.s.youtube_chat_daily_budget

    async def run(self) -> None:
        from googleapiclient.errors import HttpError

        from clipper.modules.publisher import AuthError, next_pacific_midnight

        attempt = 0
        while True:
            try:
                video_id = await self.resolve_video_id()
                info = await self._call(lambda yt: yt.videos().list(part="liveStreamingDetails", id=video_id), 1)
                items = info.get("items") or []
                chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None
                if not chat_id:
                    raise ChatUnavailable(f"video {video_id} has no active live chat (disabled or not live)")
                log.info("reading YouTube live chat for %s (≤%d units/day)", video_id, self.s.youtube_chat_daily_budget)
                await self._poll(chat_id)
                attempt = 0
            except ChatUnavailable as exc:
                log.warning("YouTube chat unavailable: %s — highlight detection paused; re-checking in 10 min", exc)
                await asyncio.sleep(600)
            except AuthError as exc:
                log.error("YouTube chat needs authorisation (run auth-youtube): %s", exc)
                await asyncio.sleep(3600)
            except HttpError as exc:
                reason = _yt_reason(exc)
                if reason in {"quotaExceeded", "dailyLimitExceeded"}:
                    wait = next_pacific_midnight() - time.time()
                    log.warning("YouTube quota exhausted; chat paused %.1fh", wait / 3600)
                    await asyncio.sleep(wait)
                    continue
                attempt += 1
                delay = backoff_delay(attempt, base=5, cap=300)
                log.warning("YouTube chat error %s %s; retry in %.0fs", exc.resp.status, reason, delay)
                await asyncio.sleep(delay)

    async def _poll(self, chat_id: str) -> None:
        from googleapiclient.errors import HttpError

        from clipper.modules.publisher import next_pacific_midnight

        page_token: str | None = None
        first = True
        while True:
            if not await self._budget_ok():
                wait = next_pacific_midnight() - time.time()
                log.warning("YouTube chat budget (%d units) spent today; paused %.1fh",
                            self.s.youtube_chat_daily_budget, wait / 3600)
                await asyncio.sleep(wait)
                continue
            try:
                resp = await self._call(lambda yt: yt.liveChatMessages().list(
                    liveChatId=chat_id, part="snippet", pageToken=page_token, maxResults=2000), self.CALL_COST)
            except HttpError as exc:
                if _yt_reason(exc) in self._GONE:
                    raise ChatUnavailable(_yt_reason(exc)) from exc
                raise
            page_token = resp.get("nextPageToken")
            if not first:  # the first page is backlog, not live reaction
                for item in resp.get("items", []):
                    snippet = item.get("snippet", {})
                    if snippet.get("type") == "textMessageEvent":
                        self.on_message("", snippet.get("displayMessage", ""))
            first = False
            if resp.get("offlineAt"):
                raise ChatUnavailable("stream ended")
            await asyncio.sleep(max(self.s.youtube_chat_poll_s, resp.get("pollingIntervalMillis", 0) / 1000))


def _yt_reason(exc: Any) -> str:
    import json

    try:
        return str(json.loads(exc.content)["error"]["errors"][0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


# ---------------------------------------------------------------- orchestrator
class ChatAnalyzer:
    """Wires the source's chat client into the detector and publishes :class:`ClipEvent` s."""

    STATS_INTERVAL_S = 60

    def __init__(
        self,
        settings: Settings,
        events: asyncio.Queue[ClipEvent],
        is_recording: Callable[[], bool],
        quota: QuotaLedger | None = None,
        source: SourceConfig | None = None,
    ) -> None:
        self.s = settings
        self.source = source or settings.all_sources()[0]
        self.events = events
        self.is_recording = is_recording
        self.detector = SpikeDetector(settings)
        self.client: TwitchChatClient | YouTubeChatClient = (
            YouTubeChatClient(settings, self._on_message, quota, url=self.source.url)
            if self.source.platform == "youtube"
            else TwitchChatClient(settings, self._on_message, str(self.source.channel))
        )

    def _on_message(self, _nick: str, message: str) -> None:
        self.detector.add_message(message)

    async def run(self) -> None:
        client_task = asyncio.create_task(self.client.run(), name=f"{self.source.slug}-chat")
        last_stats = time.monotonic()
        try:
            while True:
                await asyncio.sleep(self.s.chat_eval_interval_s)
                if client_task.done():
                    client_task.result()  # surface unexpected crashes
                if time.monotonic() - last_stats >= self.STATS_INTERVAL_S:
                    last_stats = time.monotonic()
                    mean, std, n = self.detector.baseline(time.time())
                    log.info("[" + self.source.slug + "] chat: %d msg/%ds now, baseline mean %.1f std %.1f over %d bins%s",
                             self.detector.velocity, self.s.chat_bin_s, mean, std, n,
                             "" if n >= self.s.chat_min_baseline_bins else " (warming up)")
                event = self.detector.evaluate()
                if event is None:
                    continue
                if not self.is_recording():
                    log.debug("spike ignored: not recording (%s)", event.reason)
                    continue
                event = replace(event, source_id=self.source.slug)
                log.info(
                    "[" + self.source.slug + "] CLIP EVENT %s: %d msg/%ds (mean %.1f, std %.1f, z %.1f) keywords=%s",
                    event.reason, event.velocity, self.s.chat_bin_s,
                    event.baseline_mean, event.baseline_std, event.zscore, event.keyword_hits,
                )
                try:
                    self.events.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("clip queue full — dropping event")
        finally:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await client_task
