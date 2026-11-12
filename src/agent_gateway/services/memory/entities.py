"""Entity and fact extraction for the semantic layer."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from agent_gateway.core import prompt_loader
from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import store

BATCH = 10
TIMEOUT = 90.0
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-gateway/memory/entity")


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


def entity_id(name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, normalize(name)))


def parse_batch(raw: str) -> dict[str, dict]:
    parsed = llm_client.parse_json(raw)
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for item in parsed.get("memories") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        entities = [
            entry
            for entry in item.get("entities") or []
            if isinstance(entry, dict) and str(entry.get("name") or "").strip()
        ]
        facts = [
            entry
            for entry in item.get("facts") or []
            if isinstance(entry, dict)
            and str(entry.get("subject") or "").strip()
            and str(entry.get("predicate") or "").strip()
        ]
        out[str(item["id"])] = {"entities": entities, "facts": facts}
    return out


def render_memories(memories: list[dict]) -> str:
    lines = []
    for item in memories:
        body = str(item.get("text") or "").strip()
        if body:
            lines.append(f"[{item['id']}]\n{body}")
    return "\n\n".join(lines)


async def extract_batch(memories: list[dict], *, lang: str = "") -> dict[str, dict]:
    if not memories or not llm_client.is_configured():
        return {}
    prompt = prompt_loader.prompt(
        "entity_extract", memories_text=render_memories(memories)
    )
    raw = await llm_client.achat(
        prompt,
        temperature=0.1,
        timeout=TIMEOUT,
        disable_thinking=True,
        label="Entity extraction failed",
    )
    known = {str(item.get("id")) for item in memories}
    return {
        key: value for key, value in parse_batch(str(raw or "")).items() if key in known
    }


def upsert_entities(
    connection, memory_id: str, parsed: dict, *, now: str | None = None
) -> int:
    now = now or datetime.now().isoformat()
    written = 0
    for entry in parsed.get("entities") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        key = entity_id(name)
        raw_aliases = entry.get("aliases")
        aliases = (
            [str(alias) for alias in raw_aliases if str(alias).strip()]
            if isinstance(raw_aliases, list)
            else []
        )
        connection.execute(
            "INSERT INTO entities (entity_id, name, type, aliases, metadata, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "type=COALESCE(NULLIF(excluded.type, ''), entities.type), "
            "aliases=CASE WHEN excluded.aliases='[]' THEN entities.aliases "
            "ELSE excluded.aliases END, updated_at=excluded.updated_at",
            (
                key,
                name,
                str(entry.get("type") or ""),
                json.dumps(aliases, ensure_ascii=False),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO entity_mentions (entity_id, memory_id, role, "
            "confidence, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, memory_id, "mention", 1.0, now),
        )
        written += 1
    return written


def _evidence_ids(metadata) -> list[str]:
    if not metadata:
        return []
    try:
        loaded = json.loads(metadata)
    except (TypeError, ValueError):
        return []
    items = loaded.get("evidence") if isinstance(loaded, dict) else None
    return [str(item) for item in items] if isinstance(items, list) else []


def _fact_meta(fact: dict, evidence: str, existing=None) -> str:
    meta = {}
    if existing:
        try:
            loaded = json.loads(existing)
        except (TypeError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            meta.update(loaded)
    seen = _evidence_ids(existing) or list(meta.get("evidence") or [])
    if evidence and evidence not in seen:
        seen.append(evidence)
    meta["evidence"] = seen[-50:]
    polarity = str(fact.get("polarity") or "").strip()
    if polarity:
        meta["polarity"] = polarity
    confidence = fact.get("confidence")
    if isinstance(confidence, (int, float)):
        meta["confidence"] = float(confidence)
    return json.dumps(meta, ensure_ascii=False)


def merge_fact(
    connection,
    fact: dict,
    evidence_memory_id: str,
    *,
    now: str | None = None,
) -> str:
    """Insert or reinforce one subject-predicate-object fact; returns the action."""
    now = now or datetime.now().isoformat()
    subject = str(fact.get("subject") or "").strip()
    predicate = str(fact.get("predicate") or "").strip()
    obj = str(fact.get("object") or "").strip()
    if not subject or not predicate or not obj:
        return "skipped"
    left = entity_id(subject)
    right = entity_id(obj)
    evidence = str(evidence_memory_id or "").strip()
    row = connection.execute(
        "SELECT edge_id, valid_to, metadata FROM memory_edges "
        "WHERE from_id=? AND to_id=? AND type=?",
        (left, right, predicate),
    ).fetchone()
    if row is not None and row[1] is None:
        if evidence and evidence in _evidence_ids(row[2]):
            return "duplicate"
        connection.execute(
            "UPDATE memory_edges SET supports=supports+1, metadata=? WHERE edge_id=?",
            (_fact_meta(fact, evidence, row[2]), row[0]),
        )
        return "updated"
    closed = connection.execute(
        "SELECT edge_id FROM memory_edges "
        "WHERE from_id=? AND type=? AND valid_to IS NULL",
        (left, predicate),
    ).fetchall()
    for (edge_id,) in closed:
        connection.execute(
            "UPDATE memory_edges SET valid_to=?, contradicts=contradicts+1 "
            "WHERE edge_id=?",
            (now, edge_id),
        )
    connection.execute(
        "INSERT INTO memory_edges (from_id, to_id, type, weight, created_at, "
        "metadata, valid_from, supports, contradicts) VALUES (?, ?, ?, 1.0, ?, ?, ?, 1, 0) "
        "ON CONFLICT(from_id, to_id, type) DO UPDATE SET "
        "valid_to=NULL, valid_from=excluded.valid_from, metadata=excluded.metadata",
        (left, right, predicate, now, _fact_meta(fact, evidence), now),
    )
    return "inserted"


def current_facts(connection, subject: str) -> list[dict]:
    rows = connection.execute(
        "SELECT to_id, type, supports, contradicts, valid_from FROM memory_edges "
        "WHERE from_id=? AND valid_to IS NULL ORDER BY supports DESC, type",
        (entity_id(subject),),
    ).fetchall()
    return [
        {
            "object": row[0],
            "predicate": row[1],
            "supports": row[2],
            "contradicts": row[3],
            "valid_from": row[4],
        }
        for row in rows
    ]


def store_facts(
    parsed: dict[str, dict], *, db_path: str | None = None, now: str | None = None
) -> dict[str, int]:
    counts = {"inserted": 0, "updated": 0, "skipped": 0}
    with store.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for memory_id, item in parsed.items():
            for fact in item.get("facts") or []:
                action = merge_fact(connection, fact, memory_id, now=now)
                counts[action] = counts.get(action, 0) + 1
        connection.commit()
    return counts


def consolidate_facts(
    connection, *, min_support: int = 2, now: str | None = None
) -> int:
    """Promote supported, non-conflicting facts to full weight."""
    now = now or datetime.now().isoformat()
    rows = connection.execute(
        "SELECT edge_id FROM memory_edges WHERE valid_to IS NULL "
        "AND supports >= ? AND contradicts = 0",
        (max(1, int(min_support)),),
    ).fetchall()
    for (edge_id,) in rows:
        connection.execute(
            "UPDATE memory_edges SET weight=1.0 WHERE edge_id=?", (edge_id,)
        )
    return len(rows)


def store_batch(
    results: dict[str, dict], *, db_path: str | None = None, now: str | None = None
) -> int:
    if not results:
        return 0
    total = 0
    with store.connect(db_path) as connection:
        for memory_id, parsed in results.items():
            total += upsert_entities(connection, memory_id, parsed, now=now)
    return total
