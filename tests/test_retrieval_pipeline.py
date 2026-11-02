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
from agent_gateway.services.retrieval.fusion.score import (  # noqa: E402
    HeatWeightedFusion,
    RRFFusion,
)
from agent_gateway.services.retrieval.gate.relevance import (  # noqa: E402
    LayerPresenceGate,
    PassThroughGate,
)
from agent_gateway.services.retrieval.gate import calibrate  # noqa: E402
from agent_gateway.services.retrieval.gate.thresholds import floors_for  # noqa: E402
from agent_gateway.services.retrieval.pipeline import Pipeline, Stage  # noqa: E402
from agent_gateway.services.retrieval.rerank import order_by_score  # noqa: E402
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


class StageTest(unittest.TestCase):
    def _pipeline(self, stages):
        return Pipeline(
            name="test",
            arms=[],
            fusion=MaxScoreFusion(),
            gate=LayerPresenceGate(),
            stages=stages,
        )

    def test_tier_below_runs_while_the_limit_is_short(self):
        seen: list[str] = []
        first = FakeArm("a", [item("x", 0.7)], seen)
        second = FakeArm("b", [item("y", 0.9)], seen)
        pipeline = self._pipeline([Stage("s1", [first]), Stage("s2", [second])])

        out = pipeline.run("q", limit=5)

        self.assertEqual([row["id"] for row in out["results"]], ["y", "x"])
        self.assertEqual(seen, ["q", "q"])

    def test_filled_tier_stops_the_walk(self):
        seen: list[str] = []
        first = FakeArm("a", [item("x", 0.7), item("z", 0.6)], seen)
        second = FakeArm("b", [item("y", 0.9)], seen)
        pipeline = self._pipeline([Stage("s1", [first]), Stage("s2", [second])])

        out = pipeline.run("q", limit=2)

        self.assertEqual([row["id"] for row in out["results"]], ["x", "z"])
        self.assertEqual(seen, ["q"])

    def test_empty_stage_falls_through(self):
        seen: list[str] = []
        first = FakeArm("a", [], seen)
        second = FakeArm("b", [item("y", 0.9)], seen)
        pipeline = self._pipeline([Stage("s1", [first]), Stage("s2", [second])])

        out = pipeline.run("q", limit=5)

        self.assertEqual([row["id"] for row in out["results"]], ["y"])
        self.assertEqual(seen, ["q", "q"])

    def test_last_resort_tier_is_skipped_when_anything_was_found(self):
        seen: list[str] = []
        first = FakeArm("a", [item("x", 0.7)], seen)
        second = FakeArm("b", [item("y", 0.9)], seen)
        pipeline = self._pipeline(
            [
                Stage("s1", [first]),
                Stage("s2", [second], only_if_empty=True),
            ]
        )

        out = pipeline.run("q", limit=5)

        self.assertEqual([row["id"] for row in out["results"]], ["x"])
        self.assertEqual(seen, ["q"])


