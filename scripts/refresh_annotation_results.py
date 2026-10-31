#!/usr/bin/env python3
"""Re-run a sample of an annotation file and replace only its results.

Query, clarity and context lines are copied byte for byte; the status line and
the result table are regenerated from the live pipeline. The verdict column is
left empty on purpose: the old verdicts describe rows that no longer exist.

    python scripts/refresh_annotation_results.py --count 100

Writes `<prefix>.md` plus `<prefix>.jsonl`; the jsonl is what
`import_annotations.py` needs to turn the filled-in table back into gold.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_annotation_set import (  # noqa: E402
    call_tool,
    locator_of,
    query_kind,
    short_locator,
    summarize,
)

HEADING_RE = re.compile(r"^## (\d+)\. (.*)$")
MARKER_RE = re.compile(r"^标记：`(.*)`")
CONTEXT_RE = re.compile(r"^当时：(.*)$")


def parse_blocks(path: str) -> list[dict]:
    """One dict per `## N. query` section, keeping the untouched lines."""
    blocks: list[dict] = []
    current: dict | None = None
    for line in open(path, encoding="utf-8"):
        raw = line.rstrip("\n")
        heading = HEADING_RE.match(raw)
        if heading:
            current = {
                "number": int(heading.group(1)),
                "query": heading.group(2).strip(),
            }
            blocks.append(current)
            continue
        if current is None:
            continue
        marker = MARKER_RE.match(raw)
        if marker:
            current["marker"] = marker.group(1).rstrip()
            continue
        context = CONTEXT_RE.match(raw)
        if context:
            current["context"] = context.group(1)
    return blocks


def evenly(items: list[dict], count: int) -> list[dict]:
    """Spread the sample across the whole file instead of taking a prefix."""
    if count >= len(items):
        return items
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


def render_block(block: dict, result: dict) -> list[str]:
    lines = [f"## {block['number']}. {block['query']}"]
    lines.append(f"标记：`{block.get('marker', '')}`  ")
    if "context" in block:
        lines.append(f"当时：{block['context']}")
    results = result.get("results") or []
    lines.append(f"状态：`{result.get('status')}` · 返回 {len(results)} 条")
    lines.append("")
    if not results:
        lines.append("_（无结果）_")
        return lines
    lines.append("| # | 判定 | 定位 | 分数 | 来源 | 摘要 |")
    lines.append("|---|---|---|---|---|---|")
    for rank, item in enumerate(results, 1):
        locator = locator_of(item)
        lines.append(
            f"| {rank} |  | {short_locator(item, locator)} | {item.get('score')} "
            f"| {item.get('score_kind', '')} | {summarize(item.get('content', ''))} |"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", default="tests/eval/datasets/annotation_round1.md")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9001/mcp")
    parser.add_argument(
        "--prefix", default="tests/eval/datasets/annotation_round1_jina"
    )
    args = parser.parse_args()

    blocks = parse_blocks(args.md)
    picked = evenly(blocks, args.count)
    print(f"sampled {len(picked)} of {len(blocks)} queries")

    lines = [
        f"# 检索标注（抽样重跑 {len(picked)}/{len(blocks)} 条）",
        "",
        "判定口径同第 1 轮：`相关` / `部分` / `不相关`，留空 = 未判定。",
        f"编号沿用 {Path(args.md).name} 的原始编号，便于对照。",
        "",
        "---",
        "",
    ]
    index_rows = []
    errors = 0
    for offset, block in enumerate(picked, 1):
        started = time.perf_counter()
        try:
            result = call_tool(args.endpoint, "recall", block["query"], args.limit)
        except Exception as exc:  # noqa: BLE001 - keep the rest of the sample
            errors += 1
            print(f"  [{offset}] failed: {block['query']}: {exc}")
            result = {"status": "error", "results": []}
        lines.extend(render_block(block, result))
        lines.append("")
        index_rows.append(
            {
                "query": block["query"],
                "context": block.get("context", ""),
                "clarity": block.get("marker", "specific"),
                "tool": "recall",
                "language": query_kind(block["query"]),
                "status": result.get("status"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "results": [
                    {
                        "id": item.get("id"),
                        "locator": locator_of(item),
                        "locator_short": short_locator(item, locator_of(item)),
                        "score": item.get("score"),
                        "score_kind": item.get("score_kind"),
                        "content": summarize(item.get("content", "")),
                    }
                    for item in (result.get("results") or [])[: args.limit]
                ],
                "result": result,
            }
        )
        if offset % 25 == 0:
            print(f"  {offset}/{len(picked)}")

    md_path = f"{args.prefix}.md"
    jsonl_path = f"{args.prefix}.jsonl"
    Path(md_path).write_text("\n".join(lines), encoding="utf-8")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for row in index_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {md_path} and {jsonl_path} ({len(picked)} queries, {errors} errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
