import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import asyncio
import unittest
from unittest.mock import patch

from mcp.server import MCPServer

from agent_gateway.services.mcp import server_skills

SKILLS = {
    "alpha": {"name": "alpha", "description": "A", "content": "alpha body"},
    "beta": {"name": "beta", "description": "B", "content": "beta body"},
}


class SkillRegistrationTest(unittest.TestCase):
    def _registered(self):
        mcp = MCPServer("probe")
        with patch.object(server_skills, "scan_skills", return_value=SKILLS):
            server_skills.register_skill_tools(mcp)
        return mcp

    def test_each_skill_returns_its_own_body(self):
        mcp = self._registered()
        with patch.object(server_skills, "scan_skills", return_value=SKILLS):
            alpha = asyncio.run(mcp.call_tool("alpha", {}))
            beta = asyncio.run(mcp.call_tool("beta", {}))
        self.assertEqual(alpha.content[0].text, "alpha body")
        self.assertEqual(beta.content[0].text, "beta body")

    def test_skill_result_has_no_structured_content(self):
        mcp = self._registered()
        with patch.object(server_skills, "scan_skills", return_value=SKILLS):
            alpha = asyncio.run(mcp.call_tool("alpha", {}))
        self.assertIsNone(alpha.structured_content)

    def test_skill_body_is_read_per_call(self):
        mcp = self._registered()
        changed = {"alpha": {**SKILLS["alpha"], "content": "alpha updated"}}
        with patch.object(server_skills, "scan_skills", return_value=changed):
            alpha = asyncio.run(mcp.call_tool("alpha", {}))
        self.assertEqual(alpha.content[0].text, "alpha updated")


if __name__ == "__main__":
    unittest.main()
