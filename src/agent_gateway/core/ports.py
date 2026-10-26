"""Local port probing and listener cleanup.

The Swift host probes ports by bind test; a self-managed Python service needs the
same primitives without a Swift parent. Cleanup must only ever target listeners
(`-sTCP:LISTEN`), never clients connected to the port.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

_HOST = "127.0.0.1"
_LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "lsof")
_SS_CANDIDATES = ("/usr/sbin/ss", "/sbin/ss", "/usr/bin/ss", "ss")
_PID_RE = re.compile(r"pid=(\d+)")


def is_available(port: int, host: str = _HOST) -> bool:
    """Whether 127.0.0.1:port can be bound right now (bind only, no listen)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_available(start: int, attempts: int = 100, host: str = _HOST) -> int:
    """First bindable port at or after `start`; falls back to `start` so the real bind reports."""
    for port in range(start, start + attempts):
        if is_available(port, host):
            return port
    return start


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _tool(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate.startswith("/"):
            if os.access(candidate, os.X_OK):
                return candidate
        elif shutil.which(candidate):
            return candidate
    return None


def _lsof_pids(port: int) -> list[int]:
    lsof = _tool(_LSOF_CANDIDATES)
    if lsof is None:
        return []
    # `-sTCP:LISTEN` is mandatory: without it lsof also returns clients connected
    # to the port, and killing them takes down unrelated software.
    out = _run([lsof, f"-ti:{port}", "-sTCP:LISTEN"])
    return [int(line) for line in out.split() if line.isdigit()]


def _ss_pids(port: int) -> list[int]:
    ss = _tool(_SS_CANDIDATES)
    if ss is None:
        return []
    out = _run([ss, "-lptnH", f"sport = :{port}"])
    return sorted({int(match) for match in _PID_RE.findall(out)})


def listener_pids(port: int) -> list[int]:
    """PIDs listening on `port`, empty when unknown (missing tools or permission)."""
    if sys.platform == "darwin":
        return sorted(set(_lsof_pids(port)))
    return sorted(set(_ss_pids(port) or _lsof_pids(port)))


def _wait_available(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_available(port):
            return True
        time.sleep(0.1)
    return is_available(port)


def terminate_listener(port: int, timeout: float = 3.0) -> bool:
    """Stop whatever listens on `port`; returns whether the port is free afterwards."""
    pids = [pid for pid in listener_pids(port) if pid != os.getpid()]
    if not pids:
        return is_available(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            return False
    if _wait_available(port, timeout):
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return _wait_available(port, 1.0)
