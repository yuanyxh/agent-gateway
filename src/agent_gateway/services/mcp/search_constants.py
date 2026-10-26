"""Configuration constants for project search and note retrieval."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

import numpy as np

from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.synonyms import load_synonyms
from agent_gateway.core.tool_defaults import arg_default
from agent_gateway.services.mcp.server_embed import _get_shared_embedder, embed_query
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
NOTE_MAX_CHUNKS_PER_PROJECT = 50000
NOTE_MAX_RESULTS = 20
NOTE_PREVIEW_LEN = 300
#: Default result limit for the project tools (authoritative source is
#: `tool_defaults.arg_default`, config surface is config.yaml `mcp.toolDefaults`).
#: Function signatures reference it so the handler doesn't keep a second copy of the
#: literal and drift.
DEFAULT_PROJECT_SEARCH_LIMIT = arg_default("project_search", "limit")
DEFAULT_PROJECT_STRUCTURE_LIMIT = arg_default("project_structure", "limit")
DEFAULT_PROJECT_DEPENDENCIES_LIMIT = arg_default("project_dependencies", "limit")
DEFAULT_PROJECT_DEPENDENCIES_DIRECTION = "outgoing"
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
_MAX_VECTOR_CACHE = 3
_MAX_VECTOR_ROWS = 50000
# Number of texts fed to the embedder per call (ONNX inference is batched to avoid building an oversized batch at once)
_EMBED_BATCH_SIZE = 32
_symbol_embeddings: dict[str, tuple[list[dict], np.ndarray | None]] = {}
_note_cache_lock = threading.Lock()

logger = setup_logging("server")

_HAS_PATHSPEC = False
try:
    import pathspec

    _HAS_PATHSPEC = True
except ImportError:
    pass
