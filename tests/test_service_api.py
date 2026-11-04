"""Public interfaces for the host-bound business modules."""

from __future__ import annotations

import unittest

from agent_gateway.services.knowledge import api as knowledge_api
from agent_gateway.services.summarize import api as summarize_api


class ServiceApiTest(unittest.TestCase):
    def test_knowledge_refill_is_callable(self):
        self.assertTrue(callable(knowledge_api.refill))

    def test_summarize_run_is_callable(self):
        self.assertTrue(callable(summarize_api.run))


if __name__ == "__main__":
    unittest.main()
