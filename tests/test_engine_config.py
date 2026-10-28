import os
import unittest
from unittest.mock import patch

from agent_gateway.services.search import engine_config


class EngineConfigTest(unittest.TestCase):
    def test_defaults_drop_providerless_google(self):
        with patch.dict(os.environ, {}, clear=True):
            engines, providers, order = engine_config.load()
        self.assertIn("bing", engines)
        self.assertEqual(providers, {})
        self.assertNotIn("google", order["cjk"])
        self.assertNotIn("google", order["latin"])

    def test_provider_env_enables_google(self):
        env = {
            "GATEWAY_SEARCH_PROVIDERS": (
                '{"google": {"command": ["helper", "websearch", "--query", "{q}"]}}'
            )
        }
        with patch.dict(os.environ, env, clear=True):
            _, providers, order = engine_config.load()
        self.assertIn("google", providers)
        self.assertEqual(providers["google"].command[0], "helper")
        self.assertIn("google", order["cjk"])

    def test_disable_engine(self):
        env = {"GATEWAY_SEARCH_ENGINES": '{"bing": {"enabled": false}}'}
        with patch.dict(os.environ, env, clear=True):
            engines, _, order = engine_config.load()
        self.assertNotIn("bing", engines)
        self.assertNotIn("bing", order["latin"])

    def test_endpoint_override(self):
        env = {"GATEWAY_SEARCH_ENGINES": '{"bing": {"endpoint": "https://x/{q}"}}'}
        with patch.dict(os.environ, env, clear=True):
            engines, _, _ = engine_config.load()
        self.assertEqual(engines["bing"].endpoint, "https://x/{q}")
        self.assertEqual(engines["bing"].parser, "bing")

    def test_order_override(self):
        env = {"GATEWAY_SEARCH_ORDER": '{"latin": ["duckduckgo", "bing"]}'}
        with patch.dict(os.environ, env, clear=True):
            _, _, order = engine_config.load()
        self.assertEqual(order["latin"], ("duckduckgo", "bing"))


if __name__ == "__main__":
    unittest.main()
