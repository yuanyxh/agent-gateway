"""Tests for project inference from source paths and the write path using it."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402
from agent_gateway.services.memory.project_infer import (  # noqa: E402
    KNOWN_PROJECTS,
    infer_project,
)


class InferProjectTest(unittest.TestCase):
    def test_known_project_in_path_segment(self):
        self.assertEqual(
            infer_project(["/Users/x/Documents/selfdata/projects/utility/README.md"]),
            "utility",
        )
        self.assertEqual(
            infer_project(["agent-gateway/src/agent_gateway/services/memory/store.py"]),
            "agent-gateway",
        )

    def test_project_name_inside_cjk_directory(self):
        self.assertEqual(
            infer_project(
                ["/notes/Extracts/Utility工具集/utility_token_proxy_开发.md"]
            ),
            "utility",
        )
        self.assertEqual(
            infer_project(["/notes/实践经验/移动端/yjzt-wms-app_功能移植.md"]),
            "yjzt-wms-app",
        )

    def test_unknown_or_empty_paths_return_empty(self):
        self.assertEqual(infer_project([]), "")
        self.assertEqual(infer_project(["/tmp/random/file.md"]), "")
        self.assertEqual(infer_project([""]), "")
        self.assertEqual(infer_project(["my-utility-scripts/x.py"]), "")

    def test_cross_project_paths_take_the_first_match(self):
        self.assertEqual(
            infer_project(
                [
                    "/Users/x/Documents/selfdata/projects/agent-gateway/a.py",
                    "/Users/x/Documents/selfdata/projects/utility/b.swift",
                ]
            ),
            "agent-gateway",
        )
        self.assertEqual(
            infer_project(
                [
                    "/Users/x/Documents/selfdata/projects/utility/agent-gateway/a.py",
                ]
            ),
            "utility",
        )

    def test_every_known_project_name_is_matched(self):
        for name in KNOWN_PROJECTS:
            with self.subTest(project=name):
                self.assertEqual(infer_project([f"/repos/{name}/file.py"]), name)


class WritePathTest(unittest.TestCase):
    def test_explicit_project_wins_over_inference(self):
        dimensions = server_memory._dimensions_of(
            {"project": "custom", "source_files": ["/repos/utility/a.swift"]}
        )
        self.assertEqual(dimensions["project"], "custom")

    def test_empty_project_is_inferred_from_source_files(self):
        dimensions = server_memory._dimensions_of(
            {"source_files": ["/repos/agent-gateway/a.py"]}
        )
        self.assertEqual(dimensions["project"], "agent-gateway")

    def test_empty_project_stays_empty_when_paths_are_unknown(self):
        dimensions = server_memory._dimensions_of(
            {"source_files": ["/tmp/random/file.md"]}
        )
        self.assertEqual(dimensions["project"], "")


if __name__ == "__main__":
    unittest.main()
