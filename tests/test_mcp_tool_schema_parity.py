import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import asyncio
import unittest

from agent_gateway.services.mcp import server
from agent_gateway.services.mcp.server_tool_schema import build_tools


def _unwrap_optional(schema):
    """MCPServer renders `X | None` as anyOf; compare against the concrete branch."""
    if isinstance(schema, dict) and schema.get("anyOf"):
        concrete = [item for item in schema["anyOf"] if item.get("type") != "null"]
        if len(concrete) == 1:
            merged = dict(concrete[0])
            if "description" in schema:
                merged["description"] = schema["description"]
            return merged
    return schema


def _old_schemas():
    return {
        tool.name: tool.model_dump(by_alias=True, exclude_none=True)["inputSchema"]
        for tool in build_tools()
    }


def _new_schemas():
    return {
        tool.name: tool.input_schema for tool in asyncio.run(server.mcp.list_tools())
    }


class ToolSchemaParityTest(unittest.TestCase):
    def test_same_tool_names(self):
        self.assertEqual(set(_old_schemas()), set(_new_schemas()))

    def test_properties_and_required_match(self):
        old, new = _old_schemas(), _new_schemas()
        for name in sorted(old):
            with self.subTest(tool=name):
                self.assertEqual(
                    set(old[name].get("properties", {})),
                    set(new[name].get("properties", {})),
                )
                self.assertEqual(
                    set(old[name].get("required", [])),
                    set(new[name].get("required", [])),
                )

    def test_property_shape_matches(self):
        old, new = _old_schemas(), _new_schemas()
        for name in sorted(old):
            for key, expected in old[name].get("properties", {}).items():
                with self.subTest(tool=name, prop=key):
                    actual = _unwrap_optional(new[name]["properties"][key])
                    expected = _unwrap_optional(expected)
                    for field in ("type", "enum", "description"):
                        self.assertEqual(expected.get(field), actual.get(field))
                    self.assertEqual(
                        _unwrap_optional(expected.get("items", {})),
                        _unwrap_optional(actual.get("items", {})),
                    )
                    if "default" in expected:
                        self.assertEqual(expected["default"], actual.get("default"))


if __name__ == "__main__":
    unittest.main()
