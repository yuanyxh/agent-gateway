"""Regression tests for the graph review findings."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest

from agent_gateway.services.graph import resolver
from agent_gateway.services.graph.indexer import ProjectIndex
from agent_gateway.services.graph.parsers import (
    EXTENSIONS,
    PARSERS,
    java,
    kotlin,
    objc,
    python,
    typescript,
    vue,
)
from agent_gateway.services.mcp import graph_tools


def _index(root: str, files: dict[str, bytes]) -> tuple[ProjectIndex, dict]:
    index = ProjectIndex(root)
    analyses = {}
    for rel_path, source in files.items():
        lang = EXTENSIONS[os.path.splitext(rel_path)[1].lower()]
        analysis = PARSERS[lang](source, rel_path)
        index.apply(analysis, hashlib.sha1(source).hexdigest())
        analyses[rel_path] = analysis
    return index, analyses


class ParserRegressionTest(unittest.TestCase):
    def test_ts_arrow_body_references_and_local_scope(self):
        source = (
            b"const h = () => { foo(); };\n"
            b"const x = make(1);\n"
            b"function outer() { const inner = () => { bar(); }; }\n"
        )
        analysis = typescript.parse(source, "src/a.ts")
        refs = {item.raw_name for item in analysis.references}
        self.assertTrue({"foo", "make", "bar"} <= refs)
        names = {item.name for item in analysis.symbols}
        self.assertIn("h", names)
        self.assertNotIn("inner", names)

    def test_ts_import_aliases_namespace_and_types(self):
        source = (
            b'import { Foo as Bar } from "./a";\n'
            b'import * as ns from "./b";\n'
            b"class X extends Y<Zed> implements I<Jay> { p: Prop; }\n"
        )
        analysis = typescript.parse(source, "src/a.ts")
        imports = {(item.module, item.aliases) for item in analysis.imports}
        self.assertIn(("./a", (("Bar", "Foo"),)), imports)
        self.assertIn(("./b", (("ns", "*"),)), imports)
        refs = {(item.raw_name, item.kind) for item in analysis.references}
        self.assertIn(("Y", "extends"), refs)
        self.assertIn(("I", "extends"), refs)
        self.assertIn(("Zed", "type_ref"), refs)
        self.assertIn(("Prop", "type_ref"), refs)
        self.assertNotIn(("Y<Zed>", "extends"), refs)

    def test_vue_script_lang_and_multiple_blocks(self):
        source = b"""<script lang="ts">
