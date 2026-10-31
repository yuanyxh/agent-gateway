"""Per-call retrieval traces.

One JSON line per retrieval. Trace failures never affect the retrieval result:
observability must not become a new failure mode.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import deque
from datetime import datetime
from typing import Any

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.paths import gateway_cache

logger = setup_logging("server")

_ENABLED = os.environ.get("RETRIEVAL_TRACE", "1") != "0"
_RETENTION_DAYS = int(os.environ.get("RETRIEVAL_TRACE_DAYS", "14"))
_RING_SIZE = 500
_ring: deque[dict[str, Any]] = deque(maxlen=_RING_SIZE)
_lock = threading.Lock()
_write_errors = 0


def enabled() -> bool:
    return _ENABLED


def new_trace_id() -> str:
    return f"rt_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def trace_dir() -> str:
    return os.path.join(str(gateway_cache()), "retrieval")


def _prune(directory: str) -> None:
    try:
        cutoff = datetime.now().timestamp() - _RETENTION_DAYS * 86400
        for name in os.listdir(directory):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
    except OSError:
        pass


def record(payload: dict[str, Any]) -> None:
    """Append a trace line; never raises."""
    global _write_errors
    with _lock:
        _ring.append(payload)
    if not _ENABLED:
        return
    try:
        directory = trace_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{datetime.now():%Y-%m-%d}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        if datetime.now().minute == 0 and datetime.now().second < 5:
            _prune(directory)
    except Exception as e:  # noqa: BLE001 - tracing must not break retrieval
        _write_errors += 1
        if _write_errors in (1, 10, 100):
            logger.warning(f"[retrieval-trace] write failed ({_write_errors}): {e}")


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return list(_ring)[-limit:]


def stats() -> dict[str, Any]:
    with _lock:
        entries = list(_ring)
    by_status: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    latencies: list[float] = []
    for entry in entries:
        by_status[entry.get("status", "?")] = (
            by_status.get(entry.get("status", "?"), 0) + 1
        )
        by_tool[entry.get("tool", "?")] = by_tool.get(entry.get("tool", "?"), 0) + 1
        total = (entry.get("latency_ms") or {}).get("total")
        if isinstance(total, (int, float)):
            latencies.append(float(total))
    latencies.sort()

    def pct(p: float) -> float | None:
        if not latencies:
            return None
        index = min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))
        return round(latencies[index], 2)

    return {
        "samples": len(entries),
        "by_status": by_status,
        "by_tool": by_tool,
        "latency_p50": pct(0.5),
        "latency_p95": pct(0.95),
        "write_errors": _write_errors,
    }
