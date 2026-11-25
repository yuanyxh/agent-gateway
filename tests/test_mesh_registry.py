"""Tests for the file-backed mesh service registry."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mesh.models import MeshErrorCode, ServiceCard  # noqa: E402
from agent_gateway.services.mesh.registry import FileRegistry, RegistryError  # noqa: E402


def make_card(service_id: str = "ws-1", **overrides) -> ServiceCard:
    fields = {
        "id": service_id,
        "name": "utility",
        "kind": "workspace",
        "endpoint": f"/tmp/mesh/inbox/{service_id}",
    }
    fields.update(overrides)
    return ServiceCard(**fields)


class FileRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FileRegistry(self.root, "gw-test", max_services=3)

    def tearDown(self):
        self._tmp.cleanup()

    def test_register_and_get(self):
        self.registry.register(make_card())
        card = self.registry.get("ws-1")
        self.assertIsNotNone(card)
        self.assertEqual(card.name, "utility")

    def test_register_fills_owner_gateway(self):
        stored = self.registry.register(make_card())
        self.assertEqual(stored.owner_gateway, "gw-test")
        self.assertEqual(self.registry.get("ws-1").owner_gateway, "gw-test")

    def test_rejects_path_traversal_id(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry.register(make_card("../evil"))
        self.assertEqual(ctx.exception.code, MeshErrorCode.INVALID_CARD)

    def test_registry_full(self):
        for index in range(3):
            self.registry.register(make_card(f"ws-{index}"))
        with self.assertRaises(RegistryError) as ctx:
            self.registry.register(make_card("ws-overflow"))
        self.assertEqual(ctx.exception.code, MeshErrorCode.REGISTRY_FULL)

    def test_reregister_does_not_count_as_new(self):
        for index in range(3):
            self.registry.register(make_card(f"ws-{index}"))
        self.registry.register(make_card("ws-0", name="renamed"))
        self.assertEqual(self.registry.get("ws-0").name, "renamed")

    def test_expired_card_marked_offline(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.registry.register(make_card(heartbeat_at=stale))
        listed = self.registry.list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].status, "offline")

    def test_get_hides_expired_by_default(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.registry.register(make_card(heartbeat_at=stale))
        self.assertIsNone(self.registry.get("ws-1"))
        self.assertIsNotNone(self.registry.get("ws-1", include_expired=True))

    def test_touch_refreshes_heartbeat(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.registry.register(make_card(heartbeat_at=stale))
        self.assertTrue(self.registry.touch("ws-1", status="ready"))
        card = self.registry.get("ws-1")
        self.assertIsNotNone(card)
        self.assertEqual(card.status, "ready")

    def test_touch_unknown_returns_false(self):
        self.assertFalse(self.registry.touch("missing"))

    def test_deregister_moves_to_trash(self):
        self.registry.register(make_card())
        self.assertTrue(self.registry.deregister("ws-1"))
        self.assertIsNone(self.registry.get("ws-1"))
        trashed = self.registry.trash_dir / "ws-1.json"
        self.assertTrue(trashed.is_file())

    def test_no_temp_files_left(self):
        self.registry.register(make_card())
        leftovers = list(self.registry.services_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_corrupt_file_is_skipped(self):
        self.registry.register(make_card())
        (self.registry.services_dir / "broken.json").write_text(
            "{not json", encoding="utf-8"
        )
        listed = self.registry.list()
        self.assertEqual([card.id for card in listed], ["ws-1"])

    def test_touch_rejects_unknown_status(self):
        self.registry.register(make_card())
        self.assertFalse(self.registry.touch("ws-1", status="bogus"))
        card = self.registry.get("ws-1")
        self.assertIsNotNone(card)
        self.assertEqual(card.status, "ready")

    def test_touch_after_expiry_keeps_status(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.registry.register(make_card(heartbeat_at=stale))
        self.assertTrue(self.registry.touch("ws-1"))
        card = self.registry.get("ws-1", include_expired=True)
        self.assertTrue(card.is_alive())
        self.assertEqual(card.status, "ready")

    def test_register_prunes_stale_cards_when_full(self):
        registry = FileRegistry(self.root / "small", "gw-test", max_services=1)
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        registry.register(make_card("old", heartbeat_at=stale))
        registry.register(make_card("new"))
        self.assertIsNotNone(registry.get("new"))
        self.assertIsNone(registry.get("old", include_expired=True))


if __name__ == "__main__":
    unittest.main()
