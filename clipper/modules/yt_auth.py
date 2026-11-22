"""YouTube sign-in for *reading* streams/videos (not for posting).

YouTube sometimes answers "LOGIN_REQUIRED: Sign in to confirm you're not a bot" to
anonymous requests from one internet address. Sending a signed-in browser's cookies
fixes it. We keep one cookie file (Netscape ``cookies.txt`` format, the format both
yt-dlp and Streamlink understand) at ``storage/secrets/youtube_cookies.txt`` (0600):

* yt-dlp gets ``--cookies <file>`` (it also refreshes rotated cookies in place);
* Streamlink's in-process live check loads the jar into its HTTP session;
* the Streamlink capture CLI gets ``--http-cookie`` for the youtube.com cookies.

Cookies come from a browser on this Mac via yt-dlp's own extractor
(``yt_dlp.cookies.extract_cookies_from_browser``, run in a child process so yt-dlp
never patches this process's urllib3), or from a pasted cookies.txt export.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import sys
import time
import urllib.request
import warnings
from pathlib import Path
from typing import Any

from clipper.config import Settings
from clipper.modules.utils import run_cmd

log = logging.getLogger(__name__)

BROWSERS = ("chrome", "safari", "brave", "edge", "firefox", "chromium", "vivaldi", "opera")
_APPS = {"chrome": "Google Chrome", "safari": "Safari", "brave": "Brave Browser", "edge": "Microsoft Edge",
         "firefox": "Firefox", "chromium": "Chromium", "vivaldi": "Vivaldi", "opera": "Opera"}
_DOMAINS = ("youtube.com", "google.com")
_AUTH_NAMES = {"SAPISID", "__Secure-3PSID", "__Secure-1PSID", "SID", "LOGIN_INFO"}
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/"

# yt-dlp's extractor, run in a child process; prints the jar as Netscape text.
_EXTRACT = r"""
import io, sys, http.cookiejar
from yt_dlp.cookies import extract_cookies_from_browser
jar = extract_cookies_from_browser(sys.argv[1])
out = http.cookiejar.MozillaCookieJar()
for c in jar:
    if any(c.domain.lstrip('.').endswith(d) for d in ('youtube.com', 'google.com')):
        out.set_cookie(c)
