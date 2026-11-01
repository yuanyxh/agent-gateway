"""Recall arms: hot memory, cold summaries, and notes keyword fallback."""

from __future__ import annotations

import os
from typing import Any

import numpy as np
from qdrant_client.models import FieldCondition, Filter, MatchValue

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.memory_lifecycle import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DELETED,
    STATUS_FIELD,
    STATUS_SUPERSEDED,
)
from agent_gateway.services.mcp.server_constants import (
    COLD_COLLECTION,
    COLD_STALE_FIELD,
    DEFAULT_HEAT_SCORE,
    DEFAULT_USER_ID,
    EPISODE_EXPAND_DECAY,
    EPISODE_EXPAND_PER_GROUP,
    EPISODE_EXPAND_TOP_N,
    EPISODE_FIELD,
    EXCLUDE_DIRS,
    HEAT_INCREMENT,
    HEAT_MAX,
    NOTE_EXTENSIONS,
    NOTES_DIR,
)
from agent_gateway.services.mcp import server_embed, server_state
from agent_gateway.services.memory import graph as memory_graph
from agent_gateway.services.memory import store as memory_store
from agent_gateway.services.retrieval.gate.thresholds import floors_for
from agent_gateway.services.retrieval.text.analyzer import split_terms
from agent_gateway.services.retrieval.text.matching import (
    notes_coverage,
    notes_snippet,
)
from agent_gateway.services.retrieval.types import ArmResult, Query, RetrievedItem

logger = setup_logging("server")

#: Decision fields only; timestamps, heat and access counters live in the trace.
_HOT_RESULT_KEYS = ("id", "layer", "content", "score")
_HOT_META_KEYS = ("tags", "confidence", STATUS_FIELD)


def memory_status(meta: dict | None) -> str:
    """Lifecycle status; legacy entries without the field count as active."""
    return str((meta or {}).get(STATUS_FIELD) or STATUS_ACTIVE)


def project_hot_result(row: dict) -> dict:
    """Hot-layer row -> model-facing shape, trimmed to the whitelist.

    New writes store the fact and its context separately; the response shows the
    fact as content plus context on its own, never the embedded ``fact + Context``
    text that was stored as the vector input.
    """
    out: dict[str, Any] = {k: row[k] for k in _HOT_RESULT_KEYS if k in row}
    if "content" not in out:
        out["content"] = row.get("memory", "")
    meta = row.get("metadata")
    if isinstance(meta, dict):
        fact = str(meta.get("fact") or "")
        if fact:
            out["content"] = fact
        context = str(meta.get("context") or "")
        if context:
            out["context"] = context
        kept = {k: meta[k] for k in _HOT_META_KEYS if k in meta}
        if kept:
            out["metadata"] = kept
        episode = str(meta.get(EPISODE_FIELD) or "").strip()
        if episode:
            out["episode_id"] = episode
    return out


def dense_vector(raw: Any) -> list[float] | None:
    """Qdrant keeps the dense vector under ''; the BM25 one is a sparse dict."""
    if isinstance(raw, dict):
        raw = raw.get("") or raw.get("dense")
    return raw or None


def dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def cosine(a, b) -> float:
    denominator = (dot(a, a) ** 0.5) * (dot(b, b) ** 0.5)
    return float(dot(a, b) / denominator) if denominator > 1e-10 else 0.0


def heat_key(item: RetrievedItem, heat_max: float = HEAT_MAX) -> float:
    return float(item.score) * (
        0.8 + 0.2 * min(float(item.extra.get("heat_score", 0.5)), heat_max)
    )


