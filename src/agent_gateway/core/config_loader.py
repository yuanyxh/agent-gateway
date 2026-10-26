"""YAML configuration source.

`config.yaml` is the single user-facing configuration file. The Swift host used to
parse it and inject every value as an env var; the Python side can now read it
directly, which is what `minod` needs when it runs without a Swift parent.

Precedence is decided in `config.py`: an explicit env var always wins, YAML is the
fallback. That keeps the existing env contract working while the two paths coexist.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

logger = logging.getLogger("utility.config_loader")

_CONFIG_NAME = "config.yaml"

#: (path, mtime_ns, size) -> parsed mapping. A config reload is picked up on the next read.
_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def config_file() -> Path | None:
    """Config path derived from the injected config root; never a hardcoded personal path."""
    root = os.environ.get("UTILITY_DATA_DIR", "").strip()
    if not root:
        return None
    return Path(os.path.expanduser(root)) / _CONFIG_NAME


def load(path: Path | None = None) -> dict[str, Any]:
    """Parsed config mapping; empty when the file is missing or unreadable."""
    global _cache
    target = path or config_file()
    if target is None:
        return {}
    try:
        stat = target.stat()
    except OSError:
        return {}

    key = (str(target), stat.st_mtime_ns, stat.st_size)
    if _cache is not None and _cache[0] == key:
        return _cache[1]

    data: Any = {}
    if yaml is None:
        logger.warning("PyYAML missing, cannot read %s", target)
    else:
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("config read failed: %s: %s", target, exc)
    if not isinstance(data, dict):
        data = {}

    _cache = (key, data)
    return data


def value(dotted: str, default: Any = None) -> Any:
    """Read a dotted key (`ai.apiKey`); any missing level yields `default`."""
    node: Any = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def reset_cache() -> None:
    """Drop the parsed cache (tests, explicit reload)."""
    global _cache
    _cache = None
