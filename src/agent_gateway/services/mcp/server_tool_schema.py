"""MCP tool schema: names, descriptions, and input schemas.

Split out of server.py so the schema can be built and diffed without booting the
memory layer. Defaults shared with the handlers live here too: the schema's
`default` and the handler fallback must come from the same source.
"""

from __future__ import annotations

from mcp.types import Tool

from agent_gateway.services.mcp.graph_tools import (
    IMPACT_DEFAULT_DEPTH,
    IMPACT_DEFAULT_LIMIT,
    LOOKUP_DEFAULT_LIMIT,
)
from agent_gateway.services.summarize.text import DEFAULT_MAX_TOKENS
from agent_gateway.core.tool_defaults import arg_default
from agent_gateway.core.tool_text import param_text

#: Default result limit for recall. Authoritative source is
#: `tool_defaults.arg_default` (config surface: config.yaml `mcp.toolDefaults`); the
#: tool schema's default and the runtime fallback both use this constant, avoiding two
#: copies of the literal drifting into "schema declares 6, actually takes 8".
DEFAULT_RECALL_LIMIT = arg_default("recall", "limit")

#: Default result limit for project_notes. Same as above: the schema's default and the
#: handler fallback share it; previously both wrote the literal 5, and going through the
#: same table as the other tools leaves no room for drift.
DEFAULT_PROJECT_NOTES_LIMIT = arg_default("project_notes", "limit")

DEFAULT_WEBSEARCH_LIMIT = max(1, min(arg_default("websearch_engine", "limit"), 10))
DEFAULT_WEBSEARCH_FETCH_TOP = max(
    0, min(arg_default("websearch_engine", "fetch_top"), 3)
)
DEFAULT_WEBSEARCH_MAX_CHARS = max(
    1000, min(arg_default("websearch_engine", "max_chars"), 10000)
)
DEFAULT_READ_MAX_CHARS = max(0, arg_default("read_url", "max_chars"))
DEFAULT_CATCH_UP_LIMIT = max(1, arg_default("catch_up", "limit"))
DEFAULT_CATCH_UP_MAX_CHARS = max(0, arg_default("catch_up", "max_chars"))
DEFAULT_GRAPH_LIMIT = 50
#: recall shares catch_up's budget so both tools bound their response the same way.
DEFAULT_RECALL_MAX_CHARS = DEFAULT_CATCH_UP_MAX_CHARS
DEFAULT_SUMMARIZE_MAX_TOKENS = DEFAULT_MAX_TOKENS

#: One batch item. Shared by build_tools and the registered tool signature so the
#: per-item descriptions and defaults stay single-sourced.
MEMORY_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": param_text("param.memory_content"),
        },
        "context": {
            "type": "string",
            "description": param_text("param.memory_context"),
        },
        "type": {
            "type": "string",
            "description": param_text("param.memory_type"),
        },
        "source_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": param_text("param.source_files"),
        },
        "source_url": {
            "type": "string",
            "description": param_text("param.source_url"),
        },
        "agent_id": {
            "type": "string",
            "default": "unknown",
            "description": "Required when visibility=agent-specific.",
        },
        "visibility": {
            "type": "string",
            "enum": ["all", "agent-specific", "user-only"],
            "default": "all",
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "default": 1.0},
        "importance": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 5,
            "description": param_text("param.memory_importance"),
        },
        "protected": {
            "type": "boolean",
            "default": False,
            "description": param_text("param.memory_protected"),
        },
        "supersedes": {"type": "array", "items": {"type": "string"}},
        "episode_id": {
            "type": "string",
            "description": "Optional group key; memories sharing it are recalled together.",
        },
    },
    "required": ["content"],
}

