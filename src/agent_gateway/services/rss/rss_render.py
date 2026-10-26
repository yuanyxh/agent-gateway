"""Markdown rendering, history dedup, and format self-checks for the RSS digest."""

from __future__ import annotations

import re
from pathlib import Path

from agent_gateway.core import l10n

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_CJK_RE = re.compile(r"[一-龥]")
# Path constants: both languages carry the same quarter names so archives never move.
_QUARTER_KEYS = {
    1: "rss.quarter_1",
    2: "rss.quarter_2",
    3: "rss.quarter_3",
    4: "rss.quarter_4",
}


def normalize_url(url: str) -> str:
    """Dedup key: strip query/fragment and trailing slash."""
    return re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/")


def collect_history_urls(root: Path, exclude: Path | None = None) -> set[str]:
    urls: set[str] = set()
    if not root.is_dir():
        return urls
    for path in root.rglob("*.md"):
        if exclude is not None and path == exclude:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _URL_RE.finditer(text):
            urls.add(normalize_url(match.group(0)))
    urls.discard("")
    return urls


def archive_path(root: Path, day: str) -> Path:
    month = int(day[5:7])
    quarter = l10n.t(_QUARTER_KEYS[(month - 1) // 3 + 1])
    return root / day[:4] / quarter / f"{day}.md"


def decorate_title(title: str, translation: str) -> str:
    title = (title or "").strip()
    translation = (translation or "").strip()
    if not translation or translation == title or _CJK_RE.search(title):
        return title
    return f"{title}({translation})"


def render(day: str, articles: list[dict], filtered: list[dict]) -> str:
    lines = [l10n.t("rss.digest_title", day=day), ""]
    if not articles:
        lines.append(l10n.t("rss.digest_empty"))
        lines.append("")
    for item in articles:
        lines.extend(
            [
                "---",
                "",
                f"## [{decorate_title(item['title'], item.get('title_translation', ''))}]({item['url']})",
                "",
                f"> {(item.get('summary') or '').strip()}",
                "",
            ]
        )
    if articles:
        lines.extend(["---", ""])
    if filtered:
        lines.extend([l10n.t("rss.digest_filtered_header"), ""])
        for item in filtered:
            title = (item.get("title") or item.get("url") or "").strip()
            url = (item.get("url") or "").strip()
            feed = (item.get("feed") or "").strip()
            reason = (item.get("reason") or "").strip()
            if url:
                lines.append(f"- [{title}]({url}) — [{feed}] {reason}")
            else:
                lines.append(f"- {title} — [{feed}] {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def validate(text: str) -> list[str]:
    """Return format issues; rendering already guarantees the layout, so this is a guard only."""
    issues: list[str] = []
    for match in re.finditer(r"^## \[([^\]]+)\]\((http[^)]+)\)", text, re.MULTILINE):
        if not _CJK_RE.search(match.group(1)):
            issues.append(
                f"main entry title has no Chinese translation: {match.group(1)[:50]}"
            )
    if re.search(r"\]\(http[^)]*\)[（(][^）)]*[一-龥]", text):
        issues.append("leftover [English](url)(Chinese) pattern")
    for match in re.finditer(
        r"^- \[[^\]]+\]\([^)]+\)\s*—\s*([^\s\[])", text, re.MULTILINE
    ):
        issues.append(f"filtered entry source missing brackets: {match.group(0)[:60]}")
    return issues
