import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.scanner.ast_extractor import _extract_one


class AstImportsTest(unittest.TestCase):
    def test_python_absolute_and_relative_imports(self):
        result = _extract_one(
            "import os\nimport a.b\nfrom .util import x\nfrom ..pkg.mod import y\n",
            "app/main.py",
            "py",
        )
        self.assertNotIn("error", result)
        self.assertEqual(
            [item["raw_target"] for item in result["imports"]],
            ["os", "a.b", ".util", "..pkg.mod"],
        )
        self.assertTrue(all(item["kind"] == "static" for item in result["imports"]))
        self.assertEqual([item["line"] for item in result["imports"]], [1, 2, 3, 4])

    def test_python_from_dot_import_names(self):
        result = _extract_one(
            "from . import util, other\nfrom . import *\n",
            "app/main.py",
            "py",
        )
        self.assertNotIn("error", result)
        self.assertEqual(
            [item["raw_target"] for item in result["imports"]],
            [".util", ".other"],
        )

    def test_python_symbols_still_returned(self):
        result = _extract_one("import os\n\ndef f():\n    pass\n", "a.py", "py")
        self.assertEqual([s["name"] for s in result["symbols"]], ["f"])

    def test_js_import_kinds(self):
        source = (
            'import React from "react";\n'
            'export { a } from "./a";\n'
            'export * from "./b";\n'
            'const c = require("./c");\n'
            'const d = import("./d");\n'
            "const e = require(name);\n"
        )
        result = _extract_one(source, "web/index.ts", "ts")
        if "error" in result:
            self.skipTest(f"Node parser unavailable: {result['error']}")
        pairs = [(item["raw_target"], item["kind"]) for item in result["imports"]]
        self.assertEqual(
            pairs,
            [
                ("react", "static"),
                ("./a", "export_from"),
                ("./b", "export_from"),
                ("./c", "require"),
                ("./d", "dynamic"),
            ],
        )

    def test_vue_import_line_offset(self):
        source = '<template><div /></template>\n<script setup lang="ts">\nimport x from "./x"\n</script>\n'
        result = _extract_one(source, "vue/App.vue", "vue")
        if "error" in result:
            self.skipTest(f"Node parser unavailable: {result['error']}")
        self.assertEqual(len(result["imports"]), 1)
        self.assertEqual(result["imports"][0]["raw_target"], "./x")
        self.assertEqual(result["imports"][0]["line"], 3)


if __name__ == "__main__":
    unittest.main()
