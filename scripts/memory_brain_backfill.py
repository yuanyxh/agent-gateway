#!/usr/bin/env python3
"""Backfill v3 derived data: sparse engram codes and entity mentions."""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime

from agent_gateway.services.memory import entities, network, sparse, store


def _active_without_code(connection, *, force: bool = False) -> list[tuple]:
    sql = (
        "SELECT neuron_id, centroid, episode_id, created_at FROM neurons "
        "WHERE status='active'"
    )
    if not force:
        sql += " AND (sparse_code IS NULL OR LENGTH(sparse_code)=0)"
    sql += " ORDER BY neuron_id"
    return connection.execute(sql).fetchall()


def backfill_sparse(
    db_path: str,
    *,
    batch: int = 2000,
    sample: int = 0,
    seed: int = 0,
    force: bool = False,
) -> dict:
    chosen: list[tuple] = []
    with store.connect(db_path) as connection:
        rows = _active_without_code(connection, force=force)
        if sample and len(rows) > sample:
            rows = random.Random(seed).sample(rows, sample)
        for row in rows:
            neuron_id, centroid, episode_id, created_at = row
            if not centroid:
                chosen.append((None, "", neuron_id))
                continue
            key = sparse.context_key(str(episode_id or ""), "", str(created_at or ""))
            code = sparse.encode(network.decode_centroid(centroid), key)
            chosen.append((code, key, neuron_id))
        written = 0
        missing = 0
        now = datetime.now().isoformat()
        for start in range(0, len(chosen), batch):
            chunk = chosen[start : start + batch]
            updates = [
                (code, key, now, neuron_id)
                for code, key, neuron_id in chunk
                if code is not None
            ]
            missing += sum(1 for code, _, _ in chunk if code is None)
            if updates:
                connection.executemany(
                    "UPDATE neurons SET sparse_code=?, context_key=?, updated_at=? "
                    "WHERE neuron_id=?",
                    updates,
                )
                written += len(updates)
            print(
                json.dumps({"batch": start // batch + 1, "written": written}),
                flush=True,
            )
    return {"written": written, "missing": missing, "total": len(chosen)}


async def backfill_entities(
    db_path: str,
    *,
    batch: int = 10,
    sample: int = 0,
    seed: int = 20261111,
    concurrency: int = 1,
) -> dict:
    from agent_gateway.services.mcp import server_memory

    rows: list[dict] = []
    await server_memory.ensure_hot_memory()
    user_id = str(server_memory.DEFAULT_USER_ID or "").strip()
    if server_memory.get_hot_memory() is not None and user_id:
        try:
            data = server_memory.get_hot_memory().get_all(
                filters={"user_id": user_id}, top_k=100_000
            )
        except Exception:
            data = []
        if isinstance(data, dict):
            data = data.get("results", [])
        rows = [
            {"id": str(item.get("id") or ""), "text": str(item.get("memory") or "")}
            for item in data
            if item.get("id") and item.get("memory")
        ]
        with store.connect(db_path) as connection:
            active = {
                str(row[0])
                for row in connection.execute(
                    "SELECT node_id FROM memory_nodes WHERE status='active'"
                )
            }
        rows = [row for row in rows if row["id"] in active]
    if not rows:
        with store.connect(db_path) as connection:
            rows = [
                {"id": str(row[0]), "text": str(row[1] or "")}
                for row in connection.execute(
                    "SELECT node_id, json_extract(metadata, '$.fact') "
                    "FROM memory_nodes WHERE status='active' "
                    "AND json_valid(metadata)"
                )
                if str(row[1] or "").strip()
            ]
    if sample and len(rows) > sample:
        rows = random.Random(seed).sample(rows, sample)
    import asyncio

    totals = {"parsed": 0, "failed": 0, "written": 0}
    chunks = [rows[start : start + batch] for start in range(0, len(rows), batch)]
    semaphore = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()

    async def run_chunk(index: int, chunk: list[dict]) -> None:
        async with semaphore:
            result = await entities.extract_batch(chunk)
            missing = [item for item in chunk if item["id"] not in result]
            if missing:
                result.update(await entities.extract_batch(missing))
                missing = [item for item in chunk if item["id"] not in result]
            written = 0
            if result:
                written = await asyncio.to_thread(
                    entities.store_batch, result, db_path=db_path
                )
                await asyncio.to_thread(entities.store_facts, result, db_path=db_path)
        async with lock:
            if result:
                totals["parsed"] += len(result)
                totals["written"] += written
            totals["failed"] += len(missing)
            print(
                json.dumps({"batch": index, **totals}),
                flush=True,
            )

    await asyncio.gather(
        *(run_chunk(index, chunk) for index, chunk in enumerate(chunks, 1))
    )
    return {
        "batches": len(chunks),
        "parsed": totals["parsed"],
        "failed": totals["failed"],
        "entities_written": totals["written"],
    }


def main() -> int:
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--sparse", action="store_true")
    parser.add_argument("--entities", action="store_true")
    parser.add_argument("--batch", type=int, default=0)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.sparse and not args.entities:
        raise SystemExit("nothing to do: pass --sparse or --entities")
    summaries = {}
    if args.sparse:
        summaries["sparse"] = backfill_sparse(
            args.db,
            batch=args.batch or 2000,
            sample=args.sample,
            seed=args.seed,
            force=args.force,
        )
    if args.entities:
        summaries["entities"] = asyncio.run(
            backfill_entities(
                args.db,
                batch=args.batch or 10,
                sample=args.sample,
                seed=args.seed,
                concurrency=args.concurrency,
            )
        )
    print(json.dumps({"summary": summaries}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