interface Shape { x: number }
const a: Shape = { x: 1 }
</script>
<script setup>
const go = () => { save(); }
</script>
<template><user-card @click='go' /></template>
"""
        analysis = vue.parse(source, "src/UserCard.vue")
        names = {(item.name, item.kind) for item in analysis.symbols}
        self.assertIn(("Shape", "interface"), names)
        self.assertIn(("go", "function"), names)
        self.assertIn(("UserCard", "component"), names)
        refs = {(item.raw_name, item.kind) for item in analysis.references}
        self.assertIn(("save", "call"), refs)
        self.assertIn(("UserCard", "type_ref"), refs)

    def test_objc_comment_apostrophes_do_not_hide_code(self):
        source = b"// don't edit\n[obj foo];\n// won't work\n[obj bar];\n"
        analysis = objc.parse(source, "a.m")
        refs = {item.raw_name for item in analysis.references}
        self.assertTrue({"foo", "bar"} <= refs)

    def test_kotlin_member_call_not_filtered(self):
        source = b"class S { fun go() { scanner.run(); println(x) } }\n"
        analysis = kotlin.parse(source, "a.kt")
        refs = {(item.raw_name, item.receiver) for item in analysis.references}
        self.assertIn(("run", "scanner"), refs)
        self.assertNotIn(("println", None), refs)

    def test_java_type_refs_include_bare_and_qualified(self):
        source = (
            b"class B { Foo f; com.acme.Widget w; Foo make(Bar b) { return null; } }\n"
        )
        analysis = java.parse(source, "B.java")
        refs = {
            (item.raw_name, item.kind, item.from_name) for item in analysis.references
        }
        self.assertIn(("Foo", "type_ref", "B"), refs)
        self.assertIn(("Widget", "type_ref", "B"), refs)
        self.assertIn(("Foo", "type_ref", "make"), refs)
        self.assertIn(("Bar", "type_ref", "make"), refs)
        self.assertNotIn(("com", "type_ref", "B"), refs)

    def test_ts_object_literal_method_not_symbol(self):
        source = b"const api = { get() { return 1; } };\nfunction get() {}\n"
        analysis = typescript.parse(source, "a.ts")
        names = [(item.name, item.kind, item.start_line) for item in analysis.symbols]
        self.assertEqual(names, [("get", "function", 2)])

    def test_ts_type_parameters_not_recorded_as_refs(self):
        source = (
            b"class Box<TValue, TKey extends Base> {\n"
            b"  get<KArg extends Limit>(key: KArg): TValue { return null; }\n"
            b"}\n"
            b"const map = <TItem,>(item: TItem, fallback: Default) => item;\n"
        )
        analysis = typescript.parse(source, "a.ts")
        refs = {
            item.raw_name for item in analysis.references if item.kind == "type_ref"
        }
        self.assertNotIn("TValue", refs)
        self.assertNotIn("TKey", refs)
        self.assertNotIn("KArg", refs)
        self.assertNotIn("TItem", refs)
        self.assertIn("Base", refs)
        self.assertIn("Limit", refs)
        self.assertIn("Default", refs)

    def test_python_metaclass_is_not_extends(self):
        analysis = python.parse(b"class Foo(Base, metaclass=Meta):\n    pass\n", "a.py")
        refs = {(item.raw_name, item.kind) for item in analysis.references}
        self.assertIn(("Base", "extends"), refs)
        self.assertNotIn(("Meta", "extends"), refs)

    def test_python_import_alias(self):
        analysis = python.parse(b"from pkg.core import run as go\n", "a.py")
        imports = [(item.module, item.names, item.aliases) for item in analysis.imports]
        self.assertIn(("pkg.core", ("run",), (("go", "run"),)), imports)


class ResolverRegressionTest(unittest.TestCase):
    def test_import_alias_and_namespace_bind(self):
        files = {
            "pkg/a.py": b"from pkg.b import run as go\n\ndef use():\n    go()\n",
            "pkg/b.py": b"def run():\n    pass\n",
            "web/a.ts": b'import * as ns from "./b";\nns.run();\n',
            "web/b.ts": b"export function run() {}\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            py_edges, _ = resolver.resolve_file(analyses["pkg/a.py"], index)
            ts_edges, _ = resolver.resolve_file(analyses["web/a.ts"], index)
        py = [edge for edge in py_edges if edge["raw_name"] == "go"]
        self.assertEqual(py[0]["to_path"], "pkg/b.py")
        ts = [edge for edge in ts_edges if edge["raw_name"] == "run"]
        self.assertEqual(ts[0]["to_path"], "web/b.ts")

    def test_import_alias_does_not_shadow_local(self):
        files = {
            "pkg/a.py": (
                b"from pkg.mod import log as run\n"
                b"def log():\n    pass\ndef use():\n    run()\n"
            ),
            "pkg/mod.py": b"def log():\n    pass\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["pkg/a.py"], index)
        calls = [edge for edge in edges if edge["raw_name"] == "run"]
        self.assertEqual(calls[0]["resolver"], "import_scoped")
        self.assertEqual(calls[0]["to_path"], "pkg/mod.py")

    def test_same_line_calls_keep_both_receivers(self):
        files = {
            "app.py": (
                b"class A:\n    def run(self):\n        pass\n"
                b"class B:\n    def run(self):\n        pass\n"
                b"def use():\n    A.run(); B.run()\n"
            ),
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["app.py"], index)
        calls = [edge for edge in edges if edge["raw_name"] == "run"]
        self.assertEqual(len(calls), 2)

    def test_re_export_alias_binds(self):
        files = {
            "pkg/core.py": b"def run():\n    pass\n",
            "pkg/facade.py": b"from pkg.core import run as go\n",
            "pkg/use.py": b"from pkg import facade\n\ndef f():\n    facade.go()\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["pkg/use.py"], index)
        calls = [edge for edge in edges if edge["raw_name"] == "go"]
        self.assertEqual(calls[0]["resolver"], "re_export")
        self.assertEqual(calls[0]["to_path"], "pkg/core.py")

    def test_module_alias_receiver_binds(self):
        files = {
            "pkg/core.py": b"def run():\n    pass\n",
            "pkg/use.py": b"import pkg.core as c\n\ndef go():\n    c.run()\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["pkg/use.py"], index)
        calls = [edge for edge in edges if edge["raw_name"] == "run"]
        self.assertEqual(calls[0]["to_path"], "pkg/core.py")

    def test_explicit_extension_and_mjs_import(self):
        files = {
            "web/a.mjs": b'import { run } from "./b.mjs";\nrun();\n',
            "web/b.mjs": b"export function run() {}\n",
        }
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["web/a.mjs"], index)
        calls = [edge for edge in edges if edge["raw_name"] == "run"]
        self.assertEqual(calls[0]["to_path"], "web/b.mjs")

    def test_self_binding_uses_enclosing_type(self):
        source = b"""class A:
    def run(self):
        pass

