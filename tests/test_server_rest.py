import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from agent_gateway.services.mcp import server_rest


class ServerRestTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(Starlette(routes=server_rest.REST_ROUTES))

    def test_summarize_rejects_empty_text(self):
        response = self.client.post("/api/summarize", json={"text": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False, "error": "text is required"})

    def test_summarize_clamps_max_tokens(self):
        with patch.object(
            server_rest.summarize_service,
            "summarize_text",
            return_value={"ok": True, "summary": "s"},
        ) as summarize:
            response = self.client.post(
                "/api/summarize", json={"text": "body", "max_tokens": 99999}
            )
        self.assertEqual(response.json()["summary"], "s")
        self.assertEqual(summarize.call_args.args, ("body", "", 4000))

    def test_read_rejects_empty_url(self):
        response = self.client.post("/api/read", json={"url": ""})
        self.assertEqual(response.json()["error_code"], "invalid_url")

    def test_chat_reports_unavailable_llm(self):
        with patch.object(server_rest.llm_client, "chat", return_value=None):
            response = self.client.post("/api/llm/chat", json={"prompt": "hi"})
        self.assertEqual(response.json(), {"ok": False, "error": "llm_unavailable"})

    def test_chat_forwards_disable_thinking_and_extra_body(self):
        with patch.object(server_rest.llm_client, "chat", return_value="ok") as chat:
            response = self.client.post(
                "/api/llm/chat",
                json={
                    "prompt": "hi",
                    "disable_thinking": True,
                    "extra_body": {"thinking": {"type": "disabled"}},
                    "timeout": 30,
                },
            )
        self.assertEqual(response.json(), {"ok": True, "text": "ok"})
        kwargs = chat.call_args.kwargs
        self.assertTrue(kwargs["disable_thinking"])
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(kwargs["timeout"], 30.0)


if __name__ == "__main__":
    unittest.main()
