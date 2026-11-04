"""Read gateway env names with one-release aliases for the old host names."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("gateway.env")

_ALIASES = {
    "GATEWAY_HOME": "UTILITY_DATA_DIR",
    "GATEWAY_LANG": "UTILITY_LANG",
    "GATEWAY_LOG_MAX_SIZE": "UTILITY_LOG_MAX_SIZE",
    "GATEWAY_LOG_BACKUP_COUNT": "UTILITY_LOG_BACKUP_COUNT",
    "GATEWAY_USER_ID": "UTILITY_USER_ID",
    "MODEL_PROXY_TOOL_OUTPUT_DEDUP": "MODEL_PROXY_SKILL_DEDUP",
}

_warned: set[str] = set()


def get(name: str) -> str | None:
    """Explicit gateway name wins; the deprecated alias is read with a warning."""
    raw = os.environ.get(name)
    if raw is not None:
        return raw
    old = _ALIASES.get(name)
    if old is None:
        return None
    raw = os.environ.get(old)
    if raw is None:
        return None
    if old not in _warned:
        _warned.add(old)
        logger.warning("env %s is deprecated; use %s", old, name)
    return raw


def flag(name: str, default: bool = True) -> bool:
    """Boolean env with ``0`` meaning false."""
    raw = get(name)
    if raw is None:
        return default
    return raw.strip() != "0"
