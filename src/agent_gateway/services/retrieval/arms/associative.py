"""Associative recall: soft seeds spread through neuron synapses."""

from __future__ import annotations

from agent_gateway.core import config
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.memory_lifecycle import (
    STATUS_ACTIVE,
    visible_to,
)
from agent_gateway.services.mcp import server_embed, server_state
from agent_gateway.services.mcp.server_constants import (
    DEFAULT_HEAT_SCORE,
    DEFAULT_USER_ID,
    EPISODE_FIELD,
)
from agent_gateway.services.memory import attractor, buffer, graph_index, network
from agent_gateway.services.retrieval.arms.recall import (
    cosine,
    dense_vector,
    memory_status,
    row_dimensions,
)
from agent_gateway.services.retrieval.types import Query, RetrievedItem

logger = setup_logging("server")

HOP_DECAY = 0.75
#: `neurons.heat` has no writer yet; keep the factor explicit instead of querying it.
NEIGHBOR_HEAT_FACTOR = 0.75
MAX_VISITED_NEURONS = 50
MAX_EDGES_PER_NEURON = 8
MAX_MEMBERS_PER_VISIT = 5
SOFT_SEED_FLOOR = 0.40
TIER_PENALTY = {"active": 1.0, "warm": 0.85, "archive": 0.60, "trash": 0.0}


def soft_seeds(
    query: Query,
    *,
    hot,
    exclude: set[str],
    floor: float = SOFT_SEED_FLOOR,
    limit: int = 5,
    db_path: str | None = None,
) -> list[dict]:
    if hot is None:
        return []
    try:
        raw = hot.search(
            query.text,
            filters={"user_id": DEFAULT_USER_ID, "NOT": [{"archived": True}]},
            top_k=max(limit * 3, limit),
            threshold=0.0,
            explain=True,
        )
    except Exception as e:  # noqa: BLE001 - soft seeds are optional
        logger.warning(f"Associative soft seed search failed: {e}")
        return []
    rows = raw if isinstance(raw, list) else raw.get("results", [])
    out: list[dict] = []
    for row in rows:
        if isinstance(row, str):
            continue
        memory_id = str(row.get("id") or "")
        if not memory_id or memory_id in exclude:
            continue
        semantic = (row.get("score_details") or {}).get("semantic_score")
        try:
            score = float(semantic if semantic is not None else row.get("score"))
        except (TypeError, ValueError):
            continue
        if score < floor:
            continue
        neuron_id = network.neuron_of(memory_id, db_path=db_path)
        if not neuron_id:
            continue
        out.append(
            {
                "memory_id": memory_id,
                "neuron_id": neuron_id,
                "activation": min(1.0, score),
            }
        )
        if len(out) >= limit:
            break
    return out


