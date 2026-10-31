"""Project symbol search exposed as a retrieval arm.

`_project_search_impl` already chains FTS5, file, package and vector layers and
decides exact/ambiguous itself; this adapter maps its rows onto `RetrievedItem`
so the shared pipeline records one trace per call.
"""

from __future__ import annotations

from typing import Any

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.retrieval.types import ArmResult, Query, RetrievedItem

logger = setup_logging("server")

LAYER = "project_search"


class ProjectSearchArm:
    """One project's symbol index; the db and its scope are fixed per instance."""

    name = "project_search"

    def __init__(self, db_path: str, file_path: str = "", kind: str = ""):
        self._db_path = db_path
        self._file_path = file_path
        self._kind = kind
        #: Raw payload from the last call; the handler returns it unchanged so
        #: the existing exact/ambiguous statuses survive.
        self.payload: dict[str, Any] = {}

    def retrieve(
        self, query: Query, limit: int, filters: dict[str, Any] | None = None
    ) -> ArmResult:
        from agent_gateway.services.mcp.project_tools import _project_search_impl

        try:
            result = _project_search_impl(
                self._db_path, query.text, self._file_path, self._kind, limit
            )
        except Exception as e:  # noqa: BLE001 - one arm must not kill the call
            logger.warning(f"Project search failed: {e}")
            return ArmResult(name=self.name, error=f"{type(e).__name__}: {e}")

        self.payload = result
        items: list[RetrievedItem] = []
        for row in result.get("results") or []:
            name = str(row.get("name") or "")
            file = str(row.get("file") or "")
            items.append(
                RetrievedItem(
                    id=f"project_search:{file}:{name}",
                    kind=str(row.get("kind") or "symbol"),
                    score=1.0,
                    score_kind="lexical",
                    content=str(row.get("signature") or name),
                    layer=LAYER,
                    locator={"file": file, "line": row.get("line", 0)},
                    extra={
                        "source_file": file,
                        "source_layer": str(row.get("layer") or ""),
                    },
                )
            )
        return ArmResult(name=self.name, items=items)


__all__ = ["LAYER", "ProjectSearchArm"]
