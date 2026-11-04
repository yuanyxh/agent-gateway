"""Background graph refresh scheduling and guard rails."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_gateway.core import config_loader
from agent_gateway.services.graph import indexer_marker, project_scan, refresh


class RefreshTest(unittest.TestCase):
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
        with refresh._TIMERS_LOCK:
            timers = list(refresh._TIMERS.values())
            refresh._TIMERS.clear()
        for timer in timers:
            timer.cancel()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_loader.reset_cache()
        self._tmp.cleanup()

    def test_delay_is_thirty_seconds(self):
        self.assertEqual(refresh.REFRESH_DELAY_SECONDS, 30.0)

    def test_schedule_is_deduplicated_per_project(self):
        with mock.patch.object(indexer_marker, "has_active", return_value=False):
            self.assertTrue(
                refresh.schedule_project_graph_refresh(str(self.root), delay=60)
            )
            self.assertFalse(
                refresh.schedule_project_graph_refresh(str(self.root), delay=60)
            )
        self.assertEqual(len(refresh._TIMERS), 1)

    def test_live_indexer_skips_scheduling(self):
        with mock.patch.object(indexer_marker, "has_active", return_value=True):
            self.assertFalse(
                refresh.schedule_project_graph_refresh(str(self.root), delay=60)
            )
        self.assertEqual(refresh._TIMERS, {})

    def test_missing_project_is_rejected(self):
        self.assertFalse(
            refresh.schedule_project_graph_refresh(str(self.root / "missing"), delay=60)
        )

    def test_refresh_rechecks_live_indexer(self):
        with (
            mock.patch.object(indexer_marker, "has_active", return_value=True),
            mock.patch.object(project_scan, "sync") as sync,
        ):
            refresh._run_refresh(str(self.root))
        sync.assert_not_called()

    def test_refresh_runs_sync_without_live_indexer(self):
        with (
            mock.patch.object(indexer_marker, "has_active", side_effect=[False, False]),
            mock.patch.object(project_scan, "sync", return_value={"ok": True}) as sync,
        ):
            refresh._run_refresh(str(self.root))
        sync.assert_called_once()
        self.assertEqual(refresh._TIMERS, {})

    def test_refresh_swallows_sync_errors(self):
        with (
            mock.patch.object(indexer_marker, "has_active", side_effect=[False, False]),
            mock.patch.object(project_scan, "sync", side_effect=RuntimeError("boom")),
        ):
            refresh._run_refresh(str(self.root))
        self.assertEqual(refresh._TIMERS, {})

    def test_refresh_skips_when_another_process_holds_lock(self):
        with (
            mock.patch.object(indexer_marker, "has_active", side_effect=[False, False]),
            mock.patch.object(project_scan, "sync") as sync,
            mock.patch.object(refresh.fcntl, "flock", side_effect=BlockingIOError),
        ):
            refresh._run_refresh(str(self.root))
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
