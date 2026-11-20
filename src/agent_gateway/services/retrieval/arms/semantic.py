"""Semantic arm: entity mentions become direct memory hits."""

from __future__ import annotations

import re
import time

from agent_gateway.core import config
from agent_gateway.services.memory import store
from agent_gateway.services.retrieval.types import ArmResult, Query, RetrievedItem

#: ASCII entity names must stand alone: a bare substring test lets short names
#: like `en` match `nonexistent`. CJK has no word boundaries, so it stays on
#: substring matching.
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def entity_hit(name: str, text: str) -> bool:
    """Whether `name` occurs in `text` as an entity, not as a fragment."""
    if not name or not name.strip():
        return False
    if name.isascii():
        if not _ASCII_WORD.search(name):
            return False
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return name in text


class SemanticArm:
    name = "semantic"
    fallback_only = False

    def __init__(self, *, db_path: str | None = None):
        self.db_path = db_path

    def retrieve(
        self, query: Query, limit: int, filters: dict | None = None
    ) -> ArmResult:
        started = time.perf_counter()
        if not config.memory_brain_semantic():
            return ArmResult(name=self.name, items=[], error="disabled")
        text = str(query.text or "")
        if not text:
            return ArmResult(name=self.name, items=[])
        with store.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT e.name, m.memory_id, m.confidence FROM entities e "
                "JOIN entity_mentions m ON m.entity_id = e.entity_id "
                "WHERE instr(?, e.name) > 0 LIMIT ?",
                (text, max(limit * 5, 20)),
            ).fetchall()
            ids = sorted({str(row[1]) for row in rows})
            nodes: dict[str, tuple] = {}
            if ids:
                marks = ",".join("?" * len(ids))
                for row in connection.execute(
                    f"SELECT node_id, title, metadata FROM memory_nodes "
                    f"WHERE node_id IN ({marks}) AND status='active'",
                    tuple(ids),
                ):
                    nodes[str(row[0])] = (row[1], row[2])
        items: list[RetrievedItem] = []
        seen: set[str] = set()
        stats = {"raw": len(rows), "fragment_rejected": 0}
        for name, memory_id, confidence in rows:
            memory_id = str(memory_id)
            if not entity_hit(str(name), text):
                stats["fragment_rejected"] += 1
                continue
            if memory_id in seen or memory_id not in nodes:
                continue
            seen.add(memory_id)
            title, _ = nodes[memory_id]
            items.append(
                RetrievedItem(
                    id=memory_id,
                    kind="memory",
                    score=round(0.8 * float(confidence or 1.0), 4),
                    score_kind="dense",
                    content=str(title or ""),
                    layer="hot",
                    match_kind="semantic_fact",
                    extra={"entity": str(name), "evidence": [memory_id]},
                )
            )
        items.sort(key=lambda item: -item.score)
        return ArmResult(
            name=self.name,
            items=items[:limit],
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            stats={**stats, "kept": len(items[:limit])},
        )
