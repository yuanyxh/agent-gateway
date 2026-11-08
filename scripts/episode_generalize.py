#!/usr/bin/env python3
"""Generalized episode-grouping evaluation built from real hot-layer titles."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import count
from pathlib import Path

GRAPH_DB = os.environ.get(
    "MEMORY_GRAPH_DB", os.path.expanduser("~/.config/utility/memory_graph.sqlite")
)
OUT_DIR = Path(
    os.environ.get(
        "EPISODE_GENERALIZE_OUT",
        Path.home() / ".cache/agent-gateway/episode-sandbox3",
    )
)
MCP_ENDPOINT = os.environ.get("EPISODE_MCP", "http://127.0.0.1:9013/mcp")
COUNTER = count(1)
PROJECT_TOKENS = (
    "yjzt",
    "utility",
    "agent-gateway",
    "agent-vibe",
    "token_proxy",
    "gen_articles",
    "rss",
    "memory",
    "recall",
    "self-service-docker",
    "baidu",
    "orchestrate",
)
TASK_NOISE = ("", "处理", "调整", "修复", "补充")


def project_of(text: str) -> str:
    lowered = text.lower()
    return next((token for token in PROJECT_TOKENS if token in lowered), "")


def grams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text.lower())
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def load_nodes(per_project: int = 80) -> list[dict]:
    query = (
        "SELECT node_id, created_at, title FROM memory_nodes "
        "WHERE layer='hot' AND status='active' AND title != ''"
    )
    for mode in ("mode=ro", "mode=ro&immutable=1"):
        conn = sqlite3.connect(f"file:{GRAPH_DB}?{mode}", uri=True)
        try:
            rows = conn.execute(query).fetchall()
            break
        except sqlite3.OperationalError:
            conn.close()
    else:
        raise RuntimeError(f"cannot open {GRAPH_DB}")
    conn.close()
    buckets: dict[str, list[dict]] = defaultdict(list)
    for node_id, created_at, title in rows:
        project = project_of(title)
        if not project or not created_at:
            continue
        buckets[project].append(
            {"id": node_id, "created": created_at, "title": title, "project": project}
        )
    picked = []
    for project, items in sorted(buckets.items()):
        items.sort(key=lambda item: item["created"])
        step = max(1, len(items) // per_project)
        picked.extend(items[::step][:per_project])
    return picked


def build_chains(nodes: list[dict], rng: random.Random) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for node in nodes:
        try:
            stamp = datetime.fromisoformat(node["created"])
        except ValueError:
            continue
        iso = stamp.isocalendar()
        node["week"] = f"{iso.year}-{iso.week // 2}"
        buckets[(node["project"], node["week"])].append(node)
    chains: list[dict] = []
    for (project, week), items in sorted(buckets.items()):
        items = list(items)
        rng.shuffle(items)
        while items:
            seed = items.pop()
            chain = [seed]
            rest = []
            for node in items:
                if (
                    len(chain) < 6
                    and jaccard(grams(seed["title"]), grams(node["title"])) >= 0.15
                ):
                    chain.append(node)
                else:
                    rest.append(node)
            items = rest
            if len(chain) >= 3:
                chains.append({"project": project, "week": week, "nodes": chain})
    return chains


def task_of(title: str, rng: random.Random) -> str:
    cut = rng.choice((8, 10, 12, 16))
    task = title[:cut].strip("，。：: ")
    noise = rng.choice(TASK_NOISE)
    return f"{task}{noise}" if noise and rng.random() < 0.4 else task


def _naive(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=None) if stamp.tzinfo else stamp


def _days(left: str, right: str) -> int:
    try:
        return abs((_naive(right) - _naive(left)).days)
    except ValueError:
        return 999


def simulate(chains: list[dict], threshold: float) -> dict:
    groups: list[dict] = []
    assignments: dict[str, int] = {}
    for chain in chains:
        for node in chain["nodes"]:
            node["task"] = task_of(node["title"], random.Random(node["id"]))
            chosen = None
            for index, group in enumerate(groups):
                if group["project"] != node["project"]:
                    continue
                if len(group["members"]) >= 8:
                    continue
                if _days(node["created"], group["created"]) > 14:
                    continue
                if any(
                    jaccard(grams(node["task"]), grams(task)) >= threshold
                    for task in group["tasks"]
                ):
                    chosen = index
                    break
            if chosen is None:
                groups.append(
                    {
                        "project": node["project"],
                        "created": node["created"],
                        "tasks": [node["task"]],
                        "members": [node["id"]],
                    }
                )
                assignments[node["id"]] = len(groups) - 1
            else:
                groups[chosen]["tasks"].append(node["task"])
                groups[chosen]["members"].append(node["id"])
                assignments[node["id"]] = chosen
    chain_of = {}
    for index, chain in enumerate(chains):
        for node in chain["nodes"]:
            chain_of[node["id"]] = index
    same_hit = same_total = false = cross_total = 0
    ids = list(chain_of)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            left, right = ids[i], ids[j]
            same = chain_of[left] == chain_of[right]
            grouped = assignments[left] == assignments[right]
            if same:
                same_total += 1
                same_hit += grouped
            else:
                cross_total += 1
                false += grouped
    purity_sum = purity_weight = 0
    for group in groups:
        counts = Counter(chain_of[memory_id] for memory_id in group["members"])
        purity_sum += max(counts.values())
        purity_weight += len(group["members"])
    covered = 0
    for chain in chains:
        ids_in_chain = [node["id"] for node in chain["nodes"]]
        counts = Counter(assignments[memory_id] for memory_id in ids_in_chain)
        covered += max(counts.values())
    title_of = {
        node["id"]: node["title"] for chain in chains for node in chain["nodes"]
    }
    details = [
        {
            "project": group["project"],
            "members": [
                {"chain": chain_of[memory_id], "title": title_of[memory_id][:50]}
                for memory_id in group["members"]
            ],
        }
        for group in groups
        if len({chain_of[memory_id] for memory_id in group["members"]}) > 1
    ]
    return {
        "threshold": threshold,
        "nodes": len(ids),
        "chains": len(chains),
        "groups": len(groups),
        "same_hit": same_hit,
        "same_total": same_total,
        "false": false,
        "cross_total": cross_total,
        "purity": purity_sum / purity_weight if purity_weight else 0.0,
        "coverage": covered / len(ids) if ids else 0.0,
        "details": details,
    }


def call(tool: str, arguments: dict, timeout: float = 120) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": next(COUNTER),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = urllib.request.Request(
        MCP_ENDPOINT,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    result = payload.get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    if result.get("isError"):
        raise RuntimeError(text[:300])
    return json.loads(text)


def seed() -> int:
    payload = [
        json.loads(line)
        for line in (OUT_DIR / "chains.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = []
    for chain in payload:
        memories = [
            {
                "content": f"{node['title']} [泛化评估]",
                "context": "泛化评估样本",
                "type": "fact",
                "project": chain["project"],
                "task": task_of(node["title"], random.Random(node["id"])),
            }
            for node in chain["nodes"]
        ]
        result = call(
            "remember_batch", {"memories": memories, "project": chain["project"]}
        )
        rows = result.get("results", []) if isinstance(result, dict) else result
        ids.append(
            {
                "chain": chain["chain"],
                "project": chain["project"],
                "memories": [row.get("memory_id") or "" for row in rows],
            }
        )
        print(f"[seed] chain {chain['chain']}: {len(rows)} written")
    (OUT_DIR / "ids.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in ids) + "\n",
        encoding="utf-8",
    )
    print(f"[seed] wrote {OUT_DIR / 'ids.jsonl'}")
    return 0


def evaluate() -> int:
    ids = [
        json.loads(line)
        for line in (OUT_DIR / "ids.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    payload = [
        json.loads(line)
        for line in (OUT_DIR / "chains.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    titles = {chain["chain"]: chain["nodes"][0]["title"] for chain in payload}
    chain_of = {}
    episodes = {}
    for row in ids:
        for memory_id in row["memories"]:
            if not memory_id:
                continue
            chain_of[memory_id] = row["chain"]
            graph = call(
                "memory_graph",
                {"node_id": memory_id, "edge": "episode", "limit": 1},
            )
            node = next(
                (
                    item
                    for item in graph.get("nodes") or []
                    if item["node_id"] == memory_id
                ),
                {},
            )
            episodes[memory_id] = str(node.get("metadata", {}).get("episode_id") or "")
    by_episode: dict[str, Counter] = defaultdict(Counter)
    for memory_id, episode in episodes.items():
        by_episode[episode][chain_of[memory_id]] += 1
    purity = sum(max(counter.values()) for counter in by_episode.values()) / len(
        episodes
    )
    by_chain: dict[int, list[str]] = defaultdict(list)
    for memory_id, episode in episodes.items():
        by_chain[chain_of[memory_id]].append(episode)
    covered = sum(Counter(values).most_common(1)[0][1] for values in by_chain.values())
    coverage = covered / len(episodes)
    same_results = same_related = siblings_total = 0
    for row in ids:
        query = titles.get(row["chain"], "")
        if not query:
            continue
        result = call("recall", {"query": query[:120], "keywords": [], "limit": 10})
        returned = {item.get("id") for item in result.get("results") or []}
        related = {item.get("id") for item in result.get("related") or []}
        siblings = set(row["memories"]) - {row["memories"][0]}
        same_results += len(returned & siblings)
        same_related += len(related & siblings)
        siblings_total += len(siblings)
    lines = [
        "# 泛化端到端评估（沙箱）",
        "",
        f"节点 {len(episodes)}，链 {len(ids)}。",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 组数 | {len(by_episode)} |",
        f"| 组纯度 | {purity:.1%} |",
        f"| 链覆盖 | {coverage:.1%} |",
        f"| 同链 results 命中 | {same_results}/{siblings_total} |",
        f"| 同链 related 命中 | {same_related}/{siblings_total} |",
        "",
    ]
    path = OUT_DIR / "sandbox_e2e_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[eval] wrote {path}")
    return 0


def scan() -> int:
    rng = random.Random(20261108)
    nodes = load_nodes()
    chains = build_chains(nodes, rng)
    rows = [
        simulate(chains, threshold)
        for threshold in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 1.0)
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "chain": index,
            "project": chain["project"],
            "week": chain["week"],
            "nodes": [
                {"id": node["id"], "title": node["title"], "created": node["created"]}
                for node in chain["nodes"]
            ],
        }
        for index, chain in enumerate(chains)
    ]
    (OUT_DIR / "chains.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in payload) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 泛化分组评估（真实历史标题）",
        "",
        f"节点 {rows[0]['nodes']}，链 {rows[0]['chains']}。链真值=同项目同周且标题 2-gram ≥0.15。",
        "",
        "| task 阈值 | 组数 | 链覆盖 | 同链同组 | 组纯度 | 跨链误合并 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['threshold']:.2f} | {row['groups']} | "
            f"{row['coverage']:.1%} | {row['same_hit']}/{row['same_total']} "
            f"({row['same_hit'] / row['same_total']:.1%}) | "
            f"{row['purity']:.1%} | {row['false']}/{row['cross_total']} |"
        )
    lines.append("")
    path = OUT_DIR / "generalize_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    target = next(row for row in rows if row["threshold"] == 0.30)
    sample_lines = ["# 阈值 0.30 混合组样例", ""]
    for index, detail in enumerate(target["details"][:8], 1):
        sample_lines.append(f"## 组 {index}（{detail['project']}）")
        for member in detail["members"]:
            sample_lines.append(f"- 链 {member['chain']}: {member['title']}")
        sample_lines.append("")
    sample_path = OUT_DIR / "generalize_samples.md"
    sample_path.write_text("\n".join(sample_lines) + "\n", encoding="utf-8")
    print(f"[generalize] wrote {sample_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", default="scan", choices=["scan", "seed", "eval"]
    )
    args = parser.parse_args()
    if args.command == "seed":
        return seed()
    if args.command == "eval":
        return evaluate()
    return scan()


if __name__ == "__main__":
    raise SystemExit(main())
