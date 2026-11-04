"""Tests for repeated tool-result deduplication and its reference note."""

from __future__ import annotations

import unittest

from agent_gateway.services.proxy.proxy_tool_output_dedup import dedup_tool_results


def _anthropic_round(tool_id: str, name: str, text: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text}
            ],
        },
    ]


class AnthropicDedupTest(unittest.TestCase):
    def test_duplicate_points_at_first_tool_result_id(self):
        messages = _anthropic_round("toolu_a", "write", "same body") + _anthropic_round(
            "toolu_b", "write", "same body"
        )
        replaced = dedup_tool_results({"messages": messages}, "anthropic", "zh-Hans")
        self.assertEqual(replaced, 1)
        first = messages[1]["content"][0]
        second = messages[3]["content"][0]
        self.assertEqual(first["content"], "same body")
        self.assertEqual(second["tool_use_id"], "toolu_b")
        self.assertEqual(
            second["content"], "本次运行结果与上次结果相同（工具结果 toolu_a）"
        )

    def test_distinct_text_is_untouched(self):
        messages = _anthropic_round("toolu_a", "write", "one") + _anthropic_round(
            "toolu_b", "write", "two"
        )
        replaced = dedup_tool_results({"messages": messages}, "anthropic", "zh-Hans")
        self.assertEqual(replaced, 0)
        self.assertEqual(messages[3]["content"][0]["content"], "two")

    def test_missing_first_id_renders_placeholder(self):
        messages = _anthropic_round("", "write", "same body") + _anthropic_round(
            "toolu_b", "write", "same body"
        )
        dedup_tool_results({"messages": messages}, "anthropic", "en")
        self.assertIn("(tool result ?)", messages[3]["content"][0]["content"])


class OpenAIDedupTest(unittest.TestCase):
    def test_duplicate_points_at_first_tool_call_id(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "call_a", "function": {}}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "same body"},
            {"role": "assistant", "tool_calls": [{"id": "call_b", "function": {}}]},
            {"role": "tool", "tool_call_id": "call_b", "content": "same body"},
        ]
        replaced = dedup_tool_results({"messages": messages}, "openai", "en")
        self.assertEqual(replaced, 1)
        self.assertEqual(messages[1]["content"], "same body")
        self.assertEqual(messages[3]["tool_call_id"], "call_b")
        self.assertEqual(
            messages[3]["content"],
            "This result is the same as the previous one (tool result call_a)",
        )


if __name__ == "__main__":
    unittest.main()
