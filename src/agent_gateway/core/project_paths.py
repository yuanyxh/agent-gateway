"""Project-local gateway state: graph and note caches.

New projects use `.agent-gateway/`. A legacy `.utility/` file is copied once,
never moved or deleted, so a downgrade still finds the original.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from agent_gateway.core import config_loader, env_compat

DEFAULT_STATE_DIR = ".agent-gateway"
LEGACY_STATE_DIR = ".utility"
GRAPH_DB = "graph.sqlite"
NOTE_DB = "note_vectors.sqlite"


def _name() -> str:
    raw = (env_compat.get("GATEWAY_PROJECT_DIR") or "").strip()
    if raw:
        return raw
    node = config_loader.value("graph.stateDir", DEFAULT_STATE_DIR)
    return node if isinstance(node, str) and node.strip() else DEFAULT_STATE_DIR


def project_state_dir(project_root: str) -> Path:
    raw = _name()
    candidate = Path(os.path.expanduser(raw))
    if candidate.is_absolute():
        return candidate
    return Path(os.path.abspath(project_root)) / candidate


def _legacy_dir(project_root: str) -> Path:
    return Path(os.path.abspath(project_root)) / LEGACY_STATE_DIR


def legacy_graph_db_path(project_root: str) -> Path:
    return _legacy_dir(project_root) / GRAPH_DB


def legacy_note_cache_path(project_root: str) -> Path:
    return _legacy_dir(project_root) / NOTE_DB


def graph_db_path(project_root: str) -> Path:
    return project_state_dir(project_root) / GRAPH_DB


def note_cache_path(project_root: str) -> Path:
    return project_state_dir(project_root) / NOTE_DB


def ensure_state_dir(project_root: str) -> Path:
    target = project_state_dir(project_root)
    os.makedirs(target, exist_ok=True)
    return target


def copy_legacy(legacy: Path, current: Path) -> Path:
    """Copy a legacy SQLite file when the new path is absent; keep the original.

    A plain file copy misses committed rows still in ``-wal``; the SQLite backup
    API reads a consistent snapshot across the main file and its WAL.
    """
    if current.exists() or not legacy.is_file():
        return current
    os.makedirs(current.parent, exist_ok=True)
    tmp = current.with_name(current.name + ".tmp")
    try:
        source = sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(str(tmp))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)
        shutil.copy2(legacy, tmp)
        for suffix in ("-wal", "-shm"):
            side = Path(str(legacy) + suffix)
            if side.is_file():
                shutil.copy2(side, Path(str(current) + suffix))
    os.replace(tmp, current)
    return current


def readable_graph_db(project_root: str) -> Path:
    """Preferred read path: the new DB, else the legacy DB if present."""
    current = graph_db_path(project_root)
    if current.is_file():
        return current
    legacy = legacy_graph_db_path(project_root)
    return legacy if legacy.is_file() else current


def readable_note_cache(project_root: str) -> Path:
    current = note_cache_path(project_root)
    if current.is_file():
        return current
    legacy = legacy_note_cache_path(project_root)
    return legacy if legacy.is_file() else current