class HotMemoryArm:
    """mem0 hot layer, with lifecycle filtering done in Python."""

    name = "dense_hot"

    def __init__(
        self,
        min_score: float,
        include_archived: bool = False,
        include_superseded: bool = False,
        literal_coverage: float = 0.5,
    ):
        self._min_score = min_score
        self._include_archived = include_archived
        self._include_superseded = include_superseded
        #: Share of the query's terms that must appear verbatim in a low-scoring
        #: memory for it to survive the cosine floor. Entity names score badly
        #: against long records but are exactly what the user typed.
        self._literal_coverage = literal_coverage

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        hot_memory = server_state.get_hot_memory()
        if hot_memory is None:
            return ArmResult(name=self.name)
        items: list[RetrievedItem] = []
        needles = split_terms(query.root_text or query.text)
        try:
            # No score gate inside mem0: it filters on the semantic score before
            # hybrid scoring, so a memory that literally contains the query terms
            # is dropped there and never reaches this arm. The floor is applied
            # below, where a verbatim hit can override it.
            search_filters: dict[str, Any] = {"user_id": DEFAULT_USER_ID}
            if not self._include_archived:
                # Keep archived rows out of the top_k slots instead of fetching
                # them and dropping them afterwards.
                search_filters["NOT"] = [{"archived": True}]
            raw = hot_memory.search(
                query.text,
                filters=search_filters,
                top_k=max(limit * 8, limit + 20),
                threshold=0.0,
                explain=True,
            )
            rows = raw if isinstance(raw, list) else raw.get("results", [])
            for row in rows:
                if isinstance(row, str):
                    continue
                meta = row.get("metadata") or {}
                # Archive writes land on the top-level payload as well as metadata,
                # so both scopes are honoured here.
                if not self._include_archived and (
                    meta.get("archived") or row.get("archived")
                ):
                    continue
                status = str(
                    meta.get(STATUS_FIELD) or row.get("status") or STATUS_ACTIVE
                )
                if status == STATUS_DELETED:
                    continue
                if status == STATUS_ARCHIVED and not self._include_archived:
                    continue
                if status == STATUS_SUPERSEDED and not self._include_superseded:
                    continue
                try:
                    score = float(row.get("score"))
                except (TypeError, ValueError):
                    continue
                # mem0's final score is a hybrid (semantic + BM25) / 2, so it is not
                # comparable to a cosine floor: when only one candidate survives the
                # semantic gate, the BM25 divisor still halves it (0.75 -> 0.37).
                # The gate reads the semantic score, which is what the floor was
                # calibrated on.
                semantic = (row.get("score_details") or {}).get("semantic_score")
                if semantic is not None:
                    score = float(semantic)
                match_kind = "direct"
                if score < self._min_score:
                    text = str(row.get("memory") or row.get("content") or "")
                    if (
                        not needles
                        or notes_coverage(text, needles) < self._literal_coverage
                    ):
                        continue
                    match_kind = "literal"
                items.append(self._project(row, meta, score, match_kind))
        except Exception as e:  # noqa: BLE001 - degrade to the cold layer
            logger.warning(f"Hot search failed: {e}")
            return ArmResult(name=self.name, error=str(e))
        if items:
            # Quality scores live on the graph node, not in the mem0 payload. A graph
            # failure must not discard results this arm already has: the pipeline
            # treats any exception here as a failed arm with no items at all.
            try:
                nodes = memory_store.get_nodes([item.id for item in items])
            except Exception as e:  # noqa: BLE001 - signals are optional
                logger.warning(f"Hot relevance lookup failed: {e}")
                nodes = {}
            for item in items:
                node = nodes.get(item.id)
                if node:
                    item.extra["relevance"] = (node.get("metadata") or {}).get(
                        "relevance"
                    )
        return ArmResult(name=self.name, items=items)

    def _project(
        self, row: dict, meta: dict, score: float, match_kind: str = "direct"
    ) -> RetrievedItem:
        projected = project_hot_result(
            {**row, "score": score, "layer": "hot", "match_kind": match_kind}
        )
        extra = {
            key: value
            for key, value in projected.items()
            if key not in ("id", "layer", "content", "score", "match_kind")
        }
        # Ranking signals live in metadata but stay out of the response whitelist.
        extra.update(
            {
                "heat_score": meta.get("heat_score", DEFAULT_HEAT_SCORE),
                "access_count": meta.get("access_count", 0),
                "last_accessed_at": meta.get("last_accessed_at", ""),
                "relevance": meta.get("relevance"),
            }
        )
        return RetrievedItem(
            id=str(projected.get("id", "")),
            kind="memory",
            score=float(projected.get("score", 0.0)),
            score_kind="dense",
            content=projected.get("content", ""),
            layer="hot",
            match_kind=match_kind,
            extra=extra,
        )


