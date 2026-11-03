"""Embedder load / idle-unload lifecycle."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import numpy as np

from agent_gateway.services.mcp import server_embed


class _FakeEmbedding:
    instances: list = []

    def __init__(self, **_kwargs):
        _FakeEmbedding.instances.append(self)

    def embed(self, documents, **_kwargs):
        for _ in documents:
            yield np.zeros(4, dtype=np.float32)


class EmbedderLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._saved = (
            server_embed._shared_embedder,
            server_embed._embedder_users,
            server_embed._embedder_last_used,
            server_embed.IDLE_UNLOAD_SECONDS,
            server_embed._unload_thread,
        )
        server_embed._shared_embedder = None
        server_embed._embedder_users = 0
        server_embed._embedder_last_used = 0.0
        server_embed.IDLE_UNLOAD_SECONDS = 60
        # A non-None value keeps `_ensure_unload_thread` from starting a real timer.
        server_embed._unload_thread = threading.Thread()
        _FakeEmbedding.instances.clear()
        self._patch = mock.patch.object(server_embed, "TextEmbedding", _FakeEmbedding)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        (
            server_embed._shared_embedder,
            server_embed._embedder_users,
            server_embed._embedder_last_used,
            server_embed.IDLE_UNLOAD_SECONDS,
            server_embed._unload_thread,
        ) = self._saved

    def test_loads_once_across_sessions(self):
        with server_embed.embedder_session() as first:
            self.assertIsNotNone(first)
        with server_embed.embedder_session() as second:
            self.assertIs(first, second)
        self.assertEqual(len(_FakeEmbedding.instances), 1)

    def test_does_not_unload_while_a_session_is_active(self):
        with server_embed.embedder_session() as emb:
            self.assertFalse(server_embed.unload_if_idle(now=10**9))
            self.assertIs(emb, server_embed._shared_embedder)

    def test_unloads_after_idle_and_reloads_on_demand(self):
        with server_embed.embedder_session():
            pass
        last_used = server_embed._embedder_last_used
        self.assertTrue(server_embed.unload_if_idle(now=last_used + 61))
        self.assertIsNone(server_embed._shared_embedder)
        with server_embed.embedder_session() as emb:
            self.assertIsNotNone(emb)
        self.assertEqual(len(_FakeEmbedding.instances), 2)

    def test_idle_zero_keeps_the_model(self):
        server_embed.IDLE_UNLOAD_SECONDS = 0
        with server_embed.embedder_session():
            pass
        self.assertFalse(server_embed.unload_if_idle(now=10**9))
        self.assertIsNotNone(server_embed._shared_embedder)

    def test_mem0_handle_borrows_the_shared_model(self):
        handle = server_embed.PooledEmbedder(4)
        self.assertEqual(len(list(handle.embed(["a"]))), 1)
        self.assertEqual(len(_FakeEmbedding.instances), 1)
        self.assertIsNotNone(server_embed._shared_embedder)
