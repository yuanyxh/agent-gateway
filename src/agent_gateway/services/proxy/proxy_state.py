"""Per-request proxy state."""

from dataclasses import dataclass
import json

from aiohttp import web
from agent_gateway.services.proxy.proxy_client import (
    matches_accel_client,
)
from agent_gateway.services.proxy.proxy_protocol import (
    _detect_api_format,
    _inject_classifier_disabled,
    _inject_system_prompt,
    _is_classifier_candidate,
)
from agent_gateway.services.proxy.proxy_provider import (
    api_format_of,
)


from agent_gateway.services.proxy.proxy_config import (
    CLIENT_ACCEL,
    PROVIDERS,
    UPSTREAM_URL,
    _api_format_from_path,
    _logged_first_body,
    _provider_path,
    _resolve_provider,
    _upstream_path,
    logger,
)
from agent_gateway.services.proxy.proxy_models import _handle_models
from agent_gateway.services.proxy.proxy_rewrite import _apply_rewrites, _forward


@dataclass
class _ProxyState:
    """Everything the forwarding stages share; filled in by `_prepare_request`."""

    request: web.Request
    path: str
    api_format: str
    provider: dict
    provider_format: str
    request_body: dict
    body_bytes: bytes
    record_request_body: dict
    note_parts: list[str]
    upstream: str
    #: Transparent entry (`/raw/*`): skip every content rewrite, recording still applies.
    raw: bool
    #: 实际命中的 provider 与选择原因，写入 api_usage 供追溯路由。
    route_tier: str
    route_reason: str
    route_detail: str


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    """Forward the request to the upstream API, returning the response in a streaming manner."""
    if not UPSTREAM_URL and not PROVIDERS:
        return web.json_response(
            {"error": "MODEL_PROXY_URL / MODEL_PROXY_PROVIDERS not configured"},
            status=503,
        )

    # Entry path is also the rewrite switch: /raw is transparent, /open and /v1 keep rewrites
    raw = request.path.startswith("/raw/")
    path = _upstream_path(request.path)

    # Gateway model discovery: GET /v1/models (or /open/v1/models) → return all provider
    # models, for the client model selector to list routes. The response schema is chosen by
    # protocol, see _models_schema_for.
    if request.method == "GET" and path == "v1/models":
        return await _handle_models(request)
    state = await _prepare_request(request, path, raw=raw)
    _apply_rewrites(state)
    return await _forward(state)


