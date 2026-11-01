"""Kotlin adapter for the graph indexer, built on tree-sitter."""

from __future__ import annotations

import logging

from tree_sitter import Language, Node, Parser

import tree_sitter_kotlin

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

_language = Language(tree_sitter_kotlin.language())
_parser = Parser(_language)

TYPE_KINDS = frozenset({"class", "interface", "object", "enum"})

BUILTIN_CALLS = frozenset(
    {
        "arrayOf",
        "check",
        "checkNotNull",
        "emptyList",
        "emptyMap",
        "emptySet",
        "error",
        "lazy",
        "listOf",
        "mapOf",
        "mutableListOf",
        "mutableMapOf",
        "mutableSetOf",
        "print",
        "println",
        "repeat",
        "require",
        "requireNotNull",
        "sequenceOf",
        "setOf",
    }
)

BUILTIN_TYPES = frozenset(
    {
        "Any",
        "Array",
        "Boolean",
        "BooleanArray",
        "Byte",
        "ByteArray",
        "Char",
        "CharArray",
        "CharSequence",
        "Collection",
        "Comparable",
        "Double",
        "DoubleArray",
        "Float",
        "FloatArray",
        "Int",
        "IntArray",
        "Iterable",
        "Iterator",
        "List",
        "Long",
        "LongArray",
        "Map",
        "MutableCollection",
        "MutableList",
        "MutableMap",
        "MutableSet",
        "Nothing",
        "Number",
        "Pair",
        "Result",
        "Sequence",
        "Set",
        "Short",
        "ShortArray",
        "String",
        "Triple",
        "UByte",
        "UInt",
        "ULong",
        "UShort",
        "Unit",
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


def _same_node(left: Node | None, right: Node | None) -> bool:
    if left is None or right is None:
        return False
    return (
        left.start_byte == right.start_byte
        and left.end_byte == right.end_byte
        and left.type == right.type
    )


def _contains_token(node: Node, token: str) -> bool:
    if node.type == token:
        return True
    return any(_contains_token(child, token) for child in node.children)


def _has_modifier(node: Node, token: str) -> bool:
    modifiers = _named(node, "modifiers")
    if modifiers is None:
        return False
    return _contains_token(modifiers, token)


def _declaration_kind(node: Node) -> str:
    if any(child.type == "interface" for child in node.children):
        return "interface"
    if _has_modifier(node, "enum"):
        return "enum"
    return "class"


def _name_node(node: Node) -> Node | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return name
    for child in node.named_children:
        if child.type == "identifier":
            return child
    return None


def _first_identifier(node: Node, source: bytes) -> str | None:
    if node.type == "identifier":
        return _text(source, node)
    for child in node.named_children:
        found = _first_identifier(child, source)
        if found:
            return found
    return None


def _last_identifier(node: Node, source: bytes) -> str | None:
    if node.type == "identifier":
        return _text(source, node)
    if node.named_children:
        return _last_identifier(node.named_children[-1], source)
    return None


def _call_receiver(node: Node, source: bytes) -> str | None:
    """Root receiver of a member call, e.g. helper in helper.log(name)."""
    if node.type != "navigation_expression":
        return None
    current = node
    while current.type == "navigation_expression" and current.named_children:
        current = current.named_children[0]
    if current.type == "identifier":
        return _text(source, current)
    return None


def _body_start(node: Node) -> int | None:
    for child in node.named_children:
        if child.type in ("class_body", "enum_class_body", "function_body"):
            return child.start_byte
    return None


def _property_end(node: Node) -> int | None:
    end = None
    for child in node.children:
        if child.type in ("getter", "setter"):
            return child.start_byte
        if not child.is_named and child.type == "=" and end is None:
            end = child.start_byte
    return end


def _header(source: bytes, node: Node, end_byte: int | None) -> str:
    end = end_byte if end_byte is not None else node.end_byte
    return _compact(source[node.start_byte : end].decode("utf-8", "replace"))


def _add_type_refs(
    node: Node,
    source: bytes,
    analysis: FileAnalysis,
    enclosing: SymbolDef | None,
) -> None:
    for child in node.named_children:
        if child.type == "identifier":
            raw = _text(source, child)
            if raw in BUILTIN_TYPES:
                continue
            if len(raw) == 1 and raw.isupper():
                # Single-letter generics (T, K, V) are type parameters, not symbols.
                continue
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="type_ref",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                )
            )
        else:
            _add_type_refs(child, source, analysis, enclosing)


