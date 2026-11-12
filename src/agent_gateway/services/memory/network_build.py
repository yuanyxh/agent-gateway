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

from agent_gateway.core import config, prompt_loader
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import network, retention, sparse, store

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


def _active_nodes(db_path: str | None = None, *, layer: str = "") -> list[dict]:
    nodes: list[dict] = []
    offset = 0
    while True:
        page = store.list_nodes(
            layer=layer,
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
    layer = "" if config.memory_brain_pair_mode() == "v3" else "hot"
    nodes = _active_nodes(db_path, layer=layer)
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
    groups: dict[str, list[dict]], hot, *, cold_loader=None
) -> tuple[dict[str, np.ndarray], int]:
    """Accumulate one centroid per group from hot vectors, then cold ones."""
    node_to_neuron = {
        item["node_id"]: neuron_id
        for neuron_id, members in groups.items()
        for item in members
    }
    sums: dict[str, np.ndarray] = {}
    counts: dict[str, int] = defaultdict(int)
    if hot is not None:
        vector_store = hot.vector_store
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
            except Exception as e:  # noqa: BLE001 - a missing vector store is degraded
                logger.warning(f"[memory-network] vector load failed: {e}")
                continue
            for point in points:
                vector = _dense(getattr(point, "vector", None))
                neuron_id = node_to_neuron.get(str(point.id))
                if not vector or neuron_id is None:
                    continue
                dense = np.asarray(vector, dtype=np.float32)
                sums[neuron_id] = (
                    sums[neuron_id] + dense if neuron_id in sums else dense
                )
                counts[neuron_id] += 1
    remaining = [node for node, neuron in node_to_neuron.items() if neuron not in sums]
    if cold_loader is not None and remaining:
        try:
            vectors = cold_loader(remaining)
        except Exception as e:  # noqa: BLE001 - cold vectors are an enhancement
            logger.warning(f"[memory-network] cold vector load failed: {e}")
            vectors = {}
        for node, vector in (vectors or {}).items():
            neuron_id = node_to_neuron.get(str(node))
            dense = _dense(vector)
            if neuron_id is None or not dense:
                continue
            array = np.asarray(dense, dtype=np.float32)
            sums[neuron_id] = sums[neuron_id] + array if neuron_id in sums else array
            counts[neuron_id] += 1
    centroids: dict[str, np.ndarray] = {}
    for neuron_id, total in sums.items():
        center = np.asarray(total, dtype=np.float32) / counts[neuron_id]
        norm = float(np.linalg.norm(center))
        if norm > 0:
            center = center / norm
        centroids[neuron_id] = np.asarray(center, dtype=np.float32)
    return centroids, len(groups) - len(centroids)


def _cold_loader(db_path: str | None):
    """Fetch cold-node vectors from the cold Qdrant collection by point id."""

    def load(node_ids: list[str]) -> dict[str, list[float]]:
        from agent_gateway.services.mcp import server_memory

        client = server_memory.get_qdrant()
        if client is None:
            return {}
        node_ids = [str(node) for node in node_ids if node]
        mapping: dict[str, str] = {}
        with store.connect(db_path) as connection:
            for start in range(0, len(node_ids), _VECTOR_CHUNK):
                chunk = node_ids[start : start + _VECTOR_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT node_id, json_extract(metadata, '$.point_id') "
                    "FROM memory_nodes WHERE layer='cold' AND json_valid(metadata) "
                    f"AND node_id IN ({marks})",
                    chunk,
                ).fetchall()
                for node, point in rows:
                    if point:
                        mapping[str(point)] = str(node)
        if not mapping:
            return {}
        points = client.retrieve(
            collection_name=server_memory.COLD_COLLECTION,
            ids=list(mapping),
            with_vectors=True,
        )
        vectors: dict[str, list[float]] = {}
        for point in points:
            node = mapping.get(str(point.id))
            vector = _dense(getattr(point, "vector", None))
            if node and vector:
                vectors[node] = vector
        return vectors

    return load