class B:
    def run(self):
        self.run()
"""
        analysis = python.parse(source, "app/a.py")
        line = next(item.line for item in analysis.references if item.raw_name == "run")
        scope = resolver._type_scope(analysis, line)
        self.assertIsNotNone(scope)
        self.assertEqual(scope.name, "B")

    def test_export_reference_is_not_an_edge(self):
        files = {"lib/a.js": b"module.exports = run;\nfunction run() {}\n"}
        with tempfile.TemporaryDirectory() as root:
            index, analyses = _index(root, files)
            edges, _ = resolver.resolve_file(analyses["lib/a.js"], index)
        self.assertEqual({edge["kind"] for edge in edges}, set())


class IndexerPrepareTest(unittest.TestCase):
    def test_cache_respects_requested_language(self):
        source = b"<script>const a = 1;</script>\n"
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "x.vue"), "wb") as handle:
                handle.write(source)
            index = ProjectIndex(root)
            index.prepare({"x.vue"})
            analysis, _ = index.analyze_bytes("x.vue", "typescript", source)
        self.assertNotEqual(analysis.lang, "vue")

    def test_remove_drops_deleted_symbols(self):
        with tempfile.TemporaryDirectory() as root:
            for name, source in (
                ("a.py", b"def one():\n    two()\n"),
                ("b.py", b"def two():\n    pass\n"),
            ):
                with open(os.path.join(root, name), "wb") as handle:
                    handle.write(source)
            index = ProjectIndex(root)
            self.assertEqual(index.prepare({"a.py", "b.py"}), 2)
            index.remove("b.py")
            analysis, _ = index.analyze_bytes(
                "a.py", "python", b"def one():\n    two()\n"
            )
            _, unresolved = resolver.resolve_file(analysis, index)
        self.assertTrue(any(item["raw_name"] == "two" for item in unresolved))

    def test_remove_directory_drops_descendant_files(self):
        files = {
            "pkg/a.py": b"def alpha():\n    pass\n",
            "pkg/sub/b.py": b"def beta():\n    pass\n",
            "pkg2/c.py": b"def gamma():\n    pass\n",
        }
        with tempfile.TemporaryDirectory() as root:
            for rel, source in files.items():
                full = os.path.join(root, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as handle:
                    handle.write(source)
            index = ProjectIndex(root)
            self.assertEqual(index.prepare(set(files)), 3)
            index.remove("pkg")
        self.assertEqual(index.file_paths, {"pkg2/c.py"})
        self.assertEqual(set(index.by_name), {"gamma"})


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (
  path TEXT PRIMARY KEY, lang TEXT NOT NULL, size INTEGER NOT NULL,
  mtime REAL NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE symbols (
  sym_key TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
  kind TEXT NOT NULL, signature TEXT, start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL
);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_key TEXT NOT NULL,
  to_key TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, confidence REAL NOT NULL,
  resolver TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE unresolved (
  path TEXT NOT NULL, raw_name TEXT NOT NULL, kind TEXT NOT NULL,
  line INTEGER, reason TEXT NOT NULL
);
"""


def _make_db(root: str, symbols, edges, files=None) -> str:
    path = os.path.join(root, ".agent-gateway", "graph.sqlite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO meta VALUES ('built_at', '2026-11-01T00:00:00Z')"
        )
        for value in files or []:
            connection.execute(
                "INSERT INTO files VALUES (?, ?, 10, 0, 'h')",
                (value, value.rsplit(".", 1)[-1]),
            )
        connection.executemany(
            "INSERT INTO symbols VALUES (?, ?, ?, ?, NULL, ?, ?)", symbols
        )
        connection.executemany(
            "INSERT INTO edges (from_key,to_key,kind,path,start_line,end_line,"
            "confidence,resolver,status) VALUES (?,?,?,?,?,?,?,?,?)",
            edges,
        )
        connection.commit()
    finally:
        connection.close()
    return path


