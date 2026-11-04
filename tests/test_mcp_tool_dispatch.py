import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import asyncio
import unittest

from mcp.server import MCPServer
from mcp.types import TextContent

from agent_gateway.services.mcp.server_tools import register_tools

MINIMAL_ARGS = {
    "remember": {"content": "fact"},
    "remember_batch": {"memories": [{"content": "fact"}]},
    "recall": {"keywords": ["k"]},
    "catch_up": {},
    "forget": {},
    "set_memory_processed": {"memory_ids": ["m1"]},
    "memory_get": {"node_id": "n1"},
    "memory_graph": {"node_id": "n1"},
    "project_lookup": {"path": "/tmp"},
    "project_impact": {"path": "/tmp", "target": "sym"},
    "project_notes": {"query": "q", "path": "/tmp"},
    "tool_resource": {"parentName": "write_plan", "path": "references/x.md"},
    "websearch_engine": {"query": "q"},
    "read_url": {"url": "https://example.com"},
}


class ToolDispatchTest(unittest.TestCase):
    def _server(self):
        mcp = MCPServer("probe")
        seen = []

        async def fake(arguments):
            seen.append(arguments)
            return [TextContent(type="text", text="ok")]

        register_tools(mcp, {name: fake for name in MINIMAL_ARGS})
        return mcp, seen

    def test_every_tool_forwards_arguments(self):
        for name, args in MINIMAL_ARGS.items():
            with self.subTest(tool=name):
                mcp, seen = self._server()
                result = asyncio.run(mcp.call_tool(name, dict(args)))
                self.assertFalse(result.is_error)
                self.assertEqual(result.content[0].text, "ok")
                self.assertEqual(len(seen), 1)

    def test_remember_forwards_content(self):
        mcp, seen = self._server()
        asyncio.run(mcp.call_tool("remember", {"content": "fact"}))
        self.assertEqual(seen[0]["content"], "fact")

    def test_optional_arguments_are_omitted_when_unset(self):
        mcp, seen = self._server()
        asyncio.run(mcp.call_tool("project_lookup", {"path": "/tmp"}))
        self.assertEqual(set(seen[0]), {"path", "limit"})


if __name__ == "__main__":
    unittest.main()
