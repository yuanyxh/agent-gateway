"""Host-facing REST capabilities served next to the MCP endpoint.

These routes are the gateway's generic surface (read a page, summarize text, run one
LLM turn); business orchestration stays in the host. They are module-level so they can be
built and tested without booting the memory layer.
"""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_gateway.llm import client as llm_client
from agent_gateway.services.mcp.server_tool_schema import (
    DEFAULT_READ_MAX_CHARS,
    DEFAULT_SUMMARIZE_MAX_TOKENS,
)
from agent_gateway.services.mcp.server_utils import int_arg
from agent_gateway.services.read.read_service import normalize_max_chars, read_url
from agent_gateway.services.summarize import text as summarize_service


def read_args(arguments: dict) -> tuple[str, int, bool]:
    """Shared by the `read_url` MCP tool and `/api/read`; one place owns defaults."""
    return (
        str(arguments.get("url", "")).strip(),
        normalize_max_chars(int_arg(arguments, "max_chars", DEFAULT_READ_MAX_CHARS)),
        arguments.get("use_proxy", False) is True,
    )


def summarize_args(arguments: dict) -> tuple[str, str, int]:
    """Normalize `/api/summarize` arguments."""
    return (
        str(arguments.get("text", "")),
        str(arguments.get("instruction", "")),
        max(
            64,
            min(int_arg(arguments, "max_tokens", DEFAULT_SUMMARIZE_MAX_TOKENS), 4000),
        ),
    )


async def api_summarize(request):
    text, instruction, max_tokens = summarize_args(await request.json())
    result = await asyncio.to_thread(
        summarize_service.summarize_text, text, instruction, max_tokens
    )
    return JSONResponse(result)


async def api_read(request):
    result = await asyncio.to_thread(read_url, *read_args(await request.json()))
    return JSONResponse(result)


async def api_llm_chat(request):
    body = await request.json()
    number = (int, float)
    temperature = body.get("temperature")
    timeout = body.get("timeout")
    extra_body = body.get("extra_body")
    text = await asyncio.to_thread(
        llm_client.chat,
        str(body.get("prompt", "")),
        system=str(body.get("system", "")) or None,
        temperature=float(temperature) if isinstance(temperature, number) else None,
        max_tokens=int_arg(body, "max_tokens", 1024),
        timeout=float(timeout) if isinstance(timeout, number) else 120,
        disable_thinking=body.get("disable_thinking") is True,
        extra_body=extra_body if isinstance(extra_body, dict) else None,
        label=str(body.get("label", "host_chat")),
    )
    if text is None:
        return JSONResponse({"ok": False, "error": "llm_unavailable"})
    return JSONResponse({"ok": True, "text": text})


REST_ROUTES = [
    Route("/api/summarize", endpoint=api_summarize, methods=["POST"]),
    Route("/api/read", endpoint=api_read, methods=["POST"]),
    Route("/api/llm/chat", endpoint=api_llm_chat, methods=["POST"]),
]
