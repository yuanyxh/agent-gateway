#!/usr/bin/env python3
"""
MCP MinoBraiN — hot/cold two-tier shared memory + project dependency-graph query

Hot layer: Mem0 → Qdrant (raw fragment memories, auto extract/dedup/conflict resolution)
Cold layer: Qdrant separate collection (archived knowledge index)
Project: SQLite dependency-graph query (FTS5 + synonym expansion)

Ported from self-service-docker, adapted for the Utility.app subprocess model.

Refactor (2026-08-30, phase 4): shared embedder → server_embed.py; project
dependency-graph query and note semantic search → server_search.py; this entry point
keeps memory operations / cold layer / skills / HTTP service / main().
"""

# mem0's built-in PostHog telemetry hangs initialization in an offline environment,
# so it must be disabled before any import
import os

os.environ["POSTHOG_DISABLED"] = "1"
# mem0 ships its own PostHog telemetry (a separate instance, unaffected by
# posthog.disabled); set this env var before import so AnonymousTelemetry skips init
os.environ["MEM0_TELEMETRY"] = "False"

import uuid
import time
import asyncio
import logging
import anyio
from datetime import datetime, timedelta

from qdrant_client import QdrantClient
import uvicorn
from qdrant_client.models import (
    VectorParams,
    Distance,
    PointStruct,
)
from mcp.server import Server
from mcp.server import InitializationOptions
from mcp.server.models import ServerCapabilities
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import (
    Tool,
    TextContent,
    ToolsCapability,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from agent_gateway.services.mcp.server_embed import (
    EMBEDDER_DIMS,
    FASTEMBED_CACHE_DIR,
    _get_shared_embedder,
    describe_dim_mismatch,
)
from agent_gateway.services.mcp.server_tool_schema import (
    DEFAULT_PROJECT_NOTES_LIMIT,
    DEFAULT_WEBSEARCH_FETCH_TOP,
    DEFAULT_WEBSEARCH_LIMIT,
    DEFAULT_WEBSEARCH_MAX_CHARS,
    build_tools,
)
from agent_gateway.services.mcp.server_search import (
    DEFAULT_PROJECT_DEPENDENCIES_DIRECTION,
    DEFAULT_PROJECT_DEPENDENCIES_LIMIT,
    DEFAULT_PROJECT_SEARCH_LIMIT,
    DEFAULT_PROJECT_STRUCTURE_LIMIT,
    tool_project_search,
    tool_project_structure,
    tool_project_exports,
    tool_project_dependencies,
    _query_project_notes,
)
from agent_gateway.services.mcp.server_archive import archive_scheduler
from agent_gateway.services.mcp.server_memory import (
    HANDLERS as MEMORY_HANDLERS,
    _cold_episode_id,
    contradiction_check_cold,
    ensure_hot_memory,
    set_memory_status,
    set_processed_bit,
    spawn_contradiction_check,
)
from agent_gateway.services.mcp.server_state import (
    get_hot_memory,
    get_qdrant,
    set_qdrant,
)
from agent_gateway.services.mcp.server_rest import (
    REST_ROUTES,
    read_args,
    summarize_args,
)
from agent_gateway.services.mcp.server_utils import dumps, int_arg
from agent_gateway.services.mcp.server_constants import (
    COLD_COLLECTION,
    COLD_SOURCE_MEMORY_IDS_FIELD,
    DEFAULT_HEAT_SCORE,
    DEFAULT_USER_ID,
    EPISODE_FIELD,
    ERR_BIT_VALUE_NOT_INT,
    ERR_HOT_LAYER_NOT_READY,
    ERR_MEMORY_IDS_EMPTY,
    ERR_VALUE_INVALID,
    MEM0_QDRANT_PATH,
    QDRANT_PATH,
    PROCESSED_MAX_BIT,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_HIDDEN,
    STATUS_SUPERSEDED,
    err_bit_out_of_range,
)
from agent_gateway.services.mcp.server_skills import (
    SKILL_SKIP_DIRS,
    find_skill_dir,
    normalize_name,
    scan_skills,
)
from agent_gateway.services.read.read_service import read_url
from agent_gateway.services.summarize import text as summarize_service
from agent_gateway.services.search import web_search
from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("server")

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.WARNING)
for _handler in logger.handlers:
    _root_logger.addHandler(_handler)