async def _prepare_request(
    request: web.Request, path: str, *, raw: bool = False
) -> _ProxyState:
    """Read the body, resolve the protocol/provider, inject rules, and capture the record copy."""
    # Read the request body
    body_bytes = await request.read()
    request_body = {}
    try:
        request_body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        pass

    # Client API format: **entry path takes priority** (protocol declaration); only when the
    # path does not indicate the protocol does it fall back to the request-body heuristic.
    # When the two disagree, log format_mismatch and **do not silently trust either** — the
    # disagreement itself is a signal (the client used a non-standard path, or the heuristic
    # misjudged), and silence would make the wrong conversion unattributable.
    api_format = _api_format_from_path(path)
    if api_format is None:
        api_format = _detect_api_format(request_body)
    else:
        by_body = _detect_api_format(request_body)
        if by_body != api_format:
            logger.warning(
                json.dumps(
                    {
                        "msg": "format_mismatch",
                        "path": request.path,
                        "by_path": api_format,
                        "by_body": by_body,
                        "effect": "use the path criterion",
                    }
                )
            )

    requested_model = request_body.get("model", "")
    provider, upstream_model = _resolve_provider(requested_model)
    request_body["model"] = upstream_model
    if not PROVIDERS:
        route_tier = provider.get("name") or "legacy"
        route_reason = "legacy_upstream"
    else:
        route_tier = provider.get("name") or "unknown"
        matched = any(requested_model in p.get("models", []) for p in PROVIDERS)
        route_reason = "model_match" if matched else "default_provider"
        if not matched:
            logger.warning(
                json.dumps(
                    {
                        "msg": "route_fallback",
                        "requested": requested_model,
                        "provider": route_tier,
                        "upstream_model": upstream_model,
                    }
                )
            )
    route_detail = f"requested={requested_model or '<empty>'}"

    provider_format = api_format_of(provider)
    body_bytes = json.dumps(request_body).encode()

    # Build the upstream URL. The entry forms are already normalized in `path`; when the
    # provider's protocol differs, the endpoint path must follow the provider (a converted
    # body sent to the client protocol's path hits a non-existent endpoint upstream).
    # e.g. client anthropic "v1/messages" + openai provider → provider.url/v1/chat/completions
    upstream = f"{provider['url']}/{_provider_path(path, provider_format)}"
    if request.query_string:
        upstream += f"?{request.query_string}"

    # Safety classification request (classifier) disables thinking to speed up the response.
    # The structural criterion (_is_classifier_candidate) is client-independent; **whether it
    # is enabled is decided by the configurable client allowlist** (config.yaml
    # modelProxy.clientAccel) — client identity is not a protocol fact, and hard-coding a
    # single client would leave other clients unable to get acceleration for requests of the
    # same structure, hitting the upstream timeout.
    # The critical-path log distinguishes three return points: matched allowlist and is a
    # candidate logs info; matched allowlist but not a candidate logs debug (to observe shape
    # changes); not matched logs debug (with a UA summary, to help discover new clients that
    # need registering).
    accel_client = matches_accel_client(request.headers, CLIENT_ACCEL)
    if accel_client:
        if _is_classifier_candidate(request_body):
            _inject_classifier_disabled(request_body, api_format)
            body_bytes = json.dumps(request_body).encode()
            logger.debug(
                json.dumps(
                    {
                        "msg": "classifier_thinking_disabled",
                        "client": accel_client,
                        "model": request_body.get("model", ""),
                        "messages": len(request_body.get("messages", [])),
                        "api_format": api_format,
                    }
                )
            )
        else:
            logger.debug(
                json.dumps(
                    {
                        "msg": "classifier_check_skip",
                        "client": accel_client,
                        "model": request_body.get("model", ""),
                        "stream": request_body.get("stream"),
                        "tools": len(request_body.get("tools", [])),
                        "messages": len(request_body.get("messages", [])),
                    }
                )
            )
    else:
        logger.info(
            json.dumps(
                {
                    "msg": "classifier_client_not_allowed",
                    # UA truncation: only for discovering new clients that need registering, not persisting the whole string
                    "user_agent": request.headers.get("user-agent", "")[:80],
                    "api_format": api_format,
                }
            )
        )

    # Rules: the full block on every real user input
    rule_count = 0
    if not raw:
        injected, rule_count = _inject_system_prompt(request_body)
        if injected is not None:
            body_bytes = injected

    # First-request log: output the full body after injection, to verify the system prompt takes effect
    global _logged_first_body
    if not _logged_first_body:
        _logged_first_body = True
        try:
            parsed = json.loads(body_bytes)
            # Output only the system field, the injected rule count, and model, to avoid flooding
            log_body = {
                "model": parsed.get("model", ""),
                "rule_count": rule_count,
                "system": (
                    parsed.get("system")
                    if api_format == "anthropic"
                    else parsed.get("messages", [{}])[0]
                ),
            }
            logger.info(
                json.dumps(
                    {
                        "msg": "first_request_body",
                        "api_format": api_format,
                        "body_debug": log_body,
                    }
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                json.dumps({"msg": "first_body_log_failed", "error": str(e)})
            )

    # Record the original (post-injection) request body before conversion, for record_usage
    # context extraction (the structure is the client format)
    record_request_body = json.loads(body_bytes) if body_bytes else {}

    # The list of rewrites triggered this turn, for the response-side "applied: …" append
    note_parts: list[str] = []
    return _ProxyState(
        request=request,
        path=path,
        api_format=api_format,
        provider=provider,
        provider_format=provider_format,
        request_body=request_body,
        body_bytes=body_bytes,
        record_request_body=record_request_body,
        note_parts=note_parts,
        upstream=upstream,
        raw=raw,
        route_tier=route_tier,
        route_reason=route_reason,
        route_detail=route_detail,
    )