#: Fixed English tool descriptions; parameter guidance lives in `tool_text.PARAM_DESCRIPTIONS`.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "remember": "Trigger: user says remember/record, a bug is fixed, an insight is learned, or an architecture design changes. Writes one fact to shared memory. content holds only the fact; context holds why it matters and when it applies. Pass supersedes with the id of the memory this fact replaces; the old entry is retired, not deleted.",
    "remember_batch": "Batch version of remember; prefer it for multiple facts to cut calls.",
    "recall": "Trigger: user says look up/search/find that earlier one, or refers to history or existing information. Searches shared memory, knowledge base, and notes. Results below the relevance floor are omitted; same-episode context is returned in related.",
    "catch_up": "Trigger: user says just now/last time/previously/recently, or asks what was done, the progress, or what remains unresolved. Do not substitute git log.",
    "forget": "Trigger: user asks to delete a memory, or a memory is wrong/outdated. Deletes it permanently.",
    "project_lookup": "Locate a symbol or file and return its definition with one-hop caller context. Prefer over grep for definition lookup. Omit query for a graph overview (files, symbols, edges, languages).",
    "project_impact": "Find callers or callees of a symbol with multi-hop traversal. Prefer over grep to answer what a change affects.",
    "project_notes": "Trigger: user asks to find a project's README, design doc, plan, or TODO without knowing the file name. Searches local project Markdown by meaning, not just exact keywords.",
    "tool_resource": "Load a file under references or a script under scripts when instructed.",
    "set_memory_processed": "Set or reset the processed bit of memories. Idempotent.",
    "memory_get": "Trigger: you have a memory id from recall or remember and need its full body, lifecycle status, or an archived entry.",
    "memory_graph": "Trigger: you need the version chain, episode group, contradictions, or source chain around one memory id. Follows edges until the chain ends.",
    "websearch_engine": "Trigger: user says web search/search online/look up/latest info. Returns result links and, when fetch_top>0, Markdown text of top pages.",
    "read_url": "Trigger: user says read this link, check this URL, fetch this page, 看这个链接/读一下/抓取网页. when a link needs to be read, returns the page as Markdown.",
}


