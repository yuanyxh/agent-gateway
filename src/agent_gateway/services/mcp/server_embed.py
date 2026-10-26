"""Shared embedder module for the server.

Split out of server.py (2026-08-30, refactor phase 4): fastembed configuration and
the single embedder instance. Shared by the hot layer (server.py's
MEM0_CONFIG/_patch_mem0_fastembed), the cold layer (search_cold_layer) and project
vector search (server_search.py). Code moved as-is, no logic changes.

Note: this module does not import mem0/qdrant — server.py's module-level side-effect
ordering (POSTHOG_DISABLED/MEM0_TELEMETRY + _patch_mem0_fastembed before import mem0)
is unaffected.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict

import numpy as np
from fastembed import TextEmbedding

from agent_gateway.core.log_utils import setup_logging

# ---------------------------------------------------------------------------
# Configuration (same as pre-split server.py; injected by Swift MCPProcessManager via env)
# ---------------------------------------------------------------------------

EMBEDDER_MODEL = os.environ.get(
    "EMBEDDER_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
FASTEMBED_CACHE_DIR = os.environ.get(
    "FASTEMBED_CACHE_PATH", os.path.expanduser("~/.cache/fastembed_cache")
)
os.environ.setdefault("FASTEMBED_CACHE_PATH", FASTEMBED_CACHE_DIR)

logger = setup_logging("server")

#: Fallback dims value. **A fallback, not a default**: only used when the model
#: cannot be found in the fastembed metadata. 384 = the dims of `all-MiniLM-L6-v2`,
#: the value hardcoded before this mechanism was introduced (so the default path
#: is byte-for-byte unchanged).
_FALLBACK_EMBEDDER_DIMS = 384

#: Field names of the fastembed metadata (`TextEmbedding.list_supported_models()`
#: entries). They are single-source constants because they are **external library
#: field names**: when the library renames them, it fails loudly here in one place,
#: instead of every lookup silently returning nothing and falling back across the board.
_META_MODEL_FIELD = "model"
_META_DIM_FIELD = "dim"


def resolve_embedder_dims(
    model_name: str, *, fallback: int = _FALLBACK_EMBEDDER_DIMS
) -> int:
    """Resolve a model's vector dims from the fastembed metadata.

    Dims are an **intrinsic property of the model**, so they are read only from
    fastembed's own metadata: having Swift inject them would mean building a second
    "model name → dims" mapping, which is bound to drift from the real value (swap
    the model, forget to update the mapping → collection dims no longer match vector
    dims). A missing entry / unusable field / lookup exception all fall back to
    `fallback` and log a warning.

    Pure metadata read: no ONNX model is loaded and no network is used (measured at
    ~30 entries, the cost is negligible).
    """
    try:
        for entry in TextEmbedding.list_supported_models():
            if entry.get(_META_MODEL_FIELD) != model_name:
                continue
            dim = entry.get(_META_DIM_FIELD)
            if isinstance(dim, int) and dim > 0:
                return dim
            logger.warning(
                f"fastembed metadata for {model_name!r} has unusable "
                f"{_META_DIM_FIELD}={dim!r}; falling back to {fallback} dims"
            )
            return fallback
    except Exception as e:
        logger.warning(
            f"fastembed metadata lookup failed ({type(e).__name__}: {e}); "
            f"falling back to {fallback} dims"
        )
        return fallback

    logger.warning(
        f"Model {model_name!r} is not in fastembed supported models; "
        f"falling back to {fallback} dims. If this model's dims are not {fallback}, "
        "vector search will fail on a dims mismatch — check the EMBEDDER_MODEL spelling first"
    )
    return fallback


#: Vector dims of the current embedder (resolved from model metadata, see `resolve_embedder_dims`).
EMBEDDER_DIMS = resolve_embedder_dims(EMBEDDER_MODEL)


def describe_dim_mismatch(existing_dims: int | None, expected_dims: int) -> str | None:
    """Remediation advice when the collection dims disagree with the current embedder
    dims; returns None when they match or cannot be determined.

    Why an explicit error is required: **collection dims are fixed at first creation**.
    After swapping the model (dims change) the old vectors are not comparable with the
    new query vectors, and Qdrant raises a dimension error on query — while
    `search_cold_layer` swallows the exception into a single warning, presenting as
    "cold-layer search silently fails, users just see fewer results". Calling it out at
    startup is far cheaper than tracing back from "the search results look wrong".
    """
    if existing_dims is None or existing_dims == expected_dims:
        return None
    return (
        f"Cold-layer collection dims mismatch: collection has {existing_dims} dims, the "
        f"current embedder {EMBEDDER_MODEL!r} has {expected_dims} dims. Remediation "
        f"(collection dims are immutable): revert to the original model "
        f"(EMBEDDER_MODEL / config.yaml mcp.embedderModel), or delete the cold-layer "
        f"collection so it is recreated with the new dims and all vectors are rescanned"
    )


_shared_embedder = None


def _get_shared_embedder():
    """Single embedder instance, shared by the cold layer and project vector search to
    avoid reloading the ONNX model.

    Optimizations: enable_cpu_mem_arena=False disables ONNX Runtime's CPU arena
    pre-allocation (which mallocs a ~1GB block by default); threads=1 limits the thread
    count; CPUExecutionProvider avoids the extra memory of the CoreML/Azure providers.
    """
    global _shared_embedder
    if _shared_embedder is None:
        try:
            _shared_embedder = TextEmbedding(
                model_name=EMBEDDER_MODEL,
                cache_dir=FASTEMBED_CACHE_DIR,
                threads=1,
                providers=["CPUExecutionProvider"],
                enable_cpu_mem_arena=False,
            )
        except Exception as e:
            logger.warning(f"Shared embedder init failed (retry on next call): {e}")
    return _shared_embedder


# ---------------------------------------------------------------------------
# Query vector cache
# ---------------------------------------------------------------------------

#: Query-embedding LRU capacity (entries). 0 disables caching.
_QUERY_CACHE_SIZE = int(os.environ.get("EMBED_QUERY_CACHE_SIZE", "64"))
_query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
_query_cache_lock = threading.Lock()


def embed_query(text: str) -> np.ndarray | None:
    """Embed a single query string, with a small LRU cache. Returns None when the
    embedder is unavailable.

    Within one agent turn, several search tools are often called concurrently with the
    same query (recall / project_notes / project_search are all told in the prompt to
    be called "in parallel"), and each used to embed the query independently. Query
    embedding is a pure function, ~45ms per call, so the cache eliminates this
    duplicated work. Note that mem0 internally uses its own embedder and does not go
    through this function, so that path cannot be shared.

    Returns a float32 ndarray. **Read-only for callers** — the object itself is stored
    in the cache.
    """
    emb = _get_shared_embedder()
    if emb is None:
        return None

    if _QUERY_CACHE_SIZE > 0:
        with _query_cache_lock:
            hit = _query_cache.get(text)
            if hit is not None:
                _query_cache.move_to_end(text)
                return hit

    vec = np.asarray(list(emb.embed([text]))[0], dtype=np.float32)

    if _QUERY_CACHE_SIZE > 0:
        with _query_cache_lock:
            _query_cache[text] = vec
            _query_cache.move_to_end(text)
            while len(_query_cache) > _QUERY_CACHE_SIZE:
                _query_cache.popitem(last=False)
    return vec
