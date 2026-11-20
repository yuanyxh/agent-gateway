"""Single retrieval pipeline: plan, retrieve, fuse, gate, expand, trace."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
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

#: Extra single-term passes. Three covers the most specific keywords without
#: multiplying arm latency by the whole list.
MAX_TERM_VARIANTS = 3

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
            stats=partial.stats,
            weak_items=partial.weak_items,
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
        """Keyword pass, semantic pass, then one pass per specific term."""
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

    def _term_variants(self, query: Query) -> list[tuple[str, Query]]:
        """One pass per specific term, added only when the base passes are short."""
        seen = {q.text.strip() for _, q in self._query_variants(query)}
        terms = sorted(
            {t.strip() for t in query.terms if len(t.strip()) >= 2},
            key=lambda t: (-len(t), t),
        )
        variants: list[tuple[str, Query]] = []
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            variants.append(
                (
                    f"term{len(variants) + 1}",
                    Query(
                        text=term,
                        language=detect_language(term),
                        root_text=query.root_text or query.text,
                        filters=query.filters,
                        boost=query.boost,
                        expose=query.expose,
                    ),
                )
            )
            if len(variants) >= MAX_TERM_VARIANTS:
                break
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
        # Each extra pass costs an embedding round trip, so terms only run when
        # the base passes leave the result short.
        seen_ids = {item.id for _, arms in passes for arm in arms for item in arm.items}
        if len(seen_ids) < limit:
            for label, variant in self._term_variants(query):
                passes.append((label, self._retrieve(variant, limit, filters)))
        latency["retrieve"] = round((time.perf_counter() - retrieve_started) * 1000, 2)

        # Narrowing runs before fusion truncates, or a matching row ranked below
        # the fused cut would be dropped without ever being checked.
        retrieved = sum(len(arm.items) for _, arms in passes for arm in arms)
        narrowed: list[tuple[str, list[ArmResult]]] = []
        fallback: list[RetrievedItem] = []
        weak_pool: list[RetrievedItem] = []
        for label, arms in passes:
            kept: list[ArmResult] = []
            for arm in arms:
                result, rejected = self._filtered_arm(arm, query)
                kept.append(result)
                fallback.extend(rejected)
                weak_pool.extend(self._narrow(query, arm.weak_items))
            narrowed.append((label, kept))
        passes = narrowed
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
        strict = items[:limit]
        strict_ids = {item.id for item in strict}
        merged = list(strict)
        if len(strict) < limit:
            # Near-miss rows below the floor come first: they passed every other
            # filter and were only rejected on score.
            merged.extend(
                self._weak_candidates(weak_pool, limit - len(merged), strict_ids)
            )
        if len(merged) < limit:
            # A short strict result is padded from excluded rows, and the padded
            # rows must pass through the same rerank as the strict ones.
            merged.extend(
                self._fallback_candidates(
                    query, fallback, limit - len(merged), strict_ids
                )
            )
        if self.reranker is not None and merged:
            rerank_started = time.perf_counter()
            try:
                ranked = self.reranker(query, merged)
            except Exception as e:  # noqa: BLE001 - reranking must not kill the call
                logger.warning(f"[pipeline:{self.name}] rerank failed: {e!r}")
            else:
                # Rerank may lift a padded row; the caller asked for strict rows,
                # so keep that group ahead and preserve rerank order inside both.
                reranked_ids = {item.id for item in ranked}
                dropped = [item for item in merged if item.id not in reranked_ids]
                merged = [
                    *[item for item in ranked if item.id in strict_ids],
                    *[item for item in ranked if item.id not in strict_ids],
                ]
                # Revive strict and filter-excluded rows; never floor padding.
                trusted_dropped = [
                    item
                    for item in dropped
                    if item.id in strict_ids or item.extra.get("filter_fallback")
                ]
                if len(merged) < limit and trusted_dropped:
                    merged.extend(
                        replace(item, extra={**item.extra, "rerank_fallback": True})
                        for item in trusted_dropped[: limit - len(merged)]
                    )
            latency["rerank"] = round((time.perf_counter() - rerank_started) * 1000, 2)

        items = [
            *self._prefer(query, [item for item in merged if item.id in strict_ids]),
            *self._prefer(
                query, [item for item in merged if item.id not in strict_ids]
            ),
        ]
        related = self.expander(query, items[:limit], filters) if self.expander else []
        # Expansion reaches other groups through edges; a constraint must still
        # hold for what the caller sees as related context.
        related = self._narrow(query, related)

        top = items[:limit]
        fallback_count = sum(1 for item in top if item.extra.get("filter_fallback"))
        floor_fallback_count = sum(
            1 for item in top if item.extra.get("floor_fallback")
        )
        rerank_fallback_count = sum(
            1 for item in top if item.extra.get("rerank_fallback")
        )
        verdict = self.gate.judge(query, top, related)
        padded = fallback_count + floor_fallback_count + rerank_fallback_count
        if top and padded >= len(top) and verdict.status == "ok":
            if rerank_fallback_count:
                reason = "rerank_fallback_only"
            elif floor_fallback_count:
                reason = "floor_fallback_only"
            else:
                reason = "filter_fallback_only"
            verdict = replace(verdict, status="weak_match", reason=reason)

        related = related[:limit]
        errors = [
            {"pass": label, "arm": arm.name, "error": arm.error}
            for label, arms in passes
            for arm in arms
            if arm.error
        ]
        latency["total"] = round((time.perf_counter() - started) * 1000, 2)

        trace_payload = self._trace_payload(
            query,
            passes,
            verdict,
            top,
            related,
            latency,
            tool,
            filtered,
            fallback_count,
            floor_fallback_count,
            rerank_fallback_count,
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
            # How many of those rows came back to fill a short result.
            "fallback_count": fallback_count,
            # Floor misses kept only because the strict result was short.
            "floor_fallback_count": floor_fallback_count,
            # Rows the cross-encoder rejected that the short result still needs.
            "rerank_fallback_count": rerank_fallback_count,
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

    def _filtered_arm(
        self, result: ArmResult, query: Query
    ) -> tuple[ArmResult, list[RetrievedItem]]:
        """Split one arm into kept rows and rows the constraint excluded."""
        items = self._narrow(query, result.items)
        if len(items) == len(result.items):
            return result, []
        kept_ids = {item.id for item in items}
        return (
            ArmResult(
                name=result.name,
                items=items,
                elapsed_ms=result.elapsed_ms,
                error=result.error,
                stats=result.stats,
                weak_items=result.weak_items,
            ),
            [item for item in result.items if item.id not in kept_ids],
        )

    @staticmethod
    def _weak_candidates(
        pool: list[RetrievedItem], remaining: int, exclude: set[str]
    ) -> list[RetrievedItem]:
        """Floor misses that passed every other filter, offered to fill a gap."""
        if remaining <= 0 or not pool:
            return []
        best: dict[str, RetrievedItem] = {}
        for item in pool:
            if item.id in exclude:
                continue
            current = best.get(item.id)
            if current is None or item.score > current.score:
                best[item.id] = item
        ordered = sorted(best.values(), key=lambda i: float(i.score), reverse=True)
        return [
            replace(item, extra={**item.extra, "floor_fallback": True})
            for item in ordered[:remaining]
        ]

    def _fallback_candidates(
        self,
        query: Query,
        fallback: list[RetrievedItem],
        remaining: int,
        exclude: set[str],
    ) -> list[RetrievedItem]:
        """Rows a constraint excluded, offered to fill a short strict result."""
        if remaining <= 0 or not fallback:
            return []
        pool: dict[str, RetrievedItem] = {}
        for item in fallback:
            if item.id in exclude:
                continue
            current = pool.get(item.id)
            if current is None or item.score > current.score:
                pool[item.id] = item
        rank_key = getattr(self.fusion, "rank_key", None)
        if rank_key is None:
            ordered = sorted(pool.values(), key=lambda i: float(i.score), reverse=True)
        else:
            ordered = sorted(pool.values(), key=rank_key, reverse=True)
        out: list[RetrievedItem] = []
        for item in ordered[:remaining]:
            misses = [
                key
                for key, values in query.filters.items()
                if str(item.extra.get(key) or "") not in self._allowed(values)
            ]
            out.append(replace(item, extra={**item.extra, "filter_fallback": misses}))
        return out

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
        filtered: int = 0,
        fallback_count: int = 0,
        floor_fallback_count: int = 0,
        rerank_fallback_count: int = 0,
    ) -> dict[str, Any]:
        lead = top[0] if top else None
        return {
            "trace_id": trace_mod.current_or_new(),
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
                            "stats": arm.stats,
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
                    "filter_fallback": item.extra.get("filter_fallback") or [],
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
            # Rows a caller's constraint removed and how many came back.
            "filtered": filtered,
            "fallback_count": fallback_count,
            "floor_fallback_count": floor_fallback_count,
            "rerank_fallback_count": rerank_fallback_count,
            "latency_ms": latency,
        }
