"""Tests for the mesh MCP tools."""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mesh import state, tools  # noqa: E402
from agent_gateway.services.mesh.models import ServiceCard  # noqa: E402
from agent_gateway.services.mesh.registry import FileRegistry  # noqa: E402
from agent_gateway.services.mesh.router import Router  # noqa: E402
from agent_gateway.services.mesh.transport import DirectoryTransport  # noqa: E402


class FakeMCP:
    def __init__(self):
        self.registered = {}

    def tool(self, **kwargs):
        def decorate(function):
            self.registered[kwargs["name"]] = function
            return function

        return decorate


class StubRemote:
    def __init__(self, cards):
        self.peers = []
        self.cards = {card.id: card for card in cards}
        self.delivered = []

    def find(self, service_id):
        card = self.cards.get(service_id)
        return (None, card) if card else None

    def deliver(self, card, message):
        self.delivered.append((card.id, message.id))


class MeshToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = FileRegistry(self.root / "mesh", "gw-test")
        self.router = Router(
            self.registry, DirectoryTransport(self.root / "inbox"), gateway_id="gw-test"
        )

    def tearDown(self):
        state.set_router(None)
        self._tmp.cleanup()

    def make_stale(self, service_id: str = "ws-b", startable: bool = False) -> None:
        card = ServiceCard(
            id=service_id,
            name=service_id,
            kind="workspace",
            endpoint=service_id,
            startable=startable,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=300),
        )
        self.registry.register(card)

    def test_register_and_list(self):
        self.assertTrue(tools.register(self.router, "ws-1", kind="workspace")["ok"])
        listed = tools.services(self.router)
        self.assertEqual([card["id"] for card in listed["services"]], ["ws-1"])

    def test_register_resolves_relative_endpoint(self):
        tools.register(self.router, "ws-1")
        endpoint = self.registry.get("ws-1").endpoint
        self.assertTrue(endpoint.endswith("/inbox/ws-1"))

    def test_send_and_inbox(self):
        tools.register(self.router, "ws-b")
        tools.register(self.router, "ws-a")
        result = tools.send(self.router, "ws-a", "ws-b", "task", {"text": "hi"})
        self.assertTrue(result["ok"])
        read = tools.inbox(self.router, "ws-b")
        self.assertEqual(read["messages"][0]["payload"], {"text": "hi"})

    def test_inbox_unknown(self):
        result = tools.inbox(self.router, "nope")
        self.assertEqual(result["error_code"], "target_unknown")

    def test_start_writes_control_message(self):
        self.make_stale(startable=True)
        result = tools.start(self.router, "ws-b")
        self.assertEqual(result["status"], "starting")
        messages = tools.inbox(self.router, "ws-b")["messages"]
        self.assertEqual(messages[0]["kind"], "control")
        self.assertEqual(messages[0]["payload"], {"action": "start"})

    def test_start_forwards_control_to_remote_card(self):
        card = ServiceCard(
            id="ws-r",
            name="ws-r",
            kind="workspace",
            transport="http",
            endpoint="http://gw-remote",
            startable=True,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=300),
        )
        remote = StubRemote([card])
        router = Router(
            self.registry,
            DirectoryTransport(self.root / "inbox"),
            remote=remote,
            gateway_id="gw-test",
        )
        result = tools.start(router, "ws-r")
        self.assertEqual(result["status"], "starting")
        self.assertEqual(remote.delivered[0][0], "ws-r")

    def test_start_rejects_non_startable(self):
        self.make_stale(startable=False)
        result = tools.start(self.router, "ws-b")
        self.assertEqual(result["error_code"], "target_offline")

    def test_start_already_running(self):
        tools.register(self.router, "ws-b")
        self.assertEqual(tools.start(self.router, "ws-b")["status"], "already_running")

    def test_start_alive_but_offline_is_delivered(self):
        self.registry.register(
            ServiceCard(
                id="ws-b",
                name="ws-b",
                kind="workspace",
                endpoint="ws-b",
                startable=True,
            )
        )
        self.registry.touch("ws-b", status="offline")
        result = tools.start(self.router, "ws-b")
        self.assertEqual(result["status"], "starting")
        messages = tools.inbox(self.router, "ws-b")["messages"]
        self.assertEqual(messages[0]["payload"], {"action": "start"})

    def test_heartbeat_and_probe(self):
        tools.register(self.router, "ws-b")
        self.assertTrue(tools.heartbeat(self.router, "ws-b", "busy")["ok"])
        self.assertEqual(tools.probe(self.router, "ws-b")["status"], "ready")

    def test_tools_register_and_report_disabled(self):
        mcp = FakeMCP()
        tools.register_mesh_tools(mcp)
        self.assertEqual(
            set(mcp.registered),
            {
                "service_list",
                "service_send",
                "service_inbox",
                "service_start",
                "service_heartbeat",
                "service_probe",
                "service_register",
                "service_deregister",
            },
        )
        tools.register(self.router, "ws-1")
        state.set_router(self.router)
        output = asyncio.run(mcp.registered["service_list"]())
        self.assertIn("ws-1", output)

    def test_tool_disabled_without_router(self):
        mcp = FakeMCP()
        tools.register_mesh_tools(mcp)
        state.set_router(None)
        output = asyncio.run(mcp.registered["service_list"]())
        self.assertIn("mesh_disabled", output)


if __name__ == "__main__":
    unittest.main()
