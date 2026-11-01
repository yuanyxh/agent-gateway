"""Objective-C adapter based on regex heuristics (no tree-sitter grammar)."""

from __future__ import annotations

import bisect
import re

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

SYSTEM_PREFIXES = ("NS", "UI", "CA", "CF")

SKIP_SELECTORS = frozenset(
    {
        "alloc",
        "autorelease",
        "class",
        "copy",
        "description",
        "init",
        "mutableCopy",
        "release",
        "retain",
        "self",
    }
)

TYPE_SELECTORS = frozenset({"alloc", "new"})

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT = re.compile(r"//[^\n]*")
STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"')
CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'")

IMPORT_RE = re.compile(r'^[ \t]*#[ \t]*import[ \t]+[<"]([^">]+)[>"]', re.MULTILINE)
INTERFACE_RE = re.compile(
    r"^[ \t]*@(interface|implementation)[ \t]+([A-Za-z_]\w*)", re.MULTILINE
)
END_RE = re.compile(r"@end\b")
METHOD_RE = re.compile(
    r"^[ \t]*([-+])[ \t]*\([ \t]*([^)\n]*?)[ \t]*\)[ \t]*([A-Za-z_]\w*)",
    re.MULTILINE,
)
PROPERTY_RE = re.compile(r"@property\b[^;]{0,2000};", re.DOTALL)
BLOCK_PROPERTY_RE = re.compile(r"\([ \t]*\^[ \t]*([A-Za-z_]\w*)[ \t]*\)")
PROPERTY_NAME_RE = re.compile(r"([A-Za-z_]\w*)[ \t]*;$")
RECEIVER_RE = re.compile(r"[A-Za-z_]\w*(?:(?:\.|->)[A-Za-z_]\w*)*")
IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _blank(text: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in text)


def _blank_range(chars: list[str], start: int, end: int) -> None:
    for index in range(start, min(end, len(chars))):
        if chars[index] != "\n":
            chars[index] = " "


def _mask(text: str) -> str:
    """Blank comments and literals in one pass, preserving newlines.

    A single state machine keeps quotes inside comments and comment markers
    inside strings from masking real code between them.
    """
    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
            _blank_range(chars, index, end)
            index = end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            _blank_range(chars, index, end)
            index = end
        elif text[index] in ('"', "'"):
            quote = text[index]
            start = index
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                if text[index] == "\n" and quote == "'":
                    break
                index += 1
            _blank_range(chars, start, index)
        else:
            index += 1
    return "".join(chars)


def _compact(text: str) -> str:
    return " ".join(text.split())


def _line_starts(text: str) -> list[int]:
    return [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]


def _line_at(text: str, index: int, starts: list[int] | None = None) -> int:
    if starts is None:
        return text.count("\n", 0, index) + 1
    return bisect.bisect_right(starts, index)


def _line_text(text: str, index: int) -> str:
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return _compact(text[index:end])


def _is_system(name: str) -> bool:
    return name.startswith(SYSTEM_PREFIXES)


def _looks_like_type(name: str) -> bool:
    return bool(name) and name[0].isupper() and not _is_system(name)


def _matching(text: str, index: int, opening: str, closing: str) -> int | None:
    depth = 0
    for cursor in range(index, len(text)):
        if text[cursor] == opening:
            depth += 1
        elif text[cursor] == closing:
            depth -= 1
            if depth == 0:
                return cursor
    return None


def _method_end_line(text: str, start: int, starts: list[int]) -> int:
    paren = 0
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif paren == 0 and char == ";":
            return _line_at(text, cursor, starts)
        elif paren == 0 and char == "{":
            close = _matching(text, cursor, "{", "}")
            if close is not None:
                return _line_at(text, close, starts)
            return _line_at(text, start, starts)
        cursor += 1
    return _line_at(text, start, starts)


def _class_end_line(text: str, start: int, fallback: int, starts: list[int]) -> int:
    end = END_RE.search(text, start)
    return _line_at(text, end.start(), starts) if end is not None else fallback


def _property_name(declaration: str) -> str | None:
    block = BLOCK_PROPERTY_RE.search(declaration)
    if block is not None:
        return block.group(1)
    match = PROPERTY_NAME_RE.search(declaration)
    return match.group(1) if match is not None else None


