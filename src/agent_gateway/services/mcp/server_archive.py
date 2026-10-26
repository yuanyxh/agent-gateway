"""Cold/hot layer archival: retire old hot memories, archive stale cold points, decay heat.

The entry module injects its current `hot_memory` / `qdrant` on every round via
`get_state`, because the hot layer is initialized lazily and may be missing when
the scheduler starts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from qdrant_client import models

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.mcp.server_constants import (
    ARCHIVE_DAYS,
    ARCHIVE_INTERVAL_HOURS,
    COLD_COLLECTION,
    COLD_STALE_FIELD,
    DEFAULT_HEAT_SCORE,
    DEFAULT_USER_ID,
    HEAT_DECAY_PER_DAY,
    HOT_RETENTION_DAYS,
    STATUS_HIDDEN,
    SUPERSEDED_RETENTION_DAYS,
)

logger = setup_logging("server")


def run_archive_sync_impl(hot_memory, qdrant) -> tuple[int, int, int]:
    cutoff = (datetime.now() - timedelta(days=ARCHIVE_DAYS)).isoformat()
    deleted_hot, archived_count, decayed_count = 0, 0, 0
    if hot_memory:
        try:
            now = datetime.now()
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
                created = payload.get("created_at", "")
                # Retired entries (superseded/deleted) leave sooner than active ones;
                # legacy entries without a status keep the full retention window.
                status = payload.get("status") or (payload.get("metadata") or {}).get(
                    "status"
                )
                retention = (
                    SUPERSEDED_RETENTION_DAYS
                    if status in STATUS_HIDDEN
                    else HOT_RETENTION_DAYS
                )
                hot_cutoff = (now - timedelta(days=retention)).isoformat()
                if created and created < hot_cutoff:
                    try:
                        vs.client.delete(
                            collection_name=vs.collection_name,
                            points_selector=models.PointIdsList(points=[point.id]),
                        )
                        deleted_hot += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Hot cleanup failed: {e}")
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
                        not last_access or last_access < cutoff
                    )
                    if not payload.get("archived") and (
                        (last_access and last_access < cutoff) or stale_and_old
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
                            days_since = (datetime.now() - last_dt).days
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
    return deleted_hot, archived_count, decayed_count


async def run_archive_sync(
    hot_memory, qdrant, ensure_hot_memory
) -> tuple[int, int, int]:
    # Same reason as recall: `run_archive_sync_impl` only checks `if hot_memory` and does
    # not initialize it. If there is no hot-layer call for more than one archive period
    # (24h), archiving spins idly and only reports hot_deleted=0. It must be invoked on the
    # event loop (impl runs in the executor), hence it is placed here.
    await ensure_hot_memory()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_archive_sync_impl, hot_memory, qdrant)


async def archive_scheduler(get_state, ensure_hot_memory):
    await asyncio.sleep(300)
    while True:
        hot_memory, qdrant = get_state()
        logger.info("=== Archive check started ===")
        try:
            deleted_hot, archived_count, decayed_count = await asyncio.wait_for(
                run_archive_sync(hot_memory, qdrant, ensure_hot_memory), timeout=300
            )
            logger.info(
                f"=== Archive check finished: hot_deleted={deleted_hot}, "
                f"cold_archived={archived_count}, cold_decayed={decayed_count} ==="
            )
        except asyncio.TimeoutError:
            logger.warning("Archive check timed out")
        except Exception as e:
            logger.warning(f"Archive check failed: {e}")
        await asyncio.sleep(ARCHIVE_INTERVAL_HOURS * 3600)
