"""Note embedding cache."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import numpy as np

from agent_gateway.services.mcp.server_embed import (
    EMBEDDER_SPEC,
    _get_shared_embedder,
)

from agent_gateway.services.mcp.search_constants import (
    NOTE_DB_FILENAME,
    NOTE_MAX_CHUNKS_PER_PROJECT,
    _EMBED_BATCH_SIZE,
    _note_cache_lock,
    logger,
)
from agent_gateway.services.mcp.search_notes_io import (
    _chunk_md_text,
    _scan_md_files,
    is_dev_project,
)

_prewarm_lock = threading.Lock()
_prewarm_inflight: set[str] = set()

#: Prewarm runs on a daemon thread after every search call and cannot be cancelled
#: once started, so a first-time project would otherwise pin one core for minutes
#: on each burst of tool calls. Throttle per project and cap one run's workload.
_PREWARM_MIN_INTERVAL_SECONDS = 600
_PREWARM_MAX_FILES_PER_RUN = 200
_last_prewarm: dict[str, float] = {}


def _get_note_db(db_path: str) -> sqlite3.Connection:
    """Open note_vectors.sqlite with WAL mode, create schema if needed."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                file_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                line_start INTEGER NOT NULL DEFAULT 0,
                mtime REAL NOT NULL,
                file_size INTEGER NOT NULL,
                embedding BLOB,
                PRIMARY KEY (file_path, chunk_index)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
    _enforce_embedder_identity(conn)
    return conn


def _enforce_embedder_identity(conn: sqlite3.Connection) -> int:
    """Drop the cache when it was built by another embedding model.

    Vectors from different models are not comparable, and nothing else in the
    schema records who wrote them: a model swap used to leave the old rows in
    place, so every query died on a shape mismatch and silently fell back to
    keyword scoring. Returns the number of dropped chunks.
    """
    dims = str(EMBEDDER_SPEC.dims)
    conn.row_factory = sqlite3.Row
    stored = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM meta").fetchall()
    }
    # The stored blob length is the authority, not the meta row: caches written
    # before meta existed carry no record, and a partially rebuilt cache holds
    # both sizes at once (which makes numpy reject the whole matrix).
    foreign = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL AND length(embedding) != ?",
        (EMBEDDER_SPEC.dims * 4,),
    ).fetchone()[0]
    # A same-dimension model swap passes the blob check but produces vectors that
    # are not comparable, so the recorded tag is the second signal.
    stored_tag = str(stored.get("embedder_tag") or "")
    stale_tag = bool(stored_tag) and stored_tag != EMBEDDER_SPEC.tag
    dropped = 0
    if foreign or stale_tag:
        dropped = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        reason = (
            f"{stored_tag or 'unknown'} tag, expecting {EMBEDDER_SPEC.tag}/{dims}d"
            if stale_tag
            else f"{stored_tag or 'unknown'} dims, expecting {EMBEDDER_SPEC.tag}/{dims}d"
        )
        logger.info(
            f"[project_notes] cache holds vectors of another model ({reason}), "
            f"dropping {dropped} chunks to re-embed"
        )
        with conn:
            conn.execute("DELETE FROM chunks")
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            [("embedder_tag", EMBEDDER_SPEC.tag), ("embedder_dims", dims)],
        )
    return dropped


def _embed_in_batches(emb, texts: list[str]) -> list:
    """Batched inference; output order strictly matches input one-to-one."""
    out: list = []
    for i in range(0, len(texts), _EMBED_BATCH_SIZE):
        out.extend(emb.embed(texts[i : i + _EMBED_BATCH_SIZE]))
    return out


def project_root_from_db_path(db_path: str) -> str | None:
    """Return the project root for `<root>/.utility/deps.sqlite`, else None."""
    norm = os.path.normpath(db_path)
    if os.path.basename(norm) != "deps.sqlite":
        return None
    utility_dir = os.path.dirname(norm)
    if os.path.basename(utility_dir) != ".utility":
        return None
    return os.path.dirname(utility_dir)


