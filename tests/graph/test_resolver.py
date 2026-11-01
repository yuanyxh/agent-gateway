"""Resolver layer ordering and unresolved handling."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from agent_gateway.services.graph import resolver
from agent_gateway.services.graph.indexer import ProjectIndex
from agent_gateway.services.graph.parsers import EXTENSIONS, PARSERS


def _index(root: str, files: dict[str, bytes]) -> tuple[ProjectIndex, dict]:
    index = ProjectIndex(root)
    analyses = {}
    for rel_path, source in files.items():
        lang = EXTENSIONS[os.path.splitext(rel_path)[1].lower()]
        analysis = PARSERS[lang](source, rel_path)
        index.apply(analysis, hashlib.sha1(source).hexdigest())
        analyses[rel_path] = analysis
    return index, analyses


class ResolverLayerTest(unittest.TestCase):
    def test_resolves_each_layer_and_reports_ambiguity(self):
        files = {
            "pkg/a.py": b"""from pkg.b import Shared

def helper():
    return helper()

def use_all():
    Shared()
    LocalSibling()
    UniqueThing()
    Dup()
""",
            "pkg/b.py": b"""class Shared:
    pass
""",
            "pkg/local.py": b"""def LocalSibling():
    pass
""",
            "other/c.py": b"""class UniqueThing:
    pass

class Dup:
    pass
""",
            "lib/d.py": b"""class Dup:
    pass
""",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, unresolved = resolver.resolve_file(analyses["pkg/a.py"], index)

        by_name = {}
        for edge in edges:
            by_name.setdefault(edge["raw_name"], []).append(edge)
        self.assertEqual(by_name["helper"][0]["resolver"], "local")
        self.assertEqual(by_name["Shared"][0]["resolver"], "import_scoped")
        self.assertEqual(by_name["Shared"][0]["to_path"], "pkg/b.py")
        self.assertEqual(by_name["LocalSibling"][0]["resolver"], "module_scoped")
        self.assertEqual(by_name["UniqueThing"][0]["resolver"], "global_unique")
        import_edges = [edge for edge in edges if edge["kind"] == "import"]
        self.assertEqual(import_edges[0]["to_path"], "pkg/b.py")

        self.assertNotIn("Dup", by_name)
        reasons = {(item["raw_name"], item["reason"]) for item in unresolved}
        self.assertIn(("Dup", "ambiguous"), reasons)

    def test_missing_binding_is_unresolved(self):
        files = {"pkg/a.py": b"def f():\n    return MissingName()\n"}
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, unresolved = resolver.resolve_file(analyses["pkg/a.py"], index)
        self.assertEqual(edges, [])
        self.assertEqual(unresolved[0]["reason"], "no_binding")

    def test_member_call_does_not_bind_same_file_function(self):
        files = {
            "src/a.swift": b"""func run() {}
