"""Backfill synapses by reviewing neuron groups in anchor batches."""

import argparse
import asyncio
import json
import time

from agent_gateway.services.memory import network_build, store

BATCH_ANCHORS = 200


def anchor_ids(
    db_path: str, low: int = 2, high: int = 8, all_groups: bool = False
) -> list[str]:
    with store.connect(db_path) as connection:
        if all_groups:
            rows = connection.execute(
                "SELECT neuron_id FROM neuron_members GROUP BY neuron_id "
                "ORDER BY neuron_id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT neuron_id, COUNT(*) AS n FROM neuron_members "
                "GROUP BY neuron_id HAVING n >= ? AND n <= ? ORDER BY neuron_id",
                (low, high),
            ).fetchall()
    return [str(row[0]) for row in rows]


async def run(args) -> dict:
    from agent_gateway.services.mcp import server_memory

    if not await server_memory.ensure_hot_memory():
        print(
            json.dumps(
                {"warning": "hot memory unavailable; falling back to stored centroids"}
            )
        )
    anchors = anchor_ids(args.db, all_groups=args.pair_mode == "v3")
    batches = [
        anchors[start : start + args.batch_anchors]
        for start in range(0, len(anchors), args.batch_anchors)
    ]
    if args.max_batches:
        batches = batches[: args.max_batches]
    report = []
    for index, batch in enumerate(batches, 1):
        started = time.perf_counter()
        built = await network_build.build_synapse_candidates(
            db_path=args.db,
            seed_neurons=batch,
            pair_limit=args.pair_limit,
            llm_limit=args.llm_pairs,
            concurrency=args.concurrency,
            pair_mode=args.pair_mode,
            sparse_floor=args.sparse_floor,
        )
        written = 0
        if not args.dry_run:
            written = network_build.write_synapses(built["decisions"], db_path=args.db)
        item = {
            "batch": index,
            "anchors": len(batch),
            "pairs": len(built["pairs"]),
            "decisions": len(built["decisions"]),
            "written": written,
            "calls": built["calls"],
            "parse_failed": built["parse_failed"],
            "failed_pairs": built.get("failed_pairs", 0),
            "llm_skipped": built.get("llm_skipped", False),
            "vector_unavailable": built.get("vector_unavailable", False),
            "vector_missing": built.get("vector_missing", 0),
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
        report.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    bridges = 0
    if not args.dry_run and args.pair_mode == "v3":
        proposed = network_build.bridge_components(args.db)
        bridges = network_build.write_bridges(proposed, db_path=args.db)
    return {
        "anchors": len(anchors),
        "batches": len(batches),
        "pairs": sum(item["pairs"] for item in report),
        "written": sum(item["written"] for item in report),
        "calls": sum(item["calls"] for item in report),
        "bridges": bridges,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--batch-anchors", type=int, default=BATCH_ANCHORS)
    parser.add_argument("--pair-limit", type=int, default=network_build.PAIR_LIMIT)
    parser.add_argument("--llm-pairs", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--pair-mode", default="v2", choices=("v2", "v3"))
    parser.add_argument("--sparse-floor", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
