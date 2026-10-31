"""Tests for the generic retrieval layer: protocols, fusion, gate, tracing."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
os.environ.setdefault("RETRIEVAL_TRACE", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.retrieval import config, trace  # noqa: E402
from agent_gateway.services.retrieval.fusion.score import HeatWeightedFusion  # noqa: E402
from agent_gateway.services.retrieval.gate.relevance import (  # noqa: E402
    LayerPresenceGate,
    RelativeRelevanceGate,
)
from agent_gateway.services.retrieval.gate.thresholds import (  # noqa: E402
    UncalibratedThresholds,
    floors_for,
)
from agent_gateway.services.retrieval.pipeline import Pipeline  # noqa: E402
from agent_gateway.services.retrieval.protocol import Retriever  # noqa: E402
from agent_gateway.services.retrieval.text.analyzer import (  # noqa: E402
    detect_language,
    split_terms,
)
from agent_gateway.services.retrieval.types import (  # noqa: E402
    ArmResult,
    Query,
    RetrievedItem,
)


def item(
    id_: str, score: float, *, layer: str = "hot", content: str = ""
) -> RetrievedItem:
    return RetrievedItem(
        id=id_,
        kind="memory",
        score=score,
        score_kind="dense",
        content=content,
        layer=layer,
    )


class FakeArm:
    fallback_only = False

    def __init__(
        self,
        name: str,
        items: list[RetrievedItem],
        seen: list[str] | None = None,
        fallback_only: bool = False,
    ):
        self.name = name
        self._items = items
        self.seen = seen if seen is not None else []
        self.fallback_only = fallback_only

    def retrieve(self, query: Query, limit: int, filters=None) -> ArmResult:
        self.seen.append(query.text)
        return ArmResult(name=self.name, items=list(self._items))


class AnalyzerTest(unittest.TestCase):
    def test_whitespace_boundaries_are_preserved(self):
        self.assertEqual(split_terms("RSS 治理 日报"), ["rss", "治理", "日报"])

    def test_connectors_stay_inside_latin_terms(self):
        self.assertEqual(split_terms("agent-vibe"), ["agent-vibe"])
        self.assertEqual(split_terms("notes:a/b.md"), ["notes:a/b.md"])
        self.assertEqual(split_terms("2026-10-28 RSS"), ["2026-10-28", "rss"])

    def test_language_detection(self):
        self.assertEqual(detect_language("RSS 日报"), "mixed")
        self.assertEqual(detect_language("日报"), "cjk")
        self.assertEqual(detect_language("rss"), "latin")
        self.assertEqual(detect_language("!!!"), "other")


class MaxScoreFusion:
    """Test double: merge arms by plain score, deduplicated by id."""

    name = "max_score"

    def fuse(self, arms, limit, weights=None):
        merged = {}
        for arm in arms:
            for row in arm.items:
                existing = merged.get(row.id)
                if existing is None or row.score > existing.score:
                    merged[row.id] = row
        return sorted(merged.values(), key=lambda i: i.score, reverse=True)[:limit]


class FusionTest(unittest.TestCase):
    def test_max_score_keeps_highest_per_id(self):
        arms = [
            ArmResult(name="a", items=[item("x", 0.2)]),
            ArmResult(name="b", items=[item("x", 0.7)]),
        ]
        fused = MaxScoreFusion().fuse(arms, limit=5)

        self.assertEqual([(f.id, f.score) for f in fused], [("x", 0.7)])

    def test_heat_weighted_prefers_hotter_item_on_tie(self):
        cold = item("cold", 0.8)
        hot = item("hot", 0.8)
        hot.extra["heat_score"] = 1.0
        cold.extra["heat_score"] = 0.0

        fused = HeatWeightedFusion().fuse(
            [ArmResult(name="c", items=[cold]), ArmResult(name="h", items=[hot])], 5
        )

        self.assertEqual([f.id for f in fused], ["hot", "cold"])


class GateTest(unittest.TestCase):
    def test_layer_presence_matches_legacy_semantics(self):
        gate = LayerPresenceGate()
        self.assertEqual(gate.judge(Query(text="q"), []).status, "no_match")
        self.assertEqual(
            gate.judge(Query(text="q"), [item("n", 0.55, layer="notes")]).status,
            "weak_match",
        )
        self.assertEqual(gate.judge(Query(text="q"), [item("h", 0.7)]).status, "ok")

    def test_layer_presence_accepts_a_profile_specific_layer(self):
        gate = LayerPresenceGate(semantic_layers=("project_notes",))
        verdict = gate.judge(Query(text="q"), [item("n", 0.55, layer="project_notes")])

        self.assertEqual(verdict.status, "ok")
        self.assertEqual(verdict.reason, "semantic_layer_hit")

    def test_relative_gate_requires_margin_or_anchor(self):
        gate = RelativeRelevanceGate(floor=0.5, min_margin=0.1)
        flat = [item("a", 0.62), item("b", 0.61), item("c", 0.60)]

        verdict = gate.judge(Query(text="abc"), flat)
        self.assertEqual(verdict.status, "weak_match")
        self.assertEqual(verdict.reason, "insufficient_margin")

    def test_relative_gate_passes_on_lexical_anchor(self):
        gate = RelativeRelevanceGate(floor=0.9, min_margin=0.5)
        items = [item("a", 0.3, content="episode expansion notes")]

        verdict = gate.judge(Query(text="episode"), items)
        self.assertEqual(verdict.status, "ok")
        self.assertTrue(verdict.lexical_anchor)

    def test_relative_gate_passes_on_floor_and_margin(self):
        gate = RelativeRelevanceGate(floor=0.5, min_margin=0.1)
        items = [item("a", 0.81), item("b", 0.55), item("c", 0.50)]

        verdict = gate.judge(Query(text="zzz"), items)
        self.assertEqual(verdict.status, "ok")
        self.assertEqual(verdict.reason, "floor_and_margin")

    def test_unknown_model_fails_loudly(self):
        with self.assertRaises(KeyError):
            floors_for("no_such_model")
        self.assertEqual(floors_for("minilm384")["hot"], 0.68)

    def test_calibrated_model_ships_measured_values(self):
        from agent_gateway.services.retrieval.gate import thresholds

        # Read the table, not floors_for: an override file in the user's data dir
        # would otherwise decide what this assertion sees.
        floors = thresholds.FLOORS["jinazh768"]
        self.assertAlmostEqual(floors["hot"], 0.62)
        self.assertAlmostEqual(floors["cold"], 0.55)
        self.assertNotIn("jinazh768", thresholds.SEEDED_TAGS)

    def test_missing_floor_raises(self):
        from agent_gateway.services.retrieval.gate import thresholds

        original = thresholds.FLOORS["jinazh768"]
        try:
            thresholds.FLOORS["jinazh768"] = {**original, "hot": None}
            with self.assertRaises(UncalibratedThresholds):
                floors_for("jinazh768")
        finally:
            thresholds.FLOORS["jinazh768"] = original


class PipelineTest(unittest.TestCase):
    def _pipeline(self, arms: list[FakeArm], gate=None) -> Pipeline:
        return Pipeline(
            name="test",
            arms=arms,
            fusion=MaxScoreFusion(),
            gate=gate or LayerPresenceGate(),
        )

    def test_each_arm_runs_once_per_query(self):
        seen: list[str] = []
        arm = FakeArm("a", [item("x", 0.7)], seen)
        pipeline = self._pipeline([arm], LayerPresenceGate())

        out = pipeline.run("alpha beta", limit=5)

        self.assertEqual(seen, ["alpha beta"])
        self.assertEqual(out["count"], 1)

    def test_failing_arm_is_isolated(self):
        class Boom:
            name = "boom"

            def retrieve(self, query, limit, filters=None):
                raise RuntimeError("nope")

        pipeline = self._pipeline([Boom(), FakeArm("ok", [item("x", 0.7)])])
        out = pipeline.run("q", limit=5)

        self.assertEqual(out["status"], "ok")
        self.assertIn("nope", out["_arms"][0].error)
        self.assertEqual(out["results"][0]["id"], "x")

    def test_trace_payload_is_recorded(self):
        pipeline = self._pipeline([FakeArm("a", [item("x", 0.7)])])
        with mock.patch.object(trace, "record") as recorder:
            out = pipeline.run("q", limit=5, tool="recall")

        payload = recorder.call_args[0][0]
        self.assertEqual(payload["tool"], "recall")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["arms"][0]["hits"][0]["id"], "x")
        self.assertTrue(out["trace_id"].startswith("rt_"))

    def _fallback_pipeline(self, arms: list[FakeArm], enabled: bool = True) -> Pipeline:
        return Pipeline(
            name="test",
            arms=arms,
            fusion=MaxScoreFusion(),
            gate=LayerPresenceGate(),
            lexical_only_as_fallback=enabled,
        )

    def test_lexical_results_dropped_when_semantic_layer_hits(self):
        arms = [
            FakeArm("dense_hot", [item("h", 0.7, layer="hot")]),
            FakeArm(
                "lexical_notes", [item("n", 0.6, layer="notes")], fallback_only=True
            ),
        ]
        with mock.patch.object(trace, "record"):
            out = self._fallback_pipeline(arms).run("q", limit=5)

        self.assertEqual([row["id"] for row in out["results"]], ["h"])

    def test_lexical_results_survive_without_a_semantic_hit(self):
        arms = [FakeArm("lexical_notes", [item("n", 0.6, layer="notes")])]
        with mock.patch.object(trace, "record"):
            out = self._fallback_pipeline(arms).run("q", limit=5)

        self.assertEqual([row["id"] for row in out["results"]], ["n"])

    def test_switch_off_keeps_lexical_results(self):
        arms = [
            FakeArm("dense_hot", [item("h", 0.7, layer="hot")]),
            FakeArm(
                "lexical_notes", [item("n", 0.6, layer="notes")], fallback_only=True
            ),
        ]
        with mock.patch.object(trace, "record"):
            out = self._fallback_pipeline(arms, enabled=False).run("q", limit=5)

        self.assertEqual([row["id"] for row in out["results"]], ["h", "n"])

    def test_result_shape_carries_score_kind(self):
        pipeline = self._pipeline([FakeArm("a", [item("x", 0.7)])])
        with mock.patch.object(trace, "record"):
            out = pipeline.run("q", limit=5)

        self.assertEqual(out["results"][0]["score_kind"], "dense")
        self.assertEqual(out["results"][0]["match_kind"], "direct")


class ConfigTest(unittest.TestCase):
    def test_model_tag_is_stable_and_short(self):
        self.assertEqual(
            config.model_tag("sentence-transformers/all-MiniLM-L6-v2"), "minilm384"
        )
        self.assertEqual(
            config.model_tag("jinaai/jina-embeddings-v2-base-zh"), "jinazh768"
        )

    def test_gate_defaults_to_layer_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RETRIEVAL_GATE", None)
            os.environ.pop("RETRIEVAL_GATE", None)
            self.assertEqual(config.gate_mode(), "layer")


class ProtocolTest(unittest.TestCase):
    def test_arm_satisfies_retriever_protocol(self):
        self.assertIsInstance(FakeArm("a", []), Retriever)


class TraceTest(unittest.TestCase):
    def test_record_and_stats(self):
        trace.record({"tool": "recall", "status": "ok", "latency_ms": {"total": 5}})
        trace.record(
            {"tool": "recall", "status": "no_match", "latency_ms": {"total": 15}}
        )

        stats = trace.stats()
        self.assertGreaterEqual(stats["samples"], 2)
        self.assertIn("ok", stats["by_status"])
        self.assertIsNotNone(stats["latency_p95"])

    def test_recent_is_bounded(self):
        for index in range(5):
            trace.record({"tool": "t", "status": "ok", "index": index})
        self.assertLessEqual(len(trace.recent(3)), 3)


if __name__ == "__main__":
    unittest.main()
