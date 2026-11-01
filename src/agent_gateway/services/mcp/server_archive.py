"""Cold/hot layer archival: retire old hot memories, archive stale cold points, decay heat.

The entry module injects its current `hot_memory` / `qdrant` on every round via
`get_state`, because the hot layer is initialized lazily and may be missing when
the scheduler starts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.mcp.server_constants import (
    ARCHIVED_AT_FIELD,
    ARCHIVE_DAYS,
    ARCHIVE_INTERVAL_HOURS,
    COLD_COLLECTION,
    COLD_STALE_FIELD,
    DEFAULT_HEAT_SCORE,
    DEFAULT_USER_ID,
    HEAT_DECAY_PER_DAY,
    PROCESSED_FIELD,
    PROCESSED_RETIRED_MASK,
    STATUS_ARCHIVED,
    STATUS_FIELD,
)
from agent_gateway.services.memory import link as memory_link
from agent_gateway.services.memory import retention

logger = setup_logging("server")


def run_archive_sync_impl(hot_memory, qdrant, now=None) -> tuple[int, int, int]:
    now = now or datetime.now()
    cold_cutoff = (now - timedelta(days=ARCHIVE_DAYS)).isoformat()
    archived_hot, archived_count, decayed_count = 0, 0, 0
    archived_ids: list[str] = []
    if hot_memory:
        try:
            vs = hot_memory.vector_store
            qfilter = vs._create_filter({"user_id": DEFAULT_USER_ID})
            scroll_result = vs.client.scroll(
                collection_name=vs.collection_name,
                scroll_filter=qfilter,
                limit=1000,
                with_payload=True,
                with_vectors=False,
            )
            points, _ = (
                scroll_result
                if isinstance(scroll_result, tuple)
                else (scroll_result, None)
            )
            for point in points:
                payload = point.payload or {}
                if payload.get("archived") or payload.get("status") == STATUS_ARCHIVED:
                    continue
                if payload.get("protected") or (payload.get("metadata") or {}).get(
                    "protected"
                ):
                    continue
                # Controlled forgetting archives; it never deletes a body. The window
                # scales with the memory's own quality score.
                if not retention.is_expired(payload, now):
                    continue
                meta = dict(payload.get("metadata") or {})
                flags = int(
                    payload.get(PROCESSED_FIELD) or meta.get(PROCESSED_FIELD) or 0
                )
                # Recall reads lifecycle state from metadata; the top-level fields
                # stay for vector-store filters, so both scopes are written.
                meta[STATUS_FIELD] = STATUS_ARCHIVED
                meta["archived"] = True
                meta[ARCHIVED_AT_FIELD] = now.isoformat()
                meta[PROCESSED_FIELD] = flags | PROCESSED_RETIRED_MASK
                try:
                    vs.client.set_payload(
                        collection_name=vs.collection_name,
                        payload={
                            "status": STATUS_ARCHIVED,
                            "archived": True,
                            "archived_at": now.isoformat(),
                            PROCESSED_FIELD: flags | PROCESSED_RETIRED_MASK,
                            "metadata": meta,
                        },
                        points=[point.id],
                    )
                    archived_hot += 1
                    archived_ids.append(str(point.id))
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Hot cleanup failed: {e}")
        if archived_ids:
            memory_link.record_hot_status(archived_ids, STATUS_ARCHIVED)
    if qdrant:
        try:
            scroll_limit, scroll_offset, cold_processed = 1000, None, 0
            while True:
                batch, scroll_offset = qdrant.scroll(
                    collection_name=COLD_COLLECTION,
                    with_payload=True,
                    with_vectors=False,
                    limit=scroll_limit,
                    offset=scroll_offset,
                )
                if not batch:
                    break
                for point in batch:
                    cold_processed += 1
                    payload = point.payload or {}
                    last_access = payload.get("last_accessed_at", "")
                    heat_score = payload.get("heat_score", DEFAULT_HEAT_SCORE)
                    stale_and_old = payload.get(COLD_STALE_FIELD) and (
                        not last_access or last_access < cold_cutoff
                    )
                    if not payload.get("archived") and (
                        (last_access and last_access < cold_cutoff) or stale_and_old
                    ):
                        try:
                            qdrant.set_payload(
                                collection_name=COLD_COLLECTION,
                                payload={"archived": True},
                                points=[point.id],
                            )
                            archived_count += 1
                        except Exception:
                            pass
                    if last_access and not payload.get("archived"):
                        try:
                            last_dt = datetime.fromisoformat(last_access)
                            days_since = (now - last_dt).days
                            if days_since > 0:
                                new_heat = max(
                                    0.1, heat_score - HEAT_DECAY_PER_DAY * days_since
                                )
                                if abs(new_heat - heat_score) > 0.001:
                                    qdrant.set_payload(
                                        collection_name=COLD_COLLECTION,
                                        payload={"heat_score": new_heat},
                                        points=[point.id],
                                    )
                                    decayed_count += 1
                        except Exception:
                            pass
                if scroll_offset is None:
                    break
        except Exception as e:
            logger.warning(f"Cold archive check failed: {e}")
    return archived_hot, archived_count, decayed_count


async def run_archive_sync(
    hot_memory, qdrant, ensure_hot_memory, now=None
) -> tuple[int, int, int]:
    # Same reason as recall: `run_archive_sync_impl` only checks `if hot_memory` and does
    # not initialize it. If there is no hot-layer call for more than one archive period
    # (24h), archiving spins idly and only reports hot_archived=0. It must be invoked on the
    # event loop (impl runs in the executor), hence it is placed here.
    await ensure_hot_memory()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, run_archive_sync_impl, hot_memory, qdrant, now
    )


async def archive_scheduler(get_state, ensure_hot_memory):
    await asyncio.sleep(300)
    while True:
        hot_memory, qdrant = get_state()
        logger.info("=== Archive check started ===")
        try:
            archived_hot, archived_count, decayed_count = await asyncio.wait_for(
                run_archive_sync(hot_memory, qdrant, ensure_hot_memory), timeout=300
            )
            logger.info(
                f"=== Archive check finished: hot_archived={archived_hot}, "
                f"cold_archived={archived_count}, cold_decayed={decayed_count} ==="
            )
        except asyncio.TimeoutError:
            logger.warning("Archive check timed out")
        except Exception as e:
            logger.warning(f"Archive check failed: {e}")
        await asyncio.sleep(ARCHIVE_INTERVAL_HOURS * 3600)
