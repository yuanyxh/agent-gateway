"""Keyword scoping: keywords narrow the candidate set, the query ranks it.

A primary keyword is a claim about the range of a lookup ("this project",
"that dimension"), not a second query. Records that quote every keyword form
the allowed set.

The constraint is all-or-nothing: one keyword that cannot be anchored means
the range is unknown, so nothing is filtered. Filtering on a partial claim
throws away records the missing keyword would have allowed (measured hit@1
0.83 -> 0.42 with a synonym the candidates never quote).
"""

from __future__ import annotations

from typing import Any

from agent_gateway.services.retrieval.types import Query, RetrievedItem


class KeywordScope:
    """Keep only the query hits whose text contains the caller's keywords."""

    def _sets(self, terms: list[str], items: list[RetrievedItem]) -> list[set[str]]:
        lowered = [(item.id, (item.content or "").lower()) for item in items]
        return [
            {record for record, text in lowered if term.lower() in text}
            for term in terms
        ]

    def __call__(
        self,
        query: Query,
        items: list[RetrievedItem],
        limit: int = 0,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedItem]:
        terms = [k.strip() for k in query.keywords if k and k.strip()]
        if not terms or not items:
            return items
        sets = self._sets(terms, items)
        if not all(sets):
            return items
        both = set.intersection(*sets)
        if not both:
            return items
        return [item for item in items if item.id in both]


__all__ = ["KeywordScope"]