def _stored_centroids(
    groups: dict[str, list[dict]], db_path: str | None
) -> tuple[dict[str, np.ndarray], int]:
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT neuron_id, centroid FROM neurons WHERE centroid IS NOT NULL"
        ).fetchall()
    wanted = set(groups)
    centroids: dict[str, np.ndarray] = {}
    for neuron_id, raw in rows:
        key = str(neuron_id)
        if key not in wanted:
            continue
        values = network.decode_centroid(raw)
        if not values:
            continue
        center = np.asarray(values, dtype=np.float32)
        norm = float(np.linalg.norm(center))
        centroids[key] = center / norm if norm > 0 else center
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
    pair_mode: str = "v2",
    codes: dict[str, np.ndarray] | None = None,
    sparse_floor: float = 0.0,
) -> dict:
    node_to_neuron = {
        item["node_id"]: neuron_id
        for neuron_id, members in groups.items()
        for item in members
    }
    anchor_ids = [
        neuron_id
        for neuron_id, members in groups.items()
        if (pair_mode != "v2" or 2 <= len(members) <= 8)
        and neuron_id in centroids
        and (anchors is None or neuron_id in anchors)
    ]
    if not anchor_ids:
        return {"pairs": [], "budget_exhausted": False}
    if hot is None:
        return _centroid_pairs(
            anchor_ids,
            centroids,
            groups,
            per_anchor=per_anchor,
            floor=floor,
            limit=limit,
            pair_mode=pair_mode,
            codes=codes,
            sparse_floor=sparse_floor,
        )
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
            hits = []
            for request in requests:
                try:
                    single = vector_store.client.query_batch_points(
                        collection_name=vector_store.collection_name,
                        requests=[request],
                    )
                    hits.append(single[0] if single else None)
                except Exception:  # noqa: BLE001 - one bad anchor must not stop all
                    hits.append(None)
        for neuron_id, result in zip(batch, hits):
            if result is None:
                continue
            for point in getattr(result, "points", result) or []:
                target = node_to_neuron.get(str(point.id))
                if not target or target == neuron_id:
                    continue
                cosine = float(point.score)
                if cosine < floor:
                    continue
                score = _blend_score(
                    neuron_id,
                    target,
                    cosine,
                    pair_mode=pair_mode,
                    codes=codes,
                    sparse_floor=sparse_floor,
                )
                if score is None:
                    continue
                if per_anchor_count[neuron_id] >= per_anchor:
                    continue
                key = tuple(sorted((neuron_id, target)))
                if key not in scored:
                    scored[key] = {
                        "left": key[0],
                        "right": key[1],
                        "score": score,
                        "cosine": cosine,
                    }
                    per_anchor_count[neuron_id] += 1
                else:
                    scored[key]["score"] = max(scored[key]["score"], score)
                    scored[key]["cosine"] = max(scored[key]["cosine"], cosine)
    return _shape_pairs(scored, groups, limit)


def _blend_score(
    neuron_id: str,
    target: str,
    score: float,
    *,
    pair_mode: str,
    codes: dict | None,
    sparse_floor: float,
) -> float | None:
    if pair_mode != "v3" or not codes:
        return score
    left_code = codes.get(neuron_id)
    right_code = codes.get(target)
    if left_code is None or right_code is None:
        return score
    overlap = sparse.jaccard(left_code, right_code)
    if overlap < sparse_floor:
        return None
    return 0.5 * score + 0.5 * overlap


def _shape_pairs(
    scored: dict[tuple[str, str], dict], groups: dict[str, list[dict]], limit: int
) -> dict:
    ordered = sorted(scored.values(), key=lambda item: -item["score"])
    budget_exhausted = len(ordered) > limit
    pairs = []
    for item in ordered[:limit]:
        pair_id = f"syn:{item['left']}|{item['right']}"
        pairs.append(
            {
                "pair_id": pair_id,
                "left_group": item["left"],
                "right_group": item["right"],
                "score": round(item["score"], 3),
                "cosine": round(item.get("cosine", item["score"]), 3),
                "left_samples": _sample(groups.get(item["left"], [])),
                "right_samples": _sample(groups.get(item["right"], [])),
            }
        )
    return {"pairs": pairs, "budget_exhausted": budget_exhausted}


