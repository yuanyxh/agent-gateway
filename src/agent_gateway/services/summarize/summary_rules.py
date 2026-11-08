"""summarize phase 3 rules-extraction module.

Split out of summarize.py (2026-08-30, refactor phase 3): extraction, trimming, and
rendering of rule/convention memories -> MCP/Rules/*.md. Code was moved as-is, logic
unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_gateway.core import prompt_loader
from agent_gateway.core.l10n import all_templates, t
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.summarize.constants import (
    SUMMARY_RULE_SUMMARY_MAX_CHARS,
)
from agent_gateway.services.summarize.summary_llm import (
    NOTES_DIR,
    SUMMARY_LLM_TIMEOUT,
    _llm_chat,
    _parse_json_any,
    write_error_log,
)
from agent_gateway.services.summarize.textutil import budget_text

# ---------------------------------------------------------------------------
# Configuration (same as summarize.py before the split)
# ---------------------------------------------------------------------------

MCP_DIR = NOTES_DIR / "MCP"
SKILLS_RULES_DIR = MCP_DIR / "Rules"
RULES_MIN_CANDIDATES = 3  # Skip extraction when this cycle's valid rule/convention memories are fewer than this value
RULES_MAX_PER_FILE = (
    60  # Max entries per rules file; over the limit they are pruned by the LLM
)
RULES_LLM_TEMPERATURE = (
    0.2  # Extraction allows generalization, consistent with aggregation
)
#: LLM timeout for rules extraction. Taken from the same source (config `ai.llmTimeout`) --
#: shared with single summary and batch aggregation, unlike the old assumption that "this
#: stage's prompt is bigger so it needs longer": in practice all three hit the same upstream timeout.
RULES_LLM_TIMEOUT = SUMMARY_LLM_TIMEOUT
# Multi-level rules: global + per-project-type; anything the LLM can't classify falls into global.
# Level ids are stable; their display labels come from the catalog.
_LEVEL_KEYS = {
    "global": "mcp.rules.level.global",
    "web_frontend": "mcp.rules.level.web_frontend",
    "mobile": "mcp.rules.level.mobile",
    "macos_tool": "mcp.rules.level.macos_tool",
    "backend": "mcp.rules.level.backend",
}
RULES_LEVELS = tuple(_LEVEL_KEYS)

logger = setup_logging("summarize")


def _level_label(level: str) -> str:
    return t(_LEVEL_KEYS[level])


def _field_prefixes(key: str) -> tuple[str, ...]:
    """Leading labels of a field template across all languages (`- 领域:` / `- Domain:`)."""
    prefixes = []
    for template in all_templates(key).values():
        prefix = template.split("{value}")[0].strip()
        if prefix:
            prefixes.append(prefix)
    return tuple(dict.fromkeys(prefixes))


_FIELD_PREFIXES = {
    field: _field_prefixes(f"mcp.rules.field_{field}")
    for field in ("domain", "norm", "source", "date", "keywords")
}


def _match_field(line: str) -> tuple[str, str] | None:
    """Match an entry line against any language's field label."""
    for field, prefixes in _FIELD_PREFIXES.items():
        for prefix in prefixes:
            if line.startswith(prefix + " "):
                return field, line[len(prefix) + 1 :].strip()
    return None


def _parse_rules_entries(text: str) -> list[dict]:
    """Parse the entry list from the body of a rules file."""
    entries = []
    cur = None
    for line in text.splitlines():
        if line.startswith("### "):
            if cur:
                entries.append(cur)
            cur = {
                "title": line[4:].strip(),
                "domain": "",
                "norm": "",
                "source": "",
                "date": "",
                "keywords": [],
            }
        elif cur and (matched := _match_field(line)):
            field, value = matched
            if field == "keywords":
                cur["keywords"] = [
                    k.strip() for k in re.split(r"[、,，]", value) if k.strip()
                ]
            else:
                cur[field] = value
    if cur:
        entries.append(cur)
    return entries


def _render_rules_file(level: str, entries: list[dict]) -> str:
    """Render the full content of a rules file (frontmatter + entry section).

    `description` tailors the trigger wording to the level for readers of the file:
    - global: broad trigger spanning multiple domains
    - project type: triggered when starting development on that type of project
    Keywords stay in the body entries as a scope note, not in `description`.
    """
    label = _level_label(level)
    if level == "global":
        description = t("mcp.rules.global_description", count=len(entries))
    else:
        description = t("mcp.rules.level_description", label=label, count=len(entries))
    lines = [
        "---",
        f"name: norms_{level}",
        f"description: {description}",
        "---",
        "",
        t("mcp.rules.file_title", label=label),
        "",
        t("mcp.rules.header_note", limit=RULES_MAX_PER_FILE),
        "",
        t("mcp.rules.section_entries"),
        "",
    ]
    for e in entries:
        lines.append(f"### {e['title']}")
        lines.append(t("mcp.rules.field_domain", value=e.get("domain", "")))
        lines.append(t("mcp.rules.field_norm", value=e.get("norm", "")))
        if e.get("keywords"):
            lines.append(
                t(
                    "mcp.rules.field_keywords",
                    value=t("mcp.rules.keywords_separator").join(e["keywords"]),
                )
            )
        lines.append(t("mcp.rules.field_source", value=e.get("source", "")))
        lines.append(t("mcp.rules.field_date", value=e.get("date", "")))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _load_rules_entries(level: str) -> tuple[list[dict], Path]:
    """Read the existing entries of the given-level rules file; returns an empty list and the target path when the file does not exist."""
    path = SKILLS_RULES_DIR / f"norms_{level}.md"
    if not path.exists():
        return [], path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"Failed to read rules file {path}: {e}")
        return [], path
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return _parse_rules_entries(text), path


