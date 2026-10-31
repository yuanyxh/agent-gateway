"""Vector search over symbol embeddings."""

from __future__ import annotations

import hashlib
import sqlite3

import numpy as np

from agent_gateway.core.db import conn as dbconn
from agent_gateway.services.mcp.server_embed import _get_shared_embedder, embed_query

from agent_gateway.services.mcp.server_constants import MODEL_TAG
from agent_gateway.services.mcp.search_constants import (
    _EMBED_BATCH_SIZE,
    _MAX_VECTOR_CACHE,
    _MAX_VECTOR_ROWS,
    _symbol_embeddings,
    logger,
)


def _symbol_cache_key(
    file_path: str, name: str, kind: str, signature: str, doc: str
) -> str:
    """Stable cache key for a symbol — derived from "the fields that define the
    embedding input".

    Deliberately not `symbols.id`: that column is GRDB's AUTOINCREMENT, and both write
    paths give the same symbol a new id — ProjectScanner's full scan goes through
    `clearAll()` (which first `DELETE FROM symbols` then re-inserts); batchWriteFiles
    deletes by file_path then re-inserts. Keyed by id, every rescan immediately
    invalidates all embeddings (measured: side-table 67438 rows, 0 valid, 132MB), and
    the next vector search is forced to recompute every symbol (measured 349s).

    With business fields, an unchanged symbol does not trigger recomputation on rescan;
    if any field participating in the embedding text changes, the key changes and it is
    recomputed naturally — the key is the input, so there is no stale read.
    """
    raw = "\x1f".join((MODEL_TAG, file_path, name, kind, signature, doc))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ensure_symbol_embeddings_schema(conn: sqlite3.Connection) -> None:
    """Create the side-table; a legacy symbol_id-keyed side-table is dropped and rebuilt.

    Old rows are keyed by id and cannot be mapped to the new business keys; embeddings
    are purely derived data (recomputable at any time), so dropping them costs only one
    recomputation, while keeping them is a space leak that is never reused.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(symbol_embeddings)")}
    if cols and "sym_key" not in cols:
        logger.info(
            "[symbol_embeddings] dropping legacy symbol_id-keyed table "
            "(derived data, recomputed on demand)"
        )
        conn.execute("DROP TABLE symbol_embeddings")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_embeddings (
            sym_key TEXT PRIMARY KEY,
            embedding BLOB NOT NULL
        )
    """)


def _load_symbol_embeddings(conn: sqlite3.Connection) -> dict[str, bytes]:
    """Read all embeddings (stable key → BLOB)."""
    return {
        row["sym_key"]: row["embedding"]
        for row in conn.execute("SELECT sym_key, embedding FROM symbol_embeddings")
    }


def _prune_symbol_embeddings(
    conn: sqlite3.Connection,
    cached: dict[str, bytes],
    live_keys: set[str],
    truncated: bool,
) -> None:
    """Delete embeddings no longer referenced by any symbol, so the side-table does not grow unbounded across rescans."""
    if truncated:
        # When the symbol query is truncated by LIMIT, live_keys is incomplete and pruning would wrongly delete still-valid rows
        return
    stale = cached.keys() - live_keys
    if not stale:
        return
    conn.executemany(
        "DELETE FROM symbol_embeddings WHERE sym_key = ?", [(k,) for k in stale]
    )
    conn.commit()
    for k in stale:
        cached.pop(k, None)
    logger.info(f"[symbol_embeddings] pruned {len(stale)} stale rows")


