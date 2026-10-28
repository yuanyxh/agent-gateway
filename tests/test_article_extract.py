import unittest
from unittest.mock import patch

from agent_gateway.services.markdown import article

BODY = (
    "<html><body><nav>导航菜单</nav><article><p>"
    + "正文内容。" * 60
    + "</p></article></body></html>"
)


class ArticleExtractTest(unittest.TestCase):
    def test_uses_extractor_when_it_returns_text(self):
        text = "# 标题\n\n" + "正文" * 120
        with patch.object(article, "_extract", lambda *a, **k: text):
            out = article.extract_article(BODY)
        self.assertTrue(out.startswith("# 标题"))

    def test_falls_back_when_extractor_returns_short_text(self):
        with patch.object(article, "_extract", lambda *a, **k: "短"):
            out = article.extract_article(BODY)
        self.assertIn("正文内容", out)
        self.assertNotIn("导航菜单", out)

    def test_falls_back_when_extractor_raises(self):
        def boom(*a, **k):
            raise RuntimeError("extract failed")

        with patch.object(article, "_extract", boom):
            out = article.extract_article(BODY)
        self.assertIn("正文内容", out)

    def test_prepends_html_title_when_extract_has_no_heading(self):
        raw = "<html><head><title>页面标题</title></head><body>x</body></html>"
        with patch.object(article, "_extract", lambda *a, **k: "正文" * 120):
            out = article.extract_article(raw)
        self.assertTrue(out.startswith("# 页面标题"))

    def test_keeps_existing_heading(self):
        raw = "<html><head><title>页面标题</title></head><body>x</body></html>"
        text = "# 已有标题\n\n" + "正文" * 120
        with patch.object(article, "_extract", lambda *a, **k: text):
            out = article.extract_article(raw)
        self.assertTrue(out.startswith("# 已有标题"))
        self.assertNotIn("页面标题", out)

    def test_truncates_extracted_text(self):
        with patch.object(article, "_extract", lambda *a, **k: "x" * 5000):
            out = article.extract_article(BODY, max_chars=3000)
        self.assertIn("truncated", out)
        self.assertLess(len(out), 5000)


if __name__ == "__main__":
    unittest.main()
