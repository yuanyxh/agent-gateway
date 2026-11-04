"""Engine search layer: turn a query into ranked result links.

Parsers, engine ordering, cooldowns, relevance ranking, and the Wikipedia source
live here. HTTP work is delegated to :mod:`agent_gateway.services.fetch.http`, so this
module contains no proxy, cookie, or retry logic.
"""

from __future__ import annotations

import html as htmlmod
import json
import math
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent_gateway.services.fetch import http
from agent_gateway.services.search import engine_config

TIMEOUT = http.TIMEOUT

WIKI_LANGS = ("zh", "en")
WIKI_MIN_CHARS = 600

#: Engine specs come from built-in defaults plus config/env overrides. A provider
#: (for example Google through a browser helper) is injected by the caller; this
#: module never discovers host binaries on its own.
_ENGINES, _PROVIDERS, _ORDER = engine_config.load()
_RUNTIME = engine_config.load_runtime()
_ENDPOINTS = {name: spec.endpoint for name, spec in _ENGINES.items()}
_HOME = {name: spec.home for name, spec in _ENGINES.items()}
_MIN_INTERVAL = {name: spec.min_interval for name, spec in _ENGINES.items()}
_WARMUP = tuple(name for name, spec in _ENGINES.items() if spec.warmup)
#: Chinese queries hit the CN engines first; English queries avoid their noise.
CJK_ORDER = _ORDER["cjk"]
LATIN_ORDER = _ORDER["latin"]
#: Stop cascading once this many candidates exist; IDF ranking needs a corpus.
_CANDIDATE_TARGET = 15
#: Drop candidates whose weighted token coverage is below this floor.
_MIN_SCORE = 0.25
#: CJK queries match on bigrams, so coverage runs lower than for ASCII words.
_CJK_MIN_SCORE = 0.15
ENGINE_COOLDOWN = 600.0
SEARCH_BUDGET = _RUNTIME.timeout_seconds
SEARCH_TIMEOUT = 12

_CJK_RE = re.compile(r"[㐀-鿿]")


# ── engine health ───────────────────────────────────────────────────────────

_health_lock = threading.Lock()
_health: dict[str, dict] = {}
_warm_lock = threading.Lock()
_warmed: set[str] = set()


def _engine_ok(engine: str) -> bool:
    with _health_lock:
        return time.monotonic() >= _health.get(engine, {}).get("until", 0.0)


def _record_failure(engine: str) -> None:
    with _health_lock:
        state = _health.setdefault(engine, {"failures": 0, "until": 0.0})
        state["failures"] += 1
        if state["failures"] >= 2:
            state["until"] = time.monotonic() + ENGINE_COOLDOWN
            state["failures"] = 0


def _record_success(engine: str) -> None:
    with _health_lock:
        _health[engine] = {"failures": 0, "until": 0.0}


def reset_engine_health() -> None:
    """Clear cooldowns (tests and manual recovery)."""
    with _health_lock:
        _health.clear()


def reset_warm_state() -> None:
    with _warm_lock:
        _warmed.clear()


def engine_status() -> dict:
    now = time.monotonic()
    with _health_lock:
        return {
            name: {
                "failures": state["failures"],
                "cooldown_s": max(0, int(state["until"] - now)),
            }
            for name, state in sorted(_health.items())
        }


# ── transport adapters ──────────────────────────────────────────────────────


def _headers(engine: str) -> dict:
    return http.engine_headers(engine, _HOME.get(engine, ""))


def _get(
    url: str,
    engine: str,
    timeout: int = TIMEOUT,
    retries: int = 2,
    deadline: float | None = None,
):
    return http.get_text(
        url,
        profile=engine,
        headers=_headers(engine),
        timeout=timeout,
        retries=retries,
        rate_key=engine,
        interval=_MIN_INTERVAL.get(engine, 1.5),
        deadline=deadline,
    )


