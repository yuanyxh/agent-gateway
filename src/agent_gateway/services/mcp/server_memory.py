"""Hot/cold memory operations for the MCP server.

Contains the mem0 hot-layer lifecycle, the cold-layer search, heat/contradiction
bookkeeping, and the memory tool handlers. server.py keeps the entry point,
project/websearch tools, and the HTTP endpoints; the few functions it still
calls are exported without the leading underscore.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
import uuid
from datetime import datetime, timedelta

from agent_gateway.llm import client as llm_client
from agent_gateway.core import prompt_loader
from mem0 import Memory
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
from mcp.types import TextContent

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.mcp.server_constants import (
    AI_API_KEY,
    COLD_COLLECTION,
    COLD_SOURCE_MEMORY_IDS_FIELD,
    COLD_STALE_FIELD,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    CONTRADICTION_TOP_K,
    DEDUPE_EXACT_SIMILARITY,
    DEDUPE_NOTICE_SIMILARITY,
    DEDUPE_TOP_K,
    DEFAULT_HEAT_SCORE,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_ID,
    DELETED_AT_FIELD,
    EPISODE_COLD_LOOKUP_LIMIT,
    EPISODE_COLD_MIN_SIMILARITY,
    EPISODE_EXPAND_DECAY,
    EPISODE_EXPAND_PER_GROUP,
    EPISODE_EXPAND_TOP_N,
    EPISODE_FIELD,
    EPISODE_INHERIT_SIMILARITY,
    EPISODE_MAX_GROUP_SIZE,
    ERR_HOT_LAYER_NOT_READY,
    ERR_MEMORY_IDS_EMPTY,
    ERR_VALUE_INVALID,
    EXCLUDE_DIRS,
    HEAT_INCREMENT,
    HEAT_MAX,
    HOT_INIT_MAX_RETRIES,
    HOT_INIT_MAX_RUNTIME_FAILURES,
    HOT_INIT_RETRY_DELAY,
    HOT_INIT_RUNTIME_COOLDOWN,
    MEM0_QDRANT_PATH,
    NOTE_EXTENSIONS,
    NOTES_DIR,
    PROCESSED_FIELD,
    PROCESSED_MAX_BIT,
    PROCESSED_RETIRED_MASK,
    REVISION_FIELD,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_FIELD,
    STATUS_SUPERSEDED,
    SUMMARY_LLM_API_URL,
    SUMMARY_LLM_MODEL,
    SUMMARY_LLM_TEMPERATURE,
    SUPERSEDED_AT_FIELD,
    SUPERSEDED_BY_FIELD,
    SUPERSES_FIELD,
    err_bit_out_of_range,
)
from agent_gateway.services.mcp.server_embed import (
    EMBEDDER_DIMS,
    EMBEDDER_MODEL,
    FASTEMBED_CACHE_DIR,
    _get_shared_embedder,
    embed_query,
)
from agent_gateway.services.mcp.server_state import (
    get_hot_memory,
    get_qdrant,
    set_hot_memory,
)
from agent_gateway.services.mcp.server_tool_schema import DEFAULT_RECALL_LIMIT
from agent_gateway.services.mcp.server_utils import dumps, int_arg

logger = setup_logging("server")

HANDLERS: dict[str, callable] = {}


def _register(name: str):
    def decorator(func):
        HANDLERS[name] = func
        return func

    return decorator


def _patch_mem0_fastembed():
    """Monkey-patch mem0's FastEmbedEmbedding to enable memory-optimization parameters.

    The original mem0 implementation passes only model_name to TextEmbedding, missing
    optimization parameters like cache_dir/threads/providers. Here we replace the init
    logic directly, skipping the waste of creating the original TextEmbedding and then
    rebuilding it.
    """
    try:
        from mem0.embeddings.fastembed import FastEmbedEmbedding
        from mem0.embeddings.base import EmbeddingBase
        import fastembed

        def _optimized_init(self, config=None):
            # Call EmbeddingBase.__init__ directly to set config, skipping the
            # unoptimized TextEmbedding creation in FastEmbedEmbedding's original init
            EmbeddingBase.__init__(self, config)
            self.config.model = self.config.model or "thenlper/gte-large"
            self.dense_model = fastembed.TextEmbedding(
                model_name=self.config.model,
                cache_dir=FASTEMBED_CACHE_DIR,
                threads=1,
                providers=["CPUExecutionProvider"],
                enable_cpu_mem_arena=False,
            )
            if not self.config.embedding_dims:
                self.config.embedding_dims = self.dense_model.embedding_size

        FastEmbedEmbedding.__init__ = _optimized_init
        logger.info("Patched mem0 FastEmbedEmbedding with memory-optimized config")
    except ImportError:
        pass


_patch_mem0_fastembed()

MEM0_CONFIG = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "path": MEM0_QDRANT_PATH,
            "on_disk": True,
            "embedding_model_dims": EMBEDDER_DIMS,
        },
    },
    "llm": {
        "provider": "openai",
        "config": {
            "model": SUMMARY_LLM_MODEL,
            "openai_base_url": SUMMARY_LLM_API_URL,
            "api_key": AI_API_KEY,
            "temperature": SUMMARY_LLM_TEMPERATURE,
        },
    },
    "embedder": {
        "provider": "fastembed",
        "config": {
            "model": EMBEDDER_MODEL,
        },
    },
}

_hot_runtime_failures = 0

_hot_last_init_attempt = 0.0


def _init_hot_memory() -> bool:
    # mem0's built-in PostHog telemetry blocks initialization, so force-disable it
    try:
        import posthog

        posthog.disabled = True
    except ImportError:
        pass
    logger.info(
        f"Mem0 telemetry disabled (MEM0_TELEMETRY={os.environ.get('MEM0_TELEMETRY')}), "
        "PostHog network calls will be skipped"
    )
    for attempt in range(HOT_INIT_MAX_RETRIES):
        try:
            set_hot_memory(Memory.from_config(MEM0_CONFIG))
            logger.info("Mem0 (hot layer) initialized")
            return True
        except Exception as e:
            logger.warning(
                f"Hot layer init attempt {attempt + 1}/{HOT_INIT_MAX_RETRIES} failed: {e}"
            )
            if attempt < HOT_INIT_MAX_RETRIES - 1:
                time.sleep(HOT_INIT_RETRY_DELAY)
    logger.error(f"Hot layer init failed after {HOT_INIT_MAX_RETRIES} attempts")
    return False


_ensure_hot_lock = asyncio.Lock()


async def ensure_hot_memory() -> bool:
    global _hot_runtime_failures, _hot_last_init_attempt
    if get_hot_memory() is not None:
        return True
    now = time.time()
    if now - _hot_last_init_attempt < HOT_INIT_RUNTIME_COOLDOWN:
        return False
    async with _ensure_hot_lock:
        if get_hot_memory() is not None:
            return True
        if time.time() - _hot_last_init_attempt < HOT_INIT_RUNTIME_COOLDOWN:
            return False
        _hot_last_init_attempt = time.time()
        success = _init_hot_memory()
    if success:
        _hot_runtime_failures = 0
        return True
    _hot_runtime_failures += 1
    logger.error(
        f"Hot layer runtime recovery failed ({_hot_runtime_failures}/{HOT_INIT_MAX_RUNTIME_FAILURES})"
    )
    if _hot_runtime_failures >= HOT_INIT_MAX_RUNTIME_FAILURES:
        fail_msg = f"[{datetime.now().isoformat()}] HOT LAYER FATAL: {_hot_runtime_failures} consecutive init failures"
        logger.critical(fail_msg)
        sys.exit(1)
    return False


def _notes_snippet(text: str, needle: str, max_lines: int = 3) -> str:
    """Take the first max_lines lines containing needle, in the same format as the
    original `grep -inF -m <max_lines>`.

    Output looks like `12:foo\\n45:bar` (line number:line content, joined by newlines);
    returns an empty string when there is no hit.
    """
    if not needle:
        return ""
    hits: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if needle in line.lower():
            hits.append(f"{line_no}:{line}")
            if len(hits) >= max_lines:
                break
    return "\n".join(hits)


def search_cold_layer(
    query: str, limit: int = 5, include_archived: bool = False
) -> list[dict]:
    results = []
    qvec = embed_query(query) if get_qdrant() else None
    if get_qdrant() and qvec is not None:
        try:
            vec = qvec.tolist()
            # Stale = the summary was derived from a memory that has since been
            # superseded; it must not be served even when archived entries are wanted.
            must_not = [
                FieldCondition(key=COLD_STALE_FIELD, match=MatchValue(value=True))
            ]
            if not include_archived:
                must_not.append(
                    FieldCondition(key="archived", match=MatchValue(value=True))
                )
            qfilter = Filter(must_not=must_not)
            resp = get_qdrant().query_points(
                collection_name=COLD_COLLECTION,
                query=vec,
                limit=limit,
                query_filter=qfilter,
            )
            for h in resp.points:
                results.append(
                    {
                        "id": h.id,
                        "content": h.payload.get("content", ""),
                        "source_file": h.payload.get("source_file", ""),
                        "created_at": h.payload.get("created_at", ""),
                        "type": h.payload.get("type", ""),
                        "score": h.score,
                        "layer": "cold",
                        "heat_score": h.payload.get("heat_score", DEFAULT_HEAT_SCORE),
                        "access_count": h.payload.get("access_count", 0),
                        "last_accessed_at": h.payload.get("last_accessed_at", ""),
                        "episode_id": h.payload.get(EPISODE_FIELD, ""),
                    }
                )
                _update_cold_heat(h.id)
        except Exception as e:
            logger.warning(f"Qdrant cold search failed: {e}")
    tokens = [t for t in query.split() if t]
    if not tokens:
        tokens = [query]
    needles = [t.lower() for t in tokens]
    head = needles[0]

    try:
        for root, dirs, files in os.walk(NOTES_DIR):
            dirs[:] = [
                d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")
            ]
            for f in files:
                if not f.lower().endswith(NOTE_EXTENSIONS):
                    continue
                path = os.path.join(root, f)
                rel = os.path.relpath(path, NOTES_DIR)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                # A hit on any term counts as a hit (equivalent to the original
                # grep -iFq short-circuit semantics). The original implementation spawned
                # a grep subprocess per file per term; measured on 454 files × N terms:
                # 2.4s (N=1) / 4.5s (N=2) / 7.5s (N=4), recall's single biggest cost.
                low = text.lower()
                if not any(n in low for n in needles):
                    continue
                results.append(
                    {
                        "id": f"notes:{rel}",
                        "content": _notes_snippet(text, head) or rel,
                        "source_file": rel,
                        "created_at": "",
                        "type": "notes",
                        "score": 0.5 + (len(tokens) / 100),
                        "layer": "notes",
                        "heat_score": DEFAULT_HEAT_SCORE,
                    }
                )
    except Exception as e:
        logger.warning(f"Notes search failed: {e}")
    results.sort(key=_heat_sort_key, reverse=True)
    return results[:limit]


_HOT_RESULT_KEYS = (
    "id",
    "layer",
    "content",
    "score",
    "created_at",
    "updated_at",
    "agent_id",
    "attributed_to",
    "heat_score",
    "layer",
)
_HOT_META_KEYS = (
    "tags",
    "confidence",
    "last_accessed_at",
    "access_count",
    "disputed",
    "disputed_by",
    "dispute_reason",
    STATUS_FIELD,
    SUPERSEDED_BY_FIELD,
    SUPERSEDED_AT_FIELD,
    REVISION_FIELD,
)


def _project_hot_result(r: dict) -> dict:
    """Hot-layer result → model-facing shape. When `content` is missing, normalize it
    from `memory` — `get_all`'s raw result only has `memory`, and trimming strictly by
    the whitelist would trim the body away."""
    out = {k: r[k] for k in _HOT_RESULT_KEYS if k in r}
    if "content" not in out:
        out["content"] = r.get("memory", "")
    meta = r.get("metadata")
    if isinstance(meta, dict):
        kept = {k: meta[k] for k in _HOT_META_KEYS if k in meta}
        if kept:
            out["metadata"] = kept
        episode = str(meta.get(EPISODE_FIELD) or "").strip()
        if episode:
            out["episode_id"] = episode
    return out


def _dense_vector(raw) -> list[float] | None:
    """Qdrant keeps the dense vector under ''; the BM25 one is a sparse dict."""
    if isinstance(raw, dict):
        raw = raw.get("") or raw.get("dense")
    return raw or None


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _expand_hot_episodes(results: list[dict], query_vec=None) -> list[dict]:
    """Pull same-group memories for the top hits, ranked by relevance to the query."""
    seeds = [r for r in results if r.get("episode_id")][:EPISODE_EXPAND_TOP_N]
    if not seeds or not get_hot_memory():
        return []
    known = {r.get("id") for r in results}
    vector_store = get_hot_memory().vector_store
    expanded = []
    for seed in seeds:
        episode_id = str(seed.get("episode_id") or "")
        try:
            points, _ = vector_store.client.scroll(
                collection_name=vector_store.collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key=EPISODE_FIELD,
                            match=MatchValue(value=episode_id),
                        )
                    ]
                ),
                limit=EPISODE_EXPAND_PER_GROUP + len(known),
                with_payload=True,
                with_vectors=query_vec is not None,
            )
        except Exception as e:
            logger.warning(f"Episode expansion failed: {e}")
            continue
        candidates = []
        for point in points:
            payload = point.payload or {}
            if point.id in known or payload.get("archived"):
                continue
            if _memory_status(payload) != STATUS_ACTIVE:
                continue
            vector = _dense_vector(getattr(point, "vector", None))
            if query_vec is not None and vector:
                score = _dot(query_vec, vector)
            else:
                score = float(seed.get("score", 0))
            candidates.append((score, point, payload))
        candidates.sort(key=lambda c: -c[0])
        seed_score = float(seed.get("score", 0))
        for _, point, payload in candidates[:EPISODE_EXPAND_PER_GROUP]:
            known.add(point.id)
            expanded.append(
                {
                    "id": point.id,
                    "layer": "hot",
                    "content": payload.get("data", ""),
                    "score": seed_score * EPISODE_EXPAND_DECAY,
                    "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
                    "episode_id": episode_id,
                    "expanded": True,
                }
            )
    return expanded


def _expand_cold_episodes(results: list[dict], query_vec=None) -> list[dict]:
    """Pull same-group cold summaries for the top cold hits."""
    seeds = [r for r in results if r.get("layer") == "cold" and r.get("episode_id")][
        :EPISODE_EXPAND_TOP_N
    ]
    if not seeds or not get_qdrant():
        return []
    known = {r.get("id") for r in results}
    expanded = []
    for seed in seeds:
        episode_id = str(seed.get("episode_id") or "")
        try:
            points, _ = get_qdrant().scroll(
                collection_name=COLD_COLLECTION,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key=EPISODE_FIELD,
                            match=MatchValue(value=episode_id),
                        )
                    ]
                ),
                limit=EPISODE_EXPAND_PER_GROUP + len(known),
                with_payload=True,
                with_vectors=query_vec is not None,
            )
        except Exception as e:
            logger.warning(f"Cold episode expansion failed: {e}")
            continue
        candidates = []
        for point in points:
            payload = point.payload or {}
            if point.id in known or payload.get("archived"):
                continue
            if payload.get(COLD_STALE_FIELD):
                continue
            vector = _dense_vector(getattr(point, "vector", None))
            if query_vec is not None and vector:
                score = _dot(query_vec, vector)
            else:
                score = float(seed.get("score", 0))
            candidates.append((score, point, payload))
        candidates.sort(key=lambda c: -c[0])
        seed_score = float(seed.get("score", 0))
        for _, point, payload in candidates[:EPISODE_EXPAND_PER_GROUP]:
            known.add(point.id)
            expanded.append(
                {
                    "id": point.id,
                    "layer": "cold",
                    "content": payload.get("content", ""),
                    "source_file": payload.get("source_file", ""),
                    "created_at": payload.get("created_at", ""),
                    "score": seed_score * EPISODE_EXPAND_DECAY,
                    "heat_score": payload.get("heat_score", DEFAULT_HEAT_SCORE),
                    "episode_id": episode_id,
                    "expanded": True,
                }
            )
    return expanded


def _heat_sort_key(item: dict) -> float:
    score = item.get("score", 0)
    heat = item.get("heat_score", (item.get("metadata") or {}).get("heat_score", 0.5))
    if isinstance(heat, (int, float)):
        heat = float(heat)
    else:
        heat = 0.5
    return score * (0.8 + 0.2 * min(heat, HEAT_MAX))


def _update_cold_heat(point_id: str) -> None:
    if not get_qdrant():
        return
    try:
        now_iso = datetime.now().isoformat()
        existing = get_qdrant().retrieve(
            collection_name=COLD_COLLECTION, ids=[point_id], with_payload=True
        )
        if not existing:
            return
        payload = existing[0].payload or {}
        access_count = payload.get("access_count", 0) + 1
        heat_score = min(
            HEAT_MAX, payload.get("heat_score", DEFAULT_HEAT_SCORE) + HEAT_INCREMENT
        )
        get_qdrant().set_payload(
            collection_name=COLD_COLLECTION,
            payload={
                "access_count": access_count,
                "last_accessed_at": now_iso,
                "heat_score": heat_score,
            },
            points=[point_id],
        )
    except Exception:
        pass


def _new_episode_id() -> str:
    return f"ep_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8]}"


def _episode_size(episode_id: str) -> int:
    """Active member count; a failed count returns 0 so writes are never blocked."""
    if not episode_id or not get_hot_memory():
        return 0
    vector_store = get_hot_memory().vector_store
    try:
        result = vector_store.client.count(
            collection_name=vector_store.collection_name,
            count_filter=Filter(
                must=[
                    FieldCondition(
                        key=EPISODE_FIELD,
                        match=MatchValue(value=episode_id),
                    )
                ],
                must_not=[
                    FieldCondition(
                        key=STATUS_FIELD,
                        match=MatchAny(any=[STATUS_SUPERSEDED, STATUS_DELETED]),
                    ),
                    FieldCondition(key="archived", match=MatchValue(value=True)),
                ],
            ),
            exact=False,
        )
        return int(result.count)
    except Exception as e:
        logger.warning(f"Episode size check failed: {e}")
        return 0


def _inherit_episode_id(supersedes: list[str], similar: list[dict]) -> str:
    """Reuse the target's group, then the closest memory above the inherit threshold."""
    for sid in supersedes:
        try:
            meta = (get_hot_memory().get(sid) or {}).get("metadata") or {}
        except Exception:
            meta = {}
        inherited = str(meta.get(EPISODE_FIELD) or "").strip()
        if inherited and _episode_size(inherited) < EPISODE_MAX_GROUP_SIZE:
            return inherited
    if similar and similar[0].get("score", 0) >= EPISODE_INHERIT_SIMILARITY:
        inherited = str(similar[0].get("episode_id") or "").strip()
        if inherited and _episode_size(inherited) < EPISODE_MAX_GROUP_SIZE:
            return inherited
    return _new_episode_id()


