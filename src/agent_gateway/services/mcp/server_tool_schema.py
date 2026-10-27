"""MCP tool schema: names, descriptions, and input schemas.

Split out of server.py so the schema can be built and diffed without booting the
memory layer. Defaults shared with the handlers live here too: the schema's
`default` and the handler fallback must come from the same source.
"""

from __future__ import annotations

from mcp.types import Tool

from agent_gateway.services.mcp.server_search import (
    DEFAULT_PROJECT_DEPENDENCIES_DIRECTION,
    DEFAULT_PROJECT_DEPENDENCIES_LIMIT,
    DEFAULT_PROJECT_SEARCH_LIMIT,
    DEFAULT_PROJECT_STRUCTURE_LIMIT,
)
from agent_gateway.services.mcp.server_skills import scan_skills
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

#: Fixed English tool descriptions; parameter guidance lives in `tool_text.PARAM_DESCRIPTIONS`.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "remember": "Trigger: user says remember/record, a bug is fixed, an insight is learned, or an architecture design changes. Writes one fact to shared memory. Pass supersedes with the id of the memory this fact replaces; the old entry is retired, not deleted.",
    "remember_batch": "Batch version of remember; prefer it for multiple facts to cut calls.",
    "recall": "Trigger: user says look up/search/find that earlier one, or refers to history or existing information. Searches shared memory, knowledge base, and notes.",
    "catch_up": "Trigger: user says just now/last time/previously/recently, or asks what was done, the progress, or what remains unresolved. Do not substitute git log.",
    "forget": "Trigger: user asks to delete a memory, or a memory is wrong/outdated. Deletes it permanently.",
    "project_search": "Prefer over reading whole files to locate symbols, files, and packages. Searches the project dependency graph. For file imports and importers, use project_dependencies.",
    "project_structure": "Prefer over ls/find traversal to understand the project layout.",
    "project_exports": "Prefer over reading a whole file to get its public interface. For file imports and importers, use project_dependencies.",
    "project_dependencies": "Trigger: user asks what depends on a file, what a change will affect, or before deleting/moving/renaming a file or changing a public signature. Returns outgoing imports and incoming importers (1 hop).",
    "project_notes": "Like recall, but searches local project notes only.",
    "tool_resource": "Load a file under references or a script under scripts when instructed.",
    "set_memory_processed": "Set or reset the processed bit of memories. Idempotent.",
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
                    "agent_id": {"type": "string", "default": "unknown"},
                    "visibility": {
                        "type": "string",
                        "enum": ["all", "agent-specific", "user-only"],
                        "default": "all",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "default": 1.0},
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
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": param_text("param.memory_content"),
                                },
                                "agent_id": {"type": "string", "default": "unknown"},
                                "visibility": {
                                    "type": "string",
                                    "enum": ["all", "agent-specific", "user-only"],
                                    "default": "all",
                                },
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "number", "default": 1.0},
                                "supersedes": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "episode_id": {
                                    "type": "string",
                                    "description": "Optional group key; memories sharing it are recalled together.",
                                },
                            },
                            "required": ["content"],
                        },
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
                    "query": {
                        "type": "string",
                        "description": param_text("param.query"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_RECALL_LIMIT,
                        "description": param_text("param.max_results"),
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
                },
                "required": ["query"],
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
                    "include_superseded": {
                        "type": "boolean",
                        "default": False,
                        "description": param_text("param.include_superseded"),
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
        # ── Project tools ──
        Tool(
            name="project_search",
            description=TOOL_DESCRIPTIONS["project_search"],
            inputSchema={
                "type": "object",
                "properties": {
                    "dbPath": {
                        "type": "string",
                        "description": param_text("param.db_path"),
                    },
                    "query": {
                        "type": "string",
                        "description": param_text("param.search_keyword"),
                    },
                    "file_path": {
                        "type": "string",
                        "description": param_text("param.file_path"),
                    },
                    "kind": {
                        "type": "string",
                        "description": param_text("param.symbol_kind"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_PROJECT_SEARCH_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                },
                "required": ["dbPath", "query"],
            },
        ),
        Tool(
            name="project_structure",
            description=TOOL_DESCRIPTIONS["project_structure"],
            inputSchema={
                "type": "object",
                "properties": {
                    "dbPath": {
                        "type": "string",
                        "description": param_text("param.db_path"),
                    },
                    "path": {
                        "type": "string",
                        "description": param_text("param.subpath"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_PROJECT_STRUCTURE_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                },
                "required": ["dbPath"],
            },
        ),
        Tool(
            name="project_exports",
            description=TOOL_DESCRIPTIONS["project_exports"],
            inputSchema={
                "type": "object",
                "properties": {
                    "dbPath": {
                        "type": "string",
                        "description": param_text("param.db_path"),
                    },
                    "path": {
                        "type": "string",
                        "description": param_text("param.file_or_dir"),
                    },
                },
                "required": ["dbPath", "path"],
            },
        ),
        Tool(
            name="project_dependencies",
            description=TOOL_DESCRIPTIONS["project_dependencies"],
            inputSchema={
                "type": "object",
                "properties": {
                    "dbPath": {
                        "type": "string",
                        "description": param_text("param.db_path"),
                    },
                    "path": {
                        "type": "string",
                        "description": param_text("param.file_or_dir"),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["outgoing", "incoming", "both"],
                        "default": DEFAULT_PROJECT_DEPENDENCIES_DIRECTION,
                        "description": param_text("param.dependency_direction"),
                    },
                    "limit": {
                        "type": "integer",
                        "default": DEFAULT_PROJECT_DEPENDENCIES_LIMIT,
                        "description": param_text("param.max_results"),
                    },
                },
                "required": ["dbPath", "path"],
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
    for skill_name, skill in scan_skills().items():
        tools.append(
            Tool(
                name=skill_name,
                description=skill["description"],
                inputSchema={"type": "object", "properties": {}},
            )
        )
    return tools
