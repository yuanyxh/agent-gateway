"""Schedule one background graph rebuild per project.

Queries never scan the tree. They ask for a timer; the timer checks the indexer
registry again, takes ``refresh.lock`` and lets ``project_scan.sync`` decide
whether the graph is actually stale.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from pathlib import Path

from agent_gateway.core import project_paths
from agent_gateway.services.graph import indexer_marker
from agent_gateway.services.graph.constants import ENGINE_VERSION, SCHEMA_VERSION

REFRESH_DELAY_SECONDS = 30.0
REFRESH_LOCK_NAME = "refresh.lock"

logger = logging.getLogger(__name__)

_TIMERS: dict[str, threading.Timer] = {}
_TIMERS_LOCK = threading.Lock()


def _lock_path(root: str) -> Path:
    return project_paths.project_state_dir(root) / REFRESH_LOCK_NAME


def schedule_project_graph_refresh(
    root: str, delay: float = REFRESH_DELAY_SECONDS
) -> bool:
    """Schedule a rebuild unless an indexer or an existing timer is active."""
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        return False
    if indexer_marker.has_active(root):
        return False
    with _TIMERS_LOCK:
        timer = _TIMERS.get(root)
        if timer is not None and timer.is_alive():
            return False
        timer = threading.Timer(delay, _run_refresh, args=(root,))
        timer.daemon = True
        _TIMERS[root] = timer
        timer.start()
    return True


def _run_refresh(root: str) -> None:
    lock_handle = None
    try:
        if indexer_marker.has_active(root):
            return
        project_paths.ensure_state_dir(root)
        lock_handle = open(_lock_path(root), "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        if indexer_marker.has_active(root):
            return
        from agent_gateway.services.graph import project_scan

        project_scan.sync(root, ENGINE_VERSION, SCHEMA_VERSION)
    except Exception:
        logger.exception("project graph refresh failed: %s", root)
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_handle.close()
        with _TIMERS_LOCK:
            _TIMERS.pop(root, None)
