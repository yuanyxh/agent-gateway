"""Daily and weekly consolidation over the shared memory graph.

Daily: relevance scoring, relation sampling, clustering, compression.
Weekly: contradiction detection and controlled forgetting.

Each stage is a plain function so a test can run the job with a fixed `now`;
the scheduler below only decides when to call them. Sampling sizes are work
budgets, not ranking weights; the only similarity cut used here is the
embedder's calibrated expanded floor.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta

from agent_gateway.core import prompt_loader
from agent_gateway.core.config import (
    memory_max_summaries,
    memory_max_summaries_per_episode,
    memory_neuron_build_enabled,
    memory_neuron_enabled,
    memory_neuron_llm_pairs,
    memory_neuron_review_concurrency,
    memory_neuron_maintenance_enabled,
    memory_protect_importance,
)
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.memory_lifecycle import is_public
from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import (
    graph,
    lifecycle,
    network_build,
    retention,
    strength,
    store,
)
from agent_gateway.services.memory.group_governance import govern_daily
from agent_gateway.services.retrieval.gate.thresholds import floors_for

logger = setup_logging("server")

#: Work budgets for one run.
RELATION_PAIR_SAMPLE = 200
#: Contradiction judging is one LLM call per pair, so the weekly pass stops after
#: this many judged pairs even when more sit inside the band.
CONTRADICTION_JUDGE_BUDGET = 20
LOAD_BATCH = 200
NODE_SCAN_LIMIT = 5000
CLUSTER_SIZE_CAP = 5
COMPRESS_MIN_MEMBERS = 2

CONSOLIDATION_TAG = "consolidated"
GENERATED_BY = "consolidation"
SUMMARY_LEVEL_FIELD = "level"
#: Depth ceiling so repeated folding cannot erase the details below it.
SUMMARY_LEVEL_MAX = 3


def _dense(raw):
    if isinstance(raw, dict):
        raw = raw.get("") or raw.get("dense")
    return raw or None


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return float(dot / (na * nb)) if na > 1e-10 and nb > 1e-10 else 0.0


def _similarity_floor() -> float:
    from agent_gateway.services.mcp.server_embed import EMBEDDER_SPEC

    return floors_for(EMBEDDER_SPEC.tag)["expanded"]


def _hot_rows(node_ids: list[str]) -> dict[str, dict]:
    """Vectors and payloads for hot nodes, loaded in batches."""
    from agent_gateway.services.mcp.server_state import get_hot_memory

    hot = get_hot_memory()
    if hot is None or not node_ids:
        return {}
    out: dict[str, dict] = {}
    for start in range(0, len(node_ids), LOAD_BATCH):
        batch = node_ids[start : start + LOAD_BATCH]
        try:
            points = hot.vector_store.client.retrieve(
                collection_name=hot.vector_store.collection_name,
                ids=batch,
                with_payload=True,
                with_vectors=True,
            )
        except Exception as e:  # noqa: BLE001 - one batch must not kill the run
            logger.warning(f"[consolidate] vector load failed: {e}")
            continue
        for point in points:
            vector = _dense(getattr(point, "vector", None))
            if not vector:
                continue
            out[str(point.id)] = {"vector": vector, "payload": point.payload or {}}
    return out


def active_hot(db_path: str | None = None) -> list[dict]:
    return store.list_nodes(
        layer="hot", status="active", limit=NODE_SCAN_LIMIT, db_path=db_path
    )


def score_relevance(db_path: str | None = None, now: datetime | None = None) -> int:
    """Store the quality score on each node so later passes can read it cheaply."""
    now = now or datetime.now()
    nodes = active_hot(db_path)
    rows = _hot_rows([node["node_id"] for node in nodes])
    updates = []
    for node in nodes:
        entry = rows.get(node["node_id"])
        if entry is None:
            continue
        score = retention.quality_score(entry["payload"], now)
        updates.append(
            (node["node_id"], {"relevance": score, "scored_at": now.isoformat()})
        )
    if not updates:
        return 0
    store.batch_update_metadata(updates, db_path=db_path)
    return len(updates)


def _components(pairs: list[tuple], cap: int) -> list[list[str]]:
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for left, right, _ in pairs:
        parent[find(left)] = find(right)
    groups: dict[str, list[str]] = {}
    for item in parent:
        groups.setdefault(find(item), []).append(item)
    return [
        members
        for members in groups.values()
        if COMPRESS_MIN_MEMBERS <= len(members) <= cap
    ]


def sample_relations(
    *,
    db_path: str | None = None,
    now: datetime | None = None,
    sample_size: int = RELATION_PAIR_SAMPLE,
) -> dict:
    """Random pair sampling; the seed is the day so a rerun walks the same pairs."""
    now = now or datetime.now()
    # Shared summaries are public, so scoped memories never enter the sample.
    nodes = [node for node in active_hot(db_path) if is_public(node.get("metadata"))]
    rows = _hot_rows([node["node_id"] for node in nodes])
    # Consolidation output is excluded: the candidate set must stay the same across
    # reruns, or each new summary reshuffles the sample and invents new clusters.
    node_ids = [
        node_id
        for node_id, entry in rows.items()
        if not entry["payload"].get("generated_by")
    ]
    if len(node_ids) < 2:
        return {"pairs": [], "edges": 0, "clusters": [], "rows": rows}
    floor = _similarity_floor()
    rng = random.Random(now.date().toordinal())
    budget = min(sample_size, len(node_ids) * (len(node_ids) - 1) // 2)
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str, float]] = []
    for _ in range(max(budget * 5, budget)):
        if len(pairs) >= budget:
            break
        left, right = rng.sample(node_ids, 2)
        key = (left, right) if left < right else (right, left)
        if key in seen:
            continue
        seen.add(key)
        score = _cosine(rows[left]["vector"], rows[right]["vector"])
        if score >= floor:
            pairs.append((key[0], key[1], score))
    edges = [
        (left, right, "relates", score, {"method": "random_pair"})
        for left, right, score in pairs
    ]
    written = graph.add_edges(edges, db_path=db_path)
    return {
        "pairs": pairs,
        "edges": written,
        "clusters": _components(pairs, CLUSTER_SIZE_CAP),
        "rows": rows,
    }


def _cluster_facts(members: list[str], rows: dict[str, dict]) -> tuple[list[str], bool]:
    facts, generated = [], False
    for node_id in members:
        payload = rows[node_id]["payload"]
        if payload.get("generated_by") == GENERATED_BY:
            generated = True
        text = str(payload.get("data") or payload.get("memory") or "").strip()
        if text:
            facts.append(text)
    return facts, generated


def _uncovered_members(members: list[str], db_path: str | None) -> list[str]:
    """Members not yet linked to a consolidation summary.

    A cluster that partly overlaps an earlier summary keeps only its new
    members, so different member combinations cannot re-summarize the same facts.
    """
    uncovered = []
    for member in members:
        covered = False
        for edge in graph.neighbors(
            member, types=("derived_from",), direction="up", db_path=db_path
        ):
            parent = store.get_node(edge["from_id"], db_path=db_path)
            if (
                parent
                and (parent.get("metadata") or {}).get("generated_by") == GENERATED_BY
            ):
                covered = True
                break
        if not covered:
            uncovered.append(member)
    return uncovered


async def compress_clusters(
    clusters: list[list[str]],
    rows: dict[str, dict],
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> int:
    """Summarize each cluster into one memory linked back to its members."""
    if not clusters or not llm_client.is_configured():
        return 0
    from agent_gateway.services.mcp.server_memory import _write_memory
    from agent_gateway.services.mcp.server_constants import DEFAULT_TIMEOUT

    now = now or datetime.now()
    written = 0
    skipped = {"generated": 0, "too_few": 0, "already": 0, "empty": 0, "write": 0}
    for members in clusters:
        facts, generated = _cluster_facts(members, rows)
        if generated:
            skipped["generated"] += 1
            continue
        members = _uncovered_members(members, db_path)
        facts, _ = _cluster_facts(members, rows)
        if len(facts) < COMPRESS_MIN_MEMBERS:
            skipped["already"] += 1
            continue
        prompt = prompt_loader.prompt(
            "consolidate", facts_text="\n".join(f"- {fact}" for fact in facts)
        )
        started = time.monotonic()
        raw = await llm_client.achat(
            prompt,
            temperature=0.1,
            timeout=DEFAULT_TIMEOUT,
            disable_thinking=True,
            label="Consolidation summarize failed",
        )
        elapsed = time.monotonic() - started
        parsed = llm_client.parse_json(raw) or {}
        summary = str(parsed.get("summary") or "").strip()
        if not summary:
            logger.warning(
                f"[consolidate] cluster of {len(members)} returned no summary after {elapsed:.1f}s"
            )
            skipped["empty"] += 1
            continue
        result = _write_memory(
            {
                "content": summary,
                "context": str(parsed.get("context") or ""),
                "generated_by": GENERATED_BY,
                "tags": [CONSOLIDATION_TAG],
            },
            now.isoformat(),
            db_path=db_path,
        )
        memory_id = result.get("memory_id")
        if not memory_id:
            logger.warning("[consolidate] cluster write returned no memory_id")
            skipped["write"] += 1
            continue
        graph.add_edges(
            [(memory_id, member, "derived_from") for member in members],
            db_path=db_path,
        )
        written += 1
    logger.info(
        f"[consolidate] compress: clusters={len(clusters)} written={written} skipped={skipped}"
    )
    return written


def _summary_nodes(db_path: str | None) -> list[dict]:
    nodes = store.list_nodes(
        layer="hot", status="active", limit=NODE_SCAN_LIMIT, db_path=db_path
    )
    return [
        node
        for node in nodes
        if (node.get("metadata") or {}).get("generated_by") == GENERATED_BY
    ]


def _summary_level(node: dict) -> int:
    try:
        return int((node.get("metadata") or {}).get(SUMMARY_LEVEL_FIELD) or 0)
    except (TypeError, ValueError):
        return 0


def _over_budget(items: list[dict], cap: int | None) -> list[dict]:
    """Items to fold. Cap 1 folds everything into one; otherwise the newest cap-1 stay."""
    if not cap or len(items) <= cap:
        return []
    return list(items) if cap == 1 else items[cap - 1 :]


def _merge_groups(summaries: list[dict]) -> list[list[dict]]:
    """One fold group per over-budget bucket; the global budget folds on its own.

    Nodes at `SUMMARY_LEVEL_MAX` still occupy a budget slot but are never folded
    again: the ceiling protects the detail underneath, so a bucket that is over
    budget only because of ceiling nodes stays over budget. Protected nodes are
    dropped by the caller and leave the same kind of gap. Summaries written by
    this run are counted from the next one, so a wide overflow can take a few
    days to settle.
    """

    def foldable(victims: list[dict]) -> list[dict]:
        return [n for n in victims if _summary_level(n) < SUMMARY_LEVEL_MAX]

    groups: list[list[dict]] = []
    taken: set[str] = set()
    per_episode = memory_max_summaries_per_episode()
    if per_episode:
        buckets: dict[str, list[dict]] = {}
        for node in summaries:
            episode = str((node.get("metadata") or {}).get("episode_id") or "")
            buckets.setdefault(episode, []).append(node)
        for items in buckets.values():
            victims = foldable(_over_budget(items, per_episode))
            if len(victims) < 2:
                continue
            groups.append(victims)
            taken.update(node["node_id"] for node in victims)
    rest = [node for node in summaries if node["node_id"] not in taken]
    victims = foldable(_over_budget(rest, memory_max_summaries()))
    if len(victims) >= 2:
        groups.append(victims)
    return groups


def _node_importance(node: dict) -> int:
    """Default 5 matches the write path, so a fold never lowers importance."""
    try:
        return int((node.get("metadata") or {}).get("importance") or 5)
    except (TypeError, ValueError):
        return 5


async def merge_summaries(
    *, db_path: str | None = None, now: datetime | None = None
) -> int:
    """Fold over-budget summaries into higher-level ones, one per bucket.

    Disabled unless a budget is configured. Folded summaries are archived, never
    deleted, and stay reachable through derived_from edges.
    """
    if not (memory_max_summaries() or memory_max_summaries_per_episode()):
        return 0
    if not llm_client.is_configured():
        return 0
    protect_at = memory_protect_importance()
    merged = 0
    for group in _merge_groups(_summary_nodes(db_path)):
        kept = [node for node in group if not _is_protected(node, protect_at)]
        merged += await _merge_group(kept, db_path=db_path, now=now)
    return merged


async def _merge_group(
    nodes: list[dict], *, db_path: str | None, now: datetime | None
) -> int:
    from agent_gateway.services.mcp import server_memory
    from agent_gateway.services.mcp.server_constants import DEFAULT_TIMEOUT

    if len(nodes) < 2:
        return 0
    rows = _hot_rows([node["node_id"] for node in nodes])
    covered = [
        (node, str(rows[node["node_id"]]["payload"].get("data") or "").strip())
        for node in nodes
        if node["node_id"] in rows
    ]
    covered = [(node, text) for node, text in covered if text]
    if len(covered) < 2:
        logger.info(
            f"[consolidate] merge skipped: {len(covered)} of {len(nodes)} summaries readable"
        )
        return 0
    level = min(max(_summary_level(node) for node, _ in covered) + 1, SUMMARY_LEVEL_MAX)
    episodes = {
        str((node.get("metadata") or {}).get("episode_id") or "").strip()
        for node, _ in covered
    }
    prompt = prompt_loader.prompt(
        "consolidate", facts_text="\n".join(f"- {text}" for _, text in covered)
    )
    raw = await llm_client.achat(
        prompt,
        temperature=0.1,
        timeout=DEFAULT_TIMEOUT,
        disable_thinking=True,
        label="Summary merge failed",
    )
    parsed = llm_client.parse_json(raw) or {}
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        logger.warning(
            f"[consolidate] merge returned no summary for {len(covered)} inputs"
        )
        return 0
    result = server_memory._write_memory(
        {
            "content": summary,
            "context": str(parsed.get("context") or ""),
            "generated_by": GENERATED_BY,
            "tags": [CONSOLIDATION_TAG],
            "importance": max(_node_importance(node) for node, _ in covered),
            "episode_id": episodes.pop() if len(episodes) == 1 else "",
            SUMMARY_LEVEL_FIELD: level,
        },
        (now or datetime.now()).isoformat(),
        db_path=db_path,
    )
    memory_id = result.get("memory_id")
    if not memory_id:
        logger.warning("[consolidate] merge write returned no memory_id")
        return 0
    if result.get("deduped"):
        # The text collapsed onto an existing node, which stays active and keeps its
        # body; only the siblings are retired.
        logger.info(f"[consolidate] merge collapsed onto {memory_id}")
    victims = [node["node_id"] for node, _ in covered if node["node_id"] != memory_id]
    try:
        graph.add_edges(
            [(memory_id, node_id, "derived_from") for node_id in victims],
            db_path=db_path,
        )
    except Exception as e:  # noqa: BLE001 - the summary is already written
        logger.warning(f"[consolidate] derived_from edges failed: {e}")
    try:
        archived = server_memory.set_memory_status(victims, "archived", db_path=db_path)
    except Exception as e:  # noqa: BLE001 - keep the next run recoverable
        logger.warning(f"[consolidate] merge archive failed for {len(victims)}: {e}")
        return 0
    if archived.get("failed"):
        logger.warning(
            f"[consolidate] merge archived {archived.get('updated')} of {len(victims)}"
        )
    logger.info(
        f"[consolidate] merged {len(victims)} summaries into {memory_id} level={level}"
    )
    return 1


async def run_daily(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
    sample_size: int = RELATION_PAIR_SAMPLE,
    governance_since: str = "",
) -> dict:
    now = now or datetime.now()
    scored = await asyncio.to_thread(score_relevance, db_path=db_path, now=now)
    governed = await govern_daily(
        db_path=db_path,
        since=governance_since or (now - timedelta(days=1)).isoformat(),
    )
    migrated = {}
    repaired = {}
    if memory_neuron_enabled():
        migrated = await asyncio.to_thread(
            network_build.migrate_network, db_path=db_path, apply=True, now=now
        )
        if memory_neuron_maintenance_enabled():
            migrated["decay"] = await asyncio.to_thread(
                strength.apply_decay, now=now, db_path=db_path
            )
        if memory_neuron_build_enabled():
            built = await network_build.build_synapse_candidates(
                db_path=db_path,
                since=governance_since,
                seed_neurons=governed.get("touched") or None,
                llm_limit=memory_neuron_llm_pairs(),
                concurrency=memory_neuron_review_concurrency(),
            )
            repaired = {
                "pairs": len(built["pairs"]),
                "written": await asyncio.to_thread(
                    network_build.write_synapses,
                    built["decisions"],
                    db_path=db_path,
                ),
                "calls": built["calls"],
                "truncated": built["truncated"],
                "llm_skipped": built.get("llm_skipped", False),
                "vector_unavailable": built["vector_unavailable"],
            }
    sampled = await asyncio.to_thread(
        sample_relations, db_path=db_path, now=now, sample_size=sample_size
    )
    compressed = await compress_clusters(
        sampled["clusters"], sampled["rows"], now=now, db_path=db_path
    )
    merged = await merge_summaries(db_path=db_path, now=now)
    return {
        "scored": scored,
        "pairs": len(sampled["pairs"]),
        "edges": sampled["edges"],
        "clusters": len(sampled["clusters"]),
        "compressed": compressed,
        "merged": merged,
        "governance": governed,
        "network": migrated,
        "synapses": repaired,
    }


async def detect_contradictions(
    *,
    db_path: str | None = None,
    now: datetime | None = None,
    sample_size: int = CONTRADICTION_JUDGE_BUDGET,
    dry_run: bool = False,
) -> dict:
    """Band pre-screen, then the existing LLM judge.

    Candidates come from per-node neighbour lookups (O(n * k)); the exact matrix
    stays as a fallback for stores that cannot answer them.
    """
    from agent_gateway.services.mcp import server_memory
    from agent_gateway.services.mcp.server_constants import (
        CONTRADICTION_SIMILARITY_MAX,
        CONTRADICTION_SIMILARITY_MIN,
    )

    if not llm_client.is_configured():
        logger.info(
            "[consolidate] weekly contradiction pass skipped: llm not configured"
        )
        return {"pairs": 0, "disputes": 0}
    nodes = active_hot(db_path)
    rows = _hot_rows([node["node_id"] for node in nodes])
    protect_at = memory_protect_importance()
    node_ids = [
        node_id
        for node_id, entry in rows.items()
        if not entry["payload"].get("generated_by")
        and not _is_protected(entry["payload"], protect_at)
    ]
    candidates = await _candidate_pairs(
        node_ids,
        rows,
        CONTRADICTION_SIMILARITY_MIN,
        CONTRADICTION_SIMILARITY_MAX,
        sample_size,
    )
    confirmed, skipped, review = 0, 0, []
    for _, left, right in candidates:
        if graph.has_edge(left, right, "disputes", db_path=db_path):
            skipped += 1
            continue
        result = await server_memory._check_contradiction(
            str(rows[left]["payload"].get("data") or ""),
            [
                {
                    "id": right,
                    "content": str(rows[right]["payload"].get("data") or ""),
                }
            ],
        )
        if not result or result.get("relation") != "contradiction":
            continue
        if dry_run:
            review.append(
                {
                    "left": left,
                    "right": right,
                    "reason": str(result.get("reason") or ""),
                }
            )
            continue
        graph.add_edge(
            *sorted((left, right)),
            "disputes",
            metadata={"reason": str(result.get("reason") or "")},
            db_path=db_path,
        )
        server_memory._supersede_older(left, right)
        confirmed += 1
    logger.info(
        f"[consolidate] contradictions: nodes={len(node_ids)} "
        f"candidates={len(candidates)} confirmed={confirmed} skipped={skipped} "
        f"dry_run={dry_run}"
    )
    payload = {"pairs": len(candidates), "disputes": confirmed}
    if dry_run:
        payload["dry_run"] = True
        payload["review"] = review
    return payload


def _is_protected(payload: dict, protect_at: int | None) -> bool:
    meta = payload.get("metadata") or {}
    if payload.get("protected") or meta.get("protected"):
        return True
    if protect_at is None:
        return False
    try:
        importance = int(payload.get("importance") or meta.get("importance") or 0)
    except (TypeError, ValueError):
        return False
    return importance >= protect_at


async def _candidate_pairs(node_ids, rows, low, high, limit):
    """Neighbour lookups when the vector store can do them, matrix as fallback."""
    nearby = await asyncio.to_thread(
        _nearby_band_pairs, node_ids, rows, low, high, limit
    )
    if nearby is not None:
        return nearby
    return await asyncio.to_thread(_band_pairs, node_ids, rows, low, high, limit)


def _nearby_band_pairs(node_ids, rows, low, high, limit, top_k: int = 10):
    """Per-node top-k neighbours inside the band; O(n * k) instead of O(n^2)."""
    from qdrant_client import models

    from agent_gateway.services.mcp.server_state import get_hot_memory

    hot = get_hot_memory()
    if hot is None or len(node_ids) < 2:
        return None
    allowed = set(node_ids)
    try:
        store = hot.vector_store
        pairs: dict[tuple[str, str], float] = {}
        for start in range(0, len(node_ids), 100):
            batch = node_ids[start : start + 100]
            hits = store.client.query_batch_points(
                collection_name=store.collection_name,
                requests=[
                    models.QueryRequest(
                        query=rows[node]["vector"], limit=top_k, with_payload=False
                    )
                    for node in batch
                ],
            )
            for source, result in zip(batch, hits):
                for point in getattr(result, "points", result):
                    target = str(point.id)
                    if target == source or target not in allowed:
                        continue
                    score = float(point.score)
                    if not low <= score <= high:
                        continue
                    key = (source, target) if source < target else (target, source)
                    pairs[key] = max(pairs.get(key, 0.0), score)
        ordered = sorted(
            ((score, left, right) for (left, right), score in pairs.items()),
            key=lambda item: -item[0],
        )
        return ordered[:limit]
    except Exception as e:  # noqa: BLE001 - fall back to the exact matrix
        logger.warning(f"[consolidate] neighbour lookup unavailable: {e}")
        return None


def _band_pairs(node_ids: list[str], rows: dict[str, dict], low, high, limit):
    """Pairs whose similarity sits inside the contradiction band, closest first."""
    if len(node_ids) < 2:
        return []
    import numpy as np

    matrix = np.asarray([rows[node]["vector"] for node in node_ids], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    normed = matrix / np.clip(norms, 1e-10, None)
    sims = normed @ normed.T
    pairs = []
    for i in range(len(node_ids)):
        for j in range(i + 1, len(node_ids)):
            score = float(sims[i, j])
            if low <= score <= high:
                pairs.append((score, node_ids[i], node_ids[j]))
    pairs.sort(key=lambda item: -item[0])
    return pairs[:limit]


def forget_expired(db_path: str | None = None, now: datetime | None = None) -> int:
    """Run the archive pass; it archives by quality window and never deletes."""
    from agent_gateway.services.mcp.server_archive import run_archive_sync_impl
    from agent_gateway.services.mcp.server_state import get_hot_memory, get_qdrant

    try:
        archived_hot, _, _ = run_archive_sync_impl(get_hot_memory(), get_qdrant(), now)
        return archived_hot
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[consolidate] forgetting failed: {e}")
        return 0


async def run_weekly(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
    sample_size: int = CONTRADICTION_JUDGE_BUDGET,
    dry_run: bool = False,
) -> dict:
    now = now or datetime.now()
    disputes = await detect_contradictions(
        db_path=db_path, now=now, sample_size=sample_size, dry_run=dry_run
    )
    archived = (
        0
        if dry_run
        else await asyncio.to_thread(forget_expired, db_path=db_path, now=now)
    )
    network_result = {}
    if memory_neuron_enabled() and memory_neuron_maintenance_enabled():
        tiers = await asyncio.to_thread(
            lifecycle.classify_tiers, now=now, db_path=db_path, dry_run=dry_run
        )
        unbound = await asyncio.to_thread(
            strength.unbind_weak, now=now, db_path=db_path, dry_run=dry_run
        )
        purged = await asyncio.to_thread(
            lifecycle.purge_expired, now=now, db_path=db_path, dry_run=dry_run
        )
        capped = await asyncio.to_thread(
            network_build.enforce_degree_cap, db_path=db_path, dry_run=dry_run
        )
        network_result = {
            "tiers": tiers,
            "unbound": unbound,
            "purged": purged,
            "capped": capped,
        }
    if not dry_run:
        await asyncio.to_thread(store.vacuum, db_path=db_path)
    return {
        "disputes": disputes,
        "archived": archived,
        "network": network_result,
    }


def _state_path():
    return store.graph_db_path().parent / "consolidation_state.json"


def read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"[consolidate] state write failed: {e}")


async def consolidation_scheduler(
    ensure_hot_memory,
    *,
    daily_seconds: int = 24 * 3600,
    weekly_seconds: int = 7 * 24 * 3600,
    interval_seconds: int = 3600,
    start_delay: int = 900,
) -> None:
    """Wall-clock driver: daily work every day, weekly work every week."""
    logger.info(
        f"[consolidate] scheduler armed: start_delay={start_delay}s "
        f"daily={daily_seconds}s weekly={weekly_seconds}s interval={interval_seconds}s"
    )
    await asyncio.sleep(start_delay)
    while True:
        try:
            await ensure_hot_memory()
            state = read_state()
            now_ts = time.time()
            if now_ts - float(state.get("daily", 0) or 0) >= daily_seconds:
                started = time.monotonic()
                previous = float(state.get("daily", 0) or 0)
                since = (
                    datetime.fromtimestamp(previous).astimezone().isoformat()
                    if previous
                    else ""
                )
                result = await run_daily(governance_since=since)
                state["daily"] = now_ts
                write_state(state)
                logger.info(
                    f"[consolidate] daily done in {time.monotonic() - started:.1f}s: {result}"
                )
            if now_ts - float(state.get("weekly", 0) or 0) >= weekly_seconds:
                started = time.monotonic()
                result = await run_weekly()
                state["weekly"] = now_ts
                write_state(state)
                logger.info(
                    f"[consolidate] weekly done in {time.monotonic() - started:.1f}s: {result}"
                )
        except Exception as e:  # noqa: BLE001 - the loop must survive one bad run
            logger.warning(f"[consolidate] run failed: {e}", exc_info=True)
        await asyncio.sleep(interval_seconds)
