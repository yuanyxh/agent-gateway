"""Stable short identifiers for URLs, shared by tool results and traces."""

from __future__ import annotations

import hashlib
from urllib.parse import urldefrag


def url_id(url: str) -> str:
    """SHA-1 prefix of the URL without its fragment; empty for blank input."""
    text = urldefrag(str(url or "").strip())[0]
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
