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
STATUS_DELETED = "deleted"
STATUS_HIDDEN = (STATUS_SUPERSEDED, STATUS_DELETED)

SUPERSEDED_BY_FIELD = "superseded_by"
SUPERSEDED_AT_FIELD = "superseded_at"
SUPERSES_FIELD = "supersedes"
REVISION_FIELD = "revision"
DELETED_AT_FIELD = "deleted_at"

# Cold summaries derived from hot memories; superseding a source marks them stale.
COLD_SOURCE_MEMORY_IDS_FIELD = "source_memory_ids"
COLD_STALE_FIELD = "stale"
