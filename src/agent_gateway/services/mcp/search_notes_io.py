"""Markdown scanning, chunking, and ignore rules."""

from __future__ import annotations

import os


from agent_gateway.services.mcp.search_constants import (
    NOTE_CHUNK_MAX_SIZE,
    NOTE_CHUNK_MIN_SIZE,
    NOTE_CHUNK_OVERLAP,
    NOTE_EXCLUDE_DIRS,
    NOTE_EXCLUDE_RELPATHS,
    PROJECT_MARKER_DIR_SUFFIXES,
    PROJECT_MARKER_FILES,
    PROJECT_MARKER_FILE_SUFFIXES,
    PROJECT_MARKER_MAX_DEPTH,
    _HAS_PATHSPEC,
    logger,
)

try:
    import pathspec
except ImportError:
    pathspec = None


def _load_gitignore(project_path: str):
    """Load .gitignore patterns as a PathSpec (or None)."""
    gi = os.path.join(project_path, ".gitignore")
    if not os.path.isfile(gi):
        return None
    try:
        with open(gi, "r") as f:
            spec = pathspec.PathSpec.from_lines(
                pathspec.patterns.GitIgnoreSpecPattern, f
            )
        return spec
    except Exception:
        return None


def _is_ignored(abs_path: str, ignore_spec, project_root: str, is_dir: bool) -> bool:
    """Check if a path is ignored by .gitignore spec."""
    if ignore_spec is None:
        return False
    try:
        rel = os.path.relpath(abs_path, project_root)
        return ignore_spec.match_file(rel) or (
            is_dir and ignore_spec.match_file(rel + "/")
        )
    except Exception:
        return False


def _is_excluded_relpath(abs_path: str, project_root: str) -> bool:
    rel = os.path.relpath(abs_path, project_root).replace(os.sep, "/")
    return any(rel == p or rel.startswith(p + "/") for p in NOTE_EXCLUDE_RELPATHS)


def _scan_md_files(project_path: str) -> list[dict]:
    """Recursively scan .md files under project_path, respecting exclusions."""
    ignore_spec = _load_gitignore(project_path)
    files = []
    try:
        for root, dirs, names in os.walk(project_path, followlinks=False):
            # In-place pruning of excluded directories
            dirs[:] = [
                d
                for d in dirs
                if d not in NOTE_EXCLUDE_DIRS
                and not _is_excluded_relpath(os.path.join(root, d), project_path)
            ]
            # Also check .gitignore on remaining dirs when pathspec available
            if _HAS_PATHSPEC and ignore_spec is not None and root != project_path:
                dirs[:] = [
                    d
                    for d in dirs
                    if not _is_ignored(
                        os.path.join(root, d), ignore_spec, project_path, is_dir=True
                    )
                ]

            for name in names:
                if not name.endswith(".md"):
                    continue
                fpath = os.path.join(root, name)
                if _is_excluded_relpath(fpath, project_path):
                    continue
                if _is_ignored(fpath, ignore_spec, project_path, is_dir=False):
                    continue
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                files.append({"path": fpath, "mtime": st.st_mtime, "size": st.st_size})
    except Exception as e:
        logger.warning(f"[project_notes] scan error: {e}")
    return files


def is_dev_project(project_path: str) -> bool:
    """Shallow-scan for project marker files.

    A pure document repo (only .md + .git) has none of these markers, so note
    search uses keyword-only mode and does not create a vector cache there.
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


def _chunk_md_text(text: str, file_path: str) -> list[dict]:
    """Split markdown text into chunks, merging short paragraphs.

    Dropping every paragraph under NOTE_CHUNK_MIN_SIZE makes rule-list files
    invisible: their content is a sequence of short bullets, so the whole file
    yields zero chunks. Consecutive short paragraphs are accumulated instead.
    """
    chunks: list[dict] = []
    paragraphs = text.split("\n\n")
    line_count = 1
    buffer: list[str] = []
    buffer_start = 1
    buffer_len = 0

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        chunks.append(
            {
                "file_path": file_path,
                "chunk_index": 0,
                "chunk_text": "\n\n".join(buffer),
                "line_start": buffer_start,
            }
        )
        buffer = []
        buffer_len = 0

    for para in paragraphs:
        stripped = para.strip()
        start_line = line_count
        line_count += para.count("\n") + 1
        if not stripped:
            continue

        if len(stripped) > NOTE_CHUNK_MAX_SIZE:
            flush()
            chunks.extend(_split_long_text(stripped, file_path, start_line))
            continue

        if buffer and buffer_len + len(stripped) + 2 > NOTE_CHUNK_MAX_SIZE:
            flush()
        if not buffer:
            buffer_start = start_line
        buffer.append(stripped)
        buffer_len += len(stripped) + 2
        if buffer_len >= NOTE_CHUNK_MIN_SIZE:
            flush()

    flush()

    for idx, chunk in enumerate(chunks):
        chunk["chunk_index"] = idx
    return chunks


def _split_long_text(text: str, file_path: str, base_line: int) -> list[dict]:
    """Split a long paragraph into overlapping chunks by line."""
    lines = text.split("\n")
    chunks = []
    current_lines: list[str] = []
    current_len = 0
    line_offset = 0

    for idx, line in enumerate(lines):
        current_lines.append(line)
        current_len += len(line) + 1  # +1 for the newline
        if current_len >= NOTE_CHUNK_MAX_SIZE:
            chunk_text = "\n".join(current_lines)
            chunks.append(
                {
                    "file_path": file_path,
                    "chunk_index": len(chunks),
                    "chunk_text": chunk_text,
                    "line_start": base_line + line_offset,
                }
            )
            # Rewind for overlap: drop lines until we're below overlap threshold
            overlap_len = 0
            rewound: list[str] = []
            for line in reversed(current_lines):
                if overlap_len >= NOTE_CHUNK_OVERLAP and rewound:
                    break
                rewound.insert(0, line)
                overlap_len += len(line) + 1
            current_lines = rewound
            current_len = overlap_len
            line_offset = idx + 1 - len(current_lines)

    if current_lines:
        chunk_text = "\n".join(current_lines)
        chunks.append(
            {
                "file_path": file_path,
                "chunk_index": len(chunks),
                "chunk_text": chunk_text,
                "line_start": base_line + line_offset,
            }
        )

    return chunks
