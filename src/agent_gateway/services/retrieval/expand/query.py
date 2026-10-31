"""Turn a query into the sub-queries a tier runs.

One query expands to itself. The module exists so a profile can substitute a
rewriter without touching the pipeline; the default costs nothing.
"""

from __future__ import annotations

from typing import Callable

from agent_gateway.services.retrieval.types import Query, SubQuery

Expander = Callable[[Query], list[SubQuery]]


def expand_query(query: Query) -> list[SubQuery]:
    """The query as a single weighted sub-query."""
    return [SubQuery(text=query.text, weight=1.0, label="text")]


__all__ = ["Expander", "expand_query"]
