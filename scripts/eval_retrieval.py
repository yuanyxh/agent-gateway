#!/usr/bin/env python3
"""Score a retrieval run against the gold dataset.

Recall@5 / MRR@10 / nDCG@10 are reported on `specific` queries only: a
single-topic query ("agent-vibe") cannot define relevance, so it would measure
query quality rather than retrieval quality. `vague` and `synthetic` buckets are
reported separately.

`missing` counts gold items absent from the top-k, which is how stale gold
entries surface after the index changes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from collections import Counter
from itertools import count

DEFAULT_ENDPOINT = "http://127.0.0.1:9001/mcp"
COUNTER = count(770000)
GAIN = {"relevant": 2.0, "partial": 1.0}


def call_tool(endpoint: str, tool: str, query: str, limit: int) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": next(COUNTER),
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"query": query, "limit": limit}},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode())
    result = payload.get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    if result.get("isError"):
        raise RuntimeError(text[:200])
    return json.loads(text)


def load_gold(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def dcg(gains: list[float]) -> float:
    return sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))


def score_entry(entry: dict, data: dict, limit: int) -> dict:
    """Recall@5 / MRR / nDCG for one query, using the run's own ordering."""
    hits = data.get("results") or []
    positions = {item.get("id"): index for index, item in enumerate(hits, 1)}
    grades: dict[str, float] = {}
    for label in ("relevant", "partial"):
        for gold_item in entry.get(label) or []:
            item_id = gold_item.get("id")
            grades[item_id] = max(grades.get(item_id, 0.0), GAIN[label])

    gains = []
    for rank in range(1, limit + 1):
        hit_id = hits[rank - 1].get("id") if rank - 1 < len(hits) else None
        gains.append(grades.get(hit_id, 0.0))
    first = next((rank for rank, gain in enumerate(gains, 1) if gain), None)
    ideal = sorted(
        [GAIN["relevant"]] * len(entry.get("relevant") or [])
        + [GAIN["partial"]] * len(entry.get("partial") or []),
        reverse=True,
    )
    denominator = dcg(ideal[:limit]) or 1.0
    missing = [item_id for item_id in grades if item_id not in positions]
    # Precision matters as much as recall here: five hits where one is relevant
    # still forces the caller to read four useless results.
    returned = len(hits[:5]) or 1
    weighted = sum(g for g in gains[:5]) / GAIN["relevant"]
    return {
        "status": data.get("status"),
        "recall_at_5": bool(any(gains[:5])),
        "precision_at_5": weighted / returned,
        "relevant_at_5": sum(1 for g in gains[:5] if g == GAIN["relevant"]),
        "top1_relevant": bool(gains and gains[0] == GAIN["relevant"]),
        "first_relevant": first,
        "mrr": (1.0 / first) if first else 0.0,
        "ndcg": dcg(gains) / denominator,
        "missing_gold_ids": len(missing),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default="tests/eval/datasets/queries_gold.jsonl")
    parser.add_argument("--tool", default="recall")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    gold = load_gold(args.gold)
    if not gold:
        print("empty gold set")
        return 1

    buckets: dict[str, dict] = {}
    rows: list[dict] = []
    for entry in gold:
        label = entry.get("clarity", "specific")
        bucket = buckets.setdefault(
            label,
            {
                "n": 0,
                "recall5": 0.0,
                "mrr": 0.0,
                "ndcg": 0.0,
                "stale": 0,
                "precision": 0.0,
                "top1": 0.0,
                "relevant": 0.0,
                "status": Counter(),
            },
        )
        data = call_tool(args.endpoint, args.tool, entry["query"], args.limit)
        scored = score_entry(entry, data, args.limit)
        bucket["n"] += 1
        bucket["recall5"] += 1.0 if scored["recall_at_5"] else 0.0
        bucket["mrr"] += scored["mrr"]
        bucket["ndcg"] += scored["ndcg"]
        bucket["stale"] += 1 if scored["missing_gold_ids"] else 0
        bucket["precision"] += scored["precision_at_5"]
        bucket["top1"] += 1.0 if scored["top1_relevant"] else 0.0
        bucket["relevant"] += scored["relevant_at_5"]
        bucket["status"][scored["status"]] += 1
        rows.append({"query": entry["query"], "clarity": label, **scored})

    main_bucket = buckets.get("specific", {"n": 0})
    counted = main_bucket["n"]
    summary = {
        "queries": len(gold),
        "counted": counted,
        "recall_at_5": round(main_bucket["recall5"] / counted, 4) if counted else None,
        "mrr_at_10": round(main_bucket["mrr"] / counted, 4) if counted else None,
        "ndcg_at_10": round(main_bucket["ndcg"] / counted, 4) if counted else None,
        "precision_at_5": round(main_bucket["precision"] / counted, 4)
        if counted
        else None,
        "top1_relevant_rate": round(main_bucket["top1"] / counted, 4)
        if counted
        else None,
        "relevant_per_query": round(main_bucket["relevant"] / counted, 2)
        if counted
        else None,
        "stale_queries": main_bucket.get("stale", 0),
        "by_clarity": {
            label: {
                "queries": data["n"],
                "recall_at_5": round(data["recall5"] / data["n"], 4)
                if data["n"]
                else None,
                "status": dict(data["status"]),
            }
            for label, data in buckets.items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"summary": summary, "rows": rows}, fh, ensure_ascii=False, indent=2
            )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
