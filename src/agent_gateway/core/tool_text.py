"""Fixed-English MCP tool parameter descriptions.

Tool and parameter descriptions are always English; this layer is not tied to `UTILITY_LANG`.
Keys are semantic, so text shared by several tools is defined once.
"""

from __future__ import annotations

from agent_gateway.core.l10n import MissingText

MissingParamText = MissingText

PARAM_DESCRIPTIONS: dict[str, str] = {
    "param.memory_content": (
        "One fact, stated directly. Do not include background, scenario, or reasons; "
        "put those in context."
    ),
    "param.memory_context": (
        "Why the fact matters, when it applies, and what it relates to. Embedded "
        "together with content, so it is searchable."
    ),
    "param.memory_type": (
        "Optional memory type, e.g. decision, preference, bug, convention"
    ),
    "param.memory_importance": (
        "Optional 1-10 importance. Contradiction checks skip values at or above "
        "the configured protect threshold."
    ),
    "param.memory_protected": (
        "Optional flag; protected memories are never archived or retired by the "
        "weekly maintenance pass."
    ),
    "param.source_files": "Optional file paths this fact is about",
    "param.source_url": "Optional URL this fact came from",
    "param.memories": (
        "Facts to write; same semantics as remember's content and context."
    ),
    "param.keywords": (
        "Required. 2-5 keywords or short phrases; the main query. "
        "Chinese and English are supported"
    ),
    "param.query": (
        "Optional. One sentence describing what you are looking for; searched in a "
        "second pass and merged with the keyword results"
    ),
    "param.max_results": "Max results",
    "param.since_days": "Days back to look; default 3",
    "param.offset": "Number of results to skip before returning; default 0",
    "param.catch_up_max_chars": (
        "Max JSON characters returned; default 24000, 0 = unlimited"
    ),
    "param.recall_max_chars": (
        "Max JSON characters returned; default 24000, 0 = unlimited. Related rows "
        "are dropped when the primary results fill the budget"
    ),
    "param.memory_ids": "Memory IDs",
    "param.memory_node_id": (
        "Memory id from recall, remember, or memory_graph; a cold point id is accepted"
    ),
    "param.memory_edge": (
        "Edge to follow: supersedes (version chain), episode (same event), "
        "derived_from (cold sources), disputes, relates, mentions"
    ),
    "param.memory_direction": (
        "up follows to_id -> from_id, down follows from_id -> to_id, both (default)"
    ),
    "param.memory_id": "Memory ID to delete",
    "param.point_id": "Optional point ID",
    "param.layer": "Layer to delete from; default hot",
    "param.layers": "Layers to search: hot, cold, or both; default both",
    "param.include_superseded": (
        "Include memories retired by a newer revision (history); deleted memories "
        "are never returned"
    ),
    "param.bit": "Bit index; 0 = summarized, 1 = retired",
    "param.bit_value": "1 = set, 0 = reset",
    "param.db_path": "Required. Absolute path to .utility/deps.sqlite",
    "param.graph_db_path": (
        "Required. Absolute path to .utility/graph.sqlite; "
        "the agent shell exposes it as $UTILITY_GRAPH_DB"
    ),
    "param.graph_query": (
        "Optional. Symbol name or file path to locate; omit to return a graph overview"
    ),
    "param.graph_target": (
        "Required. Symbol name, Class.method, file path, or file:<path> to analyze"
    ),
    "param.graph_direction": (
        "callers, callees, or both; callers means incoming references"
    ),
    "param.graph_depth": "Max hops to traverse; default 2",
    "param.graph_include_stale": (
        "Include edges marked stale after a file changed; default false"
    ),
    "param.file_path": "Optional. Restrict to a file path (fuzzy match)",
    "param.symbol_kind": "Optional. Restrict to a symbol kind (class/fun/val/...)",
    "param.subpath": "Optional subpath; returns its next level",
    "param.notes_keyword": "Search keyword; natural language is supported",
    "param.project_dir": "Absolute path to the project directory",
    "param.parent_name": "Registered tool name, e.g. check/write/think",
    "param.resource_path": (
        "Path relative to the tool directory, e.g. references/durable-context.md"
    ),
    "param.websearch_query": "Search query; Chinese and English are supported",
    "param.websearch_limit": "Max results; default 5",
    "param.fetch_top": "Read the top N result pages as Markdown; default 0, max 3",
    "param.max_chars": "Max Markdown chars per page; default 6000",
    "param.engines": "Optional comma-separated engines; default is a language-aware cascade",
    "param.read_url": "Absolute http/https URL to read",
    "param.read_max_chars": "Max Markdown chars to return; 0 = unlimited (default)",
    "param.read_use_proxy": (
        "Allow third-party readers (defuddle.md, r.jina.ai) when local extraction "
        "fails or is a login shell; only for public, non-sensitive URLs"
    ),
}


def param_text(key: str) -> str:
    try:
        return PARAM_DESCRIPTIONS[key]
    except KeyError:
        raise MissingParamText(f"unregistered parameter text key: {key!r}") from None
