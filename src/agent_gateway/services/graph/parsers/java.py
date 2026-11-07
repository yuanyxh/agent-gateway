"""Java adapter for the graph indexer, built on tree-sitter."""

from __future__ import annotations

import logging

from tree_sitter import Language, Node, Parser

import tree_sitter_java

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

_LANGUAGE = Language(tree_sitter_java.language())
_PARSER = Parser(_LANGUAGE)

DECLARATION_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
}

BUILTIN_CALLS = frozenset(
    {
        "clone",
        "equals",
        "getClass",
        "hashCode",
        "notify",
        "notifyAll",
        "print",
        "printf",
        "println",
        "toString",
        "wait",
    }
)

BUILTIN_TYPES = frozenset(
    {
        "ArrayList",
        "Arrays",
        "BigDecimal",
        "BigInteger",
        "Boolean",
        "Byte",
        "Character",
        "CharSequence",
        "Class",
        "Collection",
        "Collections",
        "Comparable",
        "Comparator",
        "Deque",
        "Double",
        "Enum",
        "Error",
        "Exception",
        "Files",
        "Float",
        "HashMap",
        "HashSet",
        "IOException",
        "Integer",
        "Iterable",
        "Iterator",
        "LinkedHashMap",
        "LinkedHashSet",
        "LinkedList",
        "List",
        "LocalDate",
        "LocalDateTime",
        "Long",
        "Map",
        "Math",
        "Number",
        "Object",
        "Objects",
        "Optional",
        "Path",
        "Paths",
        "Pattern",
        "Queue",
        "Record",
        "Runnable",
        "RuntimeException",
        "Set",
        "Short",
        "Stream",
        "String",
        "StringBuffer",
        "StringBuilder",
        "System",
        "Thread",
        "Throwable",
        "TreeMap",
        "TreeSet",
        "UUID",
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


def _base_type_name(node: Node, source: bytes) -> str | None:
    """Type name without generics, e.g. Base for Base<String>."""
    kind = node.type
    if kind == "type_identifier":
        return _text(source, node)
    if kind == "scoped_type_identifier":
        for child in reversed(node.named_children):
            found = _base_type_name(child, source)
            if found:
                return found
        return None
    if kind in ("generic_type", "array_type"):
        named = node.named_children
        if named:
            return _base_type_name(named[0], source)
    return None


def _call_receiver(node: Node, source: bytes) -> str | None:
    """Root receiver of a member call, e.g. a in a.b.c.run()."""
    value = node.child_by_field_name("object")
    while value is not None and value.type == "field_access":
        nested = value.child_by_field_name("object")
        if nested is None:
            break
        value = nested
    if value is not None and value.type in ("identifier", "this", "super"):
        return _text(source, value)
    return None


def _body_start(node: Node) -> int | None:
    body = node.child_by_field_name("body")
    return body.start_byte if body is not None else None


def _header(source: bytes, node: Node, end_byte: int | None) -> str:
    end = end_byte if end_byte is not None else node.end_byte
    return _compact(source[node.start_byte : end].decode("utf-8", "replace"))


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


def _add_import(node: Node, source: bytes, analysis: FileAnalysis) -> None:
    scoped = _named(node, "scoped_identifier")
    if scoped is None:
        return
    analysis.imports.append(
        ImportRef(
            module=_text(source, scoped),
            names=(),
            line=node.start_point[0] + 1,
        )
    )


def _add_extends(
    node: Node,
    source: bytes,
    analysis: FileAnalysis,
    enclosing: SymbolDef | None,
) -> None:
    if node.type == "superclass":
        candidates = list(node.named_children)
    else:
        type_list = _named(node, "type_list")
        candidates = list(type_list.named_children) if type_list else []
    for candidate in candidates:
        raw = _base_type_name(candidate, source)
        if not raw or raw in BUILTIN_TYPES:
            continue
        analysis.references.append(
            Reference(
                raw_name=raw,
                kind="extends",
                line=candidate.start_point[0] + 1,
                from_name=enclosing.name if enclosing else None,
                from_line=enclosing.start_line if enclosing else None,
            )
        )


def _add_type_refs(
    node: Node,
    source: bytes,
    analysis: FileAnalysis,
    enclosing: SymbolDef | None,
) -> None:
    if node.type == "type_identifier":
        raw = _text(source, node)
        if raw not in BUILTIN_TYPES and not (len(raw) == 1 and raw.isupper()):
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="type_ref",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                )
            )
        return
    if node.type == "scoped_type_identifier":
        # Package segments are not types; keep only the last segment.
        names = [
            child for child in node.named_children if child.type == "type_identifier"
        ]
        if names:
            raw = _text(source, names[-1])
            if raw not in BUILTIN_TYPES and not (len(raw) == 1 and raw.isupper()):
                analysis.references.append(
                    Reference(
                        raw_name=raw,
                        kind="type_ref",
                        line=names[-1].start_point[0] + 1,
                        from_name=enclosing.name if enclosing else None,
                        from_line=enclosing.start_line if enclosing else None,
                    )
                )
        for child in node.named_children:
            if child.type == "type_arguments":
                _add_type_refs(child, source, analysis, enclosing)
        return
    for child in node.named_children:
        if child.type != "type_parameters":
            _add_type_refs(child, source, analysis, enclosing)


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

    decl_kind = DECLARATION_KINDS.get(kind)
    if decl_kind is not None:
        name_node = node.child_by_field_name("name")
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

    if kind in ("method_declaration", "constructor_declaration"):
        name_node = node.child_by_field_name("name")
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol_kind = "initializer" if kind == "constructor_declaration" else "method"
        symbol = _add_symbol(
            analysis,
            name,
            symbol_kind,
            _header(source, node, _body_start(node)),
            node,
        )
        return_type = node.child_by_field_name("type")
        if return_type is not None:
            _add_type_refs(return_type, source, analysis, symbol)
        parameters = node.child_by_field_name("parameters")
        if parameters is not None:
            _add_type_refs(parameters, source, analysis, symbol)
        for child in node.named_children:
            if _same_node(child, name_node):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind == "field_declaration":
        type_node = node.child_by_field_name("type")
        type_text = _compact(_text(source, type_node)) if type_node else ""
        if type_node is not None:
            _add_type_refs(type_node, source, analysis, enclosing)
        for declarator in node.children_by_field_name("declarator"):
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(source, name_node)
            signature = f"{type_text} {name}".strip() or None
            _add_symbol(analysis, name, "property", signature, node)
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind in ("superclass", "super_interfaces", "extends_interfaces"):
        _add_extends(node, source, analysis, enclosing)
        return

    if kind == "method_invocation":
        name_node = node.child_by_field_name("name")
        raw = _text(source, name_node) if name_node else None
        if raw and raw not in BUILTIN_CALLS:
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="call",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                    receiver=_call_receiver(node, source),
                )
            )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind == "object_creation_expression":
        type_node = node.child_by_field_name("type")
        raw = _base_type_name(type_node, source) if type_node else None
        if raw and raw not in BUILTIN_TYPES:
            analysis.references.append(
                Reference(
                    raw_name=raw,
                    kind="type_ref",
                    line=node.start_point[0] + 1,
                    from_name=enclosing.name if enclosing else None,
                    from_line=enclosing.start_line if enclosing else None,
                )
            )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    for child in node.named_children:
        _visit(child, source, analysis, enclosing)


def parse(source: bytes, path: str) -> FileAnalysis:
    tree = _PARSER.parse(source)
    analysis = FileAnalysis(path=path, lang="java")
    if tree.root_node.has_error:
        analysis.partial_parse = True
        logging.getLogger("graph").warning(
            "partial parse: %s (tree-sitter error nodes)", path
        )
    _visit(tree.root_node, source, analysis, None)
    return analysis
