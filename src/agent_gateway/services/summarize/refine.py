"""Merge decisions, generation, and refine pipeline."""

import re
from pathlib import Path

from agent_gateway.core import prompt_loader
from agent_gateway.core.l10n import t
from agent_gateway.core.log_utils import setup_logging as _log_setup
from agent_gateway.services.summarize.summary_llm import (
    _llm_chat,
    _parse_json_any,
    write_error_log,
)

from agent_gateway.services.summarize.constants import (
    EXTRACTS_DIR,
    SEARCH_MIN_TOKEN_LEN,
    SUMMARY_DUP_SIM_THRESHOLD,
    SUMMARY_EXCLUDE_DIRS,
    SUMMARY_FRAGMENT_LINE_MIN,
    SUMMARY_GEN_MAX_TOKENS,
    SUMMARY_JUDGE_MAX_TOKENS,
    SUMMARY_KEYWORD_COUNT,
    SUMMARY_KEYWORD_MAX_TOKENS,
    SUMMARY_MERGE_CONTEXT_CHARS,
    SUMMARY_MERGE_LOOKBACK_CHARS,
    SUMMARY_MIN_CONTENT_LEN,
    SUMMARY_PLACEHOLDER_PATTERNS,
    SUMMARY_SEARCH_LIMIT,
    SUMMARY_TOPIC_JACCARD,
)
from agent_gateway.services.summarize.storage import _existing_extract_tops
from agent_gateway.services.summarize.textutil import (
    _max_segment_similarity,
    _relevant_segments,
)


logger = _log_setup("summarize")


def _llm_json(
    prompt: str,
    *,
    max_tokens: int,
    temperature: float | None = None,
    timeout: float | None = None,
    disable_thinking: bool = False,
    feature: str | None = None,
) -> dict | None:
    """think enabled, output JSON; on parse failure automatically downgrades to disable_thinking and retries once.

    Returns the successfully parsed dict; returns None if both attempts fail (fail-open,
    each caller handles its own fallback). When `feature` is passed, it adds two
    **external signals** -- previously a failure here was completely silent (only
    returning None, with the caller continuing as "no result"), so a bad prompt or a
    model that ignores the format was impossible to detect:
      - both attempts fail to parse -> `write_error_log(PROMPT_JSON_PARSE)`
      - parses but is missing the template-declared `json_keys` -> `write_error_log(PROMPT_JSON_MISS)`,
        **without changing the return value**: a missing key is "the model did not output
        to contract", which is different from a parse failure, and whether to treat it as
        a failure is up to the caller (this only makes it visible).
    """
    for attempt in range(2):
        raw = _llm_chat(
            prompt,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
            disable_thinking=(disable_thinking or attempt > 0),
        )
        if raw:
            parsed = _parse_json_any(raw)
            if parsed:
                if feature:
                    missing = prompt_loader.missing_json_keys(feature, parsed)
                    if missing:
                        write_error_log(
                            f"{feature}: model output is missing template-declared keys {list(missing)}",
                            code="PROMPT_JSON_MISS",
                        )
                return parsed
        if attempt == 0:
            logger.warning(
                "JSON output parse failed, downgrading to disable_thinking and retrying once"
            )
    if feature:
        write_error_log(
            f"{feature}: LLM output failed to parse as JSON on both attempts (abnormal prompt or model output)",
            code="PROMPT_JSON_PARSE",
        )
    return None


def _extract_keywords(group: dict) -> list[str]:
    """Extract retrieval keywords for a group's topic (3-8 noun-like words). Returns [] on failure."""
    facts = str(group.get("facts", ""))[:SUMMARY_MERGE_CONTEXT_CHARS]
    prompt = prompt_loader.prompt(
        "extract_keywords",
        SUMMARY_KEYWORD_COUNT=SUMMARY_KEYWORD_COUNT,
        topic=group.get("topic", ""),
        facts=facts,
        SEARCH_MIN_TOKEN_LEN=SEARCH_MIN_TOKEN_LEN,
    )
    parsed = _llm_json(
        prompt, max_tokens=SUMMARY_KEYWORD_MAX_TOKENS, feature="extract_keywords"
    )
    if not parsed:
        return []
    kws = parsed.get("keywords") or []
    if isinstance(kws, str):
        kws = re.split(r"[、,，]", kws)
    out = []
    for k in kws:
        s = str(k).strip()
        if len(s) >= SEARCH_MIN_TOKEN_LEN:
            out.append(s)
    return out[:SUMMARY_KEYWORD_COUNT]


