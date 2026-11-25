"""Mesh settings read from the gateway YAML."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path

from agent_gateway.core import config_loader, paths
from agent_gateway.services.mesh.transport import Peer


@dataclass
class MeshSettings:
    enabled: bool
    gateway_id: str
    data_dir: Path
    token: str = ""
    peers: list[Peer] = field(default_factory=list)
    heartbeat_ttl: int = 120


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _peers(raw: object) -> list[Peer]:
    if not isinstance(raw, list):
        return []
    peers = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        base_url = str(item.get("baseUrl") or "")
        if not name or not base_url:
            continue
        peers.append(
            Peer(name=name, base_url=base_url, token=str(item.get("token") or ""))
        )
    return peers


def load(mcp_enabled: bool) -> MeshSettings | None:
    """Return settings when the mesh should run; `None` disables every route."""
    if not mcp_enabled:
        return None
    if not _as_bool(config_loader.value("server.mesh.enabled", None), True):
        return None
    gateway_id = str(config_loader.value("server.mesh.gatewayId", "") or "")
    if not gateway_id:
        gateway_id = f"gw-{socket.gethostname().split('.')[0] or 'local'}"
    raw_dir = str(config_loader.value("server.mesh.dataDir", "") or "").strip()
    data_dir = Path(raw_dir).expanduser() if raw_dir else paths.gateway_home() / "mesh"
    token = str(config_loader.value("server.mesh.token", "") or "")
    ttl = int(config_loader.value("server.mesh.heartbeatTtlSeconds", 120) or 120)
    peers = _peers(config_loader.value("server.mesh.peers", None))
    return MeshSettings(
        enabled=True,
        gateway_id=gateway_id,
        data_dir=data_dir,
        token=token,
        peers=peers,
        heartbeat_ttl=ttl,
    )
