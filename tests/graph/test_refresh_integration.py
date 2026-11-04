"""Real project rebuild through the background refresh path."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway.core import config_loader
from agent_gateway.services.graph import refresh
from agent_gateway.services.mcp.graph_tools import tool_project_lookup


class RefreshIntegrationTest(unittest.TestCase):
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
        (self.root / "demo.py").write_text("def demo():\n    return 1\n")

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

    def test_refresh_builds_missing_graph(self):
        refresh._run_refresh(str(self.root))
        db = self.root / ".agent-gateway" / "graph.sqlite"
        self.assertTrue(db.is_file())
        lookup = tool_project_lookup(str(self.root), "demo")
        self.assertEqual(lookup["status"], "ok", lookup)
        self.assertEqual([row["name"] for row in lookup["results"]], ["demo"])

    def test_refresh_picks_up_changed_file(self):
        refresh._run_refresh(str(self.root))
        (self.root / "demo.py").write_text(
            "def demo():\n    return 1\n\ndef added():\n    return 2\n"
        )
        refresh._run_refresh(str(self.root))
        lookup = tool_project_lookup(str(self.root), "added")
        self.assertEqual(lookup["status"], "ok", lookup)
        self.assertEqual([row["name"] for row in lookup["results"]], ["added"])

    def test_scheduled_timer_builds_in_background(self):
        self.assertTrue(
            refresh.schedule_project_graph_refresh(str(self.root), delay=0.05)
        )
        db = self.root / ".agent-gateway" / "graph.sqlite"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not db.is_file():
            time.sleep(0.01)
        self.assertTrue(db.is_file())
        while time.monotonic() < deadline and refresh._TIMERS:
            time.sleep(0.01)
        self.assertEqual(refresh._TIMERS, {})


if __name__ == "__main__":
    unittest.main()
