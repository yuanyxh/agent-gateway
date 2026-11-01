"""Parser output for Java sources."""

from __future__ import annotations

import unittest

from agent_gateway.services.graph.parsers import java as java_parser


class JavaParserTest(unittest.TestCase):
    def test_extracts_symbols_and_references(self):
        source = b"""package com.example.demo;

import java.util.List;
import java.util.Map;

class Foo extends Base implements Runnable {
    private String name;
    protected List<String> items;

    Foo(String name) {
        this.name = name;
    }

    void run() {
        helper(this.name);
        this.name.trim();
        Bar bar = new Bar();
        registry.helper();
        a.b.c.doSomething();
    }
}

interface Sub extends Parent { void go(); }
enum Color { RED }
record Point(int x, int y) {}
"""
        analysis = java_parser.parse(source, "src/Foo.java")
        declared = [(symbol.name, symbol.kind) for symbol in analysis.symbols]
        self.assertIn(("Foo", "class"), declared)
        self.assertIn(("Foo", "initializer"), declared)
        self.assertIn(("name", "property"), declared)
        self.assertIn(("items", "property"), declared)
        self.assertIn(("run", "method"), declared)
        self.assertIn(("Sub", "interface"), declared)
        self.assertIn(("go", "method"), declared)
        self.assertIn(("Color", "enum"), declared)
        self.assertIn(("Point", "record"), declared)
        class_symbol = next(
            symbol
            for symbol in analysis.symbols
            if symbol.name == "Foo" and symbol.kind == "class"
        )
        self.assertEqual(class_symbol.start_line, 6)

        refs = [(r.raw_name, r.kind, r.line, r.receiver) for r in analysis.references]
        self.assertIn(("Base", "extends", 6, None), refs)
        self.assertIn(("Parent", "extends", 23, None), refs)
        self.assertIn(("helper", "call", 15, None), refs)
        self.assertIn(("trim", "call", 16, "this"), refs)
        self.assertIn(("Bar", "type_ref", 17, None), refs)
        self.assertIn(("helper", "call", 18, "registry"), refs)
        self.assertIn(("doSomething", "call", 19, "a"), refs)
        self.assertEqual(analysis.imports[0].module, "java.util.List")
        self.assertEqual(analysis.imports[0].names, ())

    def test_imports_keep_full_module_name(self):
        source = b"""import java.util.List;
import java.util.*;
import static java.util.Collections.sort;
"""
        analysis = java_parser.parse(source, "src/A.java")
        self.assertEqual(
            [item.module for item in analysis.imports],
            ["java.util.List", "java.util", "java.util.Collections.sort"],
        )
        for item in analysis.imports:
            self.assertEqual(item.names, ())

    def test_builtin_types_are_excluded(self):
        source = b"""class Foo implements Runnable {
    String label = new String();
    void run() {
        List<Map> values = null;
    }
}
"""
        analysis = java_parser.parse(source, "src/A.java")
        names = {ref.raw_name for ref in analysis.references}
        self.assertFalse(names & {"String", "List", "Map", "Runnable"})

    def test_error_nodes_are_tolerated(self):
        analysis = java_parser.parse(b"class {", "src/A.java")
        self.assertEqual(analysis.path, "src/A.java")
        self.assertEqual(analysis.lang, "java")


if __name__ == "__main__":
    unittest.main()