def _warm(engine: str, deadline: float | None = None) -> None:
    if engine not in _WARMUP:
        return
    with _warm_lock:
        if engine in _warmed:
            return
    remaining = 10.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return
    try:
        http.get_text(
            _HOME[engine],
            profile=engine,
            headers=_headers(engine),
            timeout=min(10.0, remaining),
            retries=0,
            rate_key=engine,
            interval=_MIN_INTERVAL.get(engine, 1.5),
            deadline=deadline,
        )
        time.sleep(min(0.4, max(0.0, remaining - 0.1)))
        with _warm_lock:
            _warmed.add(engine)
    except Exception:
        pass


def _run_provider(
    engine: str, query: str, limit: int, timeout: float | None = None
) -> list:
    """Run an injected provider command and parse its last JSON line."""
    spec = _PROVIDERS.get(engine)
    if spec is None:
        raise RuntimeError(f"search provider '{engine}' is not configured")
    command = [
        part.replace("{q}", query).replace("{limit}", str(limit))
        for part in spec.command
    ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=spec.timeout if timeout is None else timeout,
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"{engine} exit {proc.returncode}: {lines[-1] if lines else ''}"
        )
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError(f"{engine} returned no output")
    payload = json.loads(lines[-1])
    if not payload.get("ok"):
        status = payload.get("status", "failed")
        raise RuntimeError(f"{engine} {status}: {payload.get('error', '')}")
    return [
        {
            "title": str(item.get("title", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "date": "",
            "snippet": "",
        }
        for item in payload.get("results", [])
        if str(item.get("url", "")).startswith("http")
    ]


def _search_google(query: str, limit: int) -> list:
    """Backward-compatible alias for the injected Google provider."""
    return _run_provider("google", query, limit)


def fetch(url: str, engine: str = "bing", timeout: int = TIMEOUT) -> str:
    """Fetch raw HTML with a browser-like request (kept for the knowledge pipeline)."""
    host = urllib.parse.urlparse(url).netloc.lower()
    _, text, _ = http.get_text(
        url,
        profile=engine,
        headers=_headers(engine),
        timeout=timeout,
        retries=0,
        rate_key="host:" + host,
        interval=0.6,
    )
    return text


# ── result parsers ──────────────────────────────────────────────────────────


def _text(fragment: str) -> str:
    # Comments carry structured payloads; script/style bodies are not page text.
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.DOTALL)
    fragment = re.sub(
        r"<(script|style)\b.*?</\1>", "", fragment, flags=re.DOTALL | re.I
    )
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _unredirect(url: str) -> str:
    if url.startswith("/url?q="):
        return url[8:].split("&")[0]
    if "uddg=" in url:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return q.get("uddg", [url])[0]
    return url


#: Baidu ad slots link through baidu.php / e.baidu.com; `mu` then points at an
#: internal placeholder host instead of the advertiser's real page.
_AD_URL_MARKERS = (
    "baidu.php",
    "e.baidu.com",
    "nourl.ubs.baidu.com",
    "recommend_list.baidu.com",
)


def _is_ad_url(url: str) -> bool:
    if not url:
        return True
    low = url.lower()
    return any(marker in low for marker in _AD_URL_MARKERS)


#: A search engine's own result/aggregation page carries no page content.
_ENGINE_PAGES = (
    ("www.sogou.com", "/sogou"),
    ("www.sogou.com", "/web"),
    ("www.baidu.com", "/s"),
    ("www.so.com", "/s"),
    ("www.bing.com", "/search"),
    ("cn.bing.com", "/search"),
    ("html.duckduckgo.com", "/html/"),
    ("search.brave.com", "/search"),
    ("old-search.marginalia.nu", "/search"),
    ("www.google.com", "/search"),
    ("google.com", "/search"),
)


def is_engine_page(url: str) -> bool:
    parts = urllib.parse.urlparse(url.lower())
    return any(
        parts.netloc == host and parts.path.startswith(path)
        for host, path in _ENGINE_PAGES
    )


_is_engine_page = is_engine_page


def _dedup(results: list, limit: int | None = None) -> list:
    seen, out = set(), []
    for r in results:
        if r["url"] not in seen and r["title"]:
            seen.add(r["url"])
            out.append(r)
    return out if limit is None else out[:limit]


def _parse_bing(raw: str) -> list:
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', raw, re.DOTALL):
        m = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL
        )
        if not m or not m.group(1).startswith("http"):
            continue
        snippet = ""
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        if p:
            snippet = _text(p.group(1))
        md = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", snippet)
        out.append(
            {
                "title": _text(m.group(2)),
                "url": m.group(1),
                "date": md.group(0) if md else "",
                "snippet": snippet[:200],
            }
        )
    return out