class GraphToolRegressionTest(unittest.TestCase):
    def test_dotted_filename_lookup_hits_file(self):
        with tempfile.TemporaryDirectory() as root:
            _make_db(root, [], [], files=["docs/README.md"])
            result = graph_tools.tool_project_lookup(root, "README.md")
        self.assertEqual(result["results"][0]["kind"], "file")
        self.assertEqual(result["results"][0]["file"], "docs/README.md")

    def test_class_method_target_uses_owner_range(self):
        with tempfile.TemporaryDirectory() as root:
            _make_db(
                root,
                [
                    ("s1", "pkg/a.py", "A", "class", 1, 20),
                    ("s2", "pkg/a.py", "run", "method", 5, 8),
                    ("s3", "pkg/b.py", "B", "class", 1, 20),
                    ("s4", "pkg/b.py", "run", "method", 5, 8),
                ],
                [],
                files=["pkg/a.py", "pkg/b.py"],
            )
            result = graph_tools.tool_project_impact(root, "A.run")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["target"]["file"], "pkg/a.py")

    def test_both_direction_keeps_callee_with_limit(self):
        with tempfile.TemporaryDirectory() as root:
            _make_db(
                root,
                [
                    ("s1", "pkg/a.py", "target", "function", 1, 5),
                    ("s2", "pkg/a.py", "callee", "function", 10, 15),
                    ("s3", "pkg/a.py", "caller1", "function", 20, 25),
                    ("s4", "pkg/a.py", "caller2", "function", 30, 35),
                ],
                [
                    (
                        "sym:s2",
                        "sym:s1",
                        "call",
                        "pkg/a.py",
                        12,
                        12,
                        1.0,
                        "local",
                        "ok",
                    ),
                    (
                        "sym:s3",
                        "sym:s1",
                        "call",
                        "pkg/a.py",
                        22,
                        22,
                        0.9,
                        "local",
                        "ok",
                    ),
                    (
                        "sym:s4",
                        "sym:s1",
                        "call",
                        "pkg/a.py",
                        32,
                        32,
                        0.9,
                        "local",
                        "ok",
                    ),
                ],
                files=["pkg/a.py"],
            )
            result = graph_tools.tool_project_impact(
                root, "target", direction="both", limit=1
            )
        names = {
            item["symbol"] for group in result["groups"] for item in group["items"]
        }
        self.assertIn("callee", names)

    def test_callee_line_uses_symbol_start(self):
        with tempfile.TemporaryDirectory() as root:
            _make_db(
                root,
                [
                    ("s1", "pkg/a.py", "target", "function", 1, 5),
                    ("s2", "pkg/a.py", "callee", "function", 30, 35),
                ],
                [
                    ("sym:s1", "sym:s2", "call", "pkg/a.py", 3, 3, 1.0, "local", "ok"),
                ],
                files=["pkg/a.py"],
            )
            result = graph_tools.tool_project_impact(
                root, "target", direction="callees"
            )
        item = result["groups"][0]["items"][0]
        self.assertEqual(item["symbol"], "callee")
        self.assertEqual(item["line"], 30)

    def test_stale_edge_counted_once_across_directions(self):
        with tempfile.TemporaryDirectory() as root:
            _make_db(
                root,
                [
                    ("s1", "pkg/a.py", "T", "function", 1, 5),
                    ("s2", "pkg/a.py", "A", "function", 10, 15),
                    ("s3", "pkg/a.py", "B", "function", 20, 25),
                ],
                [
                    (
                        "sym:s3",
                        "sym:s1",
                        "call",
                        "pkg/a.py",
                        22,
                        22,
                        1.0,
                        "local",
                        "ok",
                    ),
                    ("sym:s1", "sym:s2", "call", "pkg/a.py", 3, 3, 1.0, "local", "ok"),
                    (
                        "sym:s2",
                        "sym:s3",
                        "call",
                        "pkg/a.py",
                        12,
                        12,
                        1.0,
                        "local",
                        "stale",
                    ),
                ],
                files=["pkg/a.py"],
            )
            result = graph_tools.tool_project_impact(
                root, "T", direction="both", depth=2, include_stale=True
            )
        self.assertEqual(result["stale"], 1)


if __name__ == "__main__":
    unittest.main()
