"""Built-in URL reader.

Runs a privacy-first cascade: fetch and convert locally first; only when the
caller opts into ``use_proxy`` are defuddle.md and r.jina.ai tried. Every
failure returns a structured payload with per-tier attempts and a hint, so the
agent can tell "network failed" apart from "the page is a login shell".

PDF extraction is intentionally unsupported: the runtime has no PDF library and
silently returning decoded binary would be worse than a clear error.
"""

from __future__ import annotations

import re
import urllib.parse

from agent_gateway.services.fetch import http
from agent_gateway.services.markdown import article as md_article
from agent_gateway.services.markdown import html as md

_MIN_CHARS = 200
_MIN_LINES = 5
_SHELL_MARKERS = (
    "sign in to continue",
    "please sign in",
    "log in to continue",
    "请登录",
    "登录后查看",
    "just a moment...",
    "checking your browser",
    "enable javascript",
    "verify you are human",
    "访问验证",
)
#: Social sites answer anonymous requests with a short login/register shell.
#: Only applied to short pages so an article's navigation does not trigger it.
_SHORT_LOGIN_MARKERS = ("登录", "注册", "log in", "sign up")
_GITHUB_BLOB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$"
)


def _assess(text: str) -> str:
    """Classify converted content as ok / thin / shell / empty."""
    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    head = stripped[:8000].lower()
    if any(marker in head for marker in _SHELL_MARKERS):
        return "shell"
    if len(stripped) < 1500 and any(marker in head for marker in _SHORT_LOGIN_MARKERS):
        return "shell"
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(stripped) < _MIN_CHARS or len(lines) < _MIN_LINES:
        return "thin"
    return "ok"


def _first_heading(text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def _route(url: str) -> tuple[str, str | None]:
    """Return ``(fetch_url, error_code)``; error_code is set for unsupported input."""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return url, "unsupported_format"
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    if (
        host == "feishu.cn"
        or host.endswith(".feishu.cn")
        or host == "larksuite.com"
        or host.endswith(".larksuite.com")
    ):
        return url, "unsupported_site"
    match = _GITHUB_BLOB_RE.match(url)
    if match:
        owner, repo, ref, rest = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rest}", None
    return url, None


def _success(
    *,
    requested: str,
    final_url: str,
    text: str,
    tier: str,
    max_chars: int,
    warnings: list[str],
    attempts: list[dict],
) -> dict:
    body, truncated = md.truncate_markdown(text, max_chars)
    payload = {
        "ok": True,
        "requested_url": requested,
        "url": final_url,
        "title": _first_heading(body),
        "markdown": body,
        "chars": len(body),
        "truncated": truncated,
        "tier": tier,
        "attempts": attempts,
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


def _failure(
    *,
    requested: str,
    error_code: str,
    message: str,
    attempts: list[dict],
    hint: str = "",
) -> dict:
    payload = {
        "ok": False,
        "requested_url": requested,
        "error_code": error_code,
        "message": message,
        "attempts": attempts,
    }
    if hint:
        payload["hint"] = hint
    return payload


def normalize_max_chars(value: int) -> int:
    """0 = no limit; positive values keep a 1000-char floor but no ceiling."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return 0 if value <= 0 else max(1000, value)


def _fetch_local(url: str) -> tuple[str, str]:
    host = urllib.parse.urlparse(url).netloc.lower()
    final, raw, _ = http.get_text(
        url,
        profile="page",
        timeout=http.TIMEOUT,
        retries=1,
        rate_key="host:" + host,
        interval=0.8,
        detect_challenge=False,
    )
    if raw.lstrip().startswith("%PDF-"):
        raise ValueError("PDF response is not supported")
    return final, md_article.extract_article(raw, base_url=final)


def _fetch_proxy(url: str, base: str) -> tuple[str, str]:
    final, text, _ = http.get_text(
        base + url,
        profile="read-proxy",
        timeout=30,
        retries=1,
        detect_challenge=False,
    )
    if text.lstrip().startswith("%PDF-"):
        raise ValueError("PDF response is not supported")
    return final, text


def read_url(url: str, max_chars: int = 0, use_proxy: bool = False) -> dict:
    """Read ``url`` as Markdown with a local-first, opt-in proxy cascade."""
    requested = (url or "").strip()
    if not requested:
        return _failure(
            requested=requested,
            error_code="invalid_url",
            message="url is empty",
            attempts=[],
        )
    parsed = urllib.parse.urlparse(requested)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return _failure(
            requested=requested,
            error_code="invalid_url",
            message="only absolute http/https URLs are supported",
            attempts=[],
        )

    fetch_url, route_error = _route(requested)
    if route_error == "unsupported_format":
        return _failure(
            requested=requested,
            error_code="unsupported_format",
            message="PDF reading is not supported by the built-in reader",
            attempts=[],
            hint="open the PDF in a browser and copy the relevant text",
        )
    if route_error == "unsupported_site":
        return _failure(
            requested=requested,
            error_code="unsupported_site",
            message="Feishu/Lark documents require an API adapter",
            attempts=[],
            hint="export the document or use the browser",
        )

    max_chars = normalize_max_chars(max_chars)
    attempts: list[dict] = []
    warnings: list[str] = []
    local_thin: tuple[str, str] | None = None

    try:
        final_url, local_text = _fetch_local(fetch_url)
    except Exception as exc:
        attempts.append(
            {
                "tier": "local",
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
    else:
        state = _assess(local_text)
        attempts.append({"tier": "local", "status": state, "chars": len(local_text)})
        if state == "ok":
            return _success(
                requested=requested,
                final_url=final_url,
                text=local_text,
                tier="local",
                max_chars=max_chars,
                warnings=warnings,
                attempts=attempts,
            )
        if state == "thin":
            local_thin = (final_url, local_text)

    if use_proxy:
        for tier, base in (
            ("defuddle", "https://defuddle.md/"),
            ("jina", "https://r.jina.ai/"),
        ):
            try:
                final_url, text = _fetch_proxy(fetch_url, base)
            except Exception as exc:
                attempts.append(
                    {
                        "tier": tier,
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            state = _assess(text)
            attempts.append({"tier": tier, "status": state, "chars": len(text)})
            if state in ("ok", "thin"):
                if state == "thin":
                    warnings.append(
                        "proxy returned short content; it may be incomplete"
                    )
                return _success(
                    requested=requested,
                    final_url=final_url,
                    text=text,
                    tier=tier,
                    max_chars=max_chars,
                    warnings=warnings,
                    attempts=attempts,
                )
    elif local_thin is not None:
        warnings.append(
            "content is short; the page may be a shell or require JavaScript"
        )
        final_url, local_text = local_thin
        return _success(
            requested=requested,
            final_url=final_url,
            text=local_text,
            tier="local",
            max_chars=max_chars,
            warnings=warnings,
            attempts=attempts,
        )

    if local_thin is not None:
        warnings.append(
            "content is short; the page may be a shell or require JavaScript"
        )
        warnings.append(
            "local extraction was kept because proxy attempts did not improve it"
        )
        final_url, local_text = local_thin
        return _success(
            requested=requested,
            final_url=final_url,
            text=local_text,
            tier="local",
            max_chars=max_chars,
            warnings=warnings,
            attempts=attempts,
        )

    hint = (
        "the page may be JavaScript-rendered or login-protected; retry with use_proxy=true "
        "only if the URL is public and non-sensitive"
        if not use_proxy
        else "all tiers failed; open the URL in a browser to verify it is reachable"
    )
    return _failure(
        requested=requested,
        error_code="all_tiers_failed",
        message="no readable content was extracted",
        attempts=attempts,
        hint=hint,
    )
