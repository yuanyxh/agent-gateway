import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_gateway.core import config, config_loader, paths


def _resolved(value: str) -> Path:
    return Path(os.path.abspath(value))


class PathsTest(unittest.TestCase):
    def test_gateway_home_wins_over_legacy(self):
        env = {"GATEWAY_HOME": "/tmp/gh", "UTILITY_DATA_DIR": "/tmp/ud"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.gateway_home(), _resolved("/tmp/gh"))

    def test_legacy_data_dir_is_honored(self):
        with patch.dict(os.environ, {"UTILITY_DATA_DIR": "/tmp/ud"}, clear=True):
            self.assertEqual(paths.gateway_home(), _resolved("/tmp/ud"))

    def test_default_home_is_agent_gateway(self):
        with patch.dict(os.environ, {}, clear=True):
            expected = Path.home() / ".config" / "agent-gateway"
            self.assertEqual(paths.gateway_home(), expected.resolve())

    def test_gateway_cache_env_wins(self):
        with patch.dict(os.environ, {"GATEWAY_CACHE": "/tmp/gc"}, clear=True):
            self.assertEqual(paths.gateway_cache(), _resolved("/tmp/gc"))

    def test_data_dir_prefers_gateway_home(self):
        with patch.dict(os.environ, {"GATEWAY_HOME": "/tmp/gh"}, clear=True):
            self.assertEqual(config.data_dir(), "/tmp/gh")


class ConfigFileTest(unittest.TestCase):
    def test_defaults_to_home_config(self):
        with patch.dict(os.environ, {"GATEWAY_HOME": "/tmp/gh"}, clear=True):
            self.assertEqual(
                config_loader.config_file(), _resolved("/tmp/gh") / "config.yaml"
            )

    def test_gateway_config_wins(self):
        env = {"GATEWAY_CONFIG": "/tmp/x/custom.yaml", "GATEWAY_HOME": "/tmp/gh"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                config_loader.config_file(), _resolved("/tmp/x/custom.yaml")
            )


if __name__ == "__main__":
    unittest.main()
