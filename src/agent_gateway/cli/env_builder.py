"""Script-specific env for a standalone run.

The Swift host builds these from its parsed config; `minod` has no Swift parent,
so it derives the same variables from agent-gateway.yaml. Only script-specific
values live here — the common config surface is read by `core.config` itself.

JSON shapes are snake_case and must match the Python consumers
(`proxy_provider`, `proxy_client`, `proxy_output_filter`, `synonyms`,
`tool_defaults`); they mirror the Swift serializers in `TokenProxyManager` /
`MCPProcessManager`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_gateway.core import config_loader, paths
from agent_gateway.core.paths import gateway_cache

MCP_BASE_PORT = 9001
PROXY_BASE_PORT = 8613
DEFAULT_CONTEXT_WARN = 300_000


def _mapping(dotted: str) -> dict[str, Any]:
    node = config_loader.value(dotted)
    return node if isinstance(node, dict) else {}


def _listing(dotted: str) -> list[Any]:
    node = config_loader.value(dotted)
    return node if isinstance(node, list) else []


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def config_root() -> Path:
    return paths.gateway_home()


def mcp_enabled() -> bool:
    return config_loader.value("server.mcp.enabled", False) is True


def proxy_enabled() -> bool:
    return config_loader.value("server.proxy.enabled", False) is True


def _expand(value: str) -> str:
    return os.path.expanduser(value) if value else ""


def mcp_environment(port: int) -> dict[str, str]:
    """`MCP_PORT` / `MEMORY_SERVER_URL` / storage paths / synonym and default tables."""
    root = config_root()
    env = {
        "MCP_PORT": str(port),
        "MEMORY_SERVER_URL": f"http://127.0.0.1:{port}",
        "GATEWAY_HOME": str(root),
    }

    qdrant = _expand(config_loader.value("memory.qdrantPath", ""))
    if not qdrant:
        qdrant = str(root / "data" / "qdrant")
    if qdrant:
        env["QDRANT_PATH"] = qdrant

    fastembed = _expand(config_loader.value("memory.embedder.cacheDir", ""))
    if not fastembed:
        fastembed = str(gateway_cache() / "fastembed")
    env["FASTEMBED_CACHE_PATH"] = fastembed

    embedder = config_loader.value("memory.embedder.model", "")
    if isinstance(embedder, str) and embedder:
        env["EMBEDDER_MODEL"] = embedder

    idle = config_loader.value("memory.embedder.idleUnloadSeconds", None)
    if isinstance(idle, (int, float)) and idle >= 0:
        env["EMBEDDER_IDLE_UNLOAD_SECONDS"] = str(int(idle))

    synonyms = _mapping("server.mcp.synonyms")
    if synonyms:
        env["MCP_SYNONYMS"] = _dumps(synonyms)

    defaults = _mapping("server.mcp.toolDefaults")
    if defaults:
        env["MCP_TOOL_DEFAULTS"] = _dumps(defaults)

    return env


def _providers() -> list[dict[str, Any]]:
    payload = []
    for item in _listing("server.proxy.providers"):
        if not isinstance(item, dict):
            continue
        payload.append(
            {
                "name": item.get("name", ""),
                "url": item.get("url", ""),
                "api_format": item.get("apiFormat", ""),
                "api_key": item.get("apiKey", ""),
                "models": list(item.get("models") or []),
            }
        )
    return payload


def _client_accel() -> dict[str, Any] | None:
    accel = _mapping("server.proxy.clientAccel")
    if not accel:
        return None
    clients = []
    for item in accel.get("clients") or []:
        if not isinstance(item, dict):
            continue
        clients.append(
            {
                "name": item.get("name", ""),
                "user_agent_contains": list(item.get("userAgentContains") or []),
                "header_present": list(item.get("headerPresent") or []),
            }
        )
    return {"enabled": bool(accel.get("enabled", True)), "clients": clients}


def _client_text() -> dict[str, Any] | None:
    text = _mapping("server.proxy.clientText")
    if not text:
        return None
    return {
        "continuation_prefixes": list(text.get("continuationPrefixes") or []),
        "summary_prefixes": list(text.get("summaryPrefixes") or []),
        "injected_tags": list(text.get("injectedTags") or []),
    }


def _output_filter() -> dict[str, Any] | None:
    flt = _mapping("server.proxy.outputFilter")
    if not flt:
        return None
    rules = []
    for item in flt.get("rules") or []:
        if not isinstance(item, dict):
            continue
        rules.append(
            {
                "command": item.get("command", ""),
                "subcommand": item.get("subcommand", ""),
                "max_bytes": int(item.get("maxBytes", 0) or 0),
                "drop_lines": list(item.get("dropLines") or []),
                "keep_lines": list(item.get("keepLines") or []),
            }
        )
    return {
        "enabled": bool(flt.get("enabled", True)),
        "max_bytes": int(flt.get("maxBytes", 0) or 0),
        "format_compress": bool(flt.get("formatCompress", True)),
        "fold_repeated_lines": bool(flt.get("foldRepeatedLines", False)),
        "bypass_tools": list(flt.get("bypassTools") or []),
        "shell_tools": list(flt.get("shellTools") or []),
        "rules": rules,
    }


def proxy_environment(port: int) -> dict[str, str]:
    """`MODEL_PROXY_*` set; the common AI config is read by `config.py` from YAML."""
    env = {"MODEL_PROXY_PORT": str(port), "GATEWAY_HOME": str(config_root())}

    providers = _providers()
    if providers:
        env["MODEL_PROXY_PROVIDERS"] = _dumps(providers)
    else:
        env["MODEL_PROXY_URL"] = str(config_loader.value("server.proxy.url", "") or "")

    accel = _client_accel()
    if accel is not None:
        env["MODEL_PROXY_CLIENT_ACCEL"] = _dumps(accel)
    text = _client_text()
    if text is not None:
        env["MODEL_PROXY_CLIENT_TEXT"] = _dumps(text)

    dedup = config_loader.value("server.proxy.toolOutputDedup", True)
    env["MODEL_PROXY_TOOL_OUTPUT_DEDUP"] = "0" if dedup is False else "1"

    output_filter = _output_filter()
    if output_filter is not None:
        env["MODEL_PROXY_OUTPUT_FILTER"] = _dumps(output_filter)

    annotation = config_loader.value("server.proxy.responseAnnotationEnabled", True)
    env["MODEL_PROXY_RESPONSE_NOTE"] = "0" if annotation is False else "1"

    warn = config_loader.value("server.proxy.contextWarnTokens", DEFAULT_CONTEXT_WARN)
    if not isinstance(warn, int):
        warn = DEFAULT_CONTEXT_WARN
    env["MODEL_PROXY_CONTEXT_WARN"] = str(warn)
    return env
