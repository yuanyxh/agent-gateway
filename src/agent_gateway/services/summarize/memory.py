"""Memory fetch, tag/processed filters, and fact text building."""

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
    MEMORY_SERVER_URL,
    SUMMARY_MEMORY_DAYS,
    SUMMARY_MEMORY_LIMIT,
)


def fetch_memories(today: date) -> list | None:
    try:
        three_days_ago = datetime.combine(
            today - timedelta(days=SUMMARY_MEMORY_DAYS),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        resp = _HTTP.get(
            f"{MEMORY_SERVER_URL}/api/memories/recent",
            params={"since": three_days_ago.isoformat(), "limit": SUMMARY_MEMORY_LIMIT},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        write_error_log(f"Failed to fetch memories: {e}", code="FETCH_MEMORY")
        return None


def _filter_processed(memories: list[dict]) -> list[dict]:
    """Skip summarized (bit0) and retired (bit1) memories, plus non-active lifecycle states.

    bit1 is the belt-and-braces signal: even a consumer that only reads processed will
    not re-digest content a newer revision replaced.
    """
    kept = []
    for m in memories:
        # metadata may be explicitly null (key present, value None); `.get("metadata", {})` would return None and raise AttributeError
        meta = m.get("metadata") or {}
        flags = int(meta.get(PROCESSED_FIELD, 0) or 0)
        if flags & PROCESSED_CONSUMED_MASK:
            continue
        if str(meta.get(STATUS_FIELD) or STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        kept.append(m)
    return kept


def _filter_by_tags(memories: list[dict]) -> list[dict]:
    """Skip memories containing excluded tags (workflow/format entries, not technical knowledge).

    The excluded-tag list is read from the SUMMARY_EXCLUDE_TAGS environment variable
    (comma-separated), or from the mcp.excludeTags config in config.yaml.
    """
    exclude = summary_exclude_tags()
    if not exclude:
        return memories
    kept = []
    skipped = 0
    for m in memories:
        tags = set((m.get("metadata") or {}).get("tags", []))
        if tags & exclude:
            skipped += 1
            logger.debug(
                "tag filter skip: tags=%s | %s",
                tags & exclude,
                str(m.get("memory", ""))[:50],
            )
        else:
            kept.append(m)
    if skipped:
        logger.info(
            f"tag filter skipped {skipped} entries (excluded tags: {exclude}), {len(kept)} remaining"
        )
    return kept


def _mark_processed(memory_ids: list[str], bit: int = 0) -> int:
    """Batch-set the processed bit to 1 for memories (called after a successful write). A failure does not block the summary; returns the success count."""
    if not memory_ids:
        return 0
    try:
        resp = _HTTP.post(
            f"{MEMORY_SERVER_URL}/api/memories/processed",
            json={"memory_ids": memory_ids, "bit": bit, "value": 1},
        )
        resp.raise_for_status()
        updated = resp.json().get("updated", 0)
        logger.info(f"Marked {updated}/{len(memory_ids)} memories as processed")
        return updated
    except Exception as e:
        logger.warning(f"Failed to mark as processed: {e}")
        return 0


def _memory_date(m: dict) -> str:
    """Extract the MM-DD date from a memory entry; prefers created_at (the server's recent
    endpoint already filters and sorts by it), returns an empty string on missing key/exception."""
    raw = m.get("created_at") or m.get("updated_at") or ""
    return raw[5:10] if len(raw) >= 10 else ""


def _build_facts_text_v2(memories: list[dict]) -> str:
    # A memory item's body is under the `memory` key (returned by the REST API); the
    # `content` key does not exist -> reading the wrong one yields empty
    # Straight double quotes are normalized to CJK quotes (alternating open/close as a
    # pair): when the LLM copies source text it carries bare quotes into JSON string
    # values without escaping, causing downstream _parse_json_any to fail (measured 2026-08-05)
    def _clean(text: str) -> str:
        out = []
        open_q = True
        for ch in text:
            if ch == '"':
                out.append("“" if open_q else "”")
                open_q = not open_q
            else:
                out.append(ch)
        return "".join(out)

    return "\n".join(
        f"- [{','.join(m.get('metadata', {}).get('tags', []))}] "
        f"[{_memory_date(m) or t('mcp.summary.unknown_date')}] "
        f"{_clean(m.get('memory') or m.get('content') or '')}"
        for m in memories
    )
