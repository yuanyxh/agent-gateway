"""Single retrieval pipeline: plan, retrieve, fuse, gate, expand, trace."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from agent_gateway.services.retrieval import trace as trace_mod
from agent_gateway.services.retrieval.expand.query import expand_query
from agent_gateway.services.retrieval.text.analyzer import detect_language
from agent_gateway.services.retrieval.types import (
    ArmResult,
    Query,
    RetrievedItem,
    SubQuery,
)

Expander = Callable[[Query, list[RetrievedItem]], list[RetrievedItem]]


@dataclass
class Stage:
    """One tier of a plan. Its arms run together; the plan stops when it accepts.

    Tiers express progressive fallback: a cheap precise stage first, then
    looser ones. Without `accept` any hit ends the walk, which lets a loose
    stage's noise mask a precise stage below it — `accept` is what makes a tier
    answer "is this good enough to stop?" rather than "is this non-empty?".
    """

    name: str
    arms: list[Any] = field(default_factory=list)
    #: (query, stage items) -> True to stop here, False to fall through.
    accept: Callable[[Query, list["RetrievedItem"]], bool] | None = None
    #: Fewer hits than this counts as a miss and the walk continues. One weak
    #: row is worse than falling through: it hides every tier below it.
    min_hits: int = 1

    def __bool__(self) -> bool:
        return bool(self.arms)

    def accepts(self, query: Query, items: list["RetrievedItem"]) -> bool:
        if len(items) < max(1, self.min_hits):
            return False
        return self.accept is None or self.accept(query, items)


class Pipeline:
    """Composable retrieval profile: stages in, ranked items out."""

    def __init__(
        self,
        name: str,
        arms: list[Any],
        fusion: Any,
        gate: Any,
        expander: Expander | None = None,
        per_arm_limit: int = 10,
        lexical_only_as_fallback: bool = False,
        stages: list[Stage] | None = None,
        query_expander: Callable[[Query], list[SubQuery]] | None = None,
    ):
        self.name = name
        self.arms = arms
        self.fusion = fusion
        self.gate = gate
        self.expander = expander
        self.per_arm_limit = per_arm_limit
        self.lexical_only_as_fallback = lexical_only_as_fallback
        self.query_expander = query_expander or expand_query
        self.stages = stages if stages is not None else self._compile(arms)

    def _compile(self, arms: list[Any]) -> list[Stage]:
        """Translate the legacy arm list into tiers.

        `lexical_only_as_fallback` used to be a boolean switch inside the loop;
        it becomes two tiers, which is what it always meant.
        """
        if not self.lexical_only_as_fallback:
            return [Stage(name="primary", arms=list(arms))]
        primary = [a for a in arms if not getattr(a, "fallback_only", False)]
        fallback = [a for a in arms if getattr(a, "fallback_only", False)]
        stages = [Stage(name="primary", arms=primary)]
        if fallback:
            stages.append(Stage(name="fallback", arms=fallback))
        return stages

    def _run_arm(
        self,
        arm: Any,
        query: Query,
        arm_limit: int,
        filters: dict[str, Any] | None,
    ) -> ArmResult:
        started = time.perf_counter()
        try:
            partial = arm.retrieve(query, arm_limit, filters)
        except Exception as e:  # noqa: BLE001 - one arm must not kill the call
            return ArmResult(
                name=arm.name,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                error=f"{type(e).__name__}: {e}",
            )
        # Truncation happens in fusion: arms keep their own over-fetch window.
        return ArmResult(
            name=arm.name,
            items=sorted(partial.items, key=lambda i: i.score, reverse=True),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=partial.error,
        )

    def _retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None
    ) -> list[ArmResult]:
        """Walk the tiers; the first tier that accepts stops the walk.

        A stage only runs when every earlier stage came back empty, so a loose
        stage can never dilute a precise hit.
        """
        arm_limit = self.per_arm_limit or limit
        subqueries = self.query_expander(query)
        for stage in self.stages:
            results: list[ArmResult] = []
            for sub in subqueries:
                sub_query = Query(
                    text=sub.text,
                    language=query.language,
                    root_text=sub.text,
                    mode=query.mode,
                )
                for arm in stage.arms:
                    result = self._run_arm(arm, sub_query, arm_limit, filters)
                    results.append(_weighted(result, sub))
            stage_items = [item for result in results for item in result.items]
            if stage.accepts(query, stage_items):
                return results
        return []

    def run(
        self,
        text: str,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
        tool: str = "",
        scope: str = "",
        debug: bool = False,
    ) -> dict[str, Any]:
        """Convenience entry for callers with only a string."""
        query = Query(text=text, language=detect_language(text), root_text=text)
        return self.run_query(query, limit, filters, tool, scope, debug)

    def run_query(
        self,
        query: Query,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
        tool: str = "",
        scope: str = "",
        debug: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        latency: dict[str, float] = {}
        if not query.language or query.language == "unknown":
            query = Query(
                text=query.text,
                language=detect_language(query.text),
                root_text=query.root_text or query.text,
                mode=query.mode,
            )

        retrieve_started = time.perf_counter()
        arms = self._retrieve(query, limit, filters)
        latency["retrieve"] = round((time.perf_counter() - retrieve_started) * 1000, 2)

        fuse_started = time.perf_counter()
        items = self.fusion.fuse(arms, limit * 2)
        latency["fuse"] = round((time.perf_counter() - fuse_started) * 1000, 2)

        related = self.expander(query, items[:limit]) if self.expander else []
        verdict = self.gate.judge(query, items[:limit], related)

        top = items[:limit]
        related = related[:limit]
        latency["total"] = round((time.perf_counter() - started) * 1000, 2)

        trace_payload = self._trace_payload(
            query, arms, verdict, top, related, latency, tool, scope
        )
        trace_mod.record(trace_payload)

        payload = {
            "status": verdict.status,
            "results": [item.to_result() for item in top],
            "related": [item.to_result() for item in related],
            "count": len(top),
            "related_count": len(related),
            "trace_id": trace_payload["trace_id"],
            "_items": top,
            "_related": related,
            "_verdict": verdict,
            "_query": query,
            "_arms": arms,
            "_latency": latency,
        }
        if debug:
            payload["trace"] = {
                key: value for key, value in trace_payload.items() if key != "query"
            }
        return payload

    def _trace_payload(
        self,
        query: Query,
        arms: list[ArmResult],
        verdict: Any,
        top: list[RetrievedItem],
        related: list[RetrievedItem],
        latency: dict[str, float],
        tool: str,
        scope: str,
    ) -> dict[str, Any]:
        return {
            "trace_id": trace_mod.new_trace_id(),
            "tool": tool or self.name,
            "profile": self.name,
            "scope": scope,
            "query": query.text,
            "query_lang": query.language,
            "arms": [
                {
                    "name": arm.name,
                    "elapsed_ms": arm.elapsed_ms,
                    "error": arm.error,
                    "hits": [
                        {
                            "id": i.id,
                            "score": float(i.score),
                            "score_kind": i.score_kind,
                        }
                        for i in arm.items
                    ],
                }
                for arm in arms
            ],
            "gate": {
                "verdict": verdict.status,
                "reason": verdict.reason,
                "top1": verdict.top1,
                "median": verdict.median,
                "margin": verdict.margin,
                "lexical_anchor": verdict.lexical_anchor,
            },
            "status": verdict.status,
            "count": len(top),
            "related_count": len(related),
            "latency_ms": latency,
        }


def _weighted(result: ArmResult, sub: SubQuery) -> ArmResult:
    """Scale a sub-query's hits so primary terms outrank the raw text.

    The arm suffix keeps the trace readable: `dense_hot@group0` says which
    expansion produced the row.
    """
    if sub.weight == 1.0 or not result.items:
        return result
    name = f"{result.name}@{sub.label}" if sub.label else result.name
    return ArmResult(
        name=name,
        items=[
            replace(item, score=float(item.score) * sub.weight) for item in result.items
        ],
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )
