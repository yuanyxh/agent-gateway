"""Compatibility facade for the three-layer web pipeline.

Layers:
- engine search: :mod:`utility.services.search.engines`
- fetch: :mod:`utility.services.fetch.http`
- HTML → Markdown: :mod:`utility.services.markdown.html`

Existing callers import this module; new code should import the layer it needs
directly. Keeping this facade means the knowledge pipeline, RSS, MCP server,
and offline checks do not need to change during the split.
"""

from __future__ import annotations

import time
import urllib.parse

from agent_gateway.services.fetch import http
from agent_gateway.services.markdown import html as md
from agent_gateway.services.search import engines as engine_layer
from agent_gateway.services.search.engines import SEARCH_BUDGET, TIMEOUT

MAX_TOTAL_CHARS = 16000
PAGE_BUDGET = 25.0
PAGE_TIMEOUT = 15

html_to_markdown = md.html_to_markdown
html_to_text = md.html_to_text
fetch = engine_layer.fetch
wiki = engine_layer.wiki
engine_status = engine_layer.engine_status
reset_engine_health = engine_layer.reset_engine_health
reset_warm_state = engine_layer.reset_warm_state

# Private aliases kept for existing offline checks and callers.
_PARSERS = engine_layer._PARSERS
_MIN_SCORE = engine_layer._MIN_SCORE
_CJK_MIN_SCORE = engine_layer._CJK_MIN_SCORE
_parse_sogou = engine_layer._parse_sogou
_parse_baidu = engine_layer._parse_baidu
is_engine_page = engine_layer.is_engine_page
_is_engine_page = is_engine_page
_score_results = engine_layer._score_results
_looks_like_challenge = http._looks_like_challenge
_respect_rate = http.respect_rate
_opener = http._opener
_proxy_map = http._proxy_map
_engine_ok = engine_layer._engine_ok
_record_failure = engine_layer._record_failure
_record_success = engine_layer._record_success


def proxy_status() -> dict:
    return http.proxy_status()


def reset_http_state() -> None:
    """Clear cached openers/UAs and the Baidu warm-up flag (tests and proxy changes)."""
    http.reset_http_state()
    reset_warm_state()


def search_report(query: str, engines=None, min_results: int = 5):
    return engine_layer.search_report(query, engines, min_results)


def search(query: str, engines=None, min_results: int = 5) -> list:
    """Backward-compatible wrapper used by the knowledge pipeline."""
    return search_report(query, engines, min_results)[0]


def fetch_markdown(url: str, max_chars: int = 6000, timeout: int = TIMEOUT) -> dict:
    host = urllib.parse.urlparse(url).netloc.lower()
    # Page fetches see the same transient SSL/EOF failures as search; retry once.
    final, raw, _ = http.get_text(
        url,
        profile="page",
        timeout=timeout,
        retries=1,
        rate_key="host:" + host,
        interval=0.8,
        detect_challenge=False,
    )
    markdown = html_to_markdown(raw, base_url=final, max_chars=max_chars)
    return {"url": final, "markdown": markdown, "chars": len(markdown)}


def websearch(
    query: str,
    limit: int = 5,
    fetch_top: int = 1,
    max_chars: int = 6000,
    engines=None,
) -> dict:
    """Search, then read the top `fetch_top` pages as Markdown (bounded by total budget)."""
    limit = max(1, min(int(limit), 10))
    fetch_top = max(0, min(int(fetch_top), 3))
    max_chars = max(1000, min(int(max_chars), 10000))
    started = time.monotonic()
    results, report = search_report(query, engines=engines, min_results=limit)
    results = results[:limit]
    pages = []
    page_errors = []
    budget = MAX_TOTAL_CHARS
    page_deadline = started + SEARCH_BUDGET + PAGE_BUDGET
    # A top result can be anti-bot protected; try a couple more before giving up.
    for attempts, result in enumerate(results, 1):
        if len(pages) >= fetch_top or attempts > fetch_top + 2 or budget <= 0:
            break
        remaining = page_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            page = fetch_markdown(
                result["url"],
                max_chars=min(max_chars, budget),
                timeout=min(PAGE_TIMEOUT, remaining),
            )
            pages.append(
                {
                    "url": page["url"],
                    "title": result["title"],
                    "markdown": page["markdown"],
                    "chars": page["chars"],
                }
            )
            budget -= page["chars"]
        except Exception as exc:
            page_errors.append(
                {
                    "url": result["url"],
                    "title": result["title"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    payload = {
        "query": query,
        "results": results,
        "pages": pages,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if page_errors and not pages:
        payload["page_errors"] = page_errors
        payload["page_error"] = f"all page fetches failed ({len(page_errors)} tried)"
    if not results:
        if report.get("no_engines_available"):
            payload["error"] = (
                "no search engine available (all failed, cooling down, or skipped)"
            )
        elif report.get("all_failed"):
            detail = "; ".join(
                f"{name}: {info.get('error', info.get('status'))}"
                for name, info in report["engines"].items()
                if info.get("status") in ("failed", "cooldown", "parse_failed")
            )
            payload["error"] = f"all search engines failed: {detail or 'unknown error'}"
        else:
            ok = [
                name
                for name, info in report["engines"].items()
                if info.get("status") == "ok"
            ]
            payload["error"] = "no relevant results" + (
                f" (engines responded: {', '.join(ok)})" if ok else ""
            )
        payload["engine_report"] = report["engines"]
    return payload


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) or "Python asyncio"
    print(websearch(query))