def _ensure_symbol_embeddings(db_path: str) -> tuple[list[dict], np.ndarray | None]:
    """Persist symbol embeddings in SQLite, lazily fill missing ones.

    1. Creates/uses the symbol_embeddings side-table in deps.sqlite
    2. Computes embeddings only for symbols whose stable key is absent
    3. Prunes keys no longer referenced by any symbol
    4. Returns (symbols_list, embedding_matrix)
    """
    emb = _get_shared_embedder()
    if emb is None:
        return [], None

    try:
        with dbconn(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            _ensure_symbol_embeddings_schema(conn)

            # Fetch one extra row to detect truncation by LIMIT (no orphan pruning when truncated)
            rows = conn.execute(
                """
                SELECT file_path, name, kind, signature, doc_comment
                FROM symbols
                LIMIT ?
            """,
                (_MAX_VECTOR_ROWS + 1,),
            ).fetchall()
            truncated = len(rows) > _MAX_VECTOR_ROWS
            rows = rows[:_MAX_VECTOR_ROWS]

            if not rows:
                # Symbol table is empty → the side-table is all orphans; clean it up
                # before returning. This explicit cleanup is required: only the normal
                # path below reaches _prune_symbol_embeddings, and returning directly
                # would leave invalid embeddings behind forever for the "project became
                # empty" case.
                _prune_symbol_embeddings(
                    conn, _load_symbol_embeddings(conn), set(), False
                )
                return [], None

            entries: list[tuple[str, str, sqlite3.Row]] = []
            for r in rows:
                sig = r["signature"] or ""
                doc = r["doc_comment"] or ""
                key = _symbol_cache_key(r["file_path"], r["name"], r["kind"], sig, doc)
                text = r["name"]
                if sig:
                    text += " " + sig
                if doc:
                    text += " " + doc
                entries.append((key, text, r))

            cached = _load_symbol_embeddings(conn)

            # Only fill in the missing ones: the key maps one-to-one to the embedding input, so a cache hit needs no recomputation
            missing = [e for e in entries if e[0] not in cached]
            if missing:
                batches = [
                    missing[i : i + _EMBED_BATCH_SIZE]
                    for i in range(0, len(missing), _EMBED_BATCH_SIZE)
                ]
                for batch in batches:
                    vectors = list(emb.embed([e[1] for e in batch]))
                    for (key, _text, _row), vec in zip(batch, vectors):
                        conn.execute(
                            "INSERT OR REPLACE INTO symbol_embeddings "
                            "(sym_key, embedding) VALUES (?, ?)",
                            (key, np.asarray(vec, dtype=np.float32).tobytes()),
                        )
                    conn.commit()
                cached = _load_symbol_embeddings(conn)

            _prune_symbol_embeddings(conn, cached, {e[0] for e in entries}, truncated)

            symbols: list[dict] = []
            vectors_list: list[np.ndarray] = []
            for key, _text, r in entries:
                blob = cached.get(key)
                if blob is None:
                    continue
                try:
                    vec = np.frombuffer(blob, dtype=np.float32).copy()
                except Exception:
                    continue
                norm = np.linalg.norm(vec)
                if norm < 1e-10:
                    continue
                vectors_list.append(vec / norm)
                symbols.append(
                    {
                        "name": r["name"],
                        "file": r["file_path"],
                        "signature": r["signature"],
                    }
                )

            if not vectors_list:
                return [], None

            return symbols, np.array(vectors_list, dtype=np.float32)

    except Exception as e:
        logger.warning(f"[symbol_embeddings] init failed: {e}")
        return [], None


def _symbols_signature(db_path: str) -> tuple[int, int] | None:
    """Cheap change fingerprint of the symbols table `(row count, MAX(id))`, used to
    decide whether the in-memory vector cache is stale.

    Deliberately **not** the DB file's mtime/size: the DB runs in WAL mode, so commits
    only write the `-wal` file and the main DB file's mtime/size does not budge at all
    (confirmed empirically); using it as the signal would always think "unchanged".
    Both columns are taken because: a full rescan clears and re-inserts with new ids
    (MAX grows), while a pure delete changes only the row count. Exceptions such as a
    missing table return None, and the caller falls back to the "no cache" path.
    """
    try:
        with dbconn(db_path, timeout=5) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), -1) FROM symbols"
            ).fetchone()
        return (row[0], row[1])
    except Exception:
        return None


def _search_vectors(db_path: str, query: str) -> list[dict]:
    """Layer 4: Vector similarity search against persisted symbol embeddings.

    Persists embeddings in SQLite (symbol_embeddings side-table) so cold
    starts read from disk (~70ms) instead of recomputing from scratch (~110s).
    Missing embeddings are filled lazily on next query.
    In-memory cache keeps subsequent calls fast, keyed by a cheap signature of
    the symbols table so an external rescan invalidates it (otherwise a rescan
    would leave the cache serving removed/renamed symbols' stale paths).
    """
    try:
        # Lazy-load from SQLite + cache in memory; use the symbols-table fingerprint to detect staleness
        sig = _symbols_signature(db_path)
        cached = _symbol_embeddings.get(db_path)
        if cached is None or cached[2] != sig:
            symbols, emb_matrix = _ensure_symbol_embeddings(db_path)
            # Evict the oldest cache entry
            while len(_symbol_embeddings) >= _MAX_VECTOR_CACHE:
                oldest = next(iter(_symbol_embeddings))
                logger.info(f"[vector] evicting cached embeddings for: {oldest}")
                del _symbol_embeddings[oldest]
            _symbol_embeddings[db_path] = (symbols, emb_matrix, sig)

        symbols, emb_matrix = _symbol_embeddings[db_path][:2]
        if emb_matrix is None or len(symbols) == 0:
            return []

        # Encode query + normalize (via the shared query cache)
        query_emb = embed_query(query)
        if query_emb is None:
            return []
        qnorm = np.linalg.norm(query_emb)
        if qnorm < 1e-10:
            return []
        query_emb = query_emb / qnorm

        sims = np.dot(emb_matrix, query_emb)

        threshold = 0.35
        indices = np.argsort(-sims)
        results = []
        for idx in indices:
            if sims[idx] < threshold:
                break
            results.append(
                {
                    "name": symbols[idx]["name"],
                    "kind": "symbol",
                    "file": symbols[idx]["file"],
                    "signature": symbols[idx]["signature"],
                    "visibility": None,
                    "score": round(float(sims[idx]), 4),
                }
            )
            if len(results) >= 5:
                break
        return results

    except Exception as e:
        logger.warning(f"[vector_search] {e}")
        return []
