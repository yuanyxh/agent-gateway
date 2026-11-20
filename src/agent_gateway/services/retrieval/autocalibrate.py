"""Calibrate the active model's floors from the data it already holds.

Runs once when the current embedder has no measured floors: read **every**
record from both layers, query each by its own full text, and take a low
percentile of the scores it earns. Writes the result where `floors_for` reads
it, so the next process starts calibrated.

Sampling is deliberately absent. A 100-record prefix is not the distribution the
retriever sees, and neither is a record's opening 20 characters; both produced
floors that rejected the very records they were meant to admit.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from typing import Any, Callable

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.retrieval.gate import calibrate as knobs
from agent_gateway.services.retrieval.gate.thresholds import (
    FLOOR_KEYS,
    override_path,
)

logger = setup_logging("server")

#: Rows fetched per scroll page while enumerating a layer.
SCAN_PAGE = 512
#: Candidates fetched per probe. The record must appear here to count.
SEARCH_LIMIT = 10
#: Probe blocks between progress logs.
LOG_EVERY = 250
#: A record shorter than this has no retrievable probe.
MIN_RECORD_CHARS = 8
#: Probes used to detect an identity self-query before running the full layer.
DEGENERATE_PROBES = 20
#: A self-score this high means the probe vector equals the stored vector.
DEGENERATE_SCORE = 0.99
#: A floor this high cannot have been measured on real queries.
MAX_TRUSTED_FLOOR = 0.95
#: Notes are re-checked against the query at a fraction of the cold floor;
#: measured tables put it at 0.64-0.71 of cold across two models.
NOTES_SEMANTIC_RATIO = 0.65
#: Coverage is a share of query terms, not a similarity, so it does not move
#: with the model.
NOTES_COVERAGE = 0.50


def _hot_samples() -> list[tuple[str, str]]:
    """Every active hot record for the default user, read page by page."""
    from agent_gateway.services.mcp import server_state
    from agent_gateway.services.mcp.server_constants import DEFAULT_USER_ID

    memory = server_state.get_hot_memory()
    if memory is None:
        return []
    store = memory.vector_store
    filters = {"user_id": DEFAULT_USER_ID}
    try:
        query_filter = store._create_filter(filters)
    except Exception as e:  # noqa: BLE001 - fall back to filtering in Python
        logger.warning(f"[calibrate] hot filter unavailable ({e}); scanning all users")
        query_filter = None

    samples: list[tuple[str, str]] = []
    offset = None
    while True:
        points, offset = store.client.scroll(
            collection_name=store.collection_name,
            scroll_filter=query_filter,
            limit=SCAN_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if query_filter is None and payload.get("user_id") != DEFAULT_USER_ID:
                continue
            text = str(payload.get("data") or "")
            if len(text.strip()) >= MIN_RECORD_CHARS:
                samples.append((str(point.id), text))
        if offset is None:
            break
    logger.info(f"[calibrate] hot layer scanned: {len(samples)} probes")
    return samples


def _hot_search(probe: str) -> list[tuple[str, float]]:
    """Semantic score of every candidate for `probe`; read-only.

    Calls the vector store directly instead of `mem0.search`: only the cosine
    score matters here, and the BM25/entity stages cost several hundred
    milliseconds per probe. It also never touches the handlers' access counters
    or heat fields.
    """
    from agent_gateway.services.mcp import server_state
    from agent_gateway.services.mcp.server_constants import DEFAULT_USER_ID

    memory = server_state.get_hot_memory()
    if memory is None:
        return []
    try:
        vector = memory.embedding_model.embed(probe, "search")
        hits = memory.vector_store.search(
            query=probe,
            vectors=vector,
            top_k=SEARCH_LIMIT,
            filters={"user_id": DEFAULT_USER_ID},
        )
    except Exception as e:  # noqa: BLE001 - one failed probe must not stop calibration
        logger.warning(f"[calibrate] hot probe failed: {e}")
        return []
    return [(str(hit.id), float(hit.score)) for hit in hits]


def _cold_samples() -> list[tuple[str, str]]:
    """Every cold record, read page by page."""
    from agent_gateway.services.mcp import server_state
    from agent_gateway.services.mcp.server_constants import COLD_COLLECTION

    client = server_state.get_qdrant()
    if client is None:
        return []
    samples: list[tuple[str, str]] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLD_COLLECTION,
            limit=SCAN_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            text = str(payload.get("content") or "")
            if len(text.strip()) >= MIN_RECORD_CHARS:
                samples.append((str(point.id), text))
        if offset is None:
            break
    logger.info(f"[calibrate] cold layer scanned: {len(samples)} probes")
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


def _token_budget(samples: list[tuple[str, str]]) -> dict[str, Any]:
    """How many probes the active tokenizer truncates, against a named limit."""
    from agent_gateway.services.mcp import server_embed

    budget = int(server_embed.EMBEDDER_SPEC.max_tokens)
    counts: list[int] = []
    for _, text in samples:
        count = server_embed.count_tokens(text)
        if count is not None:
            counts.append(count)
    if not counts:
        return {"max_tokens": budget, "counted": 0, "over_budget": None}
    over = sum(1 for count in counts if count > budget)
    return {
        "max_tokens": budget,
        "counted": len(counts),
        "over_budget": over,
        "truncated_ratio": round(over / len(counts), 4),
        "tokens_max": max(counts),
    }


def _measure(
    samples: list[tuple[str, str]],
    search: Callable[[str], list[tuple[str, float]]],
    label: str,
) -> dict[str, Any]:
    """Self-scores for every probe, in blocks so progress stays visible."""
    stats: dict[str, Any] = {}
    scores: list[float] = []
    started = time.perf_counter()
    for start in range(0, len(samples), LOG_EVERY):
        scores.extend(
            knobs.self_scores(samples[start : start + LOG_EVERY], search, stats)
        )
        done = min(start + LOG_EVERY, len(samples))
        if done % LOG_EVERY == 0 or done == len(samples):
            logger.info(
                f"[calibrate] {label} {done}/{len(samples)} probes, "
                f"{len(scores)} self-hits, {int(time.perf_counter() - started)}s"
            )
    probes = int(stats.get("probes", 0))
    spread = (
        {
            f"p{int(q * 100)}": round(knobs.percentile(scores, q), 3)
            for q in (0.05, 0.25, 0.50, 0.75)
        }
        if scores
        else {}
    )
    return {
        "floor": knobs.floor_from_scores(scores),
        "scored": len(scores),
        "probes": probes,
        "too_short": int(stats.get("too_short", 0)),
        "coverage": round(len(scores) / probes, 4) if probes else 0.0,
        "avg_chars": round(stats.get("chars_total", 0) / probes, 1) if probes else 0.0,
        "max_chars": int(stats.get("max_chars", 0)),
        "spread": spread,
    }


def _degenerate(samples: list[tuple[str, str]], search: Callable) -> bool:
    """True when a self-query scores ~1.0, i.e. the probe vector is the stored one.

    This is a short-circuit, not a sampled threshold: a full-text probe against
    the same model is an identity query, so the median floor would be 1.0 and
    reject every real query. Keep the run from spending minutes to find that out.
    """
    probe = samples[:DEGENERATE_PROBES]
    scores = knobs.self_scores(probe, search)
    return (
        len(scores) >= DEGENERATE_PROBES // 2
        and knobs.percentile(scores, 0.50) >= DEGENERATE_SCORE
    )


def _degenerate_result() -> dict[str, Any]:
    return {
        "floor": None,
        "scored": 0,
        "probes": 0,
        "too_short": 0,
        "coverage": 0.0,
        "avg_chars": 0.0,
        "max_chars": 0,
        "spread": {},
    }


def measure() -> dict[str, Any]:
    """Floor candidates for both layers plus the probe counts they came from."""
    hot_samples = _hot_samples()
    cold_samples = _cold_samples()
    hot_skip = _degenerate(hot_samples, _hot_search)
    cold_skip = _degenerate(cold_samples, _cold_search)
    if hot_skip:
        logger.warning("[calibrate] hot self-scores are identity; floors skipped")
    if cold_skip:
        logger.warning("[calibrate] cold self-scores are identity; floors skipped")
    hot = (
        _degenerate_result() if hot_skip else _measure(hot_samples, _hot_search, "hot")
    )
    cold = (
        _degenerate_result()
        if cold_skip
        else _measure(cold_samples, _cold_search, "cold")
    )
    return {
        "hot": hot["floor"],
        "cold": cold["floor"],
        "hot_samples": hot["probes"],
        "cold_samples": cold["probes"],
        "hot_scored": hot["scored"],
        "cold_scored": cold["scored"],
        "hot_coverage": hot["coverage"],
        "cold_coverage": cold["coverage"],
        "spread": {"hot": hot["spread"], "cold": cold["spread"]},
        "layers": {
            "hot": {k: v for k, v in hot.items() if k != "floor"},
            "cold": {k: v for k, v in cold.items() if k != "floor"},
        },
        "budget": {
            "hot": _token_budget(hot_samples),
            "cold": _token_budget(cold_samples),
        },
    }


def write(tag: str, measured: dict[str, Any]) -> dict[str, float] | None:
    """Merge measured floors into the override file; None when a layer lacked data."""
    hot = measured.get("hot")
    cold = measured.get("cold")
    if hot is None or cold is None:
        return None
    if not (0.0 < float(hot) < MAX_TRUSTED_FLOOR) or not (
        0.0 < float(cold) < MAX_TRUSTED_FLOOR
    ):
        logger.warning(
            f"[calibrate] refusing implausible floors hot={hot} cold={cold}; "
            f"self-query calibration cannot measure real-query floors"
        )
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
    previous = data.get(tag) if isinstance(data.get(tag), dict) else None
    data[tag] = entry
    meta = data.setdefault("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
        data["_meta"] = meta
    meta[tag] = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "probe": "full-text",
        "samples": measured.get("hot_samples"),
        "scored": {
            "hot": measured.get("hot_scored"),
            "cold": measured.get("cold_scored"),
        },
        "coverage": {
            "hot": measured.get("hot_coverage"),
            "cold": measured.get("cold_coverage"),
        },
        "spread": measured.get("spread"),
        "budget": measured.get("budget"),
        "superseded": previous,
        "superseded_reason": "measured from the first 20 characters, not full text",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
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
    logger.info(
        f"[calibrate] {tag} floors {written} in {elapsed}ms "
        f"(hot {measured['hot_scored']}/{measured['hot_samples']} probes, "
        f"cold {measured['cold_scored']}/{measured['cold_samples']})"
    )
    return {"tag": tag, "written": written, "elapsed_ms": elapsed, **measured}


__all__ = ["measure", "run", "write"]
