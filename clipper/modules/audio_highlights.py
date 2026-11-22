"""Audio-based highlight detection (works without chat: VODs, chat-disabled streams).

Loudness comes from FFmpeg's built-in EBU R128 meter (``ebur128`` filter), which
reports short-term loudness (3 s window) every 100 ms, so no extra dependency is
needed. A highlight is a moment whose short-term loudness rises well above the
surrounding baseline: crowd/streamer reactions, shouting, cheering, explosions.

``find_peaks`` is pure (unit-testable); ``loudness_series`` wraps FFmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from clipper.config import Settings, SourceConfig
from clipper.modules.chat_analyzer import ClipEvent
from clipper.modules.stream_recorder import StreamRecorder
from clipper.modules.utils import run_cmd

log = logging.getLogger(__name__)
_EBU_RE = re.compile(r"t:\s*([\d.]+).*?S:\s*(-?[\d.]+|-inf)")


@dataclass(frozen=True)
class Peak:
    t: float  # seconds from media start
    loudness: float  # LUFS (short-term)
    z: float  # standard deviations above the local baseline


async def loudness_series(ffmpeg_bin: str, media: Path, step_s: float = 0.5,
                          timeout: float = 1800) -> list[tuple[float, float]]:
    """Return ``[(t, short_term_LUFS), ...]`` sampled every ``step_s`` seconds."""
    res = await run_cmd(
        [ffmpeg_bin, "-hide_banner", "-nostats", "-i", str(media), "-vn",
         "-af", "ebur128=framelog=info", "-f", "null", "-"],
        timeout=timeout, check=False,
    )
    series: list[tuple[float, float]] = []
    next_t = 0.0
    for m in _EBU_RE.finditer(res.stderr):
        t = float(m.group(1))
        # Short-term loudness needs a full 3 s window; earlier samples read as silence and
        # would inflate the baseline variance (seen on every live segment in testing).
        if t < 3.0 or t + 1e-6 < next_t:
            continue
        lufs = -70.0 if m.group(2) == "-inf" else max(-70.0, float(m.group(2)))
        series.append((t, lufs))
        next_t = t + step_s
    if not series and res.returncode != 0:
        raise RuntimeError(f"ebur128 analysis failed for {media.name}: {res.stderr[-300:]}")
    return series


def find_peaks(series: list[tuple[float, float]], *, sigma: float = 2.5, baseline_s: float = 120.0,
               min_gap_s: float = 60.0, min_lufs: float = -40.0, top_n: int | None = None,
               history: list[float] | None = None) -> list[Peak]:
    """Moments whose loudness exceeds ``mean + sigma*std`` of the preceding ``baseline_s``.

    ``history`` (optional) seeds the baseline with earlier loudness values, used for
    live segments so each segment is judged against the stream's recent past.
    Peaks are separated by at least ``min_gap_s``; with ``top_n`` the strongest win.
    """
    if not series:
        return []
    step = (series[-1][0] - series[0][0]) / max(1, len(series) - 1) or 0.5
    n_base = max(10, int(baseline_s / step))
    window: deque[float] = deque(history[-n_base:] if history else [], maxlen=n_base)
    candidates: list[Peak] = []
    for t, lufs in series:
        if len(window) >= min(n_base, 20):
            base = np.fromiter(window, dtype=np.float64)
            mean, std = float(base.mean()), max(float(base.std()), 1.5)  # LU floor: ignore tiny wobbles
            z = (lufs - mean) / std
            if z >= sigma and lufs >= min_lufs:
                candidates.append(Peak(t, lufs, round(z, 2)))
        window.append(lufs)
    # Keep strongest peaks with minimum spacing.
    chosen: list[Peak] = []
    for p in sorted(candidates, key=lambda p: p.z, reverse=True):
        if all(abs(p.t - c.t) >= min_gap_s for c in chosen):
            chosen.append(p)
        if top_n is not None and len(chosen) >= top_n:
            break
    return sorted(chosen, key=lambda p: p.t)


def top_moments(series: list[tuple[float, float]], n: int, min_gap_s: float, edge_s: float = 20.0,
                duration: float | None = None) -> list[Peak]:
    """For VODs: the ``n`` loudest-relative moments, relaxing ``sigma`` until enough are found."""
    end = duration if duration is not None else (series[-1][0] if series else 0.0)
    usable = [(t, v) for t, v in series if edge_s <= t <= end - 15]
    for sigma in (3.0, 2.5, 2.0, 1.5, 1.0, 0.5):
        peaks = find_peaks(usable, sigma=sigma, min_gap_s=min_gap_s, top_n=n)
        if len(peaks) >= n:
            return peaks
    return peaks


class AudioSpikeWatcher:
    """Live: analyse each finished recording segment and emit ClipEvents on loudness spikes."""

    def __init__(self, settings: Settings, source: SourceConfig, recorder: StreamRecorder,
                 events: asyncio.Queue[ClipEvent], clock: Callable[[], float] = time.time) -> None:
        self.s = settings
        self.source = source
        self.recorder = recorder
        self.events = events
        self.clock = clock
        self._done: set[Path] = set()
        self._history: list[float] = []
        self._last_event = 0.0

    async def run(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self._scan()
            except Exception:
                log.exception("[%s] audio scan failed", self.source.slug)

    async def _scan(self) -> None:
        now = self.clock()
        finished = [seg for seg in self.recorder.recent_segments(max_age_s=600)
                    if seg.path not in self._done and now - seg.end > 5]
        for seg in finished:
            self._done.add(seg.path)
            series = await loudness_series(self.s.ffmpeg_bin, seg.path, timeout=120)
            peaks = find_peaks(series, sigma=self.s.audio_spike_sigma, min_gap_s=self.s.audio_min_gap_s,
                               history=self._history)
            self._history = (self._history + [v for _, v in series])[-2000:]
            for p in peaks:
                at = seg.start + p.t
                if at - self._last_event < self.s.clip_cooldown_s:
                    continue
                self._last_event = at
                log.info("[%s] AUDIO SPIKE z=%.1f at %.0f (%.1f LUFS)", self.source.slug, p.z, at, p.loudness)
                self.events.put_nowait(ClipEvent(
                    trigger_time=at, reason="audio", velocity=0, baseline_mean=0.0, baseline_std=1.0,
                    source_id=self.source.slug, audio_z=p.z,
                ))
