"""Single retrieval pipeline: plan, retrieve, fuse, gate, expand, trace."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_gateway.services.retrieval import trace as trace_mod
from agent_gateway.services.retrieval.text.analyzer import detect_language
from agent_gateway.services.retrieval.types import (
    ArmResult,
    Query,
    RetrievedItem,
)

Expander = Callable[[Query, list[RetrievedItem]], list[RetrievedItem]]

#: Candidates fetched per arm; fusion truncates to the caller's limit afterwards.
PER_ARM_LIMIT = 10


@dataclass
class Stage:
    """One tier of a plan. Its arms run together; a non-empty tier ends the walk."""

    name: str
    arms: list[Any] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.arms)


class Pipeline:
    """Composable retrieval profile: stages in, ranked items out."""

    def __init__(
        self,
        name: str,
        arms: list[Any],
        fusion: Any,
        gate: Any,
        expander: Expander | None = None,
        stages: list[Stage] | None = None,
    ):
        self.name = name
        self.arms = arms
        self.fusion = fusion
        self.gate = gate
        self.expander = expander
        self.stages = stages if stages is not None else self._compile(arms)

    def _compile(self, arms: list[Any]) -> list[Stage]:
        """Split arms into a primary tier plus a fallback tier when marked."""
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
        """Walk the tiers; the first tier with a hit stops the walk."""
        arm_limit = PER_ARM_LIMIT or limit
        for stage in self.stages:
            results = [
                self._run_arm(arm, query, arm_limit, filters) for arm in stage.arms
            ]
            if any(result.items for result in results):
                return results
        return []

    def run(
        self,
        text: str,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
        tool: str = "",
    ) -> dict[str, Any]:
        """Convenience entry for callers with only a string."""
        query = Query(text=text, language=detect_language(text), root_text=text)
        return self.run_query(query, limit, filters, tool)

    def run_query(
        self,
        query: Query,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
        tool: str = "",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        latency: dict[str, float] = {}
        if not query.language or query.language == "unknown":
            query = Query(
                text=query.text,
                language=detect_language(query.text),
                root_text=query.root_text or query.text,
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
            query, arms, verdict, top, related, latency, tool
        )
        trace_mod.record(trace_payload)

        return {
            "status": verdict.status,
            "results": [item.to_result() for item in top],
            "related": [item.to_result() for item in related],
            "count": len(top),
            "related_count": len(related),
            "trace_id": trace_payload["trace_id"],
        }

    def _trace_payload(
        self,
        query: Query,
        arms: list[ArmResult],
        verdict: Any,
        top: list[RetrievedItem],
        related: list[RetrievedItem],
        latency: dict[str, float],
        tool: str,
    ) -> dict[str, Any]:
        return {
            "trace_id": trace_mod.new_trace_id(),
            "tool": tool or self.name,
            "profile": self.name,
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
            "gate": {"verdict": verdict.status, "reason": verdict.reason},
            "status": verdict.status,
            "count": len(top),
            "related_count": len(related),
            "latency_ms": latency,
        }
