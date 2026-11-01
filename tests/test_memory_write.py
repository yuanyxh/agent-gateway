"""Tests for the write contract: fact/context split and the embedded text."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402
from agent_gateway.services.retrieval.arms.recall import project_hot_result  # noqa: E402


def _patched(similar=None):
    hot = mock.Mock()
    hot.add.return_value = {"results": [{"id": "new"}]}
    hot.get.return_value = {"metadata": {}}
    return hot, (
        mock.patch.object(server_memory, "get_hot_memory", return_value=hot),
        mock.patch.object(server_memory, "_active_similar", return_value=similar or []),
        mock.patch.object(server_memory, "_mark_superseded", return_value=([], [])),
        mock.patch.object(server_memory, "_mark_cold_stale", return_value=0),
        mock.patch.object(server_memory.memory_link, "record_hot_memory"),
    )


class WriteContractTest(unittest.TestCase):
    def _write(self, item, similar=None):
        hot, patches = _patched(similar)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = server_memory._write_memory(item, "2026-01-01T00:00:00")
        return hot, result

    def test_embedded_text_joins_fact_and_context(self):
        hot, _ = self._write({"content": "fact", "context": "why it matters"})

        embedded = hot.add.call_args[0][0]
        self.assertEqual(embedded, "fact\nContext: why it matters")
        metadata = hot.add.call_args[1]["metadata"]
        self.assertEqual(metadata["fact"], "fact")
        self.assertEqual(metadata["context"], "why it matters")

    def test_embedded_text_is_the_fact_alone_without_context(self):
        hot, _ = self._write({"content": "fact"})

        self.assertEqual(hot.add.call_args[0][0], "fact")
        self.assertEqual(hot.add.call_args[1]["metadata"]["context"], "")

    def test_type_and_sources_are_stored(self):
        hot, _ = self._write(
            {
                "content": "fact",
                "type": "decision",
                "source_files": ["a.py"],
                "source_url": "https://example.com",
            }
        )

        metadata = hot.add.call_args[1]["metadata"]
        self.assertEqual(metadata["type"], "decision")
        self.assertEqual(metadata["source_files"], ["a.py"])
        self.assertEqual(metadata["source_url"], "https://example.com")

    def test_same_fact_in_another_context_is_not_deduped(self):
        similar = [
            {
                "id": "old",
                "score": 0.99,
                "fact": "fact",
                "context": "old context",
                "episode_id": "",
            }
        ]
        _, result = self._write({"content": "fact", "context": "new context"}, similar)

        self.assertFalse(result["deduped"])
        self.assertEqual(result["memory_id"], "new")

    def test_same_fact_and_context_is_deduped(self):
        similar = [
            {
                "id": "old",
                "score": 0.99,
                "fact": "fact",
                "context": "same",
                "episode_id": "",
            }
        ]
        _, result = self._write({"content": "fact", "context": "same"}, similar)

        self.assertTrue(result["deduped"])
        self.assertEqual(result["memory_id"], "old")

    def test_importance_and_protected_are_stored(self):
        hot, _ = self._write({"content": "fact", "importance": 9, "protected": True})

        metadata = hot.add.call_args[1]["metadata"]
        self.assertEqual(metadata["importance"], 9)
        self.assertTrue(metadata["protected"])

    def test_importance_is_clamped_to_the_published_scale(self):
        hot, _ = self._write({"content": "fact", "importance": 99})

        self.assertEqual(hot.add.call_args[1]["metadata"]["importance"], 10)

    def test_long_content_returns_a_warning_without_rejecting(self):
        with mock.patch.dict(os.environ, {"MEMORY_MAX_CONTENT_CHARS": "5"}):
            hot, result = self._write({"content": "abcdefgh"})

        self.assertTrue(hot.add.called)
        self.assertTrue(
            any("keep one fact" in warning for warning in result["warnings"])
        )

    def test_deduped_consolidation_summary_keeps_its_marker(self):
        similar = [
            {
                "id": "old",
                "score": 0.99,
                "fact": "fact",
                "context": "",
                "episode_id": "",
            }
        ]
        hot, patches = _patched(similar)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            mock.patch.object(
                server_memory, "_update_metadata", return_value=True
            ) as update,
        ):
            server_memory._write_memory(
                {
                    "content": "fact",
                    "generated_by": "consolidation",
                    "tags": ["consolidated"],
                },
                "2026-01-01T00:00:00",
            )

        meta = update.call_args[0][1]
        self.assertEqual(meta["generated_by"], "consolidation")
        self.assertIn("consolidated", meta["tags"])

    def _dedupe_write(self, item):
        similar = [
            {
                "id": "old",
                "score": 0.99,
                "fact": "fact",
                "context": "",
                "episode_id": "",
            }
        ]
        hot, patches = _patched(similar)
        hot.get.return_value = {"metadata": {"importance": 9}}
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            mock.patch.object(
                server_memory, "_update_metadata", return_value=True
            ) as update,
        ):
            server_memory._write_memory(item, "2026-01-01T00:00:00")
        return update.call_args[0][1]

    def test_system_write_never_lowers_a_stored_importance(self):
        meta = self._dedupe_write(
            {"content": "fact", "generated_by": "consolidation", "importance": 5}
        )

        self.assertEqual(meta["importance"], 9)

    def test_explicit_rewrite_can_lower_importance(self):
        meta = self._dedupe_write({"content": "fact", "importance": 3})

        self.assertEqual(meta["importance"], 3)


class ProjectionTest(unittest.TestCase):
    def test_fact_replaces_the_embedded_text_and_context_is_separate(self):
        row = {
            "id": "m1",
            "memory": "fact\nContext: why",
            "metadata": {"fact": "fact", "context": "why"},
        }

        out = project_hot_result(row)

        self.assertEqual(out["content"], "fact")
        self.assertEqual(out["context"], "why")
        self.assertNotIn("Context:", out["content"])

    def test_legacy_rows_keep_the_stored_text(self):
        row = {"id": "m1", "memory": "[background] old style fact", "metadata": {}}

        out = project_hot_result(row)

        self.assertEqual(out["content"], "[background] old style fact")
        self.assertNotIn("context", out)

    def test_projection_metadata_drops_fact_and_context(self):
        row = {
            "id": "m1",
            "memory": "fact",
            "metadata": {"fact": "fact", "context": "why", "tags": ["t"]},
        }

        out = project_hot_result(row)

        self.assertEqual(out["metadata"]["tags"], ["t"])
        self.assertNotIn("fact", out["metadata"])


class SchemaContractTest(unittest.TestCase):
    def test_both_write_tools_expose_context(self):
        from agent_gateway.services.mcp.server_tool_schema import build_tools

        tools = {tool.name: tool for tool in build_tools()}
        remember = tools["remember"].input_schema["properties"]
        self.assertIn("context", remember)
        batch = tools["remember_batch"].input_schema["properties"]["memories"]["items"]
        self.assertIn("context", batch["properties"])

    def test_content_description_no_longer_asks_for_background(self):
        from agent_gateway.core.tool_text import param_text

        text = param_text("param.memory_content").lower()
        self.assertNotIn("[background]", text)
        self.assertIn("put those in context", text)


if __name__ == "__main__":
    unittest.main()
