"""MCP tools backed by the mesh router.

The plain functions stay separate so tests exercise them without an MCP server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import anyio
from pydantic import ValidationError

from agent_gateway.services.mesh.models import (
    SERVICE_STATUSES,
    MeshErrorCode,
    MeshMessage,
    ServiceCard,
)
from agent_gateway.services.mesh.router import Router

DISABLED = {"ok": False, "error_code": "mesh_disabled"}


def _payload(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"text": str(raw)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def register(
    router: Router,
    name: str,
    kind: str = "agent",
    transport: str = "directory",
    endpoint: str = "",
    capabilities: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    try:
        card = ServiceCard(
            id=name,
            name=name,
            kind=kind,
            transport=transport,
            endpoint=endpoint or name,
            capabilities=capabilities or [],
            metadata=metadata or {},
        )
    except ValidationError as exc:
        return {
            "ok": False,
            "error_code": MeshErrorCode.INVALID_CARD.value,
            "detail": str(exc),
        }
    return router.register(card)


def deregister(router: Router, service_id: str) -> dict:
    return router.deregister(service_id)


def send(
    router: Router,
    sender: str,
    recipient: str,
    kind: str = "notify",
    payload: str | dict | None = None,
    reply_to: str | None = None,
) -> dict:
    try:
        message = MeshMessage(
            sender=sender,
            recipient=recipient,
            kind=kind,
            payload=_payload(payload),
            reply_to=reply_to,
        )
    except ValidationError as exc:
        code = (
            MeshErrorCode.PAYLOAD_TOO_LARGE
            if "payload exceeds" in str(exc)
            else MeshErrorCode.INVALID_MESSAGE
        )
        return {"ok": False, "error_code": code.value, "detail": str(exc)}
    return router.send(message)


def inbox(router: Router, service_id: str, limit: int = 20) -> dict:
    card = router.registry.get(service_id, include_expired=True)
    if card is None:
        return {"ok": False, "error_code": MeshErrorCode.TARGET_UNKNOWN.value}
    box = router.local.mailbox(card)
    if not box.is_dir():
        return {"ok": True, "messages": []}
    files = sorted(box.glob("*.json"), reverse=True)[: max(0, int(limit))]
    messages = []
    for path in files:
        try:
            messages.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return {"ok": True, "messages": messages}


def start(router: Router, service_id: str) -> dict:
    card = router.registry.get(service_id, include_expired=True)
    remote = False
    if card is None and router.remote is not None:
        found = router.remote.find(service_id)
        if found is not None:
            card = found[1]
            remote = True
    if card is None:
        return {"ok": False, "error_code": MeshErrorCode.TARGET_UNKNOWN.value}
    if card.is_alive() and card.status != "offline":
        return {"ok": True, "status": "already_running"}
    if not card.startable:
        return {"ok": False, "error_code": MeshErrorCode.TARGET_OFFLINE.value}
    request = MeshMessage(
        sender="gateway",
        recipient=service_id,
        kind="control",
        payload={"action": "start"},
        sent_at=datetime.now(timezone.utc),
    )
    result = router.send(request)
    if not result.get("ok"):
        return result
    if not remote:
        router.registry.touch(service_id, status="starting")
    return {"ok": True, "status": "starting"}


def heartbeat(router: Router, service_id: str, status: str | None = None) -> dict:
    if status is not None and status not in SERVICE_STATUSES:
        return {
            "ok": False,
            "error_code": MeshErrorCode.INVALID_MESSAGE.value,
            "detail": "bad status",
        }
    if not router.registry.touch(service_id, status=status):
        return {"ok": False, "error_code": MeshErrorCode.TARGET_UNKNOWN.value}
    return {"ok": True}


def services(router: Router) -> dict:
    return {"ok": True, "services": router.list()}


def probe(router: Router, service_id: str) -> dict:
    return router.probe(service_id)


def register_mesh_tools(mcp) -> None:
    def tool(**kwargs):
        return mcp.tool(structured_output=False, **kwargs)

    def current() -> Router | None:
        from agent_gateway.services.mesh import state

        return state.get_router()

    def rendered(result: dict) -> str:
        return json.dumps(result, ensure_ascii=False)

    @tool(
        name="service_list",
        description="List registered mesh services, local and peers.",
    )
    async def service_list() -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        return rendered(await anyio.to_thread.run_sync(services, router))

    @tool(
        name="service_send",
        description="Send a message to another mesh service. Fails immediately when the target is offline.",
    )
    async def service_send(
        sender: str,
        recipient: str,
        kind: str = "notify",
        payload: str = "",
        reply_to: str | None = None,
    ) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        result = await anyio.to_thread.run_sync(
            send, router, sender, recipient, kind, payload, reply_to
        )
        return rendered(result)

    @tool(
        name="service_inbox", description="Read messages delivered to a mesh service."
    )
    async def service_inbox(service_id: str, limit: int = 20) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        return rendered(
            await anyio.to_thread.run_sync(inbox, router, service_id, limit)
        )

    @tool(
        name="service_start",
        description="Ask a registered service to start; only works when the card is startable.",
    )
    async def service_start(service_id: str) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        return rendered(await anyio.to_thread.run_sync(start, router, service_id))

    @tool(
        name="service_heartbeat",
        description="Refresh a service heartbeat and optionally its status.",
    )
    async def service_heartbeat(service_id: str, status: str | None = None) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        return rendered(
            await anyio.to_thread.run_sync(heartbeat, router, service_id, status)
        )

    @tool(name="service_probe", description="Check whether a mesh service is alive.")
    async def service_probe(service_id: str) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        return rendered(await anyio.to_thread.run_sync(probe, router, service_id))

    @tool(
        name="service_register", description="Register or update a mesh service card."
    )
    async def service_register(
        name: str,
        kind: str = "agent",
        transport: str = "directory",
        endpoint: str = "",
        capabilities: str = "",
        metadata: str = "",
    ) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        caps = [item.strip() for item in capabilities.split(",") if item.strip()]
        return rendered(
            await anyio.to_thread.run_sync(
                register,
                router,
                name,
                kind,
                transport,
                endpoint,
                caps,
                _payload(metadata),
            )
        )

    @tool(
        name="service_deregister",
        description="Remove a mesh service from the registry.",
    )
    async def service_deregister(service_id: str) -> str:
        router = current()
        if router is None:
            return rendered(DISABLED)
        return rendered(await anyio.to_thread.run_sync(deregister, router, service_id))
