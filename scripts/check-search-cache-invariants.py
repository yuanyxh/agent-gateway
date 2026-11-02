#!/usr/bin/env python3
"""Check the note-cache invalidation invariant (regression gate for the 2026-09-11 fix).

Background: the note cache once used the "project root directory mtime" to decide
invalidation, but that only changes when a direct child is added or removed, so
editing an existing deep file was judged "unchanged" and stale search results were
returned. It is an implicit contract that looks right and fails silently.

Usage (run from the project root):
    PYTHONPATH=src/agent_gateway \\
    FASTEMBED_CACHE_PATH=~/.cache/utility/fastembed \\
    ~/.config/utility/mcp-venv/bin/python3 scripts/check-search-cache-invariants.py

Exit code 0 = passed; 1 = the invariant was broken. It only uses a temp directory
and does not touch real data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def check_note_cache_freshness(tmp: str) -> None:
    """Note cache invalidation decision invariant."""
    from agent_gateway.services.mcp.search_notes_cache import _ensure_note_cache

    root = os.path.join(tmp, "notes_project")
    deep = os.path.join(root, "deep", "nested")
    os.makedirs(deep, exist_ok=True)

    def write(path: str, marker: str) -> None:
        with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
            fh.write(f"# 标题\n\n{marker}" + "填充内容" * 40)

    write("README.md", "顶层标记")
    write("deep/nested/doc.md", "深层标记甲")
    _ensure_note_cache(root)[0].close()

    # B1: no change → cache hit
    _ensure_note_cache(root)[0].close()

    # B2: change an existing deep file → must be detected.
    #     Key precondition: this operation does not change the project root mtime,
    #     exactly the scenario the old implementation missed.
    root_mtime_before = os.stat(root).st_mtime
    write("deep/nested/doc.md", "深层标记乙")
    root_mtime_unchanged = os.stat(root).st_mtime == root_mtime_before

    conn, _, _ = _ensure_note_cache(root)
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
    conn, _, _ = _ensure_note_cache(root)
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_path LIKE '%doc.md'"
    ).fetchone()[0]
    conn.close()
    check("old chunks cleared after the file shrinks", n == 1, f"{n} chunks left over")

    shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="utility-cache-invariants-")
    try:
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
