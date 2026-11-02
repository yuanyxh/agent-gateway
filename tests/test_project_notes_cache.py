"""Tests for project-note cache path resolution and prewarm skipping."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_gateway.services.mcp import search_notes_cache
from agent_gateway.services.mcp.search_notes_cache import (
    _ensure_note_cache,
    _get_note_db,
    prewarm_project_notes,
    project_root_from_db_path,
)
from agent_gateway.services.mcp.search_notes_query import _query_project_notes
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


class EmbedderIdentityTest(unittest.TestCase):
    def test_drops_chunks_written_by_another_model(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "note_vectors.sqlite")
            os.makedirs(os.path.dirname(db))
            conn = _get_note_db(db)
            with conn:
                conn.execute("DELETE FROM meta")
                conn.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('embedder_dims', '1')"
                )
                conn.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    ("/p/README.md", 0, "text", 1, 1.0, 10, b"\x00" * 16),
                )
            conn.close()

            conn = _get_note_db(db)
            remaining = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            dims = conn.execute(
                "SELECT value FROM meta WHERE key = 'embedder_dims'"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(remaining, 0)
        self.assertEqual(int(dims), search_notes_cache.EMBEDDER_SPEC.dims)

    def test_drops_chunks_when_the_recorded_tag_changes(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "note_vectors.sqlite")
            os.makedirs(os.path.dirname(db))
            conn = _get_note_db(db)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('embedder_tag', 'othermodel')"
                )
                conn.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (
                        "/p/README.md",
                        0,
                        "text",
                        1,
                        1.0,
                        10,
                        b"\x00" * (search_notes_cache.EMBEDDER_SPEC.dims * 4),
                    ),
                )
            conn.close()

            conn = _get_note_db(db)
            remaining = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            tag = conn.execute(
                "SELECT value FROM meta WHERE key = 'embedder_tag'"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(remaining, 0)
        self.assertEqual(tag, search_notes_cache.EMBEDDER_SPEC.tag)

    def test_keeps_chunks_from_the_same_model(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, ".utility", "note_vectors.sqlite")
            os.makedirs(os.path.dirname(db))
            conn = _get_note_db(db)
            with conn:
                conn.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (
                        "/p/README.md",
                        0,
                        "text",
                        1,
                        1.0,
                        10,
                        b"\x00" * (search_notes_cache.EMBEDDER_SPEC.dims * 4),
                    ),
                )
            conn.close()

            conn = _get_note_db(db)
            remaining = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            conn.close()

        self.assertEqual(remaining, 1)


class PrewarmThrottleTest(unittest.TestCase):
    def setUp(self):
        with search_notes_cache._prewarm_lock:
            search_notes_cache._last_prewarm.clear()
            search_notes_cache._prewarm_inflight.clear()

    def tearDown(self):
        with search_notes_cache._prewarm_lock:
            search_notes_cache._last_prewarm.clear()
            search_notes_cache._prewarm_inflight.clear()

    def _wait_idle(self, project_path: str, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with search_notes_cache._prewarm_lock:
                if project_path not in search_notes_cache._prewarm_inflight:
                    return
            time.sleep(0.01)
        self.fail("prewarm thread did not finish")

    def test_second_schedule_within_window_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.normpath(root)
            with mock.patch.object(
                search_notes_cache, "prewarm_project_notes", return_value={"ok": True}
            ):
                self.assertTrue(search_notes_cache.schedule_project_notes_prewarm(root))
                self._wait_idle(root)
                self.assertFalse(
                    search_notes_cache.schedule_project_notes_prewarm(root)
                )

    def test_schedule_allowed_after_window_expires(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.normpath(root)
            with (
                mock.patch.object(
                    search_notes_cache,
                    "prewarm_project_notes",
                    return_value={"ok": True},
                ),
                mock.patch.object(
                    search_notes_cache, "_PREWARM_MIN_INTERVAL_SECONDS", 0
                ),
            ):
                self.assertTrue(search_notes_cache.schedule_project_notes_prewarm(root))
                self._wait_idle(root)
                self.assertTrue(search_notes_cache.schedule_project_notes_prewarm(root))
                self._wait_idle(root)


class PrewarmFileCapTest(unittest.TestCase):
    def test_caps_changed_files_and_resumes_on_next_run(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(3):
                with open(
                    os.path.join(root, f"doc{i}.md"), "w", encoding="utf-8"
                ) as fh:
                    fh.write("# Title\n\n" + ("content sentence. " * 20))

            with mock.patch(
                "agent_gateway.services.mcp.search_notes_cache._get_shared_embedder",
                return_value=None,
            ):
                conn, _, _ = _ensure_note_cache(root, max_files=1)
                first = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                conn.close()

                conn, _, _ = _ensure_note_cache(root, max_files=1)
                second = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                conn.close()

                conn, _, _ = _ensure_note_cache(root)
                final = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                conn.close()

        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertEqual(final, 3)


class ChineseKeywordQueryTest(unittest.TestCase):
    def test_cjk_query_without_whitespace_matches_split_terms(self):
        """A space-free CJK query used to become one needle equal to itself."""
        with tempfile.TemporaryDirectory() as root:
            root = os.path.normpath(root)
            Path(root, "package.json").write_text("{}", encoding="utf-8")
            Path(root, "doc.md").write_text(
                "第一章\n\n管理剪贴板的逻辑在这里说明，涉及面板与热键注册。\n\n"
                "第二章\n\n其他无关内容，仅用于把文件撑到足够的长度。\n",
                encoding="utf-8",
            )

            with (
                mock.patch(
                    "agent_gateway.services.mcp.search_notes_cache._get_shared_embedder",
                    return_value=None,
                ),
                mock.patch(
                    "agent_gateway.services.mcp.search_notes_query.embed_query",
                    return_value=None,
                ),
                mock.patch(
                    "agent_gateway.services.mcp.search_notes_query.schedule_project_notes_prewarm",
                    return_value=False,
                ),
            ):
                result = _query_project_notes(root, "剪贴板管理", 5)

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["file"], "doc.md")


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


class PerProjectLockTest(unittest.TestCase):
    def test_locks_are_per_project(self):
        self.assertIs(
            search_notes_cache._project_lock("/tmp/a"),
            search_notes_cache._project_lock("/tmp/a"),
        )
        self.assertIsNot(
            search_notes_cache._project_lock("/tmp/a"),
            search_notes_cache._project_lock("/tmp/b"),
        )

    def test_wait_false_returns_none_while_locked(self):
        with tempfile.TemporaryDirectory() as root:
            lock = search_notes_cache._project_lock(root)
            lock.acquire()
            try:
                self.assertIsNone(_ensure_note_cache(root, wait=False))
            finally:
                lock.release()


class QueryWhileBusyTest(unittest.TestCase):
    def test_serves_existing_chunks_when_cache_is_busy(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.normpath(root)
            Path(root, "package.json").write_text("{}", encoding="utf-8")
            Path(root, "doc.md").write_text(
                "第一章\n\n管理剪贴板的逻辑在这里说明，涉及面板与热键注册。\n\n"
                "第二章\n\n其他无关内容，仅用于把文件撑到足够的长度。\n",
                encoding="utf-8",
            )
            with mock.patch(
                "agent_gateway.services.mcp.search_notes_cache._get_shared_embedder",
                return_value=None,
            ):
                conn, _, _ = _ensure_note_cache(root)
                conn.close()

            lock = search_notes_cache._project_lock(root)
            lock.acquire()
            try:
                with (
                    mock.patch(
                        "agent_gateway.services.mcp.search_notes_query.schedule_project_notes_prewarm",
                        return_value=False,
                    ),
                    mock.patch(
                        "agent_gateway.services.mcp.search_notes_query.embed_query",
                        return_value=None,
                    ),
                ):
                    result = _query_project_notes(root, "剪贴板管理", 5)
            finally:
                lock.release()

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["file"], "doc.md")


if __name__ == "__main__":
    unittest.main()
