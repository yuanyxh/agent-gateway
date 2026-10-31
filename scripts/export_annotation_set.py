#!/usr/bin/env python3
"""Export a retrieval annotation set for human judging.

Each query gets the current top-k results rendered as a Markdown table with an
empty verdict column. Fill in `相关` / `部分` / `不相关`, then run
`import_annotations.py` to build the gold dataset.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from itertools import count

DEFAULT_ENDPOINT = "http://127.0.0.1:9001/mcp"
PROJECTS = "~/.claude/projects"
SUMMARY_CHARS = 120

CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z]")
COUNTER = count(880000)


def query_kind(text: str) -> str:
    cjk = bool(CJK_RE.search(text))
    latin = bool(LATIN_RE.search(text))
    if cjk and latin:
        return "mixed"
    if cjk:
        return "cjk"
    if latin:
        return "latin"
    return "other"


SYNTHETIC_RE = re.compile(
    r"(zzzz|qwerty|asdfgh|zxcv|lorem ipsum|no_such|imaginary|pokemon|pikachu"
    r"|靐|龘|甲乙丙丁|987654321|424242|666666|112233|20261030|____)",
    re.IGNORECASE,
)
CONTEXT_CHARS = 120


def user_text(row: dict) -> str:
    """Real user prose; tool results and injected rule blocks are not context."""
    if row.get("type") != "user":
        return ""
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        return ""
    text = " ".join(text.split())
    if not text or text.startswith("<") or "--rule_start" in text:
        return ""
    return text


def query_clarity(text: str) -> str:
    """`vague` queries cannot define relevance; they are excluded from Recall.

    - synthetic: seeded negative / gibberish strings
    - vague: one topic word, so every same-topic hit looks "relevant"
    - specific: at least two terms that pin down what the answer must contain
    """
    if SYNTHETIC_RE.search(text):
        return "synthetic"
    words = [w for w in text.strip().split() if w]
    if len(words) < 2:
        return "vague"
    return "specific"


def collect_queries(tool: str) -> list[dict]:
    """Unique queries for a tool plus the user message that preceded each call."""
    suffix = f"__{tool}"
    seen: dict[str, dict] = {}
    paths = sorted(
        glob.glob(
            os.path.expanduser(os.path.join(PROJECTS, "**", "*.jsonl")), recursive=True
        ),
        key=os.path.getmtime,
        reverse=True,
    )
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        last_user = ""
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                text = user_text(row)
                if text:
                    last_user = text[:CONTEXT_CHARS]
                content = (row.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    if not str(item.get("name", "")).endswith(suffix):
                        continue
                    query = str((item.get("input") or {}).get("query") or "").strip()
                    if len(query) < 3 or query in seen:
                        continue
                    seen[query] = {
                        "query": query,
                        "context": last_user,
                        "clarity": query_clarity(query),
                    }
    return list(seen.values())


def stratified(queries: list[dict], count: int) -> list[dict]:
    """Round-robin over language buckets so no bucket dominates."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for entry in queries:
        buckets[query_kind(entry["query"])].append(entry)
    order = ["mixed", "latin", "cjk", "other"]
    picked: list[dict] = []
    index = 0
    while len(picked) < count and any(buckets[k] for k in order):
        key = order[index % len(order)]
        index += 1
        if buckets[key]:
            picked.append(buckets[key].pop(0))
    return picked


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