def _cold_episode_id(
    source_memory_ids: list, explicit: str = "", content_vec: list | None = None
) -> str:
    """Closest source memory's episode; majority is the vector-less fallback."""
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    hot = get_hot_memory()
    if not source_memory_ids or hot is None:
        return _new_episode_id()
    try:
        points = hot.vector_store.client.retrieve(
            collection_name=hot.vector_store.collection_name,
            ids=list(source_memory_ids)[:EPISODE_COLD_LOOKUP_LIMIT],
            with_payload=True,
            with_vectors=content_vec is not None,
        )
    except Exception as e:
        logger.warning(f"Cold episode lookup failed: {e}")
        return _new_episode_id()
    tally = {}
    closest, closest_score = "", -1.0
    for point in points:
        payload = point.payload or {}
        episode_id = str(payload.get(EPISODE_FIELD) or "").strip()
        if not episode_id:
            continue
        tally[episode_id] = tally.get(episode_id, 0) + 1
        if content_vec is not None:
            vector = _dense_vector(getattr(point, "vector", None))
            if vector:
                score = _dot(content_vec, vector)
                if score > closest_score:
                    closest, closest_score = episode_id, score
    if content_vec is not None:
        if closest and closest_score >= EPISODE_COLD_MIN_SIMILARITY:
            return closest
        return _new_episode_id()
    if not tally:
        return _new_episode_id()
    return max(tally, key=tally.get)


