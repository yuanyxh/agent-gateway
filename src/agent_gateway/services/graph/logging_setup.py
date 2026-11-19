"""Single owner of the graph logger.

Analysis and the indexer both log to `"graph"`, and whichever module imports
first wins the handler. Keeping the path decision here means every entry point
lands on the same file instead of the first one falling back to the cache.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from agent_gateway.core import project_paths
from agent_gateway.core.log_utils import setup_logging


def registered_root() -> str:
    """Project root passed by the host as argv[1]; empty for other entry points."""
    if len(sys.argv) < 2:
        return ""
    candidate = sys.argv[1].strip()
    if not candidate:
        return ""
    root = os.path.realpath(candidate)
    return root if os.path.isdir(root) else ""


def cli_root() -> str:
    """Project root from `graph ... --root` when the CLI drives the indexer."""
    argv = sys.argv[1:]
    if "--root" not in argv:
        return ""
    index = argv.index("--root")
    if index + 1 >= len(argv):
        return ""
    root = os.path.realpath(argv[index + 1].strip() or ".")
    return root if os.path.isdir(root) else ""


def log_file() -> str | None:
    """Per-project log path; None keeps the shared cache location."""
    root = registered_root() or cli_root()
    if not root:
        return None
    try:
        return str(project_paths.ensure_state_dir(root) / "graph.log")
    except OSError:
        return None


def graph_logger():
    """Return the shared graph logger, creating the per-project handler once."""
    return setup_logging("graph", json_format=True, path=log_file())


def log_partial_parse(path: str) -> None:
    """Emit a JSONL warning so every graph.log line carries a timestamp."""
    logging.getLogger("graph").warning(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "op": "partial_parse",
                "file": path,
                "error": "tree-sitter error nodes",
            },
            ensure_ascii=False,
        )
    )
