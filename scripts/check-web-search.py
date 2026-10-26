#!/usr/bin/env python3
"""Checks for src/agent_gateway/services/search/web_search.py.

Default is offline: parser fixtures, Markdown conversion, truncation, relevance,
rate limiting, and the engine circuit breaker. `--live` additionally hits the real
engines and is intentionally slow (per-engine rate limits).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from agent_gateway.services.fetch import http as h
from agent_gateway.services.read import read_service as r
from agent_gateway.services.search import web_search as w

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")


def selftest() -> None:
    print("[web_search] offline checks")

    html = (
        "<html><head><title>Demo</title><style>.x{color:red}</style>"
        "<script>alert(1)</script></head><body><nav>skip me</nav>"
        "<article><h1>Hello</h1><p>World <a href='/x'>link</a></p>"
        "<ul><li>A</li><li>B</li></ul><pre><code>print(1)</code></pre>"
        "<table><tr><th>K</th><th>V</th></tr><tr><td>a</td><td>1</td></tr></table>"
        "</article><footer>foot</footer></body></html>"
    )
    md = w.html_to_markdown(html, base_url="https://example.com/base")
    check("markdown: heading", "# Hello" in md, md[:80])
    check("markdown: link resolved", "https://example.com/x" in md, md[:120])
    check("markdown: list", "- A" in md and "- B" in md)
    check("markdown: code fence", "```" in md and "print(1)" in md)
    check("markdown: table", "| K | V |" in md and "| --- | --- |" in md)
    check(
        "markdown: boilerplate removed",
        all(x not in md for x in ("alert", "color", "skip me", "foot")),
        md[:120],
    )

    long_md = w.html_to_markdown("<p>" + "x" * 3000 + "</p>", max_chars=200)
    check(
        "truncate: capped",
        len(long_md) <= 260 and "truncated" in long_md,
        f"len={len(long_md)}",
    )

    malformed = w.html_to_markdown("<p>unclosed<div><script>x")
    check("malformed: no exception", isinstance(malformed, str))

    ranked = w._score_results(
        [
            {"title": "网络地址转换（NAT）详解", "snippet": ""},
            {"title": "国际资金清算系统_百度百科", "snippet": "SWIFT 组织"},
            {"title": "NAT穿透原理", "snippet": ""},
            {"title": "网络协议概述", "snippet": ""},
        ],
        "网络NAT",
    )
    check(
        "rank: relevant first",
        ranked[0]["title"] == "网络地址转换（NAT）详解",
        str(ranked)[:160],
    )
    check(
        "rank: unrelated last and zero",
        ranked[-1]["title"] == "国际资金清算系统_百度百科" and ranked[-1]["score"] == 0,
        str(ranked)[:160],
    )
    check(
        "rank: partial kept",
        any(r["title"] == "NAT穿透原理" for r in ranked),
    )
    gibberish = w._score_results(
        [{"title": "SQL query tutorial", "snippet": "query"}],
        "zzzz-no-such-query-12345",
    )
    check(
        "rank: gibberish below floor",
        gibberish[0]["score"] < w._MIN_SCORE,
        str(gibberish[0]),
    )
    check(
        "challenge: detector",
        w._looks_like_challenge("<title>Verify you are human</title>"),
    )
    check(
        "challenge: normal page",
        not w._looks_like_challenge("<title>Python asyncio</title><p>guide</p>"),
    )

    w.reset_engine_health()
    started = time.monotonic()
    w._respect_rate("selftest", 0.2)
    w._respect_rate("selftest", 0.2)
    waited = time.monotonic() - started
    check("rate: second call waits", waited >= 0.15, f"waited={waited:.2f}s")

    w.reset_engine_health()
    w._record_failure("fake")
    check("breaker: one failure still ok", w._engine_ok("fake"))
    w._record_failure("fake")
    check("breaker: two failures open", not w._engine_ok("fake"))
    w._record_success("fake")
    check("breaker: success closes", w._engine_ok("fake"))
    w.reset_engine_health()

    fixtures = {
        "bing": (
            '<li class="b_algo"><h2><a href="https://x.test/1">T1</a></h2>'
            "<p>S1</p></li>"
        ),
        "duckduckgo": (
            '<div class="result results_links">'
            '<a class="result__a" href="https://x.test/2">T2</a>'
            '<a class="result__snippet">S2</a></div></div>'
        ),
        "brave": (
            '<div class="snippet svelte">'
            '<a class="result-header" href="https://x.test/3">T3</a>'
            '<p class="snippet-description">S3</p></div></div>'
        ),
        "sogou": (
            '<div class="vrwrap"><h3 class="vr-title">'
            '<a href="https://x.test/4">T4</a></h3>'
            '<p class="space-txt">S4</p></div>'
        ),
        "so360": (
            '<li class="res-list"><h3 class="res-title">'
            '<a data-mdurl="https://x.test/5" href="/link">T5</a></h3>'
            '<span class="res-list-summary">S5</span></li></ol>'
        ),
        "baidu": (
            '<div class="result c-container"><h3>'
            '<a href="http://www.baidu.com/link?url=abc">T6</a></h3>'
            '<div class="c-abstract">S6</div></div></body>'
        ),
        "marginalia": (
            '<section class="card search-result"><h2>'
            '<a class="title" href="https://x.test/7">T7</a></h2>'
            '<p class="description">S7</p></section>'
        ),
    }
    for engine, raw in fixtures.items():
        parsed = w._PARSERS[engine](raw)
        check(
            f"parser: {engine}",
            len(parsed) == 1 and parsed[0]["title"].startswith("T"),
            str(parsed)[:120],
        )

    baidu_mixed = (
        '<div class="result c-container"><h3>'
        '<a href="https://www.baidu.com/baidu.php?url=AD">AD1</a></h3></div>'
        '<div class="result c-container"><h3>'
        '<a href="http://www.baidu.com/link?url=ok">T7</a></h3>'
        '<div class="c-abstract">S7</div>'
        '<span mu="https://real.test/a"></span></div>'
        '<div class="result c-container"><h3>'
        '<a href="http://www.baidu.com/link?url=ad2">AD2</a></h3>'
        '<span mu="http://nourl.ubs.baidu.com/1"></span></div>'
        '<div class="result c-container">'
        '<span mu="http://28608.recommend_list.baidu.com/"></span></div></body>'
    )
    baidu_parsed = w._parse_baidu(baidu_mixed)
    check(
        "baidu: ads dropped",
        len(baidu_parsed) == 1,
        str(baidu_parsed)[:200],
    )
    check(
        "baidu: real url from mu",
        bool(baidu_parsed) and baidu_parsed[0]["url"] == "https://real.test/a",
        str(baidu_parsed[:1]),
    )

    baidu_sdata = (
        '<div class="result c-container"><h3>'
        '<a href="http://www.baidu.com/link?url=x">T8</a></h3>'
        '<!--s-data:{"summaryData":{"generalLines":['
        '{"data":[{"text":"<em>北京</em>英语摘要"}]}]}}-->'
        '<span mu="https://real.test/b"></span></div></body>'
    )
    sdata_parsed = w._parse_baidu(baidu_sdata)
    check(
        "baidu: s-data summary",
        len(sdata_parsed) == 1 and "北京英语摘要" in sdata_parsed[0]["snippet"],
        str(sdata_parsed)[:160],
    )
    check(
        "baidu: no comment leakage",
        bool(sdata_parsed) and "summaryData" not in sdata_parsed[0]["snippet"],
        str(sdata_parsed[:1]),
    )

    sogou_div = (
        '<div class="vrwrap"><h3 class="vr-title">'
        '<a href="https://x.test/9">T9</a></h3>'
        "<style>.x{color:red}</style>"
        '<div class="fz-mid space-txt base-ellipsis">S9 摘要</div></div>'
    )
    sogou_parsed = w._parse_sogou(sogou_div)
    check(
        "sogou: div snippet",
        len(sogou_parsed) == 1 and "S9" in sogou_parsed[0]["snippet"],
        str(sogou_parsed)[:140],
    )
    check(
        "sogou: style stripped",
        bool(sogou_parsed) and "color" not in sogou_parsed[0]["snippet"],
        str(sogou_parsed[:1]),
    )

    check(
        "filter: engine pages dropped",
        w._is_engine_page("https://www.sogou.com/sogou?query=x")
        and w._is_engine_page("https://www.baidu.com/s?wd=x")
        and w._is_engine_page("https://www.bing.com/search?q=x"),
    )
    check(
        "filter: content pages kept",
        not w._is_engine_page("https://baike.baidu.com/item/x")
        and not w._is_engine_page("https://www.baidu.com/link?url=x")
        and not w._is_engine_page("https://zhuanlan.zhihu.com/p/1"),
    )

    # System proxy (macOS _scproxy + env) must reach the urllib opener.
    old_proxy = os.environ.get("HTTPS_PROXY")
    try:
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
        w.reset_http_state()
        status = w.proxy_status()
        check(
            "proxy: system/env read",
            status.get("https") == "http://127.0.0.1:7897",
            str(status),
        )
        opener = w._opener("page")
        handlers = [
            h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)
        ]
        check(
            "proxy: opener uses proxy",
            bool(handlers)
            and handlers[0].proxies.get("https") == "http://127.0.0.1:7897",
            str(handlers[0].proxies if handlers else None),
        )
        original_map = h._proxy_map
        h._proxy_map = lambda: {"https": "http://user:pass@127.0.0.1:7897"}
        check(
            "proxy: credentials masked",
            "user:pass" not in str(w.proxy_status()),
            str(w.proxy_status()),
        )
        h._proxy_map = original_map
    finally:
        if old_proxy is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = old_proxy
        w.reset_http_state()

    # websearch() must degrade to diagnostics instead of raising when pages fail.
    original_report, original_fetch = w.search_report, w.fetch_markdown
    try:
        w.search_report = lambda *a, **k: (
            [
                {
                    "title": "T",
                    "url": "https://x.test/1",
                    "snippet": "S",
                    "engine": "fake",
                }
            ],
            {"engines": {"fake": {"status": "ok"}}},
        )
        w.fetch_markdown = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403"))
        payload = w.websearch("q", limit=1, fetch_top=1)
        check(
            "fallback: page error reported",
            payload["pages"] == []
            and payload.get("page_errors", [{}])[0].get("error")
            and "all page fetches failed" in payload.get("page_error", ""),
            str(payload)[:160],
        )
    finally:
        w.search_report, w.fetch_markdown = original_report, original_fetch

    try:
        w.search_report = lambda *a, **k: (
            [],
            {
                "engines": {
                    "bing": {"status": "failed", "error": "TimeoutError: timed out"},
                    "sogou": {"status": "failed", "error": "Blocked: challenge page"},
                },
                "all_failed": True,
                "no_engines_available": False,
            },
        )
        payload = w.websearch("q", fetch_top=0)
        check(
            "failure: all engines message",
            "all search engines failed" in payload.get("error", "")
            and "bing" in payload.get("error", ""),
            str(payload)[:200],
        )
        check(
            "failure: engine report included",
            "bing" in payload.get("engine_report", {}),
            str(payload.get("engine_report")),
        )

        w.search_report = lambda *a, **k: (
            [],
            {"engines": {}, "all_failed": False, "no_engines_available": True},
        )
        payload = w.websearch("q", fetch_top=0)
        check(
            "failure: no engine available",
            "no search engine available" in payload.get("error", ""),
            str(payload)[:160],
        )
    finally:
        w.search_report = original_report

    print("\n[layers] read_url")
    check(
        "read: invalid url",
        r.read_url("not-a-url").get("error_code") == "invalid_url",
    )
    check(
        "read: pdf rejected",
        r.read_url("https://x.test/paper.pdf").get("error_code")
        == "unsupported_format",
    )
    check(
        "read: feishu rejected",
        r.read_url("https://acme.feishu.cn/docx/abc").get("error_code")
        == "unsupported_site",
    )

    original_local, original_proxy = r._fetch_local, r._fetch_proxy
    captured: list[str] = []
    article = "# Title\n\n" + "\n".join(f"line {i}" for i in range(20))
    try:

        def fake_local(url):
            captured.append(url)
            return "https://x.test/final", article

        r._fetch_local = fake_local
        r._fetch_proxy = lambda url, base: (_ for _ in ()).throw(
            AssertionError("proxy must not be called")
        )
        ok = r.read_url("https://github.com/o/r/blob/main/README.md")
        check(
            "read: github blob -> raw",
            captured
            and captured[0] == "https://raw.githubusercontent.com/o/r/main/README.md",
            str(captured[:1]),
        )
        check("read: local success", ok.get("ok") is True and ok.get("tier") == "local")

        r._fetch_local = lambda url: (url, "Sign in to continue")
        blocked = r.read_url("https://x.test/private")
        check(
            "read: login shell fails without proxy",
            blocked.get("ok") is False
            and blocked.get("error_code") == "all_tiers_failed"
            and any(a.get("status") == "shell" for a in blocked.get("attempts", [])),
            str(blocked)[:200],
        )

        r._fetch_proxy = lambda url, base: (url, article)
        proxied = r.read_url("https://x.test/private", use_proxy=True)
        check(
            "read: proxy fallback",
            proxied.get("ok") is True and proxied.get("tier") == "defuddle",
            str(proxied)[:160],
        )

        r._fetch_local = lambda url: (url, "short")
        thin = r.read_url("https://x.test/short")
        check(
            "read: thin content warns",
            thin.get("ok") is True and bool(thin.get("warnings")),
            str(thin)[:160],
        )
    finally:
        r._fetch_local, r._fetch_proxy = original_local, original_proxy


def live() -> None:
    print("\n[web_search] live checks")
    queries = ["Python asyncio", "端到端原则", "Rust ownership", "OpenAI"]
    for round_index in (1, 2):
        print(f" round {round_index}")
        for query in queries:
            started = time.monotonic()
            results = w.search(query, min_results=3)
            elapsed = time.monotonic() - started
            engines = sorted({r["engine"] for r in results})
            print(
                f"  {query!r}: {len(results)} results, {elapsed:.1f}s, "
                f"engines={engines}"
            )
            check(f"live: {query!r} has results", bool(results))
            time.sleep(1.0)

    try:
        page = w.fetch_markdown("https://httpbin.org/html", max_chars=2000)
        page_ok = "Moby-Dick" in page["markdown"] and "<html" not in page["markdown"]
        page_detail = page["markdown"][:120]
    except Exception as exc:
        page_ok = False
        page_detail = f"{type(exc).__name__}: {exc}"
    check("live: page markdown", page_ok, page_detail)

    payload = w.websearch("Python asyncio", limit=3, fetch_top=1, max_chars=1500)
    check("live: websearch results", bool(payload["results"]), str(payload)[:160])
    check(
        "live: websearch page or error",
        bool(payload["pages"]) or bool(payload.get("page_errors")),
        str(payload.get("page_errors", []))[:160],
    )

    junk = w.search("zzzz-no-such-query-12345", min_results=1)
    check("live: gibberish query filtered", junk == [], f"{len(junk)} results")

    print("\n[read_url] live checks")
    live_reads = (
        ("wikipedia", "https://en.wikipedia.org/wiki/Representational_state_transfer"),
        ("mdn", "https://developer.mozilla.org/en-US/docs/Web/HTTP"),
        (
            "github raw",
            "https://raw.githubusercontent.com/python/cpython/main/README.rst",
        ),
        ("example thin", "https://example.com/"),
    )
    for name, url in live_reads:
        payload = r.read_url(url, max_chars=4000)
        if not payload.get("ok"):
            # Public sites intermittently rate-limit the second request in a
            # suite; one delayed retry separates that from a broken reader.
            time.sleep(3.0)
            payload = r.read_url(url, max_chars=4000)
        check(
            f"live read: {name}",
            payload.get("ok") is True and len(payload.get("markdown", "")) > 100,
            str(payload)[:200],
        )
    missing = r.read_url("https://httpbin.org/status/404")
    check(
        "live read: 404 is perceivable",
        missing.get("ok") is False
        and missing.get("error_code") == "all_tiers_failed"
        and bool(missing.get("attempts")),
        str(missing)[:200],
    )
    social = r.read_url("https://x.com/python")
    check(
        "live read: social login shell rejected",
        social.get("ok") is False
        and any(a.get("status") == "shell" for a in social.get("attempts", [])),
        str(social)[:200],
    )

    print(f"  engine status: {w.engine_status()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also hit real engines")
    args = parser.parse_args()
    selftest()
    if args.live:
        live()
    print()
    if FAILS:
        print(f"[web_search] ✗ {len(FAILS)} failure(s)")
        for item in FAILS:
            print("  -", item)
        return 1
    print("[web_search] ✓ all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
