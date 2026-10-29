"""Tests for project-note cache path resolution and prewarm skipping."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from agent_gateway.services.mcp.search_notes_cache import (
    _ensure_note_cache,
    _get_note_db,
    prewarm_project_notes,
    project_root_from_db_path,
)
from agent_gateway.services.mcp.search_notes_io import _chunk_md_text


class ProjectRootFromDbPathTest(unittest.TestCase):
    def test_resolves_utility_layout(self):
        db = os.path.join("/tmp", "demo", ".utility", "deps.sqlite")
        self.assertEqual(project_root_from_db_path(db), "/tmp/demo")

    def test_rejects_other_file_name(self):
        self.assertIsNone(project_root_from_db_path("/tmp/demo/.utility/other.sqlite"))

    def test_rejects_other_directory(self):
        self.assertIsNone(project_root_from_db_path("/tmp/demo/cache/deps.sqlite"))


class PrewarmProjectNotesTest(unittest.TestCase):
    def test_skips_non_dev_project(self):
        with tempfile.TemporaryDirectory() as root:
            result = prewarm_project_notes(root)
        self.assertEqual(result, {"ok": True, "skipped": "non-dev"})


class NoteCachePruneTest(unittest.TestCase):
    def test_prunes_stale_rows_and_keeps_scanned_files(self):
        with tempfile.TemporaryDirectory() as root:
            readme = os.path.join(root, "README.md")
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write("# Title\n\n" + ("Detailed content. " * 20))
            db = os.path.join(root, ".utility", "note_vectors.sqlite")
            os.makedirs(os.path.dirname(db))
            conn = _get_note_db(db)
            with conn:
                conn.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    ("/old/project/CLAUDE.md", 0, "stale", 1, 1.0, 10, None),
                )
            conn.close()

            with mock.patch(
                "agent_gateway.services.mcp.search_notes_cache._get_shared_embedder",
                return_value=None,
            ):
                conn, _, total = _ensure_note_cache(root)
            paths = {
                r[0] for r in conn.execute("SELECT DISTINCT file_path FROM chunks")
            }
            conn.close()

        self.assertEqual(paths, {readme})
        self.assertEqual(total, 1)


class ChunkMarkdownTest(unittest.TestCase):
    def test_merges_short_paragraphs_instead_of_dropping(self):
        text = (
            "## R1\n\n**Rule**: keep it small.\n\n"
            "**Ban**: no bypass.\n\n**Check**: lint passes.\n"
        )
        chunks = _chunk_md_text(text, "/tmp/rules.md")
        joined = "\n\n".join(c["chunk_text"] for c in chunks)
        self.assertIn("no bypass", joined)
        self.assertEqual([c["chunk_index"] for c in chunks], list(range(len(chunks))))

    def test_long_paragraph_indices_stay_sequential(self):
        chunks = _chunk_md_text("word " * 400, "/tmp/long.md")
        self.assertGreater(len(chunks), 1)
        self.assertEqual([c["chunk_index"] for c in chunks], list(range(len(chunks))))


if __name__ == "__main__":
    unittest.main()
