"""Request-scoped trace id shared by the proxy and MCP layers."""

from __future__ import annotations

import contextvars
import uuid
from contextvars import Token
from datetime import datetime

_current: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def new_id() -> str:
    return f"rt_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def bind(trace_id: str) -> Token[str]:
    return _current.set(trace_id)


def reset(token: Token[str]) -> None:
    _current.reset(token)


def current() -> str:
    return _current.get()
