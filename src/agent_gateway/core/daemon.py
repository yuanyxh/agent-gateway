"""Resident-service helpers: runtime state file, health probe, shutdown signals.

The runtime state file tells a local viewer which port and PID a self-managed
service took. It is written atomically so a reader never sees a partial file.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

SERVICE_MCP = "mcp"
SERVICE_PROXY = "proxy"

_STATE_DIR = "runtime"


def state_dir() -> Path | None:
    """`<config root>/runtime`; None when the root was not injected."""
    root = os.environ.get("UTILITY_DATA_DIR", "").strip()
    if not root:
        return None
    return Path(os.path.expanduser(root)) / _STATE_DIR


def state_path(service: str) -> Path | None:
    root = state_dir()
    return root / f"{service}.json" if root else None


def write_state(
    service: str, port: int, pid: int | None = None, **extra: Any
) -> Path | None:
    """Record `{service, port, pid, startedAt}` for a running service."""
    path = state_path(service)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "service": service,
        "port": int(port),
        "pid": int(pid if pid is not None else os.getpid()),
        "startedAt": time.time(),
        **extra,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_state(service: str) -> dict[str, Any] | None:
    path = state_path(service)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_state(service: str) -> None:
    path = state_path(service)
    if path is not None:
        path.unlink(missing_ok=True)


def health_ok(url: str, timeout: float = 1.5) -> bool:
    """HTTP 2xx probe; any transport error counts as unhealthy."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until(
    predicate: Callable[[], bool], timeout: float, interval: float = 0.2
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def install_stop_handlers(stop: threading.Event) -> None:
    """Map SIGTERM/SIGINT/SIGHUP to `stop.set()` (main thread only)."""

    def _handle(_signum: int, _frame: object) -> None:
        stop.set()

    signals = [signal.SIGTERM, signal.SIGINT]
    hup = getattr(signal, "SIGHUP", None)
    if hup is not None:
        signals.append(hup)
    for sig in signals:
        signal.signal(sig, _handle)
