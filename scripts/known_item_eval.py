#!/usr/bin/env python3
"""Known-item retrieval gate: hand the system a memory's own opening words.

Zero labelling. A miss means the system cannot return a record when given its
own text, which is a defect by construction rather than a relevance opinion.
Run after any change to retrieval code or thresholds:

    PYTHONPATH=src python scripts/known_item_eval.py
    PYTHONPATH=src python scripts/known_item_eval.py --update-baseline

Exit codes: 0 pass, 1 regression beyond ``--max-drop``, 2 run failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Samples are verbatim memory text, so they live in the data dir, never in the
#: repo: committing them would publish real names and business details.
DATA_DIR = Path.home() / ".config" / "agent-gateway"
ITEMS = DATA_DIR / "known_items.jsonl"
BASELINE = DATA_DIR / "known_item_baseline.json"
METRICS = ("hit@1", "hit@3", "hit@5", "hit@10", "mrr")


def post(url: str, body: dict, session: str = "") -> tuple[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.headers.get("Mcp-Session-Id", session), response.read().decode()


def open_session(url: str) -> str:
    session, _ = post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "known-item-eval", "version": "1"},
            },
        },
    )
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


def recall(url: str, session: str, query: str, limit: int) -> list[str]:
    _, out = post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "recall",
                "arguments": {"keywords": [query], "limit": limit},
            },
        },
        session,
    )
    payload = json.loads(out)["result"]["content"][0]["text"]
    data = json.loads(payload)
    return [item["id"] for item in data.get("results") or []]


def score(ranks: list[int | None]) -> dict[str, float]:
    if not ranks:
        return {key: 0.0 for key in METRICS}
    total = len(ranks)
    return {
        "hit@1": sum(1 for r in ranks if r and r <= 1) / total,
        "hit@3": sum(1 for r in ranks if r and r <= 3) / total,
        "hit@5": sum(1 for r in ranks if r and r <= 5) / total,
        "hit@10": sum(1 for r in ranks if r and r <= 10) / total,
        "mrr": sum(1 / r for r in ranks if r) / total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:9001/mcp")
    parser.add_argument("--items", type=Path, default=ITEMS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--max-drop", type=float, default=0.05)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--min-samples", type=int, default=50)
    args = parser.parse_args()

    if not args.items.is_file():
        print(f"no sample set at {args.items}", file=sys.stderr)
        return 2
    items = [
        json.loads(line)
        for line in args.items.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    try:
        session = open_session(args.url)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"cannot reach {args.url}: {e}", file=sys.stderr)
        return 2

    ranks: list[int | None] = []
    missing = 0
    for item in items:
        try:
            ids = recall(args.url, session, item["query"], args.limit)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            print(f"recall failed for {item['id'][:8]}: {e}", file=sys.stderr)
            return 2
        if item["id"] in ids:
            ranks.append(ids.index(item["id"]) + 1)
        else:
            ranks.append(None)
            missing += 1

    if len(ranks) < args.min_samples:
        print(f"only {len(ranks)} samples, need {args.min_samples}", file=sys.stderr)
        return 2

    current = score(ranks)
    print(f"samples={len(ranks)} misses={missing} (remembered memory may have changed)")
    for key in METRICS:
        print(f"  {key:8s} {current[key]:.3f}")

    if args.update_baseline:
        args.baseline.write_text(
            json.dumps({"samples": len(ranks), **current}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"baseline written to {args.baseline}")
        return 0

    if not args.baseline.is_file():
        print(
            f"no baseline at {args.baseline}; run with --update-baseline",
            file=sys.stderr,
        )
        return 2

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    regressions = [
        (key, baseline[key], current[key])
        for key in METRICS
        if key in baseline and current[key] < float(baseline[key]) - args.max_drop
    ]
    for key, before, now in regressions:
        print(f"REGRESSED {key}: {before:.3f} -> {now:.3f}", file=sys.stderr)
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
