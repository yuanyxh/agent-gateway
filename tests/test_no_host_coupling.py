"""The gateway must not depend on its former host name."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_gateway"

# Migration and skip lists are the only places allowed to mention the legacy name.
WHITELIST = {
    "core/env_compat.py",
    "core/project_paths.py",
    "services/graph/analysis.py",
    "services/graph/project_scan.py",
    "services/mcp/graph_tools.py",
    "services/mcp/search_constants.py",
}

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+utility\b", re.MULTILINE)


class HostCouplingTest(unittest.TestCase):
    def test_no_host_coupling(self):
        hits: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            rel = path.relative_to(SRC).as_posix()
            if rel in WHITELIST:
                continue
            text = path.read_text(encoding="utf-8")
            for token in ("UTILITY_", ".utility"):
                if token in text:
                    hits.append(f"{rel}: {token}")
            if IMPORT_RE.search(text):
                hits.append(f"{rel}: imports utility")
        self.assertEqual(hits, [])
