"""Usage and tool-output persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from agent_gateway.llm import client as llm_client
from agent_gateway.core.config import data_dir
from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.proxy.proxy_client import load_client_text
from agent_gateway.services.proxy.proxy_normalize import _result_text as result_text
from agent_gateway.services.proxy.proxy_protocol import strip_injected_rules


from agent_gateway.services.proxy.usage_constants import (
    DB_PATH,
    TOOL_OUTPUT_DB_PATH,
    logger,
)
from agent_gateway.services.proxy.usage_extract import (
    compute_session_key,
    extract_request_context,
    extract_response_context,
)
from agent_gateway.services.proxy.usage_resolver import resolver


def record_tool_outputs(
    tool_outputs: list[dict],
    api_usage_id: int | None = None,
    stop_reason: str = "",
    model: str = "",
    session_key: str = "",
):
    """Write tool outputs into the independent tool_output_log DB.

    It belongs to a different DB file from api_usage and cannot share a transaction, so a
    separate connection is opened. A write failure is only logged and does not affect the
    main flow — tool output is analysis material and should not drag down usage recording.
    """
    if not tool_outputs:
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        with dbconn(TOOL_OUTPUT_DB_PATH) as c:
            c.executemany(
                "INSERT INTO tool_output_log "
                "(api_usage_id, timestamp, tool_name, tool_call_id, "
                "output_text, output_text_raw, output_size, output_lines, "
                "is_error, stop_reason, model, session_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        api_usage_id,
                        now,
                        t.get("tool_name", ""),
                        t.get("tool_call_id", ""),
                        t.get("output_text", ""),
                        t.get("output_text_raw", ""),
                        t.get("output_size", 0),
                        t.get("output_lines", 0),
                        t.get("is_error"),
                        stop_reason,
                        model,
                        session_key,
                    )
                    for t in tool_outputs
                ],
            )
    except Exception as e:
        logger.error(json.dumps({"msg": "tool_output_record_failed", "error": str(e)}))


async def record_usage(
    request_body: dict,
    response_body: dict,
    status: int,
    latency_ms: int,
    output_preview: str = "",
    error_message: str = "",
    tool_name_override: str = "",
    api_format: str = "anthropic",
    tool_outputs: list[dict] | None = None,
):
    """Record the token usage of one API call.

    The optional `tool_outputs` (produced by `extract_tool_outputs`) is written into
    tool_output_log as well, carrying this row's `api_usage_id` — the two tables belong to
    different DB files, and the auto-increment id can only be obtained here; splitting them
    into two background tasks to write would leave that column always NULL, breaking the
    correlation analysis between tool-output volume and token usage.
    """
    if status != 200:
        return

    # Resolve usage
    usage = await resolver.resolve(response_body)

    # Extract context
    req_ctx = extract_request_context(request_body)
    resp_ctx = extract_response_context(response_body)

    model = response_body.get("model", "unknown")
    stop_reason = resp_ctx["stop_reason"]

    # Use error_message from resp_ctx (if not provided)
    if not error_message:
        error_message = resp_ctx.get("error_message", "")

    # Determine request_level
    request_level = "core" if stop_reason == "end_turn" else "process"

    # Extract tool_name: SSE stream extraction takes priority > request-body assistant message fallback
    tool_name = tool_name_override
    if not tool_name and stop_reason == "tool_use":
        # Extract the tool name from the request (fallback)
        messages = request_body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            break
                if tool_name:
                    break

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_type": req_ctx["request_type"],
        "context_summary": req_ctx["context_summary"],
        "input_preview": req_ctx["input_preview"],
        "actual_user_input": req_ctx["actual_user_input"],
        "input_message_count": req_ctx["input_message_count"],
        "has_tool_result": req_ctx["has_tool_result"],
        "output_preview": output_preview,
        "output_type": resp_ctx["output_type"],
        "stop_reason": stop_reason,
        "response_valid": resp_ctx["response_valid"],
        "model": model,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "latency_ms": latency_ms,
        "api_format": api_format,
        "request_level": request_level,
        "tool_name": tool_name,
        "parent_id": None,
        "error_message": error_message,
    }

    try:
        with dbconn(DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO api_usage "
                "(timestamp, request_type, context_summary, "
                "input_preview, actual_user_input, input_message_count, has_tool_result, output_preview, "
                "output_type, stop_reason, response_valid, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, "
                "latency_ms, api_format, request_level, tool_name, parent_id, error_message) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["timestamp"],
                    record["request_type"],
                    record["context_summary"],
                    record["input_preview"],
                    record["actual_user_input"],
                    record["input_message_count"],
                    record["has_tool_result"],
                    record["output_preview"],
                    record["output_type"],
                    record["stop_reason"],
                    record["response_valid"],
                    record["model"],
                    record["input_tokens"],
                    record["output_tokens"],
                    record["cache_read_tokens"],
                    record["cache_write_tokens"],
                    record["latency_ms"],
                    record["api_format"],
                    record["request_level"],
                    record["tool_name"],
                    record["parent_id"],
                    record["error_message"],
                ),
            )
            record_id = cursor.lastrowid
        logger.info(
            json.dumps(
                {
                    "msg": "usage_recorded",
                    "model": model,
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "latency_ms": latency_ms,
                    "request_level": request_level,
                    "tool_name": tool_name,
                }
            )
        )

        # Persist tool outputs: with api_usage_id association, for cross-analysis of "output volume ↔ token usage"
        if tool_outputs:
            record_tool_outputs(
                tool_outputs,
                api_usage_id=record_id,
                stop_reason=stop_reason,
                model=model,
                session_key=compute_session_key(request_body),
            )
    except Exception as e:
        logger.error(json.dumps({"msg": "usage_record_failed", "error": str(e)}))
