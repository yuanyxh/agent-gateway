"""Tests for the memory network storage layer."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from agent_gateway.core import config  # noqa: E402
from agent_gateway.services.memory import (  # noqa: E402
    group_governance,
    lifecycle,
    network,
    network_build,
    store,
    strength,
)

NEURON_ENV = {
    "MEMORY_NEURON_ENABLED": "",
    "MEMORY_NEURON_BUILD": "",
    "MEMORY_NEURON_MAINTENANCE": "",
    "MEMORY_NEURON_MAX_DEPTH": "",
    "MEMORY_NEURON_BEAM": "",
    "MEMORY_NEURON_ACTIVATION_FLOOR": "",
    "MEMORY_NEURON_ARCHIVE_DAYS": "",
    "MEMORY_NEURON_TRASH_DAYS": "",
    "MEMORY_NEURON_LLM_PAIRS": "",
}


class _NetworkCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def node(self, node_id, **metadata):
        store.upsert_node(
            node_id,
            layer="hot",
            status="active",
            title=node_id,
            metadata=metadata,
            db_path=self.db,
        )


class SchemaTest(_NetworkCase):
    def test_network_tables_and_indexes_exist(self):
        with store.connect(self.db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        expected = {
            "neurons",
            "neuron_members",
            "synapses",
            "memory_activation",
            "network_meta",
        }
        self.assertTrue(expected.issubset(tables))
        self.assertIn("idx_neurons_tier", indexes)
        self.assertIn("idx_members_memory", indexes)
        self.assertIn("idx_syn_from", indexes)
        self.assertIn("idx_activation_purge", indexes)

    def test_legacy_database_keeps_data(self):
        legacy = os.path.join(self._tmp.name, "legacy.sqlite")
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE memory_nodes (node_id TEXT PRIMARY KEY, "
            "layer TEXT NOT NULL, type TEXT, status TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, title TEXT, source_file TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO memory_nodes VALUES "
            "('old', 'hot', '', 'active', 'h', 'old', '', '2026-01-01', "
            "'2026-01-01', '{}')"
        )
        connection.commit()
        connection.close()
        self.assertIsNotNone(store.get_node("old", db_path=legacy))
        self.assertTrue(network.get_neuron("missing", db_path=legacy) is None)


class NeuronTest(_NetworkCase):
    def test_upsert_get_and_list(self):
        self.assertTrue(
            network.upsert_neuron(
                {
                    "neuron_id": "ep_1",
                    "episode_id": "ep_1",
                    "title": "first",
                    "centroid": [0.1, 0.2, 0.3],
                    "member_count": 2,
                    "tier": "warm",
                    "metadata": {"project": "agent-gateway"},
                },
                db_path=self.db,
            )
        )
        neuron = network.get_neuron("ep_1", db_path=self.db)
        self.assertEqual(neuron["title"], "first")
        self.assertEqual(neuron["member_count"], 2)
        self.assertEqual(neuron["metadata"]["project"], "agent-gateway")
        self.assertAlmostEqual(neuron["centroid"][2], 0.3, places=5)
        self.assertEqual(
            [
                item["neuron_id"]
                for item in network.list_neurons(tier="warm", db_path=self.db)
            ],
            ["ep_1"],
        )
        self.assertEqual(network.list_neurons(tier="active", db_path=self.db), [])

    def test_upsert_preserves_created_at_and_updates_rest(self):
        network.upsert_neuron(
            {"neuron_id": "ep_1", "title": "a", "created_at": "2026-01-01"},
            db_path=self.db,
        )
        network.upsert_neuron(
            {"neuron_id": "ep_1", "title": "b", "tier": "warm"},
            db_path=self.db,
        )
        neuron = network.get_neuron("ep_1", db_path=self.db)
        self.assertEqual(neuron["title"], "b")
        self.assertEqual(neuron["tier"], "warm")
        self.assertEqual(neuron["created_at"], "2026-01-01")


class MemberTest(_NetworkCase):
    def test_membership_round_trip(self):
        self.node("m1", fact="one")
        self.node("m2", fact="two")
        network.upsert_neuron({"neuron_id": "ep_1"}, db_path=self.db)
        network.upsert_member("ep_1", "m1", weight=0.9, role="core", db_path=self.db)
        network.upsert_member("ep_1", "m2", weight=0.5, db_path=self.db)
        members = network.members_of("ep_1", db_path=self.db)
        self.assertEqual([item["node_id"] for item in members], ["m1", "m2"])
        self.assertEqual(members[0]["member_role"], "core")
        self.assertEqual(members[0]["metadata"]["fact"], "one")
        self.assertEqual(network.neuron_of("m2", db_path=self.db), "ep_1")
        self.assertEqual(network.refresh_neuron_counts("ep_1", db_path=self.db), 1)
        self.assertEqual(network.get_neuron("ep_1", db_path=self.db)["member_count"], 2)

    def test_remove_member_and_unknown_neuron(self):
        self.node("m1")
        network.upsert_member("ep_1", "m1", db_path=self.db)
        self.assertEqual(network.remove_member("m1", db_path=self.db), 1)
        self.assertEqual(network.members_of("ep_1", db_path=self.db), [])
        self.assertEqual(network.members_of("", db_path=self.db), [])
        self.assertEqual(network.neuron_of("", db_path=self.db), "")


class SynapseTest(_NetworkCase):
    def test_symmetric_upsert_keeps_max_weight(self):
        network.upsert_synapse(
            {"from_neuron": "b", "to_neuron": "a", "weight": 0.4},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "a", "to_neuron": "b", "weight": 0.8},
            db_path=self.db,
        )
        links = network.synapses_of("a", db_path=self.db)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["other"], "b")
        self.assertAlmostEqual(links[0]["weight"], 0.8)
        self.assertEqual(links[0]["base_weight"], 0.8)

    def test_invalid_synapse_ignored(self):
        self.assertFalse(network.upsert_synapse({"from_neuron": "a", "to_neuron": "a"}))
        self.assertFalse(network.upsert_synapse({"from_neuron": "a"}))

    def test_min_weight_filter(self):
        network.upsert_synapse(
            {"from_neuron": "a", "to_neuron": "b", "weight": 0.3},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "a", "to_neuron": "c", "weight": 0.7},
            db_path=self.db,
        )
        links = network.synapses_of("a", min_weight=0.5, db_path=self.db)
        self.assertEqual([item["other"] for item in links], ["c"])


class ActivationTest(_NetworkCase):
    def test_activation_upsert_and_tier_counts(self):
        self.assertIsNone(network.get_activation("m1", db_path=self.db))
        network.upsert_activation(
            "m1",
            {"strength": 0.6, "tier": "warm", "activation_count": 2},
            db_path=self.db,
        )
        activation = network.get_activation("m1", db_path=self.db)
        self.assertAlmostEqual(activation["strength"], 0.6)
        self.assertEqual(activation["tier"], "warm")
        self.assertEqual(activation["activation_count"], 2)
        network.upsert_activation(
            "m1", {"tier": "archive", "purge_after": "2027-01-01"}, db_path=self.db
        )
        self.assertEqual(
            network.get_activation("m1", db_path=self.db)["tier"], "archive"
        )
        counts = network.tier_counts(db_path=self.db)
        self.assertEqual(counts["archive"], 1)
        self.assertEqual(counts["active"], 0)
        self.assertEqual(set(counts), set(network.TIERS))

    def test_patch_ignores_unknown_keys(self):
        network.upsert_activation(
            "m1", {"strength": 0.4, "unknown": 1}, db_path=self.db
        )
        self.assertAlmostEqual(
            network.get_activation("m1", db_path=self.db)["strength"], 0.4
        )


class CentroidAndMetaTest(_NetworkCase):
    def test_centroid_round_trip(self):
        raw = network.encode_centroid([1.0, -2.0, 0.5])
        self.assertAlmostEqual(network.decode_centroid(raw)[1], -2.0)
        self.assertEqual(network.decode_centroid(None), [])

    def test_meta_round_trip(self):
        self.assertEqual(network.get_meta("migrated_at", db_path=self.db), "")
        network.set_meta("migrated_at", "2026-11-10", db_path=self.db)
        self.assertEqual(network.get_meta("migrated_at", db_path=self.db), "2026-11-10")
        network.set_meta("migrated_at", "2026-11-11", db_path=self.db)
        self.assertEqual(network.get_meta("migrated_at", db_path=self.db), "2026-11-11")


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, NEURON_ENV, clear=False):
            self.assertTrue(config.memory_neuron_enabled())
            self.assertTrue(config.memory_neuron_build_enabled())
            self.assertTrue(config.memory_neuron_maintenance_enabled())
            self.assertEqual(config.memory_neuron_max_depth(), 3)
            self.assertEqual(config.memory_neuron_beam(), 5)
            self.assertAlmostEqual(config.memory_neuron_activation_floor(), 0.15)
            self.assertEqual(config.memory_neuron_archive_days(), 180)
            self.assertEqual(config.memory_neuron_trash_days(), 30)
            self.assertEqual(config.memory_neuron_llm_pairs(), 100)

    def test_overrides_and_clamps(self):
        overrides = {
            **NEURON_ENV,
            "MEMORY_NEURON_ENABLED": "false",
            "MEMORY_NEURON_MAX_DEPTH": "9",
            "MEMORY_NEURON_BEAM": "0",
            "MEMORY_NEURON_ACTIVATION_FLOOR": "0.4",
            "MEMORY_NEURON_ARCHIVE_DAYS": "7",
            "MEMORY_NEURON_TRASH_DAYS": "3",
            "MEMORY_NEURON_LLM_PAIRS": "0",
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            self.assertFalse(config.memory_neuron_enabled())
            self.assertEqual(config.memory_neuron_max_depth(), 5)
            self.assertEqual(config.memory_neuron_beam(), 1)
            self.assertAlmostEqual(config.memory_neuron_activation_floor(), 0.4)
            self.assertEqual(config.memory_neuron_archive_days(), 7)
            self.assertEqual(config.memory_neuron_trash_days(), 3)
            self.assertEqual(config.memory_neuron_llm_pairs(), 0)

    def test_zero_retention_uses_default(self):
        overrides = {
            **NEURON_ENV,
            "MEMORY_NEURON_ARCHIVE_DAYS": "0",
            "MEMORY_NEURON_TRASH_DAYS": "-5",
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            self.assertEqual(config.memory_neuron_archive_days(), 180)
            self.assertEqual(config.memory_neuron_trash_days(), 30)

    def test_zero_floor_is_explicit(self):
        overrides = {**NEURON_ENV, "MEMORY_NEURON_ACTIVATION_FLOOR": "0"}
        with mock.patch.dict(os.environ, overrides, clear=False):
            self.assertEqual(config.memory_neuron_activation_floor(), 0.0)


class MigrationTest(_NetworkCase):
    def setUp(self):
        super().setUp()
        self.node("m1", episode_id="ep_a", importance=8)
        self.node("m2", episode_id="ep_a")
        self.node("m3", episode_id="ep_b")
        self.node("m4")

    def test_dry_run_does_not_write(self):
        result = network_build.migrate_network(db_path=self.db)
        self.assertEqual(result["nodes"], 4)
        self.assertEqual(result["neurons"], 3)
        self.assertEqual(result["solo"], 1)
        self.assertFalse(result["applied"])
        self.assertEqual(network.list_neurons(db_path=self.db), [])
        self.assertEqual(network.get_meta("migrated_at", db_path=self.db), "")

    def test_apply_is_idempotent(self):
        first = network_build.migrate_network(db_path=self.db, apply=True)
        network.upsert_activation("m1", {"strength": 0.9}, db_path=self.db)
        network.upsert_neuron({"neuron_id": "ep_a", "tier": "warm"}, db_path=self.db)
        second = network_build.migrate_network(db_path=self.db, apply=True)
        self.assertEqual(first["neurons"], 3)
        self.assertEqual(second["neurons"], 3)
        self.assertEqual(len(network.list_neurons(db_path=self.db)), 3)
        self.assertEqual(len(network.members_of("ep_a", db_path=self.db)), 2)
        self.assertEqual(len(network.members_of("solo:m4", db_path=self.db)), 1)
        self.assertTrue(network.get_meta("migrated_at", db_path=self.db))
        self.assertAlmostEqual(
            network.get_activation("m1", db_path=self.db)["strength"], 0.9
        )
        self.assertEqual(network.get_neuron("ep_a", db_path=self.db)["tier"], "warm")

    def test_reconcile_removes_ghosts_and_empty_neurons(self):
        network_build.migrate_network(db_path=self.db, apply=True)
        with store.connect(self.db) as connection:
            connection.execute("DELETE FROM memory_nodes WHERE node_id='m1'")
        network.upsert_neuron({"neuron_id": "empty"}, db_path=self.db)
        network.upsert_synapse(
            {"from_neuron": "empty", "to_neuron": "ep_a", "weight": 0.7},
            db_path=self.db,
        )
        result = network_build.reconcile_network(db_path=self.db)
        self.assertEqual(result["ghost_members"], 1)
        self.assertEqual(result["empty_neurons"], 1)
        self.assertEqual(len(network.members_of("ep_a", db_path=self.db)), 1)
        self.assertIsNone(network.get_neuron("empty", db_path=self.db))


class StrengthTest(_NetworkCase):
    def test_decay_half_life_and_idempotent(self):
        now = datetime(2026, 11, 10, 12, 0, 0)
        network.upsert_activation("m1", {"strength": 1.0}, db_path=self.db)
        network.upsert_synapse(
            {"from_neuron": "a", "to_neuron": "b", "weight": 1.0},
            db_path=self.db,
        )
        network.set_meta(
            "last_decay_at", (now - timedelta(days=30)).isoformat(), db_path=self.db
        )
        first = strength.apply_decay(now=now, db_path=self.db)
        self.assertAlmostEqual(
            network.get_activation("m1", db_path=self.db)["strength"], 0.9, places=3
        )
        self.assertAlmostEqual(
            network.synapses_of("a", db_path=self.db)[0]["weight"], 0.7937, places=3
        )
        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO memory_activation (node_id, strength, tier) "
                "VALUES ('m2', 1.0, 'active')"
            )
        network.set_meta(
            "last_decay_at", (now - timedelta(days=30)).isoformat(), db_path=self.db
        )
        with mock.patch.object(config, "memory_brain_forgetting", return_value=False):
            strength.apply_decay(now=now, db_path=self.db)
        self.assertAlmostEqual(
            network.get_activation("m2", db_path=self.db)["strength"], 0.5, places=3
        )
        self.assertEqual(first["memory_rows"], 1)
        second = strength.apply_decay(now=now, db_path=self.db)
        self.assertEqual(second["days"], 0.0)
        self.assertAlmostEqual(
            network.get_activation("m1", db_path=self.db)["strength"], 0.45, places=3
        )

    def test_reinforce_memory_and_synapse(self):
        network.upsert_activation("m1", {"strength": 0.2}, db_path=self.db)
        self.assertAlmostEqual(
            strength.reinforce_memory("m1", db_path=self.db), 0.32, places=5
        )
        self.assertEqual(
            network.get_activation("m1", db_path=self.db)["activation_count"], 1
        )
        network.upsert_synapse(
            {"from_neuron": "a", "to_neuron": "b", "weight": 0.2},
            db_path=self.db,
        )
        synapse_id = network.synapses_of("a", db_path=self.db)[0]["synapse_id"]
        self.assertAlmostEqual(
            strength.reinforce_synapse(synapse_id, db_path=self.db), 0.28, places=5
        )

    def test_coactivate_creates_then_strengthens(self):
        self.assertTrue(strength.coactivate("a", "b", db_path=self.db))
        link = network.synapses_of("a", db_path=self.db)[0]
        self.assertEqual(link["relation"], "coactivation")
        before = link["weight"]
        self.assertTrue(strength.coactivate("b", "a", db_path=self.db))
        self.assertGreater(
            network.synapses_of("a", db_path=self.db)[0]["weight"], before
        )

    def test_unbind_weak_archives_old_low_weight(self):
        now = datetime(2026, 11, 10)
        network.upsert_synapse(
            {
                "from_neuron": "a",
                "to_neuron": "b",
                "weight": 0.1,
                "last_activated_at": (now - timedelta(days=90)).isoformat(),
            },
            db_path=self.db,
        )
        network.upsert_synapse(
            {
                "from_neuron": "a",
                "to_neuron": "c",
                "weight": 0.5,
                "last_activated_at": (now - timedelta(days=90)).isoformat(),
            },
            db_path=self.db,
        )
        self.assertEqual(strength.unbind_weak(now=now, db_path=self.db), 1)
        active = network.synapses_of("a", db_path=self.db)
        self.assertEqual([item["other"] for item in active], ["c"])

    def test_unbind_keeps_fresh_coactivation(self):
        now = datetime.now()
        self.assertTrue(strength.coactivate("a", "b", now=now, db_path=self.db))
        self.assertEqual(strength.unbind_weak(now=now, db_path=self.db), 0)
        later = now + timedelta(days=61)
        self.assertEqual(
            strength.unbind_weak(now=later, db_path=self.db, dry_run=True), 1
        )
        self.assertTrue(
            any(
                item["other"] == "b"
                for item in network.synapses_of("a", db_path=self.db)
            )
        )
        self.assertEqual(strength.unbind_weak(now=later, db_path=self.db), 1)

    def test_coactivate_recalled_links_top_neurons(self):
        network.upsert_neuron(
            {"neuron_id": "ep_a", "episode_id": "ep_a"}, db_path=self.db
        )
        network.upsert_neuron(
            {"neuron_id": "ep_b", "episode_id": "ep_b"}, db_path=self.db
        )
        self.node("m1")
        self.node("m2")
        network.upsert_member("ep_a", "m1", db_path=self.db)
        network.upsert_member("ep_b", "m2", db_path=self.db)
        with mock.patch.object(config, "memory_neuron_enabled", return_value=True):
            pairs = strength.coactivate_recalled(
                [{"id": "m1"}, {"id": "m2"}, {"id": "notes:x.md"}],
                db_path=self.db,
            )
        self.assertEqual(pairs, 1)
        links = network.synapses_of("ep_a", db_path=self.db)
        self.assertEqual([item["other"] for item in links], ["ep_b"])

    def test_notes_rows_do_not_create_activation(self):
        result = strength.reinforce_after_recall(
            [{"id": "notes:foo.md", "layer": "notes"}], db_path=self.db
        )
        self.assertEqual(result["memories"], 0)
        with store.connect(self.db) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_activation WHERE node_id='notes:foo.md'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


class LifecycleTest(_NetworkCase):
    def activation(self, node_id, strength, days_idle, **metadata):
        self.node(node_id, **metadata)
        now = datetime(2026, 11, 10, 12, 0, 0)
        network.upsert_member("ep_a", node_id, db_path=self.db)
        network.upsert_activation(
            node_id,
            {
                "strength": strength,
                "last_activated_at": (now - timedelta(days=days_idle)).isoformat(),
                "activation_count": 1 if days_idle < 30 else 0,
            },
            db_path=self.db,
        )
        return now

    def test_protected_stays_active(self):
        now = self.activation("m1", 0.01, 400, protected=True, importance=9)
        lifecycle.classify_tiers(now=now, db_path=self.db)
        self.assertEqual(
            network.get_activation("m1", db_path=self.db)["tier"], "active"
        )

    def test_weak_signal_archived(self):
        now = self.activation("m1", 0.01, 40, importance=2)
        counts = lifecycle.classify_tiers(now=now, db_path=self.db)
        self.assertEqual(counts["archive"], 1)
        activation = network.get_activation("m1", db_path=self.db)
        self.assertEqual(activation["tier"], "archive")
        self.assertTrue(activation["purge_after"])

    def test_strength_bands(self):
        now = self.activation("warm", 0.30, 60)
        self.activation("active", 0.50, 60)
        self.activation("archive", 0.10, 100)
        lifecycle.classify_tiers(now=now, db_path=self.db)
        self.assertEqual(
            network.get_activation("warm", db_path=self.db)["tier"], "warm"
        )
        self.assertEqual(
            network.get_activation("active", db_path=self.db)["tier"], "active"
        )
        self.assertEqual(
            network.get_activation("archive", db_path=self.db)["tier"], "archive"
        )

    def test_classify_dry_run_does_not_write(self):
        now = self.activation("m1", 0.10, 100)
        counts = lifecycle.classify_tiers(now=now, db_path=self.db, dry_run=True)
        self.assertEqual(counts["archive"], 1)
        self.assertEqual(
            network.get_activation("m1", db_path=self.db)["tier"], "active"
        )

    def test_mark_trashed_sets_retention(self):
        now = datetime(2026, 11, 10, 12, 0, 0)
        self.node("m1")
        lifecycle.mark_trashed("m1", now=now, db_path=self.db)
        activation = network.get_activation("m1", db_path=self.db)
        self.assertEqual(activation["tier"], "trash")
        self.assertEqual(
            activation["purge_after"], (now + timedelta(days=30)).isoformat()
        )


class _FakeHot:
    def __init__(self):
        self.deleted = []

    def delete(self, memory_id):
        self.deleted.append(memory_id)


class PurgeTest(_NetworkCase):
    def test_purge_deletes_expired_only(self):
        now = datetime(2026, 11, 10, 12, 0, 0)
        past = (now - timedelta(days=1)).isoformat()
        future = (now + timedelta(days=10)).isoformat()
        for node_id in ("m1", "m2", "m3", "m4"):
            self.node(node_id)
            network.upsert_member("ep_a", node_id, db_path=self.db)
        network.upsert_activation(
            "m1", {"tier": "archive", "purge_after": past}, db_path=self.db
        )
        network.upsert_activation(
            "m2", {"tier": "archive", "purge_after": future}, db_path=self.db
        )
        network.upsert_activation("m3", {"tier": "active"}, db_path=self.db)
        self.node("m4", protected=True)
        network.upsert_activation(
            "m4", {"tier": "archive", "purge_after": past}, db_path=self.db
        )
        hot = _FakeHot()
        with (
            mock.patch.object(lifecycle, "_hot", return_value=hot),
            mock.patch.object(lifecycle, "_qdrant", return_value=None),
        ):
            result = lifecycle.purge_expired(now=now, db_path=self.db)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(hot.deleted, ["m1"])
        self.assertIsNone(store.get_node("m1", db_path=self.db))
        self.assertIsNone(network.get_activation("m1", db_path=self.db))
        for node_id in ("m2", "m3", "m4"):
            self.assertIsNotNone(store.get_node(node_id, db_path=self.db))

    def test_missing_hot_store_keeps_graph_rows(self):
        now = datetime(2026, 11, 10, 12, 0, 0)
        self.node("m1")
        network.upsert_activation(
            "m1",
            {"tier": "archive", "purge_after": (now - timedelta(days=1)).isoformat()},
            db_path=self.db,
        )
        with (
            mock.patch.object(lifecycle, "_hot", return_value=None),
            mock.patch.object(lifecycle, "_qdrant", return_value=None),
        ):
            result = lifecycle.purge_expired(now=now, db_path=self.db)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIsNotNone(store.get_node("m1", db_path=self.db))

    def test_dry_run_does_not_delete(self):
        now = datetime(2026, 11, 10, 12, 0, 0)
        self.node("m1")
        network.upsert_activation(
            "m1",
            {"tier": "archive", "purge_after": (now - timedelta(days=1)).isoformat()},
            db_path=self.db,
        )
        result = lifecycle.purge_expired(now=now, db_path=self.db, dry_run=True)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["deleted"], 0)
        self.assertIsNotNone(store.get_node("m1", db_path=self.db))


class ReconcileTest(_NetworkCase):
    def test_keeps_neuron_with_members_despite_stale_count(self):
        self.node("m1")
        network.upsert_neuron(
            {"neuron_id": "ep_new", "member_count": 0}, db_path=self.db
        )
        network.upsert_member("ep_new", "m1", db_path=self.db)
        result = network_build.reconcile_network(db_path=self.db)
        self.assertEqual(result["empty_neurons"], 0)
        self.assertIsNotNone(network.get_neuron("ep_new", db_path=self.db))
        self.assertEqual(network.neuron_of("m1", db_path=self.db), "ep_new")
        self.assertEqual(
            network.get_neuron("ep_new", db_path=self.db)["member_count"], 1
        )

    def test_removes_truly_empty_neuron_with_stale_count(self):
        network.upsert_neuron(
            {"neuron_id": "ep_old", "member_count": 3}, db_path=self.db
        )
        result = network_build.reconcile_network(db_path=self.db)
        self.assertEqual(result["empty_neurons"], 1)
        self.assertIsNone(network.get_neuron("ep_old", db_path=self.db))

    def test_removes_member_pointing_to_missing_neuron(self):
        self.node("m1")
        network.upsert_member("ep_missing", "m1", db_path=self.db)
        result = network_build.reconcile_network(db_path=self.db)
        self.assertEqual(result["ghost_members"], 1)
        self.assertEqual(network.neuron_of("m1", db_path=self.db), "")
        self.assertEqual(network.stats(db_path=self.db)["orphan_members"], 0)


class MergeSplitTest(_NetworkCase):
    def test_merge_moves_every_member(self):
        network.ensure_neuron(
            {"neuron_id": "ep_source", "episode_id": "ep_source"}, db_path=self.db
        )
        network.ensure_neuron(
            {"neuron_id": "ep_target", "episode_id": "ep_target"}, db_path=self.db
        )
        for index in range(205):
            node_id = f"m{index:03d}"
            self.node(node_id)
            network.upsert_member("ep_source", node_id, db_path=self.db)
        result = network_build.merge_neurons("ep_source", "ep_target", db_path=self.db)
        self.assertEqual(result["members"], 205)
        self.assertEqual(
            len(network.members_of("ep_target", limit=1000, db_path=self.db)), 205
        )
        self.assertIsNone(network.get_neuron("ep_source", db_path=self.db))


class GovernanceSyncTest(_NetworkCase):
    def setUp(self):
        super().setUp()
        self.node("a1", episode_id="ep_a")
        self.node("a2", episode_id="ep_a")
        self.node("b1", episode_id="ep_b")
        self.node("b2", episode_id="ep_b")
        self.node("x1", episode_id="ep_x")
        network_build.migrate_network(db_path=self.db, apply=True)

    def test_sync_episode_change_moves_membership(self):
        self.assertTrue(
            network_build.sync_episode_change("a1", "ep_a", "ep_b", db_path=self.db)
        )
        self.assertEqual(network.neuron_of("a1", db_path=self.db), "ep_b")
        self.assertEqual(
            [item["node_id"] for item in network.members_of("ep_b", db_path=self.db)],
            ["a1", "b1", "b2"],
        )
        self.assertEqual(
            [item["node_id"] for item in network.members_of("ep_a", db_path=self.db)],
            ["a2"],
        )

    def test_merge_neurons_moves_members_and_synapses(self):
        network.upsert_synapse(
            {"from_neuron": "ep_b", "to_neuron": "ep_x", "weight": 0.8},
            db_path=self.db,
        )
        result = network_build.merge_neurons("ep_b", "ep_a", db_path=self.db)
        self.assertEqual(result["members"], 2)
        self.assertIsNone(network.get_neuron("ep_b", db_path=self.db))
        self.assertEqual(
            [item["node_id"] for item in network.members_of("ep_a", db_path=self.db)],
            ["a1", "a2", "b1", "b2"],
        )
        self.assertEqual(
            [item["other"] for item in network.synapses_of("ep_a", db_path=self.db)],
            ["ep_x"],
        )

    def test_group_pair_candidates_come_from_synapses(self):
        network.upsert_synapse(
            {"from_neuron": "ep_a", "to_neuron": "ep_b", "weight": 0.8},
            db_path=self.db,
        )
        pairs = group_governance.load_group_pair_candidates(
            db_path=self.db, min_weight=0.62
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["kind"], "group_pair")
        self.assertEqual(len(pairs[0]["members"]), 4)
        self.assertEqual(
            group_governance._validate_decision(
                {
                    "candidate_id": pairs[0]["candidate_id"],
                    "action": "split",
                    "groups": [["a1"], ["a2"]],
                },
                pairs,
            )[1],
            "bad_split",
        )
        valid, reason = group_governance._validate_decision(
            {"candidate_id": pairs[0]["candidate_id"], "action": "merge"},
            pairs,
        )
        self.assertEqual(reason, "")
        self.assertEqual(valid["action"], "merge")

    def test_split_neuron_distributes_external_synapses(self):
        network.upsert_synapse(
            {"from_neuron": "ep_a", "to_neuron": "ep_x", "weight": 0.8},
            db_path=self.db,
        )
        result = network_build.split_neuron(
            "ep_a",
            {"ep_new1": ["a1"], "ep_new2": ["a2"]},
            db_path=self.db,
        )
        self.assertEqual(result["groups"], 2)
        self.assertIsNone(network.get_neuron("ep_a", db_path=self.db))
        first = network.synapses_of("ep_new1", db_path=self.db)
        second = network.synapses_of("ep_new2", db_path=self.db)
        self.assertEqual([item["other"] for item in first], ["ep_x"])
        self.assertEqual([item["other"] for item in second], ["ep_x"])
        self.assertAlmostEqual(first[0]["weight"], 0.4)
        self.assertAlmostEqual(second[0]["weight"], 0.4)


class _Point:
    def __init__(self, point_id, vector=None, score=None):
        self.id = point_id
        self.vector = vector
        self.score = score


class _Result:
    def __init__(self, points):
        self.points = points


class _FakeClient:
    def __init__(self, vectors):
        self.vectors = vectors

    def retrieve(self, collection_name, ids, with_payload, with_vectors):
        return [
            _Point(point_id, vector=self.vectors[point_id])
            for point_id in ids
            if point_id in self.vectors
        ]

    def query_batch_points(self, collection_name, requests):
        results = []
        for request in requests:
            scored = []
            for point_id, vector in self.vectors.items():
                dot = sum(a * b for a, b in zip(request.query, vector))
                left = sum(a * a for a in request.query) ** 0.5
                right = sum(b * b for b in vector) ** 0.5
                scored.append((dot / (left * right), point_id))
            scored.sort(reverse=True)
            results.append(
                _Result(
                    [
                        _Point(point_id, score=score)
                        for score, point_id in scored[: request.limit]
                    ]
                )
            )
        return results


class _FakeStore:
    def __init__(self, vectors):
        self.client = _FakeClient(vectors)
        self.collection_name = "mem0"


class _FakeVectorHot:
    def __init__(self, vectors):
        self.vector_store = _FakeStore(vectors)


VECTORS = {
    "a1": [1.0, 0.0],
    "a2": [0.9, 0.1],
    "b1": [0.95, 0.05],
    "b2": [0.8, 0.2],
    "c1": [0.0, 1.0],
}


class GovernanceCandidateTest(_NetworkCase):
    def test_group_pair_only_candidates_are_judged(self):
        candidates = {
            "groups": [],
            "pairs": [],
            "singles": [],
            "group_pairs": [
                {
                    "candidate_id": "g1",
                    "members": [{"id": "m1", "created_at": "2026-11-10"}],
                }
            ],
        }

        async def fake_decide(*args, **kwargs):
            return {"status": "ok", "decisions": [], "calls": 0, "parse_failed": 0}

        with (
            mock.patch.object(
                group_governance, "build_candidates", return_value=candidates
            ),
            mock.patch.object(group_governance, "_load_active_nodes", return_value=[]),
            mock.patch.object(group_governance, "decide", side_effect=fake_decide),
            mock.patch.object(
                group_governance.config, "memory_governance_apply", return_value=True
            ),
            mock.patch.object(
                group_governance.llm_client, "is_configured", return_value=True
            ),
        ):
            result = asyncio.run(group_governance.govern_daily(db_path=self.db))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"], 1)


class SynapseBuildTest(_NetworkCase):
    def setUp(self):
        super().setUp()
        self.node("a1", episode_id="ep_a")
        self.node("a2", episode_id="ep_a")
        self.node("b1", episode_id="ep_b")
        self.node("b2", episode_id="ep_b")
        self.node("c1", episode_id="ep_c")
        network_build.migrate_network(db_path=self.db, apply=True)
        self.hot = _FakeVectorHot(VECTORS)

    def test_candidates_use_centroids_and_floor(self):
        result = asyncio.run(
            network_build.build_synapse_candidates(
                db_path=self.db, hot=self.hot, llm_limit=0
            )
        )
        pairs = {(item["left_group"], item["right_group"]) for item in result["pairs"]}
        self.assertIn(("ep_a", "ep_b"), pairs)
        self.assertNotIn(("ep_a", "ep_c"), pairs)
        self.assertFalse(result["vector_unavailable"])
        self.assertEqual(result["budget_exhausted"], False)

    def test_since_anchors_compare_across_timezones(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE memory_nodes SET created_at='2026-11-11T10:00:00+00:00' "
                "WHERE node_id IN ('a1', 'a2')"
            )
        local_naive = (
            datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
            .astimezone()
            .replace(tzinfo=None)
        )
        result = asyncio.run(
            network_build.build_synapse_candidates(
                db_path=self.db,
                hot=self.hot,
                since=local_naive.isoformat(),
                llm_limit=0,
            )
        )
        pairs = {(item["left_group"], item["right_group"]) for item in result["pairs"]}
        self.assertIn(("ep_a", "ep_b"), pairs)

    def test_seed_neurons_restrict_anchor_scan(self):
        result = asyncio.run(
            network_build.build_synapse_candidates(
                db_path=self.db,
                hot=self.hot,
                llm_limit=0,
                seed_neurons=["ep_c"],
            )
        )
        for pair in result["pairs"]:
            self.assertIn("ep_c", (pair["left_group"], pair["right_group"]))

    def test_llm_unconfigured_marks_skipped(self):
        with mock.patch.object(
            network_build.llm_client, "is_configured", return_value=False
        ):
            result = asyncio.run(
                network_build.build_synapse_candidates(
                    db_path=self.db, hot=self.hot, llm_limit=10
                )
            )
        self.assertTrue(result["llm_skipped"])
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["parse_failed"], 0)
        self.assertTrue(result["decisions"])

    def test_review_pairs_rejects_cross_batch_ids(self):
        pairs = [
            {
                "pair_id": f"syn:{name}|{name}x",
                "left_group": name,
                "right_group": f"{name}x",
                "score": 0.6,
                "cosine": 0.6,
                "left_samples": [],
                "right_samples": [],
            }
            for name in ("a", "c")
        ]
        raw = json.dumps(
            {
                "decisions": [
                    {"pair_id": "syn:c|cx", "relation": "same_task", "strength": 0.9}
                ]
            }
        )
        llm = mock.AsyncMock(return_value=raw)
        result = asyncio.run(
            network_build.review_pairs(pairs, llm=llm, batch_size=1, max_calls=2)
        )
        self.assertEqual(
            [item["pair_id"] for item in result["decisions"]], ["syn:c|cx"]
        )

    def test_review_pairs_validates_output(self):
        pair = {
            "pair_id": "syn:ep_a|ep_b",
            "left_group": "ep_a",
            "right_group": "ep_b",
            "score": 0.8,
            "left_samples": [{"text": "a", "truncated": False}],
            "right_samples": [{"text": "b", "truncated": False}],
        }

        async def valid(prompt, **kwargs):
            return json.dumps(
                {
                    "decisions": [
                        {
                            "pair_id": "syn:ep_a|ep_b",
                            "relation": "same_task",
                            "strength": 0.9,
                        },
                        {
                            "pair_id": "syn:unknown|x",
                            "relation": "same_task",
                            "strength": 0.9,
                        },
                        {
                            "pair_id": "syn:ep_a|ep_b",
                            "relation": "none",
                            "strength": 0.9,
                        },
                    ]
                }
            )

        result = asyncio.run(network_build.review_pairs([pair], llm=valid, max_calls=1))
        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(result["decisions"][0]["final_weight"], 0.85)
        self.assertEqual(result["rejected"], 2)

        async def broken(prompt, **kwargs):
            return "not json"

        failed = asyncio.run(
            network_build.review_pairs([pair], llm=broken, max_calls=1)
        )
        self.assertEqual(failed["decisions"], [])
        self.assertEqual(failed["parse_failed"], 1)

    def test_write_synapses_applies_threshold(self):
        decisions = [
            {
                "left_group": "ep_a",
                "right_group": "ep_b",
                "relation": "same_task",
                "final_weight": 0.7,
            },
            {
                "left_group": "ep_a",
                "right_group": "ep_c",
                "relation": "reference",
                "final_weight": 0.5,
            },
        ]
        self.assertEqual(network_build.write_synapses(decisions, db_path=self.db), 1)
        links = network.synapses_of("ep_a", db_path=self.db)
        self.assertEqual([item["other"] for item in links], ["ep_b"])

    def test_degree_cap_archives_low_weight(self):
        for index, target in enumerate(("t1", "t2", "t3")):
            network.upsert_synapse(
                {
                    "from_neuron": "hub",
                    "to_neuron": target,
                    "weight": 0.9 - index * 0.1,
                },
                db_path=self.db,
            )
        self.assertEqual(
            network_build.enforce_degree_cap(db_path=self.db, cap=1, dry_run=True),
            2,
        )
        self.assertEqual(len(network.synapses_of("hub", db_path=self.db)), 3)
        self.assertEqual(network_build.enforce_degree_cap(db_path=self.db, cap=1), 2)
        active = network.synapses_of("hub", db_path=self.db)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["other"], "t1")


class RollbackScriptTest(_NetworkCase):
    def _script(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "memory_network_rollback.py"
        )
        spec = importlib.util.spec_from_file_location("memory_network_rollback", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_export_import_round_trip_with_blob(self):
        script = self._script()
        centroid = b"\x00\x01\x02\x03"
        network.upsert_neuron(
            {
                "neuron_id": "ep_a",
                "episode_id": "ep_a",
                "centroid": centroid,
            },
            db_path=self.db,
        )
        self.node("m1")
        network.upsert_member("ep_a", "m1", db_path=self.db)
        target = Path(self._tmp.name) / "export.json"
        result = script._export(self.db, target)
        self.assertEqual(result["tables"]["neurons"], 1)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("__bytes__", payload["neurons"][0]["centroid"])

        script._drop(self.db)
        with store.connect(self.db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("neurons", tables)
        self.assertEqual(int(version), store.SCHEMA_VERSION)

        restored = script._import(self.db, target)
        self.assertEqual(restored["tables"]["neuron_members"], 1)
        with store.connect(self.db) as connection:
            row = connection.execute(
                "SELECT centroid FROM neurons WHERE neuron_id='ep_a'"
            ).fetchone()
        self.assertEqual(row[0], centroid)


class PromptTextCompletenessTest(unittest.TestCase):
    def test_neuron_link_samples_keep_full_fact(self):
        fact = "x" * 5000
        samples = network_build._sample([{"node_id": "n1", "metadata": {"fact": fact}}])
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["id"], "n1")
        self.assertEqual(samples[0]["text"], fact)

    def test_governance_member_keeps_full_fact(self):
        fact = "y" * 5000
        member = group_governance._member({"node_id": "n2", "metadata": {"fact": fact}})
        self.assertEqual(member["text"], fact)
        self.assertNotIn("truncated", member)


class ReviewConcurrencyTest(unittest.TestCase):
    def _pairs(self, count):
        return [
            {
                "pair_id": f"syn:ep_{index}|ep_{index + 1}",
                "left_group": f"ep_{index}",
                "right_group": f"ep_{index + 1}",
                "score": 0.8,
                "left_samples": [{"id": f"a{index}", "text": "a"}],
                "right_samples": [{"id": f"b{index}", "text": "b"}],
            }
            for index in range(count)
        ]

    def test_concurrent_review_matches_sequential(self):
        pairs = self._pairs(30)

        async def llm(prompt, **kwargs):
            return json.dumps({"decisions": []})

        sequential = asyncio.run(
            network_build.review_pairs(pairs, llm=llm, concurrency=1, batch_size=10)
        )
        concurrent = asyncio.run(
            network_build.review_pairs(pairs, llm=llm, concurrency=5, batch_size=10)
        )
        self.assertEqual(sequential["calls"], concurrent["calls"])
        self.assertEqual(sequential["parse_failed"], concurrent["parse_failed"])
        self.assertEqual(sequential["rejected"], concurrent["rejected"])

    def test_semaphore_caps_in_flight_batches(self):
        pairs = self._pairs(40)
        active = 0
        peak = 0

        async def llm(prompt, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return json.dumps({"decisions": []})

        asyncio.run(
            network_build.review_pairs(pairs, llm=llm, concurrency=3, batch_size=10)
        )
        self.assertLessEqual(peak, 3)
        self.assertGreater(peak, 1)


class EventLoopYieldTest(unittest.TestCase):
    def test_build_candidates_yields_to_event_loop(self):
        ticks = 0

        def slow_group_nodes(db_path):
            time.sleep(0.15)
            return {}

        async def run():
            nonlocal ticks

            async def ticker():
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            task = asyncio.create_task(ticker())
            with (
                mock.patch.object(
                    network_build, "_group_nodes", side_effect=slow_group_nodes
                ),
                mock.patch.object(network_build, "_try_hot", return_value=None),
            ):
                await network_build.build_synapse_candidates(db_path="/tmp/x")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        self.assertGreater(ticks, 3)


if __name__ == "__main__":
    unittest.main()
