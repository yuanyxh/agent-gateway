#!/usr/bin/env python3
"""Memory-summary pipeline entry point.

Triggered by the Utility TaskScheduler. Modules: constants, runtime, storage,
memory, quality, aggregate, textutil, refine, writer. This file keeps only main().
"""

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

from agent_gateway.services.summarize.constants import EXTRACTS_DIR, NOTES_DIR
from agent_gateway.services.summarize.runtime import (
    _acquire_single_instance,
    _mark_run_success,
    _throttle_ai_run,
)
from agent_gateway.services.summarize.memory import (
    _filter_by_tags,
    _filter_processed,
    _mark_processed,
    fetch_memories,
)
from agent_gateway.services.summarize.quality import _filter_memory_quality
from agent_gateway.services.summarize.storage import git_commit, scan_notes_structure
from agent_gateway.services.summarize.aggregate import _run_aggregate_v2
from agent_gateway.services.summarize.refine import _run_refine
from agent_gateway.services.summarize.writer import _collect_tags, _write_articles_v2


def main():
    if not _acquire_single_instance():
        print("⚠️ A summarize instance is already running, skipping this run")
        return

    if not AI_API_KEY:
        write_error_log("AI_API_KEY not set", code="CONFIG")
        print("❌ AI_API_KEY not set")
        return

    if not _throttle_ai_run():
        return

    today = date.today()
    today_str = today.isoformat()
    EXTRACTS_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Fetching memories from past 3 days via REST API...")
    recent_memories = fetch_memories(today)
    if recent_memories is None:
        print("❌ Failed to fetch memories")
        return
    if not recent_memories:
        print("No new memories in the past 3 days, skipping summary")
        return

    # Input filtering: skip processed + excluded tags + quality gate (better none than a bad one); collect only the ids of passing memories
    skipped_processed = len(recent_memories)
    recent_memories = _filter_processed(recent_memories)
    skipped_processed -= len(recent_memories)
    skipped_tags = len(recent_memories)
    recent_memories = _filter_by_tags(recent_memories)
    skipped_tags -= len(recent_memories)
    skipped_quality = len(recent_memories)
    recent_memories = _filter_memory_quality(recent_memories)
    skipped_quality -= len(recent_memories)
    if skipped_processed or skipped_tags or skipped_quality:
        logger.info(
            f"input filtering: processed skipped {skipped_processed}, tag filter skipped {skipped_tags}, low-quality dropped {skipped_quality}, "
            f"{len(recent_memories)} remaining"
        )
    consumed_ids = [m["id"] for m in recent_memories if m.get("id")]
    if not recent_memories:
        print(
            "No processable new memories (all processed or failing quality), skipping summary"
        )
        return

    logger.info("Scanning notes directory structure...")
    dir_tree = scan_notes_structure(NOTES_DIR)

    # Phase 1: memory aggregation (classify / merge / dedupe / timeline, recursive convergence)
    logger.info(f"Aggregating {len(recent_memories)} memories into groups...")
    groups, agg_rounds = _run_aggregate_v2(recent_memories)
    if groups is None:
        print("⚠️ Memory aggregation failed, skipping this summary")
        return
    if not groups:
        return

    # Phase 2: per group "keywords -> search Extracts -> decide merge/new -> think enable generate"
    logger.info(
        f"Phase 2: Refining {len(groups)} groups (keyword → search → merge/new)..."
    )
    writes, refine_stats = _run_refine(groups, dir_tree)
    try:
        _stats_dump = dict(refine_stats)
        if isinstance(_stats_dump.get("hit_files"), set):
            _stats_dump["hit_files"] = sorted(_stats_dump["hit_files"])
        print("REFINE_STATS=" + json.dumps(_stats_dump, ensure_ascii=False))
    except Exception:
        pass
    if not writes:
        print(
            "All article generation failed or content was too short, skipping summary"
        )
        return

    all_tags = _collect_tags(recent_memories)
    updated_files = _write_articles_v2(
        writes, today_str, all_tags, source_memory_ids=consumed_ids
    )

    # Mark as processed only when something was written: set bit0 on successfully consumed
    # memories to avoid reprocessing next round. Without any write (this round everything was
    # judged by the LLM to have no sedimentation value), do not mark; re-evaluate next round.
    if updated_files and consumed_ids:
        _mark_processed(consumed_ids)

    # Phase 3: rules extraction (rule/convention memories -> MCP/Rules/*.md)
    phase3 = _run_phase3(groups, today_str)
    rules_updates = phase3.get("updated", "")
    if rules_updates:
        git_commit(t("mcp.summary.commit_rules", files=rules_updates), stage="rules")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    overview = t(
        "mcp.summary.commit_overview",
        time=now_str,
        facts=len(recent_memories),
        files=len(updated_files),
    )
    git_commit(overview, stage="after write")

    _mark_run_success()

    _print_report_v2(
        today_str,
        len(recent_memories),
        updated_files,
        rules_updates,
        refine_stats,
        agg_rounds,
        skipped_processed,
        skipped_quality,
    )


if __name__ == "__main__":
    main()
