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
        "params": {
            "name": "recall",
            "arguments": {"keywords": [], "query": query, "limit": limit},
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read().decode()
    payload = _jsonrpc_payload(raw)
    text = ((payload.get("result") or {}).get("content") or [{}])[0].get("text", "")
    return json.loads(text)


def _jsonrpc_payload(raw: str) -> dict:
    """The JSON-RPC response, from a plain body or from an SSE event stream."""
    if not raw.lstrip().startswith("event:") and "\ndata:" not in raw:
        return json.loads(raw)
    for event in reversed(raw.split("\n\n")):
        lines = [line[5:] for line in event.splitlines() if line.startswith("data:")]
        if not lines:
            continue
        try:
            candidate = json.loads("\n".join(lines))
        except ValueError:
            continue
        if isinstance(candidate, dict) and (
            "result" in candidate or "error" in candidate
        ):
            return candidate
    raise ValueError(f"No JSON-RPC response in: {raw[:200]!r}")


def _trace_row(trace_id: str, trace_dir: Path) -> dict | None:
    if not trace_id:
        return None
    for path in sorted(trace_dir.glob("*.jsonl"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            if trace_id not in line:
                continue
            row = json.loads(line)
            if row.get("trace_id") == trace_id:
                return row
    return None


def _dense_scores(entry: dict | None) -> dict[str, dict[str, float]]:
    """layer -> id -> best dense score across passes."""
    out: dict[str, dict[str, float]] = {"hot": {}, "cold": {}}
    for step in (entry or {}).get("passes") or []:
        for arm in step.get("arms") or []:
            name = str(arm.get("name") or "")
            if name not in ("dense_hot", "dense_cold"):
                continue
            layer = name.rsplit("_", 1)[1]
            for hit in arm.get("hits") or []:
                if hit.get("score_kind") != "dense":
                    continue
                try:
                    score = float(hit.get("score"))
                except (TypeError, ValueError):
                    continue
                rid = str(hit.get("id"))
                out[layer][rid] = max(out[layer].get(rid, -1.0), score)
    return out


def collect(
    gold: list[dict], endpoint: str, limit: int, trace_dir: Path
) -> dict[str, list[tuple[float, bool]]]:
    """layer -> [(dense score, is_relevant)] from each query's trace row.

    The result list carries fused scores, which are not on the same scale as the
    floors (those gate the dense arm). The trace row preserves the raw dense
    score of every candidate, so the sweep sees what the floor actually rejects.
    """
    samples: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for entry in gold:
        data = call_tool(endpoint, entry["query"], limit)
        good = {
            item.get("id")
            for key in ("relevant", "partial")
            for item in entry.get(key) or []
        }
        scores = _dense_scores(_trace_row(str(data.get("trace_id") or ""), trace_dir))
        for layer, hits in scores.items():
            for rid, score in hits.items():
                samples[layer].append((score, rid in good))
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
    """Write measured floors with the same derivations auto-calibration uses."""
    from agent_gateway.services.retrieval.gate.thresholds import (
        FLOOR_KEYS,
        override_path,
    )

    hot = chosen.get("hot")
    cold = chosen.get("cold")
    if hot is None or cold is None:
        return None
    entry = {
        "hot": float(hot),
        "cold": float(cold),
        "expanded": float(cold),
        "notes_coverage": 0.50,
        "notes_semantic": round(float(cold) * 0.65, 3),
    }
    path = override_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data[tag] = {key: entry[key] for key in FLOOR_KEYS}
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
    parser.add_argument("--trace-dir", default="")
    args = parser.parse_args()

    gold = [
        json.loads(line) for line in open(args.gold, encoding="utf-8") if line.strip()
    ]
    if not gold:
        print("empty gold set")
        return 1

    if args.trace_dir:
        trace_dir = Path(args.trace_dir)
    else:
        from agent_gateway.core.paths import gateway_cache

        trace_dir = gateway_cache() / "retrieval"

    samples = collect(gold, args.endpoint, args.limit, trace_dir)
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
