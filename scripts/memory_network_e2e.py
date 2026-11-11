#!/usr/bin/env python3
"""End-to-end checks on an isolated copy of the real memory store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_gateway.services.memory import (  # noqa: E402
    group_governance,
    lifecycle,
    network,
    network_build,
    store,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(f"{item.relative_to(path)}|{item.stat().st_size}".encode())
    return digest.hexdigest()


def _config_qdrant(config: Path) -> Path:
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return Path()
    match = re.search(r'"qdrantPath"\s*:\s*"([^"]+)"', text)
    if match:
        value = match.group(1).replace("\\/", "/")
    else:
        try:
            import yaml

            data = yaml.safe_load(text) or {}
            value = str((data.get("memory") or {}).get("qdrantPath") or "")
        except Exception:  # noqa: BLE001 - an unreadable config just means no source
            return Path()
    return Path(value).expanduser() if value else Path()


def _copy_graph(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def _synthetic(db: str) -> None:
    now = datetime(2026, 11, 10, 12, 0, 0)
    specs = (
        ("e2e_a1", "ep_e2e_a"),
        ("e2e_a2", "ep_e2e_a"),
        ("e2e_b1", "ep_e2e_b"),
        ("e2e_b2", "ep_e2e_b"),
        ("e2e_c1", "ep_e2e_c"),
    )
    for node_id, episode in specs:
        store.upsert_node(
            node_id,
            layer="synthetic",
            status="active",
            title=node_id,
            content_hash_value=node_id,
            metadata={"episode_id": episode, "fact": node_id, "importance": 6},
            db_path=db,
        )
        network.ensure_neuron({"neuron_id": episode, "episode_id": episode}, db_path=db)
        network.upsert_member(episode, node_id, db_path=db)
        network.upsert_activation(
            node_id,
            {
                "strength": 0.6,
                "tier": "active",
                "activation_count": 1,
                "last_activated_at": now.isoformat(),
            },
            db_path=db,
        )
    network.upsert_synapse(
        {"from_neuron": "ep_e2e_a", "to_neuron": "ep_e2e_b", "weight": 0.9},
        db_path=db,
    )
    network.upsert_synapse(
        {"from_neuron": "ep_e2e_b", "to_neuron": "ep_e2e_c", "weight": 0.9},
        db_path=db,
    )
    lifecycle_rows = (
        (
            "e2e_s1",
            "archive",
            (now - timedelta(days=1)).isoformat(),
            False,
            120,
            6,
            0.1,
        ),
        (
            "e2e_s2",
            "archive",
            (now + timedelta(days=10)).isoformat(),
            False,
            120,
            6,
            0.1,
        ),
        ("e2e_s3", "active", None, False, 0, 6, 0.6),
        ("e2e_s4", "archive", (now - timedelta(days=1)).isoformat(), True, 120, 6, 0.1),
        ("e2e_s5", "archive", (now - timedelta(days=1)).isoformat(), False, 40, 2, 0.1),
    )
    for (
        node_id,
        tier,
        purge_after,
        protected,
        idle,
        importance,
        value,
    ) in lifecycle_rows:
        store.upsert_node(
            node_id,
            layer="synthetic",
            status="active",
            title=node_id,
            content_hash_value=node_id,
            metadata={
                "episode_id": f"ep_{node_id}",
                "fact": node_id,
                "importance": importance,
                "protected": protected,
            },
            db_path=db,
        )
        network.ensure_neuron(
            {"neuron_id": f"ep_{node_id}", "episode_id": f"ep_{node_id}"}, db_path=db
        )
        network.upsert_member(f"ep_{node_id}", node_id, db_path=db)
        network.upsert_activation(
            node_id,
            {
                "strength": value,
                "tier": tier,
                "activation_count": 0,
                "last_activated_at": (now - timedelta(days=idle)).isoformat(),
                "purge_after": purge_after,
            },
            db_path=db,
        )
    network.refresh_neuron_counts(db_path=db)


def _call(port: int, tool: str, arguments: dict, timeout: float = 180.0) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
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
    return json.loads(text) if text else {}


def _wait_health(port: int, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as response:
                return json.loads(response.read().decode())
        except Exception:  # noqa: BLE001 - startup polling
            time.sleep(1)
    raise TimeoutError("MCP health check timed out")


def run(args) -> dict:
    home_src = Path(
        args.source_home or os.environ.get("GATEWAY_HOME") or "~/.config/utility"
    ).expanduser()
    config_src = Path(
        os.environ.get("GATEWAY_CONFIG") or home_src / "agent-gateway.yaml"
    ).expanduser()
    qdrant_value = (
        args.source_qdrant
        or os.environ.get("QDRANT_PATH")
        or _config_qdrant(config_src)
    )
    qdrant_src = Path(qdrant_value).expanduser() if qdrant_value else None
    if not args.copy_prod:
        raise SystemExit("--copy-prod is required to touch a real store")
    graph_src = home_src / "memory_graph.sqlite"
    if not graph_src.is_file():
        raise SystemExit(f"graph not found: {graph_src}")
    source_graph_hash = _sha256(graph_src)
    source_qdrant_hash = (
        _manifest(qdrant_src) if qdrant_src and qdrant_src.is_dir() else "missing"
    )

    root = Path(tempfile.mkdtemp(prefix="memory-network-e2e-"))
    home = root / "home"
    cache = root / "cache"
    qdrant = root / "qdrant"
    home.mkdir(parents=True)
    cache.mkdir(parents=True)
    for name in ("agent-gateway.yaml", "config.yaml", "retrieval_thresholds.json"):
        source = home_src / name
        if source.is_file():
            shutil.copy2(source, home / name)
    _copy_graph(graph_src, home / "memory_graph.sqlite")
    if qdrant_src and qdrant_src.is_dir():
        shutil.copytree(qdrant_src, qdrant)
    db = str(home / "memory_graph.sqlite")

    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_HOME": str(home),
            "GATEWAY_CACHE": str(cache),
            "GATEWAY_CONFIG": str(home / "agent-gateway.yaml"),
            "QDRANT_PATH": str(qdrant),
            "MCP_PORT": str(args.port),
            "PYTHONPATH": str(REPO / "src"),
            "MEMORY_NEURON_ENABLED": "true",
            "MEMORY_NEURON_BUILD": "true",
            "MEMORY_NEURON_LLM_PAIRS": "0",
            "RECALL_RERANK": "0",
            "GATEWAY_LANG": "zh-Hans",
            "EMBEDDER_MODEL": args.model,
            "FASTEMBED_CACHE_PATH": str(Path(args.fastembed).expanduser()),
        }
    )
    log_path = root / "mcp.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from agent_gateway.services.mcp.server import main; "
                "sys.exit(main())",
            ],
            cwd=str(REPO),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    result = {
        "sandbox": str(root),
        "checks": {},
        "started_at": datetime.now().isoformat(),
    }
    try:
        health = _wait_health(args.port)
        result["health"] = health
        first = network_build.migrate_network(db_path=db, apply=True)
        second = network_build.migrate_network(db_path=db, apply=True)
        result["checks"]["migration"] = {
            "ok": first["nodes"] == second["nodes"]
            and first["neurons"] == second["neurons"],
            "first": first,
            "second": second,
        }
        _synthetic(db)
        os.environ.setdefault("QDRANT_PATH", str(qdrant))
        from agent_gateway.services.retrieval.arms.associative import spread

        path = spread(
            [{"memory_id": "e2e_a1", "neuron_id": "ep_e2e_a", "activation": 1.0}],
            db_path=db,
            depth=3,
            beam=5,
            floor=0.15,
        )
        activated = {item["neuron_id"]: item for item in path["activated"]}
        result["checks"]["associative"] = {
            "ok": "ep_e2e_c" in activated and len(activated["ep_e2e_c"]["path"]) == 3,
            "path": activated.get("ep_e2e_c", {}).get("path", []),
        }
        tool = _call(
            args.port,
            "memory_graph",
            {"node_id": "e2e_a1", "edge": "associative"},
        )
        result["checks"]["mcp_associative"] = {
            "ok": {item["neuron_id"] for item in tool.get("activated", [])}
            == {"ep_e2e_a", "ep_e2e_b", "ep_e2e_c"},
            "activated": [item["neuron_id"] for item in tool.get("activated", [])],
        }
        now = datetime(2026, 11, 10, 12, 0, 0)
        tiers = lifecycle.classify_tiers(now=now, db_path=db)
        dry = lifecycle.purge_expired(now=now, db_path=db, dry_run=True)
        purged = lifecycle.purge_expired(now=now, db_path=db)
        result["checks"]["lifecycle"] = {
            "ok": dry["eligible"] >= 2
            and purged["deleted"] >= 2
            and store.get_node("e2e_s1", db_path=db) is None
            and store.get_node("e2e_s5", db_path=db) is None
            and store.get_node("e2e_s2", db_path=db) is not None
            and store.get_node("e2e_s4", db_path=db) is not None,
            "tiers": tiers,
            "dry": dry,
            "purged": purged,
        }
        pair_candidates = group_governance.load_group_pair_candidates(
            db_path=db, min_weight=0.62
        )
        merged = network_build.merge_neurons("ep_e2e_b", "ep_e2e_a", db_path=db)
        result["checks"]["governance"] = {
            "ok": bool(pair_candidates)
            and merged["members"] == 2
            and network.get_neuron("ep_e2e_b", db_path=db) is None,
            "candidates": len(pair_candidates),
            "merged": merged,
        }
        if not args.no_recall:
            started = time.perf_counter()
            recalled = _call(
                args.port, "recall", {"keywords": ["记忆", "分组"], "limit": 5}
            )
            result["checks"]["recall"] = {
                "ok": recalled.get("status") in ("ok", "weak_match", "no_match")
                and "results" in recalled,
                "status": recalled.get("status"),
                "count": recalled.get("count"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "trace_id": recalled.get("trace_id", ""),
            }
        result["checks"]["production_unchanged"] = {
            "ok": _sha256(graph_src) == source_graph_hash
            and (
                _manifest(qdrant_src)
                if qdrant_src and qdrant_src.is_dir()
                else "missing"
            )
            == source_qdrant_hash,
            "graph_sha256": source_graph_hash,
            "qdrant_manifest": source_qdrant_hash,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        result["log_tail"] = log_path.read_text(encoding="utf-8")[-2000:]
        result["sandbox_kept"] = bool(args.keep)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    result["all_ok"] = all(
        item.get("ok") for item in result["checks"].values() if isinstance(item, dict)
    )
    return result


def _report(result: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 记忆神经网 v2 E2E 报告",
        "",
        f"- 时间：{result.get('started_at', '')}",
        f"- 沙箱：`{result.get('sandbox', '')}`（保留：{result.get('sandbox_kept')}）",
        f"- 总体：{'PASS' if result.get('all_ok') else 'FAIL'}",
        "",
        "## 检查项",
        "",
        "| 检查 | 结果 |",
        "| --- | --- |",
    ]
    for name, item in result.get("checks", {}).items():
        lines.append(f"| {name} | {'PASS' if item.get('ok') else 'FAIL'} |")
    lines += [
        "",
        "## 关键数据",
        "",
        "```json",
        json.dumps(
            {k: v for k, v in result.get("checks", {}).items()},
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "## 健康检查",
        "",
        "```json",
        json.dumps(result.get("health", {}), ensure_ascii=False, indent=2),
        "```",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-home", default="")
    parser.add_argument("--source-qdrant", default="")
    parser.add_argument("--copy-prod", action="store_true")
    parser.add_argument("--port", type=int, default=9013)
    parser.add_argument("--model", default="jinaai/jina-embeddings-v2-base-zh")
    parser.add_argument("--fastembed", default="~/.cache/fastembed_cache")
    parser.add_argument("--no-recall", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--out", default=str(REPO / "tests/eval/memory_network/2026-11-10")
    )
    args = parser.parse_args()
    result = run(args)
    _report(result, Path(args.out))
    print(
        json.dumps(
            {"all_ok": result["all_ok"], "checks": result["checks"]}, ensure_ascii=False
        )
    )
    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
