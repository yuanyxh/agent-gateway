"""Response whitelists: adding or dropping a key must change this file too."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402
from agent_gateway.services.retrieval.types import RetrievedItem  # noqa: E402

RECALL_TOP_KEYS = {
    "status",
    "results",
    "related",
    "count",
    "filtered",
    "fallback_count",
    "errors",
    "trace_id",
}
RECALL_ITEM_KEYS = {
    "id",
    "kind",
    "score",
    "content",
    "layer",
    "locator",
    "episode_id",
    "context",
    "edge",
    "metadata",
    "filter_fallback",
}
RECALL_META_KEYS = {"tags", "status", "confidence"}


def _item() -> RetrievedItem:
    return RetrievedItem(
        id="m1",
        kind="memory",
        score=0.9,
        score_kind="dense",
        content="fact",
        layer="hot",
        match_kind="expanded",
        locator={"file": "a.md"},
        extra={
            "episode_id": "ep1",
            "context": "why",
            "edge": "supersedes",
            "heat_score": 1.0,
            "created_at": "2026-01-01",
            "updated_at": "2026-01-02",
            "agent_id": "a",
            "attributed_to": "b",
            "metadata": {
                "tags": ["t"],
                "status": "active",
                "confidence": 1.0,
                "access_count": 5,
                "superseded_by": "",
                "revision": 2,
            },
        },
    )


class RecallResponseTest(unittest.TestCase):
    def test_item_keeps_only_decision_fields(self):
        out = _item().to_result()

        self.assertLessEqual(set(out), RECALL_ITEM_KEYS)
        self.assertEqual(out["content"], "fact")
        self.assertEqual(out["context"], "why")
        self.assertEqual(out["edge"], "supersedes")
        self.assertNotIn("score_kind", out)
        self.assertNotIn("match_kind", out)
        self.assertNotIn("heat_score", out)

    def test_metadata_keeps_only_tags_status_confidence(self):
        out = _item().to_result()

        self.assertEqual(set(out["metadata"]), RECALL_META_KEYS)

    def test_handler_returns_only_top_level_decision_keys(self):
        pipeline = mock.Mock()
        pipeline.run_query.return_value = {
            "status": "ok",
            "results": [_item().to_result()],
            "related": [],
            "count": 1,
            "related_count": 0,
            "trace_id": "t1",
        }
        with (
            mock.patch.object(
                server_memory, "_build_recall_pipeline", return_value=pipeline
            ),
            mock.patch.object(
                server_memory, "ensure_hot_memory", mock.AsyncMock(return_value=True)
            ),
        ):
            result = asyncio.run(
                server_memory._handle_recall({"keywords": ["q"], "max_chars": 0})
            )

        data = json.loads(result[0].text)
        self.assertEqual(set(data), RECALL_TOP_KEYS)
        self.assertLessEqual(set(data["results"][0]), RECALL_ITEM_KEYS)

    def test_hint_is_added_only_when_the_status_needs_it(self):
        pipeline = mock.Mock()
        pipeline.run_query.return_value = {
            "status": "no_match",
            "results": [],
            "related": [],
            "count": 0,
            "related_count": 0,
            "trace_id": "t1",
        }
        with (
            mock.patch.object(
                server_memory, "_build_recall_pipeline", return_value=pipeline
            ),
            mock.patch.object(
                server_memory, "ensure_hot_memory", mock.AsyncMock(return_value=True)
            ),
        ):
            result = asyncio.run(
                server_memory._handle_recall({"keywords": ["q"], "max_chars": 0})
            )

        data = json.loads(result[0].text)
        self.assertEqual(set(data), RECALL_TOP_KEYS | {"hint"})


class LookupItemShapeTest(unittest.TestCase):
    def test_lookup_decision_fields_survive_shaping(self):
        out = RetrievedItem(
            id="project_lookup:a.py:function:scan:10",
            kind="function",
            score=1.0,
            score_kind="rank",
            content="scan()",
            layer="project_lookup",
            locator={"file": "a.py", "line": 10, "end_line": 20},
            extra={
                "name": "scan",
                "caller_count": 3,
                "top_callers": [{"file": "b.py"}],
                "resolver": "treesitter",
            },
        ).to_result()

        self.assertEqual(out["name"], "scan")
        self.assertEqual(out["caller_count"], 3)
        self.assertEqual(out["top_callers"], [{"file": "b.py"}])
        self.assertEqual(out["resolver"], "treesitter")
        self.assertEqual(out["locator"]["end_line"], 20)
        self.assertNotIn("score_kind", out)


if __name__ == "__main__":
    unittest.main()