def _build_metadata(src: dict, now_iso: str) -> dict:
    return {
        "agent_id": src.get("agent_id", "unknown"),
        "visibility": src.get("visibility", "all"),
        "tags": src.get("tags", []),
        EPISODE_FIELD: str(src.get("episode_id") or ""),
        "confidence": src.get("confidence", 1.0),
        "heat_score": DEFAULT_HEAT_SCORE,
        "last_accessed_at": now_iso,
        "access_count": 0,
        "archived": False,
        STATUS_FIELD: STATUS_ACTIVE,
        SUPERSEDED_BY_FIELD: "",
        REVISION_FIELD: 1,
        # Processed bitmask: bit0=summarized, bit1=retired (see server_constants)
        PROCESSED_FIELD: 0,
    }


def _memory_status(meta: dict | None) -> str:
    """Lifecycle status; legacy entries without the field count as active."""
    return str((meta or {}).get(STATUS_FIELD) or STATUS_ACTIVE)


def _active_similar(content: str) -> list[dict]:
    """Active hot memories closest to `content`. Search failure never blocks a write.

    mem0's final score is a hybrid (semantic + BM25) divided by 2, so identical text
    lands near 0.55. Dedupe thresholds must therefore read semantic_score from
    score_details, with the stored content hash as the deterministic exact match.
    """
    if not get_hot_memory():
        return []
    try:
        raw = get_hot_memory().search(
            content,
            top_k=DEDUPE_TOP_K,
            threshold=0.0,
            filters={"user_id": DEFAULT_USER_ID},
            explain=True,
        )
    except Exception as e:
        logger.warning(f"Hot dedupe search failed: {e}")
        return []
    rows = raw if isinstance(raw, list) else raw.get("results", [])
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
    similar = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if _memory_status(r.get("metadata")) != STATUS_ACTIVE:
            continue
        details = r.get("score_details") or {}
        score = details.get("semantic_score")
        if score is None:
            score = r.get("score") or 0
        if r.get("hash") == content_hash:
            score = 1.0
        similar.append(
            {
                "id": r["id"],
                "content": str(r.get("memory") or r.get("content") or "")[:120],
                "score": round(float(score), 3),
                "episode_id": str(
                    (r.get("metadata") or {}).get(EPISODE_FIELD) or ""
                ).strip(),
            }
        )
    return similar