def _collect_symbols(text: str, path: str, starts: list[int]) -> list[SymbolDef]:
    entries: list[tuple[int, SymbolDef]] = []

    for match in INTERFACE_RE.finditer(text):
        start_line = _line_at(text, match.start(), starts)
        entries.append(
            (
                match.start(),
                SymbolDef(
                    path=path,
                    name=match.group(2),
                    kind="class",
                    signature=_line_text(text, match.start()),
                    start_line=start_line,
                    end_line=_class_end_line(text, match.end(), start_line, starts),
                ),
            )
        )

    for match in METHOD_RE.finditer(text):
        start_line = _line_at(text, match.start(), starts)
        entries.append(
            (
                match.start(),
                SymbolDef(
                    path=path,
                    name=match.group(3),
                    kind="method",
                    signature=_line_text(text, match.start()),
                    start_line=start_line,
                    end_line=_method_end_line(text, match.start(), starts),
                ),
            )
        )

    for match in PROPERTY_RE.finditer(text):
        name = _property_name(match.group(0))
        if name is None:
            continue
        entries.append(
            (
                match.start(),
                SymbolDef(
                    path=path,
                    name=name,
                    kind="property",
                    signature=_compact(match.group(0)),
                    start_line=_line_at(text, match.start(), starts),
                    end_line=_line_at(text, match.end() - 1, starts),
                ),
            )
        )

    entries.sort(key=lambda item: item[0])
    return [symbol for _, symbol in entries]


def _enclosing_symbol(symbols: list[SymbolDef], line: int) -> SymbolDef | None:
    best: SymbolDef | None = None
    for symbol in symbols:
        if symbol.kind not in ("class", "method"):
            continue
        if not symbol.start_line <= line <= symbol.end_line:
            continue
        if best is None or symbol.end_line - symbol.start_line <= (
            best.end_line - best.start_line
        ):
            best = symbol
    return best


def _reference(
    raw_name: str,
    kind: str,
    line: int,
    symbols: list[SymbolDef],
    receiver: str | None,
) -> Reference:
    enclosing = _enclosing_symbol(symbols, line)
    return Reference(
        raw_name=raw_name,
        kind=kind,
        line=line,
        from_name=enclosing.name if enclosing is not None else None,
        from_line=enclosing.start_line if enclosing is not None else None,
        receiver=receiver,
    )


def _split_receiver(content: str) -> tuple[str | None, str | None]:
    text = content.lstrip()
    if text.startswith("["):
        end = _matching(text, 0, "[", "]")
        if end is None:
            return None, None
        return text[: end + 1], text[end + 1 :]
    match = RECEIVER_RE.match(text)
    if match is None:
        return None, None
    return match.group(0), text[match.end() :]


def _root_identifier(expression: str) -> str | None:
    text = expression.lstrip()
    if text.startswith("["):
        end = _matching(text, 0, "[", "]")
        if end is None:
            return None
        receiver, _ = _split_receiver(text[1:end])
        return _root_identifier(receiver) if receiver is not None else None
    match = RECEIVER_RE.match(text)
    if match is None:
        return None
    return re.split(r"\.|->", match.group(0), maxsplit=1)[0]


def _selector_name(rest: str) -> str | None:
    text = rest.strip()
    if not text:
        return None
    first: str | None = None
    depth = 0
    cursor = 0
    while cursor < len(text):
        char = text[cursor]
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif depth == 0:
            match = IDENT_RE.match(text, cursor)
            if match is not None:
                probe = match.end()
                while probe < len(text) and text[probe].isspace():
                    probe += 1
                if probe < len(text) and text[probe] == ":":
                    if first is None:
                        first = match.group(0)
                    cursor = probe + 1
                    continue
                if first is None:
                    return match.group(0)
                cursor = match.end()
                continue
        cursor += 1
    return first


def _message_references(
    content: str, line: int, symbols: list[SymbolDef]
) -> list[Reference]:
    receiver_expression, rest = _split_receiver(content)
    if receiver_expression is None or rest is None:
        return []
    receiver = _root_identifier(receiver_expression)
    selector = _selector_name(rest)
    if receiver is None or selector is None:
        return []

    references: list[Reference] = []
    if selector not in SKIP_SELECTORS and not _is_system(receiver):
        references.append(_reference(selector, "call", line, symbols, receiver))
    if selector in TYPE_SELECTORS and _looks_like_type(receiver):
        references.append(_reference(receiver, "type_ref", line, symbols, None))
    return references


def _iter_brackets(text: str):
    stack: list[int] = []
    for index, char in enumerate(text):
        if char == "[":
            stack.append(index)
        elif char == "]" and stack:
            yield stack.pop(), index


def parse(source: bytes, path: str) -> FileAnalysis:
    text = source.decode("utf-8", "replace")
    starts = _line_starts(text)
    masked = _mask(text)
    symbols = _collect_symbols(masked, path, starts)
    analysis = FileAnalysis(path=path, lang="objc", symbols=symbols)

    for match in IMPORT_RE.finditer(text):
        analysis.imports.append(
            ImportRef(
                module=match.group(1),
                names=(),
                line=_line_at(text, match.start(), starts),
            )
        )

    entries: list[tuple[int, Reference]] = []
    for start, end in _iter_brackets(masked):
        if start > 0 and masked[start - 1] == "@":
            continue
        line = _line_at(masked, start, starts)
        for reference in _message_references(masked[start + 1 : end], line, symbols):
            entries.append((start, reference))
    entries.sort(key=lambda item: item[0])
    analysis.references = [reference for _, reference in entries]
    return analysis
