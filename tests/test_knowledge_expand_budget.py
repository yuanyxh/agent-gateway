"""knowledge_expand completion budget: scaling, bounds and wiring."""

from __future__ import annotations

import unittest
from unittest import mock

from agent_gateway.services.knowledge import knowledge_engine as ke


class ExpandBudgetTest(unittest.TestCase):
    def test_empty_prompt_uses_floor(self):
        self.assertEqual(ke._expand_max_tokens(""), 2048)

    def test_scales_with_prompt_length(self):
        self.assertEqual(ke._expand_max_tokens("x" * 1732), 2590)

    def test_caps_at_ceiling(self):
        self.assertEqual(ke._expand_max_tokens("x" * 6000), 4096)

    def test_truncated_flag_survives_unrepairable_payload(self):
        error = ke.llm_client.LLMClientError(
            "truncated",
            kind="truncated",
            finish_reason="length",
            content="plain text",
        )
        with mock.patch.object(ke.llm_client, "chat", side_effect=error):
            with self.assertLogs(ke.logger, level="ERROR") as logs:
                result = ke._llm_json("p" * 100, max_tokens=2048, feature="expand")
        self.assertIsNone(result)
        self.assertIn("truncated=True", "\n".join(logs.output))

    def test_advance_passes_scaled_budget(self):
        with mock.patch.object(ke, "_llm_json", return_value=None) as call:
            ke.advance(
                "Java 后端",
                "1. 运行时数据区",
                scope="",
                exclude="",
                disliked="",
            )
        budget = call.call_args.kwargs["max_tokens"]
        self.assertGreaterEqual(budget, ke.EXPAND_MIN_TOKENS)
        self.assertLessEqual(budget, ke.EXPAND_MAX_TOKENS)


if __name__ == "__main__":
    unittest.main()
