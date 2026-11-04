"""YAML configuration source.

`agent-gateway.yaml` is the native file. A legacy host `config.yaml` is translated
by `config_compat` for one release, so an upgraded gateway still starts before the
host writes the native file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from agent_gateway.core import config_compat, env_compat, paths

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

logger = logging.getLogger("gateway.config_loader")

_NATIVE_NAME = "agent-gateway.yaml"
_LEGACY_NAME = "config.yaml"
_FORMAT_ENV = "GATEWAY_CONFIG_FORMAT"

#: (path, mtime_ns, size) -> parsed mapping. A config reload is picked up on the next read.
_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None
_warned_legacy = False


def config_file() -> Path:
    """`GATEWAY_CONFIG` wins, otherwise the native file, then a legacy file."""
    explicit = (env_compat.get("GATEWAY_CONFIG") or "").strip()
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    home = paths.gateway_home()
    native = home / _NATIVE_NAME
    if native.is_file():
        return native
    legacy = home / _LEGACY_NAME
    if legacy.is_file():
        return legacy
    return native


def _normalize(data: Any, target: Path) -> dict[str, Any]:
    global _warned_legacy
    if not isinstance(data, dict):
        return {}
    forced = (os.environ.get(_FORMAT_ENV) or "auto").strip().lower()
    shape = "legacy" if forced == "legacy" else config_compat.detect(data)
    if forced == "native":
        shape = "native"
    if shape != "legacy":
        return data
    if not _warned_legacy:
        _warned_legacy = True
        logger.warning(
            "legacy config shape in %s; write %s to drop the adapter",
            target,
            _NATIVE_NAME,
        )
    return config_compat.to_native(data)


def load(path: Path | None = None) -> dict[str, Any]:
    """Parsed config mapping; empty when the file is missing or unreadable."""
    global _cache
    target = path or config_file()
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
    parsed = _normalize(data, target)

    _cache = (key, parsed)
    return parsed


def value(dotted: str, default: Any = None) -> Any:
    """Read a dotted key (`llm.apiKey`); any missing level yields `default`."""
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