def build_tools() -> list[Tool]:
    tools = [
        Tool(
            name="remember",
            description=TOOL_DESCRIPTIONS["remember"],
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": param_text("param.memory_content"),
                    },
                    "context": {
                        "type": "string",
                        "description": param_text("param.memory_context"),
                    },
                    "type": {
                        "type": "string",
                        "description": param_text("param.memory_type"),
                    },
                    "source_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": param_text("param.source_files"),
                    },
                    "source_url": {
                        "type": "string",
                        "description": param_text("param.source_url"),
                    },
                    "agent_id": {
                        "type": "string",
                        "default": "unknown",
                        "description": "Required when visibility=agent-specific.",
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["all", "agent-specific", "user-only"],
                        "default": "all",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "default": 1.0},
                    "importance": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                        "description": param_text("param.memory_importance"),
                    },
                    "protected": {
                        "type": "boolean",
                        "default": False,
                        "description": param_text("param.memory_protected"),
                    },
                    "supersedes": {"type": "array", "items": {"type": "string"}},
                    "disputed_by": {"type": "array", "items": {"type": "string"}},
                    "episode_id": {
                        "type": "string",
                        "description": "Optional group key; memories sharing it are recalled together.",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="remember_batch",
            description=TOOL_DESCRIPTIONS["remember_batch"],
            inputSchema={
                "type": "object",
                "properties": {
                    "memories": {
                        "type": "array",
                        "description": param_text("param.memories"),
                        "items": MEMORY_ITEM_SCHEMA,
                    },
                },
                "required": ["memories"],
            },
        ),
        Tool(
            name="recall",
            description=TOOL_DESCRIPTIONS["recall"],
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": param_text("param.keywords"),
                    },
                    "query": {
                        "type": "string",
                        "description": param_text("param.query"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_RECALL_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": DEFAULT_RECALL_MAX_CHARS,
                        "description": param_text("param.recall_max_chars"),
                    },
                    "include_archived": {"type": "boolean", "default": False},
                    "include_superseded": {
                        "type": "boolean",
                        "default": False,
                        "description": param_text("param.include_superseded"),
                    },
                    "layers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["hot", "cold"]},
                        "description": param_text("param.layers"),
                    },
                    "agent_id": {
                        "type": "string",
                        "default": "unknown",
                        "description": "Required when visibility=agent-specific.",
                    },
                },
                "required": ["keywords"],
            },
        ),
        Tool(
            name="catch_up",
            description=TOOL_DESCRIPTIONS["catch_up"],
            inputSchema={
                "type": "object",
                "properties": {
                    "since_days": {
                        "type": "number",
                        "description": param_text("param.since_days"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_CATCH_UP_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": param_text("param.offset"),
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": DEFAULT_CATCH_UP_MAX_CHARS,
                        "description": param_text("param.catch_up_max_chars"),
                    },
                    "include_superseded": {
                        "type": "boolean",
                        "default": False,
                        "description": param_text("param.include_superseded"),
                    },
                    "agent_id": {
                        "type": "string",
                        "default": "unknown",
                        "description": "Required when visibility=agent-specific.",
                    },
                },
            },
        ),
        Tool(
            name="forget",
            description=TOOL_DESCRIPTIONS["forget"],
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": param_text("param.memory_id"),
                    },
                    "point_id": {
                        "type": "string",
                        "description": param_text("param.point_id"),
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["hot", "cold", "all"],
                        "default": "hot",
                        "description": param_text("param.layer"),
                    },
                },
            },
        ),
        Tool(
            name="set_memory_processed",
            description=TOOL_DESCRIPTIONS["set_memory_processed"],
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": param_text("param.memory_ids"),
                    },
                    "bit": {
                        "type": "integer",
                        "default": 0,
                        "description": param_text("param.bit"),
                    },
                    "value": {
                        "type": "integer",
                        "default": 1,
                        "description": param_text("param.bit_value"),
                    },
                },
                "required": ["memory_ids"],
            },
        ),
        Tool(
            name="memory_get",
            description=TOOL_DESCRIPTIONS["memory_get"],
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": param_text("param.memory_node_id"),
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="memory_graph",
            description=TOOL_DESCRIPTIONS["memory_graph"],
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": param_text("param.memory_node_id"),
                    },
                    "edge": {
                        "type": "string",
                        "enum": [
                            "supersedes",
                            "episode",
                            "derived_from",
                            "disputes",
                            "relates",
                            "mentions",
                        ],
                        "default": "supersedes",
                        "description": param_text("param.memory_edge"),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "both"],
                        "default": "both",
                        "description": param_text("param.memory_direction"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_GRAPH_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                },
                "required": ["node_id"],
            },
        ),
        # ── Project tools ──
        Tool(
            name="project_lookup",
            description=TOOL_DESCRIPTIONS["project_lookup"],
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": param_text("param.project_dir"),
                    },
                    "query": {
                        "type": "string",
                        "description": param_text("param.graph_query"),
                    },
                    "kind": {
                        "type": "string",
                        "description": param_text("param.symbol_kind"),
                    },
                    "file_path": {
                        "type": "string",
                        "description": param_text("param.file_path"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": LOOKUP_DEFAULT_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="project_impact",
            description=TOOL_DESCRIPTIONS["project_impact"],
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": param_text("param.project_dir"),
                    },
                    "target": {
                        "type": "string",
                        "description": param_text("param.graph_target"),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["callers", "callees", "both"],
                        "default": "callers",
                        "description": param_text("param.graph_direction"),
                    },
                    "depth": {
                        "type": "integer",
                        "default": IMPACT_DEFAULT_DEPTH,
                        "description": param_text("param.graph_depth"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": IMPACT_DEFAULT_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                    "includeStale": {
                        "type": "boolean",
                        "default": False,
                        "description": param_text("param.graph_include_stale"),
                    },
                },
                "required": ["path", "target"],
            },
        ),
        Tool(
            name="project_notes",
            description=TOOL_DESCRIPTIONS["project_notes"],
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": param_text("param.notes_keyword"),
                    },
                    "path": {
                        "type": "string",
                        "description": param_text("param.project_dir"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_PROJECT_NOTES_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                },
                "required": ["query", "path"],
            },
        ),
        Tool(
            name="tool_resource",
            description=TOOL_DESCRIPTIONS["tool_resource"],
            inputSchema={
                "type": "object",
                "properties": {
                    "parentName": {
                        "type": "string",
                        "description": param_text("param.parent_name"),
                    },
                    "path": {
                        "type": "string",
                        "description": param_text("param.resource_path"),
                    },
                },
                "required": ["parentName", "path"],
            },
        ),
        Tool(
            name="websearch_engine",
            description=TOOL_DESCRIPTIONS["websearch_engine"],
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": param_text("param.websearch_query"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_WEBSEARCH_LIMIT,
                        "description": param_text("param.websearch_limit"),
                    },
                    "fetch_top": {
                        "type": "integer",
                        "default": DEFAULT_WEBSEARCH_FETCH_TOP,
                        "description": param_text("param.fetch_top"),
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": DEFAULT_WEBSEARCH_MAX_CHARS,
                        "description": param_text("param.max_chars"),
                    },
                    "engines": {
                        "type": "string",
                        "description": param_text("param.engines"),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="read_url",
            description=TOOL_DESCRIPTIONS["read_url"],
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": param_text("param.read_url"),
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": DEFAULT_READ_MAX_CHARS,
                        "description": param_text("param.read_max_chars"),
                    },
                    "use_proxy": {
                        "type": "boolean",
                        "default": False,
                        "description": param_text("param.read_use_proxy"),
                    },
                },
                "required": ["url"],
            },
        ),
    ]
    return tools
