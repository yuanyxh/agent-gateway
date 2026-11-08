"""Single retrieval pipeline: plan, retrieve, fuse, gate, expand, trace."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.retrieval import trace as trace_mod
from agent_gateway.services.retrieval.fusion.score import RRFFusion
from agent_gateway.services.retrieval.text.analyzer import detect_language
from agent_gateway.services.retrieval.types import (
    ArmResult,
    Query,
    RetrievedItem,
)

logger = setup_logging("server")

Expander = Callable[
    [Query, list[RetrievedItem], dict[str, Any] | None], list[RetrievedItem]
]
Reranker = Callable[[Query, list[RetrievedItem]], list[RetrievedItem]]

#: Candidates fetched per arm; fusion truncates to the caller's limit afterwards.
PER_ARM_LIMIT = 10

#: Each matching boost field lifts a row's rank by 10%; three fields cap the lift
#: at 1.30. Only order changes, never the score the gate reads.
BOOST_STEP = 0.10
BOOST_MAX_FIELDS = 3


@dataclass
class Stage:
    """One tier of a plan.

    Tiers run in order until the caller's `limit` is filled, so the depth walked
    per call is the request itself rather than a tuned constant. `only_if_empty`
    keeps a tier as a last resort: it is skipped as soon as anything was found
    above, regardless of how short of `limit` that was.
    """

    name: str
    arms: list[Any] = field(default_factory=list)
    only_if_empty: bool = False

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
        reranker: Reranker | None = None,
        semantic_fusion: Any = None,
    ):
        self.name = name
        self.arms = arms
        self.fusion = fusion
        self.gate = gate
        self.expander = expander
        self.reranker = reranker
        self.stages = stages if stages is not None else self._compile(arms)
        #: Merges the keyword pass with the semantic pass; only used when the
        #: caller supplied both query forms.
        self.semantic_fusion = semantic_fusion or RRFFusion()

    def _compile(self, arms: list[Any]) -> list[Stage]:
        """Split arms into a primary tier plus a last-resort tier when marked."""
        primary = [a for a in arms if not getattr(a, "fallback_only", False)]
        fallback = [a for a in arms if getattr(a, "fallback_only", False)]
        stages = [Stage(name="primary", arms=primary)]
        if fallback:
            stages.append(Stage(name="fallback", arms=fallback, only_if_empty=True))
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
        """Walk the tiers until `limit` distinct items are held or tiers run out."""
        # Over-fetch covers the per-arm default, but a caller asking for more
        # must reach the arm or fusion can never fill that limit. A constraint
        # narrows after retrieval, so the window widens to keep matches reachable.
        factor = 4 if query.filters else 1
        arm_limit = max(PER_ARM_LIMIT, limit) * factor
        collected: list[ArmResult] = []
        seen: set[str] = set()
        for stage in self.stages:
            if stage.only_if_empty and seen:
                break
            results = [
                self._run_arm(arm, query, arm_limit, filters) for arm in stage.arms
            ]
            collected.extend(results)
            for result in results:
                seen.update(row.id for row in result.items)
            if len(seen) >= limit:
                break
        return collected

    def _query_variants(self, query: Query) -> list[tuple[str, Query]]:
        """The keyword pass plus, when supplied, a separate semantic pass."""
        variants = [("primary", query)]
        semantic = query.semantic_text.strip()
        if semantic and semantic != query.text.strip():
            variants.append(
                (
                    "semantic",
                    Query(
                        text=semantic,
                        language=detect_language(semantic),
                        root_text=query.root_text or query.text,
                        filters=query.filters,
                        boost=query.boost,
                        expose=query.expose,
                    ),
                )
            )
        return variants

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
                semantic_text=query.semantic_text,
                filters=query.filters,
                boost=query.boost,
                expose=query.expose,
            )

        retrieve_started = time.perf_counter()
        passes: list[tuple[str, list[ArmResult]]] = []
        for label, variant in self._query_variants(query):
            passes.append((label, self._retrieve(variant, limit, filters)))
        latency["retrieve"] = round((time.perf_counter() - retrieve_started) * 1000, 2)

        # Narrowing runs before fusion truncates, or a matching row ranked below
        # the fused cut would be dropped without ever being checked.
        retrieved = sum(len(arm.items) for _, arms in passes for arm in arms)
        passes = [
            (label, [self._filtered_arm(arm, query) for arm in arms])
            for label, arms in passes
        ]
        filtered = retrieved - sum(len(arm.items) for _, arms in passes for arm in arms)

        fuse_started = time.perf_counter()
        if len(passes) == 1:
            items = self.fusion.fuse(passes[0][1], limit * 2)
        else:
            # Ranks from two query forms are not on one scale, so only their
            # order is merged.
            ranked = [
                ArmResult(name=label, items=self.fusion.fuse(arms, limit * 2))
                for label, arms in passes
            ]
            items = self.semantic_fusion.fuse(ranked, limit * 2)
        latency["fuse"] = round((time.perf_counter() - fuse_started) * 1000, 2)

        items = self._narrow(query, items)
        if self.reranker is not None and items:
            rerank_started = time.perf_counter()
            try:
                items = self.reranker(query, items)
            except Exception as e:  # noqa: BLE001 - reranking must not kill the call
                logger.warning(f"[pipeline:{self.name}] rerank failed: {e!r}")
            latency["rerank"] = round((time.perf_counter() - rerank_started) * 1000, 2)

        items = self._prefer(query, items)
        related = self.expander(query, items[:limit], filters) if self.expander else []
        # Expansion reaches other groups through edges; a constraint must still
        # hold for what the caller sees as related context.
        related = self._narrow(query, related)
        verdict = self.gate.judge(query, items[:limit], related)

        top = items[:limit]
        related = related[:limit]
        errors = [
            {"pass": label, "arm": arm.name, "error": arm.error}
            for label, arms in passes
            for arm in arms
            if arm.error
        ]
        latency["total"] = round((time.perf_counter() - started) * 1000, 2)

        trace_payload = self._trace_payload(
            query, passes, verdict, top, related, latency, tool
        )
        trace_mod.record(trace_payload)

        return {
            "status": verdict.status,
            "results": [item.to_result(query.expose) for item in top],
            "related": [item.to_result(query.expose) for item in related],
            "count": len(top),
            "related_count": len(related),
            # Rows a caller's constraint removed; a miss and a filtered-out
            # match look identical otherwise.
            "filtered": filtered,
            # Arm failures are otherwise indistinguishable from a real miss.
            "errors": errors,
            "trace_id": trace_payload["trace_id"],
        }

    @staticmethod
    def _allowed(value: Any) -> set[str]:
        if isinstance(value, (set, frozenset, list, tuple)):
            return {str(item) for item in value if str(item)}
        text = str(value or "")
        return {text} if text else set()

    def _filtered_arm(self, result: ArmResult, query: Query) -> ArmResult:
        """Apply the caller's constraint inside one arm, before fusion truncates."""
        items = self._narrow(query, result.items)
        if len(items) == len(result.items):
            return result
        return ArmResult(
            name=result.name,
            items=items,
            elapsed_ms=result.elapsed_ms,
            error=result.error,
        )

    def _narrow(self, query: Query, items: list[RetrievedItem]) -> list[RetrievedItem]:
        """Drop rows a caller's exact-value constraint rules out."""
        if not query.filters:
            return items
        wanted = {key: self._allowed(value) for key, value in query.filters.items()}
        return [
            item
            for item in items
            if all(
                str(item.extra.get(key) or "") in values
                for key, values in wanted.items()
            )
        ]

    def _prefer(self, query: Query, items: list[RetrievedItem]) -> list[RetrievedItem]:
        """Reorder rows a caller's preferred values point at; scores stay intact."""
        if not query.boost:
            return items
        wanted = {key: self._allowed(value) for key, value in query.boost.items()}
        # Re-sorting must keep the order fusion established, so reuse its rank
        # key; a plain score sort would discard recency/access/quality weights.
        rank_key = getattr(self.fusion, "rank_key", None)

        def lift(item: RetrievedItem) -> float:
            hits = sum(
                1
                for key, values in wanted.items()
                if str(item.extra.get(key) or "") in values
            )
            base = rank_key(item) if rank_key else float(item.score)
            return float(base) * (1 + BOOST_STEP * min(hits, BOOST_MAX_FIELDS))

        return sorted(items, key=lambda item: -lift(item))

    def _trace_payload(
        self,
        query: Query,
        passes: list[tuple[str, list[ArmResult]]],
        verdict: Any,
        top: list[RetrievedItem],
        related: list[RetrievedItem],
        latency: dict[str, float],
        tool: str,
    ) -> dict[str, Any]:
        lead = top[0] if top else None
        return {
            "trace_id": trace_mod.new_trace_id(),
            "tool": tool or self.name,
            "profile": self.name,
            "query": query.text,
            "semantic_query": query.semantic_text,
            "query_lang": query.language,
            "passes": [
                {
                    "name": label,
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
                }
                for label, arms in passes
            ],
            # What the caller actually received, with locators. Adoption analysis
            # needs these fingerprints to check later references; hits include
            # candidates that were fused away or cut by the limit.
            "returned": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "layer": item.layer,
                    "score": float(item.score),
                    "score_kind": item.score_kind,
                    "locator": item.locator,
                }
                for item in top
            ],
            # The lead hit is what the caller is most likely to adopt; keeping it
            # separate makes "was the top result the right one" answerable later
            # without re-deriving the order.
            "lead": (
                {
                    "id": lead.id,
                    "layer": lead.layer,
                    "score": float(lead.score),
                    "score_kind": lead.score_kind,
                }
                if lead is not None
                else None
            ),
            "fusion": self.semantic_fusion.name
            if len(passes) > 1
            else self.fusion.name,
            "reranked": self.reranker is not None,
            "gate": {"verdict": verdict.status, "reason": verdict.reason},
            "status": verdict.status,
            "count": len(top),
            "related_count": len(related),
            "latency_ms": latency,
        }
