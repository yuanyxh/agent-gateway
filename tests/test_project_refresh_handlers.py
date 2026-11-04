"""MCP handlers must schedule graph refresh before serving a query."""

from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcp.server


def _passthrough_decorator(*_args, **_kwargs):
    def wrap(func):
        return func

    return wrap


class ProjectRefreshHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._env_patch = mock.patch.dict(
            os.environ,
            {
                "GATEWAY_HOME": str(Path(cls._tmp.name) / "home"),
                "QDRANT_PATH": str(Path(cls._tmp.name) / "qdrant"),
                "MCP_PORT": "19001",
            },
        )
        cls._env_patch.start()
        cls._list_patch = mock.patch.object(
            mcp.server.Server, "list_tools", _passthrough_decorator, create=True
        )
        cls._call_patch = mock.patch.object(
            mcp.server.Server, "call_tool", _passthrough_decorator, create=True
        )
        cls._list_patch.start()
        cls._call_patch.start()
        cls.server = importlib.import_module("agent_gateway.services.mcp.server")

    @classmethod
    def tearDownClass(cls):
        client = getattr(cls.server, "client", None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        cls._call_patch.stop()
        cls._list_patch.stop()
        cls._env_patch.stop()
        cls._tmp.cleanup()

    def test_lookup_schedules_refresh(self):
        from agent_gateway.services.graph import refresh

        project = Path(self._tmp.name) / "lookup-project"
        project.mkdir()
        with mock.patch.object(refresh, "schedule_project_graph_refresh") as schedule:
            asyncio.run(
                self.server._handle_project_lookup(
                    {"path": str(project), "query": "missing"}
                )
            )
        schedule.assert_called_once_with(str(project))

    def test_impact_schedules_refresh(self):
        from agent_gateway.services.graph import refresh

        project = Path(self._tmp.name) / "impact-project"
        project.mkdir()
        with mock.patch.object(refresh, "schedule_project_graph_refresh") as schedule:
            asyncio.run(
                self.server._handle_project_impact(
                    {"path": str(project), "target": "missing"}
                )
            )
        schedule.assert_called_once_with(str(project))


if __name__ == "__main__":
    unittest.main()
