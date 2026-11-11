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
from agent_gateway.core.memory_lifecycle import (
    OWNER_AGENT_FIELD,
    VISIBILITY_FIELD,
    visible_to,
)
from agent_gateway.services.memory.dimensions import (
    GROUPING_DIMENSIONS,
    INTERACTION_TYPES,
    LANGUAGE_TYPES,
    MEMORY_ROLES,
    MEMORY_TASK_TYPES,
    dimension_text,
    enum_value,
    language_of,
)
from agent_gateway.services.mcp.server_constants import (
    AI_API_KEY,
    ARCHIVED_AT_FIELD,
    COLD_COLLECTION,
    COLD_SOURCE_MEMORY_IDS_FIELD,
    COLD_STALE_FIELD,
    CONTRADICTION_SIMILARITY_MAX,
    CONTRADICTION_SIMILARITY_MIN,
    CONTRADICTION_TOP_K,
    DEDUPE_EXACT_SIMILARITY,
    DEDUPE_NOTICE_SIMILARITY,
    DEDUPE_TOP_K,
    DEFAULT_HEAT_SCORE,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_ID,
    DELETED_AT_FIELD,
    DISPUTED_AT_FIELD,
    EPISODE_COLD_LOOKUP_LIMIT,
    EPISODE_COLD_MIN_SIMILARITY,
    EPISODE_FIELD,
    EPISODE_INHERIT_SIMILARITY,
    EPISODE_MAX_GROUP_SIZE,
    ERR_HOT_LAYER_NOT_READY,
    ERR_MEMORY_IDS_EMPTY,
    ERR_VALUE_INVALID,
    HOT_INIT_MAX_RETRIES,
    HOT_INIT_MAX_RUNTIME_FAILURES,
    HOT_INIT_RETRY_DELAY,
    HOT_INIT_RUNTIME_COOLDOWN,
    MEM0_QDRANT_PATH,
    PROCESSED_FIELD,
    PROCESSED_MAX_BIT,
    PROCESSED_RETIRED_MASK,
    REVISION_FIELD,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
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
    EMBEDDER_SPEC,
    PooledEmbedder,
    embedder_session,
)
from agent_gateway.services.mcp.server_state import (
    get_hot_memory,
    get_qdrant,
    set_hot_memory,
)
from agent_gateway.services.mcp.server_tool_schema import (
    DEFAULT_CATCH_UP_LIMIT,
    DEFAULT_CATCH_UP_MAX_CHARS,
    DEFAULT_RECALL_LIMIT,
    DEFAULT_RECALL_MAX_CHARS,
)
from agent_gateway.services.mcp.server_utils import dumps, int_arg
from agent_gateway.services.memory import link as memory_link
from agent_gateway.services.memory import store as memory_store
from agent_gateway.services.memory import strength as memory_strength
from agent_gateway.services.retrieval.arms.recall import (
    ColdMemoryArm,
    HotMemoryArm,
    NotesArm,
    expand_episodes,
)
from agent_gateway.services.retrieval.arms.recall import (  # noqa: F401
    dense_vector as _dense_vector,
    dot as _dot,
    project_hot_result as _project_hot_result,
)
from agent_gateway.core.config import memory_max_content_chars, rank_signal_weights
from agent_gateway.services.retrieval.fusion.score import SignalWeightedFusion
from agent_gateway.services.retrieval.gate.relevance import LayerPresenceGate
from agent_gateway.services.retrieval.gate.thresholds import floors_for
from agent_gateway.services.retrieval.pipeline import Pipeline, Stage
from agent_gateway.services.retrieval.rerank import get_reranker
from agent_gateway.services.retrieval.text.analyzer import detect_language, split_terms
from agent_gateway.services.retrieval.types import Query

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

        def _optimized_init(self, config=None):
            # Call EmbeddingBase.__init__ directly to set config, skipping the
            # unoptimized TextEmbedding creation in FastEmbedEmbedding's original init
            EmbeddingBase.__init__(self, config)
            self.config.model = self.config.model or EMBEDDER_MODEL
            # Borrow the shared model so mem0 does not hold a second ~1GB copy.
            self.dense_model = PooledEmbedder(EMBEDDER_DIMS)
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


_note_needles = split_terms


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


