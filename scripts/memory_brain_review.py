#!/usr/bin/env python3
"""Review inferred synapses: list, accept, reject or expire them."""

from __future__ import annotations

import argparse
import json

from agent_gateway.services.memory import replay, store


def pending(db_path: str) -> list[dict]:
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT synapse_id, from_neuron, to_neuron, weight, created_at, metadata "
            "FROM synapses WHERE inferred=1 AND status='active' ORDER BY synapse_id"
        ).fetchall()
    return [
        {
            "synapse_id": row[0],
            "from": row[1],
            "to": row[2],
            "weight": row[3],
            "created_at": row[4],
            "metadata": row[5],
        }
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--accept", type=int, default=0)
    parser.add_argument("--reject", type=int, default=0)
    parser.add_argument("--expire-days", type=int, default=0)
    args = parser.parse_args()
    if args.expire_days:
        expired = replay.expire_inferences(days=args.expire_days, db_path=args.db)
        print(json.dumps({"expired": expired}, ensure_ascii=False))
        return 0
    if args.accept:
        ok = replay.decide_inference(args.accept, accept=True, db_path=args.db)
        print(json.dumps({"accepted": args.accept, "ok": ok}))
        return 0 if ok else 1
    if args.reject:
        ok = replay.decide_inference(args.reject, accept=False, db_path=args.db)
        print(json.dumps({"rejected": args.reject, "ok": ok}))
        return 0 if ok else 1
    print(json.dumps(pending(args.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
