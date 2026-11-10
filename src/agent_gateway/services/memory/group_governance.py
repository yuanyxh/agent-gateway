"""Offline LLM review of memory episode groups, plus apply of split/merge decisions.

``govern_daily`` writes both mem0 metadata and the local graph. The local write
runs last; if it fails, already-synced mem0 rows are restored to their previous
episode fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Awaitable, Callable

from agent_gateway.core import config, prompt_loader
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import graph, store

logger = setup_logging("server")

MAX_CANDIDATES = 850
MAX_GROUP_MEMBERS = 8
PAGE_SIZE = 2000
MAX_BATCH_CHARS = 32000
SINGLE_PAIR_LIMIT = 200
SINGLE_PAIR_PER_NODE = 2
SINGLE_PAIR_TOP_K = 5
TITLE_FALLBACK_FLOOR = 0.15
TITLE_LOOKUP_LIMIT = 10
DEFAULT_EXPANDED_FLOOR = 0.56
MAX_BATCH = 10
MAX_CALLS = 85
TEXT_LIMIT = 4000
ACTIONS = {"keep", "split", "merge"}

LlmCallable = Callable[..., Awaitable[str | None]]


def _meta(node: dict) -> dict:
    return node.get("metadata") or {}


def _episode(node: dict) -> str:
    return str(_meta(node).get("episode_id") or "").strip()


def _body(node: dict) -> tuple[str, str]:
    """Full fact text and its source; title is only a degraded fallback."""
    meta = _meta(node)
    fact = str(meta.get("fact") or "").strip()
    if fact:
        return fact[:TEXT_LIMIT], "fact"
    return str(node.get("title") or "")[:TEXT_LIMIT], "title"


def _member(node: dict, episode: str = "") -> dict:
    text, source = _body(node)
    return {
        "id": str(node.get("node_id") or ""),
        "episode_id": episode or _episode(node),
        "text": text,
        "text_source": source,
        "truncated": len(str(_meta(node).get("fact") or "")) > TEXT_LIMIT,
        "created_at": str(node.get("created_at") or ""),
    }


def _load_active_nodes(
    db_path: str | None = None, *, readonly: bool = False
) -> list[dict]:
    nodes: list[dict] = []
    offset = 0
    while True:
        page = store.list_nodes(
            layer="hot",
            status="active",
            limit=PAGE_SIZE,
            offset=offset,
            readonly=readonly,
            db_path=db_path,
        )
        nodes.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return nodes


def load_groups(db_path: str | None = None, *, readonly: bool = False) -> dict:
    """Bucket active hot nodes by episode; skip system output and protected rows."""
    nodes = _load_active_nodes(db_path, readonly=readonly)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        meta = _meta(node)
        if meta.get("generated_by") or meta.get("protected"):
            continue
        episode = _episode(node)
        if episode:
            buckets[episode].append(node)
    groups: list[dict] = []
    singles: list[dict] = []
    explicit_suspect: list[str] = []
    oversized: list[str] = []
    for episode, members in sorted(buckets.items()):
        members.sort(key=lambda node: str(node.get("node_id") or ""))
        if len(members) < 2:
            if episode.startswith("ep_"):
                singles.append(_member(members[0], episode))
            else:
                explicit_suspect.append(episode)
        elif len(members) > MAX_GROUP_MEMBERS:
            oversized.append(episode)
        elif not episode.startswith("ep_"):
            explicit_suspect.append(episode)
        else:
            groups.append(
                {
                    "candidate_id": episode,
                    "kind": "group",
                    "members": [_member(node, episode) for node in members],
                }
            )
    return {
        "groups": groups,
        "singles": singles,
        "explicit_suspect": explicit_suspect,
        "oversized": oversized,
    }


def _try_hot():
    try:
        from agent_gateway.services.mcp.server_state import get_hot_memory

        return get_hot_memory()
    except Exception:  # noqa: BLE001 - offline runs must not depend on the server
        return None


def _expanded_floor() -> float:
    try:
        from agent_gateway.services.mcp.server_embed import EMBEDDER_SPEC
        from agent_gateway.services.retrieval.gate.thresholds import floors_for

        return float(floors_for(EMBEDDER_SPEC.tag)["expanded"])
    except Exception:  # noqa: BLE001 - keep the offline prototype usable
        return DEFAULT_EXPANDED_FLOOR


def _dense(raw):
    if isinstance(raw, dict):
        raw = raw.get("") or raw.get("dense")
    return raw or None


def _load_vectors(hot, node_ids: list[str]) -> dict[str, list]:
    if hot is None or not node_ids:
        return {}
    vector_store = hot.vector_store
    out: dict[str, list] = {}
    for start in range(0, len(node_ids), 100):
        batch = node_ids[start : start + 100]
        try:
            points = vector_store.client.retrieve(
                collection_name=vector_store.collection_name,
                ids=batch,
                with_payload=False,
                with_vectors=True,
            )
        except Exception:  # noqa: BLE001 - caller falls back to titles
            return {}
        for point in points:
            vector = _dense(getattr(point, "vector", None))
            if vector:
                out[str(point.id)] = vector
    return out


def _vector_pairs(
    singles: list[dict], members: list[dict], hot, limit: int
) -> list[dict]:
    if hot is None or not singles:
        return []
    from qdrant_client import models

    allowed = {item["id"]: item for item in singles + members}
    vectors = _load_vectors(hot, [item["id"] for item in singles])
    if not vectors:
        return []
    floor = _expanded_floor()
    pairs: dict[tuple[str, str], float] = {}
    vector_store = hot.vector_store
    ids = [item["id"] for item in singles if item["id"] in vectors]
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        try:
            hits = vector_store.client.query_batch_points(
                collection_name=vector_store.collection_name,
                requests=[
                    models.QueryRequest(
                        query=vectors[node_id],
                        limit=SINGLE_PAIR_TOP_K,
                        with_payload=False,
                    )
                    for node_id in batch
                ],
            )
        except Exception:  # noqa: BLE001 - caller falls back to titles
            return []
        for source, result in zip(batch, hits):
            for point in getattr(result, "points", result):
                target = str(point.id)
                if target == source or target not in allowed:
                    continue
                if allowed[target]["episode_id"] == allowed[source]["episode_id"]:
                    continue
                score = float(point.score)
                if score < floor:
                    continue
                key = (source, target) if source < target else (target, source)
                pairs[key] = max(pairs.get(key, 0.0), score)
    ordered = sorted(pairs.items(), key=lambda item: -item[1])
    return _cap_pairs(ordered, allowed, limit)


def _grams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text.lower())
    return {clean[index : index + 2] for index in range(len(clean) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _title_pairs(singles: list[dict], members: list[dict], limit: int) -> list[dict]:
    nodes = singles + members
    if not singles or len(nodes) < 2:
        return []
    index: dict[str, set[str]] = defaultdict(set)
    for item in nodes:
        for gram in _grams(item["text"]):
            index[gram].add(item["id"])
    by_id = {item["id"]: item for item in nodes}
    grams = {item["id"]: _grams(item["text"]) for item in nodes}
    pairs: dict[tuple[str, str], float] = {}
    per_node: Counter = Counter()
    for single in singles:
        candidates: Counter = Counter()
        for gram in grams[single["id"]]:
            for other in index.get(gram, ()):
                if other == single["id"]:
                    continue
                if by_id[other]["episode_id"] == single["episode_id"]:
                    continue
                candidates[other] += 1
        ranked = sorted(candidates, key=lambda other: (-candidates[other], other))[
            :TITLE_LOOKUP_LIMIT
        ]
        scored = []
        for other in ranked:
            score = _jaccard(grams[single["id"]], grams[other])
            if score >= TITLE_FALLBACK_FLOOR:
                scored.append((score, other))
        for score, other in sorted(scored, reverse=True):
            if per_node[single["id"]] >= SINGLE_PAIR_PER_NODE:
                break
            if per_node[other] >= SINGLE_PAIR_PER_NODE:
                continue
            key = (
                (single["id"], other) if single["id"] < other else (other, single["id"])
            )
            pairs[key] = max(pairs.get(key, 0.0), score)
            per_node[single["id"]] += 1
            per_node[other] += 1
    ordered = sorted(pairs.items(), key=lambda item: -item[1])
    return _cap_pairs(ordered, by_id, limit)


def _cap_pairs(
    ordered: list[tuple[tuple[str, str], float]],
    allowed: dict[str, dict],
    limit: int,
) -> list[dict]:
    out: list[dict] = []
    per_node: Counter = Counter()
    for (left, right), score in ordered:
        if per_node[left] >= SINGLE_PAIR_PER_NODE:
            continue
        if per_node[right] >= SINGLE_PAIR_PER_NODE:
            continue
        out.append(
            {
                "candidate_id": f"pair:{left}|{right}",
                "kind": "pair",
                "members": [allowed[left], allowed[right]],
                "score": round(score, 3),
            }
        )
        per_node[left] += 1
        per_node[right] += 1
        if len(out) >= limit:
            break
    return out


def nearby_single_candidates(
    singles: list[dict],
    members: list[dict],
    *,
    limit: int = SINGLE_PAIR_LIMIT,
    hot=None,
) -> list[dict]:
    """Pairs for single-member groups; vector first, title 2-gram as fallback."""
    if limit <= 0:
        return []
    pairs = _vector_pairs(singles, members, hot, limit)
    if pairs:
        return pairs
    return _title_pairs(singles, members, limit)


def build_candidates(
    *,
    db_path: str | None = None,
    single_pair_limit: int = SINGLE_PAIR_LIMIT,
    hot=None,
    readonly: bool = False,
) -> dict:
    """Read-only candidate set for the daily governance review."""
    loaded = load_groups(db_path, readonly=readonly)
    if hot is None:
        hot = _try_hot()
    members = [member for group in loaded["groups"] for member in group["members"]]
    pairs = nearby_single_candidates(
        loaded["singles"], members, limit=single_pair_limit, hot=hot
    )
    total = len(loaded["groups"]) + len(pairs)
    truncated = total > MAX_CANDIDATES or (0 < single_pair_limit <= len(pairs))
    if total > MAX_CANDIDATES:
        room = max(0, MAX_CANDIDATES - len(loaded["groups"]))
        pairs = pairs[:room]
    return {
        "groups": loaded["groups"],
        "pairs": pairs,
        "singles": loaded["singles"],
        "explicit_suspect": loaded["explicit_suspect"],
        "oversized": loaded["oversized"],
        "truncated": truncated,
        "vector": hot is not None,
    }


def _candidate_items(candidates: dict) -> list[dict]:
    return list(candidates.get("groups") or []) + list(candidates.get("pairs") or [])


def render_candidates(items: list[dict]) -> str:
    payload = [
        {
            "candidate_id": item["candidate_id"],
            "kind": item["kind"],
            "members": [
                {"id": member["id"], "text": member["text"]}
                for member in item["members"]
            ],
        }
        for item in items
    ]
    return json.dumps({"candidates": payload}, ensure_ascii=False)


def _parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_decision(decision: dict, batch: list[dict]) -> tuple[dict | None, str]:
    if not isinstance(decision, dict):
        return None, "not_object"
    candidate_id = str(decision.get("candidate_id") or "")
    target = next(
        (item for item in batch if item["candidate_id"] == candidate_id), None
    )
    if target is None:
        return None, "unknown_candidate"
    action = str(decision.get("action") or "")
    if action not in ACTIONS:
        return None, "unknown_action"
    member_ids = {member["id"] for member in target["members"]}
    if action == "split":
        groups = decision.get("groups")
        if not isinstance(groups, list) or len(groups) < 2:
            return None, "bad_split"
        seen: list[str] = []
        for group in groups:
            if not isinstance(group, list) or not group:
                return None, "bad_split"
            for member_id in group:
                if not isinstance(member_id, str) or member_id not in member_ids:
                    return None, "unknown_member"
                seen.append(member_id)
        if sorted(seen) != sorted(member_ids) or len(set(seen)) != len(seen):
            return None, "bad_split"
    merge_with = ""
    if action == "merge" and target["kind"] == "group":
        merge_with = str(decision.get("merge_with") or "")
        if merge_with not in {item["candidate_id"] for item in batch}:
            return None, "unknown_merge_target"
        if merge_with == candidate_id:
            return None, "bad_merge"
    return {
        "candidate_id": candidate_id,
        "action": action,
        "groups": decision.get("groups") or [],
        "merge_with": merge_with,
        "reason": str(decision.get("reason") or "")[:200],
    }, ""


def _batches(items: list[dict], batch_size: int, max_chars: int) -> list[list[dict]]:
    """Group candidates into batches bounded by count and by input characters."""
    batches: list[list[dict]] = []
    batch: list[dict] = []
    size = 0
    for item in items:
        cost = sum(
            len(str(member.get("text") or "")) for member in item.get("members") or ()
        )
        if batch and (len(batch) >= batch_size or size + cost > max_chars):
            batches.append(batch)
            batch, size = [], 0
        batch.append(item)
        size += cost
    if batch:
        batches.append(batch)
    return batches


async def decide(
    candidates: dict,
    *,
    batch_size: int = MAX_BATCH,
    llm: LlmCallable | None = None,
    max_calls: int = MAX_CALLS,
) -> dict:
    """Ask the model for keep/split/merge opinions; never touches storage."""
    items = _candidate_items(candidates)
    empty = {
        "decisions": [],
        "calls": 0,
        "parse_failed": 0,
        "rejected": {},
        "status": "ok",
    }
    if not items:
        return empty
    if llm is None:
        return {**empty, "status": "llm_unavailable"}
    decisions: list[dict] = []
    rejected: Counter = Counter()
    calls = 0
    parse_failed = 0
    for batch in _batches(items, max(1, batch_size), MAX_BATCH_CHARS):
        if calls >= max_calls:
            break
        prompt = prompt_loader.prompt(
            "govern_groups", candidates_text=render_candidates(batch)
        )
        calls += 1
        try:
            raw = await llm(
                prompt,
                temperature=0.1,
                timeout=60,
                disable_thinking=True,
                label="Group governance",
            )
        except Exception:  # noqa: BLE001 - one bad batch must not drop the rest
            parse_failed += 1
            continue
        parsed = _parse_json(raw)
        if parsed is None or not isinstance(parsed.get("decisions"), list):
            parse_failed += 1
            continue
        for decision in parsed["decisions"]:
            clean, reason = _validate_decision(decision, batch)
            if clean is None:
                rejected[reason] += 1
                continue
            decisions.append(clean)
    return {
        "decisions": decisions,
        "calls": calls,
        "parse_failed": parse_failed,
        "rejected": dict(rejected),
        "status": "ok",
    }


DAILY_MAX_CALLS = 12
DAILY_BATCH_SIZE = 8


def _recent_candidates(candidates: dict, since: str) -> dict:
    """Keep only candidates touched by a memory written at or after `since`."""

    def touched(item: dict) -> bool:
        return any(
            str(member.get("created_at") or "") >= since
            for member in item.get("members") or ()
        )

    return {
        **candidates,
        "groups": [g for g in candidates.get("groups") or [] if touched(g)],
        "pairs": [p for p in candidates.get("pairs") or [] if touched(p)],
    }


def _new_episode_id(member_ids: list[str], now: datetime) -> str:
    digest = hashlib.sha1("|".join(sorted(member_ids)).encode()).hexdigest()[:8]
    return f"ep_{now:%Y%m%d}_{digest}"


def _episode_patch(member_id: str, new_episode: str, nodes: dict, run_at: str) -> dict:
    meta = (nodes.get(member_id) or {}).get("metadata") or {}
    patch = {
        "episode_id": new_episode,
        "episode_source": "auto",
        "governance_run": run_at,
    }
    previous = str(meta.get("episode_id") or "")
    if previous and not meta.get("episode_prev"):
        patch["episode_prev"] = previous
    return patch


def _sync_mem0(member_id: str, patch: dict) -> bool:
    """Mirror the episode change into mem0; recall reads its payload, not the graph."""
    try:
        from agent_gateway.services.mcp import server_memory

        hot = server_memory.get_hot_memory()
        if not hot:
            return False
        row = hot.get(member_id) or {}
        meta = dict(row.get("metadata") or {})
        meta.update(patch)
        hot.update(member_id, metadata=meta)
        return True
    except Exception as e:  # noqa: BLE001 - one node must not abort the batch
        logger.warning(f"[governance] mem0 sync failed for {member_id}: {e}")
        return False


def _restore_mem0(compensation: dict[str, dict]) -> int:
    """Write previous episode fields back; returns the rows that failed."""
    failed = 0
    for member_id, patch in compensation.items():
        if not _sync_mem0(member_id, patch):
            failed += 1
    return failed


def _rebuild_episode_edges(member_ids: list[str], db_path: str | None) -> None:
    if not member_ids:
        return
    graph.remove_episode_edges(member_ids, db_path=db_path)
    for member_id in member_ids:
        node = store.get_node(member_id, db_path=db_path) or {}
        episode = str((node.get("metadata") or {}).get("episode_id") or "")
        if not episode:
            continue
        members = store.find_nodes_by_metadata(
            "episode_id", episode, exclude=member_id, db_path=db_path
        )
        edges = [(member_id, member["node_id"], "episode") for member in members]
        if edges:
            graph.add_edges(edges, db_path=db_path)


def _apply_decisions(
    decisions: list[dict],
    candidates: dict,
    nodes: dict,
    *,
    single_ids: set[str],
    db_path: str | None,
    now: datetime,
) -> tuple[int, int, int, int]:
    """Write split/merge decisions; returns (applied, written, skipped, errors)."""
    by_id = {item["candidate_id"]: item for item in _candidate_items(candidates)}
    run_at = now.isoformat()
    updates: dict[str, dict] = {}
    applied = skipped = errors = 0
    compensation: dict[str, dict] = {}

    def queue(member_id: str, new_episode: str) -> bool:
        nonlocal errors
        patch = _episode_patch(member_id, new_episode, nodes, run_at)
        if member_id not in compensation:
            meta = (nodes.get(member_id) or {}).get("metadata") or {}
            compensation[member_id] = {
                key: str(meta[key])
                for key in (
                    "episode_id",
                    "episode_source",
                    "episode_prev",
                    "governance_run",
                )
                if key in meta
            }
        if _sync_mem0(member_id, patch):
            updates[member_id] = patch
            return True
        errors += 1
        return False

    for decision in decisions:
        item = by_id.get(decision["candidate_id"])
        if item is None:
            skipped += 1
            continue
        action = decision["action"]
        if action == "split":
            groups = decision.get("groups") or []
            if len(groups) < 2:
                skipped += 1
                continue
            keep = max(groups, key=len)
            changed = False
            for group in groups:
                if group is keep:
                    continue
                new_episode = _new_episode_id(group, now)
                for member_id in group:
                    meta = (nodes.get(member_id) or {}).get("metadata") or {}
                    if not str(meta.get("episode_id") or "").startswith("ep_"):
                        skipped += 1
                        continue
                    changed = queue(member_id, new_episode) or changed
            if changed:
                applied += 1
            else:
                skipped += 1
            continue
        if action != "merge":
            continue
        if item["kind"] == "pair":
            members = [member["id"] for member in item["members"]]
            if any(member_id not in single_ids for member_id in members):
                skipped += 1
                continue
            target_episode = str(
                ((nodes.get(members[0]) or {}).get("metadata") or {}).get("episode_id")
                or ""
            )
            if not target_episode.startswith("ep_"):
                skipped += 1
                continue
            changed = any(
                [queue(member_id, target_episode) for member_id in members[1:]]
            )
            if changed:
                applied += 1
            else:
                skipped += 1
            continue
        target = by_id.get(decision.get("merge_with") or "")
        if target is None or target["kind"] != "group":
            skipped += 1
            continue
        changed = any(
            [queue(member["id"], target["candidate_id"]) for member in item["members"]]
        )
        if changed:
            applied += 1
        else:
            skipped += 1
    written = 0
    try:
        if updates:
            written = store.batch_update_metadata(list(updates.items()), db_path)
    except Exception as e:  # noqa: BLE001 - local failure must undo mem0
        restored = _restore_mem0(compensation)
        logger.error(
            "[governance] local metadata write failed; restored %d/%d mem0 rows: %s",
            len(compensation) - restored,
            len(compensation),
            e,
        )
        errors += len(compensation)
        applied = 0
        return applied, written, skipped, errors
    if updates and written:
        _rebuild_episode_edges(list(updates), db_path)
    return applied, written, skipped, errors


async def govern_daily(
    *,
    db_path: str | None = None,
    since: str = "",
    max_calls: int = DAILY_MAX_CALLS,
    batch_size: int = DAILY_BATCH_SIZE,
) -> dict:
    """Daily pass: judge new groups, then apply split/merge to episode ids."""
    empty = {
        "status": "skipped",
        "candidates": 0,
        "applied": 0,
        "written": 0,
        "skipped": 0,
        "calls": 0,
        "errors": 0,
    }
    if not config.memory_governance_apply():
        return {**empty, "status": "disabled"}
    if not llm_client.is_configured():
        return {**empty, "status": "llm_unavailable"}
    try:
        candidates = build_candidates(db_path=db_path)
        nodes = {node["node_id"]: node for node in _load_active_nodes(db_path)}
        scoped = _recent_candidates(candidates, since)
        if not scoped["groups"] and not scoped["pairs"]:
            return {**empty, "status": "no_candidates"}
        result = await decide(
            scoped,
            batch_size=batch_size,
            llm=llm_client.achat,
            max_calls=max_calls,
        )
        single_ids = {member["id"] for member in candidates.get("singles") or []}
        applied, written, skipped, sync_errors = _apply_decisions(
            result["decisions"],
            scoped,
            nodes,
            single_ids=single_ids,
            db_path=db_path,
            now=datetime.now(),
        )
        return {
            "status": result["status"],
            "candidates": len(scoped["groups"]) + len(scoped["pairs"]),
            "applied": applied,
            "written": written,
            "skipped": skipped,
            "calls": result["calls"],
            "errors": result["parse_failed"] + sync_errors,
        }
    except Exception as e:  # noqa: BLE001 - daily work must not block on governance
        logger.warning(f"[governance] daily pass failed: {e}", exc_info=True)
        return {**empty, "status": "error"}


def _short_id(member_id: str) -> str:
    return str(member_id or "")[:8]


def _member_text(member: dict) -> str:
    text = str(member.get("text") or "").replace("|", "\\|")
    return text[:80]


def render_report(candidates: dict, result: dict) -> str:
    """Markdown review sheet: each candidate shows its members before the advice."""
    items = _candidate_items(candidates)
    decisions = result.get("decisions") or []
    by_id = {item["candidate_id"]: item for item in items}
    actions = Counter(item["action"] for item in decisions)
    reviewed = [
        by_id[item["candidate_id"]]
        for item in decisions
        if item["candidate_id"] in by_id
    ]
    members = [member for item in reviewed for member in item.get("members") or []]
    degraded = sum(1 for member in members if member.get("text_source") == "title")
    clipped = sum(1 for member in members if member.get("truncated"))
    total = len(items)
    rate = len(decisions) / total * 100 if total else 0.0
    calls = "—（render-only）" if result.get("render_only") else result.get("calls", 0)
    lines = [
        "# 记忆分组治理 dry_run 报告",
        "",
        "## 概览",
        "",
        f"- 候选总数：{total}（多成员组 {len(candidates.get('groups') or [])}，单条组近邻对 {len(candidates.get('pairs') or [])}）",
        f"- 已生成建议：{len(decisions)}（覆盖率 {rate:.1f}%）",
        f"- 单条组：{len(candidates.get('singles') or [])}；疑似显式组：{len(candidates.get('explicit_suspect') or [])}；超限组：{len(candidates.get('oversized') or [])}",
        f"- LLM 调用：{calls}；解析失败：{result.get('parse_failed', 0)}；拒绝：{result.get('rejected') or {}}；动作分布：{dict(actions)}",
        f"- 正文来源：fact {len(members) - degraded} / title {degraded}（degraded）；超 4000 字截断 {clipped}",
        f"- 向量近邻：{bool(candidates.get('vector'))}；截断：{bool(candidates.get('truncated'))}",
        "",
        f"## 抽检明细（{min(len(decisions), 50)}/{len(decisions)} 条）",
        "",
    ]
    for index, decision in enumerate(decisions[:50], 1):
        item = by_id.get(decision["candidate_id"]) or {}
        members = item.get("members") or []
        lines.append(
            f"### {index}. {decision['candidate_id']}"
            f"（{len(members)} 条，{decision['action']}）"
        )
        lines.append("")
        if members:
            lines.append("| # | 成员 |")
            lines.append("| --- | --- |")
            for position, member in enumerate(members, 1):
                lines.append(
                    f"| {position} | `{_short_id(member.get('id'))}`"
                    f" {_member_text(member)} |"
                )
            lines.append("")
        lines.append(f"**建议**：{decision['action']}")
        if decision.get("reason"):
            lines.append(f"**理由**：{decision['reason']}")
        if decision["action"] == "split" and decision.get("groups"):
            groups = []
            for position, group in enumerate(decision["groups"], 1):
                labels = []
                for member_id in group:
                    found = next(
                        (
                            number
                            for number, member in enumerate(members, 1)
                            if member.get("id") == member_id
                        ),
                        None,
                    )
                    labels.append(f"#{found}" if found else _short_id(member_id))
                groups.append(f"组 {position} = {'、'.join(labels)}")
            lines.append(f"**拆分**：{'；'.join(groups)}")
        if decision["action"] == "merge" and decision.get("merge_with"):
            lines.append(f"**合并到**：{decision['merge_with']}")
        lines.append("")
    if not decisions:
        lines.append("本次没有生成建议。")
    return "\n".join(lines)