logger.propagate = False


def _cleanup_qdrant_locks() -> None:
    """After a Qdrant Local mode crash, a leftover lock file blocks the next startup, so clean it up before starting."""
    for root_dir in (
        QDRANT_PATH,
        MEM0_QDRANT_PATH,
        os.path.expanduser("~/.mem0/migrations_qdrant"),
    ):
        lock = os.path.join(root_dir, ".lock")
        if os.path.isfile(lock):
            try:
                os.remove(lock)
                logger.info(f"Cleaned stale lock: {lock}")
            except OSError as e:
                logger.warning(f"Failed to remove lock {lock}: {e}")


_cleanup_qdrant_locks()

logger.info(
    f"ENV CHECK: FASTEMBED_CACHE_PATH={FASTEMBED_CACHE_DIR}, MCP_PORT={os.environ.get('MCP_PORT')}, "
    f"QDRANT_PATH={os.environ.get('QDRANT_PATH')}, MEM0_TELEMETRY={os.environ.get('MEM0_TELEMETRY')}"
)

SERVER_NAME = "mino-brain"

SERVER_VERSION = "1.0.0"

MCP_PORT = int(os.environ.get("MCP_PORT", "9001"))


def _collection_vector_size(client, collection_name: str) -> int | None:
    """Read a collection's vector dims; returns None if unreadable (the caller treats it
    as "cannot determine" and skips validation).

    This project uses a single-vector config; the named-vector (dict) form is also
    supported — crashing on the startup path just to read one dims value costs far more
    than the few lines it takes to support it.
    """
    try:
        vectors = client.get_collection(collection_name).config.params.vectors
    except Exception as e:
        logger.warning(f"failed to read dimension of collection {collection_name}: {e}")
        return None
    size = getattr(vectors, "size", None)
    if isinstance(size, int):
        return size
    if isinstance(vectors, dict):
        for config in vectors.values():
            value = getattr(config, "size", None)
            if isinstance(value, int):
                return value
    return None


try:
    client = QdrantClient(path=QDRANT_PATH, force_disable_check_same_thread=True)
    existing = [c.name for c in client.get_collections().collections]
    if COLD_COLLECTION not in existing:
        client.create_collection(
            collection_name=COLD_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDER_DIMS, distance=Distance.COSINE),
        )
        logger.info(f"Cold layer collection '{COLD_COLLECTION}' created")
    else:
        # Collection dims are fixed at first creation: after swapping the model (dims
        # change) the old vectors are not comparable with the new query vectors, and the
        # search side swallows the exception into a single warning — presenting as
        # cold-layer search silently failing. Call it out at startup.
        advice = describe_dim_mismatch(
            _collection_vector_size(client, COLD_COLLECTION), EMBEDDER_DIMS
        )
        if advice:
            logger.error(advice)
            # Disable the cold layer, but hot layer / project search proceed as usual (all other qdrant usages have an `if qdrant` guard)
            client = None
    set_qdrant(client)
    if client is not None:
        logger.info("Cold layer (Qdrant direct) initialized")
except Exception as e:
    logger.error(f"Cold layer Qdrant init failed: {e}")
    set_qdrant(None)


# mem0 hot-layer results are emitted as-is, and fields vary across versions; a blacklist
# would miss some, so a whitelist is used. Removal rationale: memory and content are
# byte-identical duplicates (measured 248/248, 38% of output); hash/role/user_id are
# unused by the model or always the same value; inside metadata, archived is already


server = Server(SERVER_NAME)


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return build_tools()


HANDLERS: dict[str, callable] = dict(MEMORY_HANDLERS)


def _register(name: str):
    def decorator(func):
        HANDLERS[name] = func
        return func

    return decorator


@_register("project_search")
async def _handle_project_search(arguments: dict) -> list[TextContent]:
    try:
        result = await asyncio.to_thread(
            tool_project_search,
            arguments["dbPath"],
            arguments["query"],
            file_path=arguments.get("file_path", ""),
            kind=arguments.get("kind", ""),
            limit=int_arg(arguments, "limit", DEFAULT_PROJECT_SEARCH_LIMIT),
        )
        return [TextContent(type="text", text=dumps(result))]
    except Exception:
        # Re-raise to handle_call_tool: returning {"error": ...} here makes the SDK treat
        # it as success, so a real failure is read by the client as "found it, just no
        # results". Logging is done uniformly by the fallback layer.
        raise


