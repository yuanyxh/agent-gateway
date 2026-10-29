"""Tests for project search context and structure status semantics."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_gateway.services.mcp import project_tools


def _create_db(path: str, *, scanned: bool = True) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE directories (path TEXT PRIMARY KEY, module_name TEXT, "
            "parent_path TEXT, file_count INTEGER, has_package_manifest BOOLEAN)"
        )
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, dir_path TEXT, name TEXT, "
            "ext TEXT, sym_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_path TEXT, name TEXT, "
            "kind TEXT, signature TEXT, doc_comment TEXT, visibility TEXT, "
            "start_line INTEGER, end_line INTEGER)"
        )
        conn.execute(
            "CREATE TABLE packages (name TEXT, version TEXT, type TEXT, description TEXT)"
        )
        conn.execute(
            "CREATE TABLE imports (id INTEGER PRIMARY KEY, from_path TEXT, raw_target TEXT, "
            "to_path TEXT, is_external BOOLEAN, kind TEXT, line INTEGER, "
            "confidence REAL, resolver TEXT)"
        )
        if scanned:
            conn.execute("INSERT INTO directories VALUES ('src', 'src', '', 1, 0)")
            conn.execute(
                "INSERT INTO files VALUES ('src/pkg/mod.py', 'src/pkg', 'mod.py', 'py', 1)"
            )
            conn.execute(
                "INSERT INTO symbols VALUES (1, 'src/pkg/mod.py', 'target_fn', 'function', "
                "'target_fn()', '', 'public', 3, 8)"
            )
            conn.execute(
                "INSERT INTO imports VALUES (1, 'src/app.py', 'pkg.mod', 'src/pkg/mod.py', "
                "0, 'static', 1, 1.0, 'python-module')"
            )
    conn.close()


class ProjectSearchContextTest(unittest.TestCase):
    def test_search_adds_module_and_importers(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            with mock.patch.object(project_tools, "_search_vectors", return_value=[]):
                result = project_tools.tool_project_search(db, "target_fn")
        self.assertEqual(result["status"], "exact")
        hit = result["results"][0]
        self.assertEqual(hit["module"], "src/pkg")
        self.assertEqual(hit["importedBy"], ["src/app.py"])
        self.assertEqual(hit["importCount"], 1)

    def test_module_survives_missing_imports_table(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            conn = sqlite3.connect(db)
            with conn:
                conn.execute("DROP TABLE imports")
            conn.close()
            results = [{"file": "src/pkg/mod.py"}]
            project_tools._attach_search_context(db, results)
        self.assertEqual(results[0]["module"], "src/pkg")


class LegacySymbolSchemaTest(unittest.TestCase):
    def test_search_without_line_columns_keeps_results(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            conn = sqlite3.connect(db)
            with conn:
                conn.execute("DROP TABLE symbols")
                conn.execute(
                    "CREATE TABLE symbols (name TEXT, kind TEXT, signature TEXT, "
                    "file_path TEXT, visibility TEXT)"
                )
                conn.execute(
                    "INSERT INTO symbols VALUES "
                    "('legacy_fn', 'function', 'legacy_fn()', 'src/legacy.py', 'public')"
                )
            conn.close()
            with mock.patch.object(project_tools, "_search_vectors", return_value=[]):
                result = project_tools.tool_project_search(db, "legacy_fn")
        self.assertEqual(result["status"], "exact")
        self.assertIsNone(result["result"]["startLine"])
        self.assertEqual(result["result"]["module"], "src")


class VectorOnlySearchTest(unittest.TestCase):
    def _run(self, query: str, hits: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            with (
                mock.patch.object(
                    project_tools, "_search_symbols_fts", return_value=[]
                ),
                mock.patch.object(project_tools, "_search_files", return_value=[]),
                mock.patch.object(project_tools, "_search_packages", return_value=[]),
                mock.patch.object(project_tools, "_search_vectors", return_value=hits),
            ):
                return project_tools.tool_project_search(db, query, limit=5)

    def test_vector_only_match_is_weak_and_tagged(self):
        result = self._run(
            "where do we persist the conversation history",
            [
                {
                    "name": "ConversationFile",
                    "kind": "symbol",
                    "file": "src/core/history.py",
                    "signature": None,
                    "visibility": None,
                    "score": 0.53,
                }
            ],
        )
        self.assertEqual(result["status"], "weak_match")
        self.assertNotIn("result", result)
        self.assertEqual(result["results"][0]["layer"], "vector")
        self.assertEqual(result["results"][0]["module"], "src/core")

    def test_chinese_vector_only_is_no_match(self):
        result = self._run(
            "聊天记录保存位置",
            [
                {
                    "name": "translationSeq",
                    "kind": "symbol",
                    "file": "Sources/Translation/TranslationPanelController.swift",
                    "signature": None,
                    "visibility": None,
                    "score": 0.3682,
                }
            ],
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertIn("English", result["hint"])

    def test_vector_only_noise_is_rejected(self):
        result = self._run(
            "__no_such_symbol_20261030__",
            [
                {
                    "name": "symbols",
                    "kind": "symbol",
                    "file": "src/symbols.py",
                    "signature": None,
                    "visibility": None,
                    "score": 0.5036,
                }
            ],
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["rejected"], 1)


class ProjectStructureStatusTest(unittest.TestCase):
    def test_root_and_subpath_status(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            root_result = project_tools.tool_project_structure(db)
            sub_result = project_tools.tool_project_structure(db, "src/pkg")
        self.assertEqual(root_result["status"], "ok")
        self.assertEqual(sub_result["status"], "ok")
        self.assertEqual(sub_result["files"][0]["path"], "src/pkg/mod.py")

    def test_missing_subpath_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            result = project_tools.tool_project_structure(db, "missing")
        self.assertEqual(result["status"], "path_not_found")

    def test_existing_empty_directory_is_empty_not_missing(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db)
            conn = sqlite3.connect(db)
            with conn:
                conn.execute(
                    "INSERT INTO directories VALUES ('empty', 'empty', '', 0, 0)"
                )
            conn.close()
            result = project_tools.tool_project_structure(db, "empty")
        self.assertEqual(result["status"], "empty")

    def test_unscanned_database_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "deps.sqlite")
            _create_db(db, scanned=False)
            result = project_tools.tool_project_structure(db)
        self.assertEqual(result["status"], "not_scanned")


if __name__ == "__main__":
    unittest.main()
