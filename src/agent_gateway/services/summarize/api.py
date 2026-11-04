"""External interface for the memory-summary pipeline."""

from __future__ import annotations

from agent_gateway.services.summarize.summarize import main


def run() -> int:
    """Run one summary pass; the module entry prints the same report."""
    return main()


__all__ = ["run"]
