"""Translate the legacy host ``config.yaml`` shape into the gateway schema."""

from __future__ import annotations

from typing import Any

_NATIVE_KEYS = {"llm", "language", "logging", "server", "summary"}
_LEGACY_KEYS = {"ai", "general", "mcp", "modelProxy"}
_LEGACY_NESTED = {"poolTargetTopic", "maxShownPerCard", "autoShowEnabled"}


def detect(data: Any) -> str:
    """Return ``native`` or ``legacy`` from the top-level sections present."""
    if not isinstance(data, dict):
        return "native"
    if any(key in data for key in _NATIVE_KEYS):
        return "native"
    if any(key in data for key in _LEGACY_KEYS):
        return "legacy"
    knowledge = data.get("knowledge")
    if isinstance(knowledge, dict) and any(key in knowledge for key in _LEGACY_NESTED):
        return "legacy"
    return "native"


def _get(data: dict, dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def _put(target: dict, dotted: str, value: Any) -> None:
    if value is None:
        return
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def to_native(legacy: dict) -> dict:
    """Map every consumed legacy key; unknown UI-only keys are dropped."""
    native: dict[str, Any] = {}

    for new, old in (
        ("llm.apiKey", "ai.apiKey"),
        ("llm.baseURL", "ai.baseURL"),
        ("llm.model", "ai.model"),
        ("llm.timeout", "ai.llmTimeout"),
        ("language", "general.language"),
        ("logging.maxSize", "general.logMaxSize"),
        ("logging.backupCount", "general.logBackupCount"),
        ("server.mcp.enabled", "mcp.enabled"),
        ("server.mcp.port", "mcp.port"),
        ("server.mcp.synonyms", "mcp.synonyms"),
        ("server.mcp.toolDefaults", "mcp.toolDefaults"),
        ("notes.dir", "mcp.knowledgeBasePath"),
        ("notes.excludeDirs", "mcp.noteExcludeDirs"),
        ("notes.extensions", "mcp.noteExtensions"),
        ("notes.excludeTags", "mcp.excludeTags"),
        ("memory.qdrantPath", "mcp.qdrantPath"),
        ("memory.embedder.model", "mcp.embedderModel"),
        ("memory.embedder.cacheDir", "mcp.fastembedCacheDir"),
        ("memory.embedder.idleUnloadSeconds", "mcp.embedderIdleUnloadSeconds"),
        ("memory.protectImportance", "mcp.memoryProtectImportance"),
        ("memory.maxContentChars", "mcp.memoryMaxContentChars"),
        ("memory.maxSummaries", "mcp.memoryMaxSummaries"),
        ("memory.maxSummariesPerEpisode", "mcp.memoryMaxSummariesPerEpisode"),
        ("memory.rankSignal.recency", "mcp.rankSignalRecency"),
        ("memory.rankSignal.access", "mcp.rankSignalAccess"),
        ("memory.rankSignal.quality", "mcp.rankSignalQuality"),
        ("summary.enabled", "mcp.summaryEnabled"),
        ("summary.hour", "mcp.summaryHour"),
        ("knowledge.enabled", "knowledge.enabled"),
        ("knowledge.topics", "knowledge.topics"),
        ("knowledge.topicTarget", "knowledge.poolTargetTopic"),
        ("knowledge.maxFailStreak", "knowledge.maxFailStreak"),
        ("knowledge.retentionDays", "knowledge.retentionDays"),
        ("graph", "graph"),
        ("search", "search"),
    ):
        _put(native, new, _get(legacy, old))

    _put(native, "server.proxy", _get(legacy, "modelProxy"))
    dedup = _get(legacy, "modelProxy.skillDedupEnabled")
    if dedup is not None:
        _put(native, "server.proxy.toolOutputDedup", dedup)
        proxy = native.get("server", {}).get("proxy")
        if isinstance(proxy, dict):
            proxy.pop("skillDedupEnabled", None)
    return native