def spread(
    seeds: list[dict],
    *,
    db_path: str | None = None,
    depth: int | None = None,
    beam: int | None = None,
    floor: float | None = None,
    visited_cap: int = MAX_VISITED_NEURONS,
) -> dict:
    depth = depth or config.memory_neuron_max_depth()
    beam = beam or config.memory_neuron_beam()
    floor = config.memory_neuron_activation_floor() if floor is None else floor
    activation: dict[str, float] = {}
    paths: dict[str, list[dict]] = {}
    for seed in seeds:
        neuron_id = seed.get("neuron_id") or ""
        if not neuron_id:
            continue
        value = float(seed.get("activation") or 0.0)
        if value > activation.get(neuron_id, 0.0):
            activation[neuron_id] = value
            paths[neuron_id] = [
                {"memory": seed.get("memory_id") or "", "neuron": neuron_id}
            ]
    visited = set(activation)
    truncated = False
    try:
        index = graph_index.current(db_path)
    except Exception:  # noqa: BLE001 - the SQL path stays as the fallback
        index = None
    for _ in range(max(0, depth)):
        frontier = sorted(activation.items(), key=lambda item: -item[1])[:beam]
        following: dict[str, float] = {}
        if index is None:
            links_by_neuron = network.synapses_of_many(
                [neuron_id for neuron_id, _ in frontier],
                limit=MAX_EDGES_PER_NEURON,
                db_path=db_path,
            )
        else:
            links_by_neuron = {
                neuron_id: index.neighbors(neuron_id, limit=MAX_EDGES_PER_NEURON)
                for neuron_id, _ in frontier
            }
        for current, value in frontier:
            for link in links_by_neuron.get(current, []):
                other = link["other"]
                nxt = value * float(link["weight"]) * HOP_DECAY * NEIGHBOR_HEAT_FACTOR
                if nxt < floor:
                    continue
                if nxt <= max(activation.get(other, 0.0), following.get(other, 0.0)):
                    continue
                if other not in visited and len(visited) >= visited_cap:
                    truncated = True
                    continue
                following[other] = nxt
                paths[other] = paths[current] + [
                    {
                        "synapse_id": link["synapse_id"],
                        "from": current,
                        "to": other,
                        "weight": float(link["weight"]),
                    }
                ]
                visited.add(other)
        activation.update(following)
    ordered = sorted(activation.items(), key=lambda item: -item[1])
    return {
        "activated": [
            {
                "neuron_id": neuron_id,
                "activation": value,
                "path": paths.get(neuron_id, []),
            }
            for neuron_id, value in ordered
        ],
        "visited": len(visited),
        "truncated": truncated,
    }


def _points(hot, node_ids: list[str]) -> dict[str, dict]:
    if hot is None or not node_ids:
        return {}
    client = hot.vector_store.client
    out: dict[str, dict] = {}
    for start in range(0, len(node_ids), 100):
        batch = node_ids[start : start + 100]
        try:
            points = client.retrieve(
                collection_name=hot.vector_store.collection_name,
                ids=batch,
                with_payload=True,
                with_vectors=True,
            )
        except Exception as e:  # noqa: BLE001 - degrade to no associated items
            logger.warning(f"Associative member lookup failed: {e}")
            return out
        for point in points:
            payload = point.payload or {}
            out[str(point.id)] = {
                "vector": dense_vector(getattr(point, "vector", None)),
                "payload": payload,
            }
    return out


def score_members(
    activated: list[dict],
    query_vec,
    *,
    min_score: float,
    reader_agent: str = "unknown",
    hot,
    include_archived: bool = False,
    exclude: set[str] | None = None,
    db_path: str | None = None,
) -> list[RetrievedItem]:
    if query_vec is None or not activated:
        return []
    by_neuron = network.members_of_many(
        [item["neuron_id"] for item in activated],
        limit=MAX_MEMBERS_PER_VISIT,
        db_path=db_path,
    )
    member_ids: list[str] = []
    members: dict[str, dict] = {}
    for rows in by_neuron.values():
        for member in rows:
            member_ids.append(member["node_id"])
            members[member["node_id"]] = member
    points = _points(hot, member_ids)
    activations = network.activations(member_ids, db_path=db_path)
    out: dict[str, RetrievedItem] = {}
    for item in activated:
        for member in by_neuron.get(item["neuron_id"], []):
            member_id = member["node_id"]
            if exclude and member_id in exclude:
                continue
            entry = points.get(member_id)
            if not entry or not entry["vector"]:
                continue
            payload = entry["payload"]
            if payload.get("archived") or memory_status(payload) != STATUS_ACTIVE:
                continue
            if not visible_to(payload, reader_agent):
                continue
            activation = activations.get(member_id) or {}
            tier = str(activation.get("tier") or "active")
            if tier == "trash":
                continue
            if tier == "archive" and not include_archived:
                continue
            similarity = cosine(query_vec, entry["vector"])
            weight = float(members[member_id].get("member_weight") or 1.0)
            factor = 0.5 + 0.5 * min(1.0, float(item["activation"]) * 2.0)
            final = min(similarity * factor * weight * TIER_PENALTY[tier], similarity)
            if final < min_score:
                continue
            previous = out.get(member_id)
            if previous is not None and previous.score >= final:
                continue
            out[member_id] = RetrievedItem(
                id=member_id,
                kind="memory",
                score=float(final),
                score_kind="dense",
                content=str(payload.get("data") or ""),
                layer="hot",
                match_kind="associated",
                extra={
                    **row_dimensions(payload, str(payload.get("data") or "")),
                    "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
                    "episode_id": str(payload.get(EPISODE_FIELD) or ""),
                    "neuron_id": item["neuron_id"],
                    "activation": round(float(item["activation"]), 4),
                    "hops": max(0, len(item["path"]) - 1),
                    "path": item["path"],
                    "expanded": True,
                    "edge": "associative",
                },
            )
    return list(out.values())


