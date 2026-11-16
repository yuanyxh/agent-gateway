import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import asyncio
import unittest

from agent_gateway.services.mcp import server


class RssSamplerTest(unittest.TestCase):
    def test_peak_reported_in_mib(self):
        self.assertGreater(server.rss_peak_mb(), 1.0)

    def test_zero_interval_disables_sampling(self):
        os.environ["MCP_RSS_SAMPLE_SECONDS"] = "0"
        try:
            self.assertIsNone(asyncio.run(server.rss_sampler()))
        finally:
            os.environ.pop("MCP_RSS_SAMPLE_SECONDS", None)


if __name__ == "__main__":
    unittest.main()