@_register("project_structure")
async def _handle_project_structure(arguments: dict) -> list[TextContent]:
    try:
        result = await asyncio.to_thread(
            tool_project_structure,
            arguments["dbPath"],
            arguments.get("path", ""),
            int_arg(arguments, "limit", DEFAULT_PROJECT_STRUCTURE_LIMIT),
        )
        return [TextContent(type="text", text=dumps(result))]
    except Exception:
        # Re-raise to handle_call_tool: returning {"error": ...} here makes the SDK treat
        # it as success, so a real failure is read by the client as "found it, just no
        # results". Logging is done uniformly by the fallback layer.
        raise


@_register("project_exports")
async def _handle_project_exports(arguments: dict) -> list[TextContent]:
    try:
        result = await asyncio.to_thread(
            tool_project_exports, arguments["dbPath"], arguments["path"]
        )
        return [TextContent(type="text", text=dumps(result))]
    except Exception:
        # Re-raise to handle_call_tool: returning {"error": ...} here makes the SDK treat
        # it as success, so a real failure is read by the client as "found it, just no
        # results". Logging is done uniformly by the fallback layer.
        raise


@_register("project_dependencies")
async def _handle_project_dependencies(arguments: dict) -> list[TextContent]:
    try:
        result = await asyncio.to_thread(
            tool_project_dependencies,
            arguments["dbPath"],
            arguments["path"],
            arguments.get("direction", DEFAULT_PROJECT_DEPENDENCIES_DIRECTION),
            int_arg(arguments, "limit", DEFAULT_PROJECT_DEPENDENCIES_LIMIT),
        )
        return [TextContent(type="text", text=dumps(result))]
    except Exception:
        # Re-raise so the SDK marks the call as an error instead of returning a
        # successful empty payload the client would read as "no dependencies".
        raise


@_register("project_notes")
async def _handle_project_notes(arguments: dict) -> list[TextContent]:
    try:
        query = arguments["query"]
        path = arguments["path"]
        limit = int_arg(arguments, "limit", DEFAULT_PROJECT_NOTES_LIMIT)

        if not query or not query.strip():
            return [
                TextContent(
                    type="text",
                    text=dumps({"error": "query must not be empty"}),
                )
            ]
        if not os.path.isdir(path):
            return [
                TextContent(
                    type="text",
                    text=dumps({"error": f"project directory not found: {path}"}),
                )
            ]

        # Run on a background thread to avoid blocking the event loop (ONNX embedding is CPU-intensive)
        result = await asyncio.to_thread(
            _query_project_notes, path, query.strip(), limit
        )
        return [TextContent(type="text", text=dumps(result))]

    except Exception:
        # Same as above: logging and isError are handled uniformly by handle_call_tool's fallback layer
        raise


