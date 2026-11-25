"""Tests for mesh transports."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mesh.models import (  # noqa: E402
    MeshErrorCode,
    MeshMessage,
    ServiceCard,
)
import httpx  # noqa: E402

from agent_gateway.services.mesh.transport import (  # noqa: E402
    DirectoryTransport,
    HTTPTransport,
    Peer,
    TransportError,
)


def make_card(endpoint: str, service_id: str = "ws-1") -> ServiceCard:
    return ServiceCard(
        id=service_id, name="utility", kind="workspace", endpoint=endpoint
    )


def make_message() -> MeshMessage:
    return MeshMessage(sender="ws-1", recipient="ws-2", payload={"text": "hi"})


class DirectoryTransportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.transport = DirectoryTransport(self.root / "inbox")

    def tearDown(self):
        self._tmp.cleanup()

    def test_delivers_one_file(self):
        card = make_card("/tmp/irrelevant")
        card = card.model_copy(update={"endpoint": str(self.root / "inbox" / "ws-2")})
        self.transport.deliver(card, make_message())
        files = list((self.root / "inbox" / "ws-2").glob("*.json"))
        self.assertEqual(len(files), 1)
        restored = MeshMessage.model_validate_json(files[0].read_text(encoding="utf-8"))
        self.assertEqual(restored.payload, {"text": "hi"})

    def test_relative_endpoint_uses_inbox_root(self):
        card = make_card("ws-3")
        self.transport.deliver(card, make_message())
        self.assertEqual(len(list((self.root / "inbox" / "ws-3").glob("*.json"))), 1)

    def test_probe(self):
        card = make_card("ws-4")
        self.assertFalse(self.transport.probe(card))
        (self.root / "inbox" / "ws-4").mkdir(parents=True)
        self.assertTrue(self.transport.probe(card))

    def test_no_temp_files_left(self):
        card = make_card("ws-5")
        self.transport.deliver(card, make_message())
        self.assertEqual(list((self.root / "inbox" / "ws-5").glob("*.tmp")), [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_target_raises(self):
        box = self.root / "inbox" / "ws-locked"
        box.mkdir(parents=True)
        os.chmod(box, 0o500)
        try:
            with self.assertRaises(TransportError) as ctx:
                self.transport.deliver(make_card("ws-locked"), make_message())
            self.assertEqual(ctx.exception.code, MeshErrorCode.DELIVERY_FAILED)
            self.assertEqual(list(box.glob("*.tmp")), [])
        finally:
            os.chmod(box, 0o700)


class HTTPTransportTests(unittest.TestCase):
    def build(self, handler, peers):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return HTTPTransport(peers, client=client)

    def test_deliver_sends_message_with_token(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"ok": True})

        transport = self.build(
            handler, [Peer(name="gw-b", base_url="http://gw-b", token="secret")]
        )
        card = make_card("http://gw-b")
        card = card.model_copy(update={"transport": "http", "owner_gateway": "gw-b"})
        transport.deliver(card, make_message())
        self.assertEqual(seen["path"], "/mesh/send")
        self.assertEqual(seen["auth"], "Bearer secret")

    def test_deliver_maps_remote_offline(self):
        def handler(request):
            return httpx.Response(
                409,
                json={"ok": False, "error_code": "target_offline", "startable": True},
            )

        transport = self.build(
            handler, [Peer(name="gw-b", base_url="http://gw-b", token="t")]
        )
        card = make_card("http://gw-b").model_copy(
            update={"transport": "http", "owner_gateway": "gw-b"}
        )
        with self.assertRaises(TransportError) as ctx:
            transport.deliver(card, make_message())
        self.assertEqual(ctx.exception.code, MeshErrorCode.TARGET_OFFLINE)
        self.assertTrue(ctx.exception.startable)

    def test_deliver_unreachable_raises(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        transport = self.build(
            handler, [Peer(name="gw-b", base_url="http://gw-b", token="secret")]
        )
        card = make_card("http://gw-b").model_copy(
            update={"transport": "http", "owner_gateway": "gw-b"}
        )
        with self.assertRaises(TransportError) as ctx:
            transport.deliver(card, make_message())
        self.assertEqual(ctx.exception.code, MeshErrorCode.PEER_UNREACHABLE)

    def test_deliver_directory_card_routes_by_owner(self):
        seen = {}

        def handler(request):
            seen["host"] = request.url.host
            return httpx.Response(200, json={"ok": True})

        transport = self.build(
            handler, [Peer(name="gw-b", base_url="http://gw-b", token="secret")]
        )
        card = make_card("/tmp/x").model_copy(update={"owner_gateway": "gw-b"})
        transport.deliver(card, make_message())
        self.assertEqual(seen["host"], "gw-b")

    def test_deliver_without_peer_raises(self):
        transport = self.build(lambda request: httpx.Response(200), [])
        card = make_card("/tmp/x").model_copy(update={"owner_gateway": "gw-z"})
        with self.assertRaises(TransportError) as ctx:
            transport.deliver(card, make_message())
        self.assertEqual(ctx.exception.code, MeshErrorCode.PEER_UNREACHABLE)

    def test_find_hits_second_peer(self):
        def handler(request):
            if request.url.host == "gw-a":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "card": make_card("http://gw-b")
                    .model_copy(update={"transport": "http", "owner_gateway": "gw-b"})
                    .model_dump(mode="json"),
                },
            )

        peers = [
            Peer(name="gw-a", base_url="http://gw-a"),
            Peer(name="gw-b", base_url="http://gw-b", token="t"),
        ]
        transport = self.build(handler, peers)
        found = transport.find("ws-1")
        self.assertIsNotNone(found)
        self.assertEqual(found[0].name, "gw-b")

    def test_list_peer_requests_local_cards_only(self):
        seen = {}

        def handler(request):
            seen["remote"] = request.url.params.get("remote")
            card = make_card("http://gw-b", service_id="ws-9").model_copy(
                update={"transport": "http", "owner_gateway": "gw-b"}
            )
            return httpx.Response(
                200, json={"ok": True, "services": [card.model_dump(mode="json")]}
            )

        transport = self.build(handler, [Peer(name="gw-b", base_url="http://gw-b")])
        cards = transport.list_peer(transport.peers[0])
        self.assertEqual(seen["remote"], "0")
        self.assertEqual([card.id for card in cards], ["ws-9"])

    def test_probe_health(self):
        transport = self.build(
            lambda request: httpx.Response(200),
            [Peer(name="gw-b", base_url="http://gw-b")],
        )
        card = make_card("http://gw-b").model_copy(
            update={"transport": "http", "owner_gateway": "gw-b"}
        )
        self.assertTrue(transport.probe(card))
        down = self.build(
            lambda request: httpx.Response(500),
            [Peer(name="gw-b", base_url="http://gw-b")],
        )
        self.assertFalse(down.probe(card))


if __name__ == "__main__":
    unittest.main()
