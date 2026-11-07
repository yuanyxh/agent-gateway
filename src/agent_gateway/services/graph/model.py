"""Shared data structures for the graph indexer and its parsers."""

from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(Exception):
    """Raised when a file cannot be parsed into a complete result."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class SymbolDef:
    path: str
    name: str
    kind: str
    signature: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Reference:
    raw_name: str
    kind: str
    line: int
    from_name: str | None = None
    from_line: int | None = None
    receiver: str | None = None


@dataclass(frozen=True)
class ImportRef:
    module: str
    names: tuple[str, ...]
    line: int
    level: int = 0
    kind: str = "import"
    #: local name -> original name; "*" marks a namespace import.
    aliases: tuple[tuple[str, str], ...] = ()


@dataclass
class FileAnalysis:
    path: str
    lang: str
    symbols: list[SymbolDef] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    #: Generic parameter names in scope; not persisted, only for type-ref filtering.
    type_params: set[str] = field(default_factory=set)
    #: tree-sitter produced error nodes; symbols are best-effort, not complete.
    partial_parse: bool = False
