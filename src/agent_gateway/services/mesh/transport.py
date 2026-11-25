"""Transports deliver a message to the service's endpoint."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from agent_gateway.services.mesh.models import MeshErrorCode, MeshMessage, ServiceCard


class TransportError(Exception):
    def __init__(
        self,
        code: MeshErrorCode,
        detail: str = "",
        startable: bool | None = None,
    ):
        super().__init__(detail or code.value)
        self.code = code
        self.startable = startable


class Transport(Protocol):
    def deliver(self, card: ServiceCard, message: MeshMessage) -> None: ...

    def probe(self, card: ServiceCard, timeout: float = 1.5) -> bool: ...


class DirectoryTransport:
    """Writes one JSON file per message into the card's inbox directory."""

    def __init__(self, inbox_root: Path):
        self.inbox_root = Path(inbox_root)

    def mailbox(self, card: ServiceCard) -> Path:
        box = Path(card.endpoint)
        return box if box.is_absolute() else self.inbox_root / box

    def deliver(self, card: ServiceCard, message: MeshMessage) -> None:
        box = self.mailbox(card)
        try:
            box.mkdir(parents=True, exist_ok=True)
            stamp = int(message.sent_at.timestamp() * 1000)
            target = box / f"{stamp}-{message.id}.json"
            self._write(target, message)
        except OSError as exc:
            raise TransportError(MeshErrorCode.DELIVERY_FAILED, str(exc)) from exc

    def _write(self, target: Path, message: MeshMessage) -> None:
        handle = tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp", delete=False
        )
        try:
            with handle:
                handle.write(message.model_dump_json(indent=2).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, target)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def probe(self, card: ServiceCard, timeout: float = 1.5) -> bool:
        return self.mailbox(card).is_dir()


class Peer(BaseModel):
    name: str
    base_url: str
    token: str = ""


class HTTPTransport:
    """Forwards to a sibling gateway; every remote call carries a Bearer token."""

    def __init__(
        self,
        peers: list[Peer],
        timeout: float = 3.0,
        client: httpx.Client | None = None,
    ):
        self.peers = peers
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def _peer_for(self, card: ServiceCard) -> Peer | None:
        for peer in self.peers:
            if peer.name == card.owner_gateway:
                return peer
        for peer in self.peers:
            if card.endpoint.startswith(peer.base_url.rstrip("/")):
                return peer
        return None

    def _base_for(self, card: ServiceCard) -> tuple[str, str]:
        if card.transport == "http":
            peer = self._peer_for(card)
            return card.endpoint, peer.token if peer else ""
        peer = self._peer_for(card)
        if peer is None:
            raise TransportError(MeshErrorCode.PEER_UNREACHABLE, card.owner_gateway)
        return peer.base_url, peer.token

    def deliver(self, card: ServiceCard, message: MeshMessage) -> None:
        base, token = self._base_for(card)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = {"message": message.model_dump(mode="json")}
        try:
            response = self._client.post(
                f"{base.rstrip('/')}/mesh/send", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TransportError(MeshErrorCode.PEER_UNREACHABLE, str(exc)) from exc
        if response.is_success:
            return
        code = MeshErrorCode.PEER_UNREACHABLE
        startable = None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            raw = payload.get("error_code")
            if raw in {item.value for item in MeshErrorCode}:
                code = MeshErrorCode(raw)
            if isinstance(payload.get("startable"), bool):
                startable = payload["startable"]
        raise TransportError(code, response.text[:200], startable=startable)

    def probe(self, card: ServiceCard, timeout: float = 1.5) -> bool:
        try:
            base, token = self._base_for(card)
        except TransportError:
            return False
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._client.get(
                f"{base.rstrip('/')}/mesh/health", headers=headers, timeout=timeout
            )
            return response.is_success
        except httpx.HTTPError:
            return False

    def list_peer(self, peer: Peer) -> list[ServiceCard]:
        headers = {"Authorization": f"Bearer {peer.token}"} if peer.token else {}
        try:
            response = self._client.get(
                f"{peer.base_url.rstrip('/')}/mesh/services",
                params={"remote": "0"},
                headers=headers,
                timeout=min(2.0, self.timeout),
            )
        except httpx.HTTPError:
            return []
        if not response.is_success:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        items = payload.get("services", []) if isinstance(payload, dict) else []
        cards = []
        for item in items:
            try:
                cards.append(ServiceCard.model_validate(item))
            except ValueError:
                continue
        return cards

    def find(
        self, service_id: str, total_timeout: float = 2.0
    ) -> tuple[Peer, ServiceCard] | None:
        for peer in self.peers:
            headers = {"Authorization": f"Bearer {peer.token}"} if peer.token else {}
            try:
                response = self._client.get(
                    f"{peer.base_url.rstrip('/')}/mesh/services/{service_id}",
                    headers=headers,
                    timeout=min(total_timeout, self.timeout),
                )
            except httpx.HTTPError:
                continue
            if response.status_code == 404 or not response.is_success:
                continue
            try:
                payload = response.json()
                if isinstance(payload, dict) and "card" in payload:
                    payload = payload["card"]
                return peer, ServiceCard.model_validate(payload)
            except ValueError:
                continue
        return None
