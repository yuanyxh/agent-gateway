"""HTTP surface for the mesh, mounted next to the MCP routes."""

from __future__ import annotations

from typing import Awaitable, Callable

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_gateway.services.mesh.models import (
    SERVICE_STATUSES,
    ServiceCard,
    MeshErrorCode,
    MeshMessage,
)
from agent_gateway.services.mesh.router import Router

LOCAL_HOSTS = {"127.0.0.1", "::1"}
STATUS_BY_ERROR = {
    MeshErrorCode.INVALID_CARD: 400,
    MeshErrorCode.INVALID_MESSAGE: 400,
    MeshErrorCode.PAYLOAD_TOO_LARGE: 413,
    MeshErrorCode.UNAUTHORIZED: 401,
    MeshErrorCode.TARGET_UNKNOWN: 404,
    MeshErrorCode.TARGET_OFFLINE: 409,
    MeshErrorCode.REGISTRY_FULL: 409,
    MeshErrorCode.PEER_UNREACHABLE: 502,
    MeshErrorCode.DELIVERY_FAILED: 500,
}


def error_response(code: MeshErrorCode, detail: str = "") -> JSONResponse:
    content = {"ok": False, "error_code": code.value}
    if detail:
        content["detail"] = detail
    return JSONResponse(content, status_code=STATUS_BY_ERROR.get(code, 400))


def _is_local(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in LOCAL_HOSTS


def _authorized(request: Request, token: str) -> bool:
    if _is_local(request):
        return True
    if not token:
        return False
    return request.headers.get("authorization", "") == f"Bearer {token}"


def _guard(handler: Callable[[Request], Awaitable[JSONResponse]], token: str):
    async def guarded(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return error_response(MeshErrorCode.UNAUTHORIZED)
        return await handler(request)

    return guarded


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_mesh_routes(
    router: Router,
    token: str,
    gateway_id: str,
    endpoint_base: str,
) -> list[Route]:
    async def register(request: Request) -> JSONResponse:
        if not _is_local(request):
            return error_response(MeshErrorCode.UNAUTHORIZED)
        payload = await _body(request)
        try:
            card = ServiceCard.model_validate(payload.get("card", payload))
        except ValidationError as exc:
            return error_response(MeshErrorCode.INVALID_CARD, str(exc))
        result = await run_in_threadpool(router.register, card)
        code = MeshErrorCode(result["error_code"]) if not result["ok"] else None
        status = STATUS_BY_ERROR.get(code, 409) if code else 200
        return JSONResponse(result, status_code=status)

    async def deregister(request: Request) -> JSONResponse:
        if not _is_local(request):
            return error_response(MeshErrorCode.UNAUTHORIZED)
        payload = await _body(request)
        result = await run_in_threadpool(
            router.deregister, str(payload.get("service_id") or "")
        )
        return JSONResponse(result, status_code=200 if result["ok"] else 404)

    async def send(request: Request) -> JSONResponse:
        payload = await _body(request)
        try:
            message = MeshMessage.model_validate(payload.get("message", payload))
        except ValidationError as exc:
            code = (
                MeshErrorCode.PAYLOAD_TOO_LARGE
                if "payload exceeds" in str(exc)
                else MeshErrorCode.INVALID_MESSAGE
            )
            return error_response(code, str(exc))
        if not _is_local(request):
            sender_card = payload.get("sender_card")
            if isinstance(sender_card, dict):
                try:
                    router.absorb(ServiceCard.model_validate(sender_card))
                except ValidationError:
                    pass
        result = await run_in_threadpool(router.send, message)
        if result["ok"]:
            return JSONResponse(result)
        code = MeshErrorCode(result["error_code"])
        content: dict = {"ok": False, "error_code": code.value}
        for key in ("detail", "startable"):
            if key in result:
                content[key] = result[key]
        return JSONResponse(content, status_code=STATUS_BY_ERROR.get(code, 400))

    async def heartbeat(request: Request) -> JSONResponse:
        payload = await _body(request)
        service_id = str(payload.get("service_id") or "")
        status = payload.get("status")
        if status is not None and status not in SERVICE_STATUSES:
            return error_response(MeshErrorCode.INVALID_MESSAGE, "bad status")
        updated = await run_in_threadpool(router.registry.touch, service_id, status)
        if not updated:
            return error_response(MeshErrorCode.TARGET_UNKNOWN)
        return JSONResponse({"ok": True})

    async def services(request: Request) -> JSONResponse:
        include_remote = request.query_params.get("remote", "1") != "0"
        cards = await run_in_threadpool(router.list, include_remote)
        return JSONResponse({"ok": True, "services": cards})

    async def service(request: Request) -> JSONResponse:
        card = router.registry.get(
            request.path_params["service_id"], include_expired=True
        )
        if card is None:
            return error_response(MeshErrorCode.TARGET_UNKNOWN)
        return JSONResponse({"ok": True, "card": card.model_dump(mode="json")})

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "gateway": gateway_id})

    async def agent_card(request: Request) -> JSONResponse:
        card = ServiceCard(
            id=gateway_id,
            name="agent-gateway",
            kind="gateway",
            owner_gateway=gateway_id,
            transport="http",
            endpoint=endpoint_base,
            capabilities=["mesh"],
            status="ready",
        )
        return JSONResponse(card.model_dump(mode="json"))

    routes = [
        Route("/mesh/register", _guard(register, token), methods=["POST"]),
        Route("/mesh/deregister", _guard(deregister, token), methods=["POST"]),
        Route("/mesh/send", _guard(send, token), methods=["POST"]),
        Route("/mesh/heartbeat", _guard(heartbeat, token), methods=["POST"]),
        Route("/mesh/services", _guard(services, token), methods=["GET"]),
        Route("/mesh/services/{service_id}", _guard(service, token), methods=["GET"]),
        Route("/mesh/health", _guard(health, token), methods=["GET"]),
        Route(
            "/.well-known/agent-card.json", _guard(agent_card, token), methods=["GET"]
        ),
    ]
    return routes


def remote_token_middleware(app, token: str):
    """Reject non-loopback HTTP calls unless they carry the shared Bearer token."""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        client = scope.get("client") or ("", 0)
        if client[0] in LOCAL_HOSTS:
            return await app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        expected = f"Bearer {token}".encode()
        if token and headers.get(b"authorization", b"") == expected:
            return await app(scope, receive, send)
        response = JSONResponse(
            {"ok": False, "error_code": "unauthorized"}, status_code=401
        )
        return await response(scope, receive, send)

    return wrapped
