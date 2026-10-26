import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.llm import client


class ParseJsonTest(unittest.TestCase):
    def test_fenced_with_chatter(self):
        raw = 'Here you go:\n```json\n{"accept": true, "reason": ""}\n```\nDone.'
        self.assertEqual(client.parse_json(raw), {"accept": True, "reason": ""})

    def test_trailing_comma(self):
        self.assertEqual(client.parse_json('{"a": 1,}'), {"a": 1})

    def test_unescaped_inline_quotes(self):
        raw = '{"card": "他说 "不要" 这么做"}'
        self.assertEqual(client.parse_json(raw)["card"], '他说 "不要" 这么做')

    def test_complete_output_is_not_flagged_as_repaired(self):
        parsed, repaired = client.parse_json_partial('{"concept": "拥塞控制"}')
        self.assertEqual(parsed, {"concept": "拥塞控制"})
        self.assertFalse(repaired)

    def test_truncated_value_is_recovered_and_flagged(self):
        raw = '{"accept": true, "reason": "IP组播是TCP/IP协议栈中IP层的一对多通信机制'
        parsed, repaired = client.parse_json_partial(raw)
        self.assertTrue(repaired)
        self.assertIs(parsed["accept"], True)
        self.assertIn("IP组播", parsed["reason"])
        self.assertEqual(client.parse_json(raw), parsed)

    def test_truncated_card_body_is_flagged(self):
        raw = '{"card": "# DPDK:当内核的包处理到了天花板\\n\\n**困境从一句断言开始。**'
        parsed, repaired = client.parse_json_partial(raw)
        self.assertTrue(repaired)
        self.assertIn("DPDK", parsed["card"])

    def test_dangling_key_falls_back_to_flat_fields(self):
        raw = '{"concept": "拥塞控制", "why_now":'
        parsed, repaired = client.parse_json_partial(raw)
        self.assertTrue(repaired)
        self.assertEqual(parsed["concept"], "拥塞控制")
        self.assertNotIn("why_now", parsed)

    def test_unparseable_returns_none(self):
        self.assertEqual(client.parse_json_partial("not json at all"), (None, False))
        self.assertIsNone(client.parse_json(""))


if __name__ == "__main__":
    unittest.main()
