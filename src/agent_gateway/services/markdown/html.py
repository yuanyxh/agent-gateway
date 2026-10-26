"""HTML → Markdown conversion.

Pure functions with no network access: callers fetch the HTML themselves, then
hand the raw text to :func:`html_to_markdown`. Kept separate from fetching so
the converter can be reused by search, read, RSS, and tests without pulling in
proxy/rate-limit state.
"""

from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser

_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "canvas",
    "template",
    "nav",
    "footer",
    "aside",
}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_BOILERPLATE_RE = re.compile(
    r"(?:^|[-_ ])(nav|menu|footer|sidebar|cookie|banner|advert|ads?|share|social|"
    r"comment|breadcrumb)(?:$|[-_ ])",
    re.IGNORECASE,
)


def _clean_markdown(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_markdown(text: str, max_chars: int) -> tuple[str, bool]:
    """Trim at a line boundary and report whether anything was removed."""
    if max_chars and max_chars > 0 and len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        removed = len(text) - cut
        return text[:cut].rstrip() + f"\n\n[... truncated {removed} chars ...]", True
    return text, False


def _truncate(text: str, max_chars: int) -> str:
    return truncate_markdown(text, max_chars)[0]


def _cell_text(value: str) -> str:
    return _clean_markdown(value).replace("|", "\\|").replace("\n", " ")


class _MarkdownParser(HTMLParser):
    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.skip = 0
        self.skip_stack: list[str] = []
        self.pre = 0
        self.links: list[str] = []
        self.lists: list[str] = []
        self.ol_index: list[int] = []
        self.title_parts: list[str] = []
        self.in_title = False
        self.table_rows: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def _newline(self, count: int = 2) -> None:
        if self.parts and not self.parts[-1].endswith("\n" * count):
            self.parts.append("\n" * count)

    def _abs(self, href: str) -> str:
        return urllib.parse.urljoin(self.base_url, href) if href else ""

    def _is_boilerplate(self, tag: str, attrs: dict) -> bool:
        # Structural wrappers often carry UI-state classes such as
        # "main-menu-disabled" (Wikipedia). Skipping them drops the whole page.
        if tag in ("html", "body", "main", "article"):
            return False
        marker = " ".join(filter(None, (attrs.get("class", ""), attrs.get("id", ""))))
        return bool(marker and _BOILERPLATE_RE.search(marker))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
            return
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self.skip += 1
                self.skip_stack.append(tag)
            return
        if self.skip:
            return
        if self.cell is not None:
            return
        if self._is_boilerplate(tag, attrs):
            self.skip += 1
            self.skip_stack.append(tag)
            return
        if tag in _HEADINGS:
            self._newline(2)
            self.parts.append("#" * _HEADINGS[tag] + " ")
        elif tag in ("p", "div", "section", "article", "main"):
            self._newline(2)
        elif tag in ("ul", "ol"):
            self._newline(2)
            self.lists.append(tag)
            self.ol_index.append(0)
        elif tag == "li":
            self._newline(1)
            if self.lists and self.lists[-1] == "ol":
                self.ol_index[-1] += 1
                self.parts.append(f"{self.ol_index[-1]}. ")
            else:
                self.parts.append("- ")
        elif tag == "br":
            self._newline(1)
        elif tag == "hr":
            self._newline(2)
            self.parts.append("---")
            self._newline(2)
        elif tag == "blockquote":
            self._newline(2)
            self.parts.append("> ")
        elif tag == "pre":
            self._newline(2)
            self.parts.append("```\n")
            self.pre += 1
        elif tag == "code":
            if not self.pre:
                self.parts.append("`")
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag == "a":
            href = attrs.get("href", "")
            self.links.append(href)
            self.parts.append("[")
        elif tag == "img":
            alt = attrs.get("alt", "")
            src = self._abs(attrs.get("src", ""))
            if src:
                self.parts.append(f"![{alt}]({src})")
        elif tag == "table":
            self._newline(2)
            self.table_rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
            return
        if self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()
                self.skip = max(0, self.skip - 1)
            return
        if tag in _SKIP_TAGS:
            return
        if tag in ("td", "th"):
            if self.cell is not None:
                if self.row is None:
                    self.row = []
                self.row.append(_cell_text("".join(self.cell)))
                self.cell = None
            return
        if tag == "tr":
            if self.table_rows is not None and self.row:
                self.table_rows.append(self.row)
            self.row = None
            return
        if tag == "table":
            self._render_table()
            return
        if self.cell is not None:
            return
        if tag in _HEADINGS or tag in ("p", "div", "section", "article", "main"):
            self._newline(2)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
                self.ol_index.pop()
            self._newline(2)
        elif tag == "li":
            self._newline(1)
        elif tag == "blockquote":
            self._newline(2)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.parts.append("\n```\n")
        elif tag == "code":
            if not self.pre:
                self.parts.append("`")
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag == "a":
            href = self.links.pop() if self.links else ""
            self.parts.append(f"]({self._abs(href)})" if href else "]")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_parts.append(data)
        elif self.cell is not None:
            self.cell.append(data)
        else:
            self.parts.append(data)

    def _render_table(self) -> None:
        rows = [r for r in (self.table_rows or []) if r]
        self.table_rows = None
        if not rows:
            return
        cols = max(len(r) for r in rows)
        rows = [r + [""] * (cols - len(r)) for r in rows]
        lines = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join("---" for _ in range(cols)) + " |",
        ]
        lines.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        self._newline(2)
        self.parts.append("\n".join(lines))
        self._newline(2)


def html_to_markdown(raw: str, base_url: str = "", max_chars: int = 0) -> str:
    parser = _MarkdownParser(base_url)
    try:
        parser.feed(raw or "")
        parser.close()
    except Exception:
        pass
    body = _clean_markdown("".join(parser.parts))
    title = _clean_markdown("".join(parser.title_parts))
    if title and title not in body[:300]:
        body = f"# {title}\n\n{body}"
    return _truncate(body, max_chars)


def html_to_text(raw: str, base_url: str = "", max_chars: int = 0) -> str:
    """Alias kept for callers that only need plain text; the output is Markdown."""
    return html_to_markdown(raw, base_url, max_chars)
