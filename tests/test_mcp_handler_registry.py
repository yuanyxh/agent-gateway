import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import inspect
import unittest

from agent_gateway.services.mcp import server


class HandlerRegistryTest(unittest.TestCase):
    def test_every_handler_is_async_with_one_arguments_param(self):
        """装饰器挂到辅助函数上会让 HANDLERS 指向错误的可调用对象。"""
        self.assertTrue(server.HANDLERS)
        for name, handler in server.HANDLERS.items():
            with self.subTest(tool=name):
                self.assertTrue(
                    inspect.iscoroutinefunction(handler),
                    f"{name} 注册的不是 async handler",
                )
                params = list(inspect.signature(handler).parameters.values())
                self.assertEqual([p.name for p in params], ["arguments"])


if __name__ == "__main__":
    unittest.main()
