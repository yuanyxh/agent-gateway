"""Terminal report module for summarize.

Split out of summarize.py (2026-08-30, refactor phase 3): terminal report printing
for the memory-summary pipeline. Labels come from the shared catalog.
"""

from __future__ import annotations

from agent_gateway.core.l10n import t

COLD_READY = True

_ACTION_KEYS = {
    "new": "mcp.summary.action_new",
    "append": "mcp.summary.action_append",
}


def _print_report_v2(
    today_str: str,
    memory_count: int,
    updated_files: dict[str, str],
    rules_updates: str = "",
    refine_stats: dict | None = None,
    agg_rounds: int = 0,
    skipped_processed: int = 0,
    skipped_quality: int = 0,
):
    print(t("mcp.summary.report_title", date=today_str))
    print(t("mcp.summary.report_processed", count=memory_count))
    if skipped_processed or skipped_quality:
        print(
            t(
                "mcp.summary.report_filtered",
                processed=skipped_processed,
                quality=skipped_quality,
            )
        )
    if refine_stats:
        print(
            t(
                "mcp.summary.report_refine",
                ok=refine_stats["ok"],
                new=refine_stats["new"],
                merge=refine_stats["merge"],
                kw_fail=refine_stats["kw_fail"],
                gen_fail=refine_stats["gen_fail"],
                too_short=refine_stats["too_short"],
                low_quality=refine_stats.get("too_low_quality", 0),
                dup_skip=refine_stats.get("dup_skip", 0),
                hits=len(refine_stats["hit_files"]),
            )
        )
    if agg_rounds:
        print(t("mcp.summary.report_rounds", rounds=agg_rounds))
    print(t("mcp.summary.report_files", count=len(updated_files)))
    for path, action in updated_files.items():
        icon = "📄" if action == "new" else "📎"
        print(
            t(
                "mcp.summary.report_file_line",
                icon=icon,
                path=path,
                action=t(_ACTION_KEYS.get(action, "mcp.summary.action_new")),
            )
        )
    if rules_updates:
        print(t("mcp.summary.report_rules", files=rules_updates))
    print(
        t("mcp.summary.report_cold_on" if COLD_READY else "mcp.summary.report_cold_off")
    )
