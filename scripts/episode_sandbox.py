#!/usr/bin/env python3
"""Seed and inspect an isolated episode-grouping sandbox."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path

SANDBOX = Path(
    os.environ.get(
        "EPISODE_SANDBOX", Path.home() / ".cache/agent-gateway/episode-sandbox"
    )
)
GRAPH_DB = SANDBOX / "home/memory_graph.sqlite"
IDS_FILE = SANDBOX / "ids.jsonl"
COUNTER = count(1)

CHAINS = [
    {
        "project": "agent-gateway",
        "task": "episode 维度埋点",
        "offset": 0,
        "items": [
            (
                "同项目不同任务在向量空间里不可分，需要 project 与 task 两个写入侧维度。",
                "实验结论：0.45-0.75 区间同事件对与不同事件对严重重叠。",
            ),
            (
                "决定维度先只记录不合并，避免污染现有 episode 分组。",
                "分组逻辑未接入维度前，合并会放大误判。",
            ),
            (
                "remember 与 remember_batch 增加 project、task 可选参数并透传到 metadata。",
                "调用方需要能把任务维度带进写入链路。",
            ),
            (
                "图节点 metadata 是白名单，link.py 未列出的字段会被 upsert 丢弃。",
                "context 此前只进 Qdrant payload，图里查不到。",
            ),
            (
                "重启后新写入节点 metadata 已验证含 context、project、task。",
                "端到端确认埋点生效，历史节点不回填。",
            ),
        ],
    },
    {
        "project": "yjzt-wms-app",
        "task": "底部导航改造",
        "offset": 1,
        "items": [
            (
                "底部导航改用 wot-ui 的 wd-tabbar 与 wd-tabbar-item，不再自研。",
                "自研导航与 wot-ui 主题变量不同步。",
            ),
            (
                "删除 YJTabbar 组件并迁移页面引用到 wd-tabbar。",
                "保留两套实现会导致样式与交互分叉。",
            ),
            (
                "修复底部安全区高度，改用 constant(safe-area-inset-bottom) 兼容旧 iOS。",
                "鸿蒙与旧 iOS 上 fixed 高度会遮挡内容。",
            ),
            (
                "导航改造回归通过，首页、消息、我的三个 tab 切换正常。",
                "改造完成后的验收记录。",
            ),
        ],
    },
    {
        "project": "utility",
        "task": "记忆图工具",
        "offset": 2,
        "items": [
            (
                "memory_graph 增加 episode 边查询，支持按组查看同事件记忆。",
                "recall 之外的图查询需要同组遍历能力。",
            ),
            (
                "修复 direction 参数，up 与 down 的边方向此前反了。",
                "反向遍历会漏掉版本链上游。",
            ),
            (
                "memory_graph 默认 limit 从 50 调整为 20，避免返回过大。",
                "图查询结果过大会挤占模型上下文。",
            ),
            (
                "补 episode 边与方向参数的单元测试，覆盖 up、down、both。",
                "图查询方向错误不会报错，只能靠测试发现。",
            ),
        ],
    },
    {
        "project": "self-service-docker",
        "task": "memory-server 部署",
        "offset": 3,
        "items": [
            (
                "升级 memory-server 镜像到最新构建，修复 memory_get_recent 返回空列表。",
                "旧镜像的分页参数默认值错误。",
            ),
            (
                "处理 SSL UNEXPECTED_EOF_WHILE_READING，连接池增加重试。",
                "上游网关偶发断连导致写入失败。",
            ),
            (
                "部署后健康检查通过，memory_get 与 remember 各调用一次成功。",
                "部署验收记录。",
            ),
        ],
    },
    {
        "project": "gen_articles",
        "task": "文章生成管线",
        "offset": 5,
        "items": [
            (
                "文章模板从 Jinja2 迁移到内嵌 f-string，减少运行时依赖。",
                "部署环境缺少模板目录导致渲染失败。",
            ),
            (
                "修复 markdown 表格渲染，补充管道符转义。",
                "表格内容含竖线时渲染错列。",
            ),
            (
                "生成结果增加摘要字段，长度为正文前 120 字。",
                "列表页需要摘要而不加载全文。",
            ),
            (
                "管线发布，连续生成 10 篇文章无渲染错误。",
                "发布验收记录。",
            ),
        ],
    },
    {
        "project": "agent-gateway",
        "task": "recall 检索层",
        "offset": 7,
        "items": [
            (
                "recall 采用关键词与语义双路检索，用 RRF 融合排序。",
                "单路检索在专有名词与口语查询上各有盲区。",
            ),
            (
                "增加 threshold gate，低分结果在返回前过滤。",
                "无关结果会挤占上下文并误导模型。",
            ),
            (
                "冷层命中按 episode 组扩展，每组最多 3 条并乘 0.98 系数。",
                "同组记忆能提供事件上下文。",
            ),
            (
                "线上观测 143 条查询零错误，默认关闭多信号排序。",
                "默认开启后排序收益不稳定。",
            ),
        ],
    },
    {
        "project": "utility",
        "task": "notes 层降噪",
        "offset": 9,
        "items": [
            (
                "notes 结果增加语义复核，相似度低于 0.40 的直接丢弃。",
                "笔记切片与查询意图偏差较大。",
            ),
            (
                "notes 改为仅兜底，hot 或 cold 有命中时不进入主结果。",
                "笔记会稀释主记忆的排序。",
            ),
            (
                "抽样 100 条查询，notes 占比从 26.2% 降到 8.5%。",
                "降噪效果验证记录。",
            ),
        ],
    },
    {
        "project": "orchestrate",
        "task": "任务编排重试",
        "offset": 12,
        "items": [
            (
                "编排任务增加指数退避重试，最多三次。",
                "下游偶发超时导致整批任务失败。",
            ),
            (
                "单任务超时从 60 秒调整为 180 秒。",
                "长任务被误判为超时。",
            ),
            (
                "重试与超时参数写入编排配置，不再硬编码在代码里。",
                "不同任务需要不同预算。",
            ),
        ],
    },
    {
        "project": "yjzt-wms-app",
        "task": "useToast 封装",
        "offset": 14,
        "items": [
            (
                "封装 hooks/application/useToast.ts，统一 toast 调用方式。",
                "页面直接调用 wot-ui 的 useToast 导致 selector 冲突。",
            ),
            (
                "修复 YJPage 嵌套页面中 toast 不显示，改为挂载到根节点。",
                "嵌套页面选择器命中了错误的容器。",
            ),
            (
                "useToast 封装回归通过，登录页与列表页提示正常。",
                "验收记录。",
            ),
        ],
    },
    {
        "project": "agent-gateway",
        "task": "配置解耦",
        "offset": 18,
        "items": [
            (
                "配置唯一来源改为 agent-gateway.yaml，env 只做覆盖。",
                "多份配置导致启动行为不一致。",
            ),
            (
                "旧宿主形状经 core/config_compat.py 翻译，保留一个发布周期。",
                "直接删除旧字段会让宿主启动失败。",
            ),
            (
                "跨仓契约改为 contracts/*.json，消费方不再扫描源码。",
                "源码扫描在打包后失效。",
            ),
        ],
    },
]


def call(tool: str, arguments: dict, timeout: float = 60) -> dict:
    endpoint = os.environ.get("EPISODE_MCP", "http://127.0.0.1:9011/mcp")
    body = {
        "jsonrpc": "2.0",
        "id": next(COUNTER),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = urllib.request.Request(
        endpoint,
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
    ids = []
    for index, chain in enumerate(CHAINS, 1):
        memories = [
            {
                "content": content,
                "context": context,
                "type": "fact",
                "project": chain["project"],
                "task": chain["task"],
            }
            for content, context in chain["items"]
        ]
        result = call(
            "remember_batch",
            {
                "memories": memories,
                "project": chain["project"],
                "task": chain["task"],
            },
        )
        rows = result.get("results", []) if isinstance(result, dict) else result
        written = [row.get("memory_id") or "" for row in rows]
        errors = [row.get("error") for row in rows if not row.get("memory_id")]
        ids.append(
            {
                "chain": index,
                "project": chain["project"],
                "task": chain["task"],
                "offset": chain["offset"],
                "memories": written,
            }
        )
        print(f"[seed] chain {index} {chain['task']}: {len(written)} written")
        if errors:
            print(f"[seed] chain {index} errors: {errors[:2]}")
    IDS_FILE.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in ids) + "\n",
        encoding="utf-8",
    )
    print(f"[seed] wrote {IDS_FILE}")
    return 0


def retime() -> int:
    from qdrant_client import QdrantClient

    rows = [
        json.loads(line)
        for line in IDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    base = datetime.now() - timedelta(days=28)
    qdrant = QdrantClient(path=str(SANDBOX / "qdrant/mem0"))
    conn = sqlite3.connect(str(GRAPH_DB))
    nodes = points = 0
    for row in rows:
        stamp = (base + timedelta(days=row["offset"])).isoformat()
        ids = [memory_id for memory_id in row["memories"] if memory_id]
        cursor = conn.executemany(
            "UPDATE memory_nodes SET created_at=? WHERE node_id=?",
            [(stamp, memory_id) for memory_id in ids],
        )
        nodes += cursor.rowcount
        qdrant.set_payload(
            collection_name="mem0",
            payload={"created_at": stamp},
            points=ids,
        )
        points += len(ids)
    conn.commit()
    conn.close()
    qdrant.close()
    print(f"[retime] graph nodes {nodes}, qdrant points {points}")
    return 0


def report() -> int:
    rows = [
        json.loads(line)
        for line in IDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chain_of = {}
    nodes: dict[str, dict] = {}
    for row in rows:
        for memory_id in row["memories"]:
            if not memory_id:
                continue
            chain_of[memory_id] = row["chain"]
            payload = call(
                "memory_graph",
                {"node_id": memory_id, "edge": "episode", "limit": 1},
            )
            node = next(
                (
                    item
                    for item in payload.get("nodes") or []
                    if item["node_id"] == memory_id
                ),
                {},
            )
            nodes[memory_id] = node
    items = list(nodes)
    pairs = [
        (nodes[items[i]], nodes[items[j]], chain_of[items[i]] == chain_of[items[j]])
        for i in range(len(items))
        for j in range(i + 1, len(items))
    ]
    rules = {
        "project 相同": lambda a, b: (
            a["metadata"].get("project") == b["metadata"].get("project")
        ),
        "task 相同": lambda a, b: (
            a["metadata"].get("task") == b["metadata"].get("task")
        ),
        "project 且 task 相同": lambda a, b: (
            a["metadata"].get("project") == b["metadata"].get("project")
            and a["metadata"].get("task") == b["metadata"].get("task")
        ),
    }
    recall_rows = []
    for row in rows:
        seed = row["memories"][0]
        title = str(nodes[seed].get("title") or "")
        result = call("recall", {"query": title[:120], "keywords": [], "limit": 10})
        returned = {item.get("id") for item in result.get("results") or []}
        related = {item.get("id") for item in result.get("related") or []}
        siblings = set(row["memories"]) - {seed}
        recall_rows.append(
            (
                row["task"],
                len(returned & siblings),
                len(related & siblings),
                len(siblings),
            )
        )
    lines = [
        "# 沙箱维度测试报告",
        "",
        f"节点 {len(nodes)}，配对 {len(pairs)}，同链对 "
        f"{sum(1 for pair in pairs if pair[2])}。",
        "",
        "## 配对判别（真值=同链）",
        "",
        "| 规则 | 命中 | 误合并 |",
        "| --- | --- | --- |",
    ]
    for name, rule in rules.items():
        hit = sum(1 for a, b, same in pairs if same and rule(a, b))
        false = sum(1 for a, b, same in pairs if not same and rule(a, b))
        lines.append(f"| {name} | {hit} | {false} |")
    lines += [
        "",
        "## recall 同链召回",
        "",
        "| 事件链 | results 命中 | related 命中 | 链内其他条数 |",
        "| --- | --- | --- | --- |",
    ]
    for task, hit, rel, total in recall_rows:
        lines.append(f"| {task} | {hit} | {rel} | {total} |")
    lines.append("")
    path = SANDBOX / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[report] wrote {path}")
    return 0


def _cosine(left, right) -> float:
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    scale = sum(float(a) * float(a) for a in left) ** 0.5
    scale *= sum(float(b) * float(b) for b in right) ** 0.5
    return dot / scale if scale else 0.0


def noise() -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault(
        "FASTEMBED_CACHE_PATH", os.path.expanduser("~/.cache/fastembed_cache")
    )
    from fastembed import TextEmbedding

    rows = [
        json.loads(line)
        for line in IDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    contents: dict[str, str] = {}
    chain_of: dict[str, int] = {}
    for row, chain in zip(rows, CHAINS):
        for memory_id, item in zip(row["memories"], chain["items"]):
            contents[memory_id] = item[0]
            chain_of[memory_id] = row["chain"]
    episodes: dict[str, str] = {}
    for memory_id in contents:
        payload = call(
            "memory_graph", {"node_id": memory_id, "edge": "episode", "limit": 1}
        )
        node = next(
            (
                item
                for item in payload.get("nodes") or []
                if item["node_id"] == memory_id
            ),
            {},
        )
        episodes[memory_id] = str(node.get("metadata", {}).get("episode_id") or "")
    groups: dict[str, list[str]] = {}
    for memory_id, episode in episodes.items():
        if episode:
            groups.setdefault(episode, []).append(memory_id)
    model = TextEmbedding("jinaai/jina-embeddings-v2-base-zh")
    vectors = dict(zip(contents, model.embed(list(contents.values()))))
    floors = (0.56, 0.40, 0.30, 0.0)
    totals = {floor: [0, 0] for floor in floors}
    for row, chain in zip(rows, CHAINS):
        query = chain["items"][0][0]
        query_vec = next(model.embed([query]))
        result = call("recall", {"query": query, "keywords": [], "limit": 10})
        results = result.get("results") or []
        known = {item.get("id") for item in results}
        seeds = [item for item in results if item.get("episode_id")][:3]
        for floor in floors:
            seen = set(known)
            for seed in seeds:
                for memory_id in groups.get(seed.get("episode_id"), []):
                    if memory_id in seen:
                        continue
                    seen.add(memory_id)
                    if _cosine(query_vec, vectors[memory_id]) < floor:
                        continue
                    totals[floor][0] += 1
                    if chain_of[memory_id] == row["chain"]:
                        totals[floor][1] += 1
    lines = [
        "# episode 扩展地板噪声评估",
        "",
        "| 地板 | 带出条数 | 同链 | 噪声 | 精度 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for floor in floors:
        total, same = totals[floor]
        noise_count = total - same
        precision = same / total if total else 0.0
        lines.append(
            f"| {floor:.2f} | {total} | {same} | {noise_count} | {precision:.0%} |"
        )
    lines.append("")
    path = SANDBOX / "noise_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[noise] wrote {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "retime", "report", "noise"])
    args = parser.parse_args()
    if args.command == "seed":
        return seed()
    if args.command == "retime":
        return retime()
    if args.command == "report":
        return report()
    return noise()


if __name__ == "__main__":
    raise SystemExit(main())