def _update_metadata(memory_id: str, meta: dict) -> bool:
    """Metadata-only update; mem0 skips re-embedding when data is None."""
    if not get_hot_memory():
        return False
    try:
        get_hot_memory().update(memory_id, metadata=meta)
        return True
    except Exception as e:
        logger.warning(f"Metadata update failed for {memory_id}: {e}")
        return False


def _mark_superseded(
    old_ids: list[str], new_id: str, now_iso: str
) -> tuple[list, list]:
    """Soft-retire old memories: keep content and bit0, set status plus the retired bit."""
    done, failed = [], []
    for sid in old_ids:
        try:
            old = get_hot_memory().get(sid)
        except Exception:
            old = None
        if not old:
            failed.append(sid)
            continue
        meta = dict(old.get("metadata") or {})
        meta[STATUS_FIELD] = STATUS_SUPERSEDED
        meta[SUPERSEDED_BY_FIELD] = new_id
        meta[SUPERSEDED_AT_FIELD] = now_iso
        meta[PROCESSED_FIELD] = (
            int(meta.get(PROCESSED_FIELD, 0) or 0) | PROCESSED_RETIRED_MASK
        )
        if _update_metadata(sid, meta):
            done.append(sid)
        else:
            failed.append(sid)
    return done, failed


def _mark_cold_stale(memory_ids: list[str]) -> int:
    """Flag cold summaries derived from these hot memories (best effort)."""
    if not memory_ids or not get_qdrant():
        return 0
    try:
        selector = Filter(
            must=[
                FieldCondition(
                    key=COLD_SOURCE_MEMORY_IDS_FIELD,
                    match=MatchAny(any=sorted(set(memory_ids))),
                )
            ]
        )
        points, _ = get_qdrant().scroll(
            collection_name=COLD_COLLECTION,
            scroll_filter=selector,
            limit=1000,
            with_payload=False,
            with_vectors=False,
        )
        ids = [p.id for p in points]
        if ids:
            get_qdrant().set_payload(
                collection_name=COLD_COLLECTION,
                payload={COLD_STALE_FIELD: True},
                points=ids,
            )
        return len(ids)
    except Exception as e:
        logger.warning(f"Cold stale marking failed: {e}")
        return 0


