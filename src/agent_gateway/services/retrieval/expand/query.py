"""Turn caller context into weighted sub-queries.

A bare query expands to itself, so the default path costs nothing and behaves
exactly as before. Keywords and groups are opt-in: the caller says what matters,
this module turns that into terms the pipeline can score separately.
"""

from __future__ import annotations

from typing import Callable

from agent_gateway.services.retrieval.types import Query, SubQuery

Expander = Callable[[Query], list[SubQuery]]


def expand_query(query: Query) -> list[SubQuery]:
    """Text plus each group as its own term set.

    Keywords are deliberately absent: running them as queries pulls in memories
    that contain the word but answer nothing (measured: hit@1 0.68 -> 0.58).
    They act as a re-rank boost instead, see `retrieval.boost`.
    """
    subs = [SubQuery(text=query.text, weight=1.0, label="text")]
    seen = {query.text.strip()}
    for index, group in enumerate(query.groups):
        terms = [t.strip() for t in group if t and t.strip()]
        joined = " ".join(terms)
        if not joined or joined in seen:
            continue
        seen.add(joined)
        subs.append(SubQuery(text=joined, weight=1.0, label=f"group{index}"))
    return subs


__all__ = ["Expander", "expand_query"]
