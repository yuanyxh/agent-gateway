"""Engine search layer: turn a query into ranked result links.

Parsers, engine ordering, cooldowns, relevance ranking, and the Wikipedia source
live here. HTTP work is delegated to :mod:`utility.services.fetch.http`, so this
module contains no proxy, cookie, or retry logic.
"""

from __future__ import annotations

import html as htmlmod
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from agent_gateway.services.fetch import http

TIMEOUT = http.TIMEOUT

WIKI_LANGS = ("zh", "en")
WIKI_MIN_CHARS = 600

_ENDPOINTS = {
    "bing": "https://www.bing.com/search?q={q}&count=10",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "brave": "https://search.brave.com/search?q={q}",
    "sogou": "https://www.sogou.com/web?query={q}",
    "so360": "https://www.so.com/s?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}&rn=10",
    "marginalia": "https://old-search.marginalia.nu/search?query={q}",
}
_HOME = {
    "bing": "https://www.bing.com/",
    "duckduckgo": "https://html.duckduckgo.com/html/",
    "brave": "https://search.brave.com/",
    "sogou": "https://www.sogou.com/",
    "so360": "https://www.so.com/",
    "baidu": "https://www.baidu.com/",
    "marginalia": "https://old-search.marginalia.nu/",
    "google": "https://www.google.com/",
}
#: Google runs through the app's WKWebView subcommand, not the HTTP transport.
GOOGLE_BIN_ENV = "UTILITY_BIN"
GOOGLE_TIMEOUT = 15
#: Only Baidu needs a homepage visit to receive the cookies that keep results stable.
_WARMUP = ("baidu",)
#: Seconds between two requests to the same engine. Longer for engines that ban quickly.
_MIN_INTERVAL = {
    "bing": 1.2,
    "sogou": 2.0,
    "so360": 1.5,
    "baidu": 2.0,
    "marginalia": 1.5,
    "brave": 2.5,
    "duckduckgo": 2.5,
}
#: Chinese queries hit the CN engines first; English queries avoid their noise.
CJK_ORDER = (
    "sogou",
    "google",
    "so360",
    "baidu",
    "bing",
    "marginalia",
    "brave",
    "duckduckgo",
)
LATIN_ORDER = (
    "bing",
    "google",
    "marginalia",
    "brave",
    "duckduckgo",
    "sogou",
    "so360",
    "baidu",
)
DEFAULT_ENGINES = (
    "bing",
    "google",
    "sogou",
    "so360",
    "baidu",
    "marginalia",
    "brave",
    "duckduckgo",
)
#: Stop cascading once this many candidates exist; IDF ranking needs a corpus.
_CANDIDATE_TARGET = 15
#: Drop candidates whose weighted token coverage is below this floor.
_MIN_SCORE = 0.25
#: CJK queries match on bigrams, so coverage runs lower than for ASCII words.
_CJK_MIN_SCORE = 0.15
ENGINE_COOLDOWN = 600.0
SEARCH_BUDGET = 25.0
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


def _get(url: str, engine: str, timeout: int = TIMEOUT, retries: int = 2):
    return http.get_text(
        url,
        profile=engine,
        headers=_headers(engine),
        timeout=timeout,
        retries=retries,
        rate_key=engine,
        interval=_MIN_INTERVAL.get(engine, 1.5),
    )


def _warm(engine: str) -> None:
    if engine not in _WARMUP:
        return
    with _warm_lock:
        if engine in _warmed:
            return
    try:
        http.get_text(
            _HOME[engine],
            profile=engine,
            headers=_headers(engine),
            timeout=10,
            retries=0,
            rate_key=engine,
            interval=_MIN_INTERVAL.get(engine, 1.5),
        )
        time.sleep(0.4)
        with _warm_lock:
            _warmed.add(engine)
    except Exception:
        pass


def utility_binary() -> str | None:
    """Resolve the app binary that serves the WKWebView search subcommand."""
    explicit = os.environ.get(GOOGLE_BIN_ENV, "").strip()
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    path = os.path.dirname(os.path.realpath(__file__))
    while True:
        if os.path.basename(path) == "Contents":
            candidate = os.path.join(path, "MacOS", "utility")
            if os.access(candidate, os.X_OK):
                return candidate
        if os.path.isfile(os.path.join(path, "Package.swift")):
            candidate = os.path.join(path, ".build", "debug", "utility")
            if os.access(candidate, os.X_OK):
                return candidate
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _search_google(query: str, limit: int) -> list:
    """Delegate Google to the app's offscreen WKWebView subcommand."""
    binary = utility_binary()
    if not binary:
        raise RuntimeError(f"utility binary not found (set {GOOGLE_BIN_ENV})")
    proc = subprocess.run(
        [binary, "websearch", "--query", query, "--limit", str(limit)],
        capture_output=True,
        text=True,
        timeout=GOOGLE_TIMEOUT,
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"websearch exit {proc.returncode}: {lines[-1] if lines else ''}"
        )
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError("websearch returned no output")
    payload = json.loads(lines[-1])
    if not payload.get("ok"):
        status = payload.get("status", "failed")
        raise RuntimeError(f"google {status}: {payload.get('error', '')}")
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
WIKI_UA = "utility-knowledge-panel/1.0 (personal use)"


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


