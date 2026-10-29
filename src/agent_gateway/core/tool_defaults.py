"""Tool argument defaults table: tool name → args name → default value.

Built-in defaults (`TOOL_ARG_DEFAULTS`) + user additions (config.yaml's
`mcp.toolDefaults`, injected via env `MCP_TOOL_DEFAULTS`), merged with the same
semantics as synonyms -- **same-name override**.

It is a separate table because the schema's `default` declaration and the
handler's runtime fallback are two independent derivations; writing the literal
in both places drifts into "schema says 6, actually takes 8" -- when a client
omits the arg per the declaration, the count it gets differs from what it saw
(recall hit this pit with 10→8 last time). These defaults directly determine the
return volume (token consumption) of each call, so they are made configurable:
change the config and it takes effect immediately, no code change.
"""

from __future__ import annotations

import json
import os

from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("tool_defaults")

#: env name for user-custom defaults (cross-language contract; the Swift side's
#: `MCPProcessManager.toolDefaultsEnvKey` has the same value, cross-asserted by
#: `scripts/check-router-config-contract.py`).
TOOL_DEFAULTS_ENV_KEY = "MCP_TOOL_DEFAULTS"

#: Built-in defaults. Tools/args not listed here do not use this table.
TOOL_ARG_DEFAULTS: dict[str, dict[str, int]] = {
    "recall": {"limit": 6},
    "project_search": {"limit": 15},
    "project_structure": {"limit": 15},
    "project_notes": {"limit": 5},
    "websearch_engine": {"limit": 5, "fetch_top": 0, "max_chars": 6000},
    "read_url": {"max_chars": 0},
}

_defaults: dict[str, dict[str, int]] | None = None


def load_defaults() -> dict[str, dict[str, int]]:
    """Built-in defaults + user additions (same-name override). Parsed and cached on first call; the returned table is read-only."""
    global _defaults
    if _defaults is not None:
        return _defaults

    merged = {tool: dict(args) for tool, args in TOOL_ARG_DEFAULTS.items()}
    raw = os.environ.get(TOOL_DEFAULTS_ENV_KEY, "").strip()
    if raw:
        # A broken config must not fail silently: the symptom is "my configured defaults did not take effect", indistinguishable from "this arg was never configured"
        try:
            user = json.loads(raw)
        except ValueError as e:
            logger.error(
                f"{TOOL_DEFAULTS_ENV_KEY} is not valid JSON, ignoring user config: {e}"
            )
            user = None
        if isinstance(user, dict):
            for tool, args in user.items():
                if not isinstance(args, dict):
                    logger.error(f"toolDefaults[{tool}] is not an object, ignored")
                    continue
                bucket = merged.setdefault(str(tool), {})
                for key, value in args.items():
                    try:
                        bucket[str(key)] = int(value)
                    except (TypeError, ValueError):
                        logger.error(
                            f"toolDefaults[{tool}][{key}] is not an integer, ignored"
                        )
        elif user is not None:
            logger.error(
                f"{TOOL_DEFAULTS_ENV_KEY} is not an object (got {type(user).__name__}), ignored"
            )

    _defaults = merged
    return _defaults


def arg_default(tool: str, arg: str) -> int:
    """Look up an argument default from the table. A missing entry raises KeyError -- config errors should surface during development; a silent fallback makes "present in table but not effective" indistinguishable from "not configured in table"."""
    return load_defaults()[tool][arg]