func spawn() {
    let p = Process()
    p.run()
}
""",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, unresolved = resolver.resolve_file(analyses["src/a.swift"], index)
        self.assertNotIn("run", {edge["raw_name"] for edge in edges})
        self.assertIn(
            ("run", "no_binding"), {(u["raw_name"], u["reason"]) for u in unresolved}
        )

    def test_import_scope_with_multiple_files_is_ambiguous(self):
        files = {
            "pkg/a.py": b"from pkg import one, two\n\ndef f():\n    Shared()\n",
            "pkg/one.py": b"class Shared:\n    pass\n",
            "pkg/two.py": b"class Shared:\n    pass\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, unresolved = resolver.resolve_file(analyses["pkg/a.py"], index)
        self.assertNotIn("Shared", {edge["raw_name"] for edge in edges})
        self.assertIn(
            ("Shared", "ambiguous"), {(u["raw_name"], u["reason"]) for u in unresolved}
        )


class ReExportTest(unittest.TestCase):
    def test_module_receiver_follows_re_export(self):
        files = {
            "pkg/core.py": b"def run():\n    pass\n",
            "pkg/facade.py": b"from pkg.core import run\n",
            "pkg/use.py": b"from pkg import facade\n\ndef go():\n    facade.run()\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["pkg/use.py"], index)
        run_edges = [edge for edge in edges if edge["raw_name"] == "run"]
        self.assertEqual(run_edges[0]["resolver"], "re_export")
        self.assertEqual(run_edges[0]["to_path"], "pkg/core.py")

    def test_module_receiver_finds_direct_definition(self):
        files = {
            "pkg/core.py": b"def run():\n    pass\n",
            "pkg/use.py": b"from pkg import core\n\ndef go():\n    core.run()\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["pkg/use.py"], index)
        run_edges = [edge for edge in edges if edge["raw_name"] == "run"]
        self.assertEqual(run_edges[0]["resolver"], "import_scoped")
        self.assertEqual(run_edges[0]["to_path"], "pkg/core.py")


class ProtocolConformanceTest(unittest.TestCase):
    def test_protocol_base_becomes_implements(self):
        files = {
            "src/p.swift": b"protocol P {}\n",
            "src/a.swift": b"class Foo: P {}\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["src/a.swift"], index)
        extends = [edge for edge in edges if edge["raw_name"] == "P"]
        self.assertEqual(extends[0]["kind"], "implements")
        self.assertEqual(extends[0]["resolver"], "module_scoped")


if __name__ == "__main__":
    unittest.main()


class PackageImportTest(unittest.TestCase):
    def test_java_import_maps_to_project_file(self):
        files = {
            "src/main/java/com/example/model/Pet.java": (
                b"package com.example.model;\npublic class Pet {}\n"
            ),
            "src/main/java/com/example/web/C.java": (
                b"package com.example.web;\n"
                b"import com.example.model.Pet;\n"
                b"public class C { void go() { new Pet(); } }\n"
            ),
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            path = "src/main/java/com/example/web/C.java"
            edges, _ = resolver.resolve_file(analyses[path], index)
        hits = [edge for edge in edges if edge["raw_name"] == "Pet"]
        self.assertEqual(hits[0]["resolver"], "import_scoped")
        self.assertEqual(hits[0]["to_path"], "src/main/java/com/example/model/Pet.java")


class JsAliasTest(unittest.TestCase):
    def test_at_alias_maps_to_project_file(self):
        files = {
            "src/utils/request.js": b"export function get(url) { return url; }\n",
            "src/pages/home.js": (
                b'import { get } from "@/utils/request";\n'
                b'function load() { get("/x"); }\n'
            ),
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["src/pages/home.js"], index)
        imports = [edge for edge in edges if edge["kind"] == "import"]
        self.assertEqual(imports[0]["to_path"], "src/utils/request.js")
        calls = [edge for edge in edges if edge["raw_name"] == "get"]
        self.assertEqual(calls[0]["to_path"], "src/utils/request.js")


class CommonJsRequireTest(unittest.TestCase):
    def test_require_alias_resolves_to_module_export(self):
        files = {
            "lib/app.js": (
                b"function createApplication() {}\n"
                b"module.exports = createApplication;\n"
            ),
            "index.js": (b'const express = require("./lib/app");\nexpress();\n'),
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["index.js"], index)
        calls = [edge for edge in edges if edge["raw_name"] == "express"]
        self.assertEqual(calls[0]["resolver"], "commonjs_export")
        self.assertEqual(calls[0]["to_path"], "lib/app.js")
        self.assertEqual(calls[0]["to_name"], "createApplication")


class LanguageFamilyTest(unittest.TestCase):
    def test_global_unique_does_not_cross_language_families(self):
        files = {
            "src/a.ts": b"function go() { foo(); }\n",
            "pkg/b.py": b"def foo():\n    pass\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, unresolved = resolver.resolve_file(analyses["src/a.ts"], index)
        self.assertNotIn("foo", {edge["raw_name"] for edge in edges})
        self.assertIn(
            ("foo", "no_binding"), {(u["raw_name"], u["reason"]) for u in unresolved}
        )
