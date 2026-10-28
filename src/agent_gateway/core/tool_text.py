"""Fixed-English MCP tool parameter descriptions.

Tool and parameter descriptions are always English; this layer is not tied to `UTILITY_LANG`.
Keys are semantic, so text shared by several tools is defined once.
"""

from __future__ import annotations

from agent_gateway.core.l10n import MissingText

MissingParamText = MissingText

PARAM_DESCRIPTIONS: dict[str, str] = {
    "param.memory_content": (
        "One fact. With background: line 1 [background], then [fact]; organize as "
        "scenario/decision/evolution and omit missing parts. Without background: state the "
        "fact directly; never fabricate one."
    ),
    "param.memories": "Facts to write; same semantics as remember's content.",
    "param.query": "Search query",
    "param.max_results": "Max results",
    "param.since_days": "Days back to look; default 3",
    "param.memory_ids": "Memory IDs",
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
    "param.search_keyword": (
        "Required. Search keyword; Chinese synonyms are expanded automatically"
    ),
    "param.file_path": "Optional. Restrict to a file path (fuzzy match)",
    "param.symbol_kind": "Optional. Restrict to a symbol kind (class/fun/val/...)",
    "param.subpath": "Optional subpath; returns its next level",
    "param.file_or_dir": "File or directory path",
    "param.dependency_direction": (
        "outgoing (what this path imports), incoming (what imports it), or both; "
        "default outgoing"
    ),
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
    "param.summarize_text": "Text to summarize; up to 50,000 characters",
    "param.summarize_instruction": "Optional instruction overriding the default summary style",
    "param.summarize_max_tokens": "Max tokens for the summary; default 600, max 4000",
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
