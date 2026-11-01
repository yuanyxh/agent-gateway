"""TypeScript and JavaScript adapters for the graph indexer, built on tree-sitter."""

from __future__ import annotations

import logging

from tree_sitter import Language, Node, Parser

import tree_sitter_javascript
import tree_sitter_typescript

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

_LANGUAGES = {
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
    "javascript": Language(tree_sitter_javascript.language()),
}
_PARSERS = {name: Parser(language) for name, language in _LANGUAGES.items()}

TYPE_KINDS = frozenset({"class", "interface", "enum", "typealias"})

TYPE_NODES = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "type_alias_declaration": "typealias",
}

BUILTIN_TYPES = frozenset(
    {
        "Array",
        "Boolean",
        "Date",
        "Error",
        "Function",
        "Map",
        "Number",
        "Object",
        "Promise",
        "Record",
        "RegExp",
        "Set",
        "String",
        "Symbol",
        "any",
        "boolean",
        "never",
        "null",
        "number",
        "object",
        "string",
        "undefined",
        "unknown",
        "void",
    }
)

BUILTIN_CALLS = frozenset(
    {
        "String",
        "Number",
        "Boolean",
        "require",
        "console",
        "parseInt",
        "parseFloat",
        "isNaN",
        "setTimeout",
        "setInterval",
        "clearTimeout",
        "clearInterval",
        "encodeURIComponent",
        "decodeURIComponent",
        "JSON",
        "Object",
    }
)


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _compact(text: str) -> str:
    return " ".join(text.split())


def _position(node: Node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _named(node: Node, kind: str) -> Node | None:
    for child in node.named_children:
        if child.type == kind:
            return child
    return None


def _header(source: bytes, node: Node) -> str:
    for child in node.named_children:
        if child.type in (
            "class_body",
            "interface_body",
            "enum_body",
            "statement_block",
        ):
            return _compact(
                source[node.start_byte : child.start_byte].decode("utf-8", "replace")
            )
    return _compact(source[node.start_byte : node.end_byte].decode("utf-8", "replace"))


def _type_name(node: Node, source: bytes) -> str | None:
    for child in node.named_children:
        if child.type in ("type_identifier", "identifier"):
            return _text(source, child)
    return None


def _call_name(node: Node, source: bytes) -> str | None:
    if node.type in ("identifier", "property_identifier"):
        return _text(source, node)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return _text(source, prop) if prop is not None else None
    for child in node.named_children:
        found = _call_name(child, source)
        if found:
            return found
    return None


def _call_receiver(node: Node, source: bytes) -> str | None:
    """Root object of a member call, e.g. bar in bar.baz()."""
    if node.type != "member_expression":
        return None
    value = node.child_by_field_name("object")
    while value is not None and value.type == "member_expression":
        value = value.child_by_field_name("object")
    if value is None:
        return None
    if value.type in ("identifier", "this"):
        return _text(source, value)
    return None


def _base_type_name(node: Node, source: bytes) -> str | None:
    """Base name of a type node; generic arguments are not separate types."""
    if node.type in ("type_identifier", "identifier"):
        return _text(source, node)
    if node.type == "generic_type":
        name = node.child_by_field_name("name")
        if name is not None:
            return _text(source, name)
    return None


def _add_type_refs(
    node: Node,
    source: bytes,
    analysis: FileAnalysis,
    enclosing: SymbolDef | None,
) -> None:
    for child in node.named_children:
        if child.type == "type_identifier":
            raw = _text(source, child)
            if (
                raw in BUILTIN_TYPES
                or raw in analysis.type_params
                or (len(raw) == 1 and raw.isupper())
            ):
                continue
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="type_ref",
                    line=child.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                )
            )
        elif child.type != "type_parameters":
            _add_type_refs(child, source, analysis, enclosing)


def _is_commonjs_export_target(node: Node, source: bytes) -> bool:
    """Return True for `module.exports` and `exports.<name>` left sides."""
    if node.type != "member_expression":
        return False
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    if obj is None or prop is None or obj.type != "identifier":
        return False
    obj_name = _text(source, obj)
    if obj_name == "module":
        return _text(source, prop) == "exports"
    return obj_name == "exports"


def _export_value_name(node: Node, source: bytes) -> str | None:
    """Last identifier segment of an exported value, or None."""
    if node.type == "identifier":
        return _text(source, node)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        if prop is None or prop.type not in ("property_identifier", "identifier"):
            return None
        return _text(source, prop)
    return None


def _type_params(node: Node, source: bytes) -> tuple[set[str], list[Node]]:
    params = _named(node, "type_parameters")
    if params is None:
        return set(), []
    names: set[str] = set()
    constraints: list[Node] = []
    for child in params.named_children:
        if child.type != "type_parameter":
            continue
        name = child.child_by_field_name("name") or _named(child, "type_identifier")
        if name is not None:
            names.add(_text(source, name))
        constraint = child.child_by_field_name("constraint") or _named(
            child, "constraint"
        )
        if constraint is not None:
            constraints.append(constraint)
        default = _named(child, "default_type")
        if default is not None:
            constraints.append(default)
    return names, constraints