def _write_memory(item: dict, now_iso: str) -> dict:
    """Dedupe, persist one memory, then soft-supersede the ids it replaces."""
    content = item["content"]
    similar = _active_similar(content)
    top = similar[0] if similar else None
    supersedes = [str(s) for s in (item.get("supersedes") or []) if s]

    # Idempotent write: the same fact is already active, so return its id instead of
    # creating a near-duplicate. Explicit supersede targets are still retired.
    if top and top["score"] >= DEDUPE_EXACT_SIMILARITY:
        requested = str(item.get("episode_id") or "").strip()
        if requested and not top.get("episode_id"):
            old = get_hot_memory().get(top["id"]) or {}
            meta = dict(old.get("metadata") or {})
            meta[EPISODE_FIELD] = requested
            _update_metadata(top["id"], meta)
            top["episode_id"] = requested
        # Rewriting the same fact with supersedes pointing at itself must not
        # self-retire the only active copy.
        targets = [sid for sid in supersedes if sid != top["id"]]
        superseded, failed = _mark_superseded(targets, top["id"], now_iso)
        _mark_cold_stale(superseded)
        return {
            "success": True,
            "memory_id": top["id"],
            "layer": "hot",
            "deduped": True,
            "episode_id": top.get("episode_id") or "",
            "superseded": superseded,
            "warnings": [f"supersede target not found: {sid}" for sid in failed],
            "similar": [s for s in similar if s["score"] >= DEDUPE_NOTICE_SIMILARITY],
        }

    metadata = _build_metadata(item, now_iso)
    if not metadata[EPISODE_FIELD]:
        metadata[EPISODE_FIELD] = _inherit_episode_id(supersedes, similar)
    revision = 1
    for sid in supersedes:
        try:
            old = get_hot_memory().get(sid) or {}
        except Exception:
            old = {}
        old_rev = int((old.get("metadata") or {}).get(REVISION_FIELD, 1) or 1)
        revision = max(revision, old_rev + 1)
    metadata[SUPERSES_FIELD] = supersedes
    metadata[REVISION_FIELD] = revision

    result = get_hot_memory().add(
        content, user_id=DEFAULT_USER_ID, metadata=metadata, infer=False
    )
    rows = result.get("results", [])
    memory_id = rows[0].get("id") if rows else None
    if not memory_id:
        return {
            "success": False,
            "memory_id": None,
            "layer": "hot",
            "error": "Content could not be stored.",
        }
    superseded, failed = _mark_superseded(supersedes, memory_id, now_iso)
    if superseded:
        _mark_cold_stale(superseded)
    return {
        "success": True,
        "memory_id": memory_id,
        "layer": "hot",
        "deduped": False,
        "episode_id": metadata[EPISODE_FIELD],
        "superseded": superseded,
        "warnings": [f"supersede target not found: {sid}" for sid in failed],
        "similar": [s for s in similar if s["score"] >= DEDUPE_NOTICE_SIMILARITY],
    }


async def _check_contradiction(content: str, similar_items: list[dict]) -> dict | None:
    if not similar_items or not llm_client.is_configured():
        return None
    items_text = "\n\n---\n\n".join(
        [
            f"[ID: {item.get('id', 'unknown')}] {item.get('content', item.get('memory', ''))}"
            for item in similar_items[:CONTRADICTION_TOP_K]
        ]
    )
    prompt = prompt_loader.prompt(
        "contradiction",
        items_text=items_text,
        content=content,
    )
    raw = await llm_client.achat(
        prompt,
        temperature=0.1,
        timeout=DEFAULT_TIMEOUT,
        label="Contradiction check failed",
    )
    return llm_client.parse_json(raw)


