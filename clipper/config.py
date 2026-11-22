"""Application configuration loaded from environment variables / ``.env``.

All settings are validated by Pydantic at startup so that misconfiguration fails
fast instead of surfacing hours later in the middle of a stream. Secrets are typed
as :class:`pydantic.SecretStr` so they never leak into logs or ``repr`` output.

List-valued settings are supplied as JSON in the environment, e.g.::

    CHAT_KEYWORDS='["LMAO", "POG", "W", "???"]'
    PUBLISH_PLATFORMS='["youtube"]'
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Platform = Literal["youtube", "facebook"]
# A posting account: the platform, optionally ":" + channel/Page id ("youtube", "facebook:1083…").
ACCOUNT_KEY = r"^(youtube|facebook)(:[A-Za-z0-9_-]{1,64})?$"


def account_platform(key: str) -> Platform:
    return "facebook" if key.split(":", 1)[0] == "facebook" else "youtube"
EncoderName = Literal["auto", "h264_nvenc", "h264_qsv", "h264_videotoolbox", "libx264"]
Signal = Literal["chat", "audio"]


Layout = Literal["center", "full", "fit", "cam_top", "cam_bottom"]
CaptionPosition = Literal["bottom", "middle", "top"]
RenderQuality = Literal["standard", "high", "2k"]
LayoutMode = Literal["fixed", "cycle", "random"]
FACECAM_PRESETS: dict[str, tuple[float, float, float, float]] = {
    # normalised (x, y, w, h) of the streamer's webcam inside the 16:9 source frame
    "top-left": (0.0, 0.0, 0.25, 0.30), "top-right": (0.75, 0.0, 0.25, 0.30),
    "bottom-left": (0.0, 0.70, 0.25, 0.30), "bottom-right": (0.75, 0.70, 0.25, 0.30),
    "top-left-large": (0.0, 0.0, 0.33, 0.40), "top-right-large": (0.67, 0.0, 0.33, 0.40),
    "bottom-left-large": (0.0, 0.60, 0.33, 0.40), "bottom-right-large": (0.67, 0.60, 0.33, 0.40),
}
MAX_CLIP_SECONDS = 180.0  # YouTube Shorts limit
FACEBOOK_MAX_SECONDS = 90.0  # Facebook Reels limit


class Facecam(BaseModel):
    """Where the streamer's webcam is in the source frame (fractions of width/height)."""

    x: float = Field(default=0.0, ge=0, le=1)
    y: float = Field(default=0.0, ge=0, le=1)
    w: float = Field(default=0.25, gt=0.02, le=1)
    h: float = Field(default=0.3, gt=0.02, le=1)

    @classmethod
    def preset(cls, name: str) -> Facecam:
        x, y, w, h = FACECAM_PRESETS[name]
        return cls(x=x, y=y, w=w, h=h)

    @model_validator(mode="after")
    def _inside(self) -> Facecam:
        self.w = min(self.w, 1 - self.x)
        self.h = min(self.h, 1 - self.y)
        return self


Voice = Literal["male_us", "female_us", "male_uk", "female_uk"]
# Kokoro voice ids (also the reference voice the expressive engine imitates)
VOICES: dict[str, str] = {"male_us": "am_michael", "female_us": "af_heart", "male_uk": "bm_george", "female_uk": "bf_emma"}


class RenderOptions(BaseModel):
    """How one clip looks. Stored per clip so it can be re-rendered later."""

    layout: Layout = "center"
    captions: bool = True
    caption_position: CaptionPosition = "bottom"
    hook: bool = True
    hook_text: str | None = Field(default=None, max_length=80, description="Override the generated hook.")
    quality: RenderQuality = "2k"  # 1440×2560: YouTube serves ≥1440p Shorts with its sharper codec
    facecam: Facecam | None = None
    # "Story" edit (modules/story.py + modules/voiceover.py): an AI-written hook and a short
    # spoken intro that gives viewers context, a flash-forward teaser of the payoff, punch-in
    # zoom + sound effects on the highlight and a progress bar. Off = the plain clip.
    story: bool = True
    voiceover: bool = True
    voice: Voice = "male_us"
    # Expressive voice (Chatterbox Turbo): laughs, chuckles, gasps and livelier delivery where the
    # story calls for it. Off (or not installed) = the steady Kokoro narrator.
    expressive: bool = True
    effects: bool = True


