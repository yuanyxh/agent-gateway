"""Parser output for Kotlin sources."""

from __future__ import annotations

import unittest

from agent_gateway.services.graph.parsers import kotlin as kotlin_parser


class KotlinParserTest(unittest.TestCase):
    def test_extracts_symbols_and_references(self):
        source = b"""import com.example.util.Helper
import com.other.*

interface Greeter {
    fun greet(name: String): String
}

open class Base

class Foo(val helper: Helper) : Base(), Greeter {
    val count: Int = 0

    fun greet(name: String): String {
        helper.log(name)
        return name
    }
}

object Singleton {
    fun create(): Foo = Foo(Helper())
}

enum class Color {
    RED, GREEN
}

typealias Names = List<String>

fun top(x: Int): Int = helper(x)
"""
        analysis = kotlin_parser.parse(source, "src/Foo.kt")
        symbols = {symbol.name: symbol for symbol in analysis.symbols}
        self.assertEqual(symbols["Greeter"].kind, "interface")
        self.assertEqual(symbols["Base"].kind, "class")
        self.assertEqual(symbols["Foo"].kind, "class")
        self.assertEqual(symbols["Foo"].start_line, 10)
        self.assertEqual(symbols["count"].kind, "property")
        self.assertEqual(symbols["Singleton"].kind, "object")
        self.assertEqual(symbols["Color"].kind, "enum")
        self.assertEqual(symbols["Names"].kind, "typealias")
        self.assertEqual(symbols["top"].kind, "function")
        self.assertEqual(symbols["create"].kind, "method")
        self.assertEqual(
            [symbol.kind for symbol in analysis.symbols if symbol.name == "greet"],
            ["method", "method"],
        )

        references = [(ref.raw_name, ref.kind, ref.line) for ref in analysis.references]
        self.assertIn(("Base", "extends", 10), references)
        self.assertIn(("Greeter", "extends", 10), references)
        self.assertIn(("log", "call", 14), references)
        self.assertIn(("helper", "call", 29), references)
        self.assertIn(("Foo", "call", 20), references)
        self.assertIn(("Helper", "type_ref", 10), references)
        self.assertIn(("Foo", "type_ref", 20), references)

        receivers = {ref.raw_name: ref.receiver for ref in analysis.references}
        self.assertEqual(receivers["log"], "helper")
        self.assertIsNone(receivers["helper"])

        self.assertEqual(
            [item.module for item in analysis.imports],
            ["com.example.util.Helper", "com.other"],
        )
        self.assertTrue(all(item.names == () for item in analysis.imports))

    def test_builtin_types_are_excluded(self):
        source = b"""class Holder {
    val items: List<String> = listOf()
    fun map(values: Map<String, Int>): Set<Long> = setOf()
}
"""
        analysis = kotlin_parser.parse(source, "src/Holder.kt")
        raw_names = {ref.raw_name for ref in analysis.references}
        for builtin in ("String", "Int", "Long", "List", "Map", "Set"):
            self.assertNotIn(builtin, raw_names)

    def test_nested_call_uses_root_receiver(self):
        source = b"""fun walk() {
    a.b.c.d()
}
"""
        analysis = kotlin_parser.parse(source, "src/Walk.kt")
        call = next(ref for ref in analysis.references if ref.kind == "call")
        self.assertEqual(call.raw_name, "d")
        self.assertEqual(call.receiver, "a")

    def test_property_getter_signature_stops_before_getter(self):
        source = b"""class Holder {
    val derived: List<String> get() = listOf("a")
}
"""
        analysis = kotlin_parser.parse(source, "src/Holder.kt")
        symbol = next(item for item in analysis.symbols if item.name == "derived")
        self.assertEqual(symbol.kind, "property")
        self.assertEqual(symbol.signature, "val derived: List<String>")

    def test_nested_class_encloses_methods(self):
        source = b"""class Outer {
    class Inner {
        fun m() {}
    }
}
"""
        analysis = kotlin_parser.parse(source, "src/Outer.kt")
        symbols = {symbol.name: symbol for symbol in analysis.symbols}
        self.assertEqual(symbols["Inner"].kind, "class")
        self.assertEqual(symbols["m"].kind, "method")
        self.assertEqual(symbols["m"].start_line, 3)

    def test_error_nodes_are_tolerated(self):
        analysis = kotlin_parser.parse(b"class { fun broken(:\n", "src/a.kt")
        self.assertEqual(analysis.path, "src/a.kt")
        self.assertEqual(analysis.lang, "kotlin")


if __name__ == "__main__":
    unittest.main()
