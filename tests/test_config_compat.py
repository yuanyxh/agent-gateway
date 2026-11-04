"""Legacy host config -> gateway-native schema translation."""

from __future__ import annotations

import unittest

from agent_gateway.core import config_compat


class DetectTest(unittest.TestCase):
    def test_native_shape(self):
        self.assertEqual(config_compat.detect({"llm": {}, "server": {}}), "native")

    def test_legacy_shape(self):
        self.assertEqual(config_compat.detect({"ai": {}, "mcp": {}}), "legacy")

    def test_empty_is_native(self):
        self.assertEqual(config_compat.detect({}), "native")

    def test_nested_legacy_knowledge_is_detected(self):
        self.assertEqual(
            config_compat.detect({"knowledge": {"poolTargetTopic": 3}}),
            "legacy",
        )


class ToNativeTest(unittest.TestCase):
    def test_maps_llm_and_logging(self):
        native = config_compat.to_native(
            {
                "ai": {"apiKey": "k", "baseURL": "u", "model": "m", "llmTimeout": 45},
                "general": {"language": "en", "logMaxSize": 10, "logBackupCount": 2},
            }
        )
        self.assertEqual(native["llm"]["apiKey"], "k")
        self.assertEqual(native["llm"]["timeout"], 45)
        self.assertEqual(native["language"], "en")
        self.assertEqual(native["logging"]["maxSize"], 10)

    def test_maps_notes_and_memory(self):
        native = config_compat.to_native(
            {
                "mcp": {
                    "knowledgeBasePath": "/notes",
                    "noteExcludeDirs": ["Private"],
                    "excludeTags": ["tmp"],
                    "memoryProtectImportance": 8,
                    "rankSignalRecency": 0.2,
                    "qdrantPath": "/q",
                    "embedderModel": "m",
                }
            }
        )
        self.assertEqual(native["notes"]["dir"], "/notes")
        self.assertEqual(native["notes"]["excludeDirs"], ["Private"])
        self.assertEqual(native["notes"]["excludeTags"], ["tmp"])
        self.assertEqual(native["memory"]["protectImportance"], 8)
        self.assertEqual(native["memory"]["rankSignal"]["recency"], 0.2)
        self.assertEqual(native["memory"]["qdrantPath"], "/q")
        self.assertEqual(native["memory"]["embedder"]["model"], "m")

    def test_renames_skill_dedup(self):
        native = config_compat.to_native(
            {"modelProxy": {"enabled": True, "skillDedupEnabled": False}}
        )
        proxy = native["server"]["proxy"]
        self.assertIs(proxy["toolOutputDedup"], False)
        self.assertNotIn("skillDedupEnabled", proxy)

    def test_maps_knowledge(self):
        native = config_compat.to_native(
            {
                "knowledge": {
                    "enabled": True,
                    "poolTargetTopic": 7,
                    "maxFailStreak": 2,
                    "retentionDays": 30,
                }
            }
        )
        self.assertIs(native["knowledge"]["enabled"], True)
        self.assertEqual(native["knowledge"]["topicTarget"], 7)
        self.assertEqual(native["knowledge"]["maxFailStreak"], 2)
        self.assertEqual(native["knowledge"]["retentionDays"], 30)


if __name__ == "__main__":
    unittest.main()
