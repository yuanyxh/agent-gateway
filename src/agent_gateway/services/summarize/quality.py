"""Quality gate for memory facts."""

import difflib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agent_gateway.core import prompt_loader
from agent_gateway.core.config import (
    data_dir,
    notes_dir,
    note_exclude_dirs,
    note_extensions,
    summary_exclude_tags,
)
from agent_gateway.core.l10n import all_templates, t
from agent_gateway.core.log_utils import setup_logging as _log_setup
from agent_gateway.core.memory_lifecycle import (
    COLD_SOURCE_MEMORY_IDS_FIELD,
    PROCESSED_CONSUMED_MASK,
    PROCESSED_FIELD,
    STATUS_ACTIVE,
    STATUS_FIELD,
)
from agent_gateway.services.summarize.summary_llm import (
    _HTTP,
    AI_API_KEY,
    SUMMARY_LLM_TIMEOUT,
    _llm_chat,
    _parse_json_any,
    write_error_log,
)
from agent_gateway.services.summarize.summary_rules import _run_phase3
from agent_gateway.services.summarize.summary_stats import _print_report_v2

logger = _log_setup("summarize")

from agent_gateway.services.summarize.constants import (
    MEMORY_QUALITY_CN_SHORT_MAX,
    MEMORY_QUALITY_HALF_MAX,
    MEMORY_QUALITY_LLM_THREADS,
    MEMORY_QUALITY_MIN_LEN,
    MEMORY_QUALITY_RESIDUAL_MIN,
    MEMORY_QUALITY_REVIEW_VOTES,
    QUALITY_NONTRUNC_PLACEHOLDERS,
    QUALITY_PLACEHOLDER_SUBS,
    QUALITY_TRUNC_PLACEHOLDERS,
    _QUALITY_CN_STATE_VERBS,
    _QUALITY_EMPTY_TITLE,
    _QUALITY_HALF_CN,
    _QUALITY_HALF_TRUNC,
    _QUALITY_QUOTE_PAIRS,
    _QUALITY_TRUNC_RE,
    _QUALITY_USER_SNAPSHOT,
)


def _quality_strip_quoted(text: str) -> str:
    """Remove content inside paired quotes and return the text "outside quotes". Used to exempt placeholder references in meta-discussion."""
    out = text
    for left, right in _QUALITY_QUOTE_PAIRS:
        out = re.sub(
            re.escape(left)
            + r"[^"
            + re.escape(right)
            + r"]{0,200}?"
            + re.escape(right),
            "",
            out,
        )
    return out


def _quality_residual_chars(text: str) -> int:
    """Count of remaining meaningful CJK characters after removing placeholder strings and punctuation/digits/English."""
    t = text.lower()
    for p in QUALITY_PLACEHOLDER_SUBS:
        t = t.replace(p.lower(), "")
    t = re.sub(r"[\s\-*#.。，、：:（）()【】\[\]0-9A-Za-z_`|/\\]+", "", t)
    return len(t)


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _quality_is_cn_short_snapshot(t: str) -> bool:
    """Short CJK sentence + state verb -> possibly a state snapshot. Sent to the LLM for review (no hard drop), zero false positives."""
    c = "".join(t.split())
    if len(c) >= MEMORY_QUALITY_CN_SHORT_MAX:
        return False
    if not _has_cjk(t):
        return False
    return any(v in t for v in _QUALITY_CN_STATE_VERBS)


