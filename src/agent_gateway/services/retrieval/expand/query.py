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

    Keywords stay out of this expansion: as equal-weight sub-queries they pull
    in memories that contain the word but answer nothing (measured hit@1 0.68
    -> 0.58). `expand_keywords` gives them their own weighted tier instead.
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


def expand_keywords(query: Query) -> list[SubQuery]:
    """One sub-query per primary keyword, each carrying the caller's weight.

    Separate lists rather than one joined query: a keyword is a claim about
    what this lookup must contain, so each gets its own shot at the index and
    the fusion adds up how many of them a record answered.
    """
    subs: list[SubQuery] = []
    for index, term in enumerate(query.keywords):
        clean = (term or "").strip()
        if not clean:
            continue
        subs.append(
            SubQuery(text=clean, weight=query.keyword_weight, label=f"kw{index}")
        )
    return subs


def expand_keywords_with_query(query: Query) -> list[SubQuery]:
    """Keyword lists plus the raw query, scored in one ranking.

    The raw query keeps a weight below the keywords (its own `query_weight`),
    so a record that answers the caller's emphasis outranks one that merely
    looks like the question.
    """
    subs = expand_keywords(query)
    text = (query.text or "").strip()
    if text:
        subs.append(SubQuery(text=text, weight=query.query_weight, label="text"))
    return subs


__all__ = [
    "Expander",
    "expand_keywords",
    "expand_keywords_with_query",
    "expand_query",
]
