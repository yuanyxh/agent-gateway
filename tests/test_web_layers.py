"""Layer tests for search / fetch / Markdown conversion / built-in read.

Offline and deterministic: the fetch tests use a local HTTP server, and every
network-dependent path in the read service is monkeypatched. Counterexamples
(malformed HTML, login shells, empty pages, 202 challenge, all-tier failure)
are first-class cases because the agent must be able to tell them apart from a
successful read.
"""

import gzip
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.fetch import http
from agent_gateway.services.markdown import html as md
from agent_gateway.services.read import read_service as read
from agent_gateway.services.search import engines
from agent_gateway.services.search import web_search as facade

ARTICLE_HTML = """<!doctype html>
<html><head><title>Reliable Systems</title>
<style>.x{color:red}</style><script>alert("nope")</script></head>
<body>
<nav>menu junk</nav>
<article>
<h1>Reliable Systems</h1>
<p>A retry loop without a deadline is an outage generator.</p>
<h2>Two rules</h2>
<ul><li>Bound every attempt</li><li>Make failures visible</li></ul>
<ol><li>first</li><li>second</li></ol>
<pre><code>for attempt in range(3): pass</code></pre>
<table><tr><th>Layer</th><th>Owner</th></tr>
<tr><td>search</td><td>engines</td></tr>
<tr><td>fetch</td><td>http</td></tr></table>
<p>Read the <a href="/details">details</a> before changing the cascade.</p>
<p>失败必须可被代理感知，而不是静默返回空内容。</p>
<p>Extra paragraph one.</p><p>Extra paragraph two.</p>
<p>Extra paragraph three.</p>
</article>
<footer>footer junk</footer>
</body></html>"""

