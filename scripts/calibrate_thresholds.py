#!/usr/bin/env python3
"""Calibrate relevance floors for the active embedding model.

Score distributions shift when the embedder changes, so a floor measured on one
model is not transferable. Set the semantic floors to 0 first (so recall returns
unfiltered candidates), then measure and write:

    POST /api/retrieval/rebuild                     # rebuild indexes
    set hot / cold / expanded / notes_semantic to 0 # in the override file
    python scripts/calibrate_thresholds.py --write

`--write` targets `gateway_home()/retrieval_thresholds.json`, so run it with the
same GATEWAY_HOME as the server (usually `~/.config/utility`) — otherwise it
writes a file the server never reads. Confirm with the `floors_path` field of
`POST /api/retrieval/diagnose`. No restart is needed: `floors_for()` re-reads the
file on every call.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from itertools import count
from pathlib import Path

DEFAULT_ENDPOINT = "http://127.0.0.1:9001/mcp"
DEFAULT_GOLD = "tests/eval/datasets/queries_gold.jsonl"
COUNTER = count(660000)
LOW, HIGH, STEP = 0.10, 0.90, 0.02


def call_tool(endpoint: str, query: str, limit: int) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": next(COUNTER),
        "method": "tools/call",
        "params": {"name": "recall", "arguments": {"query": query, "limit": limit}},
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
    text = ((payload.get("result") or {}).get("content") or [{}])[0].get("text", "")
    return json.loads(text)


def collect(
    gold: list[dict], endpoint: str, limit: int
) -> dict[str, list[tuple[float, bool]]]:
    """layer -> [(score, is_relevant)] using the probes' own labels."""
    samples: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for entry in gold:
        data = call_tool(endpoint, entry["query"], limit)
        good = {
            item.get("id")
            for key in ("relevant", "partial")
            for item in entry.get(key) or []
        }
        for item in data.get("results") or []:
            layer = str(item.get("layer") or "")
            if layer not in ("hot", "cold"):
                continue
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            samples[layer].append((score, item.get("id") in good))
    return samples


def sweep(pairs: list[tuple[float, bool]]) -> list[dict]:
    """Precision/recall/F1 per threshold over the given (score, relevant) pairs."""
    positives = sum(1 for _, good in pairs if good)
    rows = []
    threshold = LOW
    while threshold <= HIGH + 1e-9:
        kept = [p for p in pairs if p[0] >= threshold]
        hits = sum(1 for p in kept if p[1])
        precision = hits / len(kept) if kept else 0.0
        recall = hits / positives if positives else 0.0
        f1 = (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
        rows.append(
            {
                "threshold": round(threshold, 2),
                "kept": len(kept),
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(f1, 3),
            }
        )
        threshold += STEP
    return rows


def best(rows: list[dict]) -> dict:
    """Highest F1, preferring the lower threshold on ties (keeps more recall)."""
    return max(rows, key=lambda r: (r["f1"], -r["threshold"]))


def write_floors(tag: str, chosen: dict[str, float]) -> Path | None:
    """Merge measured floors into the override file; None when a floor is unknown.

    Unmeasured keys inherit the built-in table for `tag`, so a fresh data dir
    (no override file yet) can still be calibrated.
    """
    from agent_gateway.services.retrieval.gate.thresholds import (
        FLOOR_KEYS,
        FLOORS,
        override_path,
    )

    path = override_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    entry = {**(FLOORS.get(tag) or {}), **(data.get(tag) or {})}
    entry.update(
        {
            "cold": chosen.get("cold", entry.get("cold")),
            "hot": chosen.get("hot", entry.get("hot")),
            "expanded": chosen.get("cold", entry.get("expanded")),
        }
    )
    for key in FLOOR_KEYS:
        entry.setdefault(key, None)
    if any(entry.get(key) is None for key in FLOOR_KEYS):
        return None
    data[tag] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=DEFAULT_GOLD)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--tag", default="")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    gold = [
        json.loads(line) for line in open(args.gold, encoding="utf-8") if line.strip()
    ]
    if not gold:
        print("empty gold set")
        return 1

    samples = collect(gold, args.endpoint, args.limit)
    report: dict = {"tag": args.tag, "layers": {}}
    chosen: dict[str, float] = {}
    for layer, pairs in sorted(samples.items()):
        rows = sweep(pairs)
        pick = best(rows)
        report["layers"][layer] = {
            "samples": len(pairs),
            "positives": sum(1 for _, good in pairs if good),
            "best": pick,
            "curve": rows,
        }
        chosen[layer] = pick["threshold"]
        print(
            f"{layer}: n={len(pairs)} pos={report['layers'][layer]['positives']} "
            f"-> threshold {pick['threshold']} (p={pick['precision']} r={pick['recall']} f1={pick['f1']})"
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"wrote {args.out}")

    if args.write and args.tag and chosen:
        written = write_floors(args.tag, chosen)
        if written is None:
            print("refusing to write: some floors still unknown", file=sys.stderr)
            return 1
        print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
