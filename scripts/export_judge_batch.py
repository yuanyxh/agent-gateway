#!/usr/bin/env python3
"""Render one query batch's live results for blind relevance judging.

Gold sets built from a previous model's own results cannot compare models: the
old model always finds the rows it produced. This exports the current top-k with
no model or gold information, so a judge scores absolute relevance only.

    python scripts/export_judge_batch.py --limit 5 --split 3
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from itertools import count

DEFAULT_ENDPOINT = "http://127.0.0.1:9001/mcp"
SNIPPET = 200
COUNTER = count(990000)


def post(endpoint: str, body: dict, session: str = "") -> tuple[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.headers.get("Mcp-Session-Id", session), response.read().decode()


def open_session(endpoint: str) -> str:
    session, _ = post(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "judge-batch", "version": "1"},
            },
        },
    )
    post(endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


def call_recall(
    endpoint: str, query: str, limit: int, session: str
) -> tuple[str, dict]:
    session, out = post(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": next(COUNTER),
            "method": "tools/call",
            "params": {
                "name": "recall",
                "arguments": {"keywords": [query], "limit": limit, "max_chars": 0},
            },
        },
        session,
    )
    payload = json.loads(out)
    text = ((payload.get("result") or {}).get("content") or [{}])[0].get("text", "")
    return session, json.loads(text)


def cell(text: str) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) > SNIPPET:
        flat = flat[:SNIPPET] + "…"
    return flat.replace("|", "\\|")


def render(index: int, entry: dict, data: dict) -> str:
    lines = [f"### {index}. {entry['query']}", ""]
    context = cell(entry.get("context") or "")
    if context:
        lines += [f"当时上下文: {context}", ""]
    lines += ["| # | 层 | 分数 | 内容摘要 | 判定 |", "|---|---|---|---|---|"]
    for rank, item in enumerate(data.get("results") or [], 1):
        lines.append(
            f"| {rank} | {item.get('layer', '')} | {round(float(item.get('score') or 0), 3)} "
            f"| {cell(item.get('content'))} | |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default="tests/eval/datasets/queries_gold.jsonl")
    parser.add_argument("--out", default="tests/eval/datasets")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--split", type=int, default=3)
    parser.add_argument("--prefix", default="judge_round2")
    args = parser.parse_args()

    entries = [
        json.loads(line) for line in open(args.gold, encoding="utf-8") if line.strip()
    ]
    session = open_session(args.endpoint)
    size = (len(entries) + args.split - 1) // args.split
    for part in range(args.split):
        chunk = entries[part * size : (part + 1) * size]
        if not chunk:
            continue
        blocks = [
            "判定口径：相关=看完就不用再找；部分=还得再找；不相关=主题不同或仅词面撞车。",
            "",
        ]
        index_rows = []
        for offset, entry in enumerate(chunk, 1):
            session, data = call_recall(
                args.endpoint, entry["query"], args.limit, session
            )
            blocks.append(render(part * size + offset, entry, data))
            index_rows.append(
                {
                    "query": entry["query"],
                    "ids": [item.get("id") for item in data.get("results") or []],
                }
            )
        path = f"{args.out}/{args.prefix}_{part + 1}.md"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(blocks))
        # Row order in the markdown maps to this id list, so judging stays blind.
        with open(
            f"{args.out}/{args.prefix}_{part + 1}.jsonl", "w", encoding="utf-8"
        ) as fh:
            for row in index_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"wrote {path} ({len(chunk)} queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