def _centroid_pairs(
    anchor_ids: list[str],
    centroids: dict[str, np.ndarray],
    groups: dict[str, list[dict]],
    *,
    per_anchor: int,
    floor: float,
    limit: int,
    pair_mode: str,
    codes: dict | None,
    sparse_floor: float,
) -> dict:
    ids: dict[int, list[str]] = defaultdict(list)
    for key, vector in centroids.items():
        ids[len(vector)].append(key)
    scored: dict[tuple[str, str], dict] = {}
    per_anchor_count: dict[str, int] = defaultdict(int)
    for dims, keys in ids.items():
        matrix = np.stack([centroids[key] for key in keys]).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.clip(norms, 1e-8, None)
        position = {key: index for index, key in enumerate(keys)}
        block_index = [position[node] for node in anchor_ids if node in position]
        for start in range(0, len(block_index), 32):
            block = block_index[start : start + 32]
            scores = matrix[block] @ matrix.T
            for offset, index in enumerate(block):
                neuron_id = keys[index]
                row = scores[offset]
                take = min(len(row), per_anchor + 3)
                order = np.argpartition(-row, take - 1)[:take]
                order = order[np.argsort(-row[order])]
                for target_index in order:
                    if int(target_index) == index:
                        continue
                    target = keys[int(target_index)]
                    cosine = float(row[int(target_index)])
                    if cosine < floor:
                        continue
                    score = _blend_score(
                        neuron_id,
                        target,
                        cosine,
                        pair_mode=pair_mode,
                        codes=codes,
                        sparse_floor=sparse_floor,
                    )
                    if score is None:
                        continue
                    if per_anchor_count[neuron_id] >= per_anchor:
                        continue
                    key = tuple(sorted((neuron_id, target)))
                    if key not in scored:
                        scored[key] = {
                            "left": key[0],
                            "right": key[1],
                            "score": score,
                            "cosine": cosine,
                        }
                        per_anchor_count[neuron_id] += 1
                    else:
                        scored[key]["score"] = max(scored[key]["score"], score)
                        scored[key]["cosine"] = max(scored[key]["cosine"], cosine)
    return _shape_pairs(scored, groups, limit)


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
        by_id = {pair["pair_id"]: pair for pair in batch}
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
                    "final_weight": merge_weight(
                        pair.get("cosine", pair["score"]), strength
                    ),
                }
            )
    return {
        "decisions": decisions,
        "calls": len(results),
        "parse_failed": parse_failed,
        "failed_pairs": failed_pairs,
        "rejected": rejected,
    }


def _load_codes(db_path: str | None) -> dict[str, np.ndarray]:
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT neuron_id, sparse_code FROM neurons WHERE sparse_code IS NOT NULL"
        ).fetchall()
    return {str(row[0]): sparse.decode(row[1]) for row in rows}


def _persist_centroids(centroids: dict[str, np.ndarray], db_path: str | None) -> None:
    if not centroids:
        return
    now = datetime.now().isoformat()
    rows = [
        (network.encode_centroid(center), now, neuron_id)
        for neuron_id, center in centroids.items()
    ]
    with store.connect(db_path) as connection:
        connection.executemany(
            "UPDATE neurons SET centroid=?, updated_at=? WHERE neuron_id=?", rows
        )


