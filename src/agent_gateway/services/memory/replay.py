"""Offline sequence replay: sample seeds, walk paths, reinforce what holds."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta

from agent_gateway.core import config, prompt_loader
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.llm import client as llm_client
from agent_gateway.services.memory import graph_index, store

logger = setup_logging("server")

DEFAULT_CALLS = 40
DEFAULT_SECONDS = 1200
WIDTH = 5
HOPS = 4
REINFORCE_DELTA = 0.05


def sample_seeds(
    *,
    limit: int = DEFAULT_CALLS,
    db_path: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    now = now or datetime.now()
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT n.neuron_id, n.importance, COALESCE(MAX(a.strength), 0.0), "
            "COALESCE(MAX(a.last_activated_at), n.created_at) "
            "FROM neurons n "
            "LEFT JOIN neuron_members m ON m.neuron_id = n.neuron_id "
            "LEFT JOIN memory_activation a ON a.node_id = m.memory_id "
            "WHERE n.status='active' GROUP BY n.neuron_id "
            "ORDER BY n.importance DESC LIMIT 500"
        ).fetchall()
    scored = []
    for neuron_id, importance, strength, last_at in rows:
        novelty = 1.0 - min(1.0, float(strength or 0.0))
        weight = (1.0 + float(importance or 0.0)) * (0.2 + novelty)
        scored.append((weight, str(neuron_id)))
    scored.sort(reverse=True)
    return [
        {"neuron_id": neuron_id, "score": round(weight, 4)}
        for weight, neuron_id in scored[: max(1, limit)]
    ]


def walk_sequences(
    index: graph_index.GraphIndex,
    seed: str,
    *,
    width: int = WIDTH,
    hops: int = HOPS,
) -> list[list[str]]:
    result = index.expand(seed, hops=hops, limit=width)
    sequences = [[seed, item["neuron_id"]] for item in result["neurons"]]
    for item in result["neurons"]:
        path = item.get("path") or []
        if len(path) > 2:
            sequences.append(path)
    return sequences[:width]


def propose_inference(path: list[str]) -> dict | None:
    steps = [str(step) for step in path if step]
    if len(steps) < 3 or steps[0] == steps[-1]:
        return None
    left, right = sorted((steps[0], steps[-1]))
    return {
        "left": left,
        "right": right,
        "reason": f"co_activated:{len(steps) - 1}hops",
    }


def parse_replay(raw: str) -> dict:
    parsed = llm_client.parse_json(raw)
    if not isinstance(parsed, dict):
        return {"reinforce": [], "parse_failed": True}
    reinforce = [
        [str(step) for step in path]
        for path in parsed.get("reinforce") or []
        if isinstance(path, list) and len(path) >= 2
    ]
    return {"reinforce": reinforce, "parse_failed": False}


def _title(connection, neuron_id: str) -> str:
    row = connection.execute(
        "SELECT title, summary FROM neurons WHERE neuron_id=?", (neuron_id,)
    ).fetchone()
    return str((row[0] if row else "") or (row[1] if row else "") or neuron_id)


def calls_today(db_path: str | None = None, *, today: datetime | None = None) -> int:
    day = (today or datetime.now()).date().isoformat()
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT metadata FROM replay_runs WHERE substr(started_at, 1, 10)=?",
            (day,),
        ).fetchall()
    total = 0
    for (raw,) in rows:
        try:
            total += int((json.loads(raw or "{}") or {}).get("calls") or 0)
        except (TypeError, ValueError):
            continue
    return total


async def run_replay(
    *,
    db_path: str | None = None,
    budget_calls: int = DEFAULT_CALLS,
    budget_seconds: float = DEFAULT_SECONDS,
    now: datetime | None = None,
) -> dict:
    if not config.memory_brain_replay():
        return {"sampled": 0, "reinforced": 0, "stopped": "disabled"}
    if not llm_client.is_configured():
        return {"sampled": 0, "reinforced": 0, "stopped": "no_llm"}
    spent = calls_today(db_path)
    remaining = max(0, int(budget_calls) - spent)
    if remaining <= 0:
        return {
            "sampled": 0,
            "reinforced": 0,
            "inferred": 0,
            "stopped": "daily_budget",
            "spent": spent,
        }
    started = time.monotonic()
    run_id = f"rp_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    seeds = sample_seeds(limit=max(10, remaining * 2), db_path=db_path)
    index = graph_index.current(db_path)
    calls = 0
    reinforced = 0
    inferred = 0
    failed = 0
    stopped = "done"
    with store.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO replay_runs (run_id, kind, started_at, sampled) "
            "VALUES (?, 'daily', ?, ?)",
            (run_id, datetime.now().isoformat(), len(seeds)),
        )
    expired = 0
    try:
        for seed in seeds:
            if calls >= remaining:
                stopped = "budget"
                break
            if time.monotonic() - started > budget_seconds:
                stopped = "time"
                break
            sequences = walk_sequences(index, seed["neuron_id"])
            if not sequences:
                continue
            with store.connect(db_path) as connection:
                lines = [
                    " -> ".join(f"{step}|{_title(connection, step)}" for step in path)
                    for path in sequences
                ]
            prompt = prompt_loader.prompt(
                "replay_sequence",
                current=_title_connection(db_path, seed["neuron_id"]),
                sequences_text="\n".join(f"- {line}" for line in lines),
            )
            calls += 1
            try:
                raw = await llm_client.achat(
                    prompt,
                    temperature=0.1,
                    timeout=90.0,
                    disable_thinking=True,
                    label="Sequence replay failed",
                )
            except Exception as error:  # noqa: BLE001 - one seed must not stop replay
                logger.warning(f"[replay] {seed['neuron_id']} failed: {error}")
                failed += 1
                continue
            parsed = parse_replay(str(raw or ""))
            if parsed["parse_failed"]:
                failed += 1
                continue
            for path in parsed["reinforce"]:
                reinforced += _reinforce_path(path, db_path=db_path)
                proposal = propose_inference(path)
                if proposal is None:
                    continue
                action = write_inference(
                    proposal["left"],
                    proposal["right"],
                    run_id=run_id,
                    reason=proposal["reason"],
                    db_path=db_path,
                )
                if action == "inserted":
                    inferred += 1
        expired = expire_inferences(db_path=db_path)
    finally:
        with store.connect(db_path) as connection:
            connection.execute(
                "UPDATE replay_runs SET ended_at=?, reinforced=?, metadata=? "
                "WHERE run_id=?",
                (
                    (now or datetime.now()).isoformat(),
                    reinforced,
                    f'{{"calls": {calls}, "failed": {failed}, "stopped": "{stopped}", '
                    f'"inferred": {inferred}, "expired": {expired}}}',
                    run_id,
                ),
            )
    return {
        "run_id": run_id,
        "sampled": len(seeds),
        "calls": calls,
        "reinforced": reinforced,
        "inferred": inferred,
        "expired": expired,
        "failed": failed,
        "stopped": stopped,
    }


def _title_connection(db_path: str | None, neuron_id: str) -> str:
    with store.connect(db_path) as connection:
        return _title(connection, neuron_id)


INFER_WEIGHT = 0.35
INFER_ACCEPT_WEIGHT = 0.6
INFER_DAYS = 14


def write_inference(
    left: str,
    right: str,
    *,
    run_id: str,
    reason: str = "",
    db_path: str | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    pair = sorted((left, right))
    if pair[0] == pair[1]:
        return "skipped"
    with store.connect(db_path) as connection:
        row = connection.execute(
            "SELECT synapse_id FROM synapses WHERE from_neuron=? AND to_neuron=?",
            pair,
        ).fetchone()
        if row is not None:
            return "exists"
        connection.execute(
            "INSERT INTO synapses (from_neuron, to_neuron, weight, base_weight, "
            "relation, kind, inferred, status, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, 'inferred', 'inferred', 1, 'active', ?, ?, ?)",
            (
                pair[0],
                pair[1],
                INFER_WEIGHT,
                INFER_WEIGHT,
                now.isoformat(),
                now.isoformat(),
                f'{{"run_id": "{run_id}", "reason": "{reason}"}}',
            ),
        )
    return "inserted"


def expire_inferences(
    *, days: int = INFER_DAYS, db_path: str | None = None, now: datetime | None = None
) -> int:
    cutoff = ((now or datetime.now()) - timedelta(days=max(1, int(days)))).isoformat()
    with store.connect(db_path) as connection:
        return connection.execute(
            "UPDATE synapses SET status='archived', updated_at=? "
            "WHERE inferred=1 AND status='active' AND created_at < ?",
            (datetime.now().isoformat(), cutoff),
        ).rowcount


def decide_inference(
    synapse_id: int, *, accept: bool, db_path: str | None = None
) -> bool:
    with store.connect(db_path) as connection:
        if accept:
            cursor = connection.execute(
                "UPDATE synapses SET inferred=0, kind='assoc', weight=?, updated_at=? "
                "WHERE synapse_id=?",
                (
                    INFER_ACCEPT_WEIGHT,
                    datetime.now().isoformat(),
                    int(synapse_id),
                ),
            )
        else:
            cursor = connection.execute(
                "UPDATE synapses SET status='archived', updated_at=? WHERE synapse_id=?",
                (datetime.now().isoformat(), int(synapse_id)),
            )
    return cursor.rowcount > 0


def _reinforce_path(path: list[str], *, db_path: str | None) -> int:
    updated = 0
    for left, right in zip(path, path[1:]):
        pair = sorted((left, right))
        with store.connect(db_path) as connection:
            row = connection.execute(
                "SELECT synapse_id FROM synapses WHERE from_neuron=? AND to_neuron=?",
                (pair[0], pair[1]),
            ).fetchone()
        if row is None:
            continue
        from agent_gateway.services.memory import strength

        strength.reinforce_synapse(int(row[0]), delta=REINFORCE_DELTA, db_path=db_path)
        updated += 1
    return updated