def _parse_duckduckgo(raw: str) -> list:
    out = []
    for block in re.findall(
        r'<div class="result[^"]*".*?</div>\s*</div>', raw, re.DOTALL
    ):
        m = re.search(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if not m:
            continue
        url = _unredirect(m.group(1))
        if not url.startswith("http"):
            continue
        snippet = ""
        hit = re.search(r'<a class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)
        if hit:
            snippet = _text(hit.group(1))
        out.append(
            {
                "title": _text(m.group(2)),
                "url": url,
                "date": "",
                "snippet": snippet[:200],
            }
        )
    return out


def _parse_brave(raw: str) -> list:
    out = []
    for block in re.findall(
        r'<div class="snippet[^"]*".*?</div>\s*</div>', raw, re.DOTALL
    ):
        m = re.search(
            r'<a[^>]*class="result-header"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        ) or re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not m or not m.group(1).startswith("http"):
            continue
        snippet = ""
        hit = re.search(
            r'<p class="snippet-description[^"]*"[^>]*>(.*?)</p>', block, re.DOTALL
        )
        if hit:
            snippet = _text(hit.group(1))
        date = ""
        dm = re.search(r'<time[^>]*datetime="([^"]*)"', block, re.DOTALL)
        if dm:
            date = dm.group(1)[:10]
        out.append(
            {
                "title": _text(m.group(2)),
                "url": m.group(1),
                "date": date,
                "snippet": snippet[:200],
            }
        )
    return out


def _parse_sogou(raw: str) -> list:
    out = []
    for block in re.findall(
        r'<div class="vrwrap".*?(?=<div class="vrwrap"|</body>|\Z)', raw, re.DOTALL
    ):
        m = re.search(
            r'<h3[^>]*class="vr-title[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if not m:
            continue
        url = htmlmod.unescape(m.group(1))
        if url.startswith("/link"):
            url = "https://www.sogou.com" + url
        if not url.startswith("http"):
            continue
        snippet = ""
        p = re.search(
            r'<(?:p|div)[^>]*class="[^"]*space-txt[^"]*"[^>]*>(.*?)</(?:p|div)>',
            block,
            re.DOTALL,
        )
        if p:
            snippet = _text(p.group(1))
        out.append(
            {
                "title": _text(m.group(2)),
                "url": url,
                "date": "",
                "snippet": snippet[:200],
            }
        )
    return out


def _parse_so360(raw: str) -> list:
    out = []
    for block in re.findall(
        r'<li class="res-list[^"]*".*?(?=<li class="res-list|</ol>)', raw, re.DOTALL
    ):
        m = re.search(
            r'<h3[^>]*class="res-title[^"]*"[^>]*>\s*<a\s([^>]*)>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if not m:
            continue
        md = re.search(r'data-mdurl="([^"]+)"', m.group(1))
        href = re.search(r'href="([^"]+)"', m.group(1))
        url = htmlmod.unescape(md.group(1) if md else (href.group(1) if href else ""))
        if not url.startswith("http"):
            continue
        snippet = ""
        p = re.search(
            r'<span[^>]*class="res-list-summary"[^>]*>(.*?)</span>', block, re.DOTALL
        )
        if p:
            snippet = _text(p.group(1))
        d = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", snippet)
        out.append(
            {
                "title": _text(m.group(2)),
                "url": url,
                "date": d.group(0) if d else "",
                "snippet": snippet[:200],
            }
        )
    return out


_BAIDU_SDATA_RE = re.compile(r"<!--s-data:(.*?)-->", re.DOTALL)


def _baidu_summary(block: str) -> str:
    """New Baidu cards keep the snippet in an s-data comment, not c-abstract."""
    raw = _BAIDU_SDATA_RE.search(block)
    if raw:
        try:
            payload = json.loads(htmlmod.unescape(raw.group(1)))
            lines = payload.get("summaryData", {}).get("generalLines", [])
            parts = [
                item.get("text", "")
                for line in lines
                for item in line.get("data", [])
                if item.get("text")
            ]
            text = _text(" ".join(parts))
            if text:
                return text
        except (ValueError, TypeError, AttributeError):
            pass
    for pattern in (
        r'<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>',
        r'<span[^>]*class="[^"]*content-right[^"]*"[^>]*>(.*?)</span>',
        r'<div[^>]*class="[^"]*c-span-last[^"]*"[^>]*>(.*?)</div>',
    ):
        hit = re.search(pattern, block, re.DOTALL)
        if hit:
            return _text(hit.group(1))
    return ""


def _parse_baidu(raw: str) -> list:
    out = []
    for block in re.findall(
        r'<div[^>]*class="result[^"]*c-container[^"]*".*?'
        r'(?=<div[^>]*class="result[^"]*c-container|</body>)',
        raw,
        re.DOTALL,
    ):
        m = re.search(
            r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL
        )
        if not m:
            continue
        href = htmlmod.unescape(m.group(1)).strip()
        # Whole-block ad: the title link itself is a promotion redirect.
        if "baidu.php" in href.lower():
            continue
        # `mu` is the real target for organic results and a placeholder for ads.
        mu = re.search(r'\smu="([^"]*)"', block)
        url = htmlmod.unescape(mu.group(1)).strip() if mu else href
        if _is_ad_url(url) or not url.startswith("http"):
            continue
        title = _text(m.group(2))
        if not title:
            continue
        out.append(
            {
                "title": title,
                "url": url,
                "date": "",
                "snippet": _baidu_summary(block)[:200],
            }
        )
    return out


def _parse_marginalia(raw: str) -> list:
    out = []
    for block in re.findall(
        r'<section[^>]*class="[^"]*search-result[^"]*".*?</section>', raw, re.DOTALL
    ):
        m = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL
        )
        if not m or not m.group(1).startswith("http"):
            continue
        snippet = ""
        hit = re.search(r'<p class="description"[^>]*>(.*?)</p>', block, re.DOTALL)
        if hit:
            snippet = _text(hit.group(1))
        out.append(
            {
                "title": _text(m.group(2)),
                "url": htmlmod.unescape(m.group(1)),
                "date": "",
                "snippet": snippet[:200],
            }
        )
    return out


_PARSERS = {
    "bing": _parse_bing,
    "duckduckgo": _parse_duckduckgo,
    "brave": _parse_brave,
    "sogou": _parse_sogou,
    "so360": _parse_so360,
    "baidu": _parse_baidu,
    "marginalia": _parse_marginalia,
}


# ── Wikipedia ───────────────────────────────────────────────────────────────

_WIKI_GAP = 1.2
_wiki_clock = [0.0]
WIKI_UA = "agent-gateway/0.1 (personal use)"


def _wiki_get(url: str, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        wait = _WIKI_GAP - (time.monotonic() - _wiki_clock[0])
        if wait > 0:
            time.sleep(wait)
        _wiki_clock[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == retries:
                raise
            time.sleep(3.0 * (attempt + 1))
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def _wiki_extract(title: str, lang: str):
    url = (
        f"https://{lang}.wikipedia.org/w/api.php?action=query&prop=extracts"
        f"&explaintext=1&redirects=1&format=json&titles={urllib.parse.quote(title)}"
    )
    for p in _wiki_get(url).get("query", {}).get("pages", {}).values():
        if p.get("extract", "").strip():
            return {
                "title": p["title"],
                "lang": lang,
                "extract": p["extract"],
                "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(p['title'])}",
            }
    return None


def _overlap(a: str, b: str) -> int:
    """Length of the longest common substring of a and b."""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def _wiki_title_for(term: str, lang: str):
    url = (
        f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={urllib.parse.quote(term)}&format=json&srlimit=5"
    )
    for h in _wiki_get(url).get("query", {}).get("search", []):
        title = h["title"]
        if _overlap(term.lower(), title.lower()) >= min(len(term), 4):
            return title
    return None


def _wiki_langlink(title: str, src: str, dst: str):
    url = (
        f"https://{src}.wikipedia.org/w/api.php?action=query&prop=langlinks"
        f"&lllang={dst}&redirects=1&format=json&titles={urllib.parse.quote(title)}"
    )
    for p in _wiki_get(url).get("query", {}).get("pages", {}).values():
        links = p.get("langlinks") or []
        if links:
            return links[0]["*"]
    return None


def _wiki_find(term: str, lang: str):
    hit = _wiki_extract(term, lang)
    if hit:
        return hit
    alt = _wiki_title_for(term, lang)
    return _wiki_extract(alt, lang) if alt else None


def wiki(term: str, langs=WIKI_LANGS, min_chars: int = WIKI_MIN_CHARS):
    """Return the authoritative extract for a concept, or None."""
    try:
        return _wiki_lookup(term, langs, min_chars)
    except Exception:
        return None


def _wiki_lookup(term: str, langs, min_chars: int):
    hit = None
    for lang in langs:
        hit = _wiki_find(term, lang)
        if hit:
            break
    if not hit or len(hit["extract"]) >= min_chars:
        return hit
    for other in langs:
        if other == hit["lang"]:
            continue
        alt_title = _wiki_langlink(hit["title"], hit["lang"], other)
        if not alt_title:
            continue
        alt = _wiki_extract(alt_title, other)
        if alt and len(alt["extract"]) > len(hit["extract"]):
            return alt
    return hit


# ── jump resolution ─────────────────────────────────────────────────────────


def _marginalia_retry(raw: str, deadline: float | None = None) -> str:
    """Marginalia answers bots with a wait page; follow its manual link once."""
    if "barraged by queries" not in raw.lower():
        return raw
    match = re.search(r'href="(/search\?[^"]+sst=[^"]+)"', raw)
    if not match:
        return raw
    remaining = 2.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 2.0:
        return raw
    time.sleep(2.0)
    try:
        _, retried, _ = _get(
            "https://old-search.marginalia.nu" + htmlmod.unescape(match.group(1)),
            "marginalia",
            timeout=max(1, int(min(SEARCH_TIMEOUT, remaining - 2.0))),
            retries=0,
            deadline=deadline,
        )
        return retried
    except Exception:
        return raw


def _resolve_sogou_jump(url: str) -> str:
    try:
        raw = fetch(url, "sogou", timeout=8)
    except Exception:
        return url
    m = re.search(r"location\.replace\(\s*[\"']([^\"']+)[\"']", raw)
    return htmlmod.unescape(m.group(1)) if m else url


def _resolve_baidu_jump(url: str) -> str:
    try:
        final, _, _ = http.get_text(
            url,
            profile="baidu",
            headers=_headers("baidu"),
            timeout=8,
            retries=0,
            rate_key="baidu:jump",
            interval=0.5,
        )
        if final.startswith("http") and "baidu.com/link" not in final:
            return final
    except Exception:
        pass
    return url


def _resolve_jumps(results: list, deadline: float | None = None) -> list:
    targets = [
        r
        for r in results
        if "/link?url=" in r["url"]
        or "baidu.com/link?" in r["url"]
        or "google.com/url?" in r["url"]
    ]
    if not targets:
        return results
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 1.0:
        return results
    pool = ThreadPoolExecutor(max_workers=3)
    futures = {pool.submit(_resolve_one_jump, r): r for r in targets}
    try:
        for future in as_completed(futures, timeout=remaining):
            r = futures[future]
            try:
                r["url"] = future.result()
            except Exception:
                pass
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _resolve_one_jump(result: dict) -> str:
    if result.get("engine") == "google":
        return _resolve_google_jump(result["url"])
    if result.get("engine") == "sogou":
        return _resolve_sogou_jump(result["url"])
    return _resolve_baidu_jump(result["url"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _google_location(url: str) -> str:
    try:
        request = urllib.request.Request(url, headers=_headers("google"))
        with _NO_REDIRECT_OPENER.open(request, timeout=8) as response:
            return response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location", "")
        return ""
    except Exception:
        return ""


def _resolve_google_jump(url: str) -> str:
    """Google wraps results in /url?q=; a non-following GET returns the target."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
        if target.startswith("http"):
            return target
    final = _google_location(url)
    if final.startswith("http") and "google.com/url" not in final:
        return final
    return url


# ── relevance ───────────────────────────────────────────────────────────────


_ASCII_RUN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[㐀-鿿]+")


def _query_tokens(query: str) -> list[str]:
    """ASCII words plus CJK bigrams; no dictionary or tokenizer required."""
    q = query.lower()
    tokens = _ASCII_RUN_RE.findall(q)
    for run in _CJK_RUN_RE.findall(q):
        tokens += (
            [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
        )
    return tokens


def _contains_token(text: str, token: str) -> bool:
    """ASCII tokens match whole words; CJK bigrams match substrings.

    Plain ``token in text`` made "12345" match "123456" and "no" match "not",
    which let gibberish queries keep an unrelated result.
    """
    if token.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return token in text


_SNIPPET_MIN_CHARS = 20
_SNIPPET_PENALTY = 0.15


def _rank_key(result: dict) -> tuple[float, int]:
    snippet = (result.get("snippet") or "").strip()
    penalty = _SNIPPET_PENALTY if len(snippet) < _SNIPPET_MIN_CHARS else 0.0
    return (-(result["score"] - penalty), len(result["title"]))


def _score_results(results: list, query: str) -> list:
    """Rank candidates by query-token coverage with corpus-local IDF weights."""
    q = query.lower().strip()
    if not q or not results:
        return results
    tokens = _query_tokens(q) or [q]
    docs = [(r["title"] + " " + r.get("snippet", "")).lower() for r in results]
    n = len(docs)
    weights = {
        t: math.log(1 + n / (1 + sum(1 for d in docs if _contains_token(d, t))))
        for t in set(tokens)
    }
    total = sum(weights.values()) or 1.0
    unique = set(tokens)
    for result, doc in zip(results, docs):
        title = result["title"].lower()
        matched = {t for t in unique if _contains_token(doc, t)}
        score = sum(weights[t] for t in matched) / total
        if q in doc:
            score += 1.0
        elif len(unique) >= 3 and len(matched) < 2:
            # A single common word ("no") is not evidence for a multi-word query.
            score = 0.0
        if q in title:
            score += 0.5
        elif any(_contains_token(title, t) for t in tokens):
            score += 0.1
        result["score"] = round(score, 4)
    return sorted(results, key=_rank_key)


def _order_for(query: str):
    return CJK_ORDER if _CJK_RE.search(query) else LATIN_ORDER


def _run_engine(
    engine: str, query: str, limit: int, deadline: float
) -> tuple[list, dict]:
    """Run one engine under the shared hard deadline; returns `(results, status)`."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return [], {"status": "skipped_budget"}

    if engine in _PROVIDERS:
        spec = _PROVIDERS[engine]
        try:
            parsed = _run_provider(
                engine, query, limit, timeout=min(float(spec.timeout), remaining)
            )
        except subprocess.TimeoutExpired:
            _record_failure(engine)
            return [], {"status": "failed", "error": "timeout"}
        except Exception as exc:
            _record_failure(engine)
            return [], {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        _record_success(engine)
        for r in parsed:
            r.setdefault("engine", engine)
        return parsed, {"status": "ok", "parsed": len(parsed)}

    if engine not in _PARSERS:
        return [], {"status": "unknown_engine"}

    _warm(engine, deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return [], {"status": "skipped_budget"}
    url = _ENDPOINTS[engine].format(q=urllib.parse.quote(query))
    try:
        _, raw, _ = _get(
            url,
            engine,
            timeout=int(max(1, min(SEARCH_TIMEOUT, remaining))),
            retries=1,
            deadline=deadline,
        )
    except Exception as exc:
        _record_failure(engine)
        return [], {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    _record_success(engine)
    if engine == "marginalia":
        raw = _marginalia_retry(raw, deadline)
    try:
        parsed = _PARSERS[engine](raw)
    except Exception as exc:
        return [], {"status": "parse_failed", "error": f"{type(exc).__name__}: {exc}"}
    for r in parsed:
        r.setdefault("engine", engine)
    return parsed, {"status": "ok", "parsed": len(parsed)}


def _cascade_parallel(
    query: str, runnable: list, status: dict, deadline: float
) -> tuple[list, int, str | None]:
    """Fan out every runnable engine; unfinished ones are dropped at the deadline."""
    out: list = []
    stopped_at = None
    pool = ThreadPoolExecutor(max_workers=_RUNTIME.concurrency)
    futures = {
        pool.submit(_run_engine, engine, query, _CANDIDATE_TARGET, deadline): engine
        for engine in runnable
    }
    remaining = max(0.0, deadline - time.monotonic())
    try:
        for future in as_completed(futures, timeout=remaining):
            engine = futures[future]
            try:
                parsed, info = future.result()
            except Exception as exc:
                parsed, info = (
                    [],
                    {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            status[engine] = info
            out.extend(r for r in parsed if not _is_engine_page(r["url"]))
            if len(out) >= _CANDIDATE_TARGET:
                stopped_at = engine
                break
    except TimeoutError:
        pass
    finally:
        # cancel 只移除未启动任务；已运行线程仍跑到内部 deadline，这里只延迟回收。
        for future, engine in futures.items():
            if engine not in status:
                status[engine] = {"status": "skipped_budget"}
                future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return out, len(runnable), stopped_at


def search_report(query: str, engines=None, min_results: int = 5):
    """Cascade engines and return `(results, report)`; the report explains failures."""
    if isinstance(engines, str):
        engines = tuple(e.strip() for e in engines.split(",") if e.strip())
    order = tuple(engines) if engines else _order_for(query)
    min_results = max(1, int(min_results))
    out: list = []
    status: dict[str, dict] = {}
    attempted = 0
    stopped_at = None
    deadline = time.monotonic() + SEARCH_BUDGET
    runnable = []
    for engine in order:
        if not _engine_ok(engine):
            status[engine] = {"status": "cooldown"}
        elif engine not in _PARSERS and engine not in _PROVIDERS:
            status[engine] = {"status": "unknown_engine"}
        else:
            runnable.append(engine)

    if _RUNTIME.parallel:
        out, attempted, stopped_at = _cascade_parallel(
            query, runnable, status, deadline
        )
    else:
        for engine in runnable:
            if time.monotonic() >= deadline:
                status[engine] = {"status": "skipped_budget"}
                continue
            attempted += 1
            parsed, info = _run_engine(engine, query, _CANDIDATE_TARGET, deadline)
            status[engine] = info
            out.extend(r for r in parsed if not _is_engine_page(r["url"]))
            if len(out) >= _CANDIDATE_TARGET:
                stopped_at = engine
                break
    ranked = _score_results(_resolve_jumps(_dedup(out), deadline), query)
    # Jump resolution can land back on an engine's own result page (Sogou does
    # this for some queries), so the filter has to run again after resolution.
    ranked = [r for r in ranked if not is_engine_page(r["url"])]
    min_score = _CJK_MIN_SCORE if _CJK_RE.search(query) else _MIN_SCORE
    results = [r for r in ranked if r.get("score", 0) >= min_score][:min_results]
    for engine, info in status.items():
        if info.get("status") == "ok":
            info["kept"] = sum(1 for r in results if r.get("engine") == engine)
    known = [e for e in order if e in _PARSERS or e in _PROVIDERS]
    all_failed = bool(known) and all(
        status.get(engine, {}).get("status") in ("failed", "cooldown", "parse_failed")
        for engine in known
    )
    report = {
        "total": len(results),
        "candidates": len(out),
        "engines": status,
        "attempted": attempted,
        "stopped_at": stopped_at,
        "all_failed": all_failed,
        "no_engines_available": attempted == 0,
    }
    return results, report


def search(query: str, engines=None, min_results: int = 5) -> list:
    """Backward-compatible wrapper used by the knowledge pipeline."""
    return search_report(query, engines, min_results)[0]
