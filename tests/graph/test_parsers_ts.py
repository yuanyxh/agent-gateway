"""TypeScript/JavaScript and Vue adapters."""

from __future__ import annotations

import unittest

from agent_gateway.services.graph.parsers import typescript, vue

TS_SOURCE = b"""import { foo } from "./foo";
import * as bar from "./bar";
export class A extends B {
  run() { foo(); bar.baz(); this.x(); new C(); }
}
export function fn(x: string): number { return 1; }
const arrow = () => {};
interface I { m(): void }
type T = string;
export { run } from "./core";
"""


class TypeScriptTest(unittest.TestCase):
    def test_extracts_symbols_and_references(self):
        analysis = typescript.parse(TS_SOURCE, "src/a.ts")
        names = {(item.name, item.kind) for item in analysis.symbols}
        self.assertIn(("A", "class"), names)
        self.assertIn(("run", "method"), names)
        self.assertIn(("fn", "function"), names)
        self.assertIn(("arrow", "function"), names)
        self.assertIn(("I", "interface"), names)
        self.assertIn(("T", "typealias"), names)

        refs = {
            (item.raw_name, item.kind, item.receiver) for item in analysis.references
        }
        self.assertIn(("B", "extends", None), refs)
        self.assertIn(("foo", "call", None), refs)
        self.assertIn(("baz", "call", "bar"), refs)
        self.assertIn(("C", "type_ref", None), refs)

    def test_imports_and_re_exports(self):
        analysis = typescript.parse(TS_SOURCE, "src/a.ts")
        imports = {(item.module, item.names) for item in analysis.imports}
        self.assertIn(("./foo", ("foo",)), imports)
        self.assertIn(("./bar", ()), imports)
        self.assertIn(("./core", ("run",)), imports)

    def test_commonjs_export_references(self):
        source = b"""module.exports = createApplication;
exports.helper = helper;
exports.nested = utils.deep;
exports.skipped = () => {};
module.exports.other = other;
"""
        analysis = typescript.parse(source, "lib/app.js")
        exports = [
            (item.raw_name, item.kind, item.line, item.receiver)
            for item in analysis.references
            if item.kind == "export"
        ]
        self.assertEqual(
            exports,
            [
                ("createApplication", "export", 1, None),
                ("helper", "export", 2, None),
                ("deep", "export", 3, None),
            ],
        )


VUE_SOURCE = b"""<template><div/></template>
<script lang="ts">
import { x } from "./x";
export function f() { x(); }
</script>
"""


VUE_COMPONENT_SOURCE = b"""<template>
  <UserCard :title="msg" />
  <UserCard />
  <div>plain</div>
</template>
<script lang="ts">
import UserCard from "./UserCard.vue";
const msg = "hi";
</script>
"""


class VueTest(unittest.TestCase):
    def test_script_lines_are_shifted_to_file_lines(self):
        analysis = vue.parse(VUE_SOURCE, "src/C.vue")
        self.assertEqual(analysis.lang, "vue")
        by_name = {item.name: item for item in analysis.symbols}
        self.assertEqual(by_name["f"].start_line, 4)
        self.assertEqual(by_name["C"].kind, "component")
        self.assertEqual(analysis.references[0].line, 4)
        self.assertEqual(analysis.imports[0].line, 3)

    def test_component_without_script_has_component_symbol(self):
        analysis = vue.parse(b"<template><div/></template>\n", "src/D.vue")
        symbols = [
            (item.name, item.kind, item.start_line, item.end_line)
            for item in analysis.symbols
        ]
        self.assertEqual(symbols, [("D", "component", 1, 1)])

    def test_template_component_references_are_unique_and_line_aware(self):
        analysis = vue.parse(VUE_COMPONENT_SOURCE, "src/App.vue")
        refs = [
            (item.raw_name, item.kind, item.line)
            for item in analysis.references
            if item.kind == "type_ref"
        ]
        self.assertEqual(refs, [("UserCard", "type_ref", 2)])

    def test_component_symbol_is_added_from_filename(self):
        analysis = vue.parse(VUE_COMPONENT_SOURCE, "src/App.vue")
        symbol = {item.name: item for item in analysis.symbols}["App"]
        self.assertEqual(symbol.kind, "component")
        self.assertEqual(symbol.signature, "src/App.vue")
        self.assertEqual((symbol.start_line, symbol.end_line), (1, 1))

    def test_component_symbol_is_not_duplicated_from_script(self):
        source = (
            b"<template><div/></template>\n"
            b"<script>\nfunction UserCard() {}\n</script>\n"
        )
        analysis = vue.parse(source, "src/UserCard.vue")
        matches = [item for item in analysis.symbols if item.name == "UserCard"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].kind, "function")

    def test_template_comment_tags_are_ignored(self):
        source = b"<template>\n<!-- <Hidden/> -->\n<Visible/>\n</template>\n"
        analysis = vue.parse(source, "src/B.vue")
        names = [item.raw_name for item in analysis.references]
        self.assertEqual(names, ["Visible"])

    def test_template_event_handlers_become_calls(self):
        source = (
            b"<template>\n"
            b'<button @click="handleLogin">x</button>\n'
            b'<input @change="onChange($event)" />\n'
            b'<span v-on:keyup.enter="submit" />\n'
            b'<a @click="a.b()" />\n'
            b'<div @click="flag = true" />\n'
            b"</template>\n"
        )
        analysis = vue.parse(source, "src/L.vue")
        calls = [
            (item.raw_name, item.line)
            for item in analysis.references
            if item.kind == "call"
        ]
        self.assertEqual(calls, [("handleLogin", 2), ("onChange", 3), ("submit", 4)])


if __name__ == "__main__":
    unittest.main()