def _make_snippet(text: str, keyword: str) -> str:
    """Take ~60 characters around the first keyword hit as the retrieval snippet."""
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return text[:120]
    start = max(0, idx - 60)
    end = min(len(text), idx + len(keyword) + 60)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def search_extracts(
    keywords: list[str], limit: int = SUMMARY_SEARCH_LIMIT
) -> list[dict]:
    """Search within EXTRACTS_DIR by keyword substring match (purely local, zero external dependencies).

    The cold-layer vector store != the Extracts full text (the cold layer contains only the
    aggregate entries synced each summarize run), and Qdrant local holds an exclusive lock
    so the standalone summarize process cannot connect directly -- hence retrieval uses
    pure-Python case-insensitive substring matching. Returns the top `limit` entries by
    descending hit-keyword count as [{"rel_path", "snippet", "matched", "score"}].
    """
    if not keywords or not EXTRACTS_DIR.exists():
        return []
    kws = [k for k in keywords if k]
    hits: list[dict] = []
    for f in sorted(EXTRACTS_DIR.rglob("*.md")):
        if any(part.startswith(".") for part in f.relative_to(EXTRACTS_DIR).parts):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low = text.lower()
        matched = [k for k in kws if k.lower() in low]
        if not matched:
            continue
        # P1 hit weighting: keywords hitting the relative path (filename/dir name) + the
        # first section are counted separately as topical hits. Incidental hits in the body
        # (same word but off-topic) are not counted as title_matched and sink in the sort.
        title_feature = (
            str(f.relative_to(EXTRACTS_DIR)) + " " + re.split(r"(?m)^#", text)[0][:200]
        ).lower()
        title_matched = [k for k in matched if k.lower() in title_feature]
        hits.append(
            {
                "rel_path": str(f.relative_to(EXTRACTS_DIR)),
                "snippet": _make_snippet(text, matched[0]),
                "matched": matched,
                "title_matched": title_matched,
                "score": len(matched),
            }
        )
    # Sort: topical hits first, then total number of hit words
    hits.sort(key=lambda h: (len(h["title_matched"]), h["score"]), reverse=True)
    return hits[:limit]


def _dir_excluded(dir_name: str) -> bool:
    """True when any level of the directory path matches an excluded directory (the hand-written area at the notes root)."""
    return any(p in SUMMARY_EXCLUDE_DIRS for p in Path(dir_name).parts)


def _file_topic_feature(rel_path: str) -> str:
    """File topic feature = filename + all H1 headings + head content (before the first #).

    The matching basis for topical-attribution filtering: only the "topic a file declares
    about itself" (filename/headings/opening) counts; body hits do not -- incidental
    keyword appearances in the body are exactly the source of misplaced merges
    (e.g. a SCHEME_PANEL_DEBRIEF keyword hitting the PanelManager text in a waza file's body).
    """
    fp = EXTRACTS_DIR / rel_path
    try:
        text = fp.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return rel_path
    h1s = re.findall(r"(?m)^#\s+(.+)$", text)
    head = re.split(r"(?m)^#", text)[
        0
    ]  # before the first # (file header note / preface)
    return " ".join([rel_path, *h1s, head[:500]])


def _hit_topic_relevant(group: dict, hit: dict, keywords: list[str]) -> bool:
    """Group keywords: >=1 hit on the file topic feature -> allowed as a merge-candidate target.

    Same idea as _topical_related (shared topic words means same family), but the object
    being judged is the "file topic feature" rather than another group's keywords. When
    keyword extraction fails ([]), treat everything as related and do not block.
    """
    kws = [k for k in keywords if k]
    if not kws:
        return True
    feature = _file_topic_feature(hit["rel_path"]).lower()
    return any(k.lower() in feature for k in kws)


