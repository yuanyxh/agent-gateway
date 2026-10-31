"""Intent-driven strategy selection.

The caller knows more than a string: an agent that wants a file path can say so.
A router maps that intent onto a stage chain; anything unrecognised runs the
default chain, so adding an intent never changes existing behaviour.
"""

from __future__ import annotations

from typing import Callable

from agent_gateway.services.retrieval.pipeline import Stage
from agent_gateway.services.retrieval.types import Query

StageBuilder = Callable[[Query], list[Stage]]


class Router:
    """Pick a stage chain for a query, by intent label."""

    def __init__(
        self,
        default: StageBuilder,
        by_intent: dict[str, StageBuilder] | None = None,
    ):
        self._default = default
        self._by_intent = {k.upper(): v for k, v in (by_intent or {}).items()}

    @property
    def intents(self) -> list[str]:
        return sorted(self._by_intent)

    def stages_for(self, query: Query) -> list[Stage]:
        intent = (query.intent or "").strip().upper()
        builder = self._by_intent.get(intent, self._default)
        return builder(query)


__all__ = ["Router", "StageBuilder"]
