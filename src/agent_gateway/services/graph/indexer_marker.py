"""Register live graph indexers per project.

The registry is advisory: a missing or damaged file means "no indexer", never
an error. Entries hold the PID and the process start time so a reused PID is
not mistaken for a running indexer.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent_gateway.core import project_paths

MARKER_NAME = "indexer.json"
LOCK_NAME = "indexer.lock"


def _root_path(root: str) -> str:
    return os.path.realpath(root)


def _marker_path(root: str) -> Path:
    return project_paths.project_state_dir(root) / MARKER_NAME


def _lock_path(root: str) -> Path:
    return project_paths.project_state_dir(root) / LOCK_NAME


@contextmanager
def _locked(root: str) -> Iterator[Path]:
    project_paths.ensure_state_dir(root)
    with open(_lock_path(root), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield _marker_path(root)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("indexers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps({"indexers": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _process_state(pid: int) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2.0,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    parts = line[0].split(None, 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1].strip()


def _entry_alive(entry: dict) -> bool:
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    state = _process_state(pid)
    if state is None:
        return True
    status, started_at = state
    if status.startswith("Z"):
        return False
    recorded = entry.get("started_at")
    if isinstance(recorded, str) and recorded and started_at and recorded != started_at:
        return False
    return True


def register(root: str, pid: int | None = None) -> None:
    """Record the current (or supplied) indexer process for ``root``."""
    root = _root_path(root)
    pid = os.getpid() if pid is None else pid
    state = _process_state(pid)
    entry = {
        "pid": pid,
        "started_at": state[1] if state is not None else "",
        "root": root,
    }
    with _locked(root) as path:
        entries = [
            item
            for item in _read_entries(path)
            if not (item.get("pid") == pid and item.get("root") == root)
        ]
        entries.append(entry)
        _write_entries(path, entries)


def unregister(root: str, pid: int | None = None) -> None:
    """Remove one indexer from the registry; keep the file itself."""
    root = _root_path(root)
    pid = os.getpid() if pid is None else pid
    if not _marker_path(root).is_file():
        return
    with _locked(root) as path:
        entries = [
            item
            for item in _read_entries(path)
            if not (item.get("pid") == pid and item.get("root") == root)
        ]
        _write_entries(path, entries)


def active_pids(root: str) -> list[int]:
    """Return live indexer PIDs for ``root`` and prune dead entries."""
    root = _root_path(root)
    path = _marker_path(root)
    if not path.is_file():
        return []
    with _locked(root) as locked_path:
        entries = _read_entries(locked_path)
        kept = [
            item for item in entries if item.get("root") != root or _entry_alive(item)
        ]
        if len(kept) != len(entries):
            _write_entries(locked_path, kept)
    return [
        int(item["pid"])
        for item in kept
        if item.get("root") == root
        and isinstance(item.get("pid"), int)
        and int(item["pid"]) > 0
    ]


def has_active(root: str) -> bool:
    return bool(active_pids(root))
