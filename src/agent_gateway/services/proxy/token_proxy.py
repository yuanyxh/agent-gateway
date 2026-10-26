"""Token proxy monitoring entry point: HTTP endpoints and startup."""

import json
import sys
import time

from aiohttp import web
from agent_gateway.llm import client as llm_client  # noqa: F401 - 测试注入点
from agent_gateway.services.proxy.proxy_usage import (
    DB_PATH,
    TOOL_OUTPUT_DB_PATH,
    init_db,
    init_tool_output_db,
    record_gateway_log,
)


from agent_gateway.services.proxy.proxy_config import (
    PORT,
    PROVIDERS,
    UPSTREAM_URL,
    _close_session,
    logger,
)
from agent_gateway.services.proxy.proxy_models import (
    handle_health,
    handle_usage_layers,
    handle_usage_summary,
)
from agent_gateway.services.proxy.proxy_state import handle_proxy
# ---------------------------------------------------------------------------
# Gateway-layer log: middleware uniformly records rejected/failed/oversized requests,
# covering api_usage's troubleshooting blind spot. The recording implementation
# (record_gateway_log + constants + gateway_log table) is consolidated into proxy_usage.py;
# this entry point only keeps the middleware that catches exceptions and calls it, no longer
# maintaining the connection boilerplate separately.
# ---------------------------------------------------------------------------


@web.middleware
async def gateway_log_middleware(request, handler):
    """Wrap all routes: catch exceptions (including aiohttp 413 over-limit) and record the gateway log, without changing the original behavior."""
    start = time.monotonic()
    method = request.method
    path = request.path
    content_length = request.content_length or 0
    status_code = 0
    error_type = error_message = None
    try:
        resp = await handler(request)
        status_code = resp.status
    except web.HTTPException as e:
        status_code = e.status
        error_type = type(e).__name__
        error_message = e.text
        raise
    except Exception as e:
        status_code = 500
        error_type = type(e).__name__
        error_message = str(e)
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        record_gateway_log(
            method,
            path,
            content_length,
            status_code,
            error_type,
            error_message,
            latency_ms,
        )
    return resp


def create_app() -> web.Application:
    # aiohttp's default client_max_size is only 1MB; once a Claude Code request body
    # (including CLAUDE.md/skills/MCP tool definitions) exceeds it, it directly returns
    # 413 "Maximum request body size exceeded" and never reaches the upstream.
    # Set 64MB for headroom, with the client's 32MB check as a fallback; the proxy is no
    # longer the bottleneck.
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.middlewares.append(gateway_log_middleware)
    # The upstream pool is process-wide, so it is closed once here, not per request.
    app.on_cleanup.append(_close_session)
    app.router.add_route("*", "/health", handle_health)
    app.router.add_route("GET", "/usage/summary", handle_usage_summary)
    app.router.add_route("GET", "/usage/layers", handle_usage_layers)
    # Three entries coexist: `/v1/*` is the protocol-native entry (the client need not configure
    # base_url to an unconventional prefix), `/open/*` is the legacy compatibility alias (users
    # who already configured ANTHROPIC_BASE_URL=.../open/v1 are unaffected), `/raw/*` is the
    # transparent entry (same routing, no content rewrites — for clean agent sessions). Registered
    # after /usage/* and /health so they are not shadowed; all routes share the same handler and
    # the path is normalized in _upstream_path.
    app.router.add_route("*", "/open/{path:.*}", handle_proxy)
    app.router.add_route("*", "/raw/{path:.*}", handle_proxy)
    app.router.add_route("*", "/v1/{path:.*}", handle_proxy)
    return app


def main():
    if not UPSTREAM_URL and not PROVIDERS:
        logger.error(
            json.dumps(
                {
                    "msg": "startup_failed",
                    "error": "MODEL_PROXY_URL / MODEL_PROXY_PROVIDERS not set",
                }
            )
        )
        sys.exit(1)

    init_db()
    init_tool_output_db()
    logger.info(
        json.dumps(
            {
                "msg": "startup",
                "port": PORT,
                "upstream": UPSTREAM_URL,
                "providers": [p.get("name", "") for p in PROVIDERS],
                "db": DB_PATH,
                "tool_output_db": TOOL_OUTPUT_DB_PATH,
            }
        )
    )

    app = create_app()
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
