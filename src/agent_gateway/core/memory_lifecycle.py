"""Memory lifecycle constants shared by the MCP server and the summarize script.

Pure constants only: the summarize script runs standalone and must not import
server_constants (which requires QDRANT_PATH at import time).
"""

from __future__ import annotations

# Processed bitmask: bit0 = summarized, bit1 = retired (superseded/deleted).
PROCESSED_FIELD = "processed"
PROCESSED_SUMMARIZED_MASK = 1 << 0
PROCESSED_RETIRED_MASK = 1 << 1
PROCESSED_CONSUMED_MASK = PROCESSED_SUMMARIZED_MASK | PROCESSED_RETIRED_MASK

# Lifecycle status; legacy entries without the field count as active.
STATUS_FIELD = "status"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_ARCHIVED = "archived"
STATUS_DELETED = "deleted"
STATUS_HIDDEN = (STATUS_SUPERSEDED, STATUS_ARCHIVED, STATUS_DELETED)

SUPERSEDED_BY_FIELD = "superseded_by"
SUPERSEDED_AT_FIELD = "superseded_at"
SUPERSES_FIELD = "supersedes"
REVISION_FIELD = "revision"
ARCHIVED_AT_FIELD = "archived_at"
DISPUTED_AT_FIELD = "disputed_at"
DELETED_AT_FIELD = "deleted_at"

# Cold summaries derived from hot memories; superseding a source marks them stale.
COLD_SOURCE_MEMORY_IDS_FIELD = "source_memory_ids"
COLD_STALE_FIELD = "stale"

# Visibility. mem0 owns the agent scope (payload agent_id); this field is only for
# retrieval filtering, because mem0 drops identity keys written through metadata.
VISIBILITY_FIELD = "visibility"
OWNER_AGENT_FIELD = "owner_agent_id"


def is_public(meta: dict | None) -> bool:
    """Only public memories may feed shared summaries and consolidation."""
    return str((meta or {}).get(VISIBILITY_FIELD) or "all") == "all"


def visible_to(meta: dict | None, agent_id: str) -> bool:
    """all: every agent; agent-specific: same owner; user-only: id lookup only."""
    meta = meta or {}
    visibility = str(meta.get(VISIBILITY_FIELD) or "all")
    if visibility == "user-only":
        return False
    if visibility == "agent-specific":
        owner = str(meta.get(OWNER_AGENT_FIELD) or meta.get("agent_id") or "unknown")
        return owner == (agent_id or "unknown")
    return True
