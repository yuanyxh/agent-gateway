"""Tool functions registered on the MCPServer instance.

Each function mirrors the schema previously hand-built in server_tool_schema:
same parameter names, descriptions, enums and defaults. The defaults still come
from `arg_default`, so schema and handler fallback stay on one source.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from agent_gateway.core.tool_text import param_text
from agent_gateway.services.mcp.server_tool_schema import (
    DEFAULT_CATCH_UP_LIMIT,
    DEFAULT_CATCH_UP_MAX_CHARS,
    DEFAULT_GRAPH_LIMIT,
    DEFAULT_PROJECT_NOTES_LIMIT,
    DEFAULT_READ_MAX_CHARS,
    DEFAULT_RECALL_LIMIT,
    DEFAULT_RECALL_MAX_CHARS,
    DEFAULT_WEBSEARCH_FETCH_TOP,
    DEFAULT_WEBSEARCH_LIMIT,
    DEFAULT_WEBSEARCH_MAX_CHARS,
    MEMORY_ITEM_SCHEMA,
    TOOL_DESCRIPTIONS,
)
from agent_gateway.services.mcp.graph_tools import (
    IMPACT_DEFAULT_DEPTH,
    IMPACT_DEFAULT_LIMIT,
    LOOKUP_DEFAULT_LIMIT,
)


async def _run(handlers: dict[str, Callable], name: str, arguments: dict) -> str:
    blocks = await handlers[name](arguments)
    return "\n".join(block.text for block in blocks if block.type == "text")


def _compact(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in arguments.items() if v is not None}


def register_tools(mcp, handlers: dict[str, Callable]) -> None:
    def tool(**kwargs):
        """Keep the v1 result shape: content blocks only, no structuredContent."""
        return mcp.tool(structured_output=False, **kwargs)

    @tool(name="remember", description=TOOL_DESCRIPTIONS["remember"])
    async def remember(
        content: Annotated[str, Field(description=param_text("param.memory_content"))],
        context: Annotated[
            str | None, Field(description=param_text("param.memory_context"))
        ] = None,
        type: Annotated[
            str | None, Field(description=param_text("param.memory_type"))
        ] = None,
        source_files: Annotated[
            list[str] | None, Field(description=param_text("param.source_files"))
        ] = None,
        source_url: Annotated[
            str | None, Field(description=param_text("param.source_url"))
        ] = None,
        agent_id: str = "unknown",
        visibility: Literal["all", "agent-specific", "user-only"] = "all",
        tags: list[str] | None = None,
        confidence: float = 1.0,
        importance: Annotated[
            int,
            Field(
                ge=1,
                le=10,
                default=5,
                description=param_text("param.memory_importance"),
            ),
        ] = 5,
        protected: Annotated[
            bool, Field(default=False, description=param_text("param.memory_protected"))
        ] = False,
        supersedes: list[str] | None = None,
        disputed_by: list[str] | None = None,
        episode_id: Annotated[
            str | None,
            Field(
                description="Optional group key; memories sharing it are recalled together."
            ),
        ] = None,
    ) -> str:
        return await _run(
            handlers,
            "remember",
            _compact(
                {
                    "content": content,
                    "context": context,
                    "type": type,
                    "source_files": source_files,
                    "source_url": source_url,
                    "agent_id": agent_id,
                    "visibility": visibility,
                    "tags": tags,
                    "confidence": confidence,
                    "importance": importance,
                    "protected": protected,
                    "supersedes": supersedes,
                    "disputed_by": disputed_by,
                    "episode_id": episode_id,
                }
            ),
        )

    @tool(name="remember_batch", description=TOOL_DESCRIPTIONS["remember_batch"])
    async def remember_batch(
        memories: Annotated[
            list[dict[str, Any]],
            Field(
                description=param_text("param.memories"),
                json_schema_extra={"items": MEMORY_ITEM_SCHEMA},
            ),
        ],
    ) -> str:
        return await _run(handlers, "remember_batch", {"memories": memories})

    @tool(name="recall", description=TOOL_DESCRIPTIONS["recall"])
    async def recall(
        keywords: Annotated[list[str], Field(description=param_text("param.keywords"))],
        query: Annotated[
            str | None, Field(description=param_text("param.query"))
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_RECALL_LIMIT,
                description=param_text("param.max_results"),
            ),
        ] = DEFAULT_RECALL_LIMIT,
        max_chars: Annotated[
            int,
            Field(
                default=DEFAULT_RECALL_MAX_CHARS,
                description=param_text("param.recall_max_chars"),
            ),
        ] = DEFAULT_RECALL_MAX_CHARS,
        include_archived: bool = False,
        include_superseded: Annotated[
            bool,
            Field(default=False, description=param_text("param.include_superseded")),
        ] = False,
        layers: Annotated[
            list[Literal["hot", "cold"]] | None,
            Field(description=param_text("param.layers")),
        ] = None,
    ) -> str:
        return await _run(
            handlers,
            "recall",
            _compact(
                {
                    "keywords": keywords,
                    "query": query,
                    "limit": limit,
                    "max_chars": max_chars,
                    "include_archived": include_archived,
                    "include_superseded": include_superseded,
                    "layers": layers,
                }
            ),
        )

    @tool(name="catch_up", description=TOOL_DESCRIPTIONS["catch_up"])
    async def catch_up(
        since_days: Annotated[
            float | None, Field(description=param_text("param.since_days"))
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_CATCH_UP_LIMIT,
                description=param_text("param.max_results"),
            ),
        ] = DEFAULT_CATCH_UP_LIMIT,
        offset: Annotated[
            int, Field(default=0, description=param_text("param.offset"))
        ] = 0,
        max_chars: Annotated[
            int,
            Field(
                default=DEFAULT_CATCH_UP_MAX_CHARS,
                description=param_text("param.catch_up_max_chars"),
            ),
        ] = DEFAULT_CATCH_UP_MAX_CHARS,
        include_superseded: Annotated[
            bool,
            Field(default=False, description=param_text("param.include_superseded")),
        ] = False,
    ) -> str:
        return await _run(
            handlers,
            "catch_up",
            _compact(
                {
                    "since_days": since_days,
                    "limit": limit,
                    "offset": offset,
                    "max_chars": max_chars,
                    "include_superseded": include_superseded,
                }
            ),
        )

    @tool(name="forget", description=TOOL_DESCRIPTIONS["forget"])
    async def forget(
        memory_id: Annotated[
            str | None, Field(description=param_text("param.memory_id"))
        ] = None,
        point_id: Annotated[
            str | None, Field(description=param_text("param.point_id"))
        ] = None,
        layer: Annotated[
            Literal["hot", "cold", "all"],
            Field(default="hot", description=param_text("param.layer")),
        ] = "hot",
    ) -> str:
        return await _run(
            handlers,
            "forget",
            _compact({"memory_id": memory_id, "point_id": point_id, "layer": layer}),
        )

    @tool(
        name="set_memory_processed",
        description=TOOL_DESCRIPTIONS["set_memory_processed"],
    )
    async def set_memory_processed(
        memory_ids: Annotated[
            list[str], Field(description=param_text("param.memory_ids"))
        ],
        bit: Annotated[int, Field(default=0, description=param_text("param.bit"))] = 0,
        value: Annotated[
            int, Field(default=1, description=param_text("param.bit_value"))
        ] = 1,
    ) -> str:
        return await _run(
            handlers,
            "set_memory_processed",
            {"memory_ids": memory_ids, "bit": bit, "value": value},
        )

    @tool(name="memory_get", description=TOOL_DESCRIPTIONS["memory_get"])
    async def memory_get(
        node_id: Annotated[str, Field(description=param_text("param.memory_node_id"))],
    ) -> str:
        return await _run(handlers, "memory_get", {"node_id": node_id})

    @tool(name="memory_graph", description=TOOL_DESCRIPTIONS["memory_graph"])
    async def memory_graph(
        node_id: Annotated[str, Field(description=param_text("param.memory_node_id"))],
        edge: Annotated[
            Literal[
                "supersedes",
                "episode",
                "derived_from",
                "disputes",
                "relates",
                "mentions",
            ],
            Field(default="supersedes", description=param_text("param.memory_edge")),
        ] = "supersedes",
        direction: Annotated[
            Literal["up", "down", "both"],
            Field(default="both", description=param_text("param.memory_direction")),
        ] = "both",
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_GRAPH_LIMIT, description=param_text("param.max_results")
            ),
        ] = DEFAULT_GRAPH_LIMIT,
    ) -> str:
        return await _run(
            handlers,
            "memory_graph",
            {"node_id": node_id, "edge": edge, "direction": direction, "limit": limit},
        )

    @tool(name="project_lookup", description=TOOL_DESCRIPTIONS["project_lookup"])
    async def project_lookup(
        path: Annotated[str, Field(description=param_text("param.project_dir"))],
        query: Annotated[
            str | None, Field(description=param_text("param.graph_query"))
        ] = None,
        kind: Annotated[
            str | None, Field(description=param_text("param.symbol_kind"))
        ] = None,
        file_path: Annotated[
            str | None, Field(description=param_text("param.file_path"))
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=LOOKUP_DEFAULT_LIMIT,
                description=param_text("param.max_results"),
            ),
        ] = LOOKUP_DEFAULT_LIMIT,
    ) -> str:
        return await _run(
            handlers,
            "project_lookup",
            _compact(
                {
                    "path": path,
                    "query": query,
                    "kind": kind,
                    "file_path": file_path,
                    "limit": limit,
                }
            ),
        )

    @tool(name="project_impact", description=TOOL_DESCRIPTIONS["project_impact"])
    async def project_impact(
        path: Annotated[str, Field(description=param_text("param.project_dir"))],
        target: Annotated[str, Field(description=param_text("param.graph_target"))],
        direction: Annotated[
            Literal["callers", "callees", "both"],
            Field(default="callers", description=param_text("param.graph_direction")),
        ] = "callers",
        depth: Annotated[
            int,
            Field(
                default=IMPACT_DEFAULT_DEPTH,
                description=param_text("param.graph_depth"),
            ),
        ] = IMPACT_DEFAULT_DEPTH,
        limit: Annotated[
            int,
            Field(
                default=IMPACT_DEFAULT_LIMIT,
                description=param_text("param.max_results"),
            ),
        ] = IMPACT_DEFAULT_LIMIT,
        includeStale: Annotated[
            bool,
            Field(default=False, description=param_text("param.graph_include_stale")),
        ] = False,
    ) -> str:
        return await _run(
            handlers,
            "project_impact",
            {
                "path": path,
                "target": target,
                "direction": direction,
                "depth": depth,
                "limit": limit,
                "includeStale": includeStale,
            },
        )

    @tool(name="project_notes", description=TOOL_DESCRIPTIONS["project_notes"])
    async def project_notes(
        query: Annotated[str, Field(description=param_text("param.notes_keyword"))],
        path: Annotated[str, Field(description=param_text("param.project_dir"))],
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_PROJECT_NOTES_LIMIT,
                description=param_text("param.max_results"),
            ),
        ] = DEFAULT_PROJECT_NOTES_LIMIT,
    ) -> str:
        return await _run(
            handlers,
            "project_notes",
            {"query": query, "path": path, "limit": limit},
        )

    @tool(name="tool_resource", description=TOOL_DESCRIPTIONS["tool_resource"])
    async def tool_resource(
        parentName: Annotated[str, Field(description=param_text("param.parent_name"))],
        path: Annotated[str, Field(description=param_text("param.resource_path"))],
    ) -> str:
        return await _run(
            handlers,
            "tool_resource",
            {"parentName": parentName, "path": path},
        )

    @tool(name="websearch_engine", description=TOOL_DESCRIPTIONS["websearch_engine"])
    async def websearch_engine(
        query: Annotated[str, Field(description=param_text("param.websearch_query"))],
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_WEBSEARCH_LIMIT,
                description=param_text("param.websearch_limit"),
            ),
        ] = DEFAULT_WEBSEARCH_LIMIT,
        fetch_top: Annotated[
            int,
            Field(
                default=DEFAULT_WEBSEARCH_FETCH_TOP,
                description=param_text("param.fetch_top"),
            ),
        ] = DEFAULT_WEBSEARCH_FETCH_TOP,
        max_chars: Annotated[
            int,
            Field(
                default=DEFAULT_WEBSEARCH_MAX_CHARS,
                description=param_text("param.max_chars"),
            ),
        ] = DEFAULT_WEBSEARCH_MAX_CHARS,
        engines: Annotated[
            str | None, Field(description=param_text("param.engines"))
        ] = None,
    ) -> str:
        return await _run(
            handlers,
            "websearch_engine",
            _compact(
                {
                    "query": query,
                    "limit": limit,
                    "fetch_top": fetch_top,
                    "max_chars": max_chars,
                    "engines": engines,
                }
            ),
        )

    @tool(name="read_url", description=TOOL_DESCRIPTIONS["read_url"])
    async def read_url(
        url: Annotated[str, Field(description=param_text("param.read_url"))],
        max_chars: Annotated[
            int,
            Field(
                default=DEFAULT_READ_MAX_CHARS,
                description=param_text("param.read_max_chars"),
            ),
        ] = DEFAULT_READ_MAX_CHARS,
        use_proxy: Annotated[
            bool,
            Field(default=False, description=param_text("param.read_use_proxy")),
        ] = False,
    ) -> str:
        return await _run(
            handlers,
            "read_url",
            {"url": url, "max_chars": max_chars, "use_proxy": use_proxy},
        )
