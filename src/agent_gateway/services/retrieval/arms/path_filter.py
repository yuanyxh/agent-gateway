"""Keep only hits that quote a filesystem path.

Used as the first stage when the caller declares a path intent: a memory that
contains `/Users/.../foo.swift` is stronger evidence than one that merely reads
like the question. The stage is empty when nothing matches, and the next stage
then runs unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from agent_gateway.services.retrieval.types import (
    ArmResult,
    Query,
    RetrievedItem,
)

#: Absolute, home-relative, dot-relative and Windows paths.
PATH_RE = re.compile(r"(?:^|[\s(\"'`])(?:~|/|\./|[A-Za-z]:\\)[^\s\"'`]+")


def has_path(text: str) -> bool:
    return bool(PATH_RE.search(text or ""))


class PathOnlyArm:
    """Filters an inner arm's over-fetch down to path-bearing items."""

    name = "path_hot"

    def __init__(self, inner: Any, overfetch: int = 3):
        self._inner = inner
        self._overfetch = overfetch

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        result = self._inner.retrieve(
            query, max(limit * self._overfetch, limit), filters
        )
        kept: list[RetrievedItem] = [
            item for item in result.items if has_path(item.content)
        ]
        return ArmResult(
            name=self.name,
            items=kept[:limit],
            elapsed_ms=result.elapsed_ms,
            error=result.error,
        )


__all__ = ["PATH_RE", "PathOnlyArm", "has_path"]