def _delegation_name(node: Node, source: bytes) -> str | None:
    user_type = _named(node, "user_type")
    if user_type is None:
        invocation = _named(node, "constructor_invocation")
        if invocation is not None:
            user_type = _named(invocation, "user_type")
    if user_type is None:
        return None
    return _first_identifier(user_type, source)


def _add_import(node: Node, source: bytes, analysis: FileAnalysis) -> None:
    module_node = _named(node, "qualified_identifier")
    if module_node is None:
        return
    module = _text(source, module_node)
    if not module:
        return
    alias_node = _named(node, "import_alias")
    aliases: tuple[tuple[str, str], ...] = ()
    if alias_node is not None:
        alias = _first_identifier(alias_node, source)
        if alias:
            aliases = ((alias, module.rsplit(".", 1)[-1]),)
    analysis.imports.append(
        ImportRef(
            module=module,
            names=(),
            line=node.start_point[0] + 1,
            aliases=aliases,
        )
    )


def _add_symbol(
    analysis: FileAnalysis,
    name: str,
    kind: str,
    signature: str | None,
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


def _function_kind(enclosing: SymbolDef | None) -> str:
    if enclosing is not None and enclosing.kind in TYPE_KINDS:
        return "method"
    return "function"


def _visit(
    node: Node,
    source: bytes,
    analysis: FileAnalysis,
    enclosing: SymbolDef | None,
) -> None:
    kind = node.type

    if kind in ("import", "import_header"):
        _add_import(node, source, analysis)
        return

    if kind == "companion_object":
        name_node = _named(node, "identifier")
        name = _text(source, name_node) if name_node else "Companion"
        symbol = _add_symbol(
            analysis,
            name,
            "object",
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            if _same_node(child, name_node):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind in ("class_declaration", "object_declaration"):
        decl_kind = _declaration_kind(node) if kind == "class_declaration" else "object"
        name_node = _name_node(node)
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol = _add_symbol(
            analysis,
            name,
            decl_kind,
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            if _same_node(child, name_node):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind == "function_declaration":
        name_node = _name_node(node)
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol = _add_symbol(
            analysis,
            name,
            _function_kind(enclosing),
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            if _same_node(child, name_node):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind == "property_declaration":
        declaration = _named(node, "variable_declaration")
        name_node = (
            _named(declaration, "identifier") if declaration is not None else None
        )
        name = _text(source, name_node) if name_node else "(anonymous)"
        _add_symbol(
            analysis,
            name,
            "property",
            _header(source, node, _property_end(node)),
            node,
        )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind == "type_alias":
        name_node = node.child_by_field_name("type")
        if name_node is None:
            name_node = _named(node, "identifier")
        name = _text(source, name_node) if name_node else "(anonymous)"
        _add_symbol(analysis, name, "typealias", _header(source, node, None), node)
        return

    if kind in ("delegation_specifier", "extends"):
        raw = _delegation_name(node, source)
        if raw and raw not in BUILTIN_TYPES:
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="extends",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                )
            )
        return

    if kind == "user_type":
        _add_type_refs(node, source, analysis, enclosing)
        return

    if kind == "call_expression":
        func = node.named_children[0] if node.named_children else None
        raw = _last_identifier(func, source) if func is not None else None
        receiver = _call_receiver(func, source) if func is not None else None
        # Only bare stdlib calls are filtered; obj.run() is a project call.
        if raw and (receiver is not None or raw not in BUILTIN_CALLS):
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="call",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                    receiver=receiver,
                )
            )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    for child in node.named_children:
        _visit(child, source, analysis, enclosing)


def parse(source: bytes, path: str) -> FileAnalysis:
    tree = _parser.parse(source)
    if tree.root_node.has_error:
        logging.getLogger("graph").warning(
            "partial parse: %s (tree-sitter error nodes)", path
        )
    analysis = FileAnalysis(path=path, lang="kotlin")
    _visit(tree.root_node, source, analysis, None)
    return analysis