def _decide_merge(group: dict, hits: list[dict]) -> dict | None:
    """Decide whether the group should be merged into an existing note or created new.

    No hits returns None (create new directly); with hits returns {"action", "target" (relative to Extracts), "reason"}.
    Judgment JSON failure (still None after downgrade) -> fall back to merge into hits[0] and write a REFINE_JUDGE_JSON log.
    """
    if not hits:
        return None

    facts = str(group.get("facts", ""))[:SUMMARY_MERGE_CONTEXT_CHARS]
    candidates = "\n".join(
        f"- [{i}] {h['rel_path']}：{h['snippet'][:120]}" for i, h in enumerate(hits)
    )
    prompt = prompt_loader.prompt(
        "decide_merge",
        topic=group.get("topic", ""),
        facts=facts,
        candidates=candidates,
    )
    parsed = _llm_json(
        prompt, max_tokens=SUMMARY_JUDGE_MAX_TOKENS, feature="decide_merge"
    )
    if parsed:
        action = str(parsed.get("action", "")).strip().lower()
        if action == "merge":
            target = str(parsed.get("target", "")).strip()
            # Validate using resolved paths (so symlink environments like /tmp->/private/tmp don't make relative_to raise)
            r_extracts = EXTRACTS_DIR.resolve()
            t = (EXTRACTS_DIR / target).resolve()
            if (
                target.endswith(".md")
                and t.is_file()
                and r_extracts in t.parents
                and not _dir_excluded(t.relative_to(r_extracts))
            ):
                return {
                    "action": "merge",
                    "target": target,
                    "reason": str(parsed.get("reason", "")),
                }
            logger.warning(
                f"merge target {target!r} invalid/does not exist, falling back to new"
            )
            return {"action": "new", "target": "", "reason": "judge target invalid"}
        return {"action": "new", "target": "", "reason": str(parsed.get("reason", ""))}

    # Judgment failure (still None after downgrade) -> fall back to merge into the first
    # non-excluded hit, better than creating blindly
    # (defect 2 fix: hits[0] may be in an excluded directory, so skip to the first valid
    # hit; if none, fall back to new)
    fallback = next((h for h in hits if not _dir_excluded(h["rel_path"])), None)
    if fallback is None:
        write_error_log(
            f"merge judgment JSON failed and no valid fallback hit (topic={group.get('topic', '')!r}), falling back to new",
            code="REFINE_JUDGE_JSON",
        )
        return {
            "action": "new",
            "target": "",
            "reason": "judge failed without a valid fallback",
        }
    write_error_log(
        f"merge judgment JSON failed, falling back to merge into {fallback['rel_path']}"
        f" (topic={group.get('topic', '')!r})",
        code="REFINE_JUDGE_JSON",
    )
    return {
        "action": "merge",
        "target": fallback["rel_path"],
        "reason": "judge failed, fallback",
    }


def _gen_new(group: dict, dir_tree: str, existing_tops: set[str]) -> dict | None:
    """think enabled, generate a new article: {"dir", "title", "content"}. Returns None on failure."""
    facts = str(group.get("facts", ""))
    tops_block = (
        t("mcp.summary.list_separator").join(sorted(existing_tops))
        if existing_tops
        else t("mcp.summary.empty_tops")
    )
    prompt = prompt_loader.prompt(
        "gen_new",
        topic=group.get("topic", ""),
        facts=facts,
        dir_tree=dir_tree,
        tops_block=tops_block,
        exclude_dirs=sorted(SUMMARY_EXCLUDE_DIRS),
    )
    parsed = _llm_json(
        prompt, max_tokens=SUMMARY_GEN_MAX_TOKENS, temperature=0.2, feature="gen_new"
    )
    if not parsed:
        return None
    dir_name = str(parsed.get("dir", "")).strip().strip("/")
    title = str(parsed.get("title", "")).strip()
    content = str(parsed.get("content", "")).strip()
    if not dir_name or not title or not content:
        return None
    return {"dir": dir_name, "title": title, "content": content}