def _importance(value) -> int:
    """Clamp to the published 1-10 scale; a missing value takes the midpoint."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(number, 10))


def _level(value) -> int:
    """Consolidation depth; zero means a first-level summary."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _dimensions_of(src: dict) -> dict[str, str]:
    """Normalized dimension values; the single place a write is validated."""
    return {
        "project": dimension_text(src.get("project")),
        "subsystem": dimension_text(src.get("subsystem")),
        "task": dimension_text(src.get("task")),
        "task_type": enum_value(src.get("task_type"), MEMORY_TASK_TYPES),
        "ticket": dimension_text(src.get("ticket")),
        "language": language_of(src.get("language"), src.get("content")),
        "interaction": enum_value(src.get("interaction"), INTERACTION_TYPES),
        "session_id": dimension_text(src.get("session_id")),
        "narrative_role": enum_value(src.get("role"), MEMORY_ROLES),
    }


def _build_metadata(src: dict, now_iso: str) -> dict:
    return {
        # mem0 owns the agent scope; identity keys in metadata are dropped, so the
        # owner is mirrored under a non-identity key for read-side filtering.
        OWNER_AGENT_FIELD: str(src.get("agent_id") or "unknown"),
        VISIBILITY_FIELD: src.get("visibility", "all"),
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
        # Content and context live in separate fields so a response never echoes the
        # embedded text back as if it were the fact.
        "fact": str(src.get("content") or ""),
        "context": str(src.get("context") or ""),
        # Dimensions are explicit values; the service never infers them from text.
        **_dimensions_of(src),
        "type": str(src.get("type") or ""),
        "source_files": src.get("source_files") or [],
        "source_url": str(src.get("source_url") or ""),
        "importance": _importance(src.get("importance")),
        "protected": bool(src.get("protected", False)),
        "level": _level(src.get("level")),
        # Set by the consolidation pass so its own output never feeds the next round.
        "generated_by": str(src.get("generated_by") or ""),
    }


def _memory_status(meta: dict | None) -> str:
    """Lifecycle status; legacy entries without the field count as active."""
    return str((meta or {}).get(STATUS_FIELD) or STATUS_ACTIVE)


