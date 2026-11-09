"""recall must opt in to the cross-encoder reranker explicitly."""

from __future__ import annotations

# ruff: noqa: E402 - temp QDRANT_PATH must be set before server_memory imports

import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_QDRANT = tempfile.mkdtemp(prefix="qdrant-rerank-")
atexit.register(shutil.rmtree, _QDRANT, True)
os.environ.setdefault("QDRANT_PATH", _QDRANT)

from agent_gateway.services.mcp import server_memory
from agent_gateway.services.retrieval.rerank import CrossEncoderReranker
from agent_gateway.services.retrieval.types import Query, RetrievedItem

#: The app injects GATEWAY_CACHE=~/.cache/utility, so this is the live path.
_MODEL_DIR = os.path.expanduser("~/.cache/utility/models/bge-reranker-base-int8")


class RecallRerankerWiringTest(unittest.TestCase):
    def test_pipeline_passes_reranker_by_default(self):
        marker = object()
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(server_memory, "get_reranker", return_value=marker),
        ):
            os.environ.pop("RECALL_RERANK", None)
            pipeline = server_memory._build_recall_pipeline()
        self.assertIs(pipeline.reranker, marker)

    def test_pipeline_skips_reranker_when_disabled(self):
        with mock.patch.dict(os.environ, {"RECALL_RERANK": "0"}):
            pipeline = server_memory._build_recall_pipeline()
        self.assertIsNone(pipeline.reranker)


@unittest.skipUnless(os.path.isdir(_MODEL_DIR), "reranker model not installed")
class CrossEncoderOrderTest(unittest.TestCase):
    def test_orders_by_cross_encoder_score(self):
        reranker = CrossEncoderReranker(_MODEL_DIR)
        items = [
            RetrievedItem(
                id="venus",
                kind="memory",
                score=0.9,
                score_kind="fused",
                content="Venus is Earth's twin.",
            ),
            RetrievedItem(
                id="mars",
                kind="memory",
                score=0.1,
                score_kind="fused",
                content="Mars is known as the Red Planet.",
            ),
        ]
        ranked = reranker(Query(text="Which planet is the Red Planet?"), items)
        self.assertEqual(ranked[0].id, "mars")
        self.assertTrue(all(item.score_kind == "rerank" for item in ranked))


if __name__ == "__main__":
    unittest.main()
