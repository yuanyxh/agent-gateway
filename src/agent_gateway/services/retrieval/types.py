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

    `text` is the main query, built from the caller's keywords. `semantic_text`
    is an optional one-sentence rewording searched in a second pass.
    `root_text` is the untranslated original when the caller rewrote it.
    """

    text: str
    language: str = "unknown"
    root_text: str = ""
    semantic_text: str = ""
    #: Result constraints keyed by row field; a row whose value is absent or not
    #: allowed is dropped. Values may be a single value or a collection.
    filters: dict[str, Any] = field(default_factory=dict)
    #: Result preferences keyed by row field; each match multiplies the row's rank
    #: position without changing its score, so the relevance gate is unaffected.
    boost: dict[str, Any] = field(default_factory=dict)
    #: Extra row fields the caller wants echoed back, beyond the shared whitelist.
    #: Lets a caller verify which values a filter or boost matched on.
    expose: tuple[str, ...] = ()


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

    #: Fields a caller needs to decide; diagnostics stay in the retrieval trace.
    RESULT_EXTRA_KEYS = (
        "episode_id",
        "context",
        "project",
        "task",
        "edge",
        "name",
        "caller_count",
        "top_callers",
        "resolver",
    )
    RESULT_META_KEYS = ("tags", "status", "confidence")

    def to_result(self, expose: tuple[str, ...] = ()) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "score": float(self.score),
            "content": self.content,
        }
        if self.layer:
            out["layer"] = self.layer
        if self.locator:
            out["locator"] = self.locator
        for key in dict.fromkeys((*self.RESULT_EXTRA_KEYS, *expose)):
            value = self.extra.get(key)
            if value not in (None, ""):
                out[key] = value
        metadata = self.extra.get("metadata")
        if isinstance(metadata, dict):
            kept = {
                key: metadata[key] for key in self.RESULT_META_KEYS if key in metadata
            }
            if kept:
                out["metadata"] = kept
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
