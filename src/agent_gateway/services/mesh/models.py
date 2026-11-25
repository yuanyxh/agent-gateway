"""Service cards and messages exchanged through the agent mesh."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "utility.mesh/service-card/v1"
MAX_PAYLOAD_BYTES = 65_536
DEFAULT_TTL_SECONDS = 120

ServiceKind = Literal["workspace", "agent", "gateway", "custom"]
TransportKind = Literal["directory", "http"]
ServiceStatus = Literal["offline", "starting", "ready", "pending", "busy", "blocked"]
SERVICE_STATUSES = frozenset(
    ("offline", "starting", "ready", "pending", "busy", "blocked")
)
MessageKind = Literal["notify", "task", "reply", "control"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MeshErrorCode(StrEnum):
    INVALID_CARD = "invalid_card"
    INVALID_MESSAGE = "invalid_message"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    TARGET_UNKNOWN = "target_unknown"
    TARGET_OFFLINE = "target_offline"
    PEER_UNREACHABLE = "peer_unreachable"
    DELIVERY_FAILED = "delivery_failed"
    REGISTRY_FULL = "registry_full"
    UNAUTHORIZED = "unauthorized"


class ServiceCard(BaseModel):
    """A registered service; a workspace is one card."""

    schema_version: str = SCHEMA_VERSION
    id: str
    name: str
    kind: ServiceKind = "agent"
    owner_gateway: str = ""
    transport: TransportKind = "directory"
    endpoint: str
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: ServiceStatus = "ready"
    start_command: str | None = None
    startable: bool = False
    heartbeat_at: datetime = Field(default_factory=_now)
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    @field_validator("id", "name", "endpoint")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    def is_alive(self, now: datetime | None = None) -> bool:
        moment = now or _now()
        heartbeat = self.heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment - heartbeat).total_seconds() <= self.ttl_seconds


class MeshMessage(BaseModel):
    """One delivery; the payload cap keeps inbox files small."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    sender: str
    recipient: str
    kind: MessageKind = "notify"
    payload: dict[str, Any] = Field(default_factory=dict)
    reply_to: str | None = None
    sent_at: datetime = Field(default_factory=_now)

    @field_validator("payload")
    @classmethod
    def _fit_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        return value
