#!/usr/bin/env python3
"""Check two invariants of the vector-search cache (the regression gate for the 2026-09-11 performance fix).

Background: `project_search` and `project_notes` each have a layer of embedding cache, and both
invariants are implicit contracts that "look right and, when broken, only become slower or staler",
which are very easy to break unintentionally during a refactor:

  A. The symbol cache key must cover all input fields of the embedding
     - `symbols.id` is renumbered entirely by a full rescan (clearAll → DELETE + reinsert), so
       using id as the key invalidates the whole cache after every rescan (once caused a single
       vector search to recompute for 349s)
     - if the key misses an input field, a change to that field does not trigger a recompute →
       a stale vector is returned

  B. The note cache must be able to detect changes to deep files
     - it once used the "project root directory mtime" to decide invalidation, but that only changes
       when a direct child is added or removed → changing an existing deep file was judged
       "unchanged", returning stale search results

Usage (run from the project root):
    PYTHONPATH=src/agent_gateway \\
    FASTEMBED_CACHE_PATH=~/.cache/utility/fastembed \\
    ~/.config/utility/mcp-venv/bin/python3 scripts/check-search-cache-invariants.py

Exit code 0 = all passed; 1 = an invariant was broken. It only uses a temp directory and does not
touch real data.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

SYMBOL_TABLE_SQL = """
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    signature TEXT, doc_comment TEXT, visibility TEXT,
    start_line INTEGER, end_line INTEGER)
