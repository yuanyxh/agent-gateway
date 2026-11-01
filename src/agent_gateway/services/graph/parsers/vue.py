"""Vue single-file component adapter: parses script and template blocks."""

from __future__ import annotations

import re

from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)
from agent_gateway.services.graph.parsers import typescript as typescript_parser

SCRIPT_BLOCK = re.compile(
    rb"<script\b([^>]*)>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE
)
SCRIPT_LANG = re.compile(rb'\blang\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
SYNTAX_BY_LANG = {
    "ts": "typescript",
    "typescript": "typescript",
    "tsx": "tsx",
    "jsx": "tsx",
    "js": "javascript",
    "javascript": "javascript",
}
TEMPLATE_OPEN = re.compile(rb"<template\b[^>]*>", re.IGNORECASE)
TEMPLATE_TAG = re.compile(rb"</?template\b[^>]*>", re.IGNORECASE)
COMPONENT_TAG = re.compile(
    rb"<([A-Z][A-Za-z0-9]*|[a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?=[\s/>])"
)
EVENT_ATTR = re.compile(
    rb'(?:@|v-on:)([A-Za-z][\w.-]*)\s*=\s*(["\'])(.*?)\2', re.DOTALL
)
HANDLER_NAME = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*(?:\(|$)")
HTML_COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)


def _shift_line(line: int | None, offset: int) -> int | None:
    return None if line is None else line + offset


def _shift(analysis: FileAnalysis, offset: int) -> FileAnalysis:
    symbols = [
        SymbolDef(
            path=item.path,
            name=item.name,
            kind=item.kind,
            signature=item.signature,
            start_line=item.start_line + offset,
            end_line=item.end_line + offset,
        )
        for item in analysis.symbols
    ]
    references = [
        Reference(
            raw_name=item.raw_name,
            kind=item.kind,
            line=item.line + offset,
            from_name=item.from_name,
            from_line=_shift_line(item.from_line, offset),
            receiver=item.receiver,
        )
        for item in analysis.references
    ]
    imports = [
        ImportRef(
            module=item.module,
            names=item.names,
            line=item.line + offset,
            level=item.level,
            kind=item.kind,
            aliases=item.aliases,
        )
        for item in analysis.imports
    ]
    return FileAnalysis(
        path=analysis.path,
        lang="vue",
        symbols=symbols,
        references=references,
        imports=imports,
    )


def _template_span(source: bytes) -> tuple[int, int] | None:
    """Body span of the first top-level template block, nested ones included."""
    opening = TEMPLATE_OPEN.search(source)
    if opening is None:
        return None
    if opening.group(0).rstrip().endswith(b"/>"):
        return opening.end(), opening.end()
    depth = 0
    for tag in TEMPLATE_TAG.finditer(source, opening.start()):
        if tag.group(0).startswith(b"</"):
            depth -= 1
        elif tag.group(0).rstrip().endswith(b"/>"):
            continue
        else:
            depth += 1
        if depth == 0:
            return opening.end(), tag.start()
    return opening.end(), len(source)


def _blank_comments(block: bytes) -> bytes:
    """Replace comment bytes with spaces, preserving newlines and offsets."""
    return HTML_COMMENT.sub(
        lambda match: re.sub(rb"[^\n]", b" ", match.group(0)), block
    )


def _template_references(source: bytes) -> list[Reference]:
    span = _template_span(source)
    if span is None:
        return []
    start, end = span
    block = _blank_comments(source[start:end])
    references: list[Reference] = []
    seen: set[str] = set()
    for match in COMPONENT_TAG.finditer(block):
        name = _pascal_name(match.group(1).decode("utf-8", "replace"))
        if name in seen:
            continue
        seen.add(name)
        line = source[: start + match.start()].count(b"\n") + 1
        references.append(Reference(raw_name=name, kind="type_ref", line=line))
    return references


def _template_event_refs(source: bytes) -> list[Reference]:
    span = _template_span(source)
    if span is None:
        return []
    start, end = span
    block = _blank_comments(source[start:end])
    references: list[Reference] = []
    seen: set[str] = set()
    for match in EVENT_ATTR.finditer(block):
        expr = match.group(3).decode("utf-8", "replace")
        handler = HANDLER_NAME.match(expr)
        if handler is None:
            continue
        name = handler.group(1)
        if name in seen:
            continue
        seen.add(name)
        line = source[: start + match.start()].count(b"\n") + 1
        references.append(Reference(raw_name=name, kind="call", line=line))
    return references


def _pascal_name(name: str) -> str:
    parts = [part for part in re.split(r"[-_.]", name) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or name


def _component_name(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = name[:-4] if name.lower().endswith(".vue") else name
    return _pascal_name(stem)


def _add_component_symbol(analysis: FileAnalysis, path: str) -> None:
    name = _component_name(path)
    if not name or any(item.name == name for item in analysis.symbols):
        return
    analysis.symbols.append(
        SymbolDef(
            path=path,
            name=name,
            kind="component",
            signature=path,
            start_line=1,
            end_line=1,
        )
    )


def parse(source: bytes, path: str) -> FileAnalysis:
    analysis = FileAnalysis(path=path, lang="vue")
    for match in SCRIPT_BLOCK.finditer(source):
        offset = source[: match.start(2)].count(b"\n")
        lang = SCRIPT_LANG.search(match.group(1))
        syntax = None
        if lang is not None:
            syntax = SYNTAX_BY_LANG.get(
                lang.group(1).decode("utf-8", "replace").lower()
            )
        shifted = _shift(
            typescript_parser.parse_script(match.group(2), path, syntax), offset
        )
        analysis.symbols.extend(shifted.symbols)
        analysis.references.extend(shifted.references)
        analysis.imports.extend(shifted.imports)
    analysis.references.extend(_template_references(source))
    analysis.references.extend(_template_event_refs(source))
    _add_component_symbol(analysis, path)
    return analysis
