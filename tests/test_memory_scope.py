"""Agent scope: mem0 owns agent_id, read-side filtering reads visibility."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core.memory_lifecycle import is_public, visible_to  # noqa: E402
from agent_gateway.services.mcp import server_memory  # noqa: E402
from agent_gateway.services.retrieval.arms import recall as recall_arm  # noqa: E402
from agent_gateway.services.retrieval.types import (  # noqa: E402
    Query,
    RetrievedItem,
)


class MemoryScopeTest(unittest.TestCase):
    def test_visibility_rules(self):
        self.assertTrue(visible_to({"visibility": "all"}, "agent-a"))
        self.assertTrue(
            visible_to(
                {"visibility": "agent-specific", "owner_agent_id": "agent-a"},
                "agent-a",
            )
        )
        self.assertFalse(
            visible_to(
                {"visibility": "agent-specific", "owner_agent_id": "agent-b"},
                "agent-a",
            )
        )
        self.assertFalse(visible_to({"visibility": "user-only"}, "agent-a"))

    def test_legacy_rows_without_owner_default_to_unknown(self):
        self.assertTrue(visible_to({"visibility": "agent-specific"}, "unknown"))
        self.assertFalse(visible_to({"visibility": "agent-specific"}, "agent-a"))

    def test_metadata_uses_owner_key_not_identity_key(self):
        meta = server_memory._build_metadata(
            {"agent_id": "agent-a", "visibility": "agent-specific"}, "2026-11-08"
        )
        self.assertEqual(meta["owner_agent_id"], "agent-a")
        self.assertEqual(meta["visibility"], "agent-specific")
        self.assertNotIn("agent_id", meta)

    def test_agent_specific_rejects_unknown_agent(self):
        result = server_memory._write_memory(
            {"content": "secret", "visibility": "agent-specific"}, "2026-11-08"
        )
        self.assertFalse(result["success"])
        self.assertIn("agent-specific", result["error"])

    def test_is_public(self):
        self.assertTrue(is_public(None))
        self.assertTrue(is_public({"visibility": "all"}))
        self.assertFalse(is_public({"visibility": "agent-specific"}))
        self.assertFalse(is_public({"visibility": "user-only"}))

    def test_expansion_receives_reader_agent(self):
        item = RetrievedItem(id="m1", kind="memory", score=1.0, score_kind="dense")
        with (
            mock.patch.object(recall_arm, "_expand_hot", return_value=[]) as hot,
            mock.patch.object(recall_arm, "_expand_cold", return_value=[]),
            mock.patch.object(recall_arm, "_expand_edges", return_value=[]) as edges,
            mock.patch.object(
                recall_arm.server_embed, "embed_query", return_value=None
            ),
            mock.patch.object(
                recall_arm.server_state, "get_hot_memory", return_value=object()
            ),
            mock.patch.object(recall_arm.server_state, "get_qdrant", return_value=None),
            mock.patch.object(recall_arm, "floors_for", return_value={"expanded": 0.5}),
        ):
            recall_arm.expand_episodes(
                Query(text="q"), [item], {"reader_agent_id": "agent-a"}
            )
        hot.assert_called_once_with([item], None, 0.5, "agent-a")
        edges.assert_called_once_with([item], None, 0.5, "agent-a")


if __name__ == "__main__":
    unittest.main()
