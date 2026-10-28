"""Filesystem roots for the standalone gateway.

`GATEWAY_HOME` and `GATEWAY_CACHE` are the only knobs. During the host migration
`UTILITY_DATA_DIR` is still honored, and the legacy cache directory is reused
while it exists, so upgrading does not orphan existing data.
"""

from __future__ import annotations

import os
from pathlib import Path


def _path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def gateway_home() -> Path:
    """Writable state root: config, databases and notes."""
    explicit = os.environ.get("GATEWAY_HOME", "").strip()
    if explicit:
        return _path(explicit)
    legacy = os.environ.get("UTILITY_DATA_DIR", "").strip()
    if legacy:
        return _path(legacy)
    return _path("~/.config/agent-gateway")


def gateway_cache() -> Path:
    """Disposable cache root: logs, model caches and debug artifacts."""
    explicit = os.environ.get("GATEWAY_CACHE", "").strip()
    if explicit:
        return _path(explicit)
    if not os.environ.get("GATEWAY_HOME", "").strip():
        legacy = os.path.expanduser("~/.cache/utility")
        if os.path.isdir(legacy):
            return _path(legacy)
    return _path("~/.cache/agent-gateway")