class ClipStyle(BaseModel):
    """Per-source / per-job defaults: render options + layout rotation + timing + limits."""

    render: RenderOptions = Field(default_factory=RenderOptions)
    layout_mode: LayoutMode = "fixed"
    layouts: list[Layout] = Field(default_factory=lambda: list[Layout](["center"]))
    clip_seconds: float | None = Field(default=None, ge=10, le=MAX_CLIP_SECONDS)
    # Each clip gets a random length between RANDOM_CLIP_MIN_S and clip_seconds (default 90 s), so a
    # channel's uploads don't all have the same running time. Off = exactly clip_seconds.
    random_length: bool = True
    max_clips_per_day: int | None = Field(default=None, ge=1, le=500)
    auto_post: bool = True
    # Account keys to post to: "youtube" / "facebook" (the first channel/Page) or
    # "youtube:<channel id>" / "facebook:<page id>". None → the accounts marked as default.
    platforms: list[Annotated[str, StringConstraints(pattern=ACCOUNT_KEY)]] | None = None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "", text).lower()[:40] or "source"


class SourceConfig(BaseModel):
    """One live stream (Twitch channel or YouTube live URL) to monitor.

    ``permission_confirmed`` is per source: only clips from sources you own or
    have the rights holder's permission for are ever queued for publishing;
    everything else is rendered locally only.
    """

    platform: Literal["twitch", "youtube"]
    channel: str | None = None  # twitch login
    url: str | None = None  # youtube watch/live URL
    display_name: str | None = None
    category: str | None = None
    quality: str = "best"
    signals: list[Signal] = Field(default_factory=lambda: list[Signal](["chat", "audio"]))
    permission_confirmed: bool = False
    paused: bool = False  # recording/clipping paused by the operator (source stays configured)
    style: ClipStyle = Field(default_factory=ClipStyle)

    @model_validator(mode="after")
    def _check(self) -> SourceConfig:
        if self.platform == "twitch":
            if not self.channel:
                raise ValueError("twitch source needs 'channel'")
            self.channel = self.channel.strip().lstrip("#").lower()
        elif not self.url:
            raise ValueError("youtube source needs 'url'")
        return self

    @property
    def source_url(self) -> str:
        return f"https://twitch.tv/{self.channel}" if self.platform == "twitch" else str(self.url)

    @property
    def slug(self) -> str:
        if self.platform == "twitch":
            return str(self.channel)
        raw = str(self.url).rstrip("/")
        raw = raw.split("v=")[-1] if "v=" in raw else raw.removesuffix("/live").split("/")[-1]
        return "yt_" + _slug(raw.split("&")[0])

    @property
    def name(self) -> str:
        return self.display_name or self.slug


def clipper_home() -> Path:
    """Where Clipper keeps its settings (.env) and data: ``$CLIPPER_HOME``, else ``~/Clipper``.

    A ``.env`` in the current folder is also read (and wins), so running from a source checkout
    works the same way as an installed copy."""
    return Path(os.path.expanduser(os.environ.get("CLIPPER_HOME") or "~/Clipper"))


def _sibling_script(name: str) -> str:
    """A console script installed in the same environment as Clipper, else the bare name (PATH)."""
    import sys

    exe = Path(sys.executable).parent / (f"{name}.exe" if os.name == "nt" else name)
    return str(exe) if exe.is_file() else name