def _refine(result: dict, *, db_path: str | None) -> dict:
    """Run the attractor from the strongest spread results, keeping spread paths."""
    items = result.get("activated") or []
    if not items:
        return result
    paths = {item["neuron_id"]: item.get("path") or [] for item in items}
    seeds = {
        item["neuron_id"]: float(item["activation"])
        for item in items[: config.memory_neuron_beam()]
    }
    refined = attractor.activate(seeds, db_path=db_path)
    if not refined["activated"]:
        return result
    merged = [
        {
            "neuron_id": item["neuron_id"],
            "activation": item["activation"],
            "path": paths.get(item["neuron_id"], []),
        }
        for item in refined["activated"]
    ]
    return {**result, "activated": merged, "attractor": refined}


def associative_expand(
    query: Query,
    items: list[RetrievedItem],
    filters: dict | None = None,
    *,
    hot=None,
    db_path: str | None = None,
) -> list[RetrievedItem]:
    if not config.memory_neuron_enabled():
        return []
    query_vec = server_embed.embed_query(query.text)
    if query_vec is None:
        return []
    hot = hot if hot is not None else server_state.get_hot_memory()
    if hot is None:
        return []
    from agent_gateway.services.retrieval.arms.recall import floors_for

    min_score = floors_for(server_embed.EMBEDDER_SPEC.tag)["expanded"]
    reader_agent = str((filters or {}).get("reader_agent_id") or "unknown")
    known = {item.id for item in items}
    seeds: list[dict] = []
    for item in items[:3]:
        neuron_id = network.neuron_of(item.id, db_path=db_path)
        if neuron_id:
            seeds.append(
                {
                    "memory_id": item.id,
                    "neuron_id": neuron_id,
                    "activation": min(1.0, float(item.score)),
                }
            )
    seeds.extend(
        soft_seeds(
            query,
            hot=hot,
            exclude=known | {s["memory_id"] for s in seeds},
            db_path=db_path,
        )
    )
    if config.memory_brain_buffer():
        residual = buffer.read(db_path=db_path)
        known_neurons = {seed["neuron_id"] for seed in seeds}
        for seed in seeds:
            seed["activation"] = max(
                float(seed["activation"]), residual.get(seed["neuron_id"], 0.0)
            )
        for neuron_id, value in residual.items():
            if neuron_id not in known_neurons:
                seeds.append(
                    {"memory_id": "", "neuron_id": neuron_id, "activation": value}
                )
    if not seeds:
        return []
    result = spread(seeds, db_path=db_path)
    if config.memory_brain_attractor():
        result = _refine(result, db_path=db_path)
    if config.memory_brain_buffer():
        buffer.writeback(result["activated"][:20], db_path=db_path)
    return score_members(
        result["activated"],
        query_vec,
        min_score=min_score,
        reader_agent=reader_agent,
        hot=hot,
        include_archived=bool((filters or {}).get("include_archived")),
        exclude=known,
        db_path=db_path,
    )
