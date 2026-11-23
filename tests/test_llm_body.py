"""Thinking-disable carried over each API protocol."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.llm import client


def _target(endpoint, api_format=client.API_OPENAI):
    return client.LLMTarget(
        endpoint=endpoint, model="m", api_key="k", api_format=api_format
    )


class ThinkingBodyTest(unittest.TestCase):
    def _body(self, target, disable_thinking=True):
        return client._body(
            "hi",
            target,
            system=None,
            temperature=None,
            max_tokens=128,
            disable_thinking=disable_thinking,
            extra_body=None,
        )

    def test_every_openai_endpoint_sends_both_disable_forms(self):
        for endpoint in (
            "https://api.xiaomimimo.com/v1/chat/completions",
            "https://api.example.com/v1/chat/completions",
        ):
            with self.subTest(endpoint=endpoint):
                body = self._body(_target(endpoint))
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertEqual(body["reasoning_effort"], "none")
                self.assertNotIn("enable_thinking", body)

    def test_openai_keeps_thinking_when_not_disabled(self):
        body = self._body(
            _target("https://api.example.com/v1/chat/completions"),
            disable_thinking=False,
        )
        self.assertNotIn("thinking", body)
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("enable_thinking", body)

    def test_anthropic_keeps_thinking_block(self):
        body = self._body(
            _target("https://api.anthropic.com/v1/messages", client.API_ANTHROPIC)
        )
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("enable_thinking", body)


if __name__ == "__main__":
    unittest.main()
