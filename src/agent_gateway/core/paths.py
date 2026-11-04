"""Filesystem roots for the standalone gateway.

`GATEWAY_HOME` and `GATEWAY_CACHE` are the only knobs. The host injects both;
without a host they default to the user's config and cache directories.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_gateway.core.env_compat import get as env_get


def _path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def gateway_home() -> Path:
    """Writable state root: config, databases and notes."""
    explicit = (env_get("GATEWAY_HOME") or "").strip()
    if explicit:
        return _path(explicit)
    return _path("~/.config/agent-gateway")


def gateway_cache() -> Path:
    """Disposable cache root: logs, model caches and debug artifacts."""
    explicit = (env_get("GATEWAY_CACHE") or "").strip()
    if explicit:
        return _path(explicit)
    return _path("~/.cache/agent-gateway")