class ColdMemoryArm:
    """Qdrant cold summaries with scoring floor and heat update."""

    name = "dense_cold"

    def __init__(self, min_score: float, include_archived: bool = False):
        self._min_score = min_score
        self._include_archived = include_archived

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        client = server_state.get_qdrant()
        if client is None:
            return ArmResult(name=self.name)
        query_vec = server_embed.embed_query(query.text)
        if query_vec is None:
            return ArmResult(name=self.name)
        items: list[RetrievedItem] = []
        try:
            must_not = [
                FieldCondition(key=COLD_STALE_FIELD, match=MatchValue(value=True))
            ]
            if not self._include_archived:
                must_not.append(
                    FieldCondition(key="archived", match=MatchValue(value=True))
                )
            resp = client.query_points(
                collection_name=COLD_COLLECTION,
                query=query_vec.tolist(),
                limit=limit,
                query_filter=Filter(must_not=must_not),
                score_threshold=self._min_score,
            )
            for hit in resp.points:
                if hit.score is None or hit.score < self._min_score:
                    continue
                payload = hit.payload or {}
                items.append(
                    RetrievedItem(
                        id=str(hit.id),
                        kind="memory",
                        score=float(hit.score),
                        score_kind="dense",
                        content=payload.get("content", ""),
                        layer="cold",
                        extra={
                            "source_file": payload.get("source_file", ""),
                            "created_at": payload.get("created_at", ""),
                            "type": payload.get("type", ""),
                            "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
                            "access_count": payload.get("access_count", 0),
                            "last_accessed_at": payload.get("last_accessed_at", ""),
                            "relevance": payload.get("relevance"),
                            "episode_id": payload.get(EPISODE_FIELD, ""),
                        },
                    )
                )
                update_cold_heat(hit.id)
        except Exception as e:  # noqa: BLE001 - keep notes results usable
            logger.warning(f"Qdrant cold search failed: {e}")
            return ArmResult(name=self.name, items=items, error=str(e))
        return ArmResult(name=self.name, items=items)


