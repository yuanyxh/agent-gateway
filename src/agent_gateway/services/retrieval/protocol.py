"""Structural interfaces for the retrieval layer.

Implementations depend on these protocols only; the pipeline never imports a
concrete arm, fusion or gate.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agent_gateway.services.retrieval.types import (
    ArmResult,
    GateVerdict,
    Query,
    RetrievedItem,
)


@runtime_checkable
class Retriever(Protocol):
    name: str

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult: ...


@runtime_checkable
class Fusion(Protocol):
    name: str

    def fuse(
        self, arms: list[ArmResult], limit: int, weights: dict[str, float] | None = None
    ) -> list[RetrievedItem]: ...


@runtime_checkable
class Gate(Protocol):
    name: str

    def judge(
        self,
        query: Query,
        items: list[RetrievedItem],
        related: list[RetrievedItem] | None = None,
    ) -> GateVerdict: ...
