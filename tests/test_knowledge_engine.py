import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.knowledge import knowledge_engine as engine


class GatherEvidenceTest(unittest.TestCase):
    def test_sources_keep_titles_and_drop_duplicates(self):
        wiki_hit = {
            "extract": "维基" * 350,
            "url": "https://zh.wikipedia.org/wiki/A",
            "title": "维基标题",
        }
        hits = [
            {"url": "https://example.com/a", "title": "示例标题", "snippet": "s"},
            {"url": "https://example.com/a", "title": "重复", "snippet": "s"},
        ]
        with (
            patch.object(engine, "wiki", return_value=wiki_hit),
            patch.object(engine, "web_search_query", return_value=hits),
            patch.object(engine, "_page_text", return_value="正文" * 300),
        ):
            ev = engine.gather_evidence("概念", "主题")
        self.assertIsNotNone(ev)
        self.assertEqual(
            ev["urls"],
            [
                {"url": "https://zh.wikipedia.org/wiki/A", "title": "维基标题"},
                {"url": "https://example.com/a", "title": "示例标题"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
