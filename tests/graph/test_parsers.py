"""Parser output for Python and Swift sources."""

from __future__ import annotations

import unittest

from agent_gateway.services.graph.parsers import python as python_parser
from agent_gateway.services.graph.parsers import swift as swift_parser


class PythonParserTest(unittest.TestCase):
    def test_extracts_symbols_and_references(self):
        source = b"""from pkg.mod import Thing

class Foo(Thing):
    value: int = 1

    def bar(self, x: int) -> str:
        return helper(x)

def helper(x):
    return Thing()
"""
        analysis = python_parser.parse(source, "pkg/a.py")
        symbols = {symbol.name: symbol for symbol in analysis.symbols}
        self.assertEqual(symbols["Foo"].kind, "class")
        self.assertEqual(symbols["bar"].kind, "method")
        self.assertEqual(symbols["helper"].kind, "function")
        self.assertEqual(symbols["value"].kind, "variable")
        self.assertEqual(symbols["Foo"].start_line, 3)
        self.assertIn(
            ("Thing", "extends", 3),
            [(r.raw_name, r.kind, r.line) for r in analysis.references],
        )
        self.assertIn(
            ("helper", "call", 7),
            [(r.raw_name, r.kind, r.line) for r in analysis.references],
        )
        self.assertEqual(analysis.imports[0].module, "pkg.mod")
        self.assertEqual(analysis.imports[0].names, ("Thing",))

    def test_relative_import_level(self):
        analysis = python_parser.parse(b"from . import sibling\n", "pkg/a.py")
        self.assertEqual(analysis.imports[0].level, 1)
        self.assertEqual(analysis.imports[0].module, "")
        self.assertEqual(analysis.imports[0].names, ("sibling",))

    def test_error_nodes_are_tolerated(self):
        analysis = python_parser.parse(b"def broken(:\n", "pkg/a.py")
        self.assertEqual(analysis.path, "pkg/a.py")


class SwiftParserTest(unittest.TestCase):
    def test_extracts_symbols_and_references(self):
        source = b"""import Foundation

protocol P {}

class Foo: P {
    func bar(x: Int) -> String {
        return helper(x)
    }
}

func helper(x: Int) -> Int {
    return x
}
"""
        analysis = swift_parser.parse(source, "src/a.swift")
        symbols = {symbol.name: symbol for symbol in analysis.symbols}
        self.assertEqual(symbols["P"].kind, "protocol")
        self.assertEqual(symbols["Foo"].kind, "class")
        self.assertEqual(symbols["bar"].kind, "method")
        self.assertEqual(symbols["helper"].kind, "function")
        self.assertIn(
            ("P", "extends", 5),
            [(r.raw_name, r.kind, r.line) for r in analysis.references],
        )
        self.assertIn(
            ("helper", "call", 7),
            [(r.raw_name, r.kind, r.line) for r in analysis.references],
        )
        self.assertEqual(analysis.imports[0].module, "Foundation")

    def test_error_nodes_are_tolerated(self):
        analysis = swift_parser.parse(b"class {", "src/a.swift")
        self.assertEqual(analysis.path, "src/a.swift")


if __name__ == "__main__":
    unittest.main()
