"""Tests for sequence replay: sampling, budgets and reinforcement."""

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

from agent_gateway.services.memory import graph_index, replay, store  # noqa: E402


class _ReplayCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        self.now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            for name in ("a", "b", "c"):
                connection.execute(
                    "INSERT OR IGNORE INTO neurons (neuron_id, title, member_count, "
                    "tier, status, importance, created_at, updated_at) "
                    "VALUES (?, ?, 1, 'active', 'active', 5, ?, ?)",
                    (name, f"title {name}", self.now, self.now),
                )
            for left, right in (("a", "b"), ("b", "c")):
                connection.execute(
                    "INSERT OR REPLACE INTO synapses (from_neuron, to_neuron, "
                    "weight, base_weight, relation, created_at, updated_at) "
                    "VALUES (?, ?, 0.8, 0.8, 'same_task', ?, ?)",
                    (left, right, self.now, self.now),
                )

    def tearDown(self):
        self._tmp.cleanup()


class SamplingTests(_ReplayCase):
    def test_seeds_only_include_active_neurons(self):
        seeds = replay.sample_seeds(limit=5, db_path=self.db)
        self.assertEqual({seed["neuron_id"] for seed in seeds}, {"a", "b", "c"})

    def test_walk_produces_paths(self):
        index = graph_index.GraphIndex.load(self.db)
        sequences = replay.walk_sequences(index, "a")
        self.assertTrue(sequences)
        self.assertTrue(all(path[0] == "a" for path in sequences))

    def test_parse_rejects_bad_json(self):
        self.assertTrue(replay.parse_replay("nope")["parse_failed"])

    def test_parse_keeps_two_step_paths(self):
        raw = json.dumps({"reinforce": [["a", "b"], ["x"]]})
        self.assertEqual(replay.parse_replay(raw)["reinforce"], [["a", "b"]])


class RunTests(_ReplayCase):
    def test_no_llm_short_circuits(self):
        with mock.patch.object(replay.llm_client, "is_configured", return_value=False):
            result = asyncio.run(replay.run_replay(db_path=self.db))
        self.assertEqual(result["stopped"], "no_llm")
        self.assertEqual(result["sampled"], 0)

    def test_budget_stops_after_one_call(self):
        raw = json.dumps({"reinforce": [["a", "b"]]})
        with (
            mock.patch.object(replay.llm_client, "is_configured", return_value=True),
            mock.patch.object(
                replay.llm_client, "achat", new=mock.AsyncMock(return_value=raw)
            ),
        ):
            result = asyncio.run(
                replay.run_replay(db_path=self.db, budget_calls=1, budget_seconds=60)
            )
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["stopped"], "budget")

    def test_reinforces_existing_synapse(self):
        raw = json.dumps({"reinforce": [["a", "b"]]})
        with (
            mock.patch.object(replay.llm_client, "is_configured", return_value=True),
            mock.patch.object(
                replay.llm_client, "achat", new=mock.AsyncMock(return_value=raw)
            ),
        ):
            result = asyncio.run(
                replay.run_replay(db_path=self.db, budget_calls=1, budget_seconds=60)
            )
        self.assertEqual(result["reinforced"], 1)
        with store.connect(self.db) as connection:
            weight = connection.execute(
                "SELECT weight FROM synapses WHERE from_neuron='a' AND to_neuron='b'"
            ).fetchone()[0]
            runs = connection.execute("SELECT COUNT(*) FROM replay_runs").fetchone()[0]
        self.assertGreater(weight, 0.8)
        self.assertEqual(runs, 1)

    def test_inference_stays_low_weight_and_audited(self):
        raw = json.dumps({"reinforce": [["a", "b", "c"]]})
        with (
            mock.patch.object(replay.llm_client, "is_configured", return_value=True),
            mock.patch.object(
                replay.llm_client, "achat", new=mock.AsyncMock(return_value=raw)
            ),
        ):
            result = asyncio.run(
                replay.run_replay(db_path=self.db, budget_calls=1, budget_seconds=60)
            )
        self.assertEqual(result["inferred"], 1)
        with store.connect(self.db) as connection:
            row = connection.execute(
                "SELECT weight, inferred, relation, metadata FROM synapses "
                "WHERE from_neuron='a' AND to_neuron='c'"
            ).fetchone()
        self.assertAlmostEqual(row[0], replay.INFER_WEIGHT)
        self.assertEqual((row[1], row[2]), (1, "inferred"))
        self.assertIn(result["run_id"], row[3])

    def test_daily_budget_counts_earlier_runs(self):
        from datetime import datetime

        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO replay_runs (run_id, kind, started_at, sampled, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "rp_old",
                    "daily",
                    datetime.now().isoformat(),
                    10,
                    '{"calls": 40}',
                ),
            )
        with mock.patch.object(replay.llm_client, "is_configured", return_value=True):
            result = asyncio.run(replay.run_replay(db_path=self.db, budget_calls=40))
        self.assertEqual(result["stopped"], "daily_budget")
        self.assertEqual(result["spent"], 40)

    def test_propose_rejects_short_paths(self):
        self.assertIsNone(replay.propose_inference(["a", "b"]))
        self.assertIsNone(replay.propose_inference(["a", "b", "a"]))
        proposal = replay.propose_inference(["c", "b", "a"])
        self.assertEqual((proposal["left"], proposal["right"]), ("a", "c"))


if __name__ == "__main__":
    unittest.main()