class NotesArm:
    """Notes keyword coverage, then a semantic re-check on the matched snippet.

    Coverage alone cannot tell "about this topic" from "mentions these words":
    measured on 368 annotated queries, 32% of lexical hits were unrelated and
    they sat at every coverage level including 1.0. Re-embedding the snippet
    against the query separates the two (0.40 keeps ~70% of useful hits and
    drops ~73% of noise).
    """

    name = "lexical_notes"
    #: Skipped entirely while a semantic arm has hits; see Pipeline._retrieve.
    fallback_only = True

    def __init__(
        self,
        min_coverage: float,
        min_semantic: float = 0.0,
        semantic_candidates: int = 6,
    ):
        self._min_coverage = min_coverage
        self._min_semantic = min_semantic
        # Only the top coverage hits are re-embedded: 70 candidates cost 2.5s.
        self._semantic_candidates = semantic_candidates

    def _semantic_filter(
        self, text: str, items: list[RetrievedItem]
    ) -> list[RetrievedItem]:
        query_vec = server_embed.embed_query(text)
        if query_vec is None:
            return items
        vectors = server_embed.embed_texts([item.content[:200] for item in items])
        if len(vectors) != len(items):
            return items
        kept = [
            item
            for item, vector in zip(items, vectors)
            if cosine(query_vec, vector) >= self._min_semantic
        ]
        return kept

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        needles = split_terms(query.root_text or query.text)
        if not needles:
            return ArmResult(name=self.name)
        head = needles[0]
        items: list[RetrievedItem] = []
        try:
            for root, dirs, files in os.walk(NOTES_DIR):
                dirs[:] = [
                    d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")
                ]
                for name in files:
                    if not name.lower().endswith(NOTE_EXTENSIONS):
                        continue
                    path = os.path.join(root, name)
                    rel = os.path.relpath(path, NOTES_DIR)
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                    except OSError:
                        continue
                    coverage = notes_coverage(text, needles)
                    if coverage < self._min_coverage:
                        continue
                    items.append(
                        RetrievedItem(
                            id=f"notes:{rel}",
                            kind="note",
                            score=0.5 + 0.1 * coverage,
                            score_kind="lexical",
                            content=notes_snippet(text, head) or rel,
                            layer="notes",
                            locator={"file": rel},
                            extra={
                                "source_file": rel,
                                "type": "notes",
                                "heat_score": DEFAULT_HEAT_SCORE,
                            },
                        )
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Notes search failed: {e}")
            return ArmResult(name=self.name, items=items, error=str(e))
        if self._min_semantic > 0 and items:
            items.sort(key=lambda item: -item.score)
            items = items[: self._semantic_candidates]
            items = self._semantic_filter(query.root_text or query.text, items)
        return ArmResult(name=self.name, items=items)


def update_cold_heat(point_id: str) -> None:
    client = server_state.get_qdrant()
    if client is None:
        return
    from datetime import datetime

    try:
        existing = client.retrieve(
            collection_name=COLD_COLLECTION, ids=[point_id], with_payload=True
        )
        if not existing:
            return
        payload = existing[0].payload or {}
        client.set_payload(
            collection_name=COLD_COLLECTION,
            payload={
                "access_count": payload.get("access_count", 0) + 1,
                "last_accessed_at": datetime.now().isoformat(),
                "heat_score": min(
                    HEAT_MAX,
                    payload.get("heat_score", DEFAULT_HEAT_SCORE) + HEAT_INCREMENT,
                ),
            },
            points=[point_id],
        )
    except Exception:  # noqa: BLE001 - heat is best-effort
        pass


#: Edge types expanded around the primary results. Episode links are handled by
#: the vector-store walk above, so they are not repeated here.
_EDGE_EXPAND_TYPES = ("supersedes", "disputes", "relates", "mentions", "derived_from")
_EDGE_EXPAND_SEEDS = EPISODE_EXPAND_TOP_N
_EDGE_EXPAND_PER_SEED = 5


def expand_episodes(query: Query, items: list[RetrievedItem]) -> list[RetrievedItem]:
    """Same-episode siblings plus one-hop graph neighbours.

    Both are scored by their own similarity to the query. A missing query vector
    drops the graph walk but leaves the primary order untouched.
    """
    if not items:
        return []
    query_vec = server_embed.embed_query(query.text)
    min_score = floors_for(server_embed.EMBEDDER_SPEC.tag)["expanded"]
    hot = (
        _expand_hot(items, query_vec, min_score)
        if server_state.get_hot_memory()
        else []
    )
    cold = (
        _expand_cold(items, query_vec, min_score) if server_state.get_qdrant() else []
    )
    edges = _expand_edges(items, query_vec, min_score)
    combined = hot + cold + edges
    combined.sort(key=heat_key, reverse=True)
    return combined


def _expand_edges(items, query_vec, min_score):
    """One-hop neighbours from the edge table, excluding episode (already walked)."""
    if query_vec is None:
        return []
    seeds = [
        item for item in items[:_EDGE_EXPAND_SEEDS] if item.layer in ("hot", "cold")
    ]
    if not seeds:
        return []
    known = {item.id for item in items}
    pairs = []
    for seed in seeds:
        try:
            edges = memory_graph.neighbors(
                seed.id, types=_EDGE_EXPAND_TYPES, limit=_EDGE_EXPAND_PER_SEED
            )
        except Exception as e:  # noqa: BLE001 - graph is optional context
            logger.warning(f"Edge expansion failed: {e}")
            continue
        for edge in edges:
            other = edge["to_id"] if edge["from_id"] == seed.id else edge["from_id"]
            if other not in known:
                pairs.append((seed, other, edge["type"]))
    if not pairs:
        return []
    nodes = memory_store.get_nodes(list({other for _, other, _ in pairs}))
    lookup = {
        **_hot_points([nid for nid, node in nodes.items() if node["layer"] == "hot"]),
        **_cold_points([node for node in nodes.values() if node["layer"] == "cold"]),
    }
    out: list[RetrievedItem] = []
    for seed, other, edge_type in pairs:
        if other in known:
            continue
        entry = lookup.get(other)
        if not entry:
            continue
        score = cosine(query_vec, entry["vector"])
        if score < min_score:
            continue
        known.add(other)
        node = nodes.get(other) or {}
        out.append(
            RetrievedItem(
                id=other,
                kind="memory",
                score=float(min(seed.score * EPISODE_EXPAND_DECAY, score)),
                score_kind="dense",
                content=entry["content"],
                layer=node.get("layer", ""),
                match_kind="expanded",
                extra={
                    "heat_score": entry["heat_score"],
                    "episode_id": entry["episode_id"],
                    "edge": edge_type,
                    "expanded": True,
                },
            )
        )
    return out


def _hot_points(node_ids: list[str]) -> dict[str, dict]:
    hot = server_state.get_hot_memory()
    if not hot or not node_ids:
        return {}
    try:
        points = hot.vector_store.client.retrieve(
            collection_name=hot.vector_store.collection_name,
            ids=node_ids,
            with_payload=True,
            with_vectors=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Hot edge lookup failed: {e}")
        return {}
    out: dict[str, dict] = {}
    for point in points:
        vector = dense_vector(getattr(point, "vector", None))
        if not vector:
            continue
        payload = point.payload or {}
        out[str(point.id)] = {
            "vector": vector,
            "content": str(payload.get("data") or ""),
            "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
            "episode_id": payload.get(EPISODE_FIELD, ""),
        }
    return out


def _cold_points(nodes: list[dict]) -> dict[str, dict]:
    qdrant = server_state.get_qdrant()
    if not qdrant or not nodes:
        return {}
    point_to_node = {}
    for node in nodes:
        point_id = str((node.get("metadata") or {}).get("point_id") or "")
        if point_id:
            point_to_node[point_id] = node["node_id"]
    if not point_to_node:
        return {}
    try:
        points = qdrant.retrieve(
            collection_name=COLD_COLLECTION,
            ids=list(point_to_node),
            with_payload=True,
            with_vectors=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Cold edge lookup failed: {e}")
        return {}
    out: dict[str, dict] = {}
    for point in points:
        node_id = point_to_node.get(str(point.id))
        if not node_id:
            continue
        vector = dense_vector(getattr(point, "vector", None))
        if not vector:
            continue
        payload = point.payload or {}
        out[node_id] = {
            "vector": vector,
            "content": str(payload.get("content") or ""),
            "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
            "episode_id": payload.get(EPISODE_FIELD, ""),
        }
    return out


def _expand_hot(
    items: list[RetrievedItem],
    query_vec: np.ndarray | None,
    min_score: float | None = None,
) -> list[RetrievedItem]:
    if min_score is None:
        min_score = floors_for(server_embed.EMBEDDER_SPEC.tag)["expanded"]
    seeds = [i for i in items if i.layer == "hot" and i.extra.get("episode_id")][
        :EPISODE_EXPAND_TOP_N
    ]
    hot_memory = server_state.get_hot_memory()
    if not seeds or hot_memory is None:
        return []
    known = {i.id for i in items}
    out: list[RetrievedItem] = []
    for seed in seeds:
        episode_id = str(seed.extra.get("episode_id") or "")
        try:
            points, _ = hot_memory.vector_store.client.scroll(
                collection_name=hot_memory.vector_store.collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key=EPISODE_FIELD, match=MatchValue(value=episode_id)
                        )
                    ]
                ),
                limit=EPISODE_EXPAND_PER_GROUP + len(known),
                with_payload=True,
                with_vectors=query_vec is not None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Episode expansion failed: {e}")
            continue
        candidates = []
        for point in points:
            payload = point.payload or {}
            if point.id in known or payload.get("archived"):
                continue
            if memory_status(payload) != STATUS_ACTIVE:
                continue
            vector = dense_vector(getattr(point, "vector", None))
            score = (
                cosine(query_vec, vector)
                if query_vec is not None and vector
                else float(seed.score)
            )
            if score < min_score:
                continue
            candidates.append((score, point, payload))
        candidates.sort(key=lambda c: -c[0])
        for candidate_score, point, payload in candidates[:EPISODE_EXPAND_PER_GROUP]:
            known.add(point.id)
            out.append(
                RetrievedItem(
                    id=str(point.id),
                    kind="memory",
                    score=float(
                        min(seed.score * EPISODE_EXPAND_DECAY, candidate_score)
                    ),
                    score_kind="dense",
                    content=payload.get("data", ""),
                    layer="hot",
                    match_kind="expanded",
                    extra={
                        "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
                        "episode_id": episode_id,
                        "expanded": True,
                    },
                )
            )
    return out


def _expand_cold(
    items: list[RetrievedItem],
    query_vec: np.ndarray | None,
    min_score: float | None = None,
) -> list[RetrievedItem]:
    if min_score is None:
        min_score = floors_for(server_embed.EMBEDDER_SPEC.tag)["expanded"]
    seeds = [i for i in items if i.layer == "cold" and i.extra.get("episode_id")][
        :EPISODE_EXPAND_TOP_N
    ]
    client = server_state.get_qdrant()
    if not seeds or client is None:
        return []
    known = {i.id for i in items}
    out: list[RetrievedItem] = []
    for seed in seeds:
        episode_id = str(seed.extra.get("episode_id") or "")
        try:
            points, _ = client.scroll(
                collection_name=COLD_COLLECTION,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key=EPISODE_FIELD, match=MatchValue(value=episode_id)
                        )
                    ]
                ),
                limit=EPISODE_EXPAND_PER_GROUP + len(known),
                with_payload=True,
                with_vectors=query_vec is not None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Cold episode expansion failed: {e}")
            continue
        candidates = []
        for point in points:
            payload = point.payload or {}
            if point.id in known or payload.get("archived"):
                continue
            if payload.get(COLD_STALE_FIELD):
                continue
            vector = dense_vector(getattr(point, "vector", None))
            score = (
                cosine(query_vec, vector)
                if query_vec is not None and vector
                else float(seed.score)
            )
            if score < min_score:
                continue
            candidates.append((score, point, payload))
        candidates.sort(key=lambda c: -c[0])
        for candidate_score, point, payload in candidates[:EPISODE_EXPAND_PER_GROUP]:
            known.add(point.id)
            out.append(
                RetrievedItem(
                    id=str(point.id),
                    kind="memory",
                    score=float(
                        min(seed.score * EPISODE_EXPAND_DECAY, candidate_score)
                    ),
                    score_kind="dense",
                    content=payload.get("content", ""),
                    layer="cold",
                    match_kind="expanded",
                    extra={
                        "source_file": payload.get("source_file", ""),
                        "created_at": payload.get("created_at", ""),
                        "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
                        "episode_id": episode_id,
                        "expanded": True,
                    },
                )
            )
    return out
