"""Swift adapter for the graph indexer, built on tree-sitter."""

from __future__ import annotations

import logging

from tree_sitter import Language, Node, Parser

import tree_sitter_swift

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

_LANGUAGE = Language(tree_sitter_swift.language())
_PARSER = Parser(_LANGUAGE)

TYPE_KEYWORDS = {
    "class": "class",
    "struct": "struct",
    "enum": "enum",
    "actor": "actor",
    "extension": "extension",
}

TYPE_KINDS = frozenset({"class", "struct", "enum", "actor", "extension", "protocol"})

BUILTIN_CALLS = frozenset(
    {
        "abs",
        "assert",
        "debugPrint",
        "dump",
        "fatalError",
        "max",
        "min",
        "precondition",
        "print",
        "readLine",
        "stride",
        "swap",
        "type",
        "zip",
    }
)

BUILTIN_TYPES = frozenset(
    {
        "Any",
        "AnyObject",
        "Array",
        "Bool",
        "Character",
        "Dictionary",
        "Double",
        "Error",
        "Float",
        "Int",
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Never",
        "Optional",
        "Result",
        "Set",
        "String",
        "UInt",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
        "Void",
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


def _declaration_kind(node: Node) -> str | None:
    for child in node.children:
        if not child.is_named and child.type in TYPE_KEYWORDS:
            return TYPE_KEYWORDS[child.type]
    return None


def _first_identifier(node: Node, source: bytes) -> str | None:
    if node.type in ("type_identifier", "simple_identifier", "identifier"):
        return _text(source, node)
    for child in node.named_children:
        if child.type == "attribute":
            continue
        found = _first_identifier(child, source)
        if found:
            return found
    return None


def _last_type_identifier(node: Node, source: bytes) -> str | None:
    found = None
    for child in node.named_children:
        if child.type == "type_identifier":
            found = _text(source, child)
        else:
            nested = _last_type_identifier(child, source)
            if nested:
                found = nested
    return found


def _call_name(node: Node, source: bytes) -> str | None:
    if node.type in ("simple_identifier", "identifier"):
        return _text(source, node)
    if node.type == "navigation_expression":
        for child in reversed(node.named_children):
            if child.type == "navigation_suffix":
                for inner in reversed(child.named_children):
                    if inner.type == "simple_identifier":
                        return _text(source, inner)
    for child in node.named_children:
        found = _call_name(child, source)
        if found:
            return found
    return None


def _call_receiver(node: Node, source: bytes) -> str | None:
    """Root receiver of a member call, e.g. ProjectScanner in ProjectScanner.shared.scan."""
    if node.type != "navigation_expression" or not node.named_children:
        return None
    root = node.named_children[0]
    if root.type not in ("simple_identifier", "identifier"):
        return None
    text = _text(source, root)
    return text.split(".")[0] if text else None


def _body_start(node: Node) -> int | None:
    for child in node.named_children:
        if child.type in ("class_body", "protocol_body", "function_body"):
            return child.start_byte
    return None


def _assign_start(node: Node) -> int | None:
    for child in node.children:
        if not child.is_named and child.type == "=":
            return child.start_byte
    return None


def _header(source: bytes, node: Node, end_byte: int | None) -> str:
    end = end_byte if end_byte is not None else node.end_byte
    return _compact(source[node.start_byte : end].decode("utf-8", "replace"))


def _name_node(node: Node) -> Node | None:
    for child in node.named_children:
        if child.type in ("type_identifier", "simple_identifier"):
            return child
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
            if raw in BUILTIN_TYPES or (len(raw) == 1 and raw.isupper()):
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


def _add_import(node: Node, source: bytes, analysis: FileAnalysis) -> None:
    module = _first_identifier(node, source)
    if module:
        analysis.imports.append(
            ImportRef(
                module=module,
                names=(),
                line=node.start_point[0] + 1,
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


def _method_kind(enclosing: SymbolDef | None) -> str:
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

    if kind == "import_declaration":
        _add_import(node, source, analysis)
        return

    if kind == "protocol_declaration":
        name_node = _name_node(node)
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol = _add_symbol(
            analysis,
            name,
            "protocol",
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            if _same_node(child, name_node):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind == "class_declaration":
        decl_kind = _declaration_kind(node) or "class"
        if decl_kind == "extension":
            name_node = _named(node, "user_type")
        else:
            name_node = _name_node(node)
        name = _text(source, name_node) if name_node else "(anonymous)"
        if decl_kind == "extension":
            name = name.rsplit(".", 1)[-1]
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

    if kind in ("function_declaration", "protocol_function_declaration"):
        name_node = _name_node(node)
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol_kind = _method_kind(enclosing)
        symbol = _add_symbol(
            analysis,
            name,
            symbol_kind,
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            if _same_node(child, name_node):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind == "init_declaration":
        symbol = _add_symbol(
            analysis,
            "init",
            "initializer",
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            _visit(child, source, analysis, symbol)
        return

    if kind == "deinit_declaration":
        symbol = _add_symbol(
            analysis,
            "deinit",
            "deinitializer",
            _header(source, node, _body_start(node)),
            node,
        )
        for child in node.named_children:
            _visit(child, source, analysis, symbol)
        return

    if kind in ("property_declaration", "protocol_property_declaration"):
        pattern = _named(node, "pattern")
        name_node = _name_node(pattern) if pattern is not None else None
        name = _text(source, name_node) if name_node else "(anonymous)"
        _add_symbol(
            analysis,
            name,
            "property",
            _header(source, node, _assign_start(node)),
            node,
        )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind == "typealias_declaration":
        name_node = _name_node(node)
        name = _text(source, name_node) if name_node else "(anonymous)"
        _add_symbol(analysis, name, "typealias", _header(source, node, None), node)
        return

    if kind == "enum_entry":
        for child in node.named_children:
            if child.type == "simple_identifier":
                _add_symbol(
                    analysis,
                    _text(source, child),
                    "case",
                    _compact(_text(source, node)),
                    node,
                )
        return

    if kind == "inheritance_specifier":
        raw = _last_type_identifier(node, source)
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
        raw = _call_name(func, source) if func is not None else None
        if raw and raw not in BUILTIN_CALLS:
            receiver = _call_receiver(func, source) if func is not None else None
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
    tree = _PARSER.parse(source)
    analysis = FileAnalysis(path=path, lang="swift")
    if tree.root_node.has_error:
        analysis.partial_parse = True
        logging.getLogger("graph").warning(
            "partial parse: %s (tree-sitter error nodes)", path
        )
    _visit(tree.root_node, source, analysis, None)
    return analysis
