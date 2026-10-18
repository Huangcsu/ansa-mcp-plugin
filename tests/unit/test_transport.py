"""Transport constructor failures must not leak a live-looking registration."""

from pathlib import Path
import queue
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.transport import BridgeTransport


class TransportFailureTests(unittest.TestCase):
    def test_offline_bind_is_not_queued_for_later_backend_start(self):
        transport = BridgeTransport.__new__(BridgeTransport)
        transport.state = SimpleNamespace(connected=threading.Event(), instance_id="instance")
        transport.outgoing = queue.Queue()
        with self.assertRaisesRegex(ConnectionError, "MCP 后端未连接"):
            transport.request_bind()
        self.assertTrue(transport.outgoing.empty())
        transport.state.connected.set()
        transport.request_bind()
        self.assertEqual(transport.outgoing.get_nowait()["instance_id"], "instance")

    def test_constructor_failure_removes_registration_and_socket(self):
        root = Path(__file__).resolve().parents[2] / "runtime/unit-test-temp"
        root.mkdir(parents=True, exist_ok=True)
        for failure in ("thread_start", "publish"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=root) as directory:
                state = SimpleNamespace(instance_dir=Path(directory), pid=123, process_start="123",
                                        instance_id="instance", session_id="session")
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                original = RuntimeError("injected " + failure)
                target = "ansa_mcp_bridge.transport.threading.Thread.start" if failure == "thread_start" else "ansa_mcp_bridge.transport.os.replace"
                with patch("ansa_mcp_bridge.transport.socket.socket", return_value=listener), patch(target, side_effect=original):
                    with self.assertRaises(RuntimeError) as caught:
                        BridgeTransport(state, object(), "test-token")
                self.assertIs(caught.exception, original)
                self.assertEqual(listener.fileno(), -1)
                self.assertFalse((state.instance_dir / "connection.json").exists())
                self.assertFalse((state.instance_dir / "connection.tmp").exists())


if __name__ == "__main__":
    unittest.main()