def _quality_rule_judge(text: str) -> tuple[str, str]:
    """Scheme E v5 rule layer (deterministic, zero LLM): returns (drop/keep/review, reason).

    Core idea: treat the "truncation placeholder" itself as a hard-defect signal, rather
    than using residual information as a weak proxy -- a truncated memory naturally keeps
    a complete prefix -> residual is sufficient -> the old scheme missed it; here, the
    presence of a placeholder string means drop.
    """
    t = text.strip()
    compact = "".join(t.split())
    if len(compact) < MEMORY_QUALITY_MIN_LEN:
        return "drop", f"too short ({len(compact)}<{MEMORY_QUALITY_MIN_LEN})"
    if _QUALITY_EMPTY_TITLE.match(t):
        return "drop", "empty-value title (no content after the title)"
    # Half-word truncation: ends with a residual EN/CN word fragment and is short overall
    if (_QUALITY_HALF_TRUNC.search(t) or _QUALITY_HALF_CN.search(t)) and len(
        compact
    ) < MEMORY_QUALITY_HALF_MAX:
        return "drop", "half-word truncation"
    # Core: unquoted truncation placeholder string -> drop. strip_quoted first, then
    # compact-normalize, so newlines/spaces no longer split the placeholder string
    # (Workflow measured the missed-match form 「内容截断，保留已有\n信息」).
    outside = _quality_strip_quoted(t)
    outside_compact = "".join(outside.split()).lower()
    for p in QUALITY_TRUNC_PLACEHOLDERS:
        if "".join(p.split()).lower() in outside_compact:
            return "drop", f"unquoted truncation placeholder string ({p})"
    if _QUALITY_TRUNC_RE.search(outside_compact):
        return "drop", "unquoted truncation placeholder string (regex fallback)"
    # Quoted truncation placeholder -> borderline (same compact match, likely meta-discussion)
    compact_all = "".join(t.split()).lower()
    if any(
        "".join(p.split()).lower() in compact_all for p in QUALITY_TRUNC_PLACEHOLDERS
    ) or _QUALITY_TRUNC_RE.search(compact_all):
        return (
            "review",
            "quoted truncation placeholder string (likely meta-discussion, sent to LLM for review)",
        )
    # Other placeholder words -> residual-information judgment
    if any(p.lower() in t.lower() for p in QUALITY_NONTRUNC_PLACEHOLDERS):
        # The residual metric counts CJK characters only; it reads every English-only text
        # as "0 residual" and would hard-drop legitimate English memories. Defer those to
        # the LLM judge instead.
        if not _has_cjk(t):
            return (
                "keep",
                "placeholder word in non-CJK text (residual metric not applicable)",
            )
        rc = _quality_residual_chars(t)
        if rc < MEMORY_QUALITY_RESIDUAL_MIN:
            return (
                "drop",
                f"placeholder word with insufficient residual information ({rc}<{MEMORY_QUALITY_RESIDUAL_MIN})",
            )
        return (
            "keep",
            f"placeholder word but sufficient residual information ({rc} chars)",
        )
    # Borderline samples: English snapshot starting with User / short CJK state snapshot
    if t and len(t) < 200 and _QUALITY_USER_SNAPSHOT.match(t):
        return (
            "review",
            "English snapshot starting with User (possibly a state snapshot, sent to LLM for review)",
        )
    if _quality_is_cn_short_snapshot(t):
        return (
            "review",
            "short CJK sentence containing a state verb (possibly a state snapshot, sent to LLM for review)",
        )
    return "keep", "passed as normal"


def _quality_llm_judge_once(text: str) -> str:
    """Single LLM quality judgment, returns keep/drop/ERR (parse failure or exception, fail-open)."""
    raw = _llm_chat(
        # The user prefix is a prompt too: it was hardcoded as `f"记忆内容：\n{text}"`,
        # which under English would pair a Chinese prefix with an English system. The
        # system and user each have their own template file (see prompt_loader).
        prompt_loader.prompt("quality_judge_user", text=text),
        temperature=0,
        max_tokens=200,
        system_prompt=prompt_loader.prompt("quality_judge"),
    )
    if not raw:
        return "ERR"
    m = re.search(r'"judge"\s*:\s*"(\w+)"', raw)
    j = m.group(1) if m else ""
    return j if j in ("keep", "drop") else "ERR"


def _quality_llm_review(reviews: list[dict]) -> tuple[list[dict], list[dict]]:
    """LLM 3-vote review of borderline samples: drop only when all 3 votes are drop, otherwise keep (fail-open).

    Lesson: fail-closed drop permanently harms high-value memories under concurrent
    rate limiting (measured 3/3 ERR); LLM judgment on borderline memories is
    probabilistic, so the most conservative policy avoids false positives, with the
    aggregate layer's retain=false as a backstop.
    """

    def vote(m: dict) -> tuple[dict, bool]:
        text = str(m.get("memory") or m.get("content") or "")
        votes = [
            _quality_llm_judge_once(text) for _ in range(MEMORY_QUALITY_REVIEW_VOTES)
        ]
        valid = [v for v in votes if v in ("keep", "drop")]
        return m, valid.count("drop") == MEMORY_QUALITY_REVIEW_VOTES

    with ThreadPoolExecutor(max_workers=MEMORY_QUALITY_LLM_THREADS) as ex:
        results = list(ex.map(vote, reviews))
    kept = [m for m, is_drop in results if not is_drop]
    dropped = [m for m, is_drop in results if is_drop]
    return kept, dropped


def _filter_memory_quality(memories: list[dict]) -> list[dict]:
    """Memory input quality gate (Scheme E v5): deterministic drop at the rule layer + LLM 3-vote review of borderline cases.

    Dropped memories are not marked processed -- they keep a chance to be re-evaluated next time.
    """
    kept: list[dict] = []
    reviews: list[dict] = []
    rule_dropped = 0
    for m in memories:
        text = str(m.get("memory") or m.get("content") or "")
        j, why = _quality_rule_judge(text)
        if j == "drop":
            rule_dropped += 1
            logger.debug(f"quality-gate rule drop: {why} | {text[:50]!r}")
        elif j == "review":
            reviews.append(m)
        else:
            kept.append(m)
    if reviews:
        r_kept, r_dropped = _quality_llm_review(reviews)
        logger.info(
            f"quality-gate LLM review of {len(reviews)} borderline items: drop {len(r_dropped)}, keep {len(r_kept)}"
        )
        kept += r_kept
    if rule_dropped:
        logger.info(f"quality-gate rule layer dropped {rule_dropped} entries")
    return kept
