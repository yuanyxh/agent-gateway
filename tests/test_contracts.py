import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.cli import contracts


class EntryContractTest(unittest.TestCase):
    def test_every_entry_exposes_sync_main(self):
        self.assertEqual(contracts.check_entries(), [])

    def test_export_writes_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = contracts.export(Path(tmp))
            self.assertTrue((target / "env.json").is_file())
            text = (target / "entries.json").read_text(encoding="utf-8")
            self.assertIn('"server"', text)
            self.assertIn("agent_gateway.services.mcp.server", text)

    def test_async_main_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "fake_entry.py"
            module.write_text("async def main():\n    pass\n", encoding="utf-8")
            with mock.patch.object(contracts, "_module_path", lambda _m: module):
                problems = contracts.check_entries()
        self.assertTrue(any("must be sync" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
