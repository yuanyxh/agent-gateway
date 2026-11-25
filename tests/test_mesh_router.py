"""Tests for mesh routing between local cards and sibling gateways."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mesh.models import (  # noqa: E402
    MeshErrorCode,
    MeshMessage,
    ServiceCard,
)
from agent_gateway.services.mesh.registry import FileRegistry  # noqa: E402
from agent_gateway.services.mesh.router import Router  # noqa: E402
from agent_gateway.services.mesh.transport import (  # noqa: E402
    DirectoryTransport,
    Peer,
    TransportError,
)


def make_card(service_id: str = "ws-b", **overrides) -> ServiceCard:
    fields = {
        "id": service_id,
        "name": "gateway",
        "kind": "workspace",
        "endpoint": f"{service_id}",
    }
    fields.update(overrides)
    return ServiceCard(**fields)


def make_message(recipient: str = "ws-b") -> MeshMessage:
    return MeshMessage(
        sender="ws-a", recipient=recipient, kind="task", payload={"text": "hi"}
    )


class StubRemote:
    def __init__(self, cards=None):
        self.peers = [Peer(name="gw-remote", base_url="http://gw-remote")]
        self.cards = {card.id: card for card in cards or []}
        self.delivered = []

    def find(self, service_id):
        card = self.cards.get(service_id)
        return (self.peers[0], card) if card else None

    def deliver(self, card, message):
        self.delivered.append((card.id, message.id))

    def list_peer(self, peer):
        return list(self.cards.values())


class RouterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FileRegistry(self.root / "mesh", "gw-local")
        self.inbox = self.root / "inbox"
        self.local = DirectoryTransport(self.inbox)
        self.router = Router(self.registry, self.local, gateway_id="gw-local")

    def tearDown(self):
        self._tmp.cleanup()

    def test_send_local_writes_inbox_and_marks_pending(self):
        self.router.register(make_card())
        result = self.router.send(make_message())
        self.assertTrue(result["ok"])
        self.assertEqual(len(list((self.inbox / "ws-b").glob("*.json"))), 1)
        self.assertEqual(self.registry.get("ws-b").status, "pending")

    def test_expired_target_returns_offline_with_startable(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.router.register(
            make_card(heartbeat_at=stale, startable=True, start_command="claude")
        )
        result = self.router.send(make_message())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], MeshErrorCode.TARGET_OFFLINE.value)
        self.assertTrue(result["startable"])
        self.assertEqual(
            list((self.inbox / "ws-b").glob("*.json"))
            if (self.inbox / "ws-b").exists()
            else [],
            [],
        )

    def test_control_start_reaches_expired_card(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.router.register(
            make_card(heartbeat_at=stale, startable=True, start_command="claude")
        )
        start = MeshMessage(
            sender="gateway",
            recipient="ws-b",
            kind="control",
            payload={"action": "start"},
        )
        result = self.router.send(start)
        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        self.assertEqual(len(list((self.inbox / "ws-b").glob("*.json"))), 1)
        stored = self.registry.get("ws-b", include_expired=True)
        self.assertEqual(stored.status, "starting")

    def test_control_start_requires_startable(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.router.register(make_card(heartbeat_at=stale, startable=False))
        start = MeshMessage(
            sender="gateway",
            recipient="ws-b",
            kind="control",
            payload={"action": "start"},
        )
        result = self.router.send(start)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], MeshErrorCode.TARGET_OFFLINE.value)
        self.assertFalse(result["startable"])

    def test_register_rejects_endpoint_outside_inbox(self):
        outside = self.router.register(make_card(endpoint="/etc/passwd"))
        self.assertFalse(outside["ok"])
        self.assertEqual(outside["error_code"], MeshErrorCode.INVALID_CARD.value)
        escape = self.router.register(make_card(endpoint="../../escape"))
        self.assertFalse(escape["ok"])

    def test_absorbed_card_routes_to_peer(self):
        card = make_card(
            "ws-r",
            transport="http",
            endpoint="http://gw-remote",
            owner_gateway="gw-remote",
        )
        remote = StubRemote([card])
        router = Router(self.registry, self.local, remote=remote, gateway_id="gw-local")
        self.assertTrue(router.absorb(card))
        result = router.send(make_message("ws-r"))
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("forwarded"))
        self.assertEqual(remote.delivered[0][0], "ws-r")

    def test_probe_remote_expired_is_offline(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=300)
        card = make_card(
            "ws-r",
            transport="http",
            endpoint="http://gw-remote",
            owner_gateway="gw-remote",
            heartbeat_at=stale,
        )
        router = Router(
            self.registry, self.local, remote=StubRemote([card]), gateway_id="gw-local"
        )
        result = router.probe("ws-r")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "offline")

    def test_unknown_target(self):
        result = self.router.send(make_message("ws-missing"))
        self.assertEqual(result["error_code"], MeshErrorCode.TARGET_UNKNOWN.value)

    def test_forwards_to_remote(self):
        remote = StubRemote([make_card("ws-remote")])
        router = Router(self.registry, self.local, remote=remote, gateway_id="gw-local")
        result = router.send(make_message("ws-remote"))
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("forwarded"))
        self.assertEqual(remote.delivered[0][0], "ws-remote")

    def test_delivery_failure_is_returned(self):
        class FailingLocal:
            def deliver(self, card, message):
                raise TransportError(MeshErrorCode.DELIVERY_FAILED, "disk full")

        self.router.register(make_card())
        router = Router(self.registry, FailingLocal(), gateway_id="gw-local")
        result = router.send(make_message())
        self.assertEqual(result["error_code"], MeshErrorCode.DELIVERY_FAILED.value)

    def test_registry_full_is_returned(self):
        registry = FileRegistry(self.root / "small", "gw-local", max_services=1)
        router = Router(registry, self.local, gateway_id="gw-local")
        self.assertTrue(router.register(make_card("ws-1"))["ok"])
        result = router.register(make_card("ws-2"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], MeshErrorCode.REGISTRY_FULL.value)

    def test_probe_local_and_unknown(self):
        self.assertEqual(self.router.probe("ws-b")["status"], "unknown")
        self.router.register(make_card())
        self.assertEqual(self.router.probe("ws-b")["status"], "ready")

    def test_list_merges_remote_cards(self):
        remote = StubRemote([make_card("ws-remote")])
        router = Router(self.registry, self.local, remote=remote, gateway_id="gw-local")
        router.register(make_card("ws-local"))
        ids = {card["id"] for card in router.list()}
        self.assertEqual(ids, {"ws-local", "ws-remote"})


class AbsorbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FileRegistry(self.root / "mesh", "gw-local")
        self.router = Router(
            self.registry,
            DirectoryTransport(self.root / "inbox"),
            gateway_id="gw-local",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_absorbs_workspace_card(self):
        card = make_card("ws-remote", owner_gateway="gw-remote")
        self.assertTrue(self.router.absorb(card))
        stored = self.registry.get("ws-remote", include_expired=True)
        self.assertEqual(stored.owner_gateway, "gw-remote")

    def test_ignores_gateway_kind(self):
        card = make_card("gw-x", kind="gateway")
        self.assertFalse(self.router.absorb(card))
        self.assertIsNone(self.registry.get("gw-x", include_expired=True))

    def test_does_not_overwrite_known_card(self):
        self.router.register(make_card("ws-b", name="original"))
        changed = make_card("ws-b", name="impostor")
        self.assertFalse(self.router.absorb(changed))
        self.assertEqual(
            self.registry.get("ws-b", include_expired=True).name, "original"
        )

    def test_caps_ttl(self):
        self.router.absorb(make_card("ws-remote", ttl_seconds=9999))
        self.assertEqual(
            self.registry.get("ws-remote", include_expired=True).ttl_seconds, 300
        )


if __name__ == "__main__":
    unittest.main()
