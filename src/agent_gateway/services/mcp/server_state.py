"""Mutable runtime state shared by the MCP server modules.

`hot_memory` is initialized lazily and `qdrant` is created at import time; the
memory handlers and the archive scheduler live in separate modules now, so they
must read through these accessors instead of importing server.py (which would be
circular).
"""

from __future__ import annotations

_hot_memory = None
_qdrant = None


def get_hot_memory():
    return _hot_memory


def set_hot_memory(value) -> None:
    global _hot_memory
    _hot_memory = value


def get_qdrant():
    return _qdrant


def set_qdrant(value) -> None:
    global _qdrant
    _qdrant = value
