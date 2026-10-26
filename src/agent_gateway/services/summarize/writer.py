"""Writing refined facts into note files."""

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
    EXTRACTS_DIR,
    SUMMARY_DUP_SIM_THRESHOLD,
    SUMMARY_EXCLUDE_DIRS,
)
from agent_gateway.services.summarize.storage import (
    _existing_extract_tops,
    _normalize_slug,
    resolve_target_file,
    write_to_cold_layer,
)
from agent_gateway.services.summarize.textutil import _max_segment_similarity


def _collect_tags(memories: list[dict]) -> list[str]:
    return list(
        set(t for m in memories for t in (m.get("metadata") or {}).get("tags", []))
    )


def _strip_leading_h1(content: str) -> str:
    """Strip a leading # heading from the body, so it does not duplicate the file title written by the script."""
    lines = content.splitlines()
    while lines and re.match(r"^#\s+\S", lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()


def _merge_into_file(
    filepath: Path,
    title: str,
    content: str,
    today_str: str,
    is_append: bool,
    supersedes: bool = False,
) -> bool:
    """Write the file. Creates it when new; on append, merges by H1 section: a same-named
    H1 section is replaced (deduplicated), otherwise a new section is added at the end
    (history preserved). Returns whether the write succeeded."""
    content = _strip_leading_h1(content)
    supplement = t("mcp.summary.supplement_header", date=today_str)
    block = f"# {title}\n\n{supplement}\n\n{content}\n"
    if not is_append:
        filepath.write_text(
            f"# {title}\n\n{t('mcp.summary.auto_summary_header', date=today_str)}\n\n{content}\n",
            encoding="utf-8",
        )
        return True

    old = filepath.read_text(encoding="utf-8")
    target_slug = _normalize_slug(title)
    segments = re.split(r"(?m)^(?=#\s)", old)
    replaced = False
    out = []
    for seg in segments:
        head = seg.splitlines()[0] if seg.strip() else ""
        seg_slug = _normalize_slug(re.sub(r"^#\s+", "", head))
        if seg_slug and seg_slug == target_slug:
            # Same-named H1 section: keep the old content, append the new content as a
            # sub-section (prevents overwriting history). content has had its leading H1
            # stripped by _strip_leading_h1, so the added section needs # {title} to avoid a title-less block.
            old = seg.rstrip()
            if supersedes:
                # Correction type: mark the old section as deprecated at its end, to avoid old and new conclusions coexisting and contradicting each other
                old += "\n\n" + t("mcp.summary.deprecated_note")
            out.append(
                old
                + "\n\n---\n\n# "
                + title
                + "\n\n"
                + supplement
                + "\n\n"
                + content
                + "\n"
            )
            replaced = True
        else:
            out.append(seg)
    merged = "".join(out) if replaced else old.rstrip() + "\n\n---\n\n" + block
    filepath.write_text(merged.rstrip() + "\n", encoding="utf-8")
    return True


def _sanitize_new_dir(dir_name: str) -> str:
    """Sanitize the new directory given by the LLM: remove excluded directories; keep
    multi-level nesting only when its top level is an existing top level, otherwise fall
    back to a single level (defends against the LLM creating deep paths); when a new top
    level is in the same topic domain as an existing top level, fall back to folding into
    the existing directory (prevents cross-directory scattering + casually creating duplicate directories)."""
    raw = str(dir_name or "").strip().strip("/")
    parts = [p for p in Path(raw).parts if p and p not in SUMMARY_EXCLUDE_DIRS]
    if not parts:
        return ""
    existing_tops = _existing_extract_tops()
    if len(parts) > 1 and parts[0] not in existing_tops:
        logger.warning(
            f"new multi-level directory {raw}, falling back to single-level directory {parts[0]}"
        )
        return parts[0]
    # Shared-topic-word fallback: a new top-level directory name and some existing
    # top-level directory contain each other ("面板系统" vs "面板", "工具集" vs
    # "Utility工具集") -> same topic domain, fall back to folding into the existing
    # directory. The 3-character floor filters out generic short words like "技术/笔记";
    # matching in descending length order prefers the longer (more specific) existing
    # directory ("Utility工具集" matched before "工具集", picking the closest topic).
    if len(parts) == 1:
        new_top = parts[0]
        for top in sorted(existing_tops, key=len, reverse=True):
            if top == new_top:
                break
            if (
                len(new_top) >= 3
                and len(top) >= 3
                and (new_top in top or top in new_top)
            ):
                logger.warning(
                    f"new directory {new_top} shares a topic domain with existing directory {top}, folding into {top}"
                )
                return top
    return "/".join(parts)


def _write_articles_v2(
    results: list[dict],
    today_str: str,
    all_tags: list[str],
    source_memory_ids: list | None = None,
) -> dict[str, str]:
    """Phase 2 write: merge/fallback_merge write directly to the judged target; new goes
    through resolve_target_file's fallback to prevent collisions.
    Write exceptions are caught per item with try/except and logged as WRITE errors,
    without interrupting the remaining writes.
    Batch-write blind-spot fix: _run_refine's dup_skip reads the file "before batch
    write", so it cannot see earlier pending writes in the same run (same target, merge
    then fallback_merge); the write layer re-checks against the on-disk content before persisting."""
    updated = {}
    for item in results:
        try:
            if item["action"] in ("merge", "fallback_merge"):
                filepath = EXTRACTS_DIR / item["target"]
                filepath.parent.mkdir(parents=True, exist_ok=True)
                is_append = True
                # Batch-write blind-spot fix (a): when the same run already wrote this file
                # earlier, re-read the current on-disk content for a similarity re-check,
                # catching fallback_merge re-appending something highly similar to an earlier
                # section (measured sim 0.919).
                if str(filepath) in updated:
                    cur_text = filepath.read_text(encoding="utf-8")
                    sim = _max_segment_similarity(item.get("content", ""), cur_text)
                    if sim > SUMMARY_DUP_SIM_THRESHOLD:
                        logger.warning(
                            f"batch-write re-check: {item['target']} similarity {sim:.2f} to an"
                            f" earlier write in the same run too high, skipping"
                        )
                        continue
            else:
                dir_name = _sanitize_new_dir(item["dir"])
                target_dir = EXTRACTS_DIR / dir_name if dir_name else EXTRACTS_DIR
                target_dir.mkdir(parents=True, exist_ok=True)
                filepath, is_append = resolve_target_file(target_dir, item["title"])
            ok = _merge_into_file(
                filepath,
                item["title"],
                item["content"],
                today_str,
                is_append,
                supersedes=bool(item.get("supersedes", False)),
            )
            if not ok:
                continue
            # Lightweight QC: warn on abnormal file size (does not block)
            try:
                size = filepath.stat().st_size
                if size > 500_000:
                    logger.warning(f"QC: {filepath} abnormal size ({size // 1024}KB)")
            except OSError:
                pass
            updated[str(filepath)] = "append" if is_append else "new"
            write_to_cold_layer(
                content=item["content"],
                source_file=str(filepath),
                category=filepath.parent.name or "Extracts",
                tags=all_tags,
                confidence=1.0,
                source_memory_ids=source_memory_ids,
            )
        except Exception as e:
            write_error_log(
                f"write failed {item.get('topic', '')!r}: {e}", code="WRITE"
            )
            continue
    return updated
