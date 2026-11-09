"""recall dimensions: absent fields never become hard filters."""

from __future__ import annotations

# ruff: noqa: E402 - temp QDRANT_PATH must be set before server_memory imports

import atexit
import os
import shutil
import tempfile
import unittest

_QDRANT = tempfile.mkdtemp(prefix="qdrant-dimensions-")
atexit.register(shutil.rmtree, _QDRANT, True)
os.environ.setdefault("QDRANT_PATH", _QDRANT)

from agent_gateway.services.mcp.server_memory import _recall_dimensions


class RecallDimensionsTest(unittest.TestCase):
    def test_empty_arguments_skip_filters(self):
        filters, boost = _recall_dimensions({})
        self.assertEqual(filters, {})
        self.assertEqual(boost, {})

    def test_project_is_hard_filter(self):
        filters, boost = _recall_dimensions({"project": "utility"})
        self.assertEqual(filters, {"project": "utility"})
        self.assertNotIn("project", boost)

    def test_subsystem_is_boost(self):
        filters, boost = _recall_dimensions({"subsystem": "memory"})
        self.assertEqual(filters, {})
        self.assertEqual(boost.get("subsystem"), "memory")


if __name__ == "__main__":
    unittest.main()
