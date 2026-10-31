"""Term coverage and snippet extraction for lexical arms."""

from __future__ import annotations


def notes_coverage(text: str, needles: list[str]) -> float:
    if not needles:
        return 0.0
    lowered = text.lower()
    return sum(1 for needle in needles if needle in lowered) / len(needles)


def notes_snippet(text: str, needle: str, max_lines: int = 3) -> str:
    """First `max_lines` lines containing needle, formatted `line_no:line`."""
    if not needle:
        return ""
    hits: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if needle in line.lower():
            hits.append(f"{line_no}:{line}")
            if len(hits) >= max_lines:
                break
    return "\n".join(hits)
