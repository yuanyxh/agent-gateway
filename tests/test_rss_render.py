"""Regression tests for the deterministic parts of the RSS digest pipeline."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.rss import rss_render  # noqa: E402


class NormalizeUrlTests(unittest.TestCase):
    def test_strips_query_fragment_and_trailing_slash(self) -> None:
        self.assertEqual(
            rss_render.normalize_url("https://example.com/a/?utm=1#top"),
            "https://example.com/a",
        )

    def test_empty(self) -> None:
        self.assertEqual(rss_render.normalize_url(""), "")


class ArchivePathTests(unittest.TestCase):
    def test_quarter_mapping(self) -> None:
        root = Path("/tmp/rss")
        cases = {
            "2026-01-01": "一季度",
            "2026-03-31": "一季度",
            "2026-04-01": "二季度",
            "2026-07-15": "三季度",
            "2026-10-19": "四季度",
            "2026-12-31": "四季度",
        }
        for day, quarter in cases.items():
            with self.subTest(day=day):
                self.assertEqual(
                    rss_render.archive_path(root, day),
                    root / day[:4] / quarter / f"{day}.md",
                )


class DecorateTitleTests(unittest.TestCase):
    def test_appends_translation_for_english_title(self) -> None:
        self.assertEqual(
            rss_render.decorate_title("Hello World", "你好世界"),
            "Hello World(你好世界)",
        )

    def test_keeps_cjk_title_as_is(self) -> None:
        self.assertEqual(
            rss_render.decorate_title("已经是中文", "already"), "已经是中文"
        )

    def test_skips_identical_translation(self) -> None:
        self.assertEqual(rss_render.decorate_title("Same", "Same"), "Same")


class RenderTests(unittest.TestCase):
    def test_articles_and_filtered_sections(self) -> None:
        text = rss_render.render(
            "2026-10-19",
            [
                {
                    "title": "Title",
                    "url": "https://example.com/a",
                    "title_translation": "标题",
                    "summary": "摘要",
                }
            ],
            [
                {
                    "title": "Skip",
                    "url": "https://example.com/b",
                    "feed": "Feed",
                    "reason": "无长期价值",
                }
            ],
        )
        self.assertIn("# RSS 日报 — 2026-10-19", text)
        self.assertIn("## [Title(标题)](https://example.com/a)", text)
        self.assertIn("> 摘要", text)
        self.assertIn("## 已过滤条目", text)
        self.assertIn("- [Skip](https://example.com/b) — [Feed] 无长期价值", text)
        self.assertEqual(rss_render.validate(text), [])

    def test_empty_articles(self) -> None:
        text = rss_render.render("2026-10-19", [], [])
        self.assertIn("今日无有效内容。", text)

    def test_validate_flags_untranslated_main_entry(self) -> None:
        text = rss_render.render(
            "2026-10-19",
            [
                {
                    "title": "English Only",
                    "url": "https://example.com/a",
                    "title_translation": "",
                    "summary": "s",
                }
            ],
            [],
        )
        issues = rss_render.validate(text)
        self.assertEqual(len(issues), 1)
        self.assertIn("no Chinese translation", issues[0])


class HistoryTests(unittest.TestCase):
    def test_collects_urls_and_honors_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026").mkdir()
            (root / "2026" / "2026-10-18.md").write_text(
                "[a](https://example.com/a/?x=1)\n[b](https://example.com/b)",
                encoding="utf-8",
            )
            (root / "2026" / "2026-10-19.md").write_text(
                "[c](https://example.com/c)", encoding="utf-8"
            )
            urls = rss_render.collect_history_urls(
                root, exclude=root / "2026" / "2026-10-19.md"
            )
            self.assertEqual(urls, {"https://example.com/a", "https://example.com/b"})


if __name__ == "__main__":
    unittest.main()
