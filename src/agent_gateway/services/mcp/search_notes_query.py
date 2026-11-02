"""Project note semantic/keyword query."""

from __future__ import annotations

import os
import sqlite3

import numpy as np

from agent_gateway.services.mcp.server_embed import embed_query

from agent_gateway.services.mcp.search_constants import (
    NOTE_KEYWORD_FLOOR,
    NOTE_MAX_RESULTS,
    NOTE_PREVIEW_LEN,
    NOTE_QUERY_EMBED_BUDGET,
    NOTE_SIMILARITY_THRESHOLD,
    logger,
)
from agent_gateway.services.mcp.search_notes_cache import (
    _ensure_note_cache,
    _open_note_cache_readonly,
    schedule_project_notes_prewarm,
)
from agent_gateway.services.retrieval.text.analyzer import split_terms
from agent_gateway.services.mcp.search_notes_io import (
    _chunk_md_text,
    _scan_md_files,
    is_dev_project,
)


def _keyword_search_uncached(project_path: str, query: str, limit: int) -> dict:
    """Degraded path for non-dev projects: in-memory-only keyword search, no .utility
    cache created.

    Same logic as _query_project_notes's keyword fallback, but scans files and matches
    chunks directly without persisting to SQLite. Repeated queries against directories
    like notes leave no disk residue.
    """
    tokens = [t.lower() for t in split_terms(query)] or [t for t in query.split() if t]
    results: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for mf in _scan_md_files(project_path):
        try:
            with open(mf["path"], "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for c in _chunk_md_text(text, mf["path"]):
            ct = c["chunk_text"]
            if not tokens or not any(t.lower() in ct.lower() for t in tokens):
                continue
            matched = sum(1 for t in tokens if t.lower() in ct.lower())
            key = (mf["path"], c["chunk_index"])
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "file": os.path.relpath(mf["path"], project_path),
                    "line": c["line_start"],
                    "content": ct[:NOTE_PREVIEW_LEN],
                    "score": round(matched / max(len(tokens), 1), 4),
                }
            )

    results.sort(key=lambda x: -x["score"])
    return {
        "results": results[:limit],
        "count": min(len(results), limit),
        "total_chunks": len(seen),
    }


