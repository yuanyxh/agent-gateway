"""Single-instance lock and AI run throttle."""

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
    LAST_RUN_FILE,
    LOCK_FILE,
    MIN_RUN_INTERVAL,
    UTILITY_DATA_DIR,
)


def _acquire_single_instance() -> bool:
    """Acquire the single-instance file lock. Returns False when an instance is already running (the caller exits)."""
    try:
        UTILITY_DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            os.close(fd)
            return False
    except Exception:
        return True


def _throttle_ai_run() -> bool:
    """Returns False when less than MIN_RUN_INTERVAL has elapsed since the last full summary (prevents duplicate triggering).

    Read-only check; does not update last_run -- only _mark_run_success() records the
    time after a summary completes successfully, avoiding "write at run start" making a
    retry impossible within the 6h window after a failure.
    """
    try:
        UTILITY_DATA_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        last = (
            float(LAST_RUN_FILE.read_text().strip()) if LAST_RUN_FILE.exists() else 0.0
        )
        if now - last < MIN_RUN_INTERVAL:
            logger.info(
                f"Less than {MIN_RUN_INTERVAL // 3600} hours since the last summary, skipping (duplicate-trigger guard)"
            )
            return False
        return True
    except Exception:
        return True


def _mark_run_success() -> None:
    """Record last_run after a summary completes successfully, starting a new duplicate-prevention cooldown."""
    try:
        UTILITY_DATA_DIR.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.write_text(str(time.time()))
    except Exception:
        pass
