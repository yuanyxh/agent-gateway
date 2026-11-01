"""Tests for the optional ranking signals added to score fusion."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.retrieval.fusion.score import SignalWeightedFusion  # noqa: E402
from agent_gateway.services.retrieval.types import ArmResult, RetrievedItem  # noqa: E402


def item(item_id, score, **extra):
    return RetrievedItem(
        id=item_id, kind="memory", score=score, score_kind="dense", extra=extra
    )


class SignalWeightedFusionTest(unittest.TestCase):
    def test_zero_weights_keep_plain_score_order(self):
        fusion = SignalWeightedFusion()
        arms = [
            ArmResult(
                name="hot",
                items=[
                    item("low", 0.5, access_count=9, relevance=1.0),
                    item("high", 0.9),
                ],
            )
        ]

        fused = fusion.fuse(arms, 2)

        self.assertEqual([row.id for row in fused], ["high", "low"])

    def test_access_signal_promotes_a_frequently_used_memory(self):
        fusion = SignalWeightedFusion(access=1.0)
        arms = [
            ArmResult(
                name="hot",
                items=[
                    item("frequent", 0.6, access_count=10),
                    item("fresh", 0.6),
                ],
            )
        ]

        fused = fusion.fuse(arms, 2)

        self.assertEqual(fused[0].id, "frequent")

    def test_recency_signal_promotes_a_recently_used_memory(self):
        fusion = SignalWeightedFusion(recency=1.0)
        now = datetime.now()
        arms = [
            ArmResult(
                name="hot",
                items=[
                    item(
                        "recent",
                        0.6,
                        last_accessed_at=(now - timedelta(days=1)).isoformat(),
                    ),
                    item(
                        "stale",
                        0.6,
                        last_accessed_at=(now - timedelta(days=300)).isoformat(),
                    ),
                ],
            )
        ]

        fused = fusion.fuse(arms, 2)

        self.assertEqual(fused[0].id, "recent")

    def test_quality_signal_promotes_a_scored_memory(self):
        fusion = SignalWeightedFusion(quality=1.0)
        arms = [
            ArmResult(
                name="hot",
                items=[
                    item("quality", 0.6, relevance=0.9),
                    item("plain", 0.6),
                ],
            )
        ]

        fused = fusion.fuse(arms, 2)

        self.assertEqual(fused[0].id, "quality")


if __name__ == "__main__":
    unittest.main()
