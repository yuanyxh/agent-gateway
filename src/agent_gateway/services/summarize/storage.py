"""Cold-layer sync, note scanning, target paths, and git commits."""

import hashlib
import re
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from agent_gateway.core.l10n import t
from agent_gateway.core.log_utils import setup_logging as _log_setup
from agent_gateway.core.memory_lifecycle import (
    COLD_SOURCE_MEMORY_IDS_FIELD,
)
from agent_gateway.services.summarize.summary_llm import (
    _HTTP,
    write_error_log,
)

from agent_gateway.services.summarize.constants import (
    COLD_SYNC_DB,
    EXTRACTS_DIR,
    MEMORY_SERVER_URL,
    NOTES_DIR,
    SUMMARY_EXCLUDE_DIRS,
    SUMMARY_GIT_TIMEOUT,
    SUMMARY_NOTE_EXTENSIONS,
    GATEWAY_HOME_DIR,
)


logger = _log_setup("summarize")


def _cold_already_synced(content: str, source_file: str) -> bool:
    """Dedup by source_file+content hash: skip content already synced, to avoid duplicate writes to the cold layer."""
    h = hashlib.sha256(f"{source_file}|{content}".encode("utf-8")).hexdigest()
    try:
        GATEWAY_HOME_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(COLD_SYNC_DB))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS synced (hash TEXT PRIMARY KEY, at TEXT)"
            )
            exists = conn.execute("SELECT 1 FROM synced WHERE hash=?", (h,)).fetchone()
            if exists:
                return True
            conn.execute(
                "INSERT OR IGNORE INTO synced (hash, at) VALUES (?, ?)",
                (h, date.today().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        return False
    except Exception:
        return False


def write_to_cold_layer(
    content: str,
    source_file: str,
    category: str,
    tags: list | None = None,
    confidence: float = 1.0,
    source_memory_ids: list | None = None,
):
    if _cold_already_synced(content, source_file):
        return
    try:
        resp = _HTTP.post(
            f"{MEMORY_SERVER_URL}/api/memory/add_knowledge",
            json={
                "content": content,
                "source_file": source_file,
                "created_at": date.today().isoformat(),
                "category": category,
                "tags": tags or [],
                "confidence": confidence,
                COLD_SOURCE_MEMORY_IDS_FIELD: source_memory_ids or [],
            },
        )
        if resp.status_code != 200:
            logger.warning(
                f"Cold layer write failed: {resp.status_code} {resp.text[:200]}"
            )
    except Exception as e:
        logger.warning(f"Cold layer write failed: {e}")


def scan_notes_structure(root: Path, max_depth: int = 3) -> str:
    EXCLUDED = SUMMARY_EXCLUDE_DIRS

    def _scan(dir_path: Path, depth: int = 0) -> list[str]:
        if depth > max_depth:
            return ["  " * depth + "  ..."]
        lines = []
        try:
            for entry in sorted(dir_path.iterdir()):
                name = entry.name
                if name.startswith(".") or name in EXCLUDED:
                    continue
                if entry.is_dir():
                    lines.append("  " * depth + f"- {name}/")
                    lines.extend(_scan(entry, depth + 1))
                elif entry.suffix.lower() in SUMMARY_NOTE_EXTENSIONS:
                    lines.append("  " * depth + f"- {name}")
        except PermissionError:
            lines.append("  " * depth + t("mcp.summary.no_permission"))
        return lines

    return "\n".join(_scan(root)) if root.exists() else t("mcp.summary.empty_tree")


def _normalize_slug(s: str) -> str:
    """Normalize a title/slug for fuzzy dedup matching."""
    # Remove parenthetical content: （...）(...)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    # Strip the .md suffix to avoid producing a .md.md double-suffix filename
    s = re.sub(r"\.md$", "", s, flags=re.IGNORECASE)
    # Normalize spaces, colons, slashes to underscores
    s = re.sub(r"[\s:/]+", "_", s)
    # Collapse multiple underscores
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


def resolve_target_file(target_dir: Path, title: str) -> tuple[Path, bool]:
    slug = _normalize_slug(title)
    if not slug:
        slug = t("mcp.summary.uncategorized")
    filepath = target_dir / f"{slug}.md"

    # Collect all existing .md files across all extract dirs
    all_extract_files: list[tuple[Path, str]] = []
    if EXTRACTS_DIR.exists():
        for d in EXTRACTS_DIR.rglob("*"):
            if d.is_file() and d.suffix == ".md":
                stem = d.stem
                existing_slug = re.sub(r"_\d{4}-\d{2}-\d{2}$", "", stem)
                existing_slug = _normalize_slug(existing_slug)
                if existing_slug:
                    all_extract_files.append((d, existing_slug))

    # Priority 1: exact slug match in the same directory
    for existing_path, existing_slug in all_extract_files:
        if existing_slug == slug and existing_path.parent == target_dir:
            return existing_path, True

    # Priority 2: substring match, limited to the same directory.
    # A cross-directory hit would append to an old file and misplace content, so we do
    # not match across directories; overly short slugs have a low fuzzy-match hit rate
    # and misfire easily (e.g. "devflow" would match everything), so only enable for >=4 chars.
    if len(slug) >= 4:
        for existing_path, existing_slug in all_extract_files:
            if existing_path.parent == target_dir and (
                slug in existing_slug or existing_slug in slug
            ):
                return existing_path, True

    return filepath, False


def git_commit(message: str, stage: str = ""):
    try:
        label = f" ({stage})" if stage else ""
        r_add = subprocess.run(
            ["git", "-C", str(NOTES_DIR), "add", "-A"],
            capture_output=True,
            timeout=SUMMARY_GIT_TIMEOUT,
            text=True,
        )
        if r_add.returncode != 0:
            err = (r_add.stderr or "").strip()
            write_error_log(f"git add failed{label}: {err[:300]}", code="GIT_ADD")
            return False
        r_cmt = subprocess.run(
            ["git", "-C", str(NOTES_DIR), "commit", "-m", message],
            capture_output=True,
            timeout=SUMMARY_GIT_TIMEOUT,
            text=True,
        )
        out = ((r_cmt.stdout or "") + (r_cmt.stderr or "")).strip()
        if r_cmt.returncode == 0:
            logger.info(f"[git]{label} {out[:200]}")
            return True
        elif r_cmt.returncode == 1 and "nothing to commit" in (r_cmt.stdout or ""):
            logger.info(f"[git]{label} no changes, skipping commit")
            return True
        else:
            write_error_log(f"git commit failed{label}: {out[:300]}", code="GIT_COMMIT")
            logger.warning(f"[git]{label} commit failed: {out[:200]}")
            return False
    except subprocess.TimeoutExpired:
        write_error_log(f"git operation timed out{label}", code="GIT_TIMEOUT")
        return False
    except Exception as e:
        write_error_log(f"git exception{label}: {e}", code="GIT_ERROR")
        logger.warning(f"[git]{label} commit failed: {e}")
        return False


def _existing_extract_tops() -> set[str]:
    """Set of existing top-level directory names in Extracts (reused for the directory whitelist / LLM prompt)."""
    if not EXTRACTS_DIR.exists():
        return set()
    return {d.name for d in EXTRACTS_DIR.iterdir() if d.is_dir()}