def _mark_hot_disputed(memory_id: str, disputed_by: str, reason: str) -> bool:
    """Flag a memory as disputed with a metadata-only update.

    The previous rebuild (delete + add) changed the id, breaking superseded_by links
    and every external reference; the rebuild now stays only as a fallback.
    """
    if not get_hot_memory():
        return False
    try:
        old = get_hot_memory().get(memory_id)
        if not old:
            return False
        old_content = old.get("memory", "")
        old_meta = dict(old.get("metadata", {}) or {})
        old_meta.setdefault(PROCESSED_FIELD, 0)
        old_meta["disputed"] = True
        old_meta["disputed_by"] = disputed_by
        old_meta["dispute_reason"] = reason
        if _update_metadata(memory_id, old_meta):
            return True
        logger.warning(
            f"[dispute] in-place update failed for {memory_id}, rebuilding (id will change)"
        )
        get_hot_memory().delete(memory_id)
        try:
            get_hot_memory().add(
                old_content, user_id=DEFAULT_USER_ID, metadata=old_meta
            )
        except Exception as e:
            # A failure on the main path must be logged: previously it silently
            # degraded to an add without metadata, writing metadata=null and letting that
            # memory bypass the processed bit and be rescanned repeatedly
            logger.warning(
                f"[dispute] metadata rebuild failed, falling back to rebuild without it: {e}"
            )
            fallback_meta = {
                PROCESSED_FIELD: old_meta.get(PROCESSED_FIELD, 0),
                "disputed": True,
                "disputed_by": disputed_by,
                "dispute_reason": reason,
            }
            try:
                get_hot_memory().add(
                    old_content, user_id=DEFAULT_USER_ID, metadata=fallback_meta
                )
            except Exception:
                return False
        return True
    except Exception:
        return False


def set_processed_bit(memory_id: str, bit: int, value: int) -> bool:
    """Set/reset a specific bit of a memory's metadata['processed'] (bit0=summarized).

    Via mem0 update it changes only the payload without rebuilding vectors (data=None
    skips embed), and is idempotent; shared by the HTTP endpoint and the MCP tool, a
    single implementation.
    """
    if not get_hot_memory():
        return False
    if not (0 <= bit <= PROCESSED_MAX_BIT):
        raise ValueError(err_bit_out_of_range(bit))
    try:
        old = get_hot_memory().get(memory_id)
        if not old:
            return False
        meta = dict(old.get("metadata", {}) or {})
        flags = int(meta.get(PROCESSED_FIELD, 0) or 0)
        mask = 1 << bit
        meta[PROCESSED_FIELD] = flags | mask if value else flags & ~mask
        get_hot_memory().update(memory_id, metadata=meta)
        return True
    except Exception as e:
        logger.warning(
            f"failed to set processed bit {memory_id} bit={bit} value={value}: {e}"
        )
        return False


def set_memory_status(
    memory_ids: list[str], status: str, superseded_by: str = ""
) -> dict:
    """Set lifecycle status in place. Retiring keeps bit0 and adds the retired bit.

    Restoring to active clears only the retired bit, so an explicit re-activation does
    not cause the summarizer to re-consume content it already digested.
    """
    if status not in (STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_DELETED):
        raise ValueError(f"invalid status: {status}")
    if not get_hot_memory():
        raise RuntimeError(ERR_HOT_LAYER_NOT_READY)
    now_iso = datetime.now().isoformat()
    updated, failed, retired = 0, [], []
    for mid in memory_ids:
        try:
            old = get_hot_memory().get(mid)
        except Exception:
            old = None
        if not old:
            failed.append(mid)
            continue
        meta = dict(old.get("metadata") or {})
        meta[STATUS_FIELD] = status
        flags = int(meta.get(PROCESSED_FIELD, 0) or 0)
        if status == STATUS_ACTIVE:
            meta[PROCESSED_FIELD] = flags & ~PROCESSED_RETIRED_MASK
            meta[SUPERSEDED_BY_FIELD] = ""
            meta[SUPERSEDED_AT_FIELD] = ""
            meta[DELETED_AT_FIELD] = ""
        else:
            meta[PROCESSED_FIELD] = flags | PROCESSED_RETIRED_MASK
            if status == STATUS_SUPERSEDED:
                meta[SUPERSEDED_BY_FIELD] = superseded_by
                meta[SUPERSEDED_AT_FIELD] = now_iso
            else:
                meta[DELETED_AT_FIELD] = now_iso
            retired.append(mid)
        if _update_metadata(mid, meta):
            updated += 1
        else:
            failed.append(mid)
    cold_stale = _mark_cold_stale(retired) if retired else 0
    return {
        "status": status,
        "updated": updated,
        "failed": len(failed),
        "failed_ids": failed,
        "cold_stale": cold_stale,
    }


_pending_contradictions: set[asyncio.Task] = set()

_MAX_CONTRADICTION_TASKS = 5


async def _contradiction_check_hot(content: str, memory_id: str) -> None:
    if not get_hot_memory():
        return
    try:
        # No rerank passed: MEM0_CONFIG does not configure a reranker, so mem0's internal
        # `if rerank and self.reranker` is always false — passing it is a no-op that only
        # adds confusion.
        similar_raw = get_hot_memory().search(
            content,
            top_k=CONTRADICTION_TOP_K,
            threshold=0.0,
            filters={"user_id": DEFAULT_USER_ID},
        )
        similar_list = (
            similar_raw
            if isinstance(similar_raw, list)
            else similar_raw.get("results", [])
        )
        high_sim = [
            r
            for r in similar_list
            if isinstance(r, dict)
            and r.get("score", 0) >= CONTRADICTION_SIMILARITY_THRESHOLD
            and r.get("id") != memory_id
            # Superseded versions are history, not competing facts: comparing against
            # them turns an ordinary update into a false "contradiction".
            and _memory_status(r.get("metadata")) == STATUS_ACTIVE
        ]
        if high_sim:
            result = await _check_contradiction(content, high_sim)
            if result and result.get("relation") == "contradiction":
                cid, reason = result.get("conflicting_id", ""), result.get("reason", "")
                _mark_hot_disputed(memory_id, cid, reason)
                if cid:
                    _mark_hot_disputed(cid, memory_id, reason)
                logger.info(f"Contradiction detected: {memory_id} <-> {cid} ({reason})")
    except Exception as e:
        logger.warning(f"Hot contradiction check failed: {e}")


