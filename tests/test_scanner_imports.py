import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _binary():
    for rel in (".build/debug/utility", ".build/release/utility"):
        path = ROOT / rel
        if path.is_file():
            return path
    return None


class ScannerImportsE2ETest(unittest.TestCase):
    def setUp(self):
        binary = _binary()
        if binary is None:
            self.skipTest("utility binary not built")
        self.binary = binary
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, body: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _fixture(self) -> None:
        self._write("pyproject.toml", "[project]\nname = 'fixture'\n")
        self._write("app/__init__.py", "")
        self._write(
            "app/main.py",
            "from . import util\n"
            "from . import exported\n"
            "from .util import helper\n"
            "import os\n",
        )
        self._write("app/util.py", "def helper():\n    pass\n")
        # No manifest inside libs/: the root must be inferred from __init__.py.
        self._write("libs/pkg/__init__.py", "")
        self._write("libs/pkg/core/__init__.py", "")
        self._write("libs/pkg/core/a.py", "from pkg.core import b\n")
        self._write("libs/pkg/core/b.py", "VALUE = 1\n")
        self._write(
            "web/src/index.ts", 'import { a } from "./a"\nimport React from "react"\n'
        )
        self._write("web/src/a.ts", "export const a = 1\n")
        self._write("index.ts", "export const root = 1\n")
        self._write("web/src/root.ts", 'import "../../"\n')
        self._write(
            "java/src/main/java/com/acme/Main.java",
            "import com.acme.Util;\n"
            "import com.acme.Missing;\n"
            "import com.google.common.truth.Truth;\n"
            "class Main {}\n",
        )
        self._write("java/src/main/java/com/acme/Util.java", "class Util {}\n")
        self._write("swift/Sources/App/main.swift", "import Foundation\n")

    def _rows(self):
        db = self.root / ".utility" / "deps.sqlite"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT from_path, raw_target, to_path, is_external, resolver FROM imports"
            ).fetchall()
        finally:
            conn.close()

    def test_full_scan_resolves_and_classifies(self):
        subprocess.run(
            [str(self.binary), "init", "--path", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        all_rows = self._rows()
        rows = {(row["from_path"], row["raw_target"]): row for row in all_rows}
        self.assertEqual(rows[("app/main.py", ".util")]["to_path"], "app/util.py")
        self.assertEqual(
            rows[("app/main.py", ".exported")]["to_path"], "app/__init__.py"
        )
        self.assertFalse(any(row["raw_target"] == "." for row in all_rows))
        self.assertEqual(rows[("app/main.py", "os")]["is_external"], 1)
        self.assertEqual(
            rows[("libs/pkg/core/a.py", "pkg.core")]["to_path"],
            "libs/pkg/core/__init__.py",
        )
        self.assertEqual(rows[("web/src/index.ts", "./a")]["to_path"], "web/src/a.ts")
        self.assertEqual(rows[("web/src/index.ts", "react")]["is_external"], 1)
        self.assertEqual(rows[("web/src/root.ts", "../../")]["to_path"], "index.ts")
        main = "java/src/main/java/com/acme/Main.java"
        self.assertEqual(rows[(main, "com.acme.Missing")]["is_external"], 0)
        self.assertIsNone(rows[(main, "com.acme.Missing")]["to_path"])
        self.assertEqual(
            rows[(main, "com.google.common.truth.Truth")]["is_external"], 1
        )
        self.assertEqual(
            rows[("java/src/main/java/com/acme/Main.java", "com.acme.Util")]["to_path"],
            "java/src/main/java/com/acme/Util.java",
        )
        self.assertEqual(
            rows[("swift/Sources/App/main.swift", "Foundation")]["is_external"], 1
        )


if __name__ == "__main__":
    unittest.main()
