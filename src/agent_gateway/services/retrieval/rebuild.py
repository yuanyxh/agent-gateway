"""Re-embed existing indexes with a different model.

Runs inside the server process because a local Qdrant path holds an exclusive
lock: a standalone script cannot open the live store. The old indexes are never
modified, so switching back is a config change.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.retrieval.config import index_name, model_tag

logger = setup_logging("server")

BATCH = 128
#: Longer text costs time linearly with no extra signal for a memory summary.
MAX_CHARS = 2000
#: Measured on M2 Pro: 131ms/text at 1 thread, 35ms/text at 4.
ENCODER_THREADS = 4
_STATE: dict[str, Any] = {
    "running": False,
    "target": "",
    "model": "",
    "done": 0,
    "total": 0,
    "error": "",
    "stage": "",
}
_LOCK = threading.Lock()


def status() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def _set(**kwargs: Any) -> None:
    with _LOCK:
        _STATE.update(kwargs)


def _encoder(model_id: str, cache_dir: str) -> TextEmbedding:
    return TextEmbedding(
        model_name=model_id,
        cache_dir=cache_dir,
        threads=ENCODER_THREADS,
        providers=["CPUExecutionProvider"],
        enable_cpu_mem_arena=False,
    )


def _dims(encoder: TextEmbedding) -> int:
    return int(encoder.embedding_size)


def _rebuild_cold(
    client: QdrantClient,
    source: str,
    target: str,
    encoder: TextEmbedding,
    progress: Callable[[int, int], None],
) -> dict[str, Any]:
    if client.collection_exists(target):
        client.delete_collection(target)
    info = client.get_collection(source)
    distance = getattr(info.config.params.vectors, "distance", Distance.COSINE)
    client.create_collection(
        collection_name=target,
        vectors_config=VectorParams(size=_dims(encoder), distance=distance),
    )
    total = int(info.points_count or 0)
    written = 0
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=source,
            limit=BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        texts = [
            str((p.payload or {}).get("content") or "")[:MAX_CHARS] for p in points
        ]
        vectors = list(encoder.embed(texts))
        client.upsert(
            collection_name=target,
            points=[
                PointStruct(id=p.id, vector=v.tolist(), payload=p.payload)
                for p, v in zip(points, vectors)
            ],
        )
        written += len(points)
        progress(written, total)
        if offset is None:
            break
    return {"source": source, "target": target, "written": written, "total": total}


def _rebuild_hot(
    source_path: str,
    target: QdrantClient,
    source_collection: str,
    encoder: TextEmbedding,
    progress: Callable[[int, int], None],
) -> dict[str, Any]:
    """Read the legacy store, write into the live client's own collection.

    The live client already holds the target path (mem0 points at it), so the
    target must be reused rather than reopened.
    """
    source = QdrantClient(path=source_path)
    try:
        if target.collection_exists(source_collection):
            target.delete_collection(source_collection)
        info = source.get_collection(source_collection)
        sparse_config = info.config.params.sparse_vectors or {}
        # Anonymous dense vector: mem0 queries without `using`, so a named vector
        # would make Qdrant unable to pick a target and return nothing.
        source_vectors = info.config.params.vectors
        distance = getattr(source_vectors, "distance", Distance.COSINE)
        target.create_collection(
            collection_name=source_collection,
            vectors_config=VectorParams(size=_dims(encoder), distance=distance),
            sparse_vectors_config=sparse_config or None,
        )
        total = int(info.points_count or 0)
        written = 0
        offset = None
        while True:
            points, offset = source.scroll(
                collection_name=source_collection,
                limit=BATCH,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if not points:
                break
            texts = [
                str((p.payload or {}).get("data") or "")[:MAX_CHARS] for p in points
            ]
            vectors = list(encoder.embed(texts))
            batch = []
            for point, vector in zip(points, vectors):
                raw = (
                    point.vector
                    if isinstance(point.vector, dict)
                    else {"": point.vector}
                )
                payload_vector: dict[str, Any] = {"": vector.tolist()}
                # BM25 is a bag-of-words sparse vector: independent of the dense model.
                if "bm25" in raw:
                    payload_vector["bm25"] = raw["bm25"]
                batch.append(
                    PointStruct(
                        id=point.id, vector=payload_vector, payload=point.payload
                    )
                )
            target.upsert(collection_name=source_collection, points=batch)
            written += len(points)
            progress(written, total)
            if offset is None:
                break
        return {"target": source_collection, "written": written, "total": total}
    finally:
        source.close()


def _run(
    target: str, model_id: str, cold_client, hot_store, qdrant_path: str, cache_dir: str
) -> None:
    try:
        tag = model_tag(model_id)
        encoder = _encoder(model_id, cache_dir)
        _set(stage="loading_model")
        list(encoder.embed(["warmup"]))
        results = []
        if target in ("cold", "all") and cold_client is not None:
            _set(stage="cold", done=0, total=0)
            results.append(
                _rebuild_cold(
                    cold_client,
                    index_name("knowledge_base", "minilm384"),
                    index_name("knowledge_base", tag),
                    encoder,
                    lambda done, total: _set(done=done, total=total),
                )
            )
        if target in ("hot", "all") and hot_store is not None:
            _set(stage="hot", done=0, total=0)
            results.append(
                _rebuild_hot(
                    os.path.join(qdrant_path, index_name("mem0", "minilm384")),
                    hot_store.client,
                    hot_store.collection_name,
                    encoder,
                    lambda done, total: _set(done=done, total=total),
                )
            )
        _set(running=False, stage="done", results=results)
        logger.info(f"[rebuild] finished: {results}")
    except Exception as e:  # noqa: BLE001 - surface the error through status
        logger.error(f"[rebuild] failed: {e}")
        _set(running=False, stage="failed", error=f"{type(e).__name__}: {e}")


def start(
    target: str,
    model_id: str,
    cold_client,
    hot_store,
    qdrant_path: str,
    cache_dir: str,
) -> bool:
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update(
            running=True,
            target=target,
            model=model_id,
            done=0,
            total=0,
            error="",
            stage="starting",
        )
    threading.Thread(
        target=_run,
        args=(target, model_id, cold_client, hot_store, qdrant_path, cache_dir),
        daemon=True,
    ).start()
    return True
