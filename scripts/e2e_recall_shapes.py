#!/usr/bin/env python3
"""Probe a running MCP endpoint with the recall shape matrix.

Each sample runs in several caller shapes (query only, keywords only, both, and
with a project scope) so a regression in one shape is visible in the report.
Output is one JSON line per call plus a summary; run it against a sandbox
endpoint, never the live one.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from itertools import count

DEFAULT_ENDPOINT = "http://127.0.0.1:9011/mcp"
COUNTER = count(910000)

#: (name, query, keywords, project, expect_hit). expect_hit marks samples whose
#: topic is known to exist in the store; the no-content samples guard against
#: false positives from floor fallback.
SAMPLES: list[tuple[str, str, list[str], str | None, bool]] = [
    (
        "release-clear",
        "放货页面点击异常登记后返回，页面内容被清空",
        ["放货", "异常登记"],
        None,
        True,
    ),
    (
        "devanning-print",
        "拆柜打托的重新打印与托号扫描逻辑",
        ["拆柜打托", "isPrint", "重新打印"],
        None,
        True,
    ),
    (
        "safe-area",
        "我的页安全区域如何避让状态栏",
        ["安全区域", "safe-area", "底部间距"],
        None,
        True,
    ),
    (
        "review",
        "项目审查上次的结论与未关闭项",
        ["projects_review", "审查", "遗留"],
        None,
        True,
    ),
    (
        "npm-publish",
        "npm publish version bump flow",
        ["npm publish", "Verdaccio", "版本号"],
        None,
        True,
    ),
    (
        "floor-design",
        "recall floor and filter_fallback design",
        ["recall", "floor", "filter_fallback"],
        None,
        True,
    ),
    ("symbol", "getSafeAreaInset implementation", ["getSafeAreaInset"], None, True),
    (
        "ticket",
        "HZQWBQ-6016 提货签到拦截",
        ["HZQWBQ-6016", "提货签到", "放货"],
        None,
        True,
    ),
    (
        "memory-caller",
        "记忆体调用方的设计讨论",
        ["记忆体", "调用方", "记忆使用时机"],
        None,
        True,
    ),
    (
        "local-model",
        "本地推理 ollama 的成本与隐私",
        ["本地推理", "ollama", "API 成本"],
        None,
        True,
    ),
    (
        "episode",
        "episode 分组治理与组间关联",
        ["episode_id", "分组治理", "记忆组"],
        None,
        True,
    ),
    (
        "symlink",
        "软链接 postinstall 同步框架",
        ["软链接", "postinstall", "同步"],
        None,
        True,
    ),
    ("absent-physics", "量子纠缠实验的采购流程", ["量子纠缠", "采购流程"], None, False),
    ("absent-mars", "火星基地的水循环系统", ["火星基地", "水循环"], None, False),
    ("absent-token", "qzx-void-9471", ["qzx-void-9471"], None, False),
    # A 2026-09 porting-lessons memory exists, so this sample guards recall of
    # a real topic rather than a false positive.
    ("porting-history", "TMS 销售端页面移植进度", ["页面移植", "DT50"], None, True),
    ("scoped-wms", "拆柜打托重新打印", ["拆柜打托", "isPrint"], "yjzt-wms-app", True),
    ("scoped-tms", "我的页安全区域", ["安全区域", "safe-area"], "yjzt-tms-app", True),
]


def _jsonrpc_payload(raw: str) -> dict:
    """The JSON-RPC response, from a plain body or from an SSE event stream."""
    if not raw.lstrip().startswith("event:") and "\ndata:" not in raw:
        return json.loads(raw)
    candidates = []
    for event in raw.split("\n\n"):
        lines = [line[5:] for line in event.splitlines() if line.startswith("data:")]
        if not lines:
            continue
        try:
            candidates.append(json.loads("\n".join(lines)))
        except ValueError:
            continue
    for candidate in reversed(candidates):
        if isinstance(candidate, dict) and (
            "result" in candidate or "error" in candidate
        ):
            return candidate
    raise ValueError(f"No JSON-RPC response in: {raw[:200]!r}")


def call(endpoint: str, arguments: dict, retries: int = 1) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": next(COUNTER),
        "method": "tools/call",
        "params": {"name": "recall", "arguments": arguments},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
            payload = _jsonrpc_payload(raw)
            result = payload.get("result") or {}
            text = (result.get("content") or [{}])[0].get("text", "")
            if result.get("isError"):
                raise RuntimeError(text[:200])
            return json.loads(text)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(2)
    return {}


def shapes(sample: tuple) -> list[tuple[str, dict]]:
    _, query, keywords, project, _ = sample
    # keywords is required by the schema; an empty list is the query-only form.
    out = [
        ("query", {"keywords": [], "query": query, "limit": 5}),
        ("keywords", {"keywords": keywords, "limit": 5}),
        ("both", {"query": query, "keywords": keywords, "limit": 5}),
        ("single", {"keywords": keywords[:1], "limit": 5}),
    ]
    if project:
        out.append(
            (
                "both+project",
                {"query": query, "keywords": keywords, "project": project, "limit": 5},
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--out", default="")
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    rows = []
    for sample in SAMPLES:
        name, _, _, _, expect = sample
        for shape, arguments in shapes(sample):
            started = time.perf_counter()
            data = call(args.endpoint, arguments)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            rows.append(
                {
                    "label": args.label,
                    "sample": name,
                    "shape": shape,
                    "expect_hit": expect,
                    "status": data.get("status"),
                    "count": data.get("count"),
                    "floor_fallback_count": data.get("floor_fallback_count"),
                    "rerank_fallback_count": data.get("rerank_fallback_count"),
                    "fallback_count": data.get("fallback_count"),
                    "filtered": data.get("filtered"),
                    "ids": [r.get("id") for r in data.get("results") or []],
                    "elapsed_ms": elapsed,
                }
            )
            print(json.dumps(rows[-1], ensure_ascii=False))

    by_shape = defaultdict(lambda: {"calls": 0, "hits": 0, "empty": 0})
    for row in rows:
        bucket = by_shape[row["shape"]]
        bucket["calls"] += 1
        if row["count"]:
            bucket["hits"] += 1
        else:
            bucket["empty"] += 1
    expected = [r for r in rows if r["expect_hit"]]
    absent = [r for r in rows if not r["expect_hit"]]
    summary = {
        "label": args.label,
        "calls": len(rows),
        "by_shape": {k: dict(v) for k, v in by_shape.items()},
        "expected_hit_rate": round(
            sum(1 for r in expected if r["count"]) / max(1, len(expected)) * 100, 1
        ),
        "absent_hit_rate": round(
            sum(1 for r in absent if r["count"]) / max(1, len(absent)) * 100, 1
        ),
        "floor_fallback_rows": sum(1 for r in rows if r["floor_fallback_count"]),
        "latency_p50": round(
            statistics.median([r["elapsed_ms"] for r in rows]) if rows else 0, 1
        ),
        "latency_max": max((r["elapsed_ms"] for r in rows), default=0),
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {"summary": summary, "rows": rows}, fh, ensure_ascii=False, indent=2
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