def _marginalia_retry(raw: str) -> str:
    """Marginalia answers bots with a wait page; follow its manual link once."""
    if "barraged by queries" not in raw.lower():
        return raw
    match = re.search(r'href="(/search\?[^"]+sst=[^"]+)"', raw)
    if not match:
        return raw
    time.sleep(2.0)
    try:
        _, retried, _ = _get(
            "https://old-search.marginalia.nu" + htmlmod.unescape(match.group(1)),
            "marginalia",
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


def _resolve_jumps(results: list) -> list:
    targets = [
        r
        for r in results
        if "/link?url=" in r["url"]
        or "baidu.com/link?" in r["url"]
        or "google.com/url?" in r["url"]
    ]
    if not targets:
        return results
    with ThreadPoolExecutor(max_workers=3) as pool:
        for r, real in zip(targets, pool.map(lambda r: _resolve_one_jump(r), targets)):
            r["url"] = real
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
    return sorted(results, key=lambda r: (-r["score"], len(r["title"])))


def _order_for(query: str):
    return CJK_ORDER if _CJK_RE.search(query) else LATIN_ORDER


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
    for engine in order:
        if engine == "google":
            if not _engine_ok(engine):
                status[engine] = {"status": "cooldown"}
                continue
            if time.monotonic() >= deadline:
                status[engine] = {"status": "skipped_budget"}
                continue
            attempted += 1
            try:
                parsed = _search_google(query, _CANDIDATE_TARGET)
            except Exception as exc:
                _record_failure(engine)
                status[engine] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                continue
            _record_success(engine)
            status[engine] = {"status": "ok", "parsed": len(parsed)}
            for r in parsed:
                r.setdefault("engine", engine)
            out.extend(parsed)
            if len(out) >= _CANDIDATE_TARGET:
                stopped_at = engine
                break
            continue
        if engine not in _PARSERS:
            status[engine] = {"status": "unknown_engine"}
            continue
        if not _engine_ok(engine):
            status[engine] = {"status": "cooldown"}
            continue
        if time.monotonic() >= deadline:
            status[engine] = {"status": "skipped_budget"}
            continue
        attempted += 1
        _warm(engine)
        if time.monotonic() >= deadline:
            status[engine] = {"status": "skipped_budget"}
            break
        url = _ENDPOINTS[engine].format(q=urllib.parse.quote(query))
        remaining = max(2.0, deadline - time.monotonic())
        try:
            _, raw, _ = _get(
                url,
                engine,
                timeout=min(SEARCH_TIMEOUT, remaining),
                retries=1,
            )
        except Exception as exc:
            _record_failure(engine)
            status[engine] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            continue
        _record_success(engine)
        if engine == "marginalia":
            raw = _marginalia_retry(raw)
        try:
            parsed = _PARSERS[engine](raw)
        except Exception as exc:
            parsed = []
            status[engine] = {
                "status": "parse_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            status[engine] = {"status": "ok", "parsed": len(parsed)}
        for r in parsed:
            r.setdefault("engine", engine)
        out.extend(r for r in parsed if not _is_engine_page(r["url"]))
        if len(out) >= _CANDIDATE_TARGET:
            stopped_at = engine
            break
    ranked = _score_results(_resolve_jumps(_dedup(out)), query)
    # Jump resolution can land back on an engine's own result page (Sogou does
    # this for some queries), so the filter has to run again after resolution.
    ranked = [r for r in ranked if not is_engine_page(r["url"])]
    min_score = _CJK_MIN_SCORE if _CJK_RE.search(query) else _MIN_SCORE
    results = [r for r in ranked if r.get("score", 0) >= min_score][:min_results]
    for engine, info in status.items():
        if info.get("status") == "ok":
            info["kept"] = sum(1 for r in results if r.get("engine") == engine)
    known = [e for e in order if e in _PARSERS or e == "google"]
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