def summarize(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat[:SUMMARY_CHARS].replace("|", "\\|")


def locator_of(item: dict) -> str:
    """Locator plus a short id: several chunks share a file path, so the id is
    what makes one table row distinguishable from another."""
    loc = item.get("locator") or {}
    if loc.get("file"):
        line = loc.get("line")
        base = f"{loc['file']}:{line}" if line else str(loc["file"])
    elif item.get("source_file"):
        base = str(item["source_file"])
    else:
        base = f"{item.get('layer', '?')}"
    short = str(item.get("id", ""))[:8]
    return f"{base} [{short}]" if short else base


def short_id(item_id: str) -> str:
    """Stable 6-char handle; raw ids are uuid or `notes:<path>`, both unreadable."""
    return hashlib.sha1(str(item_id).encode("utf-8")).hexdigest()[:6]


def short_locator(item: dict, locator: str) -> str:
    """Readable locator: source kind + file name, plus a unique short handle."""
    item_id = str(item.get("id", ""))
    layer = str(item.get("layer") or "")
    if item_id.startswith("notes:"):
        layer = "notes"
    path = locator.split(" [")[0]
    name = os.path.basename(path) or path
    tag = layer or item.get("kind") or "?"
    handle = short_id(item_id) if item_id else ""
    return f"{tag} · {name} [{handle}]" if handle else f"{tag} · {name}"


def render(out_dir: str, rows: list[dict], limit: int) -> str:
    md_path = os.path.join(out_dir, "annotation_round1.md")
    jsonl_path = os.path.join(out_dir, "annotation_round1.jsonl")
    lines = [
        "# 检索标注（第 1 轮）",
        "",
        "**判断标准**：想象你当时问了这个 query，看到这条结果会不会觉得有用。",
        "",
        "- `相关` — 这条直接回答了 query 要的东西",
        "- `部分` — 同主题，或能顺着它找到答案（给了正确的文件名 / 符号名）",
        "- `不相关` — 主题不同，或只是词面撞车",
        "",
        "只看内容和 query 的关系，不用管内容本身对不对。留空 = 未判定，不影响统计。",
        "",
        "填完运行：`python scripts/import_annotations.py --dir tests/eval/datasets`",
        "",
        "---",
        "",
    ]
    for index, row in enumerate(rows, 1):
        lines.append(f"## {index}. {row['query']}")
        meta = [f"标记：`{row.get('clarity', '?')}`"]
        if row.get("context"):
            meta.append(f"当时：{row['context']}")
        lines.append("  \n".join(meta))
        data = row["result"]
        lines.append(
            f"状态：`{data.get('status')}` · 返回 {data.get('count', 0)} 条"
            + (
                f" · 关联 {data.get('related_count')} 条"
                if data.get("related_count")
                else ""
            )
        )
        lines.append("")
        results = data.get("results") or []
        if not results:
            lines.append("_（无结果）_")
            lines.append("")
            continue
        lines.append("| # | 判定 | 定位 | 分数 | 来源 | 摘要 |")
        lines.append("|---|---|---|---|---|---|")
        for rank, item in enumerate(results[:limit], 1):
            lines.append(
                f"| {rank} |  | {short_locator(item, locator_of(item))} | {item.get('score')} "
                f"| {item.get('score_kind', '')} | {summarize(item.get('content', ''))} |"
            )
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default="recall")
    parser.add_argument("--count", type=int, default=400)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out", default="tests/eval/datasets")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()

    queries = collect_queries(args.tool)
    picked = stratified(queries, args.count)
    print(f"collected {len(queries)} unique queries, picked {len(picked)}")

    os.makedirs(args.out, exist_ok=True)
    rows: list[dict] = []
    errors = 0
    for index, entry in enumerate(picked, 1):
        query = entry["query"]
        started = time.perf_counter()
        try:
            result = call_tool(args.endpoint, args.tool, query, args.limit)
        except Exception as exc:  # noqa: BLE001 - keep going, record the failure
            errors += 1
            print(f"  [{index}/{len(picked)}] ERROR {query[:40]}: {exc}")
            continue
        rows.append(
            {
                "query": query,
                "context": entry["context"],
                "clarity": entry["clarity"],
                "tool": args.tool,
                "language": query_kind(query),
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
        if index % 25 == 0:
            print(f"  {index}/{len(picked)}")

    path = render(args.out, rows, args.limit)
    print(f"wrote {path} ({len(rows)} queries, {errors} errors)")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
