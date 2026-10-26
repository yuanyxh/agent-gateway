"""RSS daily digest: fetch, filter, summarize, and archive.

Runs as an internal TaskScheduler job (or manually from the menu bar).
`RSS_DAILY_CONFIG` carries `rss.daily` as JSON; AI credentials arrive through
the standard `AI_*` env vars injected by Swift.

Pipeline: fetch -> unread -> history dedup -> LLM pre-filter -> body fetch ->
LLM summaries -> deterministic Markdown render -> write -> mark read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

from agent_gateway.core import l10n
from agent_gateway.core.config import rss_daily_config
from agent_gateway.llm import client as llm_client
from agent_gateway.services.rss import rss_article
from agent_gateway.services.rss import rss_render
from agent_gateway.services.search import web_search
from agent_gateway.services.rss.clash_proxy import ClashSwitcher
from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("rss_daily")

DEFAULT_CONFIG = {
    "outputDir": "",
    "markRead": True,
    "maxConcurrency": 4,
    "articleMaxChars": 12000,
    "filterBatch": 40,
    "summaryBatch": 4,
    "maxDigestLinks": 10,
    "fetchTimeout": 20,
    "llmTimeout": 120,
    "clash": {"enabled": False, "group": "", "target": ""},
}


# ---------------------------------------------------------------------------
# Settings and storage
# ---------------------------------------------------------------------------


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = rss_daily_config().strip()
    if not raw:
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("invalid RSS_DAILY_CONFIG, using defaults")
        return cfg
    if not isinstance(data, dict):
        return cfg
    for key in DEFAULT_CONFIG:
        if key in data and data[key] is not None:
            cfg[key] = data[key]
    clash = data.get("clash")
    if isinstance(clash, dict):
        for key in DEFAULT_CONFIG["clash"]:
            if key in clash and clash[key] is not None:
                cfg["clash"][key] = clash[key]
    return cfg


def _db_path() -> Path:
    base = os.environ.get("UTILITY_DATA_DIR", "").strip()
    return Path(base or (Path.home() / ".config/utility")).expanduser() / "data.sqlite"


def output_root(cfg: dict) -> Path:
    configured = (cfg.get("outputDir") or "").strip()
    if configured:
        return Path(configured).expanduser()
    notes = os.environ.get("NOTES_DIR", "").strip()
    base = (
        Path(notes).expanduser()
        if notes
        else Path.home() / ".config" / "utility" / "notes"
    )
    return base / "RSS"


def load_unread() -> list[dict]:
    db = _db_path()
    if not db.is_file():
        raise RuntimeError(f"database not found: {db}")
    conn = sqlite3.connect(str(db), timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            "SELECT id, title, link, item_description, feed_name FROM rss_item "
            "WHERE is_read = 0 ORDER BY pub_date DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row[0],
            "title": (row[1] or "").strip(),
            "link": (row[2] or "").strip(),
            "item_description": row[3] or "",
            "feed_name": (row[4] or "").strip(),
        }
        for row in rows
    ]


def mark_read(ids: list[str]) -> None:
    if not ids:
        return
    conn = sqlite3.connect(str(_db_path()), timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executemany(
            "UPDATE rss_item SET is_read = 1 WHERE id = ?", [(i,) for i in ids]
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _cli_path() -> str:
    configured = os.environ.get("UTILITY_CLI_PATH", "").strip()
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("utility")
    if found:
        return found
    raise RuntimeError("utility CLI not found (UTILITY_CLI_PATH unset)")


def fetch_feeds() -> bool:
    try:
        proc = subprocess.run(
            [_cli_path(), "rss", "--fetch"],
            capture_output=True,
            check=False,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("rss --fetch failed: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "rss --fetch exited %s: %s",
            proc.returncode,
            (proc.stderr or "").strip()[:500],
        )
        return False
    logger.info("%s", (proc.stdout or "").strip())
    return True


# ---------------------------------------------------------------------------
# LLM steps
# ---------------------------------------------------------------------------


def _plain(html: str, limit: int) -> str:
    text = web_search.html_to_text(html or "", max_chars=limit)
    return re.sub(r"\s+", " ", text).strip()


def _to_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def decide(items: list[dict], cfg: dict) -> dict[int, dict]:
    """Pre-filter on title/feed/description only; failures default to keep."""
    decisions: dict[int, dict] = {}
    batch = max(1, int(cfg["filterBatch"]))
    for start in range(0, len(items), batch):
        chunk = items[start : start + batch]
        lines = [
            f"[{i}] {item['title']} | {item['feed_name']} | {_plain(item['item_description'], 300)}"
            for i, item in enumerate(chunk)
        ]
        parsed = rss_article.chat_json(
            "rss_filter",
            {"items_text": "\n".join(lines)},
            label="rss_filter",
            llm_timeout=int(cfg["llmTimeout"]),
            max_tokens=min(4000, max(800, len(chunk) * 120)),
            attempts=2,
        )
        if not parsed:
            logger.warning(
                "filter batch %s..%s failed, keep all", start, start + len(chunk)
            )
            continue
        for entry in parsed.get("items") or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(chunk):
                continue
            kind = entry.get("kind") or "article"
            if kind not in ("article", "digest", "skip"):
                kind = "article"
            decisions[start + index] = {
                "keep": _to_bool(entry.get("keep"), default=kind != "skip"),
                "kind": "digest" if kind == "digest" else "article",
                "reason": (entry.get("reason") or "").strip(),
            }
    return decisions


def summarize(entries: list[dict], cfg: dict) -> dict[str, dict]:
    results: dict[str, dict] = {}
    batch = max(1, int(cfg["summaryBatch"]))
    for start in range(0, len(entries), batch):
        chunk = entries[start : start + batch]
        blocks = [
            f"[{i}] 标题：{entry['item']['title']}\n"
            f"来源：{entry['item']['feed_name']}\n"
            f"正文：\n{entry['text'][:6000]}"
            for i, entry in enumerate(chunk)
        ]
        parsed = rss_article.chat_json(
            "rss_summary",
            {"items_text": "\n\n".join(blocks)},
            label="rss_summary",
            llm_timeout=int(cfg["llmTimeout"]),
            max_tokens=min(3000, max(1000, len(chunk) * 400)),
            attempts=2,
        )
        if not parsed:
            logger.warning("summary batch %s..%s failed", start, start + len(chunk))
            continue
        for entry in parsed.get("items") or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(chunk):
                continue
            results[chunk[index]["item"]["link"]] = entry
    return results


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _filtered(item: dict, reason: str) -> dict:
    return {
        "title": item.get("title", ""),
        "url": item.get("link", ""),
        "feed": item.get("feed_name", ""),
        "reason": reason,
    }


def run(dry_run: bool, day: str) -> int:
    cfg = load_config()
    configured = llm_client.is_configured()
    if not configured and not dry_run:
        logger.error("LLM is not configured (AI_API_KEY missing)")
        print("rss daily: LLM is not configured", file=sys.stderr)
        return 1

    root = output_root(cfg)
    if not dry_run:
        fetch_feeds()

    items = load_unread()
    archive = rss_render.archive_path(root, day)
    history = rss_render.collect_history_urls(root, exclude=archive)

    filtered: list[dict] = []
    candidates: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not item["link"]:
            filtered.append(_filtered(item, l10n.t("rss.filter_no_link")))
            continue
        key = rss_render.normalize_url(item["link"])
        if key in history:
            filtered.append(_filtered(item, l10n.t("rss.filter_historical")))
            continue
        if key in seen:
            filtered.append(_filtered(item, l10n.t("rss.filter_duplicate")))
            continue
        seen.add(key)
        candidates.append(item)
    logger.info(
        "unread=%s candidates=%s prefiltered=%s",
        len(items),
        len(candidates),
        len(filtered),
    )

    decisions = decide(candidates, cfg) if configured else {}
    selected: list[dict] = []
    for index, item in enumerate(candidates):
        decision = decisions.get(index, {"keep": True, "kind": "article", "reason": ""})
        if decision["keep"]:
            selected.append({"item": item, "kind": decision["kind"]})
        else:
            filtered.append(
                _filtered(
                    item, decision["reason"] or l10n.t("rss.filter_no_long_term_value")
                )
            )

    if dry_run:
        digest_count = sum(1 for entry in selected if entry["kind"] == "digest")
        summary = (
            f"dry-run: unread={len(items)} candidates={len(candidates)} "
            f"keep={len(selected)} digest={digest_count} filtered={len(filtered)}"
        )
        logger.info(summary)
        print(summary)
        return 0

    switcher = None
    if cfg["clash"]["enabled"]:
        group = str(cfg["clash"].get("group") or "").strip()
        target = str(cfg["clash"].get("target") or "").strip()
        if group and target:
            switcher = ClashSwitcher(
                group,
                target,
                state_file=_db_path().parent / "clash_switch.json",
            )
            switcher.recover()
            switcher.switch()
        else:
            logger.warning("clash enabled but group/target is empty, skip proxy switch")
    try:
        expanded = _expand_digests(selected, history, seen, filtered, cfg)
        bodies = rss_article.fetch_many(
            [entry["item"]["link"] for entry in expanded],
            max_workers=int(cfg["maxConcurrency"]),
            max_chars=int(cfg["articleMaxChars"]),
            timeout=int(cfg["fetchTimeout"]),
        )
        ready: list[dict] = []
        for entry in expanded:
            text = bodies.get(entry["item"]["link"])
            if not text or len(text) < 50:
                filtered.append(
                    _filtered(entry["item"], l10n.t("rss.filter_fetch_failed"))
                )
                continue
            entry["text"] = text
            ready.append(entry)

        summaries = summarize(ready, cfg)
        articles: list[dict] = []
        for entry in ready:
            summary = summaries.get(entry["item"]["link"])
            if not summary or not (summary.get("summary") or "").strip():
                filtered.append(
                    _filtered(entry["item"], l10n.t("rss.filter_summary_failed"))
                )
                continue
            articles.append(
                {
                    "title": entry["item"]["title"],
                    "url": entry["item"]["link"],
                    "title_translation": (
                        summary.get("title_translation") or ""
                    ).strip(),
                    "summary": summary.get("summary").strip(),
                }
            )

        text = rss_render.render(day, articles, filtered)
        for issue in rss_render.validate(text):
            logger.warning("format: %s", issue)
        archive.parent.mkdir(parents=True, exist_ok=True)
        tmp = archive.with_suffix(archive.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, archive)
        logger.info(
            "written %s (articles=%s filtered=%s)",
            archive,
            len(articles),
            len(filtered),
        )

        if cfg["markRead"]:
            mark_read([item["id"] for item in items])
        print(f"rss daily: {archive} articles={len(articles)} filtered={len(filtered)}")
        return 0
    finally:
        if switcher:
            switcher.restore()


def _expand_digests(
    selected: list[dict],
    history: set[str],
    seen: set[str],
    filtered: list[dict],
    cfg: dict,
) -> list[dict]:
    digest_entries = [entry for entry in selected if entry["kind"] == "digest"]
    if not digest_entries:
        return selected

    pages = rss_article.fetch_many(
        [entry["item"]["link"] for entry in digest_entries],
        max_workers=int(cfg["maxConcurrency"]),
        max_chars=max(int(cfg["articleMaxChars"]), 30000),
        timeout=int(cfg["fetchTimeout"]),
    )
    expanded: list[dict] = []
    for entry in selected:
        if entry["kind"] != "digest":
            expanded.append(entry)
            continue
        item = entry["item"]
        page = pages.get(item["link"])
        if not page:
            filtered.append(_filtered(item, l10n.t("rss.filter_weekly_fetch_failed")))
            continue
        links = rss_article.extract_digest_links(
            item["feed_name"],
            page,
            limit=int(cfg["maxDigestLinks"]),
            llm_timeout=int(cfg["llmTimeout"]),
        )
        if not links:
            filtered.append(_filtered(item, l10n.t("rss.filter_weekly_no_links")))
            continue
        for link in links:
            key = rss_render.normalize_url(link["url"])
            if key in history:
                filtered.append(
                    {
                        "title": link["title"],
                        "url": link["url"],
                        "feed": item["feed_name"],
                        "reason": l10n.t("rss.filter_historical"),
                    }
                )
                continue
            if key in seen:
                continue
            seen.add(key)
            expanded.append(
                {
                    "item": {
                        "title": link["title"],
                        "link": link["url"],
                        "feed_name": item["feed_name"],
                    },
                    "kind": "article",
                }
            )
    return expanded


def main() -> int:
    parser = argparse.ArgumentParser(description="RSS daily digest")
    parser.add_argument(
        "--dry-run", action="store_true", help="stop after filtering; write nothing"
    )
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print("invalid --date, expected YYYY-MM-DD", file=sys.stderr)
            return 2
    day = args.date or date.today().isoformat()
    try:
        return run(dry_run=args.dry_run, day=day)
    except Exception:
        logger.exception("rss daily failed")
        print("rss daily failed, see ~/.cache/utility/rss_daily.log", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
