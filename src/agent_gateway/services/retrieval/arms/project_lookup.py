"""Project symbol/file lookup exposed as a retrieval arm.

The graph query already returns exact and path matches in priority order; this
adapter maps those rows onto `RetrievedItem` so the shared pipeline owns trace,
error reporting and limit handling. Scores are position-based on purpose:
inventing relevance numbers here would be an uncalibrated threshold.
"""

from __future__ import annotations

from typing import Any

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.retrieval.types import ArmResult, Query, RetrievedItem

logger = setup_logging("server")

LAYER = "project_lookup"


class ProjectLookupArm:
    """One project's call graph; the project path and filters are fixed."""

    name = "project_lookup"

    def __init__(self, project_path: str, kind: str = "", file_path: str = ""):
        self._project_path = project_path
        self._kind = kind
        self._file_path = file_path

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        from agent_gateway.services.mcp.graph_tools import tool_project_lookup

        try:
            result = tool_project_lookup(
                self._project_path, query.text, self._kind, self._file_path, limit
            )
        except Exception as e:  # noqa: BLE001 - one arm must not kill the call
            logger.warning(f"Project lookup failed: {e}")
            return ArmResult(name=self.name, error=f"{type(e).__name__}: {e}")

        if result.get("status") == "error":
            return ArmResult(
                name=self.name, error=str(result.get("error") or "lookup failed")
            )

        items: list[RetrievedItem] = []
        for rank, row in enumerate(result.get("results") or []):
            file = str(row.get("file") or "")
            line = int(row.get("startLine") or 0)
            name = str(row.get("name") or "")
            items.append(
                RetrievedItem(
                    id=f"{LAYER}:{file}:{row.get('kind', '')}:{name}:{line}",
                    kind=str(row.get("kind") or "symbol"),
                    score=1.0 / (rank + 1),
                    score_kind="rank",
                    content=str(row.get("signature") or name),
                    layer=LAYER,
                    locator={
                        "file": file,
                        "line": line,
                        "end_line": int(row.get("endLine") or 0),
                    },
                    extra={
                        "name": name,
                        "caller_count": int(row.get("callerCount") or 0),
                        "top_callers": row.get("topCallers") or [],
                        "resolver": str(row.get("resolver") or ""),
                    },
                )
            )
        return ArmResult(name=self.name, items=items)


__all__ = ["LAYER", "ProjectLookupArm"]
