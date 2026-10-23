"""Focused regressions for bridge connection retries and diagnostics."""

import queue
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ansa_mcp_plugin.backend.connection import BridgeConnection
from ansa_mcp_plugin.backend.service import Service


def registration(instance_id="instance", session_id="session", port=41001):
    return {
        "instance_id": instance_id,
        "session_id": session_id,
        "pid": 1234,
        "process_start": "123456",
        "port": port,
    }


class FakeSocket:
    def __init__(self):
        self.timeouts = []
        self.shutdown_called = False
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


def make_connection(sock=None):
    sock = sock or FakeSocket()
    sent = []

    def send(_sock, message):
        sent.append(message)

    def receive(_sock):
        return {
            "kind": "reply",
            "request_id": sent[0]["request_id"],
            "ok": True,
            "data": {"instance_id": "instance", "session_id": "session"},
        }

    with patch("ansa_mcp_plugin.backend.connection.socket.create_connection", return_value=sock) as create:
        with patch("ansa_mcp_plugin.backend.connection.send_frame", side_effect=send):
            with patch("ansa_mcp_plugin.backend.connection.recv_frame", side_effect=receive):
                with patch("ansa_mcp_plugin.backend.connection.validate", side_effect=lambda message: message):
                    connection = BridgeConnection(registration(), "a" * 64, Mock(), start_reader=False)
    return connection, sock, create


def make_service():
    service = object.__new__(Service)
    service._lock = threading.RLock()
    service.connections = {}
    service._connecting = set()
    service._connection_diagnostics = {}
    service.closed = False
    return service


class BridgeConnectionTimeoutTests(unittest.TestCase):
    def test_connection_and_authentication_have_separate_timeouts(self):
        connection, sock, create = make_connection()
        self.addCleanup(connection.close)

        self.assertEqual(create.call_args.kwargs["timeout"], 2)
        self.assertEqual(sock.timeouts[:2], [5, 5])

    def test_existing_positional_start_reader_argument_remains_compatible(self):
        sock = FakeSocket()
        sent = []
        with patch("ansa_mcp_plugin.backend.connection.socket.create_connection", return_value=sock):
            with patch("ansa_mcp_plugin.backend.connection.send_frame", side_effect=lambda _sock, msg: sent.append(msg)):
                with patch("ansa_mcp_plugin.backend.connection.recv_frame") as receive:
                    receive.side_effect = lambda _sock: {
                        "kind": "reply",
                        "request_id": sent[0]["request_id"],
                        "ok": True,
                        "data": {"instance_id": "instance", "session_id": "session"},
                    }
                    with patch("ansa_mcp_plugin.backend.connection.validate", side_effect=lambda message: message):
                        connection = BridgeConnection(registration(), "a" * 64, Mock(), None, 2, False)

        self.assertIsNone(connection.thread)
        connection.close()

    def test_connect_timeout_is_tagged_and_auth_timeout_closes_socket(self):
        connect_error = socket.timeout("connect deadline")
        with patch("ansa_mcp_plugin.backend.connection.socket.create_connection", side_effect=connect_error):
            with self.assertRaises(socket.timeout) as caught:
                BridgeConnection(registration(), "a" * 64, Mock(), start_reader=False)
        self.assertEqual(caught.exception.connection_stage, "connect")

        sock = FakeSocket()
        with patch("ansa_mcp_plugin.backend.connection.socket.create_connection", return_value=sock):
            with patch("ansa_mcp_plugin.backend.connection.send_frame"):
                with patch("ansa_mcp_plugin.backend.connection.recv_frame", side_effect=socket.timeout("auth deadline")):
                    with patch("ansa_mcp_plugin.backend.connection.validate", side_effect=lambda message: message):
                        with self.assertRaises(socket.timeout) as caught:
                            BridgeConnection(registration(), "a" * 64, Mock(), start_reader=False)
        self.assertEqual(caught.exception.connection_stage, "authentication")
        self.assertTrue(sock.closed)

    def test_reader_exit_closes_socket_and_preserves_disconnect_reason(self):
        connection, sock, _ = make_connection()
        receiver = queue.Queue(maxsize=1)
        connection.pending["pending-id"] = receiver
        with patch("ansa_mcp_plugin.backend.connection.select.select", return_value=([sock], [], [])):
            with patch("ansa_mcp_plugin.backend.connection.recv_frame", side_effect=EOFError("peer closed")):
                connection._reader()

        self.assertTrue(connection.closed)
        self.assertTrue(sock.closed)
        self.assertEqual(connection.failure_stage, "reader")
        self.assertEqual(connection.failure_reason, "peer closed")
        self.assertIn("peer closed", receiver.get_nowait()["error"])