def _venv_python(venv: str) -> Path:
    """The Python inside a tool virtualenv under storage/tools (Scripts\\python.exe on Windows)."""
    return Path("storage/tools") / venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class Settings(BaseSettings):
    """Typed, validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",  # plus <CLIPPER_HOME>/.env, resolved per load in __init__
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __init__(self, **values: object) -> None:
        # ~/Clipper/.env first, then ./.env (later files win); looked up now, not at import time
        values.setdefault("_env_file", (str(clipper_home() / ".env"), ".env"))
        super().__init__(**values)

    # ------------------------------------------------------------------ general
    log_level: str = "INFO"
    log_file: Path | None = Field(Path("storage/logs/clipper.log"), description="Rotating log file ('' to disable).")
    storage_dir: Path = Field(default_factory=lambda: clipper_home() / "storage")
    library_dir: Path | None = Field(
        None, description="Where clips and downloaded videos are saved (any folder or external drive). "
        "Usually set from the dashboard; default storage/library.")

    # ------------------------------------------------------------------- source
    sources: list[SourceConfig] = Field(
        default_factory=list,
        description='Multiple live sources as JSON, e.g. [{"platform":"twitch","channel":"x"},'
        '{"platform":"youtube","url":"https://www.youtube.com/@NASA/live","permission_confirmed":true}]. '
        "When empty, the single-source fields below are used.",
    )
    render_workers: int = Field(1, ge=1, le=8, description="Clips rendered in parallel.")
    vod_concurrency: int = Field(2, ge=1, le=8, description="VOD URLs downloaded/analysed in parallel.")
    vod_clips_per_video: int = Field(3, ge=1, le=20)
    random_clip_min_s: float = Field(30, ge=10, le=180, description="Shortest clip when a style uses random length.")
    random_clip_max_s: float = Field(90, ge=10, le=180, description="Longest random clip when no clip length is set.")
    vod_full_download_max_s: float = Field(
        1800, ge=60, description="Videos longer than this (and ended live streams) are not downloaded whole: "
                                 "only their audio is fetched to find the highlights, then just those sections.")
    vod_max_height: int = Field(4320, ge=360, le=4320, description="Highest source resolution downloaded for "
                                                                    "non-live videos (default: the best available, "
                                                                    "up to 8K; lower it to save disk/bandwidth).")
    clip_ended_streams: bool = Field(True, description="When a watched stream is not live (ended / a normal video), "
                                                        "clip its recording (VOD) instead of waiting forever.")
    audio_spike_sigma: float = Field(2.5, gt=0, description="Loudness z-score that counts as a highlight.")
    audio_min_gap_s: float = Field(60.0, ge=10)
    js_runtime: str = Field("node", description="JS runtime yt-dlp uses for YouTube (node or deno).")
    source_platform: Literal["twitch", "youtube"] = "twitch"
    twitch_channel: str | None = Field(None, description="Twitch login name (source_platform=twitch).")
    youtube_source_url: str | None = Field(
        None,
        description="YouTube live URL: a watch?v= URL or a channel '/@handle/live' URL "
        "(source_platform=youtube).",
    )
    youtube_chat_poll_s: float = Field(10.0, ge=2.0, description="Min seconds between chat polls.")
    youtube_chat_daily_budget: int = Field(
        3000, ge=0, description="Max quota units/day spent reading chat (5 units per poll)."
    )
    streamer_display_name: str | None = Field(
        None, description="Name used in titles/attribution. Defaults to the channel/source name."
    )
    stream_category: str | None = Field(None, description="Game/category used for hashtags.")
    stream_quality: str = "best"
    live_check_interval_s: int = Field(30, ge=10)
    youtube_live_check_interval_s: int = Field(
        90, ge=30, description="YouTube live checks (YouTube rate-limits page loads per internet address).")
    segment_duration_s: int = Field(60, ge=10, le=600)
    stall_timeout_s: int = Field(90, ge=30, description="Restart capture if no bytes for this long.")
    raw_retention_hours: float = Field(6.0, gt=0)
    rendered_retention_days: float = Field(14.0, ge=0, description="0 = keep clips forever.")
    streamlink_extra_args: list[str] = Field(default_factory=list)
    # Streamlink is installed next to Clipper (same environment), which isn't on PATH under pipx/uv tool
    streamlink_bin: str = Field(default_factory=lambda: _sibling_script("streamlink"))
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # --------------------------------------------------------------------- chat
    chat_ws_url: str = "wss://irc-ws.chat.twitch.tv:443"
    chat_nick: str = "justinfan88321"
    chat_bin_s: int = Field(10, ge=1)
    chat_baseline_window_s: int = Field(300, ge=60)
    chat_eval_interval_s: float = Field(2.0, gt=0)
    chat_spike_sigma: float = Field(2.5, gt=0)
    chat_min_std: float = Field(1.0, ge=0, description="Std-dev floor so dead chats don't trigger.")
    chat_min_baseline_bins: int = Field(12, ge=3, description="Warm-up bins before detecting.")
    chat_min_spike_messages: int = Field(8, ge=1)
    chat_min_spike_ratio: float = Field(
        1.8, ge=1.0, description="Velocity must also be ≥ this multiple of the baseline mean."
    )
    chat_keywords: list[str] = Field(default_factory=lambda: ["LMAO", "POG", "W", "???"])
    chat_keyword_density: float = Field(0.30, gt=0, le=1)
    chat_keyword_min_hits: int = Field(6, ge=1)
    clip_cooldown_s: int = Field(60, ge=0)

    # --------------------------------------------------------------------- clip
    # Shorter default window: the payoff comes ~12 s in (Shorts retention drops fast; the
    # flash-forward teaser covers the wait).
    clip_pre_roll_s: float = Field(13.0, ge=0)
    clip_post_roll_s: float = Field(10.0, ge=0)
    clip_min_duration_s: float = Field(10.0, gt=0)
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int | None = Field(None, description="Force output FPS (None = keep source).")
    foreground_aspect: float = Field(1.0, gt=0, description="Aspect of the center crop (1.0 = square: bigger "
                                                             "gameplay, less blurred background).")
    background_blur: str = "25:5"
    video_encoder: EncoderName = "auto"
    x264_preset: str = "fast"
    x264_crf: int = Field(19, ge=0, le=51)
    loudnorm: bool = True

    # ----------------------------------------------------------------- captions
    whisper_model: str = "small.en"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    caption_font: str = "Montserrat Black"
    caption_font_size: int = Field(96, ge=10)
    caption_primary_color: str = Field("#FFFF00", description="Spoken/highlighted word colour.")
    caption_secondary_color: str = Field("#FFFFFF", description="Upcoming word colour.")
    caption_outline_px: int = Field(6, ge=0)
    caption_max_words: int = Field(3, ge=1, le=8)
    caption_margin_v: int = Field(380, ge=0)
    caption_uppercase: bool = True
    hook_overlay: bool = Field(True, description="Burn the hook line into the top of the video.")
    hook_font_size: int = Field(82, ge=10)
    thumbnails: bool = Field(True, description="Generate and upload custom thumbnails.")
    thumbnail_font_path: str | None = Field(None, description="Bold TTF for thumbnails (auto-detected).")
    fonts_dir: Path = Field(Path("storage/fonts"), description="Extra fonts for burned-in captions (libass fontsdir).")

    # -------------------------------------------------------------------- story
    # Local LLM (Ollama) writes the hook, the spoken intro, title and description from the
    # transcript and source info. Falls back to the template hooks when Ollama isn't running.
    story_llm: bool = True
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_vision_model: str | None = Field("qwen3-vl:8b", description="Looks at frames of the clip so the "
                                            "story describes what is on screen (None/empty = transcript only).")
    story_timeout_s: float = Field(240, gt=0)
    story_think: bool = Field(True, description="Let the model reason before answering (slower, fewer made-up facts).")
    # Kokoro TTS (Apache-2.0 model) runs as a separate program in its own venv (it needs GPL
    # espeak-ng for pronunciation, which must never be linked into this process).
    tts_python: Path = _venv_python("tts-venv")
    tts_model_dir: Path = Path("storage/models/kokoro")
    tts_voice: str = Field("am_michael", description="Kokoro voice: am_michael/am_adam/af_heart (US), "
                                                     "bm_george/bm_fable/bf_emma (UK).")
    tts_speed: float = Field(1.1, ge=0.6, le=1.6)
    # Chatterbox Turbo (MIT) in its own venv: laughs/gasps/emotion; pins torch 2.6 / transformers.
    emotion_python: Path = _venv_python("emotion-venv")
    teaser_s: float = Field(1.6, ge=0, le=4, description="Flash-forward of the payoff before the clip (0 = off).")
    progress_bar: bool = True
    my_channel_name: str = Field("", description="Your name/brand, used as the creator of your own uploaded videos.")

    # --------------------------------------------------------------- publishing
    publish_platforms: list[Platform] = Field(
        default_factory=lambda: list[Platform](["youtube", "facebook"])
    )
    channel_permission_confirmed: bool = Field(
        False,
        description=(
            "Must be set true to publish. Confirms you own the channel or hold the rights "
            "holder's permission to repost its content."
        ),
    )
    token_encryption_key: SecretStr | None = Field(
        None, description="Fernet key (clipper gen-key) used to encrypt stored tokens."
    )
    max_posts_per_day: int = Field(3, ge=1, le=4)
    min_post_spacing_minutes: int = Field(180, ge=30)
    post_jitter_max_minutes: int = Field(45, ge=0)
    clip_max_queue_age_hours: float = Field(36.0, gt=0)
    publish_poll_interval_s: int = Field(60, ge=10)
    max_publish_attempts: int = Field(5, ge=1)

    youtube_client_id: SecretStr | None = None
    youtube_client_secret: SecretStr | None = None
    youtube_privacy_status: Literal["public", "unlisted", "private"] = "private"
    youtube_category_id: str = "20"
    youtube_daily_quota: int = Field(10_000, ge=1)
    youtube_quota_safety_margin: float = Field(0.10, ge=0, lt=1)
    youtube_upload_cost: int = Field(1600, ge=1, description="Quota units per videos.insert.")

    twitch_client_id: str | None = Field(default=None, description="Twitch app (dev.twitch.tv) for Discover.")
    twitch_client_secret: SecretStr | None = None

    facebook_app_id: str | None = None
    facebook_app_secret: SecretStr | None = None
    facebook_page_id: str | None = None
    facebook_graph_version: str = "v27.0"
    facebook_video_state: Literal["PUBLISHED", "DRAFT"] = "DRAFT"

    # ---------------------------------------------------------------- validators
    @field_validator("twitch_channel")
    @classmethod
    def _normalise_channel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lstrip("#").lower()
        if not value or not value.replace("_", "").isalnum():
            raise ValueError(f"invalid Twitch channel name: {value!r}")
        return value

    @field_validator("caption_primary_color", "caption_secondary_color")
    @classmethod
    def _validate_hex(cls, value: str) -> str:
        raw = value.lstrip("#")
        if len(raw) != 6 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            raise ValueError(f"expected #RRGGBB colour, got {value!r}")
        return f"#{raw.upper()}"

    @model_validator(mode="after")
    def _check_cross_field(self) -> Settings:
        # Paths given relative to "storage/" (the defaults) live inside STORAGE_DIR, wherever that is.
        for name in ("log_file", "tts_python", "tts_model_dir", "emotion_python", "fonts_dir"):
            value = getattr(self, name)
            if value is not None and not value.is_absolute() and value.parts[:1] == ("storage",):
                object.__setattr__(self, name, self.storage_dir.joinpath(*value.parts[1:]))
        if len({s.slug for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate sources in SOURCES")
        if self.min_post_spacing_minutes * self.max_posts_per_day > 24 * 60:
            raise ValueError("min_post_spacing_minutes * max_posts_per_day exceeds 24h")
        return self

    # ------------------------------------------------------------------- paths
    def all_sources(self) -> list[SourceConfig]:
        """Configured live sources (SOURCES, or the legacy single-source fields)."""
        if self.sources:
            return list(self.sources)
        if not (self.twitch_channel if self.source_platform == "twitch" else self.youtube_source_url):
            return []  # nothing in .env: streams are added from the dashboard
        return [SourceConfig(
            platform=self.source_platform, channel=self.twitch_channel, url=self.youtube_source_url,
            display_name=self.streamer_display_name, category=self.stream_category,
            quality=self.stream_quality, permission_confirmed=self.channel_permission_confirmed,
        )]

    def raw_dir_for(self, source: SourceConfig) -> Path:
        return self.storage_dir / "raw_streams" / source.slug

    @field_validator("storage_dir", "library_dir", mode="after")
    @classmethod
    def _absolute(cls, v: Path | None) -> Path | None:
        """Absolute paths, so stored clip paths stay valid whatever folder the app starts from."""
        return Path(os.path.abspath(os.path.expanduser(v))) if v is not None else None

    @property
    def default_library(self) -> Path:
        return self.storage_dir / "library"

    @property
    def library_root(self) -> Path:
        """The chosen library folder (may be on a drive that is currently unplugged)."""
        return Path(self.library_dir).expanduser() if self.library_dir else self.default_library

    def library_online(self) -> bool:
        root = self.library_root
        return root.is_dir() and os.access(root, os.W_OK)

    @property
    def active_library(self) -> Path:
        """Where new files go right now: the chosen library, or this Mac while its drive is away."""
        return self.library_root if self.library_online() else self.default_library

    @property
    def clips_dir(self) -> Path:
        return self.active_library / "Clips"

    @property
    def vod_dir(self) -> Path:
        return self.active_library / "Source videos"

    @property
    def source_url(self) -> str:
        """URL handed to Streamlink and used for attribution."""
        if self.source_platform == "youtube":
            return str(self.youtube_source_url)
        return f"https://twitch.tv/{self.twitch_channel}"

    @property
    def source_slug(self) -> str:
        """Filesystem-safe identifier for the source (directory and file names)."""
        if self.source_platform == "twitch":
            return str(self.twitch_channel)
        raw = str(self.youtube_source_url).rstrip("/")
        raw = raw.split("v=")[-1] if "v=" in raw else raw.removesuffix("/live").split("/")[-1]
        return "yt_" + re.sub(r"[^A-Za-z0-9_-]+", "", raw.split("&")[0]).lower()

    @property
    def display_name(self) -> str:
        return self.streamer_display_name or self.source_slug

    @property
    def raw_dir(self) -> Path:
        return self.storage_dir / "raw_streams" / self.source_slug

    @property
    def work_dir(self) -> Path:
        return self.storage_dir / "work"

    @property
    def rendered_dir(self) -> Path:
        return self.storage_dir / "rendered"

    @property
    def state_dir(self) -> Path:
        return self.storage_dir / "state"

    @property
    def secrets_dir(self) -> Path:
        return self.storage_dir / "secrets"

    def ensure_dirs(self) -> None:
        """Create every storage directory the application writes to."""
        for path in (self.storage_dir / "raw_streams", self.work_dir, self.rendered_dir, self.state_dir,
                     self.default_library):
            path.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()  # type: ignore[call-arg]  # populated from env
