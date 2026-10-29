"""Model list schema per API format."""

import json
from datetime import datetime, timedelta, timezone

from aiohttp import web
from agent_gateway.core.db import conn as dbconn
from agent_gateway.services.proxy.proxy_usage import (
    DB_PATH,
)


from agent_gateway.services.proxy.proxy_config import PROVIDERS, logger

_MODEL_CREATED_EPOCH = 1767225600


def _models_schema_for(request: web.Request) -> str:
    """The protocol format of the model-discovery response. The criterion is taken from
    **protocol-level request headers**, not the UA:

    - carries `anthropic-version` (the Anthropic SDK / Claude Code sends it on every request) → anthropic
    - carries `Authorization: Bearer ...` (the OpenAI SDK's auth method) → openai
    - neither → falls back to anthropic for the `/open/*` compatibility entry, and logs info

    The UA is a client product fingerprint, unrelated to the protocol; using it to judge the
    protocol is exactly the kind of approach this stage aims to eliminate.
    """
    if request.headers.get("anthropic-version"):
        return "anthropic"
    if request.headers.get("authorization", "").lower().startswith("bearer"):
        return "openai"
    logger.info(
        json.dumps(
            {
                "msg": "models_schema_fallback",
                "path": request.path,
                "schema": "anthropic",
            }
        )
    )
    return "anthropic"