class ServiceConnectionRecoveryTests(unittest.TestCase):
    def test_discovery_and_status_do_not_start_two_handshakes(self):
        service = make_service()
        service.token = 'a' * 64
        service.backend_id = 'backend'
        service._panel_bind = Mock()
        target = registration()
        entered, release = threading.Event(), threading.Event()
        candidate = Mock(registration=target, closed=False)
        outcome = []
        def connect(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test did not release authentication')
            return candidate
        def discover():
            outcome.append(service._connect(target))
        with patch('ansa_mcp_plugin.backend.service.BridgeConnection', side_effect=connect) as factory:
            worker = threading.Thread(target=discover)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(ConnectionError, 'being established'):
                    service._connect(target)
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome, [candidate])
            factory.assert_called_once()
            self.assertIs(service._connect(target), candidate)
            self.assertEqual(service._connecting, set())

    def test_authentication_failure_reaches_status_and_suppresses_early_retry(self):
        service = make_service()
        target = registration()
        service.token = "a" * 64
        service.backend_id = "backend"
        service._panel_bind = Mock()
        service.store = SimpleNamespace(current=lambda: None)
        service.tasks = SimpleNamespace(pending=lambda: [])
        service.last_error = None
        service.dispatch_blocked = False
        service.discover = lambda: [target]
        failure = PermissionError("invalid token")
        failure.connection_stage = "authentication"

        with patch("ansa_mcp_plugin.backend.service.time.monotonic", return_value=100.0):
            with patch("ansa_mcp_plugin.backend.service.BridgeConnection", side_effect=failure) as factory:
                with self.assertRaises(PermissionError):
                    service._connect(target)
                with patch("ansa_mcp_plugin.backend.service.time.monotonic", return_value=100.5):
                    status = service.status()

        diagnostic = status["connection_diagnostics"]["instance"]
        self.assertEqual(diagnostic["failure_stage"], "authentication")
        self.assertEqual(diagnostic["failure_reason"], "invalid token")
        self.assertEqual(diagnostic["failure_count"], 1)
        self.assertEqual(diagnostic["retry_in_seconds"], 0.5)
        self.assertFalse(status["instances"][0]["ready"])
        self.assertEqual(factory.call_count, 1)

    def test_backoff_caps_at_fifteen_seconds_and_status_exposes_remaining_time(self):
        service = make_service()
        target = registration()
        expected_delays = (1, 2, 4, 8, 15, 15)
        for count, delay in enumerate(expected_delays, 1):
            now = 1000.0 + count * 20
            with patch("ansa_mcp_plugin.backend.service.time.monotonic", return_value=now):
                service._record_connection_failure(target, "authentication", PermissionError("denied"))
            diagnostic = service._connection_diagnostics["instance"]
            self.assertEqual(diagnostic["failure_count"], count)
            self.assertEqual(diagnostic["retry_at"] - now, delay)

        service.backend_id = "backend"
        service.store = SimpleNamespace(current=lambda: None)
        service.tasks = SimpleNamespace(pending=lambda: [])
        service.last_error = None
        service.dispatch_blocked = False
        service.discover = lambda: [target]
        service._connect = Mock(side_effect=ConnectionError("retry scheduled"))
        with patch("ansa_mcp_plugin.backend.service.time.monotonic", return_value=now + 2):
            status = service.status()

        diagnostic = status["connection_diagnostics"]["instance"]
        self.assertEqual(diagnostic["failure_stage"], "authentication")
        self.assertEqual(diagnostic["failure_reason"], "denied")
        self.assertEqual(diagnostic["failure_count"], 6)
        self.assertEqual(diagnostic["retry_in_seconds"], 13)

    def test_discovery_honors_backoff_without_binding_or_replaying_requests(self):
        service = make_service()
        target = registration()
        now = [0.0]

        class StopAfterBackoff:
            def __init__(self):
                self.times = (0.0, 0.5, 1.0)
                self.index = 0

            def wait(self, _interval):
                if self.index == len(self.times):
                    return True
                now[0] = self.times[self.index]
                self.index += 1
                return False

        candidate = Mock()
        candidate.registration = target
        candidate.closed = False
        candidate.failure_reason = None
        candidate.start_reader = Mock()
        candidate.call = Mock()
        service._stop_scheduler = StopAfterBackoff()
        service.discover = Mock(return_value=[target])
        service.token = "a" * 64
        service.backend_id = "backend"
        service._panel_bind = Mock()
        with patch("ansa_mcp_plugin.backend.service.time.monotonic", side_effect=lambda: now[0]):
            with patch("ansa_mcp_plugin.backend.service.BridgeConnection", side_effect=[OSError("refused"), candidate]) as factory:
                service._discover_loop()

        self.assertEqual(factory.call_count, 2)
        self.assertNotIn("instance", service._connection_diagnostics)
        self.assertIs(service.connections["instance"], candidate)
        candidate.start_reader.assert_called_once_with()
        candidate.call.assert_not_called()
        service._panel_bind.assert_not_called()

    def test_stale_instances_are_removed_from_connections_and_diagnostics(self):
        service = make_service()
        active = registration("active")
        stale = registration("stale")
        active_connection = Mock(registration=active)
        stale_connection = Mock(registration=stale)
        service.connections = {"active": active_connection, "stale": stale_connection}
        service._connection_diagnostics = {
            "active": {"registration": service._registration_identity(active), "failure_count": 1},
            "stale": {"registration": service._registration_identity(stale), "failure_count": 9},
        }

        service._prune_connection_state([active])

        self.assertEqual(set(service.connections), {"active"})
        self.assertEqual(set(service._connection_diagnostics), {"active"})
        stale_connection.close.assert_called_once_with()
        active_connection.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
