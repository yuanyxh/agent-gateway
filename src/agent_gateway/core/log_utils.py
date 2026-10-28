"""Logging module shared by all Python subprocesses.
Usage:
    from agent_gateway.core.log_utils import setup_logging, write_error_log
    logger = setup_logging("server")       # → ~/.cache/utility/server.log
    logger = setup_logging("token_proxy", json_format=True)  # → JSON Lines
"""

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone

from agent_gateway.core.config import log_backup_count, log_max_size
from agent_gateway.core.paths import gateway_cache

_CACHE_BASE = str(gateway_cache())


def setup_logging(name: str, level=logging.INFO, json_format=False):
    os.makedirs(_CACHE_BASE, exist_ok=True)

    root = logging.getLogger(name)
    root.setLevel(level)
    if root.handlers:
        return root

    log_path = os.path.join(_CACHE_BASE, f"{name}.log")
    # Size rotation, matching Swift debug.log: <name>.log -> .1 -> .2, keeps at most N backups.
    fh = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=log_max_size(),
        backupCount=log_backup_count(),
        encoding="utf-8",
        delay=True,  # create the file on the first actual record, not at import time
    )

    if json_format:
        fh.setFormatter(logging.Formatter("%(message)s"))
    else:
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    root.addHandler(fh)

    # Silence third-party libraries to avoid noise
    for lib in ("httpx", "httpcore", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    return root


def write_error_log(log_path: str, entry: dict):
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
