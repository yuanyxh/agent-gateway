"""Small helpers shared by server.py and server_memory.py."""

from __future__ import annotations

import json


def dumps(obj) -> str:
    """Unified outlet for tool output: keep CJK as UTF-8 instead of \\uXXXX escapes."""
    return json.dumps(obj, ensure_ascii=False)


def int_arg(arguments: dict, key: str, default: int) -> int:
    """Read an integer argument; missing/None/non-integer all fall back to default.

    An explicit `null` must not reach slicing (`found[:None]` means no truncation).
    """
    try:
        return int(arguments.get(key, default))
    except (TypeError, ValueError):
        return default
