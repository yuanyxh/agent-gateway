"""Single retrieval pipeline: retrieve, fuse, gate, expand, trace."""

from __future__ import annotations

import time
from typing import Any, Callable

from agent_gateway.services.retrieval import trace as trace_mod
from agent_gateway.services.retrieval.text.analyzer import detect_language
from agent_gateway.services.retrieval.types import (
    ArmResult,
    Query,
    RetrievedItem,
)

Expander = Callable[[Query, list[RetrievedItem]], list[RetrievedItem]]


class Pipeline:
    """Composable retrieval profile: arms in, ranked items out."""

    def __init__(
        self,
        name: str,
        arms: list[Any],
        fusion: Any,
        gate: Any,
        expander: Expander | None = None,
        per_arm_limit: int = 10,
        lexical_only_as_fallback: bool = False,
    ):
        self.name = name
        self.arms = arms
        self.fusion = fusion
        self.gate = gate
        self.expander = expander
        self.per_arm_limit = per_arm_limit
        self.lexical_only_as_fallback = lexical_only_as_fallback

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
        """Run every primary arm, then the fallback arms only if none had hits.

        Fallback arms pay a full notes walk plus re-embedding, and measured 36.4%
        of their rows were unrelated against 1.5% for hot, so they stay out of the
        way unless nothing semantic matched.
        """
        arm_limit = self.per_arm_limit or limit
        primary = [a for a in self.arms if not getattr(a, "fallback_only", False)]
        fallback = [a for a in self.arms if getattr(a, "fallback_only", False)]
        if not self.lexical_only_as_fallback:
            primary, fallback = list(self.arms), []
        results = [self._run_arm(arm, query, arm_limit, filters) for arm in primary]
        if fallback and not any(result.items for result in results):
            results += [
                self._run_arm(arm, query, arm_limit, filters) for arm in fallback
            ]
        return results

    def run(
        self,
        text: str,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
        tool: str = "",
        scope: str = "",
        debug: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        latency: dict[str, float] = {}
        query = Query(text=text, language=detect_language(text), root_text=text)

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
