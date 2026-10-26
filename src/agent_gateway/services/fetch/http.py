"""HTTP transport for search and page fetching.

Owns everything that is not search-specific: browser headers, proxy settings,
cookie jar, gzip/deflate decoding, per-key rate limiting, retry policy, and
challenge-page detection. Search engines and page readers share this layer so a
proxy fix or retry change lands in one place.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA_POOL = (
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
)
TIMEOUT = 20

_CHALLENGE_MARKERS = (
    "verify you are human",
    "unusual traffic",
    "are you a robot",
    "enable javascript",
    "security check",
    "captcha",
    "访问验证",
    "请输入验证码",
    "滑动验证",
)

_meta_lock = threading.Lock()
_openers: dict[str, urllib.request.OpenerDirector] = {}
_uas: dict[str, str] = {}
_rate_lock = threading.Lock()
_rate_next: dict[str, float] = {}


class Blocked(Exception):
    """The server answered with a challenge page instead of content."""


def _proxy_map() -> dict:
    """System proxy settings (macOS `_scproxy` + env), re-read when an opener is built."""
    try:
        return {k: v for k, v in urllib.request.getproxies().items() if v}
    except Exception:
        return {}


def proxy_status() -> dict:
    """Proxy map with credentials stripped, for diagnostics."""
    out = {}
    for key, value in _proxy_map().items():
        try:
            parsed = urllib.parse.urlsplit(
                value if "://" in value else "http://" + value
            )
            host = parsed.hostname or ""
            out[key] = f"{parsed.scheme}://{host}" + (
                f":{parsed.port}" if parsed.port else ""
            )
        except Exception:
            out[key] = "configured"
    return out


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(_proxy_map()),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def reset_http_state() -> None:
    """Clear cached openers and user agents (tests and proxy changes)."""
    with _meta_lock:
        _openers.clear()
        _uas.clear()


def _opener(profile: str) -> urllib.request.OpenerDirector:
    with _meta_lock:
        opener = _openers.get(profile)
        if opener is None:
            opener = _build_opener()
            _openers[profile] = opener
        return opener


def user_agent(profile: str) -> str:
    with _meta_lock:
        ua = _uas.get(profile)
        if ua is None:
            ua = random.choice(UA_POOL)
            _uas[profile] = ua
        return ua


def default_headers(profile: str = "page") -> dict:
    return {
        "User-Agent": user_agent(profile),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
    }


def engine_headers(profile: str, home: str = "") -> dict:
    """Browser headers for a search-engine request, with a same-origin referer."""
    headers = default_headers(profile)
    if home:
        headers["Referer"] = home
        headers["Sec-Fetch-Site"] = "same-origin"
    return headers


def respect_rate(key: str, interval: float) -> None:
    """Reserve a request slot for `key`; concurrent callers queue without the lock."""
    if interval <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = max(0.0, _rate_next.get(key, 0.0) - now)
        _rate_next[key] = now + wait + interval + random.uniform(0, 0.25)
    if wait > 0:
        time.sleep(wait)


def _looks_like_challenge(raw: str) -> bool:
    head = (raw or "")[:8000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _request(
    url: str,
    *,
    profile: str = "page",
    headers: dict | None = None,
    timeout: int = TIMEOUT,
    detect_challenge: bool = True,
):
    req = urllib.request.Request(url, headers=headers or default_headers(profile))
    with _opener(profile).open(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                pass
        charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="ignore")
        status = getattr(resp, "status", 200)
        if status == 202 or (detect_challenge and _looks_like_challenge(text)):
            raise Blocked(f"challenge page (status {status})")
        return resp.geturl(), text, status


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            return min(float(header), 10.0)
        except ValueError:
            pass
    return 2.0 * (attempt + 1)


def get_text(
    url: str,
    *,
    profile: str = "page",
    headers: dict | None = None,
    timeout: int = TIMEOUT,
    retries: int = 2,
    rate_key: str | None = None,
    interval: float = 0.0,
    detect_challenge: bool = True,
) -> tuple[str, str, int]:
    """Fetch a URL as text, applying rate limiting and retry policy.

    Returns ``(final_url, text, status)``. Raises :class:`Blocked` when the
    response is a challenge page, and the original exception after retries.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        if rate_key:
            respect_rate(rate_key, interval)
        try:
            return _request(
                url,
                profile=profile,
                headers=headers,
                timeout=timeout,
                detect_challenge=detect_challenge,
            )
        except Blocked as exc:
            last = exc
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
        except urllib.error.HTTPError as exc:
            last = exc
            if (
                exc.code in (403, 408, 425, 429, 500, 502, 503, 504)
                and attempt < retries
            ):
                time.sleep(_retry_delay(exc, attempt))
                continue
            raise
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise last or RuntimeError("request failed")
