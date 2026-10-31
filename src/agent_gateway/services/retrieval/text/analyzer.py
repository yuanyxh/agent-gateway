"""Script-driven text analysis.

Language affects term splitting only. Adding a language means registering one
analyzer for its Unicode script; nothing downstream branches on a language code.
"""

from __future__ import annotations

import re
from typing import Protocol

CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z0-9]")
#: Connectors stay inside a Latin chunk: "agent-vibe" is one term, not two.
CONNECTOR_RE = re.compile(r"[-_.:/]")
_ASCII_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]*")
_MIN_TERM_LEN = 2


class TextAnalyzer(Protocol):
    script: str

    def split(self, chunk: str) -> list[str]: ...


class LatinAnalyzer:
    """Latin/ASCII scripts: lowercased identifier-ish terms."""

    script = "latin"

    def split(self, chunk: str) -> list[str]:
        return [
            term
            for term in _ASCII_TERM_RE.findall(chunk.lower())
            if len(term) >= _MIN_TERM_LEN
        ]


class CjkAnalyzer:
    """CJK: bigrams only, single characters are too broad to anchor."""

    script = "cjk"

    def split(self, chunk: str) -> list[str]:
        chars = "".join(CJK_RE.findall(chunk))
        return [chars[i : i + 2] for i in range(len(chars) - 1)]


_ANALYZERS: dict[str, TextAnalyzer] = {
    LatinAnalyzer.script: LatinAnalyzer(),
    CjkAnalyzer.script: CjkAnalyzer(),
}


def register_analyzer(analyzer: TextAnalyzer) -> None:
    _ANALYZERS[analyzer.script] = analyzer


def script_of(char: str) -> str | None:
    if CJK_RE.match(char):
        return CjkAnalyzer.script
    if LATIN_RE.match(char) or CONNECTOR_RE.match(char):
        return LatinAnalyzer.script
    return None


def _script_chunks(token: str) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_script: str | None = None
    for char in token:
        script = script_of(char)
        if script is None:
            continue
        if script != current_script:
            if current and current_script:
                chunks.append((current_script, "".join(current)))
            current = []
            current_script = script
        current.append(char)
    if current and current_script:
        chunks.append((current_script, "".join(current)))
    return chunks


def split_terms(text: str) -> list[str]:
    """Whitespace tokens are kept as boundaries, then split by script."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in (text or "").lower().split():
        for script, chunk in _script_chunks(token):
            analyzer = _ANALYZERS.get(script)
            if analyzer is None:
                continue
            for term in analyzer.split(chunk):
                if term and term not in seen:
                    seen.add(term)
                    terms.append(term)
    return terms


def detect_language(text: str) -> str:
    cjk = len(CJK_RE.findall(text or ""))
    latin = len(LATIN_RE.findall(text or ""))
    if cjk and latin:
        return "mixed"
    if cjk:
        return "cjk"
    if latin:
        return "latin"
    return "other"
