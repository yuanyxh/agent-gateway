"""Re-rank boosts applied after fusion.

Boosts only reorder what retrieval already returned. Running a keyword as its
own query pulls in unrelated memories that merely contain the word, so caller
emphasis is expressed here instead of through extra sub-queries.
"""

from __future__ import annotations

from typing import Callable

from agent_gateway.services.retrieval.types import Query, RetrievedItem

Booster = Callable[[Query, list[RetrievedItem]], None]


def keyword_boost(query: Query, items: list[RetrievedItem]) -> None:
    """Multiply the score of hits whose text quotes a caller keyword.

    In place, because the fusion output is already ours. A no-op when the
    caller supplied no keywords, so the default path is untouched.
    """
    terms = [k.strip().lower() for k in query.keywords if k and k.strip()]
    if not terms or query.keyword_weight == 1.0:
        return
    for item in items:
        text = (item.content or "").lower()
        if any(term in text for term in terms):
            item.score = float(item.score) * query.keyword_weight


__all__ = ["Booster", "keyword_boost"]
