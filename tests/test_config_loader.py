import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import config, config_loader


class ConfigLoaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved_root = os.environ.pop("UTILITY_DATA_DIR", None)
        config_loader.reset_cache()

    def tearDown(self):
        config_loader.reset_cache()
        if self._saved_root is not None:
            os.environ["UTILITY_DATA_DIR"] = self._saved_root
        self._tmp.cleanup()

    def _write(self, text):
        (self.root / "config.yaml").write_text(text, encoding="utf-8")

    def test_missing_file_is_empty(self):
        self.assertEqual(config_loader.load(self.root / "config.yaml"), {})

    def test_nested_value_and_missing_level(self):
        self._write("ai:\n  apiKey: secret\n  model: m1\n")
        data = config_loader.load(self.root / "config.yaml")
        self.assertEqual(data["llm"]["apiKey"], "secret")
        self.assertEqual(config_loader.value("llm.model"), None)

    def test_dotted_value_reads_loaded_file(self):
        self._write("general:\n  language: en\n")
        with mock.patch.dict(os.environ, {"UTILITY_DATA_DIR": str(self.root)}):
            config_loader.reset_cache()
            self.assertEqual(config_loader.value("language"), "en")

    def test_invalid_yaml_falls_back_to_empty(self):
        self._write("ai: [unclosed\n")
        self.assertEqual(config_loader.load(self.root / "config.yaml"), {})

    def test_non_mapping_document_is_empty(self):
        self._write("- a\n- b\n")
        self.assertEqual(config_loader.load(self.root / "config.yaml"), {})

    def test_cache_invalidates_when_file_changes(self):
        self._write("ai:\n  model: first\n")
        path = self.root / "config.yaml"
        self.assertEqual(config_loader.load(path)["llm"]["model"], "first")
        self._write("ai:\n  model: second-longer-name\n")
        self.assertEqual(config_loader.load(path)["llm"]["model"], "second-longer-name")


class ConfigFallbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        config_loader.reset_cache()
        self._env = mock.patch.dict(
            os.environ, {"UTILITY_DATA_DIR": str(self.root)}, clear=False
        )
        self._env.start()
        for key in config._YAML_FALLBACKS:
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        config_loader.reset_cache()
        self._tmp.cleanup()

    def _write(self, text):
        (self.root / "config.yaml").write_text(text, encoding="utf-8")
        config_loader.reset_cache()

    def test_neuron_review_concurrency_reads_yaml(self):
        self._write("memory:\n  neuron:\n    reviewConcurrency: 6\n")
        self.assertEqual(config.memory_neuron_review_concurrency(), 6)

    def test_env_wins_over_yaml(self):
        self._write("ai:\n  apiKey: from-yaml\n")
        os.environ["AI_API_KEY"] = "from-env"
        try:
            self.assertEqual(config.ai_api_key(), "from-env")
        finally:
            os.environ.pop("AI_API_KEY")

    def test_text_fallback(self):
        self._write("ai:\n  apiKey: from-yaml\n  model: m1\n")
        self.assertEqual(config.ai_api_key(), "from-yaml")
        self.assertEqual(config.summary_model(), "m1")

    def test_numeric_fallback_keeps_convention(self):
        self._write("ai:\n  llmTimeout: 45\n")
        self.assertEqual(config.llm_timeout(), 45.0)
        self._write("ai:\n  llmTimeout: 0\n")
        self.assertEqual(config.llm_timeout(), 120.0)

    def test_json_fallback_matches_env_shape(self):
        self._write("knowledge:\n  topics:\n    - name: t1\n      kind: principle\n")
        topics = config.knowledge_topics()
        self.assertEqual(topics, [{"name": "t1", "kind": "principle"}])

    def test_csv_fallback(self):
        self._write("mcp:\n  excludeTags:\n    - a\n    - b\n")
        self.assertEqual(config.summary_exclude_tags(), {"a", "b"})

    def test_null_key_uses_default(self):
        self._write("mcp:\n  excludeTags:\n")
        self.assertEqual(config.summary_exclude_tags(), set())

    def test_notes_dir_derives_from_config_root(self):
        self._write("mcp:\n  knowledgeBasePath: ''\n")
        self.assertEqual(config.notes_dir(), str(self.root / "notes"))

    def test_notes_dir_honours_configured_path(self):
        self._write("mcp:\n  knowledgeBasePath: /tmp/some-notes\n")
        self.assertEqual(config.notes_dir(), "/tmp/some-notes")

    def test_unknown_key_uses_default(self):
        self._write("ai:\n  model: m1\n")
        self.assertEqual(config.get("NOT_INJECTED_ENV", "x"), "x")

    def test_zero_protect_threshold_disables_it(self):
        # 0 must mean "off": every importance is >= 0, so returning 0 would make
        # the weekly pass treat every memory as protected.
        self._write("mcp:\n  memoryProtectImportance: 0\n")
        self.assertIsNone(config.memory_protect_importance())

    def test_protect_threshold_is_read_from_config(self):
        self._write("mcp:\n  memoryProtectImportance: 8\n")
        self.assertEqual(config.memory_protect_importance(), 8)

    def test_zero_summary_budget_disables_folding(self):
        self._write(
            "mcp:\n  memoryMaxSummaries: 0\n  memoryMaxSummariesPerEpisode: 0\n"
        )
        self.assertIsNone(config.memory_max_summaries())
        self.assertIsNone(config.memory_max_summaries_per_episode())

    def test_rank_signals_default_to_zero(self):
        self._write("mcp:\n  rankSignalRecency: 0.2\n")
        weights = config.rank_signal_weights()

        self.assertEqual(weights["recency"], 0.2)
        self.assertEqual(weights["access"], 0.0)


if __name__ == "__main__":
    unittest.main()
