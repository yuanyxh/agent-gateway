"""Cold-layer writes carry the source memories' importance."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.summarize import storage  # noqa: E402


class ColdImportanceTest(unittest.TestCase):
    def _post_body(self, source_ids, nodes):
        response = mock.Mock(status_code=200, text="")
        with (
            mock.patch.object(storage, "_cold_already_synced", return_value=False),
            mock.patch.object(storage.memory_store, "get_nodes", return_value=nodes),
            mock.patch.object(storage._HTTP, "post", return_value=response) as post,
        ):
            storage.write_to_cold_layer(
                "summary",
                "notes/a.md",
                "Extracts",
                source_memory_ids=source_ids,
            )
        return post.call_args.kwargs["json"]

    def test_inherits_highest_source_importance(self):
        nodes = {
            "a": {"metadata": {"importance": 8}},
            "b": {"metadata": {"importance": 5}},
        }
        body = self._post_body(["a", "b"], nodes)
        self.assertEqual(body["importance"], 8)

    def test_defaults_to_five_without_sources(self):
        body = self._post_body([], {})
        self.assertEqual(body["importance"], 5)


if __name__ == "__main__":
    unittest.main()