def _trim_rules_entries(level: str, entries: list[dict]) -> list[dict]:
    """When over capacity, let the LLM prune the least reusable entries, keeping no more than the limit."""
    if len(entries) <= RULES_MAX_PER_FILE:
        return entries
    separator = t("mcp.rules.topic_separator")
    item_list = "\n".join(
        t(
            "mcp.rules.item_line",
            index=i,
            title=e["title"],
            sep=separator,
            summary=budget_text(
                str(e.get("norm", "")),
                SUMMARY_RULE_SUMMARY_MAX_CHARS,
                "rule summary",
            ),
        )
        for i, e in enumerate(entries)
    )
    prompt = prompt_loader.prompt(
        "prune_rules",
        level=_level_label(level),
        limit=str(RULES_MAX_PER_FILE),
        items=item_list,
    )
    raw = _llm_chat(
        prompt,
        temperature=0.1,
        timeout=RULES_LLM_TIMEOUT,
        max_tokens=16384,
        disable_thinking=True,
    )
    if not raw:
        logger.warning(
            f"Rules-file pruning LLM call failed, keeping the first {RULES_MAX_PER_FILE} entries"
        )
        return entries[:RULES_MAX_PER_FILE]
    parsed = _parse_json_any(raw)
    keep = parsed.get("keep_indexes", []) if parsed else []
    keep = [int(i) for i in keep if str(i).strip().isdigit()]
    kept = [entries[i] for i in keep if 0 <= i < len(entries)]
    if not kept:
        logger.warning("Pruning result is empty, falling back to the first N entries")
        return entries[:RULES_MAX_PER_FILE]
    return kept[:RULES_MAX_PER_FILE]


def _run_phase3(groups: list[dict], today_str: str) -> dict:
    """Phase 3: extract "rule/convention" specs from this cycle's aggregated memories, writing them to MCP/Rules/*.md.

    Only extract reusable, stable, actionable knowledge; incidental bugs, one-off
    fixes, and version-bound workarounds are not extracted. Skipped when this
    cycle's valid candidates < RULES_MIN_CANDIDATES.
    """
    if not groups:
        return {"skipped": "no_groups"}

    separator = t("mcp.rules.topic_separator")
    facts_text = "\n".join(
        f"- {g.get('topic', '')}{separator}{g.get('facts', '')}" for g in groups
    )

    # Existing title lists, so the LLM can avoid duplicates during extraction
    existing_titles = {}
    for level in RULES_LEVELS:
        entries, _ = _load_rules_entries(level)
        existing_titles[level] = [e["title"] for e in entries]
    empty = t("mcp.rules.empty_list")
    titles_block = "\n".join(
        f"[{level}] {'; '.join(titles)}" if titles else f"[{level}] {empty}"
        for level, titles in existing_titles.items()
    )
    level_labels = "\n".join(
        f"- {level}: {_level_label(level)}" for level in RULES_LEVELS
    )

    prompt = prompt_loader.prompt(
        "extract_rules",
        facts_text=facts_text,
        level_labels=level_labels,
        titles_block=titles_block,
    )
    raw = _llm_chat(
        prompt,
        temperature=RULES_LLM_TEMPERATURE,
        timeout=RULES_LLM_TIMEOUT,
        max_tokens=16384,
        disable_thinking=True,
    )
    if not raw:
        write_error_log("Phase 3 rules extraction LLM call failed", code="RULES_LLM")
        return {"skipped": "llm_failed"}
    parsed = _parse_json_any(raw)
    if not parsed:
        write_error_log(
            f"Phase 3 rules extraction JSON parse error:\n{raw[:1000]}",
            code="RULES_JSON",
        )
        return {"skipped": "json_failed"}

    norms = parsed.get("norms", []) or []
    # The gate is based on the norms actually extracted — one topic can yield several
    # norms, so the LLM's self-assessed topic count would not be a usable basis.
    if not norms:
        logger.info("Phase 3 skipped: no extracted rules entries")
        return {"skipped": "no_norms"}
    if len(norms) < RULES_MIN_CANDIDATES:
        logger.info(
            f"Phase 3 skipped: extracted rules {len(norms)} < {RULES_MIN_CANDIDATES}"
        )
        return {"skipped": "norms_lt_min", "count": len(norms)}

    updated = {}
    for norm in norms:
        title = str(norm.get("title", "")).strip()
        level = str(norm.get("level", "global")).strip()
        summary = str(norm.get("summary", "")).strip()
        if not title or not summary:
            continue
        if level not in RULES_LEVELS:
            level = "global"
        entries, path = _load_rules_entries(level)
        if any(e["title"] == title for e in entries):
            logger.info(f"Phase 3 skipping duplicate rule: {title}")
            continue
        # keywords is compatible with both forms: the LLM may return a string
        # (comma-separated) or an array
        kws = norm.get("keywords") or []
        if isinstance(kws, str):
            kws = re.split(r"[、,，]", kws)
        keywords = [k.strip() for k in kws if str(k).strip()]
        entries.append(
            {
                "title": title,
                "domain": _level_label(level),
                "norm": summary,
                "keywords": keywords,
                "source": str(norm.get("source", "")).strip(),
                "date": today_str,
            }
        )
        entries = _trim_rules_entries(level, entries)
        SKILLS_RULES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_rules_file(level, entries), encoding="utf-8")
        updated[path.name] = updated.get(path.name, 0) + 1

    logger.info(f"Phase 3 done: {updated}")
    # Return an empty string when nothing was actually written; main() uses this to
    # skip git_commit and avoid an empty commit
    return {"updated": ", ".join(f"{k}+{v}" for k, v in updated.items())}
