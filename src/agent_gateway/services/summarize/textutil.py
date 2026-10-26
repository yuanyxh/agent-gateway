"""Shared text similarity and segment helpers."""

import difflib
import re

from agent_gateway.core.log_utils import setup_logging as _log_setup

logger = _log_setup("summarize")


def _max_segment_similarity(new_content: str, existing_text: str) -> float:
    """Max "match coverage" of the merge content against existing sections of the target file (difflib, zero external dependencies).

    Semantics: the fraction of characters in new_content that are matched
    character-for-character by an existing section, directly measuring the amount of new
    information -- coverage approaching 1 -> the generated section is basically a
    restatement of existing sections -> judged no addition and skipped.
    ratio is disturbed by length differences (long sections dilute, short sections
    inflate), while coverage distinguishes "rewrite-style duplication" better (measured:
    ScanTextUtil duplicate coverage 0.767 vs typical increment 0.595).
    Sections are split on multi-level headings (#{1,6}) rather than H1 only: for a file
    organized with multi-level headings (##/###), splitting by H1 degrades into one
    whole-file section, diluting similarity with unrelated content (2026-08-07 root-cause fix).
    """
    if not new_content or not existing_text:
        return 0.0
    best = 0.0
    for seg in re.split(r"(?m)^(?=#{1,6}\s)", existing_text):
        body = "\n".join(seg.splitlines()[1:]).strip()
        if not body:
            continue
        sm = difflib.SequenceMatcher(None, new_content, body)
        matched = sum(b.size for b in sm.get_matching_blocks())
        cov = matched / len(new_content)
        if cov > best:
            best = cov
    return best


def _relevant_segments(existing_text: str, facts: str, max_chars: int) -> str:
    """Slice the existing sections most relevant to facts out of the target note, for the LLM's increment judgment during merge generation.

    The original implementation took the first N characters of existing_text: for a large
    file the target section is at the file's end (e.g. existing sections appended at the
    tail), so the LLM could not see the existing content and had no way to judge
    duplication, directly restating relevant facts -> duplicate sections (2026-08-07
    root-cause fix). Now it splits on multi-level headings, sorts sections by similarity to
    facts, and takes the top relevant sections, capped at max_chars. With no relevant
    section (empty facts / no sections), it falls back to the file head.
    """
    if not existing_text or not facts:
        return existing_text[:max_chars]
    scored = []
    for seg in re.split(r"(?m)^(?=#{1,6}\s)", existing_text):
        if not seg.strip():
            continue
        body = "\n".join(seg.splitlines()[1:]).strip()
        if not body:
            continue
        sim = difflib.SequenceMatcher(None, facts, body).ratio()
        scored.append((sim, seg))
    scored.sort(reverse=True, key=lambda x: x[0])
    out: list[str] = []
    total = 0
    for _, seg in scored:
        if out and total + len(seg) > max_chars:
            break
        out.append(seg)
        total += len(seg)
    if not out:
        return existing_text[:max_chars]
    return "".join(out)[:max_chars]