path = sys.argv[2]
out.save(path, ignore_discard=True, ignore_expires=True)
print(len(out))
"""


def cookie_file(s: Settings) -> Path:
    return s.secrets_dir / "youtube_cookies.txt"


def installed_browsers() -> list[str]:
    """Browsers yt-dlp can read that look installed on this Mac (all of them elsewhere)."""
    if sys.platform != "darwin":
        return list(BROWSERS)
    roots = [Path("/Applications"), Path.home() / "Applications"]
    return [b for b in BROWSERS if any((r / f"{_APPS[b]}.app").exists() for r in roots)]


def _load(path: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def _youtube_cookies(jar: http.cookiejar.CookieJar) -> list[http.cookiejar.Cookie]:
    now = time.time()
    return [c for c in jar if c.domain.lstrip(".").endswith("youtube.com") and (not c.expires or c.expires > now)]


def status(s: Settings) -> dict[str, Any]:
    path = cookie_file(s)
    if not path.is_file():
        return {"signed_in": False}
    try:
        yt = _youtube_cookies(_load(path))
    except (OSError, http.cookiejar.LoadError) as exc:
        return {"signed_in": False, "error": f"cookie file unreadable: {exc}"}
    auth = [c for c in yt if c.name in _AUTH_NAMES]
    expiries = [c.expires for c in auth if c.expires]
    return {"signed_in": bool(auth), "saved_at": path.stat().st_mtime,
            "expires_at": min(expiries) if expiries else None,
            "verified": _verified.get("result"), "verified_at": _verified.get("at")}


def _write(s: Settings, jar: http.cookiejar.MozillaCookieJar) -> int:
    if not any(c.name in _AUTH_NAMES for c in _youtube_cookies(jar)):
        raise ValueError("no signed-in YouTube cookies found. Sign in to youtube.com in that browser first, then try again")
    s.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = cookie_file(s)
    tmp = dest.with_suffix(".tmp")
    jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
    os.chmod(tmp, 0o600)
    tmp.replace(dest)
    _verified.clear()
    return len(jar)


async def import_from_browser(s: Settings, browser: str) -> int:
    if browser not in BROWSERS:
        raise ValueError(f"unsupported browser: {browser}")
    s.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = s.secrets_dir / "youtube_cookies.import"
    try:
        res = await run_cmd([sys.executable, "-c", _EXTRACT, browser, str(tmp)], timeout=120, check=False)
        if res.returncode != 0 or not tmp.is_file():
            err = (res.stderr or "").strip().splitlines()
            msg = err[-1] if err else "couldn't read cookies"
            if browser == "safari" and "Operation not permitted" in (res.stderr or ""):
                msg = "macOS blocked access to Safari's cookies. Give the app running Clipper Full Disk Access (System Settings → Privacy & Security) or use another browser"
            raise ValueError(msg[:300])
        os.chmod(tmp, 0o600)
        return _write(s, _load(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def save_pasted(s: Settings, text: str) -> int:
    """Store a pasted Netscape cookies.txt (e.g. from a 'Get cookies.txt' browser extension)."""
    text = text.strip()
    if not text:
        raise ValueError("paste the contents of a cookies.txt file")
    if not text.startswith("# "):
        text = "# Netscape HTTP Cookie File\n" + text
    s.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = s.secrets_dir / "youtube_cookies.paste"
    try:
        tmp.write_text(text + "\n")
        os.chmod(tmp, 0o600)
        try:
            with warnings.catch_warnings():  # cookiejar warns loudly on malformed lines
                warnings.simplefilter("ignore")
                src = _load(tmp)
        except http.cookiejar.LoadError as exc:
            raise ValueError("that isn't a cookies.txt (Netscape format) export") from exc
    finally:
        tmp.unlink(missing_ok=True)
    out = http.cookiejar.MozillaCookieJar()
    for c in src:
        if any(c.domain.lstrip(".").endswith(d) for d in _DOMAINS):
            out.set_cookie(c)
    return _write(s, out)


def sign_out(s: Settings) -> None:
    cookie_file(s).unlink(missing_ok=True)
    _verified.clear()


_verified: dict[str, Any] = {}


def verify(s: Settings) -> bool:
    """Load youtube.com with the cookies and check YouTube says we're logged in (blocking)."""
    path = cookie_file(s)
    if not path.is_file():
        return False
    jar = _load(path)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request("https://www.youtube.com/account", headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"})
    with opener.open(req, timeout=20) as resp:
        body = resp.read(3_000_000).decode("utf-8", "replace")
    ok = '"LOGGED_IN":true' in body or '"logged_in","value":"1"' in body
    _verified.update(result=ok, at=time.time())
    return ok


# ------------------------------------------------------------ consumers
def ytdlp_args(s: Settings) -> list[str]:
    path = cookie_file(s)
    return ["--cookies", str(path.resolve())] if path.is_file() else []


def is_youtube(url: str) -> bool:
    return "youtu" in url


def load_into(s: Settings, session_cookies: http.cookiejar.CookieJar) -> bool:
    """Put the saved YouTube cookies into a requests/Streamlink cookie jar. Returns True if any."""
    path = cookie_file(s)
    if not path.is_file():
        return False
    try:
        for c in _load(path):
            session_cookies.set_cookie(c)
    except (OSError, http.cookiejar.LoadError) as exc:
        log.warning("YouTube cookies unreadable: %s", exc)
        return False
    return True


def streamlink_args(s: Settings) -> list[str]:
    """``--http-cookie`` flags for the Streamlink CLI (youtube.com cookies only)."""
    path = cookie_file(s)
    if not path.is_file():
        return []
    try:
        cookies = _youtube_cookies(_load(path))
    except (OSError, http.cookiejar.LoadError):
        return []
    args: list[str] = []
    for c in cookies:
        if c.value is not None:
            args += ["--http-cookie", f"{c.name}={c.value}"]
    return args


def describe_json(s: Settings) -> str:  # handy for debugging from a shell
    return json.dumps(status(s), indent=2)
