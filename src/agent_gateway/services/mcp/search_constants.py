"""Configuration constants for project search and note retrieval."""

from __future__ import annotations

import importlib.util

from agent_gateway.core.log_utils import setup_logging
# ---------------------------------------------------------------------------
# Configuration (same as pre-split server.py; NOTE_*/PROJECT_MARKER_* are server-specific)
# ---------------------------------------------------------------------------

NOTE_DB_FILENAME = "note_vectors.sqlite"
NOTE_EXCLUDE_DIRS = frozenset(
    {
        ".build",
        "node_modules",
        "dist",
        ".git",
        ".utility",
        "Pods",
        ".venv",
        "__pycache__",
        ".svn",
        ".hg",
        ".gradle",
        "build",
        "DerivedData",
        "Utility.app",
    }
)
NOTE_CHUNK_MIN_SIZE = 50
NOTE_CHUNK_MAX_SIZE = 512
NOTE_CHUNK_OVERLAP = 48
NOTE_SIMILARITY_THRESHOLD = 0.35
#: CJK bigram coverage needed to keep a chunk whose vector score is below the
#: floor. Any single bigram matches something, so "> 0" admits noise.
NOTE_KEYWORD_FLOOR = 0.4
NOTE_MAX_CHUNKS_PER_PROJECT = 50000
NOTE_MAX_RESULTS = 20
NOTE_PREVIEW_LEN = 300
#: Project-relative prefixes excluded from note indexing. Eval datasets and
#: pytest caches are not project documentation and can dwarf real content.
NOTE_EXCLUDE_RELPATHS = ("tests/eval", ".pytest_cache")
#: Changed files embedded per commit during a rebuild, so a long run keeps
#: finished work and can log progress instead of committing once at the end.
NOTE_EMBED_FILES_PER_COMMIT = 8
PROJECT_MARKER_FILES = frozenset(
    {
        # JS/TS
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        # Swift/Apple
        "Package.swift",
        "Podfile",
        "Podfile.lock",
        # Go
        "go.mod",
        "go.sum",
        # Rust
        "Cargo.toml",
        # Java/Kotlin
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        # Python
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "Pipfile",
        # Others
        "composer.json",
        "Gemfile",
        "mix.exs",
    }
)
PROJECT_MARKER_MAX_DEPTH = 2
PROJECT_MARKER_FILE_SUFFIXES = (".csproj", ".sln")
PROJECT_MARKER_DIR_SUFFIXES = (".xcodeproj", ".xcworkspace")
# Number of texts fed to the embedder per call (ONNX inference is batched to avoid building an oversized batch at once)
_EMBED_BATCH_SIZE = 32
#: A query never embeds: one file takes seconds, so embedding in the request
#: thread is what made callers time out. The background prewarm owns this work.
NOTE_QUERY_EMBED_BUDGET = 0

logger = setup_logging("server")

_HAS_PATHSPEC = importlib.util.find_spec("pathspec") is not None
