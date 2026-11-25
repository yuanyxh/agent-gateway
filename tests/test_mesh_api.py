"""Tests for the mesh HTTP surface."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mesh import settings  # noqa: E402
from agent_gateway.services.mesh.api import (  # noqa: E402
    build_mesh_routes,
    remote_token_middleware,
)
from agent_gateway.services.mesh.models import ServiceCard  # noqa: E402
from agent_gateway.services.mesh.registry import FileRegistry  # noqa: E402
from agent_gateway.services.mesh.router import Router  # noqa: E402
from agent_gateway.services.mesh.transport import DirectoryTransport, Peer  # noqa: E402

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CARD = {
    "id": "ws-b",
    "name": "utility",
    "kind": "workspace",
    "endpoint": "ws-b",
}


class MeshApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FileRegistry(self.root / "mesh", "gw-test")
        self.inbox = self.root / "inbox"
        self.router = Router(
            self.registry, DirectoryTransport(self.inbox), gateway_id="gw-test"
        )
        routes = build_mesh_routes(self.router, TOKEN, "gw-test", "http://gw-test")
        app = Starlette(routes=routes)
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.remote = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def test_register_and_list(self):
        response = self.client.post("/mesh/register", json=CARD, headers=AUTH)
        self.assertEqual(response.status_code, 200)
        listed = self.client.get("/mesh/services", headers=AUTH).json()
        self.assertEqual([card["id"] for card in listed["services"]], ["ws-b"])

    def test_services_remote_flag_stops_peer_recursion(self):
        calls = []

        class StubRemote:
            peers = [Peer(name="gw-b", base_url="http://gw-b")]

            def list_peer(self, peer):
                calls.append(peer.name)
                return []

        router = Router(
            self.registry,
            DirectoryTransport(self.inbox),
            remote=StubRemote(),
            gateway_id="gw-test",
        )
        client = TestClient(
            Starlette(
                routes=build_mesh_routes(router, TOKEN, "gw-test", "http://gw-test")
            ),
            client=("127.0.0.1", 50000),
        )
        client.get("/mesh/services?remote=0", headers=AUTH)
        self.assertEqual(calls, [])
        client.get("/mesh/services", headers=AUTH)
        self.assertEqual(calls, ["gw-b"])

    def test_requires_token(self):
        self.assertEqual(self.remote.get("/mesh/services").status_code, 401)
        self.assertEqual(self.remote.post("/mesh/register", json=CARD).status_code, 401)

    def test_remote_cannot_manage_registry(self):
        self.assertEqual(
            self.remote.post("/mesh/register", json=CARD, headers=AUTH).status_code, 401
        )
        self.assertEqual(
            self.remote.post(
                "/mesh/deregister", json={"service_id": "ws-b"}, headers=AUTH
            ).status_code,
            401,
        )

    def test_heartbeat_rejects_unknown_status(self):
        self.client.post("/mesh/register", json=CARD, headers=AUTH)
        response = self.client.post(
            "/mesh/heartbeat",
            json={"service_id": "ws-b", "status": "bogus"},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 400)
        card = self.registry.get("ws-b", include_expired=True)
        self.assertEqual(card.status, "ready")

    def test_invalid_card(self):
        response = self.client.post(
            "/mesh/register",
            json={"id": "", "name": "x", "endpoint": "y"},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_card")

    def test_send_local(self):
        self.client.post("/mesh/register", json=CARD, headers=AUTH)
        message = {
            "sender": "ws-a",
            "recipient": "ws-b",
            "kind": "task",
            "payload": {"t": 1},
        }
        response = self.client.post(
            "/mesh/send", json={"message": message}, headers=AUTH
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(len(list((self.inbox / "ws-b").glob("*.json"))), 1)

    def test_send_unknown_returns_404(self):
        message = {"sender": "ws-a", "recipient": "ws-missing"}
        response = self.client.post(
            "/mesh/send", json={"message": message}, headers=AUTH
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error_code"], "target_unknown")

    def test_send_offline_returns_status_code_and_startable(self):
        stale = ServiceCard(**CARD).model_copy(
            update={
                "heartbeat_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                "startable": True,
            }
        )
        self.registry.register(stale)
        message = {"sender": "ws-a", "recipient": "ws-b"}
        response = self.client.post(
            "/mesh/send", json={"message": message}, headers=AUTH
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "target_offline")
        self.assertTrue(response.json()["startable"])

    def test_oversized_payload_returns_413(self):
        self.client.post("/mesh/register", json=CARD, headers=AUTH)
        message = {"sender": "ws-a", "recipient": "ws-b", "payload": {"t": "x" * 70000}}
        response = self.client.post(
            "/mesh/send", json={"message": message}, headers=AUTH
        )
        self.assertEqual(response.status_code, 413)

    def test_heartbeat_marks_ready(self):
        self.client.post("/mesh/register", json=CARD, headers=AUTH)
        response = self.client.post(
            "/mesh/heartbeat",
            json={"service_id": "ws-b", "status": "ready"},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200)
        card = self.registry.get("ws-b")
        self.assertEqual(card.status, "ready")

    def test_unknown_service_card_returns_404(self):
        self.assertEqual(
            self.client.get("/mesh/services/nope", headers=AUTH).status_code, 404
        )

    def test_agent_card(self):
        body = self.client.get("/.well-known/agent-card.json", headers=AUTH).json()
        self.assertEqual(body["kind"], "gateway")
        self.assertEqual(body["id"], "gw-test")

    def test_health(self):
        body = self.client.get("/mesh/health", headers=AUTH).json()
        self.assertEqual(body["gateway"], "gw-test")


class RemoteTokenMiddlewareTests(unittest.TestCase):
    def build(self, token, client=None):
        async def ok(request):
            return JSONResponse({"ok": True})

        app = remote_token_middleware(
            Starlette(routes=[Route("/probe", ok, methods=["GET"])]), token
        )
        kwargs = {"client": client} if client else {}
        return TestClient(app, **kwargs)

    def test_remote_requires_token(self):
        client = self.build("secret")
        self.assertEqual(client.get("/probe").status_code, 401)
        self.assertEqual(
            client.get(
                "/probe", headers={"Authorization": "Bearer secret"}
            ).status_code,
            200,
        )

    def test_local_is_exempt(self):
        client = self.build("", ("127.0.0.1", 50000))
        self.assertEqual(client.get("/probe").status_code, 200)


class MeshSettingsTests(unittest.TestCase):
    def test_disabled_when_mcp_disabled(self):
        self.assertIsNone(settings.load(False))

    def test_disabled_by_yaml_flag(self):
        with mock.patch.object(settings.config_loader, "value", return_value=False):
            self.assertIsNone(settings.load(True))

    def test_defaults(self):
        with mock.patch.object(settings.config_loader, "value", return_value=None):
            loaded = settings.load(True)
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.enabled)
        self.assertTrue(loaded.gateway_id.startswith("gw-"))
        self.assertEqual(loaded.peers, [])
        self.assertEqual(loaded.heartbeat_ttl, 120)

    def test_parses_peers(self):
        def fake(key, default=None):
            if key == "server.mesh.peers":
                return [{"name": "gw-b", "baseUrl": "http://gw-b", "token": "t"}]
            return default

        with mock.patch.object(settings.config_loader, "value", side_effect=fake):
            loaded = settings.load(True)
        self.assertEqual(loaded.peers[0].name, "gw-b")
        self.assertEqual(loaded.peers[0].token, "t")


class ReverseRegistrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FileRegistry(self.root / "mesh", "gw-test")
        self.router = Router(
            self.registry, DirectoryTransport(self.root / "inbox"), gateway_id="gw-test"
        )
        routes = build_mesh_routes(self.router, TOKEN, "gw-test", "http://gw-test")
        app = Starlette(routes=routes)
        self.local = TestClient(app, client=("127.0.0.1", 50000))
        self.remote = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def sender_card(self) -> dict:
        return {
            "id": "ws-a",
            "name": "a",
            "kind": "workspace",
            "endpoint": "ws-a",
            "owner_gateway": "gw-a",
        }

    def test_peer_sender_is_registered(self):
        self.local.post("/mesh/register", json=CARD, headers=AUTH)
        message = {"sender": "ws-a", "recipient": "ws-b"}
        response = self.remote.post(
            "/mesh/send",
            json={"message": message, "sender_card": self.sender_card()},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.registry.get("ws-a", include_expired=True))

    def test_missing_sender_card_does_not_register(self):
        self.local.post("/mesh/register", json=CARD, headers=AUTH)
        message = {"sender": "ws-a", "recipient": "ws-b"}
        self.remote.post("/mesh/send", json={"message": message}, headers=AUTH)
        self.assertIsNone(self.registry.get("ws-a", include_expired=True))

    def test_local_sender_is_not_absorbed(self):
        self.local.post("/mesh/register", json=CARD, headers=AUTH)
        message = {"sender": "ws-a", "recipient": "ws-b"}
        with mock.patch("agent_gateway.services.mesh.api._is_local", return_value=True):
            self.remote.post(
                "/mesh/send",
                json={"message": message, "sender_card": self.sender_card()},
                headers=AUTH,
            )
        self.assertIsNone(self.registry.get("ws-a", include_expired=True))


if __name__ == "__main__":
    unittest.main()