def _query_project_notes(project_path: str, query: str, limit: int) -> dict:
    """Semantic search over project .md files using cached vectors.

    Tracing moved to the pipeline that now drives this arm, so the decorator
    here would record the same call twice.
    """
    limit = max(1, min(limit, NOTE_MAX_RESULTS))

    # Non-dev project (no marker files): no .utility cache persisted, in-memory-only keyword search
    if not is_dev_project(project_path):
        logger.info(
            f"[project_notes] non-dev project, uncached keyword search: {project_path}"
        )
        return _keyword_search_uncached(project_path, query, limit)

    try:
        ensured = _ensure_note_cache(
            project_path, max_files=NOTE_QUERY_EMBED_BUDGET, wait=False
        )
    except Exception as e:
        logger.error(f"[project_notes] cache init failed: {e}")
        return {"error": f"Cache init failed: {e}", "results": [], "count": 0}

    if ensured is None:
        ensured = _open_note_cache_readonly(project_path)
        logger.info(
            f"[project_notes] {project_path}: cache busy, serving existing chunks"
        )
    if ensured is None:
        return _keyword_search_uncached(project_path, query, limit)
    conn, db_path, total_chunks = ensured

    if total_chunks == 0:
        conn.close()
        schedule_project_notes_prewarm(project_path)
        logger.info(f"[project_notes] {project_path}: empty cache, keyword fallback")
        return _keyword_search_uncached(project_path, query, limit)

    try:
        rows = conn.execute(
            "SELECT file_path, chunk_index, chunk_text, line_start, embedding FROM chunks"
        ).fetchall()
    except sqlite3.DatabaseError as e:
        conn.close()
        logger.error(f"[project_notes] db error: {e}")
        return {"error": f"DB error: {e}", "results": [], "count": 0}

    conn.close()
    # Anything this query capped is finished in the background.
    schedule_project_notes_prewarm(project_path)

    if not rows:
        return {"results": [], "count": 0, "total_chunks": total_chunks}

    # Vector scores over cached chunks. Chunks without embeddings fall back to
    # keyword-only scoring below.
    query_vec = embed_query(query)
    vector_scores: dict[tuple[str, int], float] = {}
    vectors_by_key: dict[tuple[str, int], np.ndarray] = {}

    if query_vec is not None:
        try:
            qnorm = np.linalg.norm(query_vec)
            if qnorm > 1e-10:
                query_vec = query_vec / qnorm
                keys: list[tuple[str, int]] = []
                vectors = []
                for r in rows:
                    blob = r["embedding"]
                    if blob is None:
                        continue
                    try:
                        vec = np.frombuffer(blob, dtype=np.float32).copy()
                        norm = np.linalg.norm(vec)
                        normalized = vec / norm if norm > 1e-10 else vec
                        vectors.append(normalized)
                        keys.append((r["file_path"], r["chunk_index"]))
                    except Exception:
                        continue
                if vectors:
                    matrix = np.array(vectors, dtype=np.float32)
                    sims = np.dot(matrix, query_vec)
                    for key, sim, vec in zip(keys, sims, vectors):
                        vector_scores[key] = float(sim)
                        vectors_by_key[key] = vec
        except Exception as e:
            logger.error(
                f"[project_notes] vector search failed, scoring is keyword-only "
                f"for this query: {e}"
            )

    # Hybrid rerank: semantic similarity, exact token coverage, and a small
    # path boost. A chunk must clear the vector threshold or match a token;
    # low-similarity vector hits are dropped instead of filling the limit.
    # CJK queries carry no whitespace, so a raw split leaves one needle equal to
    # the whole query and the keyword layer never fires. Content matching reuses
    # the recall analyzer (CJK bigrams); path matching keeps the raw tokens so
    # two-character bigrams do not match every directory name.
    path_tokens = [t for t in query.split() if t]
    needles = [t.lower() for t in split_terms(query)] or [
        t.lower() for t in path_tokens
    ]
    path_needles = [t.lower() for t in path_tokens] or needles
    candidates: dict[tuple[str, int], dict] = {}

    for r in rows:
        text = r["chunk_text"]
        lowered = text.lower()
        matched = sum(1 for t in needles if t in lowered)
        keyword_score = matched / max(len(needles), 1) if needles else 0.0
        key = (r["file_path"], r["chunk_index"])
        vector_score = vector_scores.get(key, 0.0)

        if (
            vector_score < NOTE_SIMILARITY_THRESHOLD
            and keyword_score < NOTE_KEYWORD_FLOOR
        ):
            continue

        rel = os.path.relpath(r["file_path"], project_path)
        path_hit = (
            1.0 if path_needles and any(t in rel.lower() for t in path_needles) else 0.0
        )
        final_score = 0.65 * vector_score + 0.30 * keyword_score + 0.05 * path_hit
        candidates[key] = {
            "file": rel,
            "line": r["line_start"],
            "content": text[:NOTE_PREVIEW_LEN],
            "score": round(final_score, 4),
            "_vector": vector_score,
            "_vec": vectors_by_key.get(key),
        }

    # MMR-style diversity pass: near-duplicate docs (e.g. copied language
    # variants) otherwise fill the whole limit. Keep relevance dominant, but
    # penalize chunks that are nearly identical to an already selected one.
    remaining = sorted(candidates.values(), key=lambda x: (-x["score"], -x["_vector"]))
    ordered: list[dict] = []
    while remaining and len(ordered) < limit:
        best_idx = 0
        best_value = float("-inf")
        for idx, item in enumerate(remaining):
            vec = item["_vec"]
            similarity = 0.0
            if vec is not None and ordered:
                similarity = max(
                    (
                        float(np.dot(vec, selected["_vec"]))
                        for selected in ordered
                        if selected["_vec"] is not None
                    ),
                    default=0.0,
                )
            value = 0.7 * item["score"] - 0.3 * similarity
            if value > best_value:
                best_value = value
                best_idx = idx
        ordered.append(remaining.pop(best_idx))

    for item in ordered:
        item.pop("_vector", None)
        item.pop("_vec", None)

    return {
        "results": ordered[:limit],
        "count": len(ordered[:limit]),
        "total_chunks": total_chunks,
    }
