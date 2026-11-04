"""Indexer registration: live PID, dead entries and shared state directories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_gateway.core import config_loader
from agent_gateway.services.graph import indexer_marker

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


class IndexerMarkerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {
            "GATEWAY_HOME": str(self.root / "home"),
            "GATEWAY_PROJECT_DIR": ".agent-gateway",
        }
        self._saved = {key: os.environ.get(key) for key in self._env}
        os.environ.update(self._env)
        config_loader.reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_loader.reset_cache()
        self._tmp.cleanup()

    def _marker(self) -> Path:
        return self.root / ".agent-gateway" / "indexer.json"

    def test_register_and_unregister_current_process(self):
        indexer_marker.register(str(self.root))
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [os.getpid()])
        data = json.loads(self._marker().read_text(encoding="utf-8"))
        self.assertEqual(data["indexers"][0]["root"], str(self.root.resolve()))

        indexer_marker.unregister(str(self.root))
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [])

    def test_dead_pid_is_removed(self):
        indexer_marker.register(str(self.root), pid=999999)
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [])
        data = json.loads(self._marker().read_text(encoding="utf-8"))
        self.assertEqual(data["indexers"], [])

    def test_corrupt_marker_is_treated_as_empty(self):
        state = self.root / ".agent-gateway"
        state.mkdir(parents=True)
        self._marker().write_text("{not-json", encoding="utf-8")
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [])
        self.assertFalse(indexer_marker.has_active(str(self.root)))

    def test_process_state_forces_utc(self):
        result = mock.Mock(stdout="S  Wed Nov  4 08:00:00 2026\n")
        with mock.patch.object(
            indexer_marker.subprocess, "run", return_value=result
        ) as run:
            state = indexer_marker._process_state(os.getpid())
        self.assertEqual(state, ("S", "Wed Nov  4 08:00:00 2026"))
        self.assertEqual(run.call_args.kwargs["env"]["TZ"], "UTC")

    def test_timezone_changes_do_not_hide_live_indexer(self):
        with mock.patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
            indexer_marker.register(str(self.root))
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            self.assertEqual(indexer_marker.active_pids(str(self.root)), [os.getpid()])

    def test_large_pid_is_ignored(self):
        state = self.root / ".agent-gateway"
        state.mkdir(parents=True)
        self._marker().write_text(
            json.dumps(
                {"indexers": [{"pid": 2**31, "root": str(self.root.resolve())}]}
            ),
            encoding="utf-8",
        )
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [])

    def test_start_time_mismatch_is_treated_as_reused_pid(self):
        indexer_marker.register(str(self.root))
        data = json.loads(self._marker().read_text(encoding="utf-8"))
        data["indexers"][0]["started_at"] = "Thu Jan  1 00:00:00 1970"
        self._marker().write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [])

    def test_entries_from_another_root_are_ignored(self):
        shared = self.root / "shared-state"
        os.environ["GATEWAY_PROJECT_DIR"] = str(shared)
        config_loader.reset_cache()
        other = self.root / "other"
        other.mkdir()

        indexer_marker.register(str(self.root))
        self.assertEqual(indexer_marker.active_pids(str(self.root)), [os.getpid()])
        self.assertEqual(indexer_marker.active_pids(str(other)), [])

    def test_repeated_sigterm_does_not_interrupt_cleanup(self):
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(SRC_ROOT) + (
            os.pathsep + existing if existing else ""
        )
        code = """
import sys, time
from agent_gateway.services.graph import indexer, indexer_marker

_original = indexer_marker.unregister

def _slow(root, pid=None):
    time.sleep(0.2)
    return _original(root, pid)

indexer_marker.unregister = _slow
sys.exit(indexer.main())
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            text=True,
        )
        try:
            for _ in range(100):
                if self._marker().is_file():
                    break
                time.sleep(0.01)
            self.assertTrue(self._marker().is_file())
            process.terminate()
            time.sleep(0.05)
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()
        data = json.loads(self._marker().read_text(encoding="utf-8"))
        self.assertEqual(data["indexers"], [])

    def test_main_without_root_does_not_register(self):
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(SRC_ROOT) + (
            os.pathsep + existing if existing else ""
        )
        subprocess.run(
            [sys.executable, "-m", "agent_gateway.services.graph.indexer"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.root,
            env=environment,
            timeout=10,
            check=True,
        )
        self.assertFalse(self._marker().exists())


if __name__ == "__main__":
    unittest.main()
