"""Routes a message to a local card or a sibling gateway."""

from __future__ import annotations

from pathlib import Path

from agent_gateway.services.mesh.models import MeshErrorCode, MeshMessage, ServiceCard
from agent_gateway.services.mesh.registry import FileRegistry, RegistryError
from agent_gateway.services.mesh.transport import (
    DirectoryTransport,
    HTTPTransport,
    TransportError,
)


class Router:
    def __init__(
        self,
        registry: FileRegistry,
        local: DirectoryTransport,
        remote: HTTPTransport | None = None,
        gateway_id: str = "",
    ):
        self.registry = registry
        self.local = local
        self.remote = remote
        self.gateway_id = gateway_id or registry.gateway_id

    def register(self, card: ServiceCard) -> dict:
        if card.transport == "directory":
            root = self.local.inbox_root.resolve()
            raw = Path(card.endpoint)
            resolved = (raw if raw.is_absolute() else root / raw).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                return {
                    "ok": False,
                    "error_code": MeshErrorCode.INVALID_CARD.value,
                    "detail": "endpoint outside mesh inbox",
                }
            card = card.model_copy(update={"endpoint": str(resolved)})
        try:
            stored = self.registry.register(card)
        except RegistryError as exc:
            return {"ok": False, "error_code": exc.code.value, "detail": str(exc)}
        return {"ok": True, "card": stored.model_dump(mode="json")}

    def absorb(self, card: ServiceCard, ttl_cap: int = 300) -> bool:
        """Register a peer's sender card on first contact; only agent-like kinds."""
        if card.kind not in ("workspace", "agent"):
            return False
        if self.registry.get(card.id, include_expired=True) is not None:
            return False
        stored = card.model_copy(
            update={
                "ttl_seconds": min(card.ttl_seconds, ttl_cap),
                "owner_gateway": card.owner_gateway or "remote",
            }
        )
        try:
            self.registry.register(stored)
        except RegistryError:
            return False
        return True

    def deregister(self, service_id: str) -> dict:
        removed = self.registry.deregister(service_id)
        if not removed:
            return {"ok": False, "error_code": MeshErrorCode.TARGET_UNKNOWN.value}
        return {"ok": True}

    def send(self, message: MeshMessage) -> dict:
        card = self.registry.get(message.recipient, include_expired=True)
        is_local = card is not None and card.owner_gateway == self.gateway_id
        if is_local:
            is_start = (
                message.kind == "control" and message.payload.get("action") == "start"
            )
            if not card.is_alive() and not is_start:
                return {
                    "ok": False,
                    "error_code": MeshErrorCode.TARGET_OFFLINE.value,
                    "startable": card.startable,
                }
            if is_start and not card.startable:
                return {
                    "ok": False,
                    "error_code": MeshErrorCode.TARGET_OFFLINE.value,
                    "startable": False,
                }
            try:
                self.local.deliver(card, message)
            except TransportError as exc:
                return {
                    "ok": False,
                    "error_code": exc.code.value,
                    "detail": str(exc),
                    "startable": exc.startable,
                }
            self.registry.touch(
                message.recipient, status="starting" if is_start else "pending"
            )
            return {"ok": True, "message_id": message.id, "started": is_start}

        if self.remote is not None:
            found = self.remote.find(message.recipient)
            if found is not None:
                _, remote_card = found
                try:
                    self.remote.deliver(remote_card, message)
                except TransportError as exc:
                    return {
                        "ok": False,
                        "error_code": exc.code.value,
                        "detail": str(exc),
                        "startable": exc.startable,
                    }
                return {"ok": True, "message_id": message.id, "forwarded": True}

        return {"ok": False, "error_code": MeshErrorCode.TARGET_UNKNOWN.value}

    def probe(self, service_id: str) -> dict:
        card = self.registry.get(service_id, include_expired=True)
        if card is not None and card.owner_gateway == self.gateway_id:
            status = "ready" if card.is_alive() else "offline"
            return {"ok": card.is_alive(), "status": status, "local": True}
        if self.remote is not None:
            found = self.remote.find(service_id)
            if found is not None:
                alive = found[1].is_alive()
                return {
                    "ok": alive,
                    "status": "ready" if alive else "offline",
                    "local": False,
                }
        return {"ok": False, "status": "unknown", "local": False}

    def list(self, include_remote: bool = True) -> list[dict]:
        cards = [card.model_dump(mode="json") for card in self.registry.list()]
        if include_remote and self.remote is not None:
            seen = {card["id"] for card in cards}
            for peer in self.remote.peers:
                for card in self.remote.list_peer(peer):
                    if card.id not in seen:
                        seen.add(card.id)
                        cards.append(card.model_dump(mode="json"))
        return cards
