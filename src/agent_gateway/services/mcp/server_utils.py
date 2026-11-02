"""Small helpers shared by server.py and server_memory.py."""

from __future__ import annotations

import asyncio
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


async def run_with_timeout(fn, *args, timeout: float, **kwargs):
    """Run a blocking call off the loop; return `(result, timed_out)`.

    The worker thread cannot be cancelled: the deadline only stops the wait, so
    a long rebuild keeps running and its late result is discarded.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs), timeout=timeout
        )
        return result, False
    except asyncio.TimeoutError:
        return None, True