def _gen_merge(group: dict, target_file: Path, existing_text: str) -> dict | None:
    """think enabled, generate merge content: writes only new information not already covered by existing content. Returns None on failure.

    no_increment semantic judgment: the LLM compares "relevant facts" with the "target note's
    relevant sections"; when every point is already covered it returns
    {"action":"no_increment",...}, which refine recognizes and skips (added 2026-08-07).
    Only no_increment carries an action field; normal merge does not, avoiding **item's action
    overwriting refine's local action on results.append (fallback_merge polluted into merge).
    Context is taken from _relevant_segments rather than the first N characters -- for a large
    file the first 2K cannot see the existing sections at the end of the file, leaving the LLM
    unable to judge duplication (2026-08-07 root-cause fix).
    """
    facts = str(group.get("facts", ""))
    context = _relevant_segments(existing_text, facts, SUMMARY_MERGE_LOOKBACK_CHARS)
    prompt = prompt_loader.prompt(
        "gen_merge",
        topic=group.get("topic", ""),
        facts=facts,
        target=target_file.relative_to(EXTRACTS_DIR),
        context=context,
    )
    parsed = _llm_json(
        prompt, max_tokens=SUMMARY_GEN_MAX_TOKENS, temperature=0.2, feature="gen_merge"
    )
    if not parsed:
        return None
    if str(parsed.get("action", "")).strip().lower() == "no_increment":
        return {
            "action": "no_increment",
            "reason": str(parsed.get("reason", "")).strip(),
        }
    title = str(parsed.get("title", "")).strip()
    content = str(parsed.get("content", "")).strip()
    if not title or not content:
        return None
    return {
        "title": title,
        "content": content,
        "supersedes": bool(parsed.get("supersedes", False)),
    }


def _topical_related(g1: dict, g2: dict, kw1: list[str], kw2: list[str]) -> bool:
    """Pre-aggregation merge decision: shared topic keywords + Jaccard >= SUMMARY_TOPIC_JACCARD (purely deterministic, 0 LLM).

    Measured calibration (2026-08-06, real 20 unprocessed memories -> 12 groups -> 4 hit targets):
    - same-family groups (e.g. the summarize.py quality-gate scheme / pendulum / 5bd2e21 /
      placeholder list, all hitting the same RSS note): keyword sets share >=1 topic word, Jaccard 0.07-0.15;
    - unrelated groups with the same target (e.g. the RSS daily read-marker convention vs the
      summarize family): 0 shared words, Jaccard=0;
    - whole-text SequenceMatcher ratio / char-bigram Jaccard overlap between within-family
      (0.06-0.17) and cross-family (0.06-0.16) ranges, **no static threshold can separate
      them** -- the old `_max_segment_similarity > 0.6` gate judged every group unrelated
      (measured topic_merged=0, topic_blocked=22, the 5bd2e21 fragment root cause never converged).
    So relevance is decided by "keyword sharing + Jaccard >= 0.05 (>=1 shared topic word)";
    sharing a project/topic word is the same-family signal. The ffe8857 over-merge guard does
    not degrade from loosening this gate: pre-aggregated groups still go through LLM final
    review; and the count gate (a single target allows 1 merge per round) + after pre-aggregation
    groups with the same target are mutually unrelated -> at most 1 merge write per target per
    round, structurally preventing "6 sections at once" pileups.
    """
    set1 = {k.lower() for k in kw1 if k}
    set2 = {k.lower() for k in kw2 if k}
    if not set1 or not set2:
        return False
    inter = set1 & set2
    if not inter:
        return False
    jaccard = len(inter) / len(set1 | set2)
    return jaccard >= SUMMARY_TOPIC_JACCARD


def _resolve_group_targets(groups: list[dict]) -> dict:
    """Pre-resolve each group's "keywords + search hits + top1 valid target", returns {index: {...}}.

    target is the top1 hit passing validity checks (.md / is_file / inside EXTRACTS /
    !_dir_excluded), reused by pre-aggregation and refine, eliminating repeated keyword
    extraction/search inside the loop (0 new LLM).
    Failure/no hits -> {"keywords": [], "hits": [], "target": None}.
    """
    resolved: dict[int, dict] = {}
    for gi, group in enumerate(groups):
        try:
            keywords = _extract_keywords(group)
            if not keywords:
                resolved[gi] = {"keywords": [], "hits": [], "target": None}
                continue
            hits = search_extracts(keywords)
            target = None
            for h in hits:
                rel = h["rel_path"]
                fp = EXTRACTS_DIR / rel
                if (
                    rel.endswith(".md")
                    and fp.is_file()
                    and EXTRACTS_DIR.resolve() in fp.resolve().parents
                    and not _dir_excluded(rel)
                ):
                    target = rel
                    break
            resolved[gi] = {"keywords": keywords, "hits": hits, "target": target}
        except Exception:
            resolved[gi] = {"keywords": [], "hits": [], "target": None}
    return resolved


