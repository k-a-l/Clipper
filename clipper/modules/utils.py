"""Shared helpers: logging, subprocess execution, retry/backoff, encrypted token storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


# --------------------------------------------------------------------- logging
class _UpperLevelFormatter(logging.Formatter):
    """Streamlink registers lowercase level names globally ("info"); normalise them."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = record.levelname.upper()
        return super().format(record)


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Log to stdout and (optionally) a rotating file: 10 MB x 5, so `tail -f` always works."""
    from logging.handlers import RotatingFileHandler

    fmt = _UpperLevelFormatter("%(asctime)s %(levelname)-7s %(name)-24s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)
    # Third-party libraries are chatty at INFO.
    for noisy in ("httpx", "googleapiclient.discovery_cache", "websockets", "faster_whisper",
                  "ffmpeg_core", "streamlink"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ subprocess
class CommandError(RuntimeError):
    """Raised when an external command exits non-zero."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        tail = stderr.strip().splitlines()[-15:]
        super().__init__(f"{args[0]} exited {returncode}:\n" + "\n".join(tail))
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


async def run_cmd(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a command asynchronously, capturing output.

    The child is killed if ``timeout`` elapses or the awaiting task is cancelled,
    so no orphan ffmpeg processes survive a shutdown.
    """
    log.debug("exec: %s", " ".join(map(str, args)))
    proc = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    result = CommandResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
    if check and result.returncode != 0:
        raise CommandError(list(map(str, args)), result.returncode, result.stderr)
    return result


async def ffprobe_duration(ffprobe_bin: str, path: Path, timeout: float = 30) -> float | None:
    """Return a media file's duration in seconds, or ``None`` if unreadable.

    Uses typed-ffmpeg's ``probe`` (ffprobe JSON) in a worker thread.
    """
    import subprocess

    import ffmpeg  # typed-ffmpeg

    try:
        info = await asyncio.to_thread(ffmpeg.probe, path, cmd=ffprobe_bin, timeout=int(timeout))
        return float(info["format"]["duration"])
    except (ffmpeg.FFMpegExecuteError, subprocess.TimeoutExpired, OSError, KeyError, ValueError, TypeError):
        return None


# ----------------------------------------------------------------------- retry
class RetryableError(Exception):
    """A failure that is expected to succeed on retry (HTTP 429/5xx, network blips)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    """Full-jitter exponential delay for hand-written reconnect loops.

    Used only where a loop must *reset* its backoff after a healthy period
    (stream capture, chat connection, job rescheduling) — a policy tenacity
    doesn't model. Same formula as tenacity's ``wait_random_exponential``.
    """
    return max(0.5, random.uniform(0, min(cap, base * 2 ** max(0, attempt - 1))))


class _wait_retry_after:
    """tenacity wait strategy: jittered exponential, but never shorter than a server hint."""

    def __init__(self, base: float, cap: float) -> None:
        from tenacity import wait_random_exponential

        self._fallback = wait_random_exponential(multiplier=base / 2, max=cap)
        self._cap = cap

    def __call__(self, state: Any) -> float:
        delay = float(self._fallback(state))
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableError) and exc.retry_after:
            delay = max(delay, min(exc.retry_after, self._cap * 4))
        return delay


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = 5,
    base: float = 2.0,
    cap: float = 120.0,
) -> T:
    """Call ``fn``, retrying :class:`RetryableError` via tenacity (full-jitter backoff).

    A server-provided ``retry_after`` is honoured when longer than the computed
    backoff. After the last attempt the original exception is re-raised.
    """
    from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

    def _log(state: Any) -> None:
        exc = state.outcome.exception() if state.outcome else None
        delay = state.next_action.sleep if state.next_action else 0.0
        log.warning("%s failed (%s); retry %d/%d in %.1fs",
                    what, exc, state.attempt_number, attempts - 1, delay)

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=_wait_retry_after(base, cap),
        retry=retry_if_exception_type(RetryableError),
        before_sleep=_log,
        reraise=True,
    ):
        with attempt:
            return await fn()
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- token store
class TokenStoreError(RuntimeError):
    """Raised when tokens are missing, undecryptable or the key is not configured."""


class EncryptedTokenStore:
    """Persist OAuth token material encrypted at rest with Fernet (AES-128-CBC + HMAC).

    The key lives only in the environment (``TOKEN_ENCRYPTION_KEY``); the file on
    disk is useless without it. Files are written atomically with mode 0600.
    """

    def __init__(self, path: Path, key: str | None) -> None:
        if not key:
            raise TokenStoreError("TOKEN_ENCRYPTION_KEY is not set (generate one: clipper gen-key)")
        from cryptography.fernet import Fernet  # local import keeps CLI startup light

        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise TokenStoreError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        from cryptography.fernet import InvalidToken

        if not self.path.is_file():
            raise TokenStoreError(f"no stored token at {self.path}; run the matching auth command")
        try:
            return json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except InvalidToken as exc:
            raise TokenStoreError(f"cannot decrypt {self.path}: wrong TOKEN_ENCRYPTION_KEY?") from exc

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        blob = self._fernet.encrypt(json.dumps(data).encode())
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


def pot_helper_up(host: str = "127.0.0.1", port: int = 4416, timeout: float = 0.3) -> bool:
    """Is the YouTube anti-bot token helper (bgutil PO-token server) answering on this Mac?"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def try_lock(fh: Any) -> bool:
    """Non-blocking exclusive lock on an open file (flock on macOS/Linux, msvcrt on Windows).
    False if another process holds it. The lock lasts as long as the file stays open."""
    import os

    if os.name == "nt":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def daemon_running(lock_path: Path) -> bool:
    """Is a Clipper daemon holding ``lock_path`` right now?"""
    if not lock_path.is_file():
        return False
    with open(lock_path, "a+") as fh:  # noqa: PTH123
        return not try_lock(fh)
