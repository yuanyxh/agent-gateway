"""Read-only memory graph tools: one node's body and one node's chains.

Both tools read the SQLite node/edge registry; bodies are fetched from the store
that owns them (Mem0, Qdrant, or the notes tree), so an archived node stays
readable after it leaves retrieval.
"""

from __future__ import annotations

import asyncio
import os

from mcp.types import TextContent

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.mcp.server_constants import COLD_COLLECTION, NOTES_DIR
from agent_gateway.services.mcp.server_state import get_hot_memory, get_qdrant
from agent_gateway.services.mcp.server_tool_schema import DEFAULT_GRAPH_LIMIT
from agent_gateway.services.mcp.server_utils import dumps, int_arg
from agent_gateway.services.memory import graph, store

logger = setup_logging("server")

MAX_GRAPH_NODES = 500


def _reply(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=dumps(payload))]


def _hot_content(node_id: str) -> str:
    hot = get_hot_memory()
    if hot is None:
        return ""
    row = hot.get(node_id) or {}
    return str(row.get("memory") or row.get("content") or "")


def _cold_content(node: dict) -> str:
    point_id = str((node.get("metadata") or {}).get("point_id") or "")
    qdrant = get_qdrant()
    if not qdrant or not point_id:
        return ""
    points = qdrant.retrieve(
        collection_name=COLD_COLLECTION, ids=[point_id], with_payload=True
    )
    if not points:
        return ""
    return str((points[0].payload or {}).get("content") or "")


def _note_content(node: dict) -> str:
    rel = str(node.get("source_file") or "")
    if not rel or not NOTES_DIR:
        return ""
    root = os.path.realpath(NOTES_DIR)
    path = os.path.realpath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def read_node_content(node: dict) -> str:
    layer = str(node.get("layer") or "")
    if layer == "hot":
        return _hot_content(str(node.get("node_id") or ""))
    if layer == "cold":
        return _cold_content(node)
    if layer == "note":
        return _note_content(node)
    return ""


def _resolve(node_id: str) -> dict | None:
    """A tool caller may hand back the Qdrant point id instead of the node id."""
    return store.get_node(node_id) or store.find_node_by_point_id(node_id)


async def handle_memory_get(arguments: dict) -> list[TextContent]:
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        return _reply({"error": "node_id is required"})
    node = _resolve(node_id)
    if node is None:
        logger.info(f"[memory-tool] memory_get miss: {node_id}")
        return _reply({"node_id": node_id, "found": False})
    try:
        content = await asyncio.to_thread(read_node_content, node)
    except Exception as e:  # noqa: BLE001 - one lookup must not break the tool
        logger.warning(f"[memory-tool] memory_get read failed for {node_id}: {e}")
        return _reply({"error": str(e)})
    if not content:
        logger.warning(
            f"[memory-tool] memory_get empty body: {node_id} layer={node.get('layer')}"
        )
    return _reply({"found": True, "node": node, "content": content})


async def handle_memory_graph(arguments: dict) -> list[TextContent]:
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        return _reply({"error": "node_id is required"})
    edge = str(arguments.get("edge") or "supersedes").strip()
    direction = str(arguments.get("direction") or "both").strip()
    limit = max(
        1, min(int_arg(arguments, "limit", DEFAULT_GRAPH_LIMIT), MAX_GRAPH_NODES)
    )
    if edge not in graph.EDGE_TYPES:
        return _reply({"error": f"edge must be one of: {', '.join(graph.EDGE_TYPES)}"})
    if direction not in ("up", "down", "both"):
        return _reply({"error": "direction must be up, down or both"})
    node = _resolve(node_id)
    if node is None:
        logger.info(f"[memory-tool] memory_graph miss: {node_id}")
        return _reply({"node_id": node_id, "found": False})
    canonical = str(node.get("node_id") or node_id)
    try:
        if edge == "derived_from":
            result = await asyncio.to_thread(
                graph.walk_sources, canonical, max_nodes=limit
            )
        else:
            result = await asyncio.to_thread(
                graph.walk,
                canonical,
                edge,
                direction=direction,
                max_nodes=limit,
            )
    except ValueError as e:
        return _reply({"error": str(e)})
    except Exception as e:  # noqa: BLE001 - graph reads must not crash the tool
        logger.warning(f"[memory-tool] memory_graph walk failed for {canonical}: {e}")
        return _reply({"error": str(e)})
    if result.get("truncated"):
        logger.info(
            f"[memory-tool] memory_graph truncated: {canonical} edge={edge} limit={limit}"
        )
    return _reply({"found": True, **result})