async def _handle_models(request: web.Request) -> web.Response:
    """GET /v1/models (or /open/v1/models): return all provider models.

    The same endpoint must serve clients of both protocols, and the response schema differs:
    - Anthropic (Claude Code's /model selector): `{data:[{type,id,display_name,
      created_at}], has_more, first_id, last_id}`
    - OpenAI (SDK `models.list()`): `{object:"list", data:[{id,object:"model",
      created, owned_by}]}`

    Providing only one would make the other protocol's SDK fail to parse.
    """
    entries: list[tuple[str, str]] = [
        (p.get("name", ""), m) for p in PROVIDERS for m in p.get("models", [])
    ]

    if _models_schema_for(request) == "openai":
        data = [
            {
                "id": m,
                "object": "model",
                "created": _MODEL_CREATED_EPOCH,
                "owned_by": pname,
            }
            for pname, m in entries
        ] or [
            {
                "id": "unknown",
                "object": "model",
                "created": _MODEL_CREATED_EPOCH,
                "owned_by": "",
            }
        ]
        return web.json_response({"object": "list", "data": data})

    data = [
        {
            "type": "model",
            "id": m,
            "display_name": f"{pname} · {m}",
            "created_at": "2026-01-01T00:00:00Z",
        }
        for pname, m in entries
    ]
    if not data:
        data.append(
            {
                "type": "model",
                "id": "unknown",
                "display_name": "unknown",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
    return web.json_response(
        {
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"],
            "last_id": data[-1]["id"],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


async def handle_usage_summary(request: web.Request) -> web.Response:
    """Today's usage summary (for Swift to query)."""
    try:
        with dbconn(DB_PATH) as conn:
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(input_tokens), 0),
                    COALESCE(SUM(output_tokens), 0),
                    COALESCE(SUM(cache_read_tokens), 0),
                    COUNT(*),
                    SUM(CASE WHEN response_valid = 0 THEN 1 ELSE 0 END)
                FROM api_usage
                WHERE timestamp >= date('now', 'start of day')
            """).fetchone()

        return web.json_response(
            {
                "input_tokens": row[0],
                "output_tokens": row[1],
                "cache_read_tokens": row[2],
                "total_requests": row[3],
                "invalid_requests": row[4] or 0,
            }
        )
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)


def _tool_names_of(raw: str | None, fallback: str | None) -> list[str]:
    """Decode the stored JSON array, falling back to the legacy single name."""
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            names = [str(n) for n in parsed if n]
            if names:
                return names
    return [fallback] if fallback else []


def _usage_days(request: web.Request, default: int = 7) -> int:
    try:
        days = int(request.query.get("days", default))
    except (TypeError, ValueError):
        days = default
    return max(1, min(days, 90))


async def handle_usage_tools(request: web.Request) -> web.Response:
    """Per-tool call counts, counting every call in a response.

    Rows written before `tool_names` existed only hold the first tool name; they are
    counted once and reported separately as `legacy_calls`.
    """
    try:
        days = _usage_days(request)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with dbconn(DB_PATH) as conn:
            rows = conn.execute(
                """
                WITH calls AS (
                    SELECT je.value AS name, 0 AS legacy
                    FROM api_usage,
                         json_each(CASE WHEN tool_names IS NOT NULL AND tool_names != ''
                                        THEN tool_names ELSE '[]' END) AS je
                    WHERE timestamp >= ?
                    UNION ALL
                    SELECT tool_name AS name, 1 AS legacy
                    FROM api_usage
                    WHERE timestamp >= ?
                      AND (tool_names IS NULL OR tool_names = '')
                      AND tool_name IS NOT NULL AND tool_name != ''
                )
                SELECT name, COUNT(*) AS calls, SUM(legacy) AS legacy_calls
                FROM calls
                WHERE name != ''
                GROUP BY name
                ORDER BY calls DESC
                """,
                (cutoff, cutoff),
            ).fetchall()

        tools = [{"name": r[0], "calls": r[1], "legacy_calls": r[2] or 0} for r in rows]
        return web.json_response(
            {
                "days": days,
                "total_calls": sum(t["calls"] for t in tools),
                "legacy_calls": sum(t["legacy_calls"] for t in tools),
                "tools": tools,
            }
        )
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)


async def handle_usage_layers(request: web.Request) -> web.Response:
    """Layered query: core exchange (end_turn) + process records (tool_use)."""
    try:
        level = request.query.get("level", "core")
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))

        with dbconn(DB_PATH) as conn:
            if level == "core":
                # query only end_turn (core exchange)
                rows = conn.execute(
                    """
                    SELECT id, timestamp, actual_user_input, input_preview, output_preview,
                           input_tokens, output_tokens, stop_reason, latency_ms, model
                    FROM api_usage
                    WHERE stop_reason = 'end_turn'
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                """,
                    (limit, offset),
                ).fetchall()

                result = []
                for r in rows:
                    result.append(
                        {
                            "id": r[0],
                            "timestamp": r[1],
                            "user_input": r[2]
                            or r[
                                3
                            ],  # actual_user_input first, fallback to input_preview
                            "output": r[4],
                            "input_tokens": r[5],
                            "output_tokens": r[6],
                            "stop_reason": r[7],
                            "latency_ms": r[8],
                            "model": r[9],
                        }
                    )
            elif level == "process":
                # query tool_use process records
                rows = conn.execute(
                    """
                    SELECT id, timestamp, tool_name, input_tokens, output_tokens,
                           latency_ms, parent_id, tool_names
                    FROM api_usage
                    WHERE stop_reason = 'tool_use'
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                """,
                    (limit, offset),
                ).fetchall()

                result = []
                for r in rows:
                    result.append(
                        {
                            "id": r[0],
                            "timestamp": r[1],
                            "tool_name": r[2],
                            "tool_names": _tool_names_of(r[7], r[2]),
                            "input_tokens": r[3],
                            "output_tokens": r[4],
                            "latency_ms": r[5],
                            "parent_id": r[6],
                        }
                    )
            else:
                # query all
                rows = conn.execute(
                    """
                    SELECT id, timestamp, request_level, input_preview, output_preview,
                           input_tokens, output_tokens, stop_reason, latency_ms, tool_name,
                           tool_names
                    FROM api_usage
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                """,
                    (limit, offset),
                ).fetchall()

                result = []
                for r in rows:
                    result.append(
                        {
                            "id": r[0],
                            "timestamp": r[1],
                            "level": r[2],
                            "input": r[3],
                            "output": r[4],
                            "input_tokens": r[5],
                            "output_tokens": r[6],
                            "stop_reason": r[7],
                            "latency_ms": r[8],
                            "tool_name": r[9],
                            "tool_names": _tool_names_of(r[10], r[9]),
                        }
                    )

        return web.json_response(result)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)
