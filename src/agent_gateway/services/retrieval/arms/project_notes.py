"""Project-note chunks exposed as a retrieval arm.

The chunk cache and hybrid scoring already live in `search_notes_query`; this
adapter only maps its rows onto `RetrievedItem` so the shared pipeline controls
(floors, gate, trace) apply here as well.
"""

from __future__ import annotations

from typing import Any

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.mcp.server_constants import DEFAULT_HEAT_SCORE
from agent_gateway.services.retrieval.types import ArmResult, Query, RetrievedItem

logger = setup_logging("server")

LAYER = "project_notes"


class ProjectNotesArm:
    """One project's note chunks; the project path is fixed per instance."""

    name = "project_notes"

    def __init__(self, project_path: str):
        self._project_path = project_path
        #: Filled by retrieve(); the handler reports it without re-scanning.
        self.total_chunks = 0

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        from agent_gateway.services.mcp.search_notes_query import _query_project_notes

        try:
            result = _query_project_notes(self._project_path, query.text, limit)
        except Exception as e:  # noqa: BLE001 - one arm must not kill the call
            logger.warning(f"Project notes search failed: {e}")
            return ArmResult(name=self.name, error=f"{type(e).__name__}: {e}")

        self.total_chunks = int(result.get("total_chunks") or 0)
        items: list[RetrievedItem] = []
        for row in result.get("results") or []:
            file = str(row.get("file") or "")
            line = int(row.get("line") or 0)
            items.append(
                RetrievedItem(
                    id=f"project_notes:{file}:{line}",
                    kind="note",
                    score=float(row.get("score") or 0.0),
                    score_kind="hybrid",
                    content=str(row.get("content") or ""),
                    layer=LAYER,
                    locator={"file": file, "line": line},
                    extra={"source_file": file, "heat_score": DEFAULT_HEAT_SCORE},
                )
            )
        return ArmResult(name=self.name, items=items, error=result.get("error"))


__all__ = ["LAYER", "ProjectNotesArm"]
