"""Python adapter for the graph indexer, built on tree-sitter."""

from __future__ import annotations

import logging

from tree_sitter import Language, Node, Parser

import tree_sitter_python

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

_LANGUAGE = Language(tree_sitter_python.language())
_PARSER = Parser(_LANGUAGE)

BUILTIN_CALLS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "classmethod",
        "dict",
        "dir",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "input",
        "int",
        "isinstance",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "object",
        "open",
        "print",
        "property",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "setattr",
        "sorted",
        "staticmethod",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "vars",
        "zip",
    }
)

BUILTIN_TYPES = frozenset(
    {
        "Any",
        "Callable",
        "Dict",
        "Iterable",
        "Iterator",
        "List",
        "Mapping",
        "None",
        "Optional",
        "Sequence",
        "Set",
        "Tuple",
        "Type",
        "Union",
        "bool",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "memoryview",
        "object",
        "set",
        "str",
        "tuple",
    }
)


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _compact(text: str) -> str:
    return " ".join(text.split())


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


def _call_receiver(node: Node, source: bytes) -> str | None:
    """Root receiver of a member call, e.g. facade in facade.run()."""
    if node.type != "attribute":
        return None
    value = node.child_by_field_name("object")
    while value is not None and value.type == "attribute":
        value = value.child_by_field_name("object")
    if value is not None and value.type == "identifier":
        return _text(source, value)
    return None


def _last_name(node: Node, source: bytes) -> str | None:
    if node.type in ("identifier", "simple_identifier"):
        return _text(source, node)
    if node.type == "keyword_argument":
        value = node.child_by_field_name("value")
        return _last_name(value, source) if value is not None else None
    if node.type in ("attribute", "dotted_name", "relative_import"):
        attr = node.child_by_field_name("attribute")
        if attr is not None:
            return _text(source, attr)
        named = node.named_children
        if named:
            return _last_name(named[-1], source)
        return None
    named = node.named_children
    if named:
        return _last_name(named[0], source)
    return None


def _type_names(node: Node, source: bytes) -> list[str]:
    names: list[str] = []

    def walk(current: Node) -> None:
        if current.type == "attribute":
            attr = current.child_by_field_name("attribute")
            if attr is not None:
                names.append(_text(source, attr))
            return
        if current.type == "identifier":
            names.append(_text(source, current))
            return
        for child in current.named_children:
            walk(child)

    walk(node)
    return names


def _function_kind(enclosing: SymbolDef | None) -> str:
    if enclosing is not None and enclosing.kind == "class":
        return "method"
    return "function"


def _signature(node: Node, source: bytes, name: str, kind: str) -> str | None:
    if kind in ("function", "method"):
        params = _named(node, "parameters")
        signature = name + (_compact(_text(source, params)) if params else "()")
        return_type = _named(node, "type")
        if return_type is not None:
            signature += " -> " + _compact(_text(source, return_type))
        return signature
    if kind == "class":
        bases = _named(node, "argument_list")
        if bases is not None:
            return name + _compact(_text(source, bases))
        return name
    if kind == "variable":
        type_node = _named(node, "type")
        if type_node is not None:
            return name + ": " + _compact(_text(source, type_node))
        return name
    return None


def _position(node: Node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _add_imports(node: Node, source: bytes, analysis: FileAnalysis) -> None:
    line = node.start_point[0] + 1
    if node.type == "import_statement":
        for child in node.named_children:
            aliases: tuple[tuple[str, str], ...] = ()
            if child.type == "aliased_import":
                target = child.child_by_field_name("name")
                alias = child.child_by_field_name("alias")
                module = _text(source, target) if target is not None else ""
                if module and alias is not None:
                    aliases = ((_text(source, alias), module),)
            elif child.type == "dotted_name":
                module = _text(source, child)
            else:
                continue
            if module:
                analysis.imports.append(
                    ImportRef(module=module, names=(), line=line, aliases=aliases)
                )
        return

    named = node.named_children
    if not named:
        return
    module_node = named[0]
    module = ""
    level = 0
    if module_node.type == "relative_import":
        raw = _text(source, module_node)
        rest = raw.lstrip(".")
        level = len(raw) - len(rest)
        module = rest
    elif module_node.type == "dotted_name":
        module = _text(source, module_node)
    else:
        return

    names: list[str] = []
    aliases: list[tuple[str, str]] = []
    for child in named[1:]:
        if child.type == "dotted_name":
            names.append(_text(source, child))
        elif child.type == "aliased_import":
            target = child.child_by_field_name("name") or (
                child.named_children[0] if child.named_children else None
            )
            if target is not None:
                original = _text(source, target)
                names.append(original)
                alias = child.child_by_field_name("alias")
                if alias is not None:
                    aliases.append((_text(source, alias), original))
    analysis.imports.append(
        ImportRef(
            module=module,
            names=tuple(names),
            line=line,
            level=level,
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

    if kind == "decorated_definition":
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind == "class_definition":
        name_node = _named(node, "identifier")
        name = _text(source, name_node) if name_node else "(anonymous)"
        start, end = _position(node)
        symbol = SymbolDef(
            path=analysis.path,
            name=name,
            kind="class",
            signature=_signature(node, source, name, "class"),
            start_line=start,
            end_line=end,
        )
        analysis.symbols.append(symbol)
        bases = _named(node, "argument_list")
        if bases is not None:
            for base in bases.named_children:
                if base.type in ("keyword_argument", "dictionary_splat"):
                    continue
                raw = _last_name(base, source)
                if raw and raw not in BUILTIN_TYPES:
                    analysis.references.append(
                        Reference(
                            raw_name=raw,
                            kind="extends",
                            line=base.start_point[0] + 1,
                            from_name=name,
                            from_line=start,
                        )
                    )
        for child in node.named_children:
            if _same_node(child, bases):
                continue
            _visit(child, source, analysis, symbol)
        return

    if kind == "function_definition":
        name_node = _named(node, "identifier")
        name = _text(source, name_node) if name_node else "(anonymous)"
        symbol_kind = _function_kind(enclosing)
        start, end = _position(node)
        symbol = SymbolDef(
            path=analysis.path,
            name=name,
            kind=symbol_kind,
            signature=_signature(node, source, name, symbol_kind),
            start_line=start,
            end_line=end,
        )
        analysis.symbols.append(symbol)
        for child in node.named_children:
            _visit(child, source, analysis, symbol)
        return

    if kind == "assignment":
        if enclosing is None or enclosing.kind == "class":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                name = _text(source, left)
                start, end = _position(node)
                analysis.symbols.append(
                    SymbolDef(
                        path=analysis.path,
                        name=name,
                        kind="variable",
                        signature=_signature(node, source, name, "variable"),
                        start_line=start,
                        end_line=end,
                    )
                )
        for child in node.named_children:
            _visit(child, source, analysis, enclosing)
        return

    if kind in ("import_statement", "import_from_statement"):
        _add_imports(node, source, analysis)
        return

    if kind == "type":
        for raw in _type_names(node, source):
            if raw in BUILTIN_TYPES:
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
        return

    if kind == "call":
        func = node.child_by_field_name("function")
        if func is None and node.named_children:
            func = node.named_children[0]
        raw = _last_name(func, source) if func is not None else None
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
    if tree.root_node.has_error:
        logging.getLogger("graph").warning(
            "partial parse: %s (tree-sitter error nodes)", path
        )
    analysis = FileAnalysis(path=path, lang="python")
    _visit(tree.root_node, source, analysis, None)
    return analysis