def _pre_aggregate_same_target(
    groups: list[dict], resolved: dict
) -> tuple[list[dict], dict, dict]:
    """Pre-aggregation: merge groups that hit the same target and are topically related into one group, curing same-family fragmentation.

    Background: the root cause of 5bd2e21 over-splitting was "the 2nd+ group of a same-topic
    family hits the same target and gets forced to new by merge_counts". Here, groups that hit
    the same target and are _topical_related are merged into a single group **before** the count
    decision -> a single merge write -> count never triggers; after pre-aggregation the groups in
    a bucket are mutually unrelated, so count's semantics automatically degrade to "limit 1
    unrelated merge", keeping the ffe8857 guard as-is.

    Merge: facts joined by newline, tags unioned (deduplicated, order preserved), target keeps
    the cache of the group merged in.
    Returns (new_groups, new_resolved, stats); new_resolved index aligns with the new groups.
    stats: {"topic_merged": actual merge count, "topic_blocked": count of same-target unrelated judgments}
    """
    by_target: dict[str, list[int]] = {}
    for gi, info in resolved.items():
        tgt = info.get("target")
        by_target.setdefault(tgt if tgt else "<none>", []).append(gi)

    new_groups: list[dict] = []
    new_resolved: dict[int, dict] = {}
    merged = 0
    blocked = 0
    new_idx = 0

    for tgt, idxs in by_target.items():
        if tgt == "<none>" or len(idxs) == 1:
            for gi in idxs:
                new_groups.append(groups[gi])
                new_resolved[new_idx] = resolved[gi]
                new_idx += 1
            continue
        # Greedy within a bucket: try to fold each group into an already-selected representative (merge if related), otherwise make it its own group
        reps: list[dict] = []
        for gi in idxs:
            placed = False
            for rep in reps:
                if _topical_related(
                    groups[gi],
                    rep["group"],
                    resolved[gi]["keywords"],
                    rep["resolved"]["keywords"],
                ):
                    rep["members"].append(gi)
                    placed = True
                    break
                blocked += 1  # judged unrelated: record one topic_blocked
            if not placed:
                reps.append(
                    {"members": [gi], "group": groups[gi], "resolved": resolved[gi]}
                )
        for rep in reps:
            members = rep["members"]
            merged_group = dict(groups[members[0]])
            if len(members) > 1:
                parts = [
                    str(groups[m].get("facts", "")).strip()
                    for m in members
                    if str(groups[m].get("facts", "")).strip()
                ]
                tags: list[str] = []
                for m in members:
                    tags.extend(groups[m].get("tags", []) or [])
                merged_group["facts"] = "\n\n".join(parts)
                if tags:
                    merged_group["tags"] = list(dict.fromkeys(t for t in tags if t))
                merged_group["_pre_merged_from"] = members
                merged += len(members) - 1
            new_groups.append(merged_group)
            new_resolved[new_idx] = rep["resolved"]
            new_idx += 1

    return new_groups, new_resolved, {"topic_merged": merged, "topic_blocked": blocked}


