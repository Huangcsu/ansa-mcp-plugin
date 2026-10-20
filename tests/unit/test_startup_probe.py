import queue
import socket
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ansa_mcp_bridge.state import BridgeState
from ansa_mcp_bridge.transport import BridgeTransport
from ansa_mcp_shared.framing import recv_frame, send_frame


class FakeExecutor:
    def __init__(self):
        self.pending = queue.Queue()
        self.submissions = []

    def submit(self, kind, fields, done):
        self.submissions.append((kind, fields, threading.current_thread().name))
        self.pending.put((kind, fields, done))

    def next_job(self, timeout=1):
        return self.pending.get(timeout=timeout)

    @staticmethod
    def complete(job, value=None, error=None):
        job[2](value, error)


class StartupProbeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        instance_dir = Path(self.temp.name) / "instance"
        instance_dir.mkdir()
        self.state = types.SimpleNamespace(
            instance_dir=instance_dir,
            pid=123,
            process_start="123",
            instance_id="instance",
            session_id="session",
            connected=threading.Event(),
            connected_backend_id=None,
            startup_probe={
                "state": "idle", "backend_id": None, "started_at": None,
                "attempt": 0, "error": None,
            },
            authorization=None,
            probe=Mock(side_effect=AssertionError("transport called probe directly")),
        )
        self.executor = FakeExecutor()
        self.transport = BridgeTransport(self.state, self.executor, "test-token")
        self.clients = []

    def tearDown(self):
        self.transport.close()
        self.transport._thread.join(timeout=2)
        for client in self.clients:
            try:
                client.close()
            except OSError:
                pass
        self.temp.cleanup()

    def connect_backend(self, backend_id):
        client = socket.create_connection(("127.0.0.1", self.transport.port), timeout=2)
        client.settimeout(2)
        self.clients.append(client)
        send_frame(client, {
            "version": 1,
            "kind": "hello",
            "request_id": "hello-" + str(len(self.clients)),
            "token": "test-token",
            "instance_id": self.state.instance_id,
            "process_start": self.state.process_start,
            "backend_id": backend_id,
        })
        reply = recv_frame(client)
        self.assertTrue(reply["ok"])
        return client

    def disconnect(self, client):
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()
        self.wait_for(lambda: not self.state.connected.is_set())

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("condition was not reached before timeout")

    def test_handshake_queues_one_read_only_probe_without_mcp_query_or_authorization(self):
        self.connect_backend("backend-a")
        job = self.executor.next_job()

        self.assertEqual(job[0], "probe")
        self.assertEqual(job[1], {})
        self.assertEqual(self.executor.submissions, [("probe", {}, "ansa-mcp-transport")])
        self.assertEqual(self.state.startup_probe["state"], "pending")
        self.assertEqual(self.state.startup_probe["backend_id"], "backend-a")
        self.assertEqual(self.state.startup_probe["attempt"], 1)
        self.assertIsNone(self.state.authorization)
        self.state.probe.assert_not_called()

        # A delayed main-thread tick keeps the same queued item; it is not requeued.
        time.sleep(0.1)
        self.assertTrue(self.executor.pending.empty())
        self.assertEqual(len(self.executor.submissions), 1)

        FakeExecutor.complete(job, {"deck": "ABAQUS"})
        self.assertEqual(self.state.startup_probe["state"], "ready")
        self.assertIsNone(self.state.authorization)

    def test_reconnect_requeues_after_late_completion_without_stale_state_writes(self):
        clock = Mock(side_effect=(10.0, 20.0, 30.0))
        fake_time = types.SimpleNamespace(monotonic=clock)
        with patch("ansa_mcp_bridge.transport.time", fake_time):
            first_client = self.connect_backend("same-backend")
            first_job = self.executor.next_job()
            self.assertEqual(self.state.startup_probe["started_at"], 10.0)
            self.disconnect(first_client)
            self.assertEqual(self.state.startup_probe["state"], "idle")

            second_client = self.connect_backend("same-backend")
            second_started_at = self.state.startup_probe["started_at"]
            self.assertEqual(second_started_at, 20.0)
            self.assertTrue(self.executor.pending.empty())

            FakeExecutor.complete(first_job, {"deck": "ABAQUS"})
            second_job = self.executor.next_job()
            self.assertEqual(self.state.startup_probe["state"], "pending")
            self.assertEqual(self.state.startup_probe["backend_id"], "same-backend")
            self.assertEqual(self.state.startup_probe["started_at"], second_started_at)

            self.disconnect(second_client)
            third_client = self.connect_backend("new-backend")
            third_started_at = self.state.startup_probe["started_at"]
            self.assertEqual(third_started_at, 30.0)

            FakeExecutor.complete(second_job, {"deck": "ABAQUS"})
            third_job = self.executor.next_job()
            self.assertEqual(self.state.startup_probe["state"], "pending")
            self.assertEqual(self.state.startup_probe["backend_id"], "new-backend")
            self.assertEqual(self.state.startup_probe["started_at"], third_started_at)

            FakeExecutor.complete(third_job, {"deck": "ABAQUS"})
            self.assertEqual(self.state.startup_probe["state"], "ready")
            self.assertEqual(self.state.startup_probe["backend_id"], "new-backend")
            self.assertEqual(self.state.startup_probe["started_at"], third_started_at)
            self.assertEqual(clock.call_count, 3)

    def test_execution_errors_retry_at_most_three_times(self):
        with patch("ansa_mcp_bridge.transport._STARTUP_PROBE_BACKOFF", (0.01, 0.01)):
            self.connect_backend("backend-a")
            first = self.executor.next_job()
            FakeExecutor.complete(first, error="first failure")
            self.assertEqual(self.state.startup_probe["state"], "retrying")
            self.assertEqual(self.state.startup_probe["attempt"], 2)
            self.assertEqual(self.state.startup_probe["error"], "first failure")

            second = self.executor.next_job(timeout=1)
            self.assertEqual(second[0], "probe")
            FakeExecutor.complete(second, error="second failure")
            self.assertEqual(self.state.startup_probe["state"], "retrying")
            self.assertEqual(self.state.startup_probe["attempt"], 3)

            third = self.executor.next_job(timeout=1)
            self.assertEqual(third[0], "probe")
            FakeExecutor.complete(third, error="third failure")
            self.assertEqual(self.state.startup_probe["state"], "failed")
            self.assertEqual(self.state.startup_probe["attempt"], 3)
            self.assertEqual(self.state.startup_probe["error"], "third failure")
            time.sleep(0.05)
            self.assertTrue(self.executor.pending.empty())
            self.assertEqual(len(self.executor.submissions), 3)

    def test_successful_state_probe_records_wall_and_monotonic_cache_times(self):
        state = object.__new__(BridgeState)
        state.main_thread_id = threading.get_ident()
        state.model_epoch = 1
        state.model_key = None
        state.model_detection = "incomplete_same_path_open"
        state._witness = None
        state._model_witness = None
        state._guard_observed = False
        state._multiple_models_seen = False
        state.cached_probe = None
        state.cached_probe_at = None
        state.cached_probe_monotonic = None
        state.cached_probe_backend_id = None
        state.pid = 123
        state.process_start = "123"
        state.instance_id = "instance"
        state.session_id = "session"
        state.authorization = None
        state.connected = threading.Event()
        state.connected.set()
        state.connected_backend_id = "backend-a"
        state.stopped = threading.Event()
        state.journal = None
        state.last_error = None
        state.panel_info = {}

        base = types.SimpleNamespace(
            CurrentDeck=lambda: 2,
            DataBaseName=lambda: "model.ansa",
            GetCurrentAnsaModel=lambda: None,
            GetAllAnsaWindows=lambda: [],
            CollectEntities=lambda *args: [],
        )
        constants = types.SimpleNamespace(app_version="test", NASTRAN=1, ABAQUS=2)
        ansa = types.ModuleType("ansa")
        ansa.base = base
        ansa.constants = constants
        started = time.monotonic()
        with patch.dict("sys.modules", {"ansa": ansa}):
            state.probe()

        self.assertIsInstance(state.cached_probe_at, float)
        self.assertIsInstance(state.cached_probe_monotonic, float)
        self.assertGreaterEqual(state.cached_probe_monotonic, started)
        self.assertEqual(state.cached_probe_backend_id, "backend-a")
        self.assertEqual(state.cached_probe["deck_name"], "ABAQUS")


if __name__ == "__main__":
    unittest.main()
