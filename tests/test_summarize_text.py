import unittest
from unittest.mock import patch

from agent_gateway.services.summarize import text as summarize


class SummarizeTextTest(unittest.TestCase):
    def test_empty_text_is_rejected(self):
        self.assertEqual(
            summarize.summarize_text(" "), {"ok": False, "error": "text is required"}
        )

    def test_returns_trimmed_summary(self):
        with patch.object(summarize.llm_client, "chat", return_value="  要点  "):
            out = summarize.summarize_text("正文")
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"], "要点")
        self.assertEqual(out["chars"], 2)

    def test_unconfigured_llm(self):
        with patch.object(summarize.llm_client, "chat", return_value=None):
            out = summarize.summarize_text("正文")
        self.assertEqual(out, {"ok": False, "error": "llm_unavailable"})

    def test_long_input_is_truncated(self):
        seen = {}

        def fake(prompt, **kwargs):
            seen["len"] = len(prompt) - len("Summarize the following text:\n\n")
            return "ok"

        with patch.object(summarize.llm_client, "chat", side_effect=fake):
            out = summarize.summarize_text("x" * (summarize.MAX_INPUT_CHARS + 10))
        self.assertTrue(out["input_truncated"])
        self.assertEqual(seen["len"], summarize.MAX_INPUT_CHARS)

    def test_instruction_is_passed_as_system(self):
        captured = {}

        def fake(prompt, **kwargs):
            captured.update(kwargs)
            return "ok"

        with patch.object(summarize.llm_client, "chat", side_effect=fake):
            summarize.summarize_text("正文", instruction="用三句话")
        self.assertTrue(captured["system"].startswith("用三句话"))
        self.assertIn(summarize.OUTPUT_RULES, captured["system"])


if __name__ == "__main__":
    unittest.main()