class AutoCalibrateTest(unittest.TestCase):
    def test_self_score_requires_the_record_in_its_own_results(self):
        def search(probe):
            return {"甲记忆开头": [("a", 0.8), ("b", 0.5)], "乙记忆开头": [("c", 0.6)]}[
                probe
            ]

        scores = calibrate.self_scores(
            [("a", "甲记忆开头"), ("b", "乙记忆开头")], search
        )

        self.assertEqual(scores, [0.8])

    def test_floor_is_the_median_self_score(self):
        scores = [0.5 + i * 0.01 for i in range(40)]

        self.assertEqual(calibrate.floor_from_scores(scores), 0.7)

    def test_too_few_probes_yields_no_floor(self):
        self.assertIsNone(calibrate.floor_from_scores([0.7, 0.8]))

    def test_percentile_picks_expected_index(self):
        self.assertEqual(calibrate.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)


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

    def fuse(self, arms, limit):
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

    def test_rerank_order_drops_non_positive_scores(self):
        rows = [item("a", 0.9), item("b", 0.8), item("c", 0.7)]

        out = order_by_score([1.0, -0.5, 2.0], rows)

        self.assertEqual([r.id for r in out], ["c", "a"])
        self.assertEqual(out[0].score_kind, "rerank")

    def test_rrf_rewards_agreement_between_lists(self):
        first = ArmResult(name="a", items=[item("x", 0.9), item("y", 0.8)])
        second = ArmResult(name="b", items=[item("y", 0.4), item("z", 0.3)])

        fused = RRFFusion().fuse([first, second], 5)

        self.assertEqual([f.id for f in fused], ["y", "x", "z"])
        self.assertEqual(fused[0].score_kind, "rrf")


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

    def test_unknown_model_gets_provisional_floors(self):
        from agent_gateway.services.retrieval.gate import thresholds

        floors = floors_for("no_such_model")
        self.assertEqual(floors, thresholds.PROVISIONAL_FLOORS)
        self.assertFalse(thresholds.is_calibrated("no_such_model"))

    def test_override_file_supplies_floors(self):
        from agent_gateway.services.retrieval.gate import thresholds

        measured = {
            "hot": 0.61,
            "cold": 0.57,
            "expanded": 0.57,
            "notes_coverage": 0.50,
            "notes_semantic": 0.37,
        }
        with mock.patch.object(thresholds, "_override", return_value=measured):
            self.assertTrue(thresholds.is_calibrated("any_model"))
            self.assertEqual(floors_for("any_model"), measured)

    def test_incomplete_override_falls_back_to_provisional(self):
        from agent_gateway.services.retrieval.gate import thresholds

        self.assertIsNone(thresholds._override("missing_tag"))


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
        with mock.patch.object(trace, "record") as recorder:
            out = pipeline.run("q", limit=5)

        self.assertEqual(out["status"], "ok")
        self.assertIn("nope", recorder.call_args[0][0]["passes"][0]["arms"][0]["error"])
        self.assertEqual(out["results"][0]["id"], "x")

    def test_failing_arm_is_reported_in_response(self):
        class Boom:
            name = "boom"

            def retrieve(self, query, limit, filters=None):
                raise RuntimeError("nope")

        pipeline = self._pipeline([Boom(), FakeArm("ok", [item("x", 0.7)])])
        with mock.patch.object(trace, "record"):
            out = pipeline.run("q", limit=5)

        self.assertEqual(
            out["errors"],
            [{"pass": "primary", "arm": "boom", "error": "RuntimeError: nope"}],
        )

    def test_successful_arms_report_no_errors(self):
        pipeline = self._pipeline([FakeArm("a", [item("x", 0.7)])])
        with mock.patch.object(trace, "record"):
            out = pipeline.run("q", limit=5)

        self.assertEqual(out["errors"], [])

    def test_arm_limit_covers_the_caller_limit(self):
        seen_limits: list[int] = []

        class LimitArm:
            name = "limit"

            def retrieve(self, query, limit, filters=None):
                seen_limits.append(limit)
                return ArmResult(name=self.name, items=[])

        pipeline = self._pipeline([LimitArm()])
        with mock.patch.object(trace, "record"):
            pipeline.run("q", limit=50)

        self.assertEqual(seen_limits, [50])

    def test_pass_through_gate_reports_presence_only(self):
        gate = PassThroughGate()

        self.assertEqual(gate.judge(Query(text="q"), [item("x", 0.01)]).status, "ok")
        self.assertEqual(gate.judge(Query(text="q"), []).status, "no_match")

    def test_trace_payload_is_recorded(self):
        pipeline = self._pipeline([FakeArm("a", [item("x", 0.7)])])
        with mock.patch.object(trace, "record") as recorder:
            out = pipeline.run("q", limit=5, tool="recall")

        payload = recorder.call_args[0][0]
        self.assertEqual(payload["tool"], "recall")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["passes"][0]["arms"][0]["hits"][0]["id"], "x")
        self.assertEqual(payload["returned"][0]["id"], "x")
        self.assertEqual(payload["returned"][0]["score_kind"], "dense")
        self.assertEqual(payload["lead"]["id"], "x")
        self.assertTrue(out["trace_id"].startswith("rt_"))

    def test_trace_returned_lists_only_delivered_items(self):
        """Adoption analysis must not credit candidates that were cut by the limit."""
        pipeline = self._pipeline(
            [
                FakeArm(
                    "a",
                    [item("first", 0.9), item("second", 0.8), item("dropped", 0.7)],
                )
            ]
        )
        with mock.patch.object(trace, "record") as recorder:
            pipeline.run("q", limit=2)

        returned = [row["id"] for row in recorder.call_args[0][0]["returned"]]
        self.assertEqual(returned, ["first", "second"])

    def test_semantic_query_runs_a_second_pass_and_rrf_merges_it(self):
        seen: list[str] = []
        arm = FakeArm("a", [item("x", 0.7)], seen)
        pipeline = self._pipeline([arm])

        with mock.patch.object(trace, "record") as recorder:
            out = pipeline.run_query(
                Query(text="kw1 kw2", language="mixed", semantic_text="a sentence"),
                limit=5,
            )

        self.assertEqual(seen, ["kw1 kw2", "a sentence"])
        self.assertEqual(recorder.call_args[0][0]["fusion"], "rrf")
        self.assertNotIn("score_kind", out["results"][0])

    def test_semantic_query_equal_to_keywords_runs_once(self):
        seen: list[str] = []
        pipeline = self._pipeline([FakeArm("a", [item("x", 0.7)], seen)])

        with mock.patch.object(trace, "record"):
            pipeline.run_query(
                Query(text="same", language="latin", semantic_text="same")
            )

        self.assertEqual(seen, ["same"])

    def _fallback_pipeline(self, arms: list[FakeArm]) -> Pipeline:
        return Pipeline(
            name="test",
            arms=arms,
            fusion=MaxScoreFusion(),
            gate=LayerPresenceGate(),
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

    def test_result_shape_omits_diagnostic_fields(self):
        pipeline = self._pipeline([FakeArm("a", [item("x", 0.7)])])
        with mock.patch.object(trace, "record"):
            out = pipeline.run("q", limit=5)

        row = out["results"][0]
        self.assertNotIn("score_kind", row)
        self.assertNotIn("match_kind", row)
        self.assertEqual(row["id"], "x")
        self.assertIn("content", row)


class ConfigTest(unittest.TestCase):
    def test_model_tag_is_stable_and_short(self):
        self.assertEqual(
            config.model_tag("sentence-transformers/all-MiniLM-L6-v2"), "minilm384"
        )
        self.assertEqual(
            config.model_tag("jinaai/jina-embeddings-v2-base-zh"), "jinazh768"
        )

    def test_arm_exposes_the_retriever_surface(self):
        self.assertTrue(callable(FakeArm("a", []).retrieve))


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