"""

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def _new_symbol_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(SYMBOL_TABLE_SQL)
    return conn


def _insert(conn: sqlite3.Connection, sig: str, start_line: int | None = None) -> None:
    conn.execute(
        "INSERT INTO symbols (file_path, name, kind, signature, start_line) "
        "VALUES ('/p/a.swift', 'alpha', 'func', ?, ?)",
        (sig, start_line),
    )
    conn.commit()


def check_symbol_cache_key(tmp: str) -> None:
    """A. Symbol cache key invariant.

    The decision uses "how many rows were actually embedded this round" (by wrapping a counting
    embedder) rather than "whether the vectors are the same"——a single symbol recompute yields a
    vector bit-for-bit identical to the cached value, so comparing vectors cannot detect the
    "pointless recompute" defect.
    """
    print("\nA. Symbol cache key (symbol_embeddings)")

    from agent_gateway.services.mcp import server_search as S

    db = os.path.join(tmp, "symbols.sqlite")
    conn = _new_symbol_db(db)
    _insert(conn, "func alpha()")

    from agent_gateway.services.mcp import search_vectors as SV

    real_getter = SV._get_shared_embedder
    embedded: list[str] = []

    class _CountingEmbedder:
        def __init__(self, inner):
            self._inner = inner

        def embed(self, texts):
            texts = list(texts)
            embedded.extend(texts)
            return self._inner.embed(texts)

    def run() -> None:
        embedded.clear()
        SV._get_shared_embedder = lambda: _CountingEmbedder(real_getter())
        try:
            SV._ensure_symbol_embeddings(db)
        finally:
            SV._get_shared_embedder = real_getter

    def edge_rows() -> int:
        return conn.execute("SELECT COUNT(*) FROM symbol_embeddings").fetchone()[0]

    run()
    check(
        "first time computing an embedding for the symbol",
        len(embedded) == 1,
        f"actually {len(embedded)} rows",
    )

    # A1: only id changes (simulating DELETE + reinsert after clearAll) → must not recompute, must not leave orphans
    conn.execute("DELETE FROM symbols")
    _insert(conn, "func alpha()")
    run()
    check(
        "no recompute after a rescan renumbers ids",
        len(embedded) == 0,
        f"recomputed {len(embedded)} rows —— failure = the key is bound back to the drift-prone symbols.id",
    )
    check(
        "no orphan accumulation in the edge table after a rescan",
        edge_rows() == 1,
        f"edge table has {edge_rows()} rows (1 live symbol)",
    )

    # A2: a field participating in the embedding text changes → must recompute
    conn.execute("UPDATE symbols SET signature='func alpha(x: Int, y: Int) -> R'")
    conn.commit()
    run()
    check(
        "must recompute after the signature changes",
        len(embedded) == 1,
        f"actually {len(embedded)} rows —— failure = the key omits signature and will return a stale vector",
    )

    # A3: a field not participating in the embedding text changes → should not recompute
    conn.execute("UPDATE symbols SET start_line=999")
    conn.commit()
    run()
    check(
        "a line-number drift alone does not trigger a recompute",
        len(embedded) == 0,
        f"recomputed {len(embedded)} rows —— failure = the key mixes in a positional field",
    )

    # A4: after a symbol is deleted, its embedding must be cleaned up (otherwise the edge table grows without bound)
    conn.execute("DELETE FROM symbols")
    conn.commit()
    run()
    check(
        "no orphans in the edge table after a symbol is deleted",
        edge_rows() == 0,
        f"{edge_rows()} rows left over",
    )

    # A5: DB content changes but the process has not restarted → the in-memory cache must be invalidated
    # Defect form: the cache is looked up only by db_path, so after the DB is rescanned it still
    # returns old symbol metadata (old paths, deleted symbols), and new symbols can't be found either,
    # until the process restarts. The key is to call **without clearing the in-memory cache**, which
    # is the only way to expose this defect.
    conn.execute("DELETE FROM symbol_embeddings")
    conn.execute("DELETE FROM symbols")
    conn.commit()
    _insert(conn, "func alpha()")
    # First warm up the in-memory cache via _search_vectors (_ensure_symbol_embeddings itself does not fill that cache)
    S._symbol_embeddings.clear()
    S._search_vectors(db, "warmup")
    rows_before = edge_rows()

    # Add a symbol and commit: the DB content and the file mtime change at the same time, while the process cache is still at the old state
    conn.execute(
        "INSERT INTO symbols (file_path,name,kind,signature) "
        "VALUES ('/p/b.swift','beta','var','var beta')"
    )
    conn.commit()
    S._search_vectors(db, "beta")  # do not clear _symbol_embeddings
    rows_after = edge_rows()
    check(
        "cache invalidated after a DB change and an embedding built for the new symbol",
        rows_after == rows_before + 1,
        f"edge table {rows_before} → {rows_after} —— no growth means it is still using the stale in-memory cache",
    )
    conn.close()


def check_note_cache_freshness(tmp: str) -> None:
    """B. Note cache invalidation decision invariant."""
    from agent_gateway.services.mcp import server_search as S

    root = os.path.join(tmp, "notes_project")
    deep = os.path.join(root, "deep", "nested")
    os.makedirs(deep, exist_ok=True)

    def write(path: str, marker: str) -> None:
        with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
            fh.write(f"# 标题\n\n{marker}" + "填充内容" * 40)

    write("README.md", "顶层标记")
    write("deep/nested/doc.md", "深层标记甲")
    S._ensure_note_cache(root)

    # B1: no change → cache hit
    conn, _, _ = S._ensure_note_cache(root)
    conn.close()

    # B2: change an existing deep file → must be detected.
    #     Key precondition: this operation does not change the project root mtime, exactly the
    #     scenario the old implementation missed.
    root_mtime_before = os.stat(root).st_mtime
    write("deep/nested/doc.md", "深层标记乙")
    root_mtime_unchanged = os.stat(root).st_mtime == root_mtime_before

    conn, db_path, _ = S._ensure_note_cache(root)
    hit = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_path LIKE '%doc.md' "
        "AND chunk_text LIKE '%深层标记乙%'"
    ).fetchone()[0]
    conn.close()
    check(
        "changes to a deep file are detected",
        hit > 0,
        "failure = reverted to using the directory mtime as a fast skip"
        + (
            " (this change indeed did not modify the root directory mtime)"
            if root_mtime_unchanged
            else ""
        ),
    )

    # B3: when the file shrinks, the old trailing chunk must disappear
    write("deep/nested/doc.md", "只剩一段" + "填充内容" * 10)
    conn, _, _ = S._ensure_note_cache(root)
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_path LIKE '%doc.md'"
    ).fetchone()[0]
    conn.close()
    check("old chunks cleared after the file shrinks", n == 1, f"{n} chunks left over")

    shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="utility-cache-invariants-")
    try:
        check_symbol_cache_key(tmp)
        check_note_cache_freshness(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print(f"❌ {len(_failures)} invariant(s) broken:")
        for f in _failures:
            print(f"   - {f}")
        return 1
    print("✅ all invariants passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
