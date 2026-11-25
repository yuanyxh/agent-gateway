"""Process-wide mesh router; tools read it after `serve()` builds it."""

from __future__ import annotations

from agent_gateway.services.mesh.router import Router

_router: Router | None = None


def set_router(router: Router | None) -> None:
    global _router
    _router = router


def get_router() -> Router | None:
    return _router
