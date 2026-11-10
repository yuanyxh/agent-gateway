"""URL fingerprints shared by tool results and retrieval traces."""

import unittest

from agent_gateway.core.url_identity import url_id


class UrlIdentityTest(unittest.TestCase):
    def test_ignores_fragment(self):
        self.assertEqual(
            url_id("https://a.example/x#top"), url_id("https://a.example/x")
        )

    def test_length_and_distinctness(self):
        first = url_id("https://a.example/x")
        second = url_id("https://a.example/y")
        self.assertEqual(len(first), 16)
        self.assertNotEqual(first, second)

    def test_blank_input(self):
        self.assertEqual(url_id(""), "")
        self.assertEqual(url_id("   "), "")


if __name__ == "__main__":
    unittest.main()