def prewarm_project_notes(project_path: str) -> dict:
    """Warm the note vector cache; non-dev projects and empty doc sets are skipped."""
    project_path = os.path.normpath(project_path)
    if not is_dev_project(project_path):
        return {"ok": True, "skipped": "non-dev"}
    if not _scan_md_files(project_path):
        return {"ok": True, "skipped": "no-markdown"}

    # Capped on purpose: anything left over stays uncached (its mtime/size still
    # differ) and is picked up by the next prewarm or a real query.
    conn, db_path, total_chunks = _ensure_note_cache(
        project_path, max_files=_PREWARM_MAX_FILES_PER_RUN
    )
    conn.close()
    return {"ok": True, "chunks": total_chunks, "dbPath": db_path}


def schedule_project_notes_prewarm(project_path: str) -> bool:
    """Run `prewarm_project_notes` in a daemon thread; deduplicates per project."""
    project_path = os.path.normpath(project_path)

    with _prewarm_lock:
        if project_path in _prewarm_inflight:
            return False
        now = time.monotonic()
        last = _last_prewarm.get(project_path)
        if last is not None and now - last < _PREWARM_MIN_INTERVAL_SECONDS:
            return False
        _prewarm_inflight.add(project_path)
        _last_prewarm[project_path] = now

    def _run() -> None:
        try:
            result = prewarm_project_notes(project_path)
            logger.info(f"[project_notes] prewarm {project_path}: {result}")
        except Exception as e:
            logger.warning(f"[project_notes] prewarm failed for {project_path}: {e}")
        finally:
            with _prewarm_lock:
                _prewarm_inflight.discard(project_path)

    threading.Thread(
        target=_run,
        name="project-notes-prewarm",
        daemon=True,
    ).start()
    return True


def schedule_prewarm_for_db(db_path: str) -> bool:
    """Schedule prewarm from a `.utility/deps.sqlite` path."""
    project_path = project_root_from_db_path(db_path)
    if not project_path:
        return False
    return schedule_project_notes_prewarm(project_path)


