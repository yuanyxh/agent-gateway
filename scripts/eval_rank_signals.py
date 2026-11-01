#!/usr/bin/env python3
"""Compare ranking-signal configurations on one fixed query set.

Run against an isolated server whose RANK_SIGNAL_* variables differ per run;
the production process must not be restarted for this. Reports the same
known-item metrics as `known_item_eval.py` plus the returned character count,
which is the context budget the signals are trading against.

    PYTHONPATH=src python scripts/eval_rank_signals.py --url http://127.0.0.1:51999/mcp \
        --items "$GATEWAY_HOME/known_items.jsonl" --label baseline

recall updates access_count/last_accessed_at, so comparing access or recency
weights needs a fresh store snapshot per run.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

METRICS = ("hit@1", "hit@3", "hit@5", "hit@10", "mrr")


def default_items() -> Path:
    """Derived from GATEWAY_HOME so an isolated snapshot reads its own sample set.

    Imported here, not at module scope, so `--help` works without PYTHONPATH.
    """
    from agent_gateway.core.paths import gateway_home

    return gateway_home() / "known_items.jsonl"


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
                "clientInfo": {"name": "rank-signal-eval", "version": "1"},
            },
        },
    )
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


def recall(url: str, session: str, query: str, limit: int) -> dict:
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
    return json.loads(json.loads(out)["result"]["content"][0]["text"])


def score(ranks: list[int | None]) -> dict[str, float]:
    total = len(ranks)
    if not total:
        return {key: 0.0 for key in METRICS}
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
    parser.add_argument("--items", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    items_path = args.items or default_items()

    if not items_path.is_file():
        print(f"no sample set at {items_path}", file=sys.stderr)
        return 2
    items = [
        json.loads(line)
        for line in items_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # A tiny set produces confident-looking numbers from noise; refuse instead.
    if len(items) < args.min_samples:
        print(
            f"need at least {args.min_samples} samples, got {len(items)}",
            file=sys.stderr,
        )
        return 2
    try:
        session = open_session(args.url)
    except OSError as e:
        print(f"cannot reach {args.url}: {e}", file=sys.stderr)
        return 2

    ranks: list[int | None] = []
    chars = 0
    for item in items:
        data = recall(args.url, session, item["query"], args.limit)
        results = data.get("results") or []
        ids = [row.get("id") for row in results]
        chars += sum(
            len(str(row.get("content") or "")) + len(str(row.get("context") or ""))
            for row in results
        )
        ranks.append(ids.index(item["id"]) + 1 if item["id"] in ids else None)

    metrics = score(ranks)
    report = {
        "label": args.label,
        "samples": len(ranks),
        "misses": sum(1 for r in ranks if r is None),
        "chars_per_query": round(chars / max(len(ranks), 1), 1),
        **{key: round(value, 3) for key, value in metrics.items()},
    }
    print(json.dumps(report, ensure_ascii=False))
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
