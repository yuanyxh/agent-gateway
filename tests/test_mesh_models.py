"""Tests for mesh service cards and messages."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import ValidationError  # noqa: E402

from agent_gateway.services.mesh.models import (  # noqa: E402
    DEFAULT_TTL_SECONDS,
    MAX_PAYLOAD_BYTES,
    MeshMessage,
    ServiceCard,
)


def make_card(**overrides) -> ServiceCard:
    fields = {
        "id": "ws-1",
        "name": "utility",
        "kind": "workspace",
        "endpoint": "/tmp/mesh/inbox/ws-1",
    }
    fields.update(overrides)
    return ServiceCard(**fields)


class ServiceCardTests(unittest.TestCase):
    def test_roundtrip_preserves_fields(self):
        card = make_card(status="pending", start_command="claude", startable=True)
        restored = ServiceCard.model_validate_json(card.model_dump_json())
        self.assertEqual(restored, card)

    def test_defaults(self):
        card = make_card()
        self.assertEqual(card.schema_version, "utility.mesh/service-card/v1")
        self.assertEqual(card.transport, "directory")
        self.assertEqual(card.ttl_seconds, DEFAULT_TTL_SECONDS)
        self.assertFalse(card.startable)

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValidationError):
            make_card(kind="robot")

    def test_rejects_empty_identifier(self):
        with self.assertRaises(ValidationError):
            make_card(id="  ")

    def test_rejects_empty_endpoint(self):
        with self.assertRaises(ValidationError):
            make_card(endpoint="")

    def test_is_alive_until_ttl(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        card = make_card(heartbeat_at=now)
        self.assertTrue(card.is_alive(now + timedelta(seconds=119)))
        self.assertFalse(card.is_alive(now + timedelta(seconds=121)))

    def test_is_alive_accepts_naive_timestamp(self):
        card = make_card(heartbeat_at=datetime(2026, 11, 25, 12, 0))
        now = datetime(2026, 11, 25, 12, 1, tzinfo=timezone.utc)
        self.assertTrue(card.is_alive(now))


class MeshMessageTests(unittest.TestCase):
    def test_roundtrip(self):
        message = MeshMessage(
            sender="ws-1",
            recipient="ws-2",
            kind="task",
            payload={"text": "改一下解析器"},
        )
        restored = MeshMessage.model_validate_json(message.model_dump_json())
        self.assertEqual(restored.sender, "ws-1")
        self.assertEqual(restored.kind, "task")

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValidationError):
            MeshMessage(sender="a", recipient="b", kind="broadcast")

    def test_rejects_oversized_payload(self):
        big = "x" * (MAX_PAYLOAD_BYTES + 1)
        with self.assertRaises(ValidationError):
            MeshMessage(sender="a", recipient="b", payload={"text": big})

    def test_accepts_payload_at_limit(self):
        text = "x" * (MAX_PAYLOAD_BYTES - 20)
        message = MeshMessage(sender="a", recipient="b", payload={"text": text})
        self.assertTrue(message.id)


if __name__ == "__main__":
    unittest.main()