def spawn_contradiction_check(coro) -> None:
    """Spawn a contradiction check task, limiting concurrent tasks to avoid memory leak."""
    # Clean up completed tasks
    done = {t for t in _pending_contradictions if t.done()}
    _pending_contradictions.difference_update(done)
    if len(_pending_contradictions) >= _MAX_CONTRADICTION_TASKS:
        return  # too many backed up, skip and wait for the next round
    task = asyncio.create_task(coro)
    _pending_contradictions.add(task)


async def contradiction_check_cold(content: str, point_id: str) -> None:
    if not get_qdrant():
        return
    emb = _get_shared_embedder()
    if emb is None:
        return
    try:
        vec = list(emb.embed(content))[0].tolist()
        resp = get_qdrant().query_points(
            collection_name=COLD_COLLECTION, query=vec, limit=CONTRADICTION_TOP_K
        )
        high_sim = [
            r
            for r in resp.points
            if r.score >= CONTRADICTION_SIMILARITY_THRESHOLD and r.id != point_id
        ]
        if high_sim:
            similar = [
                {"id": r.id, "content": r.payload.get("content", "")[:500]}
                for r in high_sim
            ]
            result = await _check_contradiction(content, similar)
            if result and result.get("relation") == "contradiction":
                cid, reason = result.get("conflicting_id", ""), result.get("reason", "")
                for pid in [point_id, cid]:
                    if pid:
                        try:
                            get_qdrant().set_payload(
                                collection_name=COLD_COLLECTION,
                                payload={
                                    "disputed": True,
                                    "disputed_by": cid if pid == point_id else point_id,
                                    "dispute_reason": reason,
                                },
                                points=[pid],
                            )
                        except Exception:
                            pass
                logger.info(
                    f"Cold contradiction detected: {point_id} <-> {cid} ({reason})"
                )
    except Exception as e:
        logger.warning(f"Cold contradiction check failed: {e}")


@_register("remember")
async def _handle_remember(arguments: dict) -> list[TextContent]:
    if not await ensure_hot_memory():
        return [
            TextContent(
                type="text",
                text=dumps(
                    {
                        "error": ERR_HOT_LAYER_NOT_READY,
                        "retry_after_seconds": HOT_INIT_RUNTIME_COOLDOWN,
                    }
                ),
            )
        ]
    now_iso = datetime.now().isoformat()
    result = _write_memory(arguments, now_iso)
    if result.get("memory_id") and not result.get("deduped"):
        spawn_contradiction_check(
            _contradiction_check_hot(arguments["content"], result["memory_id"])
        )
    return [TextContent(type="text", text=dumps(result))]


@_register("remember_batch")
async def _handle_remember_batch(arguments: dict) -> list[TextContent]:
    if not await ensure_hot_memory():
        return [
            TextContent(
                type="text",
                text=dumps(
                    {
                        "error": ERR_HOT_LAYER_NOT_READY,
                        "retry_after_seconds": HOT_INIT_RUNTIME_COOLDOWN,
                    }
                ),
            )
        ]
    memories = arguments.get("memories", [])
    if not memories:
        return [
            TextContent(type="text", text=dumps({"error": "memories array is empty"}))
        ]
    results = []
    now_iso = datetime.now().isoformat()
    for mem in memories:
        try:
            item_result = _write_memory(mem, now_iso)
        except Exception as e:
            item_result = {"success": False, "error": str(e)}
        results.append(item_result)
        if item_result.get("memory_id") and not item_result.get("deduped"):
            spawn_contradiction_check(
                _contradiction_check_hot(mem["content"], item_result["memory_id"])
            )
    return [
        TextContent(
            type="text",
            text=dumps(
                {
                    "results": results,
                    "total": len(results),
                    "succeeded": sum(1 for r in results if r.get("success")),
                    "failed": sum(1 for r in results if not r.get("success")),
                }
            ),
        )
    ]


@_register("recall")
async def _handle_recall(arguments: dict) -> list[TextContent]:
    query = arguments["query"]
    limit = int_arg(arguments, "limit", DEFAULT_RECALL_LIMIT)
    include_archived = arguments.get("include_archived", False)
    include_superseded = bool(arguments.get("include_superseded", False))
    layers = arguments.get("layers")
    if isinstance(layers, str):
        layers = [layers]
    layers = set(layers) if layers else {"hot", "cold"}
    hot_enabled = "hot" in layers
    cold_enabled = "cold" in layers
    # Lazy hot-layer init: skipping this step makes the first recall after a restart
    # silently drop the entire hot layer (`if get_hot_memory()` is false so it is skipped and
    # hot_count is 0). Init failure does not terminate — the cold layer and notes do not
    # depend on the hot layer, so recall should degrade rather than fail.
    await ensure_hot_memory()
    results = []
    if hot_enabled and get_hot_memory():
        try:
            # No rerank passed: MEM0_CONFIG does not configure a reranker, so mem0's
            # internal `if rerank and self.reranker` is always false — passing it is a
            # no-op that only adds confusion.
            # Over-fetch: lifecycle filtering happens in Python so legacy entries
            # without a status field still count as active. A plain top_k would
            # silently shrink the hot-layer result count after the first supersede.
            hot_raw = get_hot_memory().search(
                query,
                filters={"user_id": DEFAULT_USER_ID},
                top_k=max(limit * 3, limit + 5),
                threshold=0.0,
            )
            hot = hot_raw if isinstance(hot_raw, list) else hot_raw.get("results", [])
            for r in hot:
                if isinstance(r, str):
                    continue
                meta = r.get("metadata") or {}
                if not include_archived and meta.get("archived"):
                    continue
                status = _memory_status(meta)
                if status == STATUS_DELETED:
                    continue
                if status == STATUS_SUPERSEDED and not include_superseded:
                    continue
                r["layer"] = "hot"
                r["content"] = r.get("memory", r.get("content", ""))
                r["heat_score"] = meta.get("heat_score", DEFAULT_HEAT_SCORE)
                results.append(_project_hot_result(r))
        except Exception as e:
            logger.warning(f"Hot search failed: {e}")
    cold = (
        search_cold_layer(query, limit, include_archived=include_archived)
        if cold_enabled
        else []
    )
    results.extend(cold)
    results.sort(key=_heat_sort_key, reverse=True)
    query_vec = embed_query(query)
    extended = _expand_hot_episodes(results[:limit], query_vec)
    extended += _expand_cold_episodes(results[:limit], query_vec)
    if extended:
        results.extend(extended)
        results.sort(key=_heat_sort_key, reverse=True)
    # Per-layer counts must use the same basis as `count` — both taken from the
    # truncated list. Previously they were counted over the full `results`, and the sum
    # of the three could reach twice `count` (measured 10+1+9 for count=10), leading the
    # caller to think it got all the hot-layer hits.
    top = results[:limit]
    return [
        TextContent(
            type="text",
            text=dumps(
                {
                    "results": top,
                    "count": len(top),
                    "hot_count": sum(1 for r in top if r.get("layer") == "hot"),
                    "cold_count": sum(1 for r in top if r.get("layer") == "cold"),
                    "notes_count": sum(1 for r in top if r.get("layer") == "notes"),
                }
            ),
        )
    ]


