import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import daemon, ports

PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PortsTest(unittest.TestCase):
    def test_is_available_reflects_bind_state(self):
        port = _free_port()
        self.assertTrue(ports.is_available(port))
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        try:
            self.assertFalse(ports.is_available(port))
        finally:
            server.close()

    def test_find_available_skips_listener(self):
        port = _free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        try:
            found = ports.find_available(port)
            self.assertNotEqual(found, port)
            self.assertEqual(found, port + 1)
        finally:
            server.close()

    def test_listener_pids_reports_only_the_listener(self):
        port = _free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            self.assertEqual(ports.listener_pids(port), [os.getpid()])
        finally:
            client.close()
            server.close()

    def test_terminate_listener_stops_child(self):
        port = _free_port()
        code = (
            "import socket, time\n"
            "s = socket.socket()\n"
            f"s.bind(('127.0.0.1', {port}))\n"
            "s.listen(1)\n"
            "time.sleep(30)\n"
        )
        child = subprocess.Popen([PYTHON, "-c", code])
        try:
            self.assertTrue(daemon.wait_until(lambda: not ports.is_available(port), 5))
            self.assertTrue(ports.terminate_listener(port, timeout=5))
            self.assertTrue(ports.is_available(port))
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


class RuntimeStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"UTILITY_DATA_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_write_read_clear_roundtrip(self):
        path = daemon.write_state(daemon.SERVICE_MCP, 9123, pid=42)
        self.assertEqual(path, Path(self._tmp.name) / "runtime" / "mcp.json")
        state = daemon.read_state(daemon.SERVICE_MCP)
        self.assertEqual(state["port"], 9123)
        self.assertEqual(state["pid"], 42)
        self.assertIn("startedAt", state)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        daemon.clear_state(daemon.SERVICE_MCP)
        self.assertIsNone(daemon.read_state(daemon.SERVICE_MCP))

    def test_missing_root_uses_default_home(self):
        env = {"UTILITY_DATA_DIR": "", "GATEWAY_HOME": self._tmp.name}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(daemon.state_dir(), Path(self._tmp.name) / "runtime")
            self.assertIsNotNone(daemon.write_state(daemon.SERVICE_PROXY, 8613))

    def test_corrupt_state_reads_as_none(self):
        daemon.write_state(daemon.SERVICE_MCP, 9001)
        daemon.state_path(daemon.SERVICE_MCP).write_text("{oops", encoding="utf-8")
        self.assertIsNone(daemon.read_state(daemon.SERVICE_MCP))


class _QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_health_ok_only_accepts_2xx(self):
        self.assertTrue(daemon.health_ok(self.base + "/"))
        self.assertFalse(daemon.health_ok(self.base + "/missing"))

    def test_health_ok_false_when_unreachable(self):
        self.assertFalse(
            daemon.health_ok(f"http://127.0.0.1:{_free_port()}/", timeout=0.3)
        )

    def test_wait_until_times_out(self):
        started = time.monotonic()
        self.assertFalse(daemon.wait_until(lambda: False, 0.3, interval=0.05))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)


class SignalHandlerTest(unittest.TestCase):
    def tearDown(self):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)

    def test_installs_handlers(self):
        stop = threading.Event()
        daemon.install_stop_handlers(stop)
        handler = signal.getsignal(signal.SIGTERM)
        self.assertTrue(callable(handler))
        handler(signal.SIGTERM, None)
        self.assertTrue(stop.is_set())


if __name__ == "__main__":
    unittest.main()
