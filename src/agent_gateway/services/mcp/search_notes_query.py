"""Project note semantic/keyword query."""

from __future__ import annotations

import os
import sqlite3

import numpy as np

from agent_gateway.services.mcp.server_embed import embed_query

from agent_gateway.services.mcp.search_constants import (
    NOTE_EXCLUDE_DIRS,
    NOTE_MAX_RESULTS,
    NOTE_PREVIEW_LEN,
    NOTE_SIMILARITY_THRESHOLD,
    PROJECT_MARKER_DIR_SUFFIXES,
    PROJECT_MARKER_FILES,
    PROJECT_MARKER_FILE_SUFFIXES,
    PROJECT_MARKER_MAX_DEPTH,
    logger,
)
from agent_gateway.services.mcp.search_notes_cache import _ensure_note_cache
from agent_gateway.services.mcp.search_notes_io import _chunk_md_text, _scan_md_files


def _is_dev_project(project_path: str) -> bool:
    """Shallow-scan the target path for common project marker files — decides whether it
    is a "dev project".

    A pure document repo (e.g. notes: only .md + .git) has none of these markers → returns
    False, so project_notes uses an in-memory-only keyword search for non-dev projects and
    no longer creates the .utility cache. Note: .git/.gitignore are deliberately excluded
    (document repos such as notes have them too) to avoid a false positive.
    """
    project_path = os.path.normpath(project_path)

    def _scan(root: str, depth: int) -> bool:
        if depth > PROJECT_MARKER_MAX_DEPTH:
            return False
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_dir():
                        if entry.name in NOTE_EXCLUDE_DIRS:
                            continue
                        if entry.name.endswith(PROJECT_MARKER_DIR_SUFFIXES):
                            return True
                        if _scan(entry.path, depth + 1):
                            return True
                    elif entry.is_file():
                        if entry.name in PROJECT_MARKER_FILES:
                            return True
                        if entry.name.endswith(PROJECT_MARKER_FILE_SUFFIXES):
                            return True
        except OSError:
            pass
        return False

    return _scan(project_path, 0)


def _keyword_search_uncached(project_path: str, query: str, limit: int) -> dict:
    """Degraded path for non-dev projects: in-memory-only keyword search, no .utility
    cache created.

    Same logic as _query_project_notes's keyword fallback, but scans files and matches
    chunks directly without persisting to SQLite. Repeated queries against directories
    like notes leave no disk residue.
    """
    tokens = [t for t in query.split() if t]
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
    """Semantic search over project .md files using cached vectors."""
    limit = max(1, min(limit, NOTE_MAX_RESULTS))

    # Non-dev project (no marker files): no .utility cache persisted, in-memory-only keyword search
    if not _is_dev_project(project_path):
        logger.info(
            f"[project_notes] non-dev project, uncached keyword search: {project_path}"
        )
        return _keyword_search_uncached(project_path, query, limit)

    try:
        conn, db_path, total_chunks = _ensure_note_cache(project_path)
    except Exception as e:
        logger.error(f"[project_notes] cache init failed: {e}")
        return {"error": f"Cache init failed: {e}", "results": [], "count": 0}

    try:
        rows = conn.execute(
            "SELECT file_path, chunk_index, chunk_text, line_start, embedding FROM chunks"
        ).fetchall()
    except sqlite3.DatabaseError as e:
        conn.close()
        logger.error(f"[project_notes] db error: {e}")
        return {"error": f"DB error: {e}", "results": [], "count": 0}

    conn.close()

    if not rows:
        return {"results": [], "count": 0, "total_chunks": total_chunks}

    # Try vector search first (via the shared query cache)
    query_vec = embed_query(query)
    vector_results: list[dict] = []

    if query_vec is not None:
        try:
            qnorm = np.linalg.norm(query_vec)
            if qnorm > 1e-10:
                query_vec = query_vec / qnorm

                entries = []
                vectors = []
                for r in rows:
                    blob = r["embedding"]
                    if blob is None:
                        continue
                    try:
                        vec = np.frombuffer(blob, dtype=np.float32).copy()
                        norm = np.linalg.norm(vec)
                        if norm > 1e-10:
                            vectors.append(vec / norm)
                        else:
                            vectors.append(vec)
                        entries.append(
                            {
                                "file_path": r["file_path"],
                                "chunk_text": r["chunk_text"],
                                "line_start": r["line_start"],
                            }
                        )
                    except Exception:
                        continue

                if vectors:
                    matrix = np.array(vectors, dtype=np.float32)
                    sims = np.dot(matrix, query_vec)
                    indices = np.argsort(-sims)
                    for idx in indices:
                        if sims[idx] < NOTE_SIMILARITY_THRESHOLD:
                            break
                        if len(vector_results) >= limit:
                            break
                        e = entries[idx]
                        vector_results.append(
                            {
                                "file": os.path.relpath(e["file_path"], project_path),
                                "line": e["line_start"],
                                "content": e["chunk_text"][:NOTE_PREVIEW_LEN],
                                "score": round(float(sims[idx]), 4),
                            }
                        )

            if vector_results:
                pass  # fall through to keyword merge below
        except Exception as e:
            logger.warning(f"[project_notes] vector search failed: {e}")

    # Keyword search (grep-lite) — always runs; exact matches are inserted ahead of vector results
    tokens = [t for t in query.split() if t]
    keyword_results: list[dict] = []
    seen_files: set[tuple[str, int]] = set()

    for r in rows:
        text = r["chunk_text"]
        if not tokens or not any(t.lower() in text.lower() for t in tokens):
            continue
        matched = sum(1 for t in tokens if t.lower() in text.lower())
        key = (r["file_path"], r["chunk_index"])
        if key in seen_files:
            continue
        seen_files.add(key)
        keyword_results.append(
            {
                "file": os.path.relpath(r["file_path"], project_path),
                "line": r["line_start"],
                "content": text[:NOTE_PREVIEW_LEN],
                "score": round(matched / max(len(tokens), 1), 4),
            }
        )

    keyword_results.sort(key=lambda x: -x["score"])

    if vector_results:
        # Exact matches inserted ahead of vector results (vector search can rank poorly for CJK/cross-language queries)
        seen_keys = {(r["file"], r["line"]) for r in vector_results}
        exact_hits = [
            r for r in keyword_results if (r["file"], r["line"]) not in seen_keys
        ]
        merged = exact_hits + vector_results
        return {
            "results": merged[:limit],
            "count": len(merged[:limit]),
            "total_chunks": total_chunks,
        }

    return {
        "results": keyword_results[:limit],
        "count": len(keyword_results[:limit]),
        "total_chunks": total_chunks,
    }
