"""Build and reconcile neurons, membership and synapses from the memory graph."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import defaultdict
from datetime import datetime

import numpy as np
from qdrant_client import models

from agent_gateway.core import prompt_loader
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import network, retention, store

logger = setup_logging("server")

PAGE_SIZE = 2000
PAIR_FLOOR = 0.56
PER_ANCHOR = 5
PAIR_LIMIT = 2000
LLM_PAIRS = 200
LLM_BATCH = 10
LLM_MAX_CALLS = 20
MIN_SYNAPSE_WEIGHT = 0.62
MAX_SYNAPSE_DEGREE = 20
RELATIONS = ("same_task", "follow_up", "cause", "reference", "none")
_ALL_MEMBERS = 10_000_000
_ALL_SYNAPSES = 10_000_000


def _active_nodes(db_path: str | None = None) -> list[dict]:
    nodes: list[dict] = []
    offset = 0
    while True:
        page = store.list_nodes(
            layer="hot",
            status="active",
            limit=PAGE_SIZE,
            offset=offset,
            db_path=db_path,
        )
        nodes.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return nodes


def _initial_strength(node: dict, now: datetime) -> float:
    meta = dict(node.get("metadata") or {})
    meta.setdefault("created_at", node.get("created_at"))
    importance = int(meta.get("importance") or 5)
    quality = retention.quality_score(meta, now)
    return min(1.0, 0.3 + 0.5 * importance / 10 + 0.2 * quality)


def _last_active(node: dict) -> str:
    meta = node.get("metadata") or {}
    return str(
        meta.get("last_accessed_at")
        or meta.get("created_at")
        or node.get("created_at")
        or ""
    )


def migrate_network(
    db_path: str | None = None,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    nodes = _active_nodes(db_path)
    groups: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        episode = str((node.get("metadata") or {}).get("episode_id") or "").strip()
        neuron_id = episode or f"solo:{node['node_id']}"
        groups[neuron_id].append(node)
    if apply:
        current_members = network.membership_map(db_path)
        current_activation = network.activation_ids(db_path)
        for neuron_id, members in groups.items():
            episode_id = "" if neuron_id.startswith("solo:") else neuron_id
            network.ensure_neuron(
                {
                    "neuron_id": neuron_id,
                    "episode_id": episode_id,
                    "title": members[0].get("title") or "",
                    "member_count": len(members),
                    "created_at": min(
                        (
                            str(item.get("created_at") or now.isoformat())
                            for item in members
                        ),
                        default=now.isoformat(),
                    ),
                },
                db_path=db_path,
            )
            for node in members:
                if current_members.get(node["node_id"]) != neuron_id:
                    network.remove_member(node["node_id"], db_path=db_path)
                    network.upsert_member(
                        neuron_id,
                        node["node_id"],
                        layer=str(node.get("layer") or "hot"),
                        joined_at=str(node.get("created_at") or now.isoformat()),
                        db_path=db_path,
                    )
                if node["node_id"] not in current_activation:
                    network.upsert_activation(
                        node["node_id"],
                        {
                            "strength": _initial_strength(node, now),
                            "tier": "active",
                            "last_activated_at": _last_active(node),
                        },
                        db_path=db_path,
                    )
        network.refresh_neuron_counts(db_path=db_path)
        network.set_meta("migrated_at", now.isoformat(), db_path=db_path)
    return {
        "nodes": len(nodes),
        "neurons": len(groups),
        "members": len(nodes),
        "solo": sum(1 for key in groups if key.startswith("solo:")),
        "applied": apply,
    }


def reconcile_network(db_path: str | None = None) -> dict:
    with store.connect(db_path) as connection:
        ghost_members = connection.execute(
            "DELETE FROM neuron_members WHERE memory_id NOT IN "
            "(SELECT node_id FROM memory_nodes) OR neuron_id NOT IN "
            "(SELECT neuron_id FROM neurons)"
        ).rowcount
        orphan_activation = connection.execute(
            "DELETE FROM memory_activation WHERE node_id NOT IN "
            "(SELECT node_id FROM memory_nodes)"
        ).rowcount
        empty_sql = (
            "SELECT neuron_id FROM neurons WHERE NOT EXISTS "
            "(SELECT 1 FROM neuron_members m WHERE m.neuron_id=neurons.neuron_id)"
        )
        empty = [row[0] for row in connection.execute(empty_sql)]
        for neuron_id in empty:
            connection.execute(
                "DELETE FROM synapses WHERE from_neuron=? OR to_neuron=?",
                (neuron_id, neuron_id),
            )
        empty_neurons = connection.execute(
            "DELETE FROM neurons WHERE NOT EXISTS "
            "(SELECT 1 FROM neuron_members m WHERE m.neuron_id=neurons.neuron_id)"
        ).rowcount
        dangling = connection.execute(
            "DELETE FROM synapses WHERE from_neuron NOT IN (SELECT neuron_id FROM neurons) "
            "OR to_neuron NOT IN (SELECT neuron_id FROM neurons)"
        ).rowcount
    network.refresh_neuron_counts(db_path=db_path)
    return {
        "ghost_members": ghost_members,
        "orphan_activation": orphan_activation,
        "empty_neurons": empty_neurons,
        "dangling_synapses": dangling,
    }


def sync_episode_change(
    memory_id: str,
    old_episode: str,
    new_episode: str,
    *,
    db_path: str | None = None,
) -> bool:
    if not memory_id or not new_episode or old_episode == new_episode:
        return False
    network.ensure_neuron(
        {"neuron_id": new_episode, "episode_id": new_episode}, db_path=db_path
    )
    network.remove_member(memory_id, db_path=db_path)
    network.upsert_member(new_episode, memory_id, db_path=db_path)
    return True


def sync_episode_updates(
    updates: dict[str, dict], nodes: dict[str, dict], *, db_path: str | None = None
) -> dict:
    moved = 0
    for memory_id, patch in updates.items():
        new_episode = str(patch.get("episode_id") or "")
        old_episode = str(
            ((nodes.get(memory_id) or {}).get("metadata") or {}).get("episode_id") or ""
        )
        if sync_episode_change(memory_id, old_episode, new_episode, db_path=db_path):
            moved += 1
    reconciled = reconcile_network(db_path=db_path)
    return {"moved": moved, **reconciled}


def merge_neurons(source: str, target: str, *, db_path: str | None = None) -> dict:
    if not source or not target or source == target:
        return {"members": 0, "synapses": 0}
    network.ensure_neuron({"neuron_id": target, "episode_id": target}, db_path=db_path)
    members = network.members_of(source, limit=_ALL_MEMBERS, db_path=db_path)
    for member in members:
        network.upsert_member(
            target,
            member["node_id"],
            layer=member.get("member_layer") or member.get("layer") or "hot",
            weight=member.get("member_weight") or 1.0,
            role=member.get("member_role") or "member",
            db_path=db_path,
        )
    links = network.synapses_of(source, limit=_ALL_SYNAPSES, db_path=db_path)
    for link in links:
        other = link["other"]
        if other == target:
            continue
        network.upsert_synapse(
            {
                "from_neuron": target,
                "to_neuron": other,
                "weight": link["weight"],
                "base_weight": link.get("base_weight") or link["weight"],
                "relation": link.get("relation") or "",
            },
            db_path=db_path,
        )
    with store.connect(db_path) as connection:
        connection.execute("DELETE FROM neuron_members WHERE neuron_id=?", (source,))
        connection.execute(
            "DELETE FROM synapses WHERE from_neuron=? OR to_neuron=?",
            (source, source),
        )
        connection.execute("DELETE FROM neurons WHERE neuron_id=?", (source,))
    network.refresh_neuron_counts(db_path=db_path)
    return {"members": len(members), "synapses": len(links)}


def split_neuron(
    source: str, groups: dict[str, list[str]], *, db_path: str | None = None
) -> dict:
    total = sum(len(ids) for ids in groups.values())
    if not source or total == 0:
        return {"groups": 0, "members": 0}
    external = network.synapses_of(source, limit=_ALL_SYNAPSES, db_path=db_path)
    for neuron_id, memory_ids in groups.items():
        network.ensure_neuron(
            {"neuron_id": neuron_id, "episode_id": neuron_id}, db_path=db_path
        )
        for memory_id in memory_ids:
            network.upsert_member(neuron_id, memory_id, db_path=db_path)
    for neuron_id, memory_ids in groups.items():
        share = len(memory_ids) / total
        for link in external:
            other = link["other"]
            if other == source or other in groups:
                continue
            network.upsert_synapse(
                {
                    "from_neuron": neuron_id,
                    "to_neuron": other,
                    "weight": round(float(link["weight"]) * share, 3),
                    "base_weight": round(float(link["weight"]) * share, 3),
                    "relation": link.get("relation") or "",
                },
                db_path=db_path,
            )
    with store.connect(db_path) as connection:
        connection.execute("DELETE FROM neuron_members WHERE neuron_id=?", (source,))
        connection.execute(
            "DELETE FROM synapses WHERE from_neuron=? OR to_neuron=?",
            (source, source),
        )
        connection.execute("DELETE FROM neurons WHERE neuron_id=?", (source,))
    network.refresh_neuron_counts(db_path=db_path)
    return {"groups": len(groups), "members": total}


def _metadata(raw) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _dense(raw):
    if isinstance(raw, dict):
        raw = raw.get("") or raw.get("dense")
    return raw or None


def _try_hot():
    try:
        from agent_gateway.services.mcp.server_state import get_hot_memory

        return get_hot_memory()
    except Exception:  # noqa: BLE001 - offline builds run without the server
        return None


def _group_nodes(db_path: str | None = None) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT m.neuron_id, n.node_id, n.title, n.metadata, n.created_at, n.layer "
            "FROM neuron_members m JOIN memory_nodes n ON n.node_id=m.memory_id "
            "WHERE n.status='active' ORDER BY m.neuron_id"
        ).fetchall()
    for neuron_id, node_id, title, metadata, created_at, layer in rows:
        groups[str(neuron_id)].append(
            {
                "node_id": str(node_id),
                "title": str(title or ""),
                "metadata": _metadata(metadata),
                "created_at": str(created_at or ""),
                "layer": str(layer or "hot"),
            }
        )
    return dict(groups)


def group_centroids(
    groups: dict[str, list[dict]], hot
) -> tuple[dict[str, np.ndarray], int]:
    """Accumulate one centroid per group while streaming vectors in batches."""
    if hot is None:
        return {}, len(groups)
    node_to_neuron = {
        item["node_id"]: neuron_id
        for neuron_id, members in groups.items()
        for item in members
    }
    vector_store = hot.vector_store
    sums: dict[str, np.ndarray] = {}
    counts: dict[str, int] = defaultdict(int)
    ids = list(node_to_neuron)
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        try:
            points = vector_store.client.retrieve(
                collection_name=vector_store.collection_name,
                ids=batch,
                with_payload=False,
                with_vectors=True,
            )
        except Exception as e:  # noqa: BLE001 - a missing vector store is degraded mode
            logger.warning(f"[memory-network] vector load failed: {e}")
            break
        for point in points:
            vector = _dense(getattr(point, "vector", None))
            neuron_id = node_to_neuron.get(str(point.id))
            if not vector or neuron_id is None:
                continue
            dense = np.asarray(vector, dtype=np.float32)
            sums[neuron_id] = sums[neuron_id] + dense if neuron_id in sums else dense
            counts[neuron_id] += 1
    centroids: dict[str, np.ndarray] = {}
    for neuron_id, total in sums.items():
        center = np.asarray(total, dtype=np.float32) / counts[neuron_id]
        norm = float(np.linalg.norm(center))
        if norm > 0:
            center = center / norm
        centroids[neuron_id] = np.asarray(center, dtype=np.float32)
    return centroids, len(groups) - len(centroids)


def _as_datetime(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _sample(members: list[dict]) -> list[dict]:
    return [
        {
            "id": str(member.get("node_id") or ""),
            "text": str(member["metadata"].get("fact") or member.get("title") or ""),
        }
        for member in members
    ]


def candidate_pairs(
    groups: dict[str, list[dict]],
    centroids: dict[str, np.ndarray],
    *,
    hot,
    anchors: set[str] | None = None,
    per_anchor: int = PER_ANCHOR,
    floor: float = PAIR_FLOOR,
    limit: int = PAIR_LIMIT,
) -> dict:
    node_to_neuron = {
        item["node_id"]: neuron_id
        for neuron_id, members in groups.items()
        for item in members
    }
    anchor_ids = [
        neuron_id
        for neuron_id, members in groups.items()
        if 2 <= len(members) <= 8
        and neuron_id in centroids
        and (anchors is None or neuron_id in anchors)
    ]
    if hot is None or not anchor_ids:
        return {"pairs": [], "truncated": False}
    vector_store = hot.vector_store
    scored: dict[tuple[str, str], dict] = {}
    per_anchor_count: dict[str, int] = defaultdict(int)
    for start in range(0, len(anchor_ids), 50):
        batch = anchor_ids[start : start + 50]
        requests = [
            models.QueryRequest(
                query=centroids[neuron_id].tolist(),
                limit=per_anchor + 2,
                with_payload=False,
            )
            for neuron_id in batch
        ]
        try:
            hits = vector_store.client.query_batch_points(
                collection_name=vector_store.collection_name,
                requests=requests,
            )
        except Exception as e:  # noqa: BLE001 - candidate build must degrade
            logger.warning(f"[memory-network] centroid query failed: {e}")
            continue
        for neuron_id, result in zip(batch, hits):
            for point in getattr(result, "points", result):
                target = node_to_neuron.get(str(point.id))
                if not target or target == neuron_id:
                    continue
                score = float(point.score)
                if score < floor:
                    continue
                if per_anchor_count[neuron_id] >= per_anchor:
                    continue
                key = tuple(sorted((neuron_id, target)))
                if key not in scored:
                    scored[key] = {"left": key[0], "right": key[1], "score": score}
                    per_anchor_count[neuron_id] += 1
                else:
                    scored[key]["score"] = max(scored[key]["score"], score)
    ordered = sorted(scored.values(), key=lambda item: -item["score"])
    truncated = len(ordered) > limit
    pairs = []
    for item in ordered[:limit]:
        pair_id = f"syn:{item['left']}|{item['right']}"
        pairs.append(
            {
                "pair_id": pair_id,
                "left_group": item["left"],
                "right_group": item["right"],
                "score": round(item["score"], 3),
                "left_samples": _sample(groups.get(item["left"], [])),
                "right_samples": _sample(groups.get(item["right"], [])),
            }
        )
    return {"pairs": pairs, "truncated": truncated}


def render_pairs(pairs: list[dict]) -> str:
    payload = [
        {
            "pair_id": pair["pair_id"],
            "left": pair.get("left_samples") or [],
            "right": pair.get("right_samples") or [],
        }
        for pair in pairs
    ]
    return json.dumps(payload, ensure_ascii=False)


def _parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def merge_weight(cosine: float, strength: float) -> float:
    return round(0.5 * float(cosine) + 0.5 * float(strength), 3)


PROMPT_PAIR_CHARS = 120_000


def _pair_chars(pair: dict) -> int:
    samples = (pair.get("left_samples") or []) + (pair.get("right_samples") or [])
    return sum(len(str(sample.get("text") or "")) for sample in samples)


def _batch_pairs(
    pairs: list[dict], batch_size: int, char_budget: int
) -> list[list[dict]]:
    batches: list[list[dict]] = []
    batch: list[dict] = []
    used = 0
    for pair in pairs:
        size = _pair_chars(pair)
        if batch and (len(batch) >= batch_size or used + size > char_budget):
            batches.append(batch)
            batch, used = [], 0
        batch.append(pair)
        used += size
    if batch:
        batches.append(batch)
    return batches


async def review_pairs(
    pairs: list[dict],
    *,
    limit: int = LLM_PAIRS,
    batch_size: int = LLM_BATCH,
    max_calls: int = LLM_MAX_CALLS,
    concurrency: int = 1,
    llm=llm_client.achat,
) -> dict:
    selected = pairs[:limit]
    by_id = {pair["pair_id"]: pair for pair in selected}
    batches = _batch_pairs(selected, batch_size, PROMPT_PAIR_CHARS)[:max_calls]
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_batch(batch: list[dict]) -> tuple[list[dict], str | None]:
        async with semaphore:
            prompt = prompt_loader.prompt("neuron_link", pairs_text=render_pairs(batch))
            try:
                raw = await llm(
                    prompt,
                    temperature=0.1,
                    timeout=60,
                    disable_thinking=True,
                    retry_503=1,
                )
            except Exception as e:  # noqa: BLE001 - one failed batch must not stop review
                logger.warning(f"[memory-network] LLM review failed: {e}")
                return batch, None
            return batch, raw

    results = await asyncio.gather(*[run_batch(batch) for batch in batches])
    decisions: list[dict] = []
    parse_failed = 0
    failed_pairs = 0
    rejected = 0
    for batch, raw in results:
        parsed = _parse_json(raw)
        if parsed is None or not isinstance(parsed.get("decisions"), list):
            parse_failed += 1
            failed_pairs += len(batch)
            continue
        for decision in parsed["decisions"]:
            if not isinstance(decision, dict):
                rejected += 1
                continue
            pair_id = str(decision.get("pair_id") or "")
            pair = by_id.get(pair_id)
            relation = str(decision.get("relation") or "")
            if pair is None or relation not in RELATIONS:
                rejected += 1
                continue
            try:
                strength = float(decision.get("strength"))
            except (TypeError, ValueError):
                rejected += 1
                continue
            if not 0.0 <= strength <= 1.0:
                rejected += 1
                continue
            if relation == "none" or strength < 0.5:
                rejected += 1
                continue
            decisions.append(
                {
                    **pair,
                    "relation": relation,
                    "strength": strength,
                    "final_weight": merge_weight(pair["score"], strength),
                }
            )
    return {
        "decisions": decisions,
        "calls": len(results),
        "parse_failed": parse_failed,
        "failed_pairs": failed_pairs,
        "rejected": rejected,
    }


def _persist_centroids(centroids: dict[str, np.ndarray], db_path: str | None) -> None:
    for neuron_id, center in centroids.items():
        network.set_neuron_centroid(neuron_id, center.tolist(), db_path=db_path)


async def build_synapse_candidates(
    *,
    db_path: str | None = None,
    since: str = "",
    hot=None,
    pair_limit: int = PAIR_LIMIT,
    llm_limit: int = LLM_PAIRS,
    concurrency: int = 1,
    seed_neurons: list[str] | None = None,
) -> dict:
    groups = await asyncio.to_thread(_group_nodes, db_path)
    hot = hot if hot is not None else _try_hot()
    centroids, missing = await asyncio.to_thread(group_centroids, groups, hot)
    if hot is None:
        return {
            "pairs": [],
            "decisions": [],
            "calls": 0,
            "parse_failed": 0,
            "rejected": 0,
            "truncated": False,
            "vector_missing": missing,
            "vector_unavailable": True,
        }
    anchors: set[str] | None = None
    since_at = _as_datetime(since)
    if since_at is not None:
        anchors = set()
        for neuron_id, members in groups.items():
            for item in members:
                created = _as_datetime(item.get("created_at"))
                if created is not None and created >= since_at:
                    anchors.add(neuron_id)
                    break
    elif since:
        logger.warning(
            f"[memory-network] unparsable since={since!r}; scanning all groups"
        )
    seeds = {str(value) for value in (seed_neurons or []) if value}
    if seeds:
        anchors = (anchors or set()) | seeds
    await asyncio.to_thread(_persist_centroids, centroids, db_path)
    built = await asyncio.to_thread(
        candidate_pairs,
        groups,
        centroids,
        hot=hot,
        anchors=anchors,
        limit=pair_limit,
    )
    pairs = built["pairs"]
    use_llm = llm_limit > 0 and bool(pairs) and llm_client.is_configured()
    if use_llm:
        reviewed = await review_pairs(
            pairs,
            limit=llm_limit,
            max_calls=max(1, math.ceil(llm_limit / LLM_BATCH)),
            concurrency=concurrency,
        )
        decisions = reviewed["decisions"]
    else:
        reviewed = {"calls": 0, "parse_failed": 0, "rejected": 0}
        decisions = [
            {
                **pair,
                "relation": "centroid",
                "strength": pair["score"],
                "final_weight": pair["score"],
            }
            for pair in pairs
        ]
    return {
        "pairs": pairs,
        "decisions": decisions,
        "calls": reviewed["calls"],
        "parse_failed": reviewed["parse_failed"],
        "rejected": reviewed["rejected"],
        "truncated": built["truncated"],
        "llm_skipped": bool(pairs) and not use_llm,
        "vector_missing": missing,
        "vector_unavailable": False,
    }


def write_synapses(
    decisions: list[dict],
    *,
    db_path: str | None = None,
    min_weight: float = MIN_SYNAPSE_WEIGHT,
) -> int:
    written = 0
    for decision in decisions:
        final = float(decision.get("final_weight") or 0.0)
        if final < min_weight:
            continue
        method = (
            "centroid" if decision.get("relation") == "centroid" else "centroid+llm"
        )
        if network.upsert_synapse(
            {
                "from_neuron": decision["left_group"],
                "to_neuron": decision["right_group"],
                "weight": final,
                "base_weight": final,
                "relation": decision.get("relation") or "",
                "metadata": {"method": method},
            },
            db_path=db_path,
        ):
            written += 1
    return written


def top_synapses(
    neuron_id: str,
    *,
    min_weight: float = 0.0,
    limit: int = MAX_SYNAPSE_DEGREE,
    db_path: str | None = None,
) -> list[dict]:
    return network.synapses_of(
        neuron_id, min_weight=min_weight, limit=limit, db_path=db_path
    )


def enforce_degree_cap(
    *,
    db_path: str | None = None,
    cap: int = MAX_SYNAPSE_DEGREE,
    dry_run: bool = False,
) -> int:
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT synapse_id, from_neuron, to_neuron FROM synapses "
            "WHERE status='active' ORDER BY weight DESC, synapse_id"
        ).fetchall()
        kept: dict[str, int] = defaultdict(int)
        archive: list[int] = []
        for synapse_id, left, right in rows:
            if kept[str(left)] >= cap or kept[str(right)] >= cap:
                archive.append(int(synapse_id))
                continue
            kept[str(left)] += 1
            kept[str(right)] += 1
        if archive and not dry_run:
            connection.executemany(
                "UPDATE synapses SET status='archived' WHERE synapse_id=?",
                [(synapse_id,) for synapse_id in archive],
            )
    return len(archive)