def _active_similar(
    content: str, agent_id: str = "unknown", visibility: str = "all"
) -> list[dict]:
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
        meta = r.get("metadata") or {}
        # Dedupe only within the same scope: folding a private write into a public
        # row (or the reverse) would silently change who can read the fact.
        row_visibility = str(meta.get(VISIBILITY_FIELD) or "all")
        if row_visibility != visibility:
            continue
        if visibility != "all":
            row_owner = str(meta.get(OWNER_AGENT_FIELD) or "unknown")
            if row_owner != agent_id:
                continue
        similar.append(
            {
                "id": r["id"],
                "content": str(r.get("memory") or r.get("content") or "")[:120],
                "score": round(float(score), 3),
                "episode_id": str(meta.get(EPISODE_FIELD) or "").strip(),
                "fact": str(meta.get("fact") or ""),
                "context": str(meta.get("context") or ""),
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


def _touch_hot_access(results: list[dict]) -> None:
    """Bump access counters for the rows a caller actually saw.

    Keep this off the ranking path: the value is read on the next query.
    """
    hot = get_hot_memory()
    if hot is None or not results:
        return
    now = datetime.now().isoformat()
    for row in results:
        if row.get("layer") != "hot":
            continue
        memory_id = str(row.get("id") or "")
        if not memory_id:
            continue
        try:
            current = hot.get(memory_id) or {}
            meta = dict(current.get("metadata") or {})
            if not meta:
                continue
            meta["access_count"] = int(meta.get("access_count", 0) or 0) + 1
            meta["last_accessed_at"] = now
            _update_metadata(memory_id, meta)
        except Exception as e:  # noqa: BLE001 - never fail a recall on bookkeeping
            logger.warning(f"[recall] access touch failed for {memory_id}: {e}")


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
    if done:
        memory_link.record_hot_status(done, STATUS_SUPERSEDED, superseded_by=new_id)
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


def _embed_text(item: dict) -> str:
    """The text actually embedded: the fact plus its context, so context is searchable."""
    fact = str(item.get("content") or "")
    context = str(item.get("context") or "").strip()
    return f"{fact}\nContext: {context}" if context else fact


def _content_warning(content: str) -> str:
    limit = memory_max_content_chars()
    if limit and len(content) > limit:
        return f"content is {len(content)} chars; keep one fact per memory"
    return ""


def _write_memory(item: dict, now_iso: str, db_path: str | None = None) -> dict:
    """Dedupe, persist one memory, then soft-supersede the ids it replaces.

    `db_path` only targets the graph mirror; the hot layer has a single store.
    """
    content = item["content"]
    agent_id = str(item.get("agent_id") or "unknown")
    visibility = str(item.get("visibility") or "all")
    if visibility == "agent-specific" and agent_id == "unknown":
        return {
            "success": False,
            "memory_id": None,
            "layer": "hot",
            "error": "agent-specific memory requires a non-unknown agent_id.",
        }
    content_warning = _content_warning(content)
    embed_text = _embed_text(item)
    similar = _active_similar(embed_text, agent_id, visibility)
    top = similar[0] if similar else None
    context = str(item.get("context") or "").strip()
    supersedes = [str(s) for s in (item.get("supersedes") or []) if s]
    # The same fact written for a different situation is a second memory, not a
    # duplicate: the dedupe key is the fact plus its context.
    different_context = bool(
        top and top.get("fact") == content and top.get("context", "") != context
    )

    # Idempotent write: the same fact is already active, so return its id instead of
    # creating a near-duplicate. Explicit supersede targets are still retired.
    if top and top["score"] >= DEDUPE_EXACT_SIMILARITY and not different_context:
        old = get_hot_memory().get(top["id"]) or {}
        meta = dict(old.get("metadata") or {})
        changed = False
        requested = str(item.get("episode_id") or "").strip()
        if requested and not top.get("episode_id"):
            meta[EPISODE_FIELD] = requested
            top["episode_id"] = requested
            changed = True
        # A repeated consolidation summary must keep its marker; without it the next
        # pass sees a plain memory and summarizes the same cluster again.
        if item.get("generated_by"):
            meta["generated_by"] = str(item["generated_by"])
            changed = True
        merged_tags = list(meta.get("tags") or [])
        for tag in item.get("tags") or []:
            if tag not in merged_tags:
                merged_tags.append(tag)
                changed = True
        if merged_tags:
            meta["tags"] = merged_tags
        for key in ("fact", "context", "type", "source_files", "source_url"):
            if item.get(key) and not meta.get(key):
                meta[key] = item[key]
                changed = True
        # A repeated write must still attach dimensions the stored copy lacks;
        # otherwise a later recall filter cannot find the fact it just re-sent.
        for key, value in _dimensions_of(item).items():
            if value and not meta.get(key):
                meta[key] = value
                changed = True
        if item.get("importance") is not None:
            importance = _importance(item["importance"])
            # A system-generated write must never pull a user's rating down; an
            # explicit user write still can.
            if item.get("generated_by"):
                importance = max(importance, _importance(meta.get("importance")))
            meta["importance"] = importance
            changed = True
        if item.get("protected"):
            meta["protected"] = True
            changed = True
        # A folded summary that collapses onto an existing node must keep its depth,
        # or the next pass reads level 0 and folds it again forever.
        if item.get("level") is not None:
            meta["level"] = max(_level(meta.get("level")), _level(item["level"]))
            changed = True
        if changed:
            _update_metadata(top["id"], meta)
        # Rewriting the same fact with supersedes pointing at itself must not
        # self-retire the only active copy.
        targets = [sid for sid in supersedes if sid != top["id"]]
        superseded, failed = _mark_superseded(targets, top["id"], now_iso)
        _mark_cold_stale(superseded)
        # Resending the merged metadata keeps level/importance/protected on the node;
        # the graph upsert replaces metadata wholesale.
        memory_link.record_hot_memory(
            top["id"],
            content,
            {
                **meta,
                "episode_id": top.get("episode_id") or "",
                "status": STATUS_ACTIVE,
            },
            supersedes=tuple(targets),
            created_at=now_iso,
            db_path=db_path,
        )
        return {
            "success": True,
            "memory_id": top["id"],
            "layer": "hot",
            "deduped": True,
            "episode_id": top.get("episode_id") or "",
            "superseded": superseded,
            "warnings": [f"supersede target not found: {sid}" for sid in failed]
            + ([content_warning] if content_warning else []),
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
        embed_text,
        user_id=DEFAULT_USER_ID,
        agent_id=agent_id,
        metadata=metadata,
        infer=False,
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
    memory_link.record_hot_memory(
        memory_id,
        content,
        metadata,
        supersedes=tuple(supersedes),
        created_at=now_iso,
        db_path=db_path,
    )
    return {
        "success": True,
        "memory_id": memory_id,
        "layer": "hot",
        "deduped": False,
        "episode_id": metadata[EPISODE_FIELD],
        "superseded": superseded,
        "warnings": [f"supersede target not found: {sid}" for sid in failed]
        + ([content_warning] if content_warning else []),
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
        old_meta[DISPUTED_AT_FIELD] = datetime.now().isoformat()
        if _update_metadata(memory_id, old_meta):
            memory_link.record_dispute(memory_id, disputed_by, reason)
            return True
        logger.warning(
            f"[dispute] in-place update failed for {memory_id}, rebuilding (id will change)"
        )
        agent_scope = str(
            old.get("agent_id") or old_meta.get(OWNER_AGENT_FIELD) or "unknown"
        )
        get_hot_memory().delete(memory_id)
        try:
            get_hot_memory().add(
                old_content,
                user_id=DEFAULT_USER_ID,
                agent_id=agent_scope,
                metadata=old_meta,
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
            for key in (VISIBILITY_FIELD, OWNER_AGENT_FIELD):
                if key in old_meta:
                    fallback_meta[key] = old_meta[key]
            try:
                get_hot_memory().add(
                    old_content,
                    user_id=DEFAULT_USER_ID,
                    agent_id=agent_scope,
                    metadata=fallback_meta,
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
    memory_ids: list[str],
    status: str,
    superseded_by: str = "",
    *,
    db_path: str | None = None,
) -> dict:
    """Set lifecycle status in place. Retiring keeps bit0 and adds the retired bit.

    Restoring to active clears only the retired bit, so an explicit re-activation does
    not cause the summarizer to re-consume content it already digested.
    """
    if status not in (
        STATUS_ACTIVE,
        STATUS_SUPERSEDED,
        STATUS_ARCHIVED,
        STATUS_DELETED,
    ):
        raise ValueError(f"invalid status: {status}")
    if not get_hot_memory():
        raise RuntimeError(ERR_HOT_LAYER_NOT_READY)
    now_iso = datetime.now().isoformat()
    updated, failed, retired, updated_ids = 0, [], [], []
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
            meta[ARCHIVED_AT_FIELD] = ""
            meta["archived"] = False
        else:
            meta[PROCESSED_FIELD] = flags | PROCESSED_RETIRED_MASK
            if status == STATUS_SUPERSEDED:
                meta[SUPERSEDED_BY_FIELD] = superseded_by
                meta[SUPERSEDED_AT_FIELD] = now_iso
            elif status == STATUS_ARCHIVED:
                meta[ARCHIVED_AT_FIELD] = now_iso
                meta["archived"] = True
            else:
                meta[DELETED_AT_FIELD] = now_iso
                meta["archived"] = True
            retired.append(mid)
        if _update_metadata(mid, meta):
            updated += 1
            updated_ids.append(mid)
        else:
            failed.append(mid)
    memory_link.record_hot_status(
        updated_ids, status, superseded_by=superseded_by, db_path=db_path
    )
    cold_stale = _mark_cold_stale(retired) if retired else 0
    return {
        "status": status,
        "updated": updated,
        "failed": len(failed),
        "failed_ids": failed,
        "cold_stale": cold_stale,
    }


def _memory_created_at(memory_id: str) -> str:
    try:
        row = get_hot_memory().get(memory_id) or {}
    except Exception:
        return ""
    return str(row.get("created_at") or "")


def _supersede_older(current_id: str, other_id: str) -> None:
    """Retire the older side of a confirmed contradiction; never both."""
    if not current_id or not other_id or current_id == other_id:
        return
    created = _memory_created_at(current_id)
    other_created = _memory_created_at(other_id)
    if not created or not other_created:
        return
    older, newer = (
        (other_id, current_id) if created >= other_created else (current_id, other_id)
    )
    done, _ = _mark_superseded([older], newer, datetime.now().isoformat())
    if done:
        _mark_cold_stale(done)


_pending_contradictions: set[asyncio.Task] = set()

_MAX_CONTRADICTION_TASKS = 5


def _contradiction_band(rows: list, memory_id: str) -> list[dict]:
    """Active neighbours inside the band: closer is a duplicate, farther is unrelated."""
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("id") != memory_id
        and _memory_status(row.get("metadata")) == STATUS_ACTIVE
        and CONTRADICTION_SIMILARITY_MIN
        <= float(row.get("score") or 0)
        <= CONTRADICTION_SIMILARITY_MAX
    ]


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
        band = _contradiction_band(similar_list, memory_id)
        if band:
            result = await _check_contradiction(content, band)
            if result and result.get("relation") == "contradiction":
                cid, reason = result.get("conflicting_id", ""), result.get("reason", "")
                _mark_hot_disputed(memory_id, cid, reason)
                if cid:
                    _mark_hot_disputed(cid, memory_id, reason)
                    _supersede_older(memory_id, cid)
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
    try:
        with embedder_session() as emb:
            if emb is None:
                return
            vec = list(emb.embed(content))[0].tolist()
        resp = get_qdrant().query_points(
            collection_name=COLD_COLLECTION, query=vec, limit=CONTRADICTION_TOP_K
        )
        band = [
            r
            for r in resp.points
            if r.id != point_id
            and CONTRADICTION_SIMILARITY_MIN
            <= float(r.score or 0)
            <= CONTRADICTION_SIMILARITY_MAX
        ]
        if band:
            similar = [
                {"id": r.id, "content": r.payload.get("content", "")} for r in band
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
                                    DISPUTED_AT_FIELD: datetime.now().isoformat(),
                                },
                                points=[pid],
                            )
                        except Exception:
                            pass
                left = memory_store.find_node_by_point_id(str(point_id))
                right = memory_store.find_node_by_point_id(str(cid))
                if left and right:
                    memory_link.record_dispute(
                        left["node_id"], right["node_id"], reason
                    )
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
            _contradiction_check_hot(_embed_text(arguments), result["memory_id"])
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
    batch_defaults = {
        key: str(arguments.get(key) or "").strip()
        for key in (
            "episode_id",
            "project",
            "subsystem",
            "task",
            "task_type",
            "ticket",
            "language",
            "interaction",
            "session_id",
            "role",
        )
    }
    batch_defaults = {key: value for key, value in batch_defaults.items() if value}
    for mem in memories:
        item = mem
        for key, value in batch_defaults.items():
            if not str(mem.get(key) or "").strip():
                item = {**item, key: value}
        try:
            item_result = _write_memory(item, now_iso)
        except Exception as e:
            item_result = {"success": False, "error": str(e)}
        results.append(item_result)
        if item_result.get("memory_id") and not item_result.get("deduped"):
            spawn_contradiction_check(
                _contradiction_check_hot(_embed_text(item), item_result["memory_id"])
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


def _recall_keywords(arguments: dict) -> list[str]:
    """Keyword terms from the request; a bare string is accepted as one term."""
    raw = arguments.get("keywords")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        # Older callers sent the whole query as `query`; keep them working.
        legacy = str(arguments.get("query") or "").strip()
        return [legacy] if legacy else []
    return [str(term).strip() for term in raw if str(term).strip()]


def _recall_dimensions(arguments: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Explicit dimension values, normalized the same way a write normalizes them."""
    filters = {
        "project": dimension_text(arguments.get("project")),
        "ticket": dimension_text(arguments.get("ticket")),
        "language": enum_value(arguments.get("language"), LANGUAGE_TYPES),
    }
    boost = {
        "subsystem": dimension_text(arguments.get("subsystem")),
        "task": dimension_text(arguments.get("task")),
        "task_type": enum_value(arguments.get("task_type"), MEMORY_TASK_TYPES),
        "narrative_role": enum_value(arguments.get("role"), MEMORY_ROLES),
        "session_id": dimension_text(arguments.get("session_id")),
    }
    return (
        {key: value for key, value in filters.items() if value},
        {key: value for key, value in boost.items() if value},
    )


@_register("recall")
async def _handle_recall(arguments: dict) -> list[TextContent]:
    keywords = _recall_keywords(arguments)
    semantic = str(arguments.get("query") or "").strip()
    text = " ".join(keywords) or semantic
    if not text:
        return [
            TextContent(
                type="text",
                text=dumps({"error": "keywords must contain at least one term"}),
            )
        ]
    limit = int_arg(arguments, "limit", DEFAULT_RECALL_LIMIT)
    max_chars = int_arg(arguments, "max_chars", DEFAULT_RECALL_MAX_CHARS)
    if max_chars < 0:
        max_chars = DEFAULT_RECALL_MAX_CHARS
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
    dimension_filters, dimension_boost = _recall_dimensions(arguments)
    request = Query(
        text=text,
        language=detect_language(text),
        root_text=text,
        semantic_text=semantic,
        filters=dimension_filters,
        boost=dimension_boost,
        expose=GROUPING_DIMENSIONS,
    )

    def _run():
        # Build and run on one worker: the first get_reranker() loads ONNX.
        pipeline = _build_recall_pipeline(
            hot_enabled=hot_enabled,
            cold_enabled=cold_enabled,
            include_archived=include_archived,
            include_superseded=include_superseded,
        )
        return pipeline.run_query(
            request,
            limit,
            {
                "reader_agent_id": str(arguments.get("agent_id") or "unknown"),
                "include_archived": include_archived,
            },
            "recall",
        )

    out = await asyncio.to_thread(_run)
    top = out["results"]
    related = out["related"]
    # The budget is spent on the primary results first. Once they are cut, related
    # context is dropped rather than crowding out a better-scored memory; when they
    # fit, related rows may use whatever is left.
    top, top_truncated, used = _bound_catch_up_results(top, max_chars)
    if top_truncated:
        related = []
    elif max_chars > 0:
        remaining = max_chars - used
        related, _, _ = (
            _bound_catch_up_results(related, remaining)
            if remaining > 0
            else ([], False, 0)
        )
    # After truncation: only the rows that survived are ones the caller saw.
    await asyncio.to_thread(_touch_hot_access, top)
    try:
        await asyncio.to_thread(memory_strength.reinforce_after_recall, top)
        await asyncio.to_thread(memory_strength.coactivate_recalled, top)
    except Exception as e:  # noqa: BLE001 - reinforcement must not drop recalled rows
        logger.warning(f"[memory] reinforce after recall failed: {e}")
    status = out["status"]
    filtered = int(out.get("filtered") or 0)
    fallback_count = int(out.get("fallback_count") or 0)
    payload = {
        "status": status,
        "results": top,
        "related": related,
        "count": len(top),
        # Rows a dimension filter removed, so an empty result is explainable.
        "filtered": filtered,
        # Excluded rows that returned only because the strict result was short.
        "fallback_count": fallback_count,
        "errors": out.get("errors") or [],
        # The pipeline already writes this trace; returning it lets callers and
        # adoption analysis join a result back to its arms.
        "trace_id": out.get("trace_id") or "",
    }
    if fallback_count:
        payload["hint"] = (
            f"{fallback_count} result(s) miss the dimension filter; each carries "
            "filter_fallback with the failed dimensions."
        )
    elif filtered and status in ("weak_match", "no_match"):
        payload["hint"] = (
            f"{filtered} row(s) failed the dimension filter; rows without that "
            "field, including legacy and notes results, never match."
        )
    elif status == "weak_match":
        payload["hint"] = (
            "No semantic memory cleared the relevance floor; results are lexical or related context."
        )
    elif status == "no_match":
        payload["hint"] = (
            "No memory cleared the relevance floor; try a more specific topic."
        )
    return [TextContent(type="text", text=dumps(payload))]


def _recall_rerank_enabled() -> bool:
    """On by default after the 2026-11-09 known-item comparison.

    100 samples, limit 10: hit@1 0.90 -> 0.97, mrr 0.932 -> 0.975,
    chars/query 1621.5 -> 1238.7, p95 458 -> 2552 ms (budget 3000).
    `RECALL_RERANK=0` disables it without a code change.
    """
    value = os.environ.get("RECALL_RERANK", "1").strip().lower()
    return value not in ("", "0", "false", "off")


def _build_recall_pipeline(
    *,
    hot_enabled: bool = True,
    cold_enabled: bool = True,
    include_archived: bool = False,
    include_superseded: bool = False,
) -> Pipeline:
    # Floors are measured for the active embedder; a constant calibrated on one
    # model silently discards another model's hits.
    floors = floors_for(EMBEDDER_SPEC.tag)
    arms = []
    # Hot and cold are one tier: both score against the same embedding, so their
    # hits are comparable and must compete on score. Splitting them let a short
    # hot result starve a better cold hit (measured: cold held the 0.764 match
    # while hot's 0.56 filled the limit).
    memory_arms = []
    if hot_enabled:
        memory_arms.append(
            HotMemoryArm(
                min_score=floors["hot"],
                include_archived=include_archived,
                include_superseded=include_superseded,
            )
        )
    if cold_enabled:
        memory_arms.append(
            ColdMemoryArm(
                min_score=floors["cold"],
                include_archived=include_archived,
            )
        )
    arms.extend(memory_arms)
    stages: list[Stage] = []
    if memory_arms:
        stages.append(Stage(name="memory", arms=memory_arms))
    notes_arm = NotesArm(
        min_coverage=floors["notes_coverage"],
        min_semantic=floors["notes_semantic"],
    )
    arms.append(notes_arm)
    # Notes stay a last resort: entered only when the memory tier found nothing.
    stages.append(Stage(name="notes", arms=[notes_arm], only_if_empty=True))
    return Pipeline(
        name="recall",
        arms=arms,
        fusion=SignalWeightedFusion(**rank_signal_weights()),
        gate=LayerPresenceGate(),
        reranker=get_reranker() if _recall_rerank_enabled() else None,
        expander=expand_episodes,
        stages=stages,
    )


def _bound_catch_up_results(
    results: list[dict], max_chars: int
) -> tuple[list[dict], bool, int]:
    """Keep the newest results within a character budget.

    The first item is always returned; if it alone exceeds the budget, only its
    content is truncated so pagination can still advance. Returns the kept rows,
    whether anything was dropped, and the characters actually used.
    """
    if max_chars <= 0:
        return results, False, 0
    kept: list[dict] = []
    used = 0
    for item in results:
        encoded = dumps(item)
        if used + len(encoded) <= max_chars:
            kept.append(item)
            used += len(encoded)
            continue
        if not kept:
            trimmed = dict(item)
            content = trimmed.get("content")
            if isinstance(content, str):
                keep = len(content)
                while keep > 0:
                    trimmed["content"] = content[:keep]
                    trimmed["content_truncated"] = True
                    if len(dumps(trimmed)) <= max_chars:
                        break
                    keep //= 2
                if len(dumps(trimmed)) > max_chars:
                    trimmed["content"] = ""
            kept.append(trimmed)
            used = len(dumps(trimmed))
        return kept, True, used
    return kept, False, used


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
    agent_id = str(arguments.get("agent_id") or "unknown")
    limit = max(1, min(int_arg(arguments, "limit", DEFAULT_CATCH_UP_LIMIT), 500))
    offset = max(0, int_arg(arguments, "offset", 0))
    max_chars = int_arg(arguments, "max_chars", DEFAULT_CATCH_UP_MAX_CHARS)
    if max_chars < 0:
        max_chars = DEFAULT_CATCH_UP_MAX_CHARS
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
        and visible_to(m.get("metadata"), agent_id)
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
    total = len(data)
    window = data[offset : offset + limit]
    results, char_truncated, _ = _bound_catch_up_results(
        [_project_hot_result(m) for m in window], max_chars
    )
    payload = {
        "results": results,
        "count": total,
        "returned": len(results),
        "offset": offset,
        "limit": limit,
        "truncated": char_truncated or offset + len(results) < total,
    }
    if char_truncated:
        payload["truncated_by"] = "chars"
    elif offset + len(results) < total:
        payload["truncated_by"] = "limit"
    if offset + len(results) < total:
        payload["has_more"] = True
        payload["next_offset"] = offset + len(results)
    return [TextContent(type="text", text=dumps(payload))]


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
                if _update_metadata(memory_id, meta):
                    memory_link.record_hot_status([memory_id], STATUS_DELETED)
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