def _add_symbol(
    analysis: FileAnalysis,
    name: str,
    kind: str,
    signature: str,
    node: Node,
) -> SymbolDef:
    start, end = _position(node)
    symbol = SymbolDef(
        path=analysis.path,
        name=name,
        kind=kind,
        signature=signature,
        start_line=start,
        end_line=end,
    )
    analysis.symbols.append(symbol)
    return symbol


def _string_value(node: Node, source: bytes) -> str:
    fragment = _named(node, "string_fragment")
    return _text(source, fragment) if fragment is not None else ""


def _add_import(node: Node, source: bytes, analysis: FileAnalysis) -> None:
    module_node = None
    for child in node.named_children:
        if child.type == "string":
            module_node = child
    if module_node is None:
        return
    module = _string_value(module_node, source)
    names: list[str] = []
    aliases: list[tuple[str, str]] = []
    clause = _named(node, "import_clause")
    if clause is not None:
        for spec in clause.named_children:
            if spec.type == "identifier":
                names.append(_text(source, spec))
            elif spec.type == "namespace_import":
                local = _named(spec, "identifier")
                if local is not None:
                    aliases.append((_text(source, local), "*"))
            elif spec.type == "named_imports":
                for item in spec.named_children:
                    if item.type != "import_specifier":
                        continue
                    name = item.child_by_field_name("name") or _named(
                        item, "identifier"
                    )
                    if name is None:
                        continue
                    original = _text(source, name)
                    names.append(original)
                    alias = item.child_by_field_name("alias")
                    if alias is not None:
                        aliases.append((_text(source, alias), original))
    analysis.imports.append(
        ImportRef(
            module=module,
            names=tuple(names),
            line=node.start_point[0] + 1,
            aliases=tuple(aliases),
        )
    )


def _add_export_source(node: Node, source: bytes, analysis: FileAnalysis) -> None:
    module_node = None
    names: list[str] = []
    aliases: list[tuple[str, str]] = []
    for child in node.named_children:
        if child.type == "string":
            module_node = child
        elif child.type == "export_clause":
            for item in child.named_children:
                if item.type != "export_specifier":
                    continue
                name = item.child_by_field_name("name") or _named(item, "identifier")
                if name is None:
                    continue
                original = _text(source, name)
                names.append(original)
                alias = item.child_by_field_name("alias")
                if alias is not None:
                    aliases.append((_text(source, alias), original))
    if module_node is None:
        return
    analysis.imports.append(
        ImportRef(
            module=_string_value(module_node, source),
            names=tuple(names),
            line=node.start_point[0] + 1,
            aliases=tuple(aliases),
        )
    )


