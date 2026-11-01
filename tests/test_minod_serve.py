import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_gateway.cli import env_builder, minod  # noqa: E402
from agent_gateway.core import config_loader, daemon, paths  # noqa: E402

PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CheckTest(unittest.TestCase):
    def test_check_passes_with_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(
                f"mcp:\n  port: {_free_port()}\nmodelProxy:\n  port: {_free_port()}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"UTILITY_DATA_DIR": str(root)}):
                config_loader.reset_cache()
                with mock.patch("builtins.print"):
                    self.assertEqual(minod.check(), 0)
                config_loader.reset_cache()


class EnvBuilderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"UTILITY_DATA_DIR": str(self.root)})
        self._env.start()
        config_loader.reset_cache()

    def tearDown(self):
        self._env.stop()
        config_loader.reset_cache()
        self._tmp.cleanup()

    def _write(self, text):
        (self.root / "config.yaml").write_text(text, encoding="utf-8")
        config_loader.reset_cache()

    def test_mcp_environment_defaults(self):
        self._write("mcp:\n  enabled: true\n")
        env = env_builder.mcp_environment(9123)
        self.assertEqual(env["MCP_PORT"], "9123")
        self.assertEqual(env["MEMORY_SERVER_URL"], "http://127.0.0.1:9123")
        self.assertEqual(env["QDRANT_PATH"], str(self.root / "data" / "qdrant"))
        # Derived from the active cache root, not a hardcoded legacy path.
        self.assertEqual(
            Path(env["FASTEMBED_CACHE_PATH"]),
            Path(paths.gateway_cache()) / "fastembed",
        )
        self.assertNotIn("EMBEDDER_MODEL", env)

    def test_mcp_environment_tables(self):
        self._write(
            "mcp:\n"
            "  qdrantPath: /tmp/qd\n"
            "  embedderModel: my-embed\n"
            "  synonyms:\n"
            "    部署: deploy\n"
            "  toolDefaults:\n"
            "    recall:\n"
            "      limit: 3\n"
        )
        env = env_builder.mcp_environment(9001)
        self.assertEqual(env["QDRANT_PATH"], "/tmp/qd")
        self.assertEqual(env["EMBEDDER_MODEL"], "my-embed")
        self.assertEqual(json.loads(env["MCP_SYNONYMS"]), {"部署": "deploy"})
        self.assertEqual(json.loads(env["MCP_TOOL_DEFAULTS"]), {"recall": {"limit": 3}})

    def test_proxy_environment_uses_snake_case_payloads(self):
        self._write(
            "modelProxy:\n"
            "  enabled: true\n"
            "  providers:\n"
            "    - name: p1\n"
            "      url: 'http://x/v1'\n"
            "      apiFormat: anthropic\n"
            "      apiKey: k\n"
            "      models: [m1]\n"
            "  outputFilter:\n"
            "    maxBytes: 500\n"
            "    rules:\n"
            "      - command: npm\n"
            "        maxBytes: 100\n"
        )
        env = env_builder.proxy_environment(8613)
        providers = json.loads(env["MODEL_PROXY_PROVIDERS"])
        self.assertEqual(
            providers,
            [
                {
                    "name": "p1",
                    "url": "http://x/v1",
                    "api_format": "anthropic",
                    "api_key": "k",
                    "models": ["m1"],
                }
            ],
        )
        flt = json.loads(env["MODEL_PROXY_OUTPUT_FILTER"])
        self.assertEqual(flt["max_bytes"], 500)
        self.assertEqual(flt["rules"][0]["command"], "npm")

    def test_proxy_environment_falls_back_to_single_url(self):
        self._write("modelProxy:\n  enabled: true\n  url: 'http://legacy/v1'\n")
        env = env_builder.proxy_environment(8613)
        self.assertEqual(env["MODEL_PROXY_URL"], "http://legacy/v1")
        self.assertNotIn("MODEL_PROXY_PROVIDERS", env)

    def test_switches_are_always_injected(self):
        self._write("modelProxy:\n  enabled: false\n")
        env = env_builder.proxy_environment(8613)
        self.assertEqual(env["MODEL_PROXY_SKILL_DEDUP"], "1")
        self.assertEqual(env["MODEL_PROXY_RESPONSE_NOTE"], "1")
        self.assertEqual(env["MODEL_PROXY_CONTEXT_WARN"], "300000")


class ServeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.port = _free_port()
        (self.root / "config.yaml").write_text(
            "modelProxy:\n"
            "  enabled: true\n"
            f"  port: {self.port}\n"
            "  providers:\n"
            "    - name: fake\n"
            "      url: 'http://127.0.0.1:1/v1'\n"
            "      apiFormat: openai\n"
            "      apiKey: ''\n"
            "      models: [fake]\n",
            encoding="utf-8",
        )
        self.env = dict(
            os.environ,
            UTILITY_DATA_DIR=str(self.root),
            PYTHONPATH=str(ROOT / "src"),
        )
        self.proc = None

    def tearDown(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=10)
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        self._tmp.cleanup()

    def test_serve_records_state_and_cleans_up(self):
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "agent_gateway.cli.minod", "--serve", "--service", "proxy"],
            env=self.env,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            self.assertTrue(
                daemon.wait_until(
                    lambda: daemon.health_ok(f"http://127.0.0.1:{self.port}/health"),
                    60,
                ),
                "proxy did not become healthy",
            )
            state = json.loads((self.root / "runtime" / "proxy.json").read_text())
            self.assertEqual(state["port"], self.port)
            self.assertGreater(state["pid"], 0)
        finally:
            self.proc.terminate()
            self.proc.wait(timeout=30)
        self.assertFalse((self.root / "runtime" / "proxy.json").exists())


if __name__ == "__main__":
    unittest.main()