async def build_synapse_candidates(
    *,
    db_path: str | None = None,
    since: str = "",
    hot=None,
    pair_limit: int = PAIR_LIMIT,
    llm_limit: int = LLM_PAIRS,
    concurrency: int = 1,
    seed_neurons: list[str] | None = None,
    pair_mode: str = "v2",
    sparse_floor: float = 0.0,
) -> dict:
    groups = await asyncio.to_thread(_group_nodes, db_path)
    hot = hot if hot is not None else _try_hot()
    centroids, missing = await asyncio.to_thread(group_centroids, groups, hot)
    if hot is not None and not centroids:
        hot = None
    pending = {key: value for key, value in groups.items() if key not in centroids}
    if pending:
        cold, _ = await asyncio.to_thread(
            group_centroids, pending, None, cold_loader=_cold_loader(db_path)
        )
        centroids.update(cold)
        missing = len(groups) - len(centroids)
    if not centroids:
        centroids, missing = await asyncio.to_thread(_stored_centroids, groups, db_path)
        hot = None
    elif missing:
        pending = {key: value for key, value in groups.items() if key not in centroids}
        stored, _ = await asyncio.to_thread(_stored_centroids, pending, db_path)
        centroids.update(stored)
        missing = len(groups) - len(centroids)
    dims = sorted({int(vector.shape[0]) for vector in centroids.values()})
    if len(dims) > 1:
        logger.warning(f"[memory-network] mixed centroid dims {dims}")
    if not centroids:
        return {
            "pairs": [],
            "decisions": [],
            "calls": 0,
            "parse_failed": 0,
            "rejected": 0,
            "budget_exhausted": False,
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
    if hot is not None:
        await asyncio.to_thread(_persist_centroids, centroids, db_path)
    codes = await asyncio.to_thread(_load_codes, db_path) if pair_mode == "v3" else {}
    built = await asyncio.to_thread(
        candidate_pairs,
        groups,
        centroids,
        hot=hot,
        anchors=anchors,
        limit=pair_limit,
        pair_mode=pair_mode,
        codes=codes,
        sparse_floor=sparse_floor,
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
        reviewed = {"calls": 0, "parse_failed": 0, "failed_pairs": 0, "rejected": 0}
        decisions = [
            {
                **pair,
                "relation": "centroid",
                "strength": pair["score"],
                "final_weight": pair.get("cosine", pair["score"]),
            }
            for pair in pairs
        ]
    return {
        "pairs": pairs,
        "decisions": decisions,
        "calls": reviewed["calls"],
        "parse_failed": reviewed["parse_failed"],
        "failed_pairs": reviewed.get("failed_pairs", 0),
        "rejected": reviewed["rejected"],
        "budget_exhausted": built["budget_exhausted"],
        "llm_skipped": bool(pairs) and not use_llm,
        "vector_missing": missing,
        "vector_unavailable": hot is None,
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


BRIDGE_KIND = "bridge"
BRIDGE_MAX_MAIN = 4000
_VECTOR_CHUNK = 500


def _components(edges) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    components: list[list[str]] = []
    seen: set[str] = set()
    for start in adjacency:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: list[str] = []
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(members)
    components.sort(key=len, reverse=True)
    return components


def _vectors_for(ids, db_path) -> dict[str, np.ndarray]:
    wanted = sorted(set(ids))
    vectors: dict[str, np.ndarray] = {}
    with store.connect(db_path) as connection:
        for start in range(0, len(wanted), _VECTOR_CHUNK):
            chunk = wanted[start : start + _VECTOR_CHUNK]
            rows = connection.execute(
                "SELECT neuron_id, centroid FROM neurons WHERE neuron_id IN "
                f"({','.join('?' for _ in chunk)}) AND centroid IS NOT NULL",
                chunk,
            ).fetchall()
            for neuron_id, raw in rows:
                values = network.decode_centroid(raw)
                if not values:
                    continue
                vector = np.asarray(values, dtype=np.float32)
                norm = float(np.linalg.norm(vector))
                if norm > 0:
                    vectors[str(neuron_id)] = vector / norm
    return vectors


def bridge_components(
    db_path: str | None = None, *, max_main: int = BRIDGE_MAX_MAIN
) -> list[dict]:
    with store.connect(db_path) as connection:
        edges = connection.execute(
            "SELECT from_neuron, to_neuron FROM synapses WHERE status='active'"
        ).fetchall()
    components = _components(edges)
    if len(components) < 2:
        return []
    main = components[0]
    degree = defaultdict(int)
    for left, right in edges:
        degree[left] += 1
        degree[right] += 1
    main_nodes = sorted(main, key=lambda node: -degree[node])[:max_main]
    small_nodes = [node for component in components[1:] for node in component]
    vectors = _vectors_for(main_nodes + small_nodes, db_path)
    by_dims: dict[int, tuple[list[str], np.ndarray]] = {}
    for target in main_nodes:
        vector = vectors.get(target)
        if vector is None or len(vector) in by_dims:
            continue
        members = [
            node
            for node in main_nodes
            if vectors.get(node) is not None and vectors[node].shape == vector.shape
        ]
        matrix = np.stack([vectors[node] for node in members]).astype(np.float32)
        by_dims[len(vector)] = (members, matrix)
    bridges = []
    for component in components[1:]:
        best = (0.0, "", "")
        for node in component:
            vector = vectors.get(node)
            entry = by_dims.get(len(vector)) if vector is not None else None
            if entry is None:
                continue
            members, matrix = entry
            scores = matrix @ vector
            index = int(np.argmax(scores))
            score = float(scores[index])
            if score > best[0]:
                best = (score, node, members[index])
        if not best[1]:
            continue
        bridges.append(
            {
                "left_group": best[1],
                "right_group": best[2],
                "relation": BRIDGE_KIND,
                "strength": round(best[0], 3),
                "final_weight": round(best[0], 3),
            }
        )
    return bridges


def write_bridges(bridges: list[dict], *, db_path: str | None = None) -> int:
    now = datetime.now().isoformat()
    written = 0
    with store.connect(db_path) as connection:
        for item in bridges:
            left, right = sorted((item["left_group"], item["right_group"]))
            weight = float(item.get("final_weight") or 0.0)
            cursor = connection.execute(
                "INSERT INTO synapses (from_neuron, to_neuron, weight, "
                "base_weight, relation, kind, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?) "
                "ON CONFLICT(from_neuron, to_neuron) DO UPDATE SET "
                "status='active', weight=MAX(synapses.weight, excluded.weight), "
                "base_weight=MAX(synapses.base_weight, excluded.base_weight), "
                "updated_at=excluded.updated_at "
                "WHERE synapses.status!='active'",
                (left, right, weight, weight, BRIDGE_KIND, BRIDGE_KIND, now, now),
            )
            written += cursor.rowcount
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