@_register("catch_up")
async def _handle_catch_up(arguments: dict) -> list[TextContent]:
    if not await ensure_hot_memory():
        return [
            TextContent(
                type="text",
                text=dumps(
                    {
                        "error": ERR_HOT_LAYER_NOT_READY,
                        "retry_after_seconds": HOT_INIT_RUNTIME_COOLDOWN,
                    }
                ),
            )
        ]
    since_days = arguments.get("since_days", 3)
    include_superseded = bool(arguments.get("include_superseded", False))
    try:
        data = get_hot_memory().get_all(
            filters={"user_id": DEFAULT_USER_ID}, top_k=5000
        )
        if isinstance(data, dict):
            data = data.get("results", [])
    except Exception as e:
        logger.warning(f"Hot layer catch_up failed: {e}")
        data = []
    since_ts = (datetime.now() - timedelta(days=since_days)).timestamp()
    data = [
        m
        for m in data
        if datetime.fromisoformat(m.get("created_at", "")).timestamp() >= since_ts
        and _memory_status(m.get("metadata")) != STATUS_DELETED
        and (
            include_superseded or _memory_status(m.get("metadata")) != STATUS_SUPERSEDED
        )
    ]
    data = sorted(
        data,
        key=lambda x: datetime.fromisoformat(x.get("created_at", "")).timestamp(),
        reverse=True,
    )
    return [
        TextContent(
            type="text",
            text=dumps(
                {"results": [_project_hot_result(m) for m in data], "count": len(data)}
            ),
        )
    ]


@_register("forget")
async def _handle_forget(arguments: dict) -> list[TextContent]:
    memory_id = arguments.get("memory_id", "")
    point_id = arguments.get("point_id", "")
    layer = arguments.get("layer", "hot")
    if layer in ("hot", "all") and memory_id:
        if await ensure_hot_memory():
            try:
                old = get_hot_memory().get(memory_id)
                meta = dict((old or {}).get("metadata") or {})
                meta[STATUS_FIELD] = STATUS_DELETED
                meta[DELETED_AT_FIELD] = datetime.now().isoformat()
                meta[PROCESSED_FIELD] = (
                    int(meta.get(PROCESSED_FIELD, 0) or 0) | PROCESSED_RETIRED_MASK
                )
                _update_metadata(memory_id, meta)
            except Exception:
                pass
    if layer in ("cold", "all") and point_id and get_qdrant():
        # Soft-retire: cold points have no trash, archived keeps them out of search
        # while preserving the ability to audit what was removed.
        try:
            get_qdrant().set_payload(
                collection_name=COLD_COLLECTION,
                payload={"archived": True, COLD_STALE_FIELD: True},
                points=[point_id],
            )
        except Exception:
            pass
    return [TextContent(type="text", text=dumps({"success": True}))]


@_register("set_memory_processed")
async def _handle_set_memory_processed(arguments: dict) -> list[TextContent]:
    if not await ensure_hot_memory():
        return [
            TextContent(
                type="text",
                text=dumps(
                    {
                        "error": ERR_HOT_LAYER_NOT_READY,
                        "retry_after_seconds": HOT_INIT_RUNTIME_COOLDOWN,
                    }
                ),
            )
        ]
    memory_ids = arguments.get("memory_ids", [])
    bit = int(arguments.get("bit", 0))
    value = int(arguments.get("value", 1))
    if not memory_ids:
        return [TextContent(type="text", text=dumps({"error": ERR_MEMORY_IDS_EMPTY}))]
    if not (0 <= bit <= PROCESSED_MAX_BIT):
        return [
            TextContent(
                type="text",
                text=dumps({"error": err_bit_out_of_range()}),
            )
        ]
    if value not in (0, 1):
        return [
            TextContent(
                type="text",
                text=dumps({"error": ERR_VALUE_INVALID}),
            )
        ]
    results = []
    for mid in memory_ids:
        try:
            ok = await asyncio.to_thread(set_processed_bit, mid, bit, value)
            results.append({"memory_id": mid, "success": ok})
        except Exception as e:
            results.append({"memory_id": mid, "success": False, "error": str(e)})
    return [
        TextContent(
            type="text",
            text=dumps(
                {
                    "results": results,
                    "updated": sum(1 for r in results if r.get("success")),
                    "failed": sum(1 for r in results if not r.get("success")),
                }
            ),
        )
    ]