def _run_refine(groups: list[dict], dir_tree: str) -> tuple[list[dict], dict]:
    """Phase 2: per group "extract keywords -> search Extracts -> decide merge/new -> think enable generate".

    A failure in any step of a single group does not interrupt the whole: write_error_log +
    stats count + skip that group.
    Returns (results, stats); results are the write units (with action: new/merge).
    """
    stats = {
        "ok": 0,
        "kw_fail": 0,
        "gen_fail": 0,
        "too_short": 0,
        "too_low_quality": 0,
        "dup_skip": 0,
        "new": 0,
        "merge": 0,
        "topic_merged": 0,  # pre-aggregation merged group count (same target, same family)
        "topic_blocked": 0,  # count of same-target unrelated judgments (keeps the ffe8857 guard)
        "fallback_merge": 0,  # micro-fragment fallback merge count
        "fallback_dropped": 0,  # micro-fragment fallback failure / no-target count
        "hit_files": set(),
    }
    merge_counts: dict[
        str, int
    ] = {}  # merge count per target file within a single run (limited to 1 topic)
    results: list[dict] = []
    existing_tops = _existing_extract_tops()
    # Pre-resolve all groups' keywords/search/target + same-target same-family pre-aggregation (scheme A+C fusion)
    resolved_all = _resolve_group_targets(groups)
    groups, resolved_all, pre_stats = _pre_aggregate_same_target(groups, resolved_all)
    stats["topic_merged"] = pre_stats["topic_merged"]
    stats["topic_blocked"] = pre_stats["topic_blocked"]
    if pre_stats["topic_merged"]:
        logger.info(
            f"pre-aggregation: merged {pre_stats['topic_merged']} same-target same-family groups, "
            f"{pre_stats['topic_blocked']} same-target unrelated times"
        )
    for gi, group in enumerate(groups):
        topic = str(group.get("topic", "")) or f"group{gi}"
        logger.info(f"Refine [{gi}] {topic} ...")
        try:
            keywords = (resolved_all.get(gi) or {}).get("keywords", [])
            if not keywords:
                stats["kw_fail"] += 1
                write_error_log(
                    f"topic {topic!r} keyword extraction failed, skipping",
                    code="REFINE_KEYWORD",
                )
                continue
            hits = (resolved_all.get(gi) or {}).get("hits", []) or []
            if hits:
                stats["hit_files"].add(hits[0]["rel_path"])

            # P0 topical-attribution filter: only hits whose "keywords hit the file topic
            # feature (filename/H1/first section)" can be merge candidates. search_extracts is
            # a body substring match, and an incidental body hit drags topic-irrelevant files
            # into the candidates, causing misplaced merges (a SCHEME_PANEL_DEBRIEF hit on the
            # PanelManager text in a waza file's body is one such case) -- this layer intercepts
            # deterministically; with no relevant candidate ->
            # _decide_merge returns None -> go to new.
            if keywords and hits:
                cands = [h for h in hits if _hit_topic_relevant(group, h, keywords)]
                dropped = [h["rel_path"] for h in hits if h not in cands]
                if dropped:
                    logger.info(
                        f"topic {topic!r} topical-attribution filter removed {len(dropped)} candidates"
                        f" (body-only hits, not filename/heading/first section): {dropped}"
                    )
                hits = cands

            try:
                decision = _decide_merge(group, hits)
            except Exception as e:
                stats["gen_fail"] += 1
                write_error_log(
                    f"topic {topic!r} merge judgment exception: {e}",
                    code="REFINE_JUDGE",
                )
                continue

            # At most 1 merge into the same target file per run: separate topics are split into their own files (prevents the "6 sections at once" counterexample)
            if (
                decision is not None
                and decision["action"] == "merge"
                and merge_counts.get(decision["target"], 0) >= 1
            ):
                logger.warning(
                    f"topic {topic!r} target {decision['target']} already merged, switching to new this time"
                )
                decision = {
                    "action": "new",
                    "target": "",
                    "reason": "target already merged, one topic per file",
                }

            try:
                if decision is not None and decision["action"] == "merge":
                    target_file = EXTRACTS_DIR / decision["target"]
                    existing_text = (
                        target_file.read_text(encoding="utf-8")
                        if target_file.exists()
                        else ""
                    )
                    item = _gen_merge(group, target_file, existing_text)
                    action = "merge"
                    target_label = decision["target"]
                    if item:
                        # _gen_merge returns {action, title, content, ...}; add target so the write layer can locate it
                        item["target"] = decision["target"]
                        # LLM no-increment judgment (rewrite-style duplication: different
                        # wording but the information is already covered by an existing
                        # section; mechanical similarity thresholds can't catch it -- 2026-08-07 root-cause fix)
                        if item.get("action") == "no_increment":
                            stats["dup_skip"] += 1
                            logger.warning(
                                f"topic {topic!r} judged no increment (LLM): "
                                f"{item.get('reason', '')}"
                            )
                            continue
                        # Pre-write increment judgment: if the generated section's match coverage against some existing section is too high, treat it as no addition and skip writing
                        sim = _max_segment_similarity(
                            item.get("content", ""), existing_text
                        )
                        if sim > SUMMARY_DUP_SIM_THRESHOLD:
                            stats["dup_skip"] += 1
                            logger.warning(
                                f"topic {topic!r} existing-content coverage against {decision['target']}"
                                f" is {sim:.2f}, too high, judged no increment and skipped"
                            )
                            continue
                        # Note: merge_counts used to increment right after dup_skip -- the QC
                        # placeholder/too-short rejections come after it, so "merge content
                        # rejected by QC" still occupied that file's merge slot, falsely
                        # blocking later legitimate merges (measured: after groups [0]/[1] were
                        # placeholder-rejected, group [3] was wrongly blocked). Moved to count
                        # after a successful write (see before results.append), counting only
                        # real persisted merges.
                else:
                    item = _gen_new(group, dir_tree, existing_tops)
                    action = "new"
                    target_label = item["dir"] if item else ""
                    # Micro-fragment fallback (C): a new product <10 lines -> fall back to
                    # merging into the first non-excluded hit. If the merge product passes the
                    # similarity gate, merge normally; with no usable target / merge failure,
                    # keep the new write (prevents information loss).
                    if (
                        item
                        and len(str(item.get("content", "")).splitlines())
                        < SUMMARY_FRAGMENT_LINE_MIN
                    ):
                        fb = next(
                            (
                                h
                                for h in hits  # local hits have passed the P0 topical-attribution filter
                                if not _dir_excluded(h["rel_path"])
                            ),
                            None,
                        )
                        if fb is not None:
                            try:
                                fb_file = EXTRACTS_DIR / fb["rel_path"]
                                fb_text = (
                                    fb_file.read_text(encoding="utf-8")
                                    if fb_file.exists()
                                    else ""
                                )
                                fb_item = _gen_merge(group, fb_file, fb_text)
                                if fb_item:
                                    fb_item["target"] = fb["rel_path"]
                                    if fb_item.get("action") == "no_increment":
                                        stats["dup_skip"] += 1
                                        logger.warning(
                                            f"topic {topic!r} fragment fallback merge judged no increment"
                                            f" (LLM): {fb_item.get('reason', '')}"
                                        )
                                        continue
                                    fb_sim = _max_segment_similarity(
                                        fb_item.get("content", ""), fb_text
                                    )
                                    if fb_sim > SUMMARY_DUP_SIM_THRESHOLD:
                                        stats["dup_skip"] += 1
                                        logger.warning(
                                            f"topic {topic!r} fragment fallback merge into "
                                            f"{fb['rel_path']} coverage {fb_sim:.2f} "
                                            f"no increment, skipping"
                                        )
                                        continue
                                    item = fb_item
                                    action = "fallback_merge"
                                    target_label = fb["rel_path"]
                                    # Counting is accumulated uniformly by stats[action] at the end; do not add again here
                                else:
                                    stats["fallback_dropped"] += 1
                                    logger.warning(
                                        f"topic {topic!r} fragment fallback merge generation failed, keeping new"
                                    )
                            except Exception as e:
                                stats["fallback_dropped"] += 1
                                write_error_log(
                                    f"topic {topic!r} fragment fallback merge exception: {e}",
                                    code="REFINE_FALLBACK",
                                )
                        else:
                            stats["fallback_dropped"] += 1
                            logger.warning(
                                f"topic {topic!r} fragment has no usable fallback target, keeping the new write"
                            )
            except Exception as e:
                stats["gen_fail"] += 1
                write_error_log(
                    f"topic {topic!r} generation exception: {e}", code="REFINE_GEN"
                )
                continue

            if not item:
                stats["gen_fail"] += 1
                write_error_log(
                    f"topic {topic!r} generation failed, skipping", code="REFINE_GEN"
                )
                continue
            content_text = str(item.get("content", ""))
            if len(content_text) < SUMMARY_MIN_CONTENT_LEN:
                stats["too_short"] += 1
                logger.warning(f"topic {topic!r} content too short, skipping")
                continue
            # QC placeholder detection: content containing placeholder wording like 「未详述/待补全」 is treated as low quality and not written
            content_lower = content_text.lower()
            ph = next(
                (p for p in SUMMARY_PLACEHOLDER_PATTERNS if p.lower() in content_lower),
                None,
            )
            if ph:
                stats["too_low_quality"] += 1
                logger.warning(
                    f"topic {topic!r} contains placeholder wording 「{ph}」, judged low quality and skipped"
                )
                continue

            if action == "merge":
                # Only a directly merged, successfully persisted article takes the slot (QC
                # rejections do not); fallback_merge is a fragment fallback and is not subject
                # to the count limit (<10-line micro-fragments cannot form the ffe8857 pileup of many unrelated sections)
                merge_counts[item["target"]] = merge_counts.get(item["target"], 0) + 1

            results.append({"topic": topic, "action": action, **item})
            stats["ok"] += 1
            stats[action] += 1
            logger.info(f"  -> {action} {target_label} ({topic})")
        except Exception as e:
            stats["gen_fail"] += 1
            write_error_log(f"topic {topic!r} refine exception: {e}", code="REFINE_GEN")
            continue
    stats["hit_files"] = sorted(stats["hit_files"])
    return results, stats
