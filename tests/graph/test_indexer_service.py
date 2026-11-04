"""Indexer service protocol: subprocess ping/index_file and in-process errors."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_gateway.services.graph import indexer

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


class ServiceProcessTest(unittest.TestCase):
    def setUp(self):
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(SRC_ROOT) + (
            os.pathsep + existing if existing else ""
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "agent_gateway.services.graph.indexer"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

    def tearDown(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process.stdout.close()
        self.process.stderr.close()

    def _send(self, request: dict) -> dict:
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        self.assertTrue(line, "service returned no response")
        return json.loads(line)

    def test_ping(self):
        response = self._send({"op": "ping"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["op"], "ping")
        self.assertIn("python", response["languages"])
        self.assertIn("swift", response["languages"])
        self.assertEqual(response["engine_version"], "2")

    def test_index_file_in_gateway_repo(self):
        rel_path = "src/agent_gateway/services/graph/model.py"
        data = (REPO_ROOT / rel_path).read_bytes()
        response = self._send(
            {
                "op": "index_file",
                "root": str(REPO_ROOT),
                "path": rel_path,
                "content_hash": hashlib.sha1(data).hexdigest(),
            }
        )
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["path"], rel_path)
        names = {symbol["name"] for symbol in response["symbols"]}
        self.assertIn("SymbolDef", names)
        self.assertIn("ParseError", names)

    def test_unsupported_language_is_explicit(self):
        response = self._send(
            {
                "op": "index_file",
                "root": str(REPO_ROOT),
                "path": "README.md",
            }
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "unsupported_language")


class InProcessProtocolTest(unittest.TestCase):
    def _write(self, root: str, rel_path: str, source: bytes) -> str:
        full = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(source)
        return hashlib.sha1(source).hexdigest()

    def test_batch_indexes_all_files(self):
        with tempfile.TemporaryDirectory() as root:
            a = b"from pkg.b import Shared\n\ndef run():\n    return Shared()\n"
            b = b"class Shared:\n    pass\n"
            hash_a = self._write(root, "pkg/a.py", a)
            hash_b = self._write(root, "pkg/b.py", b)
            response = indexer.handle_line(
                json.dumps(
                    {
                        "op": "index_batch",
                        "root": root,
                        "files": [
                            {"path": "pkg/a.py", "content_hash": hash_a},
                            {"path": "pkg/b.py", "content_hash": hash_b},
                        ],
                    }
                )
            )
        self.assertTrue(response["ok"], response)
        symbol_names = {
            symbol["name"]
            for result in response["results"]
            for symbol in result["symbols"]
        }
        self.assertEqual(symbol_names, {"run", "Shared"})
        shared_edge = [
            edge
            for result in response["results"]
            for edge in result["edges"]
            if edge["raw_name"] == "Shared"
        ][0]
        self.assertEqual(shared_edge["resolver"], "import_scoped")

    def test_failed_item_is_isolated_in_batch(self):
        with tempfile.TemporaryDirectory() as root:
            hash_v1 = self._write(root, "a.py", b"def old_name():\n    pass\n")
            first = indexer.handle_line(
                json.dumps(
                    {
                        "op": "index_file",
                        "root": root,
                        "path": "a.py",
                        "content_hash": hash_v1,
                    }
                )
            )
            self.assertTrue(first["ok"], first)

            hash_v2 = self._write(root, "a.py", b"def new_name():\n    pass\n")
            batch = indexer.handle_line(
                json.dumps(
                    {
                        "op": "index_batch",
                        "root": root,
                        "files": [
                            {"path": "a.py", "content_hash": hash_v2},
                            {"path": "missing.py"},
                        ],
                    }
                )
            )
            self.assertTrue(batch["ok"], batch)
            by_path = {result["path"]: result for result in batch["results"]}
            self.assertTrue(by_path["a.py"]["ok"])
            self.assertFalse(by_path["missing.py"]["ok"])

            index = indexer._INDEXES[os.path.realpath(root)]
            names = {symbol.name for symbol in index.analyses["a.py"].symbols}
            self.assertIn("new_name", names)
            self.assertNotIn("old_name", names)

    def test_hash_mismatch_fails_without_partial_result(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "a.py", b"def f():\n    pass\n")
            response = indexer.handle_line(
                json.dumps(
                    {
                        "op": "index_file",
                        "root": root,
                        "path": "a.py",
                        "content_hash": "0" * 40,
                    }
                )
            )
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "content_hash_mismatch")
        self.assertNotIn("symbols", response)

    def test_path_outside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            response = indexer.handle_line(
                json.dumps({"op": "index_file", "root": root, "path": "../escape.py"})
            )
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "path_outside_root")

    def test_index_file_skips_hidden_dir(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, ".cache/gen.py", b"def hook():\n    pass\n")
            response = indexer.handle_line(
                json.dumps(
                    {
                        "op": "index_file",
                        "root": root,
                        "path": ".cache/gen.py",
                    }
                )
            )
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["skipped"])
        self.assertNotIn("symbols", response)

    def test_batch_skips_dot_and_skip_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, ".build/out/gen.py", b"def gen():\n    pass\n")
            self._write(root, ".cache/gen.py", b"def cached():\n    pass\n")
            self._write(root, "node_modules/pkg/i.py", b"def i():\n    pass\n")
            self._write(root, "pkg/real.py", b"def real():\n    pass\n")
            response = indexer.handle_line(
                json.dumps(
                    {
                        "op": "index_batch",
                        "root": root,
                        "files": [
                            {"path": ".build/out/gen.py"},
                            {"path": ".cache/gen.py"},
                            {"path": "node_modules/pkg/i.py"},
                            {"path": "pkg/real.py"},
                        ],
                    }
                )
            )
            index = indexer._INDEXES[os.path.realpath(root)]
            by_path = {result["path"]: result for result in response["results"]}
        self.assertTrue(response["ok"], response)
        self.assertTrue(by_path[".build/out/gen.py"]["skipped"])
        self.assertTrue(by_path[".cache/gen.py"]["skipped"])
        self.assertTrue(by_path["node_modules/pkg/i.py"]["skipped"])
        self.assertTrue(by_path["pkg/real.py"]["ok"])
        self.assertNotIn("skipped", by_path["pkg/real.py"])
        self.assertEqual(set(index.analyses), {"pkg/real.py"})

    def test_prepare_drops_skipped_paths(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, ".build/out/gen.py", b"def helper():\n    pass\n")
            self._write(root, "pkg/a.py", b"def run():\n    return helper()\n")
            prepared = indexer.handle_line(
                json.dumps(
                    {
                        "op": "prepare",
                        "root": root,
                        "paths": [".build/out/gen.py", "pkg/a.py"],
                    }
                )
            )
            response = indexer.handle_line(
                json.dumps({"op": "index_file", "root": root, "path": "pkg/a.py"})
            )
        self.assertTrue(prepared["ok"], prepared)
        self.assertEqual(prepared["count"], 1)
        to_paths = {edge.get("to_path") for edge in response.get("edges", [])}
        self.assertNotIn(".build/out/gen.py", to_paths)
        self.assertEqual(
            [item["raw_name"] for item in response.get("unresolved", [])], ["helper"]
        )

    def test_remove_file_accepts_skipped_path(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, ".build/out/gen.py", b"def gen():\n    pass\n")
            response = indexer.handle_line(
                json.dumps(
                    {
                        "op": "remove_file",
                        "root": root,
                        "path": ".build/out/gen.py",
                    }
                )
            )
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["path"], ".build/out/gen.py")

    def test_bad_json_and_unknown_op(self):
        bad = indexer.handle_line("{not json")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "bad_request")
        unknown = indexer.handle_line(json.dumps({"op": "nope"}))
        self.assertEqual(unknown["reason"], "unknown_op")


if __name__ == "__main__":
    unittest.main()
