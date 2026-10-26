"""Optional Clash proxy-group switching over the mihomo Unix socket.

Every failure is a warning: proxy switching must never abort the digest run.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import urllib.parse
from pathlib import Path

from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("rss_daily")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost", timeout=5)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self._socket_path)


def _socket_candidates():
    yield os.path.join(tempfile.gettempdir(), "verge-mihomo.sock")
    yield "/tmp/verge/verge-mihomo.sock"


def _find_socket() -> str | None:
    for path in _socket_candidates():
        if not os.path.exists(path):
            continue
        conn = _UnixHTTPConnection(path)
        try:
            conn.request("GET", "/version")
            if conn.getresponse().status == 200:
                return path
        except OSError:
            continue
        finally:
            conn.close()
    return None


class ClashSwitcher:
    """Switch one proxy group to `target`, restore the previous selection afterwards."""

    def __init__(self, group: str, target: str, state_file: Path | None = None):
        self._group = group
        self._target = target
        self._socket: str | None = None
        self._backup = ""
        self._state_file = state_file

    def _request(self, method: str, path: str, body: dict | None = None):
        conn = _UnixHTTPConnection(self._socket)
        try:
            payload = json.dumps(body, ensure_ascii=False).encode() if body else None
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "ignore")
        finally:
            conn.close()

    def _write_state(self) -> None:
        """Persist the pending switch so a crash can be recovered on the next run."""
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(
                    {"group": self._group, "previous": self._backup}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("clash state write failed: %s", exc)

    def _drop_state(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.unlink(missing_ok=True)
        except OSError:
            pass

    def recover(self) -> None:
        """Restore a switch left behind by a previous crash (the `finally` never ran)."""
        if not self._state_file or not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._drop_state()
            return
        group = (data.get("group") or "").strip() if isinstance(data, dict) else ""
        previous = (
            (data.get("previous") or "").strip() if isinstance(data, dict) else ""
        )
        if not group or not previous:
            self._drop_state()
            return
        self._group = group
        self._backup = previous
        self._socket = _find_socket()
        if not self._socket:
            logger.warning(
                "clash state found but control socket missing; retry next run"
            )
            return
        self.restore()

    def _group_path(self) -> str:
        return "/proxies/" + urllib.parse.quote(self._group, safe="")

    def switch(self) -> bool:
        self._socket = _find_socket()
        if not self._socket:
            logger.warning("clash control socket not found, skip proxy switch")
            return False
        try:
            path = self._group_path()
            status, raw = self._request("GET", path)
            if status != 200:
                logger.warning("clash read group failed: HTTP %s", status)
                return False
            self._backup = (json.loads(raw) or {}).get("now") or ""
            if not self._backup or self._backup == self._target:
                return False
            status, _ = self._request("PUT", path, {"name": self._target})
            if status not in (200, 204):
                logger.warning("clash switch failed: HTTP %s", status)
                return False
            logger.info(
                "clash group %s: %s -> %s", self._group, self._backup, self._target
            )
            self._write_state()
            return True
        except (OSError, ValueError) as exc:
            logger.warning("clash switch error: %s", exc)
            return False

    def restore(self) -> None:
        if not (self._socket and self._backup):
            return
        try:
            self._request("PUT", self._group_path(), {"name": self._backup})
            logger.info("clash group %s restored to %s", self._group, self._backup)
            self._drop_state()
        except OSError as exc:
            logger.warning("clash restore error: %s", exc)