@_register("tool_resource")
async def _handle_tool_resource(arguments: dict) -> list[TextContent]:
    parent_name = str(arguments.get("parentName", "")).strip()
    rel_path = str(arguments.get("path", "")).strip()
    if not parent_name or not rel_path:
        return [
            TextContent(
                type="text",
                text=dumps({"error": "parentName and path are required"}),
            )
        ]
    skill_dir = find_skill_dir(parent_name)
    if skill_dir is None:
        return [
            TextContent(
                type="text",
                text=dumps({"error": f"skill not found: {parent_name}"}),
            )
        ]
    # Only allow referencing resources inside the companion subdirectories (references/agents/scripts)
    top = rel_path.split("/", 1)[0]
    if top not in SKILL_SKIP_DIRS:
        return [
            TextContent(
                type="text",
                text=dumps(
                    {"error": f"path must be under one of: {sorted(SKILL_SKIP_DIRS)}"}
                ),
            )
        ]
    full = os.path.normpath(os.path.join(skill_dir, rel_path))
    if full != skill_dir and not full.startswith(skill_dir + os.sep):
        return [
            TextContent(
                type="text",
                text=dumps({"error": "path escapes the skill directory"}),
            )
        ]
    # symlink protection: normpath only blocks syntactic ../ and does not resolve
    # symlinks. After resolving the real path it must still be inside the skill
    # directory's real path, preventing a symlink inside the skill's companion
    # directories from pointing at an external file and leaking information. Reads
    # uniformly use real_full (already resolved), avoiding a second follow between
    # validation and open (TOCTOU).
    real_skill = os.path.realpath(skill_dir)
    real_full = os.path.realpath(full)
    if real_full != real_skill and not real_full.startswith(real_skill + os.sep):
        return [
            TextContent(
                type="text",
                text=dumps({"error": "path escapes the skill directory via symlink"}),
            )
        ]
    full = real_full
    if not os.path.isfile(full):
        return [
            TextContent(
                type="text",
                text=dumps({"error": f"resource not found: {rel_path}"}),
            )
        ]
    try:
        with open(full, "r", encoding="utf-8") as fh:
            content = fh.read()
    except UnicodeDecodeError:
        return [
            TextContent(
                type="text",
                text=dumps({"error": "resource is not utf-8 text"}),
            )
        ]
    except Exception as e:
        # Same as handle_call_tool: unexpected exceptions must be re-raised, otherwise isError is always False
        logger.error(f"[tool_resource] {e}", exc_info=True)
        raise
    return [TextContent(type="text", text=content)]


@_register("websearch_engine")
async def _handle_websearch_engine(arguments: dict) -> list[TextContent]:
    query = str(arguments.get("query", "")).strip()
    if not query:
        return [TextContent(type="text", text=dumps({"error": "query is required"}))]
    limit = max(1, min(int_arg(arguments, "limit", DEFAULT_WEBSEARCH_LIMIT), 10))
    fetch_top = max(
        0, min(int_arg(arguments, "fetch_top", DEFAULT_WEBSEARCH_FETCH_TOP), 3)
    )
    max_chars = max(
        1000, min(int_arg(arguments, "max_chars", DEFAULT_WEBSEARCH_MAX_CHARS), 10000)
    )
    engines = arguments.get("engines")
    if isinstance(engines, list):
        engines = tuple(str(e).strip() for e in engines if str(e).strip())
    elif not isinstance(engines, str) or not engines.strip():
        engines = None
    # urllib is blocking; keep the MCP event loop free for other calls.
    result = await asyncio.to_thread(
        web_search.websearch, query, limit, fetch_top, max_chars, engines
    )
    return [TextContent(type="text", text=dumps(result))]


@_register("read_url")
async def _handle_read_url(arguments: dict) -> list[TextContent]:
    url, max_chars, use_proxy = read_args(arguments)
    result = await asyncio.to_thread(read_url, url, max_chars, use_proxy)
    return [TextContent(type="text", text=dumps(result))]


@_register("summarize")
async def _handle_summarize(arguments: dict) -> list[TextContent]:
    text, instruction, max_tokens = summarize_args(arguments)
    result = await asyncio.to_thread(
        summarize_service.summarize_text, text, instruction, max_tokens
    )
    return [TextContent(type="text", text=dumps(result))]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    started = time.perf_counter()
    ok = True
    try:
        handler = HANDLERS.get(name)
        if handler:
            return await handler(arguments)
        skills = scan_skills()
        if name in skills:
            return [TextContent(type="text", text=skills[name]["content"])]
        normalized = normalize_name(name)
        if normalized in skills:
            return [TextContent(type="text", text=skills[normalized]["content"])]
        ok = False
        return [
            TextContent(type="text", text=dumps({"error": f"Unknown tool: {name}"}))
        ]
    except Exception as e:
        # Must keep re-raising: returning a {"error": ...} TextContent here is, to the
        # SDK, "the tool returned normally" with isError staying False — a real failure is
        # treated as success by the client, just with an extra error field in the result,
        # leaving the caller no basis to retry or degrade. After re-raising, the SDK's
        # call_tool fallback turns it into isError=True.
        ok = False
        logger.error(f"Error in {name}: {e}", exc_info=True)
        raise
    finally:
        # Measurement anchor for latency auditing: round-trip time and success/failure of each tool call
        logger.info(
            f"tool_call name={name} ok={ok} "
            f"elapsed_ms={int((time.perf_counter() - started) * 1000)}"
        )


