"""Article-level extraction on top of the full-page HTML -> Markdown converter."""

from __future__ import annotations

import re
from html import unescape

from agent_gateway.services.markdown import html as md

try:
    from trafilatura import extract as _extract
except ImportError:  # optional at runtime; fall back to full-page conversion
    _extract = None

MIN_ARTICLE_CHARS = 200
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+\S", re.MULTILINE)


def _html_title(raw: str) -> str:
    match = _TITLE_RE.search(raw or "")
    return re.sub(r"\s+", " ", unescape(match.group(1))).strip() if match else ""


def extract_article(raw: str, base_url: str = "", max_chars: int = 0) -> str:
    """Return article Markdown, falling back to full-page conversion when extraction yields too little text."""
    if _extract is not None:
        try:
            text = _extract(
                raw,
                output_format="markdown",
                include_comments=False,
                include_tables=True,
            )
        except Exception:  # a broken page must not break the fetch path
            text = None
        if text and len(text) >= MIN_ARTICLE_CHARS:
            title = _html_title(raw)
            if title and not _HEADING_RE.search(text):
                text = f"# {title}\n\n{text}"
            return md.truncate_markdown(text, max_chars)[0]
    return md.html_to_markdown(raw, base_url=base_url, max_chars=max_chars)
