"""Tests for the annotation export/import helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export_mod = _load("export_annotation_set", "scripts/export_annotation_set.py")
import_mod = _load("import_annotations", "scripts/import_annotations.py")
calibrate_mod = _load("calibrate_thresholds", "scripts/calibrate_thresholds.py")


class QueryKindTest(unittest.TestCase):
    def test_classifies_scripts(self):
        self.assertEqual(export_mod.query_kind("RSS 日报"), "mixed")
        self.assertEqual(export_mod.query_kind("日报"), "cjk")
        self.assertEqual(export_mod.query_kind("rss"), "latin")
        self.assertEqual(export_mod.query_kind("!!!"), "other")


class ClarityTest(unittest.TestCase):
    def test_single_topic_query_is_vague(self):
        self.assertEqual(export_mod.query_clarity("agent-vibe"), "vague")
        self.assertEqual(export_mod.query_clarity("酷酷的鑫"), "vague")

    def test_multi_term_query_is_specific(self):
        self.assertEqual(export_mod.query_clarity("RSS 治理 日报"), "specific")

    def test_seeded_negative_is_synthetic(self):
        self.assertEqual(
            export_mod.query_clarity("zzzz_no_such_memory_20261030 qwertyuiop"),
            "synthetic",
        )


class UserTextTest(unittest.TestCase):
    def test_ignores_tool_results_and_rule_blocks(self):
        self.assertEqual(export_mod.user_text({"type": "assistant"}), "")
        self.assertEqual(
            export_mod.user_text(
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "x"}]},
                }
            ),
            "",
        )
        self.assertEqual(
            export_mod.user_text(
                {"type": "user", "message": {"content": "--rule_start 规则"}}
            ),
            "",
        )

    def test_extracts_plain_text(self):
        self.assertEqual(
            export_mod.user_text(
                {"type": "user", "message": {"content": "统计一下 token 消耗"}}
            ),
            "统计一下 token 消耗",
        )


class LocatorTest(unittest.TestCase):
    def test_locator_is_unique_per_chunk(self):
        first = export_mod.locator_of(
            {"id": "abcdef1234", "locator": {"file": "a.md", "line": 3}}
        )
        second = export_mod.locator_of(
            {"id": "abcdef9999", "locator": {"file": "a.md", "line": 3}}
        )
        self.assertNotEqual(first, second)
        self.assertIn("a.md:3", first)


class StratifiedTest(unittest.TestCase):
    def test_round_robin_keeps_languages_mixed(self):
        texts = ["mix 混合"] * 3 + ["plain"] * 3 + ["中文"] * 3
        queries = [
            {"query": text, "context": "", "clarity": "specific"} for text in texts
        ]
        picked = export_mod.stratified(queries, 6)
        kinds = {export_mod.query_kind(entry["query"]) for entry in picked}
        self.assertEqual(len(picked), 6)
        self.assertEqual(len(kinds), 3)


class ParseMarkdownTest(unittest.TestCase):
    def test_parses_verdicts_and_skips_unjudged(self):
        body = "\n".join(
            [
                "## 1. first query",
                "",
                "| # | 判定 | 定位 | 分数 | 来源 | 摘要 |",
                "|---|---|---|---|---|---|",
                "| 1 | 相关 | a.md [1] | 0.7 | dense | x |",
                "| 2 |  | b.md [2] | 0.6 | dense | y |",
                "| 3 | 不相关 | c.md [3] | 0.5 | lexical | z |",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "a.md")
            path.write_text(body, encoding="utf-8")
            judged = import_mod.parse_markdown(str(path))

        self.assertEqual(
            judged["first query"], [("relevant", "a.md [1]"), ("unrelated", "c.md [3]")]
        )

    def test_numeric_shorthand_maps_to_verdicts(self):
        body = "\n".join(
            [
                "## 1. q",
                "| 1 | 1 | a.md [1] | 0.7 | dense | x |",
                "| 2 | 2 | b.md [2] | 0.6 | dense | y |",
                "| 3 | 3 | c.md [3] | 0.5 | lexical | z |",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "a.md")
            path.write_text(body, encoding="utf-8")
            judged = import_mod.parse_markdown(str(path))

        self.assertEqual(
            judged["q"],
            [
                ("relevant", "a.md [1]"),
                ("partial", "b.md [2]"),
                ("unrelated", "c.md [3]"),
            ],
        )

    def test_unescapes_pipes_in_locator(self):
        body = "\n".join(
            [
                "## 1. q",
                "| 1 | 部分 | a\\|b.md [1] | 0.5 | dense | x |",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "a.md")
            path.write_text(body, encoding="utf-8")
            judged = import_mod.parse_markdown(str(path))

        self.assertEqual(judged["q"], [("partial", "a|b.md [1]")])


class WriteFloorsTest(unittest.TestCase):
    def _write(self, root, tag, chosen, table):
        from agent_gateway.services.retrieval.gate import thresholds

        path = Path(root, "retrieval_thresholds.json")
        with mock.patch.object(thresholds, "override_path", return_value=path):
            return calibrate_mod.write_floors(tag, chosen), path

    def test_fresh_dir_inherits_unmeasured_keys(self):
        table = {
            "calib_test": {
                "hot": 0.9,
                "cold": 0.8,
                "expanded": 0.8,
                "search": 0.35,
                "notes_coverage": 0.6,
                "notes_semantic": 0.4,
            }
        }
        with tempfile.TemporaryDirectory() as root:
            written, path = self._write(
                root, "calib_test", {"hot": 0.62, "cold": 0.55}, table
            )
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(written, path)
        self.assertEqual(data["calib_test"]["hot"], 0.62)
        self.assertEqual(data["calib_test"]["cold"], 0.55)
        self.assertEqual(data["calib_test"]["expanded"], 0.55)
        self.assertEqual(data["calib_test"]["search"], 0.35)

    def test_unknown_key_refuses_to_write(self):
        table = {
            "calib_test": {
                "hot": 0.9,
                "cold": None,
                "expanded": 0.8,
                "search": 0.35,
                "notes_coverage": 0.6,
                "notes_semantic": 0.4,
            }
        }
        with tempfile.TemporaryDirectory() as root:
            written, path = self._write(root, "calib_test", {"hot": 0.62}, table)

        self.assertIsNone(written)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
