"""Calibrate the active model's floors from the data it already holds.

Runs once when the current embedder has no measured floors: sample records from
both layers, query each by its own opening words, and take a low percentile of
the scores it earns. Writes the result where `floors_for` reads it, so the next
process starts calibrated.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from agent_gateway.services.retrieval.gate import calibrate as knobs
from agent_gateway.services.retrieval.gate.thresholds import (
    FLOOR_KEYS,
    override_path,
)

logger = logging.getLogger("agent_gateway.retrieval")

#: Records sampled per layer; enough for a stable 5th percentile, small enough
#: that a first start stays in seconds.
SAMPLE_SIZE = 100
#: Candidates fetched per probe. The record must appear here to count.
SEARCH_LIMIT = 10
#: Notes are re-checked against the query at a fraction of the cold floor;
#: measured tables put it at 0.64-0.71 of cold across two models.
NOTES_SEMANTIC_RATIO = 0.65
#: Coverage is a share of query terms, not a similarity, so it does not move
#: with the model.
NOTES_COVERAGE = 0.50


def _hot_samples(limit: int) -> list[tuple[str, str]]:
    from agent_gateway.services.mcp import server_state
    from agent_gateway.services.mcp.server_constants import DEFAULT_USER_ID

    memory = server_state.get_hot_memory()
    if memory is None:
        return []
    rows = memory.get_all(filters={"user_id": DEFAULT_USER_ID}, top_k=limit * 3)
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    samples: list[tuple[str, str]] = []
    for row in rows:
        text = str(row.get("memory") or row.get("content") or "")
        record = row.get("id")
        if record and len(text.strip()) >= 8:
            samples.append((str(record), text))
        if len(samples) >= limit:
            break
    return samples


def _hot_search(probe: str) -> list[tuple[str, float]]:
    from agent_gateway.services.mcp import server_state
    from agent_gateway.services.mcp.server_constants import DEFAULT_USER_ID

    memory = server_state.get_hot_memory()
    if memory is None:
        return []
    raw = memory.search(
        probe,
        filters={"user_id": DEFAULT_USER_ID},
        top_k=SEARCH_LIMIT,
        threshold=0.0,
        explain=True,
    )
    rows = raw if isinstance(raw, list) else raw.get("results", [])
    out: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = (row.get("score_details") or {}).get("semantic_score")
        if score is None:
            score = row.get("score")
        try:
            out.append((str(row.get("id")), float(score)))
        except (TypeError, ValueError):
            continue
    return out


def _cold_samples(limit: int) -> list[tuple[str, str]]:
    from agent_gateway.services.mcp import server_state
    from agent_gateway.services.mcp.server_constants import COLD_COLLECTION

    client = server_state.get_qdrant()
    if client is None:
        return []
    points, _ = client.scroll(
        collection_name=COLD_COLLECTION,
        limit=limit * 2,
        with_payload=True,
        with_vectors=False,
    )
    samples: list[tuple[str, str]] = []
    for point in points:
        payload = point.payload or {}
        text = str(payload.get("content") or "")
        if len(text.strip()) >= 8:
            samples.append((str(point.id), text))
        if len(samples) >= limit:
            break
    return samples


def _cold_search(probe: str) -> list[tuple[str, float]]:
    from agent_gateway.services.mcp import server_embed, server_state
    from agent_gateway.services.mcp.server_constants import COLD_COLLECTION

    client = server_state.get_qdrant()
    vector = server_embed.embed_query(probe)
    if client is None or vector is None:
        return []
    resp = client.query_points(
        collection_name=COLD_COLLECTION,
        query=vector.tolist(),
        limit=SEARCH_LIMIT,
        with_payload=False,
    )
    return [(str(point.id), float(point.score)) for point in resp.points]


def _measure(
    samples: list[tuple[str, str]],
    search: Callable[[str], list[tuple[str, float]]],
) -> tuple[float | None, int, dict[str, float]]:
    scores = knobs.self_scores(samples, search)
    spread = (
        {
            f"p{int(q * 100)}": round(knobs.percentile(scores, q), 3)
            for q in (0.05, 0.25, 0.50, 0.75)
        }
        if scores
        else {}
    )
    return knobs.floor_from_scores(scores), len(scores), spread


def measure() -> dict[str, Any]:
    """Floor candidates for both layers plus the probe counts they came from."""
    hot_floor, hot_n, hot_spread = _measure(_hot_samples(SAMPLE_SIZE), _hot_search)
    cold_floor, cold_n, cold_spread = _measure(_cold_samples(SAMPLE_SIZE), _cold_search)
    return {
        "hot": hot_floor,
        "cold": cold_floor,
        "hot_samples": hot_n,
        "cold_samples": cold_n,
        "spread": {"hot": hot_spread, "cold": cold_spread},
    }


def write(tag: str, measured: dict[str, Any]) -> dict[str, float] | None:
    """Merge measured floors into the override file; None when a layer lacked data."""
    hot = measured.get("hot")
    cold = measured.get("cold")
    if hot is None or cold is None:
        return None
    floors = {
        "hot": float(hot),
        "cold": float(cold),
        "expanded": float(cold),
        "notes_coverage": NOTES_COVERAGE,
        "notes_semantic": round(float(cold) * NOTES_SEMANTIC_RATIO, 3),
    }
    entry = {key: floors[key] for key in FLOOR_KEYS}
    path = override_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data[tag] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def run(tag: str) -> dict[str, Any]:
    """Measure and persist floors for `tag`; safe to call more than once."""
    started = time.perf_counter()
    measured = measure()
    written = write(tag, measured)
    elapsed = int((time.perf_counter() - started) * 1000)
    if written is None:
        logger.warning(
            f"[calibrate] insufficient data for {tag}: "
            f"hot={measured['hot_samples']} cold={measured['cold_samples']} probes"
        )
        return {"tag": tag, "written": None, "elapsed_ms": elapsed, **measured}
    logger.info(f"[calibrate] {tag} floors {written} in {elapsed}ms")
    return {"tag": tag, "written": written, "elapsed_ms": elapsed, **measured}


__all__ = ["measure", "run", "write"]