def _visit(
    node: Node,
    source: bytes,
    analysis: FileAnalysis,
    enclosing: SymbolDef | None,
) -> None:
    kind = node.type

    if kind == "import_statement":
        _add_import(node, source, analysis)
        return

    if kind == "export_statement":
        has_source = any(child.type == "string" for child in node.named_children)
        if has_source:
            _add_export_source(node, source, analysis)
            return
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind in TYPE_NODES:
        name = _type_name(node, source)
        if name:
            symbol = _add_symbol(
                analysis, name, TYPE_NODES[kind], _header(source, node), node
            )
            names, constraints = _type_params(node, source)
            previous = set(analysis.type_params)
            analysis.type_params |= names
            for constraint in constraints:
                _add_type_refs(constraint, source, analysis, symbol)
            for child in node.named_children:
                _visit(child, source, analysis, symbol)
            analysis.type_params = previous
        return

    if kind == "function_declaration":
        name_node = _named(node, "identifier")
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol_kind = (
            "method"
            if enclosing is not None and enclosing.kind in TYPE_KINDS
            else "function"
        )
        symbol = _add_symbol(analysis, name, symbol_kind, _header(source, node), node)
        names, constraints = _type_params(node, source)
        previous = set(analysis.type_params)
        analysis.type_params |= names
        for constraint in constraints:
            _add_type_refs(constraint, source, analysis, symbol)
        for child in node.named_children:
            _visit(child, source, analysis, symbol)
        analysis.type_params = previous
        return

    if kind in ("method_definition", "method_signature", "abstract_method_signature"):
        name_node = _named(node, "property_identifier")
        name = _text(source, name_node) if name_node else "(anonymous)"
        names, constraints = _type_params(node, source)
        previous = set(analysis.type_params)
        analysis.type_params |= names
        for constraint in constraints:
            _add_type_refs(constraint, source, analysis, enclosing)
        if enclosing is not None and enclosing.kind in TYPE_KINDS:
            symbol = _add_symbol(analysis, name, "method", _header(source, node), node)
            for child in node.named_children:
                _visit(child, source, analysis, symbol)
        else:
            # Object literal or class expression value: traverse without a symbol.
            for child in node.named_children:
                _visit(child, source, analysis, enclosing)
        analysis.type_params = previous
        return

    if kind == "variable_declarator":
        name_node = _named(node, "identifier")
        value = node.child_by_field_name("value")
        if (
            name_node is not None
            and value is not None
            and value.type == "call_expression"
        ):
            func = value.child_by_field_name("function")
            if func is not None and _text(source, func) == "require":
                args = _named(value, "arguments")
                if args is not None and args.named_children:
                    first = args.named_children[0]
                    if first.type == "string":
                        analysis.imports.append(
                            ImportRef(
                                module=_string_value(first, source),
                                names=(_text(source, name_node),),
                                line=node.start_point[0] + 1,
                                kind="require",
                            )
                        )
        top_level = enclosing is None or enclosing.kind in TYPE_KINDS
        is_function = value is not None and value.type in (
            "arrow_function",
            "function_expression",
            "function",
        )
        scope = enclosing
        if top_level and is_function and name_node is not None:
            scope = _add_symbol(
                analysis,
                _text(source, name_node),
                "function",
                _header(source, node),
                node,
            )
        names, constraints = (
            _type_params(value, source) if value is not None else (set(), [])
        )
        previous = set(analysis.type_params)
        analysis.type_params |= names
        for constraint in constraints:
            _add_type_refs(constraint, source, analysis, scope)
        # Visit the initializer: calls inside arrow-function bodies are the main
        # reference source for TS/Vue script setup.
        for child in node.named_children:
            _visit(child, source, analysis, scope)
        analysis.type_params = previous
        return

    if kind == "assignment_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if (
            left is not None
            and right is not None
            and _is_commonjs_export_target(left, source)
        ):
            exported = _export_value_name(right, source)
            if exported:
                analysis.references.append(
                    Reference(
                        raw_name=exported,
                        kind="export",
                        line=node.start_point[0] + 1,
                        receiver=None,
                    )
                )
        name = _call_name(left, source) if left is not None else None
        top_level = enclosing is None or enclosing.kind in TYPE_KINDS
        if name and name != "exports" and right is not None and top_level:
            symbol_kind = (
                "method"
                if right.type in ("arrow_function", "function", "function_expression")
                else "property"
            )
            _add_symbol(analysis, name, symbol_kind, _header(source, node), node)
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind == "call_expression":
        func = node.child_by_field_name("function")
        raw = _call_name(func, source) if func is not None else None
        if raw and raw not in BUILTIN_CALLS:
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="call",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                    receiver=_call_receiver(func, source) if func is not None else None,
                )
            )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind == "new_expression":
        ctor = node.child_by_field_name("constructor")
        raw = _text(source, ctor) if ctor is not None else None
        if raw and raw not in BUILTIN_TYPES:
            analysis.references.append(
                Reference(
                    raw_name=raw.split(".")[-1],
                    kind="type_ref",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                )
            )

    if kind in ("extends_clause", "implements_clause", "extends_type_clause"):
        for child in node.named_children:
            if child.type == "type_arguments":
                _add_type_refs(child, source, analysis, enclosing)
                continue
            raw = _base_type_name(child, source)
            if raw and raw not in BUILTIN_TYPES:
                analysis.references.append(
                    Reference(
                        raw_name=raw,
                        kind="extends",
                        line=child.start_point[0] + 1,
                        from_name=enclosing.name if enclosing else None,
                        from_line=enclosing.start_line if enclosing else None,
                    )
                )
            arguments = _named(child, "type_arguments")
            if arguments is not None:
                _add_type_refs(arguments, source, analysis, enclosing)
        return

    if kind == "type_annotation":
        _add_type_refs(node, source, analysis, enclosing)
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    for child in node.named_children:
        _visit(child, source, analysis, enclosing)


def _parse_with(language: str, source: bytes, path: str) -> FileAnalysis:
    tree = _PARSERS[language].parse(source)
    if tree.root_node.has_error:
        logging.getLogger("graph").warning(
            "partial parse: %s (tree-sitter error nodes)", path
        )
    analysis = FileAnalysis(path=path, lang=language)
    _visit(tree.root_node, source, analysis, None)
    return analysis


def _syntax_for(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".tsx", ".jsx")):
        return "tsx"
    if lower.endswith(".ts"):
        return "typescript"
    return "javascript"


def parse(source: bytes, path: str) -> FileAnalysis:
    return _parse_with(_syntax_for(path), source, path)


def parse_script(source: bytes, path: str, syntax: str | None = None) -> FileAnalysis:
    """Parse a Vue script block with an explicit grammar chosen from `lang`."""
    if syntax not in _PARSERS:
        syntax = _syntax_for(path)
    return _parse_with(syntax, source, path)
