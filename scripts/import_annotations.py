#!/usr/bin/env python3
"""Turn filled-in annotation Markdown into a gold dataset.

Verdict cells accept `相关` / `部分` / `不相关` or the one-keystroke shorthand
`1` / `2` / `3`; anything else (including empty) counts as not judged and is
dropped, so a half-annotated file still produces a consistent gold set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

VERDICTS = {
    "相关": "relevant",
    "部分": "partial",
    "不相关": "unrelated",
    # Shorthand for hand-filling: one keystroke per cell.
    "1": "relevant",
    "2": "partial",
    "3": "unrelated",
}
HEADING_RE = re.compile(r"^##\s+\d+\.\s+(.*)$")
ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([^|]*)\|\s*((?:[^|\\]|\\.)*)\|\s*([^|]*)\|\s*([^|]*)\|\s*(.*?)\s*\|$"
)


def parse_markdown(path: str) -> dict[str, list[tuple[str, str]]]:
    """query -> [(verdict, locator)]."""
    judged: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            heading = HEADING_RE.match(line.rstrip("\n"))
            if heading:
                current = heading.group(1).strip()
                judged.setdefault(current, [])
                continue
            if current is None:
                continue
            row = ROW_RE.match(line.rstrip("\n"))
            if not row:
                continue
            verdict = row.group(2).strip()
            locator = row.group(3).strip().replace("\\|", "|")
            if verdict in VERDICTS:
                judged[current].append((VERDICTS[verdict], locator))
    return judged


def index_results(path: str) -> dict[str, dict]:
    """query -> exported row (results + clarity + context)."""
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            out[row["query"]] = row
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="tests/eval/datasets")
    parser.add_argument("--md", default="")
    parser.add_argument("--jsonl", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    md_path = args.md or os.path.join(args.dir, "annotation_round1.md")
    jsonl_path = args.jsonl or os.path.join(args.dir, "annotation_round1.jsonl")
    out_path = args.out or os.path.join(args.dir, "queries_gold.jsonl")

    judged = parse_markdown(md_path)
    results = index_results(jsonl_path)

    gold: list[dict] = []
    stats = Counter()
    for query, verdicts in judged.items():
        if not verdicts:
            continue
        exported = results.get(query) or {}
        by_locator = {}
        for row in exported.get("results") or []:
            by_locator[row["locator"]] = row
            if row.get("locator_short"):
                by_locator[row["locator_short"]] = row
        relevant, partial = [], []
        for verdict, locator in verdicts:
            item = by_locator.get(locator)
            if item is None:
                stats["missing_locator"] += 1
                continue
            entry = {
                "id": item.get("id"),
                "locator": locator,
                "score": item.get("score"),
                "anchor": (item.get("content") or "")[:60],
            }
            stats[verdict] += 1
            if verdict == "relevant":
                relevant.append(entry)
            elif verdict == "partial":
                partial.append(entry)
        if not relevant and not partial:
            stats["query_without_hits"] += 1
            continue
        gold.append(
            {
                "query": query,
                "clarity": exported.get("clarity", "specific"),
                "context": exported.get("context", ""),
                "relevant": relevant,
                "partial": partial,
                "unrelated_ok": not relevant and not partial,
            }
        )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in gold:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    total_judged = sum(stats[k] for k in ("relevant", "partial", "unrelated"))
    print(
        f"judged {total_judged} results across {len(gold)} queries "
        f"(relevant {stats['relevant']}, partial {stats['partial']}, "
        f"unrelated {stats['unrelated']}, missing {stats['missing_locator']})"
    )
    print(f"wrote {out_path}")
    return 0 if gold else 1


if __name__ == "__main__":
    sys.exit(main())