def main() -> None:
    """Sync entry: hosts bootstrap by module name (`-c "from <module> import main; main()"`)."""
    asyncio.run(serve())


async def serve():
    asyncio.create_task(
        archive_scheduler(lambda: (get_hot_memory(), get_qdrant()), ensure_hot_memory)
    )

    transport = StreamableHTTPServerTransport(
        mcp_session_id=None, is_json_response_enabled=True
    )

    async def health_check(scope, receive, send):
        """Health check endpoint for process manager monitoring."""
        response = JSONResponse({"status": "ok", "service": SERVER_NAME})
        await response(scope, receive, send)

    async def mcp_asgi_app(scope, receive, send):
        if scope["path"] == "/health":
            await health_check(scope, receive, send)
            return
        if scope["path"].startswith("/api/"):
            rest_scope = dict(scope)
            await rest_app(rest_scope, receive, send)
            return
        await transport.handle_request(scope, receive, send)

    async def api_memory_add_knowledge(request):
        body = await request.json()
        emb = _get_shared_embedder()
        if not get_qdrant() or emb is None:
            return JSONResponse(
                {"error": "Cold layer not initialized"}, status_code=500
            )
        content = body["content"]
        now_iso = datetime.now().isoformat()
        vec = list(emb.embed(content))[0].tolist()
        point_id = str(uuid.uuid4())
        source_ids = body.get(COLD_SOURCE_MEMORY_IDS_FIELD, [])
        episode_id = _cold_episode_id(source_ids, body.get(EPISODE_FIELD, ""), vec)
        get_qdrant().upsert(
            collection_name=COLD_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vec,
                    payload={
                        "content": content,
                        "source_file": body.get("source_file", ""),
                        "created_at": body.get("created_at", ""),
                        "category": body.get("category", ""),
                        "type": "knowledge",
                        "heat_score": DEFAULT_HEAT_SCORE,
                        "last_accessed_at": now_iso,
                        "access_count": 0,
                        "archived": False,
                        "tags": body.get("tags", []),
                        "user_id": DEFAULT_USER_ID,
                        "confidence": body.get("confidence", 1.0),
                        COLD_SOURCE_MEMORY_IDS_FIELD: source_ids,
                        EPISODE_FIELD: episode_id,
                    },
                )
            ],
        )
        spawn_contradiction_check(contradiction_check_cold(content, point_id))
        return JSONResponse({"success": True, "layer": "cold"})

    async def api_memories_recent(request):
        if not await ensure_hot_memory():
            return JSONResponse({"error": ERR_HOT_LAYER_NOT_READY}, status_code=503)
        since_str = request.query_params.get("since", "")
        limit = int(request.query_params.get("limit", 200))
        try:
            data = get_hot_memory().get_all(
                filters={"user_id": DEFAULT_USER_ID}, top_k=5000
            )
            if isinstance(data, dict):
                data = data.get("results", [])
        except Exception:
            data = []
        if since_str:
            try:
                since_dt = datetime.fromisoformat(since_str)
                since_ts = since_dt.timestamp()
            except ValueError:
                since_ts = (datetime.now() - timedelta(days=3)).timestamp()
        else:
            since_ts = (datetime.now() - timedelta(days=3)).timestamp()
        data = [
            m
            for m in data
            if datetime.fromisoformat(m.get("created_at", "")).timestamp() >= since_ts
        ]
        # Superseded/deleted memories must not reach the summarizer, or it will
        # sediment facts that a newer revision already replaced.
        data = [
            m
            for m in data
            if (m.get("metadata") or {}).get("status") not in STATUS_HIDDEN
        ]
        data = sorted(
            data,
            key=lambda x: datetime.fromisoformat(x.get("created_at", "")).timestamp(),
            reverse=True,
        )[:limit]
        return JSONResponse({"results": data, "count": len(data)})

    async def api_memories_processed(request):
        """Batch set/reset memory processed bits (bit0=summarized), idempotent; used by summarize write-back and test restoration."""
        if not await ensure_hot_memory():
            return JSONResponse({"error": ERR_HOT_LAYER_NOT_READY}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        memory_ids = body.get("memory_ids", [])
        try:
            bit = int(body.get("bit", 0))
            value = int(body.get("value", 1))
        except (TypeError, ValueError):
            return JSONResponse({"error": ERR_BIT_VALUE_NOT_INT}, status_code=400)
        if not memory_ids:
            return JSONResponse({"error": ERR_MEMORY_IDS_EMPTY}, status_code=400)
        if not (0 <= bit <= PROCESSED_MAX_BIT):
            return JSONResponse({"error": err_bit_out_of_range()}, status_code=400)
        if value not in (0, 1):
            return JSONResponse({"error": ERR_VALUE_INVALID}, status_code=400)
        updated = 0
        failed_ids = []
        for mid in memory_ids:
            try:
                if await asyncio.to_thread(set_processed_bit, mid, bit, value):
                    updated += 1
                else:
                    failed_ids.append(mid)
            except Exception:
                failed_ids.append(mid)
        return JSONResponse(
            {
                "success": not failed_ids,
                "bit": bit,
                "value": value,
                "updated": updated,
                "failed": len(failed_ids),
                "failed_ids": failed_ids,
            }
        )

    async def api_memories_status(request):
        """Set memory lifecycle status (active/superseded/deleted); maintenance entry point."""
        if not await ensure_hot_memory():
            return JSONResponse({"error": ERR_HOT_LAYER_NOT_READY}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        memory_ids = body.get("memory_ids", [])
        status = body.get("status", "")
        if not memory_ids:
            return JSONResponse({"error": ERR_MEMORY_IDS_EMPTY}, status_code=400)
        if status not in (STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_DELETED):
            return JSONResponse(
                {"error": "status must be active, superseded or deleted"},
                status_code=400,
            )
        try:
            result = await asyncio.to_thread(
                set_memory_status,
                memory_ids,
                status,
                body.get("superseded_by", ""),
            )
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_project_search(request):
        body = await request.json()
        try:
            result = tool_project_search(
                body.get("dbPath", ""),
                body.get("query", ""),
                limit=int_arg(body, "limit", DEFAULT_PROJECT_SEARCH_LIMIT),
            )
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_project_structure(request):
        body = await request.json()
        try:
            result = tool_project_structure(
                body.get("dbPath", ""),
                body.get("path", ""),
                int_arg(body, "limit", DEFAULT_PROJECT_STRUCTURE_LIMIT),
            )
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_project_exports(request):
        body = await request.json()
        try:
            result = tool_project_exports(body.get("dbPath", ""), body.get("path", ""))
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_project_dependencies(request):
        body = await request.json()
        try:
            result = tool_project_dependencies(
                body.get("dbPath", ""),
                body.get("path", ""),
                body.get("direction", DEFAULT_PROJECT_DEPENDENCIES_DIRECTION),
                int_arg(body, "limit", DEFAULT_PROJECT_DEPENDENCIES_LIMIT),
            )
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    rest_app = Starlette(
        routes=[
            *REST_ROUTES,
            Route(
                "/api/memory/add_knowledge",
                endpoint=api_memory_add_knowledge,
                methods=["POST"],
            ),
            Route(
                "/api/memories/recent", endpoint=api_memories_recent, methods=["GET"]
            ),
            Route(
                "/api/memories/processed",
                endpoint=api_memories_processed,
                methods=["POST"],
            ),
            Route(
                "/api/memories/status",
                endpoint=api_memories_status,
                methods=["POST"],
            ),
            Route("/api/project/search", endpoint=api_project_search, methods=["POST"]),
            Route(
                "/api/project/structure",
                endpoint=api_project_structure,
                methods=["POST"],
            ),
            Route(
                "/api/project/exports", endpoint=api_project_exports, methods=["POST"]
            ),
            Route(
                "/api/project/dependencies",
                endpoint=api_project_dependencies,
                methods=["POST"],
            ),
        ]
    )

    app = Starlette(routes=[Mount("/", app=mcp_asgi_app)])
    config = uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="info")
    uvi_server = uvicorn.Server(config)

    async with transport.connect() as streams:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                server.run,
                streams[0],
                streams[1],
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    capabilities=ServerCapabilities(tools=ToolsCapability()),
                ),
            )
            await anyio.sleep(0.1)
            await uvi_server.serve()


if __name__ == "__main__":
    main()
