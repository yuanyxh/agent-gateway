"""Batch aggregation of memory facts."""

from agent_gateway.core import prompt_loader
from agent_gateway.core.l10n import t
from agent_gateway.core.log_utils import setup_logging as _log_setup
from agent_gateway.services.summarize.summary_llm import (
    _llm_chat,
    _parse_json_any,
    write_error_log,
)

from agent_gateway.services.summarize.constants import (
    AGGREGATE_BATCH_SIZE,
    AGGREGATE_BATCH_THRESHOLD,
    AGGREGATE_LLM_TEMPERATURE,
    AGGREGATE_LLM_TIMEOUT,
    AGGREGATE_MAX_ROUNDS,
)
from agent_gateway.services.summarize.memory import _build_facts_text_v2


logger = _log_setup("summarize")


def _aggregate_round(
    facts_text: str, label: str
) -> tuple[list[dict] | None, str | None]:
    """Single aggregate round: classify memory facts by topic / merge and dedupe within groups / assess retain, returns (groups, raw).

    label is used in logs to distinguish "batching" from "recursive merge round". When the
    LLM output is invalid (still failing after 1 retry), returns (None, raw); the caller
    decides whether to abort (first round) or keep the current result and continue (recursive round).
    """
    prompt = prompt_loader.prompt(
        "aggregate",
        label=label,
        facts_text=facts_text,
    )
    parsed = None
    raw = None
    for attempt in range(
        2
    ):  # retry once on failure (the LLM occasionally outputs incomplete/non-JSON)
        raw = _llm_chat(
            prompt,
            temperature=AGGREGATE_LLM_TEMPERATURE,
            timeout=AGGREGATE_LLM_TIMEOUT,
            max_tokens=16384,
            disable_thinking=True,
        )
        parsed = _parse_json_any(raw) if raw else None
        if parsed:
            break
        logger.warning(
            f"aggregate{label} attempt {attempt + 1} invalid"
            + (", retrying" if attempt == 0 else ", giving up")
        )
    if not parsed:
        return None, raw
    return parsed.get("groups", []) or [], raw


def _groups_to_facts(groups: list[dict]) -> str:
    """Convert aggregated groups back to fact text for the recursive merge round to aggregate again."""
    return "\n".join(
        f"- [{','.join(g.get('tags', []) or [])}] "
        f"{g.get('topic', '')}：{g.get('facts', '')}"
        for g in groups
    )


def _filter_retain(groups: list[dict]) -> list[dict]:
    """Keep only groups with long-term sedimentation value (retain=true); treat missing as false (better none than a bad one)."""
    retained = []
    for g in groups:
        if g.get("retain") not in (True, "true", "True"):
            logger.info(
                f"aggregate discarded group with no sedimentation value: {g.get('topic', '')!r}"
            )
            continue
        if not str(g.get("facts", "")).strip():
            continue
        retained.append(g)
    return retained


def _run_aggregate_v2(memories: list[dict]) -> tuple[list[dict] | None, int]:
    """Phase 1 v2: batch aggregate -> cross-batch merge -> retain filter -> recursive merge until convergence.

    Returns (groups, rounds): groups being None only means a fatal failure in the first
    round (abort); rounds is the number of recursive rounds actually executed (not
    counting the first batching). Recursion only merges and never splits, so the group
    count is monotonically non-increasing and it must converge within
    AGGREGATE_MAX_ROUNDS; a failed recursive round keeps the current result and
    continues (non-fatal).
    """
    batches: list[list[dict]] = []
    if len(memories) > AGGREGATE_BATCH_THRESHOLD:
        for i in range(0, len(memories), AGGREGATE_BATCH_SIZE):
            batches.append(memories[i : i + AGGREGATE_BATCH_SIZE])
    else:
        batches = [memories]

    groups: list[dict] = []
    for bi, batch in enumerate(batches):
        facts_text = _build_facts_text_v2(batch)
        batch_label = (
            t("mcp.summary.batch_label", index=bi + 1, total=len(batches))
            if len(batches) > 1
            else ""
        )
        batch_groups, raw = _aggregate_round(facts_text, batch_label)
        if batch_groups is None:
            write_error_log(
                f"aggregate batch {bi + 1}/{len(batches)} LLM output invalid (still failing after retry):\n{(raw or '')[:1000]}",
                code="AGGREGATE_JSON",
            )
            return None, 0
        groups.extend(batch_groups)

    if not groups:
        print("aggregate produced no valid topics, skipping summary")
        return [], 0

    # When batch-aggregating, do a second cross-batch merge: groups with the same/similar topic converge into one
    if len(batches) > 1:
        groups = _merge_cross_batch_groups(groups)

    # Recursive merge: converges when the group count stops decreasing; at most AGGREGATE_MAX_ROUNDS rounds
    rounds = 0
    for rnd in range(1, AGGREGATE_MAX_ROUNDS + 1):
        prev_len = len(groups)
        round_groups, _raw = _aggregate_round(
            _groups_to_facts(groups), t("mcp.summary.round_label", round=rnd)
        )
        if round_groups is None:
            logger.warning(
                f"recursive merge round {rnd} failed, keeping current grouping and continuing"
            )
            break
        groups = round_groups
        rounds = rnd
        logger.info(
            f"aggregate converged: round {rnd} {prev_len} -> {len(groups)} groups"
        )
        if len(groups) >= prev_len:
            break

    return _filter_retain(groups), rounds


def _merge_cross_batch_groups(groups: list[dict]) -> list[dict]:
    """Second cross-batch merge: converge groups with the same/similar topic into one (called when batch-aggregating)."""
    entries = "\n".join(
        f"- [{i}] {g.get('topic', '')}：{g.get('facts', '')[:150]}"
        for i, g in enumerate(groups)
    )
    prompt = prompt_loader.prompt(
        "merge_cross_batch",
        entries=entries,
    )
    raw = _llm_chat(
        prompt,
        temperature=AGGREGATE_LLM_TEMPERATURE,
        timeout=AGGREGATE_LLM_TIMEOUT,
        max_tokens=16384,
        disable_thinking=True,
    )
    if not raw:
        logger.warning(
            "cross-batch second merge LLM call failed, keeping original grouping"
        )
        return groups
    parsed = _parse_json_any(raw)
    if not parsed or not parsed.get("groups"):
        logger.warning(
            "cross-batch second merge parse failed, keeping original grouping"
        )
        return groups
    return parsed["groups"]