SHELL_HTML = """<html><body><h1>Sign in to continue</h1>
<p>Please sign in to read this article.</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    flaky_hits = 0

    def do_GET(self):
        if self.path == "/article":
            self._send(200, ARTICLE_HTML.encode("utf-8"))
        elif self.path == "/shell":
            self._send(200, SHELL_HTML.encode("utf-8"))
        elif self.path == "/empty":
            self._send(200, b"")
        elif self.path == "/js-notice":
            body = ARTICLE_HTML.replace(
                "<body>", "<body><noscript>Enable JavaScript to continue</noscript>"
            )
            self._send(200, body.encode("utf-8"))
        elif self.path == "/gzip":
            body = gzip.compress(ARTICLE_HTML.encode("utf-8"))
            self._send(200, body, {"Content-Encoding": "gzip"})
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/article")
            self.end_headers()
        elif self.path == "/challenge":
            self._send(202, b"Verify you are human")
        elif self.path == "/flaky":
            Handler.flaky_hits += 1
            if Handler.flaky_hits == 1:
                self._send(500, b"temporary")
            else:
                self._send(200, ARTICLE_HTML.encode("utf-8"))
        elif self.path == "/always-500":
            self._send(500, b"down")
        else:
            self._send(404, b"missing")

    def _send(self, status, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        # Never consult the machine's system proxy, and do not spend real time
        # on per-host spacing (rate limiting has its own check in check-web-search).
        self.proxy_patch = patch.object(http, "_proxy_map", lambda: {})
        self.rate_patch = patch.object(http, "respect_rate", lambda *a, **k: None)
        self.proxy_patch.start()
        self.rate_patch.start()
        http.reset_http_state()

    def tearDown(self):
        self.rate_patch.stop()
        self.proxy_patch.stop()
        http.reset_http_state()


class MarkdownStructureTest(unittest.TestCase):
    def test_article_structure(self):
        text = md.html_to_markdown(ARTICLE_HTML, base_url="https://example.com/root")
        self.assertIn("# Reliable Systems", text)
        self.assertIn("## Two rules", text)
        self.assertIn("- Bound every attempt", text)
        self.assertIn("1. first", text)
        self.assertIn("```", text)
        self.assertIn("| Layer | Owner |", text)
        self.assertIn("https://example.com/details", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("menu junk", text)
        self.assertNotIn("footer junk", text)

    def test_malformed_html_does_not_raise(self):
        text = md.html_to_markdown("<p>unclosed<div><script>x<table><tr><td>a")
        self.assertIsInstance(text, str)
        self.assertNotIn("<div>", text)

    def test_empty_and_whitespace(self):
        self.assertEqual(md.html_to_markdown(""), "")
        self.assertEqual(md.html_to_markdown("   \n\t"), "")

    def test_uneven_table_columns_are_padded(self):
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>"
        text = md.html_to_markdown(html)
        self.assertIn("| A | B |", text)
        self.assertIn("| 1 | |", text)

    def test_truncation_reports_boundary(self):
        text, truncated = md.truncate_markdown("line\n" * 1000, 100)
        self.assertTrue(truncated)
        self.assertIn("truncated", text)
        self.assertLess(len(text), 200)

    def test_structural_wrapper_with_menu_class_keeps_content(self):
        html = (
            '<html class="feature-main-menu-disabled"><body>'
            "<article><p>Body survives wrapper classes.</p></article>"
            "</body></html>"
        )
        text = md.html_to_markdown(html)
        self.assertIn("Body survives wrapper classes", text)

    def test_chinese_content_survives(self):
        text = md.html_to_markdown("<h1>失败可感知</h1><p>不要静默返回空内容。</p>")
        self.assertIn("失败可感知", text)
        self.assertIn("不要静默返回空内容", text)


class EngineParserTest(unittest.TestCase):
    def test_parsers_return_empty_on_unknown_markup(self):
        for name, parser in engines._PARSERS.items():
            with self.subTest(engine=name):
                self.assertEqual(parser("<html><body>no results</body></html>"), [])

    def test_bing_counterexample_rejects_javascript_href(self):
        raw = '<li class="b_algo"><h2><a href="javascript:void(0)">Bad</a></h2></li>'
        self.assertEqual(engines._parse_bing(raw), [])

    def test_baidu_drops_ads_and_keeps_mu_target(self):
        raw = (
            '<div class="result c-container"><h3><a href="https://www.baidu.com/link?url=x">'
            'Real</a></h3><span mu="https://example.com/real"></span></div>'
            '<div class="result c-container"><h3><a href="https://www.baidu.com/baidu.php?url=ad">'
            "Ad</a></h3></div>"
        )
        parsed = engines._parse_baidu(raw)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["url"], "https://example.com/real")

    def test_ascii_tokens_match_whole_words_only(self):
        ranked = engines._score_results(
            [
                {
                    "title": "Denmark 123456 password",
                    "url": "https://x.test/1",
                    "snippet": "",
                }
            ],
            "zzzz-no-such-query-12345",
        )
        self.assertLess(ranked[0]["score"], engines._MIN_SCORE)

    def test_single_common_word_cannot_carry_multiword_query(self):
        ranked = engines._score_results(
            [{"title": "No Zzz for Sims", "url": "https://x.test/1", "snippet": ""}],
            "zzzz-no-such-query-12345",
        )
        self.assertLess(ranked[0]["score"], engines._MIN_SCORE)

    def test_cjk_bigram_still_matches_substring(self):
        ranked = engines._score_results(
            [{"title": "网络地址转换", "url": "https://x.test/1", "snippet": ""}],
            "网络NAT",
        )
        self.assertGreater(ranked[0]["score"], 0)

    def test_engine_page_filter(self):
        self.assertTrue(engines._is_engine_page("https://www.bing.com/search?q=x"))
        self.assertFalse(engines._is_engine_page("https://example.com/article"))

    def test_jump_to_engine_page_is_dropped_after_resolution(self):
        sogou = (
            '<div class="vrwrap"><h3 class="vr-title">'
            '<a href="/link?url=x">test result</a></h3>'
            '<div class="space-txt">test snippet</div></div>'
        )
        with (
            patch.object(engines, "_get", lambda *a, **k: ("", sogou, 200)),
            patch.object(
                engines,
                "fetch",
                lambda *a, **k: (
                    "location.replace('https://www.sogou.com/web?query=test')"
                ),
            ),
        ):
            results, _ = engines.search_report("test", engines=("sogou",))
        self.assertEqual(results, [])

    def test_knowledge_source_filter(self):
        from agent_gateway.services.knowledge import knowledge_engine

        self.assertFalse(
            knowledge_engine._usable_source(
                "https://www.sogou.com/web?query=test site:blog.csdn.net"
            )
        )
        self.assertTrue(
            knowledge_engine._usable_source("https://zh.wikipedia.org/wiki/Test")
        )


class FetchLayerTest(LocalServerTest):
    def test_plain_article(self):
        final, text, status = http.get_text(self.base + "/article")
        self.assertEqual(status, 200)
        self.assertTrue(final.endswith("/article"))
        self.assertIn("Reliable Systems", text)

    def test_gzip_decoded(self):
        _, text, _ = http.get_text(self.base + "/gzip")
        self.assertIn("Reliable Systems", text)

    def test_redirect_final_url(self):
        final, text, _ = http.get_text(self.base + "/redirect")
        self.assertTrue(final.endswith("/article"))
        self.assertIn("Reliable Systems", text)

    def test_challenge_page_raises_blocked(self):
        with self.assertRaises(http.Blocked):
            http.get_text(self.base + "/challenge", retries=0)

    def test_page_fetch_can_skip_marker_detection(self):
        # Wikipedia contains "Enable JavaScript" in a noscript block. Treating
        # that as a challenge made a healthy article impossible to read.
        _, text, _ = http.get_text(
            self.base + "/js-notice", retries=0, detect_challenge=False
        )
        self.assertIn("Reliable Systems", text)

    def test_missing_page_raises(self):
        with self.assertRaises(Exception):
            http.get_text(self.base + "/missing", retries=0)

    def test_retry_recovers_from_transient_500(self):
        Handler.flaky_hits = 0
        with patch.object(http, "_retry_delay", lambda *_: 0):
            _, text, status = http.get_text(self.base + "/flaky", retries=1)
        self.assertEqual(status, 200)
        self.assertIn("Reliable Systems", text)

    def test_retry_gives_up_on_persistent_500(self):
        with patch.object(http, "_retry_delay", lambda *_: 0):
            with self.assertRaises(Exception):
                http.get_text(self.base + "/always-500", retries=1)


class ReadServiceTest(LocalServerTest):
    def test_local_article_success(self):
        result = read.read_url(self.base + "/article")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "local")
        self.assertEqual(result["title"], "Reliable Systems")
        self.assertIn("失败必须可被代理感知", result["markdown"])
        self.assertFalse(result["truncated"])

    def test_short_social_login_shell_fails(self):
        shell = "<html><body><h1>登录</h1><p>注册以查看此内容</p></body></html>"
        with patch.object(
            read, "_fetch_local", lambda url: (url, md.html_to_markdown(shell))
        ):
            result = read.read_url("https://x.com/someone")
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"][0]["status"], "shell")

    def test_noscript_javascript_notice_is_not_a_shell(self):
        result = read.read_url(self.base + "/js-notice")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("Enable JavaScript", result["markdown"])

    def test_empty_page_fails_with_attempts(self):
        result = read.read_url(self.base + "/empty")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "all_tiers_failed")
        self.assertEqual(result["attempts"][0]["status"], "empty")
        self.assertIn("hint", result)

    def test_login_shell_fails_without_proxy(self):
        with patch.object(read, "_fetch_proxy") as proxy:
            result = read.read_url(self.base + "/shell")
        proxy.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"][0]["status"], "shell")

    def test_login_shell_uses_proxy_when_opted_in(self):
        body = "# Recovered\n\n" + "\n".join(f"line {i}" for i in range(20))

        def fake_proxy(url, base):
            return url, body

        with patch.object(read, "_fetch_proxy", fake_proxy):
            result = read.read_url(self.base + "/shell", use_proxy=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "defuddle")
        self.assertIn("Recovered", result["markdown"])

    def test_all_proxy_tiers_fail(self):
        def broken(url, base):
            raise RuntimeError("blocked")

        with patch.object(read, "_fetch_proxy", broken):
            result = read.read_url(self.base + "/shell", use_proxy=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "all_tiers_failed")
        self.assertEqual(
            [a["tier"] for a in result["attempts"]], ["local", "defuddle", "jina"]
        )

    def test_thin_page_success_with_warning(self):
        body = "<html><body><p>short</p></body></html>"
        with patch.object(
            read, "_fetch_local", lambda url: (url, md.html_to_markdown(body))
        ):
            result = read.read_url("https://example.com/short")
        self.assertTrue(result["ok"])
        self.assertIn("warnings", result)

    def test_pdf_rejected_before_network(self):
        with patch.object(read, "_fetch_local") as fetch:
            result = read.read_url("https://example.com/paper.pdf")
        fetch.assert_not_called()
        self.assertEqual(result["error_code"], "unsupported_format")

    def test_feishu_rejected(self):
        self.assertEqual(
            read.read_url("https://acme.feishu.cn/docx/abc")["error_code"],
            "unsupported_site",
        )

    def test_feishu_lookalike_host_is_not_rejected(self):
        body = "# Article\n\n" + "\n".join(f"line {i}" for i in range(20))
        with patch.object(read, "_fetch_local", lambda url: (url, body)):
            result = read.read_url("https://notfeishu.cn/doc")
        self.assertTrue(result["ok"])

    def test_invalid_url_rejected(self):
        for url in ("", "ftp://example.com/x", "example.com"):
            with self.subTest(url=url):
                self.assertEqual(read.read_url(url)["error_code"], "invalid_url")

    def test_github_blob_rewritten_to_raw(self):
        captured = []
        body = "# Readme\n\n" + "\n".join(f"line {i}" for i in range(20))

        def fake_local(url):
            captured.append(url)
            return url, body

        with patch.object(read, "_fetch_local", fake_local):
            result = read.read_url("https://github.com/o/r/blob/main/docs/a.md")
        self.assertTrue(result["ok"])
        self.assertEqual(
            captured[0], "https://raw.githubusercontent.com/o/r/main/docs/a.md"
        )

    def test_default_is_unlimited(self):
        body = "# Long\n\n" + "\n".join(f"line {i}" for i in range(5000))
        with patch.object(read, "_fetch_local", lambda url: (url, body)):
            result = read.read_url("https://example.com/long")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["markdown"], body)

    def test_zero_max_chars_means_unlimited(self):
        body = "# Long\n\n" + "\n".join(f"line {i}" for i in range(5000))
        with patch.object(read, "_fetch_local", lambda url: (url, body)):
            result = read.read_url("https://example.com/long", max_chars=0)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["markdown"], body)

    def test_large_max_chars_is_not_capped(self):
        body = "# Long\n\n" + "\n".join(f"line {i}" for i in range(5000))
        with patch.object(read, "_fetch_local", lambda url: (url, body)):
            result = read.read_url("https://example.com/long", max_chars=500000)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["markdown"], body)

    def test_normalize_max_chars(self):
        self.assertEqual(read.normalize_max_chars(0), 0)
        self.assertEqual(read.normalize_max_chars(-1), 0)
        self.assertEqual(read.normalize_max_chars(500), 1000)
        self.assertEqual(read.normalize_max_chars(20000), 20000)
        self.assertEqual(read.normalize_max_chars(500000), 500000)

    def test_truncation_is_explicit(self):
        body = "# Long\n\n" + "\n".join(f"line {i}" for i in range(2000))
        with patch.object(read, "_fetch_local", lambda url: (url, body)):
            result = read.read_url("https://example.com/long", max_chars=1000)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["chars"], 1100)

    def test_failure_payload_is_agent_readable(self):
        def broken(url):
            raise ConnectionError("dns failure")

        with patch.object(read, "_fetch_local", broken):
            result = read.read_url("https://example.com/nope")
        self.assertIn("ok", result)
        self.assertIn("error_code", result)
        self.assertIn("message", result)
        self.assertIn("attempts", result)


class FacadeCompatibilityTest(unittest.TestCase):
    def test_markdown_alias_points_at_layer(self):
        self.assertIs(facade.html_to_markdown, md.html_to_markdown)

    def test_websearch_uses_patched_report_and_fetch(self):
        # The facade's globals are what websearch() calls; this is the contract
        # the offline check script relies on.
        report = (
            [{"title": "T", "url": "https://x.test/1", "snippet": "S", "engine": "e"}],
            {"engines": {"e": {"status": "ok"}}},
        )
        original_report, original_fetch = facade.search_report, facade.fetch_markdown
        try:
            facade.search_report = lambda *a, **k: report
            facade.fetch_markdown = lambda *a, **k: {
                "url": "https://x.test/1",
                "markdown": "# X",
                "chars": 3,
            }
            payload = facade.websearch("q", limit=1, fetch_top=1)
            self.assertEqual(payload["pages"][0]["markdown"], "# X")
        finally:
            facade.search_report, facade.fetch_markdown = (
                original_report,
                original_fetch,
            )

    def test_unknown_engine_is_reported(self):
        results, report = engines.search_report("q", engines=("nope",))
        self.assertEqual(results, [])
        self.assertEqual(report["engines"]["nope"]["status"], "unknown_engine")

    def test_facade_search_report_not_shadowed_by_engines_arg(self):
        # Regression: the module was once imported as `engines` while the public
        # argument is also named `engines`, so calling with that argument crashed.
        results, report = facade.search_report("q", engines=("nope",))
        self.assertEqual(results, [])
        self.assertEqual(report["engines"]["nope"]["status"], "unknown_engine")


if __name__ == "__main__":
    unittest.main()