def _ensure_note_cache(
    project_path: str,
    max_files: int | None = None,
) -> tuple[sqlite3.Connection, str, int]:
    """Incremental cache update: scan .md files, diff mtime/size, embed only changed.

    ``max_files`` caps how many changed files are embedded in this call; files
    beyond the cap keep their old rows and are retried on the next call.
    """
    project_path = os.path.normpath(project_path)
    cache_dir = os.path.join(project_path, ".utility")
    os.makedirs(cache_dir, exist_ok=True)
    db_path = os.path.join(cache_dir, NOTE_DB_FILENAME)

    conn = _get_note_db(db_path)

    # This used to quick-skip via "project root dir mtime <= the max mtime of .md files
    # in the cache", but the two have different semantics: the former is the project
    # *root directory*'s mtime (changes only when direct children are added/removed),
    # the latter is the mtime of each cached .md file (last write time). Distortion goes
    # both ways — editing a deep existing file does not trigger a rescan (returning stale
    # results), while adding/removing at the root triggers a full rescan.
    # Changed to always scan + per-file diff: measured, scanning 453 .md files takes only
    # 7ms, whereas a wrong staleness decision means re-embedding everything (6.3s
    # average, 150s worst case). The scan cost is negligible.

    # Serialized rebuild: when multiple requests arrive at once, embedding runs only once (ONNX inference is blocking and memory-intensive)
    _note_cache_lock.acquire()
    try:
        md_files = _scan_md_files(project_path)
        cached = {}
        try:
            rows = conn.execute(
                "SELECT DISTINCT file_path, mtime, file_size FROM chunks"
            ).fetchall()
            cached = {r[0]: {"mtime": r[1], "size": r[2]} for r in rows}
        except sqlite3.DatabaseError:
            conn.close()
            try:
                os.remove(db_path)
            except OSError:
                pass
            conn = _get_note_db(db_path)

        # The cache is derived data owned by this project, so any row not backed by a
        # currently scanned file is stale: deleted docs, newly ignored paths, or rows
        # written before the project moved (absolute paths still point at the old root).
        # Keeping them pollutes retrieval and can hold a majority of the chunks.
        scanned_paths = {mf["path"] for mf in md_files}
        stale_paths = [
            p
            for p in cached
            if p not in scanned_paths or not p.startswith(project_path + os.sep)
        ]
        if stale_paths:
            with conn:
                conn.executemany(
                    "DELETE FROM chunks WHERE file_path = ?",
                    [(p,) for p in stale_paths],
                )
            for p in stale_paths:
                cached.pop(p, None)
            logger.info(
                f"[project_notes] {project_path}: pruned {len(stale_paths)} stale files"
            )

        changed = [
            mf
            for mf in md_files
            if mf["path"] not in cached
            or cached[mf["path"]]["mtime"] != mf["mtime"]
            or cached[mf["path"]]["size"] != mf["size"]
        ]
        if max_files is not None and len(changed) > max_files:
            logger.info(
                f"[project_notes] {project_path}: capping this run at {max_files} "
                f"of {len(changed)} changed files"
            )
            changed = changed[:max_files]

        if changed:
            logger.info(
                f"[project_notes] {project_path}: {len(changed)} files need embedding "
                f"(of {len(md_files)} total .md files)"
            )
            emb = _get_shared_embedder()
            if emb is None:
                logger.warning(
                    "[project_notes] embedder unavailable, skipping embedding"
                )

            # Phase 1: read files and chunk (read-only, no DB touched — files that fail
            # to read are skipped outright and their old rows are kept as-is, so there is
            # no window of "old rows deleted but new rows not yet written")
            prepared: list[tuple[dict, list[dict]]] = []
            for mf in changed:
                try:
                    with open(
                        mf["path"], "r", encoding="utf-8", errors="replace"
                    ) as fh:
                        text = fh.read()
                except Exception as e:
                    logger.warning(f"[project_notes] error reading {mf['path']}: {e}")
                    continue
                chunks = _chunk_md_text(text, mf["path"])
                if chunks:
                    prepared.append((mf, chunks))

            # Phase 2: trim to the global quota (preserving the
            # NOTE_MAX_CHUNKS_PER_PROJECT cap semantics). The baseline must subtract the
            # old chunks of files being rewritten — they are replaced wholesale in phase 4.
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            for mf, _chunks in prepared:
                total -= conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE file_path = ?", (mf["path"],)
                ).fetchone()[0]
            accepted: list[tuple[dict, list[dict]]] = []
            for mf, chunks in prepared:
                if total + len(chunks) > NOTE_MAX_CHUNKS_PER_PROJECT:
                    logger.warning(
                        f"[project_notes] {project_path}: "
                        f"truncating at {NOTE_MAX_CHUNKS_PER_PROJECT} chunks"
                    )
                    chunks = chunks[: NOTE_MAX_CHUNKS_PER_PROJECT - total]
                if not chunks:
                    continue
                accepted.append((mf, chunks))
                total += len(chunks)

            # Phase 3: run batched inference over all texts to embed at once. Calling
            # per file multiplies the Python↔ONNX round trips to the order of the file
            # count; batched, it depends only on the total item count.
            vectors: list = []
            if emb is not None:
                texts = [c["chunk_text"] for _mf, chunks in accepted for c in chunks]
                vectors = _embed_in_batches(emb, texts)

            # Phase 4: write back (vectors map one-to-one to the flattened order of
            # accepted). A single file's "delete old rows + insert new rows" is committed
            # in one transaction to stay atomic.
            cursor = 0
            for mf, chunks in accepted:
                try:
                    conn.execute(
                        "DELETE FROM chunks WHERE file_path = ?", (mf["path"],)
                    )
                    for c in chunks:
                        vec = vectors[cursor] if cursor < len(vectors) else None
                        cursor += 1
                        if vec is not None:
                            conn.execute(
                                "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
                                (
                                    mf["path"],
                                    c["chunk_index"],
                                    c["chunk_text"],
                                    c["line_start"],
                                    mf["mtime"],
                                    mf["size"],
                                    np.asarray(vec, dtype=np.float32).tobytes(),
                                ),
                            )
                        else:
                            # No embedder: store without embedding (keyword fallback later)
                            conn.execute(
                                "INSERT OR REPLACE INTO chunks "
                                "(file_path, chunk_index, chunk_text, line_start, mtime, file_size) "
                                "VALUES (?,?,?,?,?,?)",
                                (
                                    mf["path"],
                                    c["chunk_index"],
                                    c["chunk_text"],
                                    c["line_start"],
                                    mf["mtime"],
                                    mf["size"],
                                ),
                            )
                    conn.commit()
                except Exception as e:
                    logger.warning(f"[project_notes] error writing {mf['path']}: {e}")
                    conn.rollback()
                    continue

        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return conn, db_path, total_chunks
    finally:
        _note_cache_lock.release()
