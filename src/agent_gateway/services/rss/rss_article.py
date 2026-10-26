"""Article fetching and weekly-digest expansion for the RSS daily pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from agent_gateway.llm import client as llm_client
from agent_gateway.core import prompt_loader
from agent_gateway.services.search import web_search
from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("rss_daily")


def fetch_many(
    urls: list[str],
    *,
    max_workers: int,
    max_chars: int,
    timeout: int,
) -> dict[str, str | None]:
    """Fetch pages concurrently; a failed page maps to None instead of aborting the run."""
    results: dict[str, str | None] = {}
    unique = [url for url in dict.fromkeys(urls) if url]
    if not unique:
        return results
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(web_search.fetch_markdown, url, max_chars, timeout): url
            for url in unique
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                page = future.result()
                text = (page.get("markdown") or "").strip()
                results[url] = text or None
            except Exception as exc:  # noqa: BLE001 - any network/parse failure must degrade per URL
                logger.warning("fetch failed %s: %s", url, exc)
                results[url] = None
    return results


def extract_digest_links(
    feed_name: str,
    page_text: str,
    *,
    limit: int,
    llm_timeout: int,
) -> list[dict]:
    """Ask the LLM for worthwhile external links inside a weekly/roundup page."""
    links: list[dict] = []
    parsed = chat_json(
        "rss_digest",
        {"feed_name": feed_name, "page_text": page_text},
        label="rss_digest",
        llm_timeout=llm_timeout,
        max_tokens=2500,
        attempts=2,
    )
    if not parsed:
        logger.warning("digest output invalid for %s", feed_name)
        return links

    seen: set[str] = set()
    for item in parsed.get("links") or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        links.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": url,
                "why": (item.get("why") or "").strip(),
            }
        )
        if len(links) >= max(1, limit):
            break
    return links


def chat_json(
    feature: str,
    variables: dict,
    *,
    label: str,
    llm_timeout: int,
    max_tokens: int,
    attempts: int,
) -> dict | None:
    prompt = prompt_loader.prompt(feature, **variables)
    for attempt in range(max(1, attempts)):
        raw = llm_client.chat(
            prompt,
            label=label,
            timeout=llm_timeout,
            max_tokens=max_tokens,
            disable_thinking=True,
            # Provider-private switch: without it reasoning consumes max_tokens and content comes back empty.
            extra_body={"thinking": {"type": "disabled"}},
        )
        parsed = llm_client.parse_json(raw)
        if parsed is not None and not prompt_loader.missing_json_keys(feature, parsed):
            return parsed
        logger.warning("%s attempt %s invalid", label, attempt + 1)
    return None
