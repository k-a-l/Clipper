# Third-Party Components

This application is built by combining maintained open-source tools with custom
orchestration and business logic. This file records every integrated project,
the alternatives that were evaluated and rejected, and the remaining custom code
with the reason it exists.

**Policy**

1. Before writing new functionality, search GitHub and PyPI for a mature solution.
2. Assess each candidate's maintenance (recent releases and commits, open issues),
   license, dependencies and security.
3. Install published packages; don't vendor or clone repositories.
4. **No third-party source code has been copied into this repository.**
   All integration is through package imports, CLI subprocesses or HTTP APIs.
5. Only permissive licenses (MIT / BSD / Apache-2.0) are linked in-process.
   Copyleft tools (FFmpeg's GPL build) run only as separate executables.

Evaluation date: **2026-11-18**. Sources: PyPI JSON API (versions, release dates,
declared licenses), GitHub API (stars, last push, archive status, SPDX license),
and READMEs and issue trackers where a capability had to be confirmed.

---

## 1. Integrated Python packages

| Component | Repository | License | Version (evaluated) | Pinned range | Purpose | Integration method | Used in |
|---|---|---|---|---|---|---|---|
| **bgutil-ytdlp-pot-provider** | https://github.com/Brainicism/bgutil-ytdlp-pot-provider | GPL-3.0 (separate program + yt-dlp plugin; not linked into this code) | 2.0.2 | YouTube PO tokens so downloads look like a real player (fewer "confirm you're not a bot" blocks) | Node helper on 127.0.0.1:4416 (optional; run it as a background service, see the plugin's README) + pip plugin `bgutil-ytdlp-pot-provider==2.0.2` picked up by yt-dlp automatically | yt-dlp downloads |
| **Streamlink** | https://github.com/streamlink/streamlink | BSD-2-Clause | 8.6.3 (2026-10-29) | `>=8.0,<9` | Live detection and stream download for **Twitch and YouTube** (ad filtering, reconnects and HLS handled by its plugins) | **Python API** (`Streamlink().streams()`) for the 30 s live check; **CLI subprocess** (`--stdout`) for capture so a hung download can always be killed | `stream_recorder.py` |
| **OpenCLIP** | https://github.com/mlfoundations/open_clip | MIT | 4.2.2 (ViT-B-32, LAION-2B weights) | optional | What happens in a clip (police chase, racing, fight…) for titles and hashtags | Package import in a worker thread, MPS GPU | `actions.py` |
| **PyTorch / torchvision** | https://github.com/pytorch/pytorch | BSD-3-Clause | 2.15.0 / 0.29.2 | optional | Runs OpenCLIP | dependency | `actions.py` |
| **OpenCV (opencv-python-headless)** | https://github.com/opencv/opencv-python | Apache-2.0 | 4.14.0.94 (5.1 evaluated: no bundled Haar cascades) | `>=4.12,<5` | Face detection, Sobel edges and image I/O for webcam auto-detect | Package import, run in a worker thread | `facecam.py` |
| **YuNet face model** | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet | MIT | `face_detection_yunet_2023mar.onnx` (sha256 `8f2383e4…52fa4`, 227 KB) | vendored in `models/` | Fast, angle-tolerant face detector (Haar cascades are the fallback and missed tilted faces in testing) | `cv2.FaceDetectorYN` | `facecam.py` |
| **typed-ffmpeg** | https://github.com/lucemia/typed-ffmpeg | MIT | 4.6.2 (2026-10-21) | `>=4.6,<5` | Builds FFmpeg filter graphs and argv with filter and option names checked against FFmpeg's schema, and automatic filter escaping; `probe()` wraps ffprobe JSON | Package import (`import ffmpeg`). We call `.compile()` and run the argv with our own async runner (timeouts, cancellation). Pulls in `typed-ffmpeg-v9` and `ffmpeg-core` (both MIT, same authors) | `video_editor.py`, `utils.py` |
| **faster-whisper** | https://github.com/SYSTRAN/faster-whisper | MIT | 1.2.1 (2025-10-31) | `>=1.2,<2` | Speech-to-text with word-level timestamps and VAD (CTranslate2 backend, CPU int8 / GPU) | Package import; lazy model load; inference in a worker thread | `subtitles.py` |
| **pysubs2** | https://github.com/tkarabela/pysubs2 | MIT | 1.9.1 (2026-10-30) | `>=1.9,<2` | ASS document model: styles, colours, events, timestamps, serialization | Package import (`SSAFile`, `SSAStyle`, `SSAEvent`). Replaced a hand-written ASS writer | `subtitles.py` |
| **yt-dlp** (`[default]` extra incl. yt-dlp-ejs) | https://github.com/yt-dlp/yt-dlp | Unlicense (ejs: Unlicense AND MIT AND ISC) | 2026.10.26 | `>=2026.10.26` | Download non-live URLs (YouTube, Twitch VODs, ~1800 sites) capped at 1080p; YouTube JS challenges solved with the configured runtime (`JS_RUNTIME=node`) | **CLI subprocess** (`python -m yt_dlp`). Importing it in-process monkey-patches `urllib3` and broke Streamlink's live checks in end-to-end testing (2026-11-19); a child process also isolates hangs | `vod.py`; `yt_auth.py` (`extract_cookies_from_browser`, child process, for the YouTube sign-in that fixes "confirm you're not a bot") |
| **FastAPI** | https://github.com/fastapi/fastapi | MIT | 0.165.0 | `>=0.115,<1` | Local dashboard HTTP API, request validation (pydantic), static media with HTTP range support (via Starlette) | Package import; app built in `web.py`, served in the daemon's own event loop | `web.py` |
| **uvicorn** | https://github.com/Kludex/uvicorn | BSD-3-Clause | 0.54.0 | `>=0.30,<1` | ASGI server for the dashboard | `uvicorn.Server(...).serve()` as a task next to the daemon; its signal capture disabled so the daemon owns Ctrl+C | `clipper/main.py` |
| **Pillow** | https://github.com/python-pillow/Pillow | MIT-CMU (HPND) | 13.0.0 (2026-10-16) | `>=11.0,<14` | Thumbnail composition: resize, contrast/saturation/auto-exposure, gradient, outlined text rendering, JPEG size control | Package import (`PIL.Image`, `ImageDraw`, `ImageEnhance`, `ImageFont`) | `thumbnail.py` |
| **tenacity** | https://github.com/jd/tenacity | Apache-2.0 | 9.2.1 (2026-10-07) | `>=9.0,<10` | Retry engine: stop/wait/retry policies, full-jitter exponential backoff | `AsyncRetrying` in `retry_async()`, plus a small custom wait strategy that honors server `Retry-After` hints | `utils.py`, `publisher.py` (Meta Graph calls) |
| **google-api-python-client** | https://github.com/googleapis/google-api-python-client | Apache-2.0 | 2.202.0 (2026-11-10) | `>=2.130,<3` | YouTube Data API v3: `videos.insert` resumable upload, and `videos.list` + `liveChatMessages.list` for reading YouTube live chat when the source is a YouTube stream | Package import. **Built-in retry** `next_chunk(num_retries=8)` handles 5xx, 429, rate-limit 403s and connection errors (replaced a custom loop) | `publisher.py` |
| **google-auth** | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth | Apache-2.0 | 2.62.2 (2026-11-10) | `>=2.29,<3` | OAuth 2.0 credentials and automatic access-token refresh | `Credentials.from_authorized_user_info()` + `.refresh()` | `publisher.py` |
| **google-auth-oauthlib** | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth-oauthlib | Apache-2.0 | 1.5.0 (2026-09-29) | `>=1.2,<2` | One-time installed-app consent flow (offline access → refresh token) | `InstalledAppFlow.run_local_server()` in `auth-youtube` command | `publisher.py` |
| **google-auth-httplib2** | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth-httplib2 | Apache-2.0 | 0.4.4 (2026-10-01) | `>=0.2,<1` | Auth transport used by the discovery client | Transitive runtime requirement, pinned explicitly | `publisher.py` |
| **httpx** | https://github.com/encode/httpx | BSD-3-Clause | 0.28.1 (2024-12-06) | `>=0.27,<1` | Async HTTP client for the Meta Graph / rupload APIs | `httpx.AsyncClient` | `publisher.py` |
| **websockets** | https://github.com/python-websockets/websockets | BSD-3-Clause | 17.4 (2026-10-30) | `>=13.0,<18` | WebSocket transport (TLS, framing, keepalive) for Twitch IRC | `websockets.asyncio.client.connect` | `chat_analyzer.py` |
| **pydantic** / **pydantic-settings** | https://github.com/pydantic/pydantic, https://github.com/pydantic/pydantic-settings | MIT | 2.14.3 / 2.16.3 | `>=2.7,<3` / `>=2.3,<3` | Typed, validated configuration from env / `.env`; `SecretStr` keeps secrets out of logs | `BaseSettings` subclass | `config.py` |
| **cryptography** | https://github.com/pyca/cryptography | Apache-2.0 OR BSD-3-Clause | 50.0.2 (2026-09-30) | `>=42` | Fernet authenticated encryption of stored OAuth tokens | `cryptography.fernet.Fernet` | `utils.py` |
| **tzdata** | https://github.com/python/tzdata | Apache-2.0 | 2026.6 | `>=2024.1` | IANA zone data so `zoneinfo` works in slim containers (YouTube quota resets at midnight Pacific) | Data package used by stdlib `zoneinfo` | `publisher.py` |

Notable transitive dependencies (not imported directly): `ctranslate2` 4.9.0 (MIT,
faster-whisper backend), `onnxruntime` 1.32.0 (MIT, Silero VAD), `av` (PyAV) 19.0.2
(BSD-3-Clause), `numpy`, `httplib2` 0.32.1 (MIT), Streamlink's `lxml`,
`pycryptodome`, `requests`, `trio` (BSD/MIT/Apache).

**Known incompatibility (found in end-to-end testing, 2026-11-19):** faster-whisper
1.2.1's built-in decoder calls `av.open(..., metadata_errors=...)`, which PyAV 19
removed, so transcribing a *file path* raises `TypeError`. We avoid that path: FFmpeg
extracts 16 kHz mono WAV, read with stdlib `wave` into a NumPy array, which is then
passed to `transcribe()`. PyAV is still installed as a faster-whisper dependency but
not used. Re-check when faster-whisper releases a version compatible with PyAV 19.

## 2. System and runtime components

| Component | Source | License | Version | Purpose | Integration method |
|---|---|---|---|---|---|
| **FFmpeg** (ffmpeg, ffprobe) | https://ffmpeg.org (Debian `ffmpeg` package in Docker) | LGPL-2.1+; Debian's build enables GPL parts (e.g. libx264) → GPL-2+ | Debian bookworm package | Lossless segmenting (`-c copy`), decode, filtering, H.264/AAC encode | **Separate executable** invoked via subprocess (no linking, so no license propagation to our code) |
| **libass** | https://github.com/libass/libass | ISC | bundled with the FFmpeg build | Renders ASS subtitles (`ass` filter) | Via FFmpeg |
| **Whisper `small.en` weights (CTranslate2 conversion)** | https://huggingface.co/Systran/faster-whisper-small.en | MIT | downloaded at first run to `storage/models/` | ASR model | Loaded by faster-whisper |
| **DejaVu fonts** | https://dejavu-fonts.github.io (Debian `fonts-dejavu-core`) | Bitstream Vera / public-domain changes (free) | Debian package | Caption font | System font found by libass |
| **Unbounded** typeface | https://github.com/googlefonts/unbounded | SIL OFL 1.1 (`brand/fonts/OFL.txt`) | variable, wght 200–900 | Brand wordmark, covers, intro titles | Font file in `brand/fonts/`, rendered by Pillow in `brand/make_brand.py`; downloaded from the official google/fonts repo |
| **tini** | https://github.com/krallin/tini | MIT | Debian package | PID 1 in Docker; forwards SIGTERM so FFmpeg finalizes segments | `ENTRYPOINT` |
| **python:3.12-slim** | https://hub.docker.com/_/python | PSF + Debian licenses | 3.12 | Base image | `FROM` |
| **Ollama** | https://github.com/ollama/ollama | MIT | macOS app (upgraded 2026-11-20) | Local LLM server for the clip "story" (hook, spoken intro, title, description) and frame descriptions; nothing leaves the Mac | **HTTP API** on 127.0.0.1:11434 (`/api/chat` with JSON-schema structured output, `/api/show` for vision capability) via httpx. Falls back to template hooks if not running |
| **Qwen3 8B** (text) | https://ollama.com/library/qwen3 | Apache-2.0 | `qwen3:8b` | Writes the story; `think` mode on (fewer invented facts in testing) | Served by Ollama (`OLLAMA_MODEL`) |
| **Qwen3-VL 8B** (vision) | https://ollama.com/library/qwen3-vl | Apache-2.0 | `qwen3-vl:8b` | Describes 4 frames around the highlight so the story says what is on screen | Served by Ollama (`OLLAMA_VISION_MODEL`); optional |
| **kokoro-onnx** | https://github.com/thewh1teagle/kokoro-onnx | MIT (its deps phonemizer + espeak-ng are **GPL-3.0**) | 0.6.1 | Runs the Kokoro TTS model for the spoken intro | **Separate program**: own virtualenv `storage/tools/tts-venv`, invoked as `tools/tts_kokoro.py` subprocess (text in, WAV out). Never imported by Clipper, so the GPL deps are not linked in-process |
| **Kokoro-82M v1.0** model + voices | https://huggingface.co/hexgrad/Kokoro-82M (ONNX export: https://huggingface.co/fastrtc/kokoro-onnx) | Apache-2.0 | `kokoro-v1.0.onnx` sha256 `7d5df8ec…6a6c5`, `voices-v1.0.bin` sha256 `bca610b8…f1fbf7d` | Natural US/UK English voices (`am_michael` default; `bm_george`, `af_heart`, `bf_emma`…) | Files in `storage/models/kokoro/`. Downloaded from the HF mirror because GitHub release downloads ran at ~20 KB/s; hashes match the mirror's LFS records |
| **Chatterbox Turbo** (`chatterbox-tts`) | https://github.com/resemble-ai/chatterbox, model https://huggingface.co/ResembleAI/chatterbox-turbo | MIT (code and model) | 0.1.7, model ~2.3 GB on disk | Expressive voiceover: performs `[laugh]`, `[chuckle]`, `[gasp]`, `[sigh]`, `[groan]` tags; imitates the chosen Kokoro voice from a 10 s reference clip so male/female US/UK stay consistent. Output carries Resemble's inaudible Perth watermark (marks it as AI audio) | **Separate program**: own virtualenv `storage/tools/emotion-venv` (pins torch 2.6 / transformers 5.2, which clash with Clipper's), invoked as `tools/tts_chatterbox.py`. Falls back to Kokoro on any failure. Installed from `clipper/tools/emotion-requirements.lock.txt` (every package pinned to its official PyPI/PyTorch SHA-256; `setuptools<81` because Perth still imports `pkg_resources`) |
| **Montserrat Black** typeface | https://github.com/JulietaUla/Montserrat | SIL OFL 1.1 (`brand/fonts/Montserrat-OFL.txt`) | static TTF | Burned-in captions and hook (the heavy rounded sans used by most top Shorts channels) | Font file in `brand/fonts/`, passed to libass via the `ass` filter's `fontsdir` |

## 3. Evaluated and not adopted

| Module | Candidate | Repository | License | Finding | Decision |
|---|---|---|---|---|---|
| Chat | **TwitchIO 3.x** | https://github.com/TwitchIO/TwitchIO | MIT | Actively maintained (3.3.2, 2026-08), but v3 is built on **EventSub** and requires OAuth app and user tokens; anonymous `justinfan` IRC access (a hard requirement) is not supported | Rejected |
| Chat | **chat-downloader** | https://github.com/xenova/chat-downloader | MIT | Last release 0.2.8 (2023-09); several open, recent "Twitch chat downloading fails" bugs (2025-10 → 2026-10) | Rejected (stale, broken for Twitch) |
| Chat (YouTube) | **chat-downloader** | https://github.com/xenova/chat-downloader | MIT | **Tested live 2026-11-19:** `ParsingError: Unable to parse initial video data` on a live YouTube stream (matches open issue from 2026-01) | Rejected; official API used instead |
| Chat (YouTube) | pytchat | https://github.com/taizan-hokuto/pytchat | MIT | Repository **archived**, last release 2021-07 | Rejected |
| Chat (YouTube) | yt-chat-downloader | PyPI `yt-chat-downloader` 0.0.3 | MIT (declared) | Project URL is a template placeholder (`github.com/yourusername/...`); provenance unverifiable | Rejected (trust) |
| Chat (YouTube) | YouTube Data API `liveChatMessages.streamList` | https://developers.google.com/youtube/v3/live/docs/liveChatMessages/streamList | Google APIs ToS | Server-streaming variant, same 5-unit cost; not exposed by the discovery-based Python client | Deferred; `list` polling used |
| Chat | Twitch-Chat-Downloader | https://github.com/PetterKraabol/Twitch-Chat-Downloader | MIT | VOD chat only (no live), last push 2022 | Rejected |
| Highlight detection | twitch-highlights-bot | https://github.com/viniciusenari/twitch-highlights-bot | **GPL-3.0** | Compiles existing Twitch clips via Helix API; not live chat detection; copying would impose GPL | Rejected; no code used |
| Highlight detection | twitch-highlights | https://github.com/pelledrijver/twitch-highlights | Custom (NOASSERTION) | Helix clip compilation; unclear license; last push 2023 | Rejected |
| Highlight detection | twitch-highlight-detection | https://github.com/dscig/twitch-highlight-detection | **None** (all rights reserved) | Research code, last push 2021, no license grants reuse | Rejected; no code used |
| Full app | AI-Youtube-Shorts-Generator | https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator | MIT | Well-starred, but a script app for pre-recorded videos (LLM picks highlights, MoviePy); no live capture, chat signal, or publishing safeguards; not a library | Reviewed for ideas only; not integrated |
| Full app | ClippedAI | https://github.com/Shaarav4795/ClippedAI | Custom (NOASSERTION) | Similar VOD-to-shorts app; non-standard license | Rejected |
| FFmpeg | **ffmpeg-python** | https://github.com/kkroening/ffmpeg-python | Apache-2.0 | Named in the original spec, but last release 0.2.0 is from **2019**, ~478 open issues, and its import name `ffmpeg` clashes with typed-ffmpeg | Rejected in favor of typed-ffmpeg |
| FFmpeg | PyAV | https://github.com/PyAV-Org/PyAV | BSD-3-Clause | Excellent, but bundles a second copy of FFmpeg libraries in its wheels; we only need ffprobe metadata | Not needed |
| FFmpeg | ffmpeg-progress-yield | https://github.com/slhck/ffmpeg-progress-yield | MIT (per PyPI) | Progress reporting only; renders are ~35 s clips | Not needed now; easy to add |
| Highlights (audio) | librosa | https://github.com/librosa/librosa | ISC | Excellent, but adds numba/scipy/soundfile; FFmpeg's built-in `ebur128` filter already gives EBU R128 short-term loudness every 100 ms | Not needed |
| Highlights (audio) | pyloudnorm | https://github.com/csteinmetz1/pyloudnorm | MIT | Integrated loudness only (no time series without extra windowing code) | Not needed |
| Clip boundaries | PySceneDetect | https://github.com/Breakthrough/PySceneDetect | BSD-3-Clause | Maintained; could snap clip start/end to scene cuts | Deferred (nice-to-have) |
| Webcam detect | MediaPipe | https://github.com/google/mediapipe | Apache-2.0 | Heavier (7 deps), its face task still needs a separately downloaded model; OpenCV + YuNet does the same in one wheel | Not used |
| Discover | twitchAPI | https://github.com/Teekeks/pyTwitchAPI | MIT | Maintained async Helix wrapper, but we use only 4 GET endpoints + client-credentials token; ~60 lines on the existing httpx client avoids another dependency | Not needed (Helix called directly) |
| Dashboard | NiceGUI | https://github.com/zauberzeug/nicegui | MIT | Capable, but owns the page model and websockets; we need a thin API over a daemon we already control | Rejected |
| Dashboard | Streamlit / Gradio | https://github.com/streamlit/streamlit, https://github.com/gradio-app/gradio | Apache-2.0 | Script-rerun / demo models don't fit a long-running controller (state lives outside, actions must not re-run code) | Rejected |
| Dashboard | sse-starlette | https://github.com/sysid/sse-starlette | BSD-3-Clause | Log streaming is ~15 lines with FastAPI's built-in `StreamingResponse` | Not needed |
| Captions | **stable-ts** | https://github.com/jianfch/stable-ts | MIT | Can emit karaoke ASS directly, but the repository is **archived** (2026-05) and pulls in PyTorch | Rejected (archived) |
| Captions | whisperX | https://github.com/m-bain/whisperX | BSD-2-Clause | Better word alignment (wav2vec2), but requires PyTorch and pyannote (GBs) and ~180 open issues | Deferred; possible optional upgrade if timing quality is insufficient |
| Publishing | facebook-business (Meta Business SDK) | https://github.com/meta/facebook-python-business-sdk | Custom Meta license (NOASSERTION) | Official and maintained, but synchronous (`requests`), large, and lacks a helper for the Reels `rupload` binary step; we need only 3 Graph calls | Rejected; thin httpx client instead |
| Publishing | facebook-sdk | https://github.com/mobolic/facebook-sdk | Apache-2.0 | Last release 2018 | Rejected (unmaintained) |
| Publishing | youtube-upload | https://github.com/tokland/youtube-upload | Not detected by GitHub | CLI wrapper, last push 2024-04, 106 open issues; license unclear | Rejected |
| Publishing | Selenium/Playwright uploaders (e.g. youtube_uploader_selenium) | https://github.com/linouk23/youtube_uploader_selenium | MIT | Browser automation bypasses official APIs: fragile, and conflicts with platform terms and account-safety goals | Rejected on compliance grounds |
| Keywords | YAKE | https://github.com/INESCTEC/yake | **LGPL-3.0** | Maintained, but copyleft; violates our permissive-only in-process policy | Rejected |
| Keywords | rake-nltk | https://github.com/csurfer/rake-nltk | MIT | Last release 2021-09; needs NLTK corpus downloads at runtime | Rejected (stale) |
| Keywords | KeyBERT | https://github.com/MaartenGr/KeyBERT | MIT | Good quality but pulls in sentence-transformers + PyTorch (GBs) for 35-second transcripts | Rejected (weight) |
| Copywriting | Anthropic SDK (Claude) | https://github.com/anthropics/anthropic-sdk-python | Apache-2.0 | Would write the best per-clip titles/captions, but needs an API key and wasn't testable in this environment | Deferred; add when `ANTHROPIC_API_KEY` is available |
| Retry | backoff | https://github.com/litl/backoff | MIT | Repository **archived**; last release 2022-10 | Rejected (tenacity is maintained) |
| Retry | stamina | https://github.com/hynek/stamina | MIT | Well maintained opinionated wrapper around tenacity; we need a custom wait (server `Retry-After`), which plain tenacity supports directly | Not needed |
| Queue | huey (SqliteHuey) | https://github.com/coleifer/huey | MIT | Mature task queue with retries and ETA, but adds a consumer process model and has no notion of per-platform daily caps, minimum spacing, or score-ranked selection | Rejected; see custom scheduler |
| Queue | APScheduler 3.x | https://github.com/agronholm/apscheduler | MIT | Time-based job scheduler, not a durable work queue with ranking/expiry | Rejected |
| Queue | persist-queue | https://github.com/peter-wangxu/persist-queue | BSD-3-Clause | FIFO/ack queues; no priority, delayed retry or cadence | Rejected |
| Rate limiting | aiolimiter | https://github.com/mjpieters/aiolimiter | MIT | Leaky bucket for req/s; our limits are posts per day plus spacing, and quota units per Pacific day | Not applicable |
| Secrets | keyring | https://github.com/jaraco/keyring | MIT (per PyPI) | Needs an OS secret-service backend, which headless Docker lacks | Rejected; Fernet + env key |
| TTS | kokoro (official) | https://github.com/hexgrad/kokoro | Apache-2.0 | Needs torch + transformers + misaki[en], which still pulls GPL phonemizer/espeak-ng | Rejected for kokoro-onnx in a separate venv (lighter; same model) |
| TTS | Piper (piper1-gpl) | https://github.com/OHF-Voice/piper1-gpl | GPL-3.0 | Usable as a CLI, but noticeably more robotic English voices than Kokoro | Not adopted |
| Sound effects | Freesound / Pixabay SFX packs | — | CC0 / custom licenses per file | Per-file license tracking for 2 sounds isn't worth it | Synthesised with FFmpeg `anoisesrc`/`sine` instead (`voiceover.sfx`) |
| LLM | Ollama `qwen2.5:1.5b` (already installed) | — | Apache-2.0 | Too weak: generic hooks, invented facts | Replaced by `qwen3:8b` |

## 4. Custom code and why it exists

| Custom component | File | Why no library |
|---|---|---|
| Spike detector (rolling 10 s velocity vs 5-min baseline mean/σ, relative floor, keyword density, cooldown) | `chat_analyzer.py` | Application-specific business logic; no maintained library implements chat-velocity highlight detection |
| Anonymous Twitch IRC client (~50 lines: NICK/JOIN, PING/PONG, RECONNECT, PRIVMSG parse) | `chat_analyzer.py` | TwitchIO v3 requires OAuth/EventSub; chat-downloader is broken for Twitch. Transport, TLS and framing come from `websockets` |
| YouTube live-chat reader (`/live` URL → video ID, `activeLiveChatId`, polling at `max(pollingIntervalMillis, YOUTUBE_CHAT_POLL_S)`, skip backlog page, 5-unit cost charged to the shared `youtube` quota ledger plus a capped `youtube_chat` ledger) | `chat_analyzer.py` | Every evaluated scraper is broken, archived or untrustworthy; the official API is reliable but quota-metered, so the budget logic is ours. Handles chat-disabled streams (e.g. NASA ISS) by pausing detection |
| Capture orchestration (streamlink→ffmpeg pipe, UTC wall-clock segment names, stall watchdog, reconnect backoff, ffprobe integrity check and quarantine, retention janitor) | `stream_recorder.py` | Glue between Streamlink and FFmpeg specific to mapping chat timestamps onto recordings |
| Vertical layout recipe and hardware-encoder probing with libx264 fallback | `video_editor.py` | Product-specific composition; the graph itself is built with typed-ffmpeg |
| Word grouping, karaoke `\k` timing, pop-in animation | `subtitles.py` | Styling and business logic; the file format is handled by pysubs2 |
| Publishing scheduler and SQLite queue (per-platform daily cap ≤ 4, min spacing + jitter, score ranking, expiry, quota accounting by Pacific day, auth-failure pause, crash recovery) | `publisher.py` | Cadence and compliance rules are the product's core policy; evaluated queue libraries (§3) don't model them |
| Meta Reels flow (start → rupload → finish → status poll, `appsecret_proof`, `X-App-Usage`/`X-Business-Use-Case-Usage` cooldowns) | `publisher.py` | No maintained async SDK covers the Reels upload protocol; built on httpx + tenacity |
| Metadata generator: one hook reused on video/thumbnail/title; chat-based hooks only when chat actually spiked; YouTube (≤60-char titles, keyword-first description, subscribe CTA, 3-5 hashtags) vs Facebook (hook + genuine question, avoiding Meta's demoted engagement-bait phrasing); title de-duplication; attribution | `metadata.py` | Content-specific business logic |
| Keyword extractor (~15 lines: stopword-filtered frequency, acronyms boosted) | `metadata.py` | Evaluated libraries are copyleft, stale or GB-scale (above) |
| Audio highlight detector (EBU R128 short-term loudness from FFmpeg `ebur128=framelog=info`, z-score vs rolling 2-min baseline, min-gap peak picking; live per-segment with carried-over history, VOD whole-file top-N) | `audio_highlights.py` | No library does excitement detection; this is ~80 lines over FFmpeg's meter. Note: `framelog=verbose` emits nothing at default log level (bug found in testing) |
| Multi-source orchestration (per-source recorder/chat/audio, shared event queue, N render workers, per-source cooldown merge, per-source publish permission) and VOD batch processing | `clipper/main.py`, `pipeline.py`, `config.py` | Application-specific orchestration |
| Runtime controller (add/remove sources and VOD jobs while running, persisted `sources.json`, single-instance lock, log ring buffer) and the dashboard (vanilla HTML/CSS/JS, no build step, no external requests; localhost-only with Host check + per-session token) | `daemon.py`, `web.py`, `static/index.html` | Application-specific; a framework-free page keeps it dependency-free and offline |
| Thumbnail frame scoring (Laplacian-variance sharpness, exposure, Hasler-Süsstrunk colourfulness) on clean layout frames around the spike moment | `thumbnail.py`, `video_editor.py` | Three numpy formulas; no library needed. Composition itself uses Pillow |
| Encrypted token store (atomic write, 0600 mode) | `utils.py` | ~40-line wrapper; cryptography's Fernet does the encryption |
| `backoff_delay` (full-jitter formula) | `utils.py` | Used by reconnect loops that reset backoff after a healthy period, which tenacity doesn't model; one line of maths |
| Story prompt, JSON schema, validation and **grounding check** (names/numbers/family words in the output must appear in the transcript, frame description or source info; uses the system word list to tell names from words) | `story.py` | Content policy specific to this channel; no library checks LLM output against a transcript this way |
| Story edit: flash-forward teaser, punch-in zoom + flash on the highlight, progress bar, voiceover ducking, SFX mix | `video_editor.py` (`Effects`), `pipeline.py` | Product-specific editing recipe; the graph is built with typed-ffmpeg |
| Upload endpoint (raw-body streaming to disk, no multipart dependency) and local-file clipping | `web.py`, `vod.py` | ~30 lines; avoids adding python-multipart |
| Post performance report (`clipper stats`) | `stats.py` | Thin wrapper over the YouTube Data API / Graph API calls already in use |

## 5. License compliance notes

* Everything imported in-process is MIT, BSD or Apache-2.0, compatible with
  each other and with any license this project chooses (including proprietary).
* Apache-2.0 components (tenacity, Google libraries, cryptography, tzdata) carry
  NOTICE/attribution obligations when redistributed; keep this file and the
  packages' license files (shipped inside the installed distributions) with any
  distributed image.
* FFmpeg in the Docker image is Debian's GPL-enabled build. It runs as a separate
  program, which does not make this application a derivative work. If you
  redistribute the image, the GPL's source-offer obligations for the Debian
  packages apply (Debian provides the source).
* GPL-3.0, custom-licensed and unlicensed repositories in §3 were reviewed **by README and metadata
  only**; no code from them is used.
* The TTS program (kokoro-onnx + GPL-3.0 phonemizer/espeak-ng) lives in its own virtualenv and
  runs as a separate process, the same arrangement as FFmpeg.

## 6. Keeping this current

* Re-run the evaluation when bumping a major version or every ~6 months:
  `pip index versions <pkg>`, `gh repo view <owner/repo> --json pushedAt,isArchived`.
* Run `pip-audit (on an environment with Clipper installed)` (https://github.com/pypa/pip-audit, Apache-2.0)
  in CI for known vulnerabilities.
* Update the version column and the pinned ranges in `pyproject.toml` together.
