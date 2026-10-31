"""Core data types shared by the retrieval layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    """Embedding model descriptor.

    Prefixes and dims are data, not code branches: switching models must not add
    `if model.startswith(...)` anywhere in the pipeline.
    """

    id: str
    tag: str
    dims: int
    query_prefix: str = ""
    doc_prefix: str = ""
    max_tokens: int = 512

    def encode_query(self, text: str) -> str:
        return f"{self.query_prefix}{text}"

    def encode_doc(self, text: str) -> str:
        return f"{self.doc_prefix}{text}"


@dataclass
class Query:
    """A retrieval request.

    `text` is what the caller typed. Everything else is optional context a
    caller that knows more can supply: keywords carry the caller's own emphasis
    and groups split a query into independently scored term sets.
    """

    text: str
    language: str = "unknown"
    root_text: str = ""
    #: Terms the caller considers primary; each is retrieved on its own.
    keywords: list[str] = field(default_factory=list)
    #: Weight of hits a primary keyword fetched, relative to the raw query.
    keyword_weight: float = 2.0
    #: Weight of the raw query when keyword lists merge into one ranking.
    query_weight: float = 1.0
    #: Independent term sets; each yields its own hit list before merging.
    groups: list[list[str]] = field(default_factory=list)
    #: Retrieval hint from the caller: semantic / lexical / auto.
    mode: str = "auto"


@dataclass(frozen=True)
class SubQuery:
    """One query to run against a stage, with the weight its hits carry.

    A plain query expands to a single sub-query with weight 1.0, so the default
    path is unchanged.
    """

    text: str
    weight: float = 1.0
    label: str = ""


@dataclass
class RetrievedItem:
    """One candidate. `score_kind` records where the score came from."""

    id: str
    kind: str
    score: float
    score_kind: str
    content: str = ""
    layer: str = ""
    match_kind: str = "direct"
    locator: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "score": float(self.score),
            "score_kind": self.score_kind,
            "content": self.content,
            "match_kind": self.match_kind,
        }
        if self.layer:
            out["layer"] = self.layer
        if self.locator:
            out["locator"] = self.locator
        out.update(self.extra)
        return out


@dataclass
class ArmResult:
    """Output of a single retriever."""

    name: str
    items: list[RetrievedItem] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None


@dataclass
class GateVerdict:
    status: str = ""
    reason: str = ""
    top1: float = 0.0
    median: float = 0.0
    margin: float = 0.0
    lexical_anchor: bool = False
