#!/usr/bin/env python3
"""Offline dry_run of LLM episode-group governance."""

from __future__ import annotations

# ruff: noqa: E402 - cache isolation must run before agent_gateway imports

try:
    from _eval_env import isolate_cache
except ModuleNotFoundError:
    from scripts._eval_env import isolate_cache

isolate_cache(__file__)

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import group_governance

DEFAULT_OUT = (
    Path(__file__).resolve().parents[1]
    / "tests/eval/group_governance"
    / datetime.now().strftime("%Y-%m-%d")
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="memory graph sqlite path")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--single-pair-limit", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-calls", type=int, default=85)
    parser.add_argument("--no-llm", action="store_true", help="candidates only")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="re-render report.md from the existing suggestions.jsonl",
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    candidates = group_governance.build_candidates(
        db_path=args.db,
        single_pair_limit=args.single_pair_limit,
        readonly=True,
    )
    rows = list(candidates["groups"]) + list(candidates["pairs"])
    if not rows and not candidates["singles"]:
        print(
            json.dumps(
                {
                    "error": "no candidates; check --db points at the graph",
                    "db": args.db,
                },
                ensure_ascii=False,
            )
        )
        return 2
    if args.render_only:
        decisions = _read_jsonl(out / "suggestions.jsonl")
        result = {
            "decisions": decisions,
            "calls": 0,
            "parse_failed": 0,
            "rejected": {},
            "status": "render_only",
            "render_only": True,
        }
    else:
        _write_jsonl(out / "candidates.jsonl", rows)
        llm = (
            None if args.no_llm or not llm_client.is_configured() else llm_client.achat
        )
        result = asyncio.run(
            group_governance.decide(
                candidates,
                batch_size=args.batch_size,
                llm=llm,
                max_calls=args.max_calls,
            )
        )
        _write_jsonl(out / "suggestions.jsonl", result["decisions"])

    report = group_governance.render_report(candidates, result)
    report += (
        f"\n- 耗时：{time.monotonic() - started:.1f}s"
        f"\n- 数据库：{args.db or 'default'}\n"
    )
    (out / "report.md").write_text(report, encoding="utf-8")

    print(
        json.dumps(
            {
                "candidates": len(rows),
                "decisions": len(result["decisions"]),
                "calls": result["calls"],
                "parse_failed": result["parse_failed"],
                "status": result["status"],
                "out": str(out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
