"""Fault-injection checks for bridge bootstrap and shutdown cleanup."""

import builtins
import json
import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ansa_mcp_bridge import bootstrap

from ansa_mcp_bridge.executor import MainThreadExecutor


IDENTITY = {
    "pid": 1234,
    "process_start": "98765",
    "instance_id": "instance-test",
    "session_id": "session-test",
}


class CloseResource:
    def __init__(self, error=None):
        self.error = error
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class FakeJournal(CloseResource):
    def __init__(self, records=(), *, busy_error=None, cancel_error=None, close_error=None):
        super().__init__(close_error)
        self.records = list(records)
        self.busy_error = busy_error
        self.cancel_error = cancel_error
        self.cancelled = []

    def busy(self):
        if self.busy_error is not None:
            raise self.busy_error
        return list(self.records)

    def cancel(self, task, state):
        if self.cancel_error is not None:
            raise self.cancel_error
        record = {"task_id": task["task_id"], "task": task, "state": state}
        self.cancelled.append(record)
        return record


class FakeState:
    def __init__(self, session_dir, authorization=None):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.authorization_file = self.session_dir / "authorization.json"
        self.authorization = authorization
        self.stopped = threading.Event()
        self.journal = None
        self.panel_info = {}
        self.save_error = None
        self.mark_error = None
        if authorization is not None:
            self.authorization_file.write_text(json.dumps(authorization), encoding="utf-8")

    def assert_main(self):
        return None

    def identity(self):
        return dict(IDENTITY)

    def save_authorization(self, value):
        if self.save_error is not None:
            raise self.save_error
        temporary = self.authorization_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.authorization_file)
        self.authorization = value

    def _read_authorization(self):
        if not self.authorization_file.is_file():
            return None
        return json.loads(self.authorization_file.read_text(encoding="utf-8"))

    def mark_closed(self):
        marker = self.session_dir / "closed.json"
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps({**IDENTITY, "executor_stopped": True}), encoding="utf-8")
        temporary.replace(marker)
        if self.mark_error is not None:
            raise self.mark_error


class BridgeBootstrapChecks(unittest.TestCase):
    def setUp(self):
        project_root = Path(__file__).resolve().parents[2]
        runtime_root = (project_root / "runtime").resolve()
        self.temp_root = (runtime_root / "unit-test-temp").resolve()
        if self.temp_root.parent != runtime_root:
            raise RuntimeError("Bootstrap test temp root resolved outside project runtime")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        if self.temp_root.resolve() != self.temp_root:
            raise RuntimeError("Bootstrap test temp root must not be a reparse point")
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        temp_path = Path(self.temp.name).resolve()
        if temp_path.parent != self.temp_root:
            raise RuntimeError("Refusing to clean bootstrap test temp outside project runtime/unit-test-temp")
        self.temp.cleanup()

    def _install_bridge(self, *, journal=None, authorization=None, state=None,
                        transport_error=None, timer_error=None, mutex_error=None):
        if state is None:
            state = FakeState(self.root / "session", authorization)
        state.journal = journal or FakeJournal()
        bridge = bootstrap.Bridge.__new__(bootstrap.Bridge)
        bridge.state = state
        bridge.transport = CloseResource(transport_error)
        bridge.timer = CloseResource(timer_error)
        bridge.mutex = CloseResource(mutex_error)
        bridge.executor = MainThreadExecutor(state)
        bridge.runtime_home = self.root / "runtime-home"
        bridge.closed = False
        bridge._shutdown_retry_required = False
        builtins._ansa_mcp_plugin_bridge = bridge

        def cleanup_builtin():
            if getattr(builtins, "_ansa_mcp_plugin_bridge", None) is bridge:
                del builtins._ansa_mcp_plugin_bridge

        self.addCleanup(cleanup_builtin)
        return bridge, state, state.journal

    def _construct_with_failure(self, failing_stage, original_error, cleanup_error=None):
        runtime = self.root / "runtime-home"
        config = runtime / "config"
        config.mkdir(parents=True)
        (config / "token.txt").write_text("t" * 64, encoding="ascii")
        mutex = CloseResource(cleanup_error if failing_stage == "timer" else None)
        journal = FakeJournal(close_error=cleanup_error if failing_stage == "timer" else None)
        state = FakeState(self.root / "init-session")
        timer = CloseResource(cleanup_error if failing_stage == "transport" else None)

        def make_timer(_callback):
            if failing_stage == "timer":
                raise original_error
            return timer

        def make_transport(*_args):
            raise original_error

        with (
            patch.object(bootstrap, "verify_runtime"),
            patch.object(bootstrap, "ProcessMutex", return_value=mutex),
            patch.object(bootstrap, "BridgeState", return_value=state),
            patch.object(bootstrap, "Journal", return_value=journal),
            patch.object(bootstrap, "MainThreadTimer", side_effect=make_timer),
            patch.object(bootstrap, "BridgeTransport", side_effect=make_transport),
        ):
            with self.assertRaises(RuntimeError) as caught:
                bootstrap.Bridge(runtime)

        self.assertIs(caught.exception, original_error)
        if failing_stage == "transport":
            self.assertEqual(1, timer.close_calls)
            self.assertEqual(0, journal.close_calls)
            self.assertEqual(0, mutex.close_calls)
            bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
            self.assertIsNotNone(bridge)
            self.addCleanup(
                lambda: delattr(builtins, "_ansa_mcp_plugin_bridge")
                if getattr(builtins, "_ansa_mcp_plugin_bridge", None) is bridge else None
            )
            self.assertTrue(bridge._shutdown_retry_required)
            self.assertTrue(bridge.executor.shutdown)
            self.assertTrue(state.stopped.is_set())
            self.assertIs(bridge.timer, timer)
            self.assertIs(bridge.mutex, mutex)
            with self.assertRaisesRegex(RuntimeError, "timer is not confirmed stopped"):
                bootstrap.start(runtime, show=False)
            timer.error = None
            bridge.close()
            self.assertEqual(1, journal.close_calls)
            self.assertEqual(1, mutex.close_calls)
            self.assertTrue(bridge.closed)
            self.assertIsNone(getattr(builtins, "_ansa_mcp_plugin_bridge", None))
        else:
            self.assertEqual(1, journal.close_calls)
            self.assertEqual(1, mutex.close_calls)
            self.assertIsNone(getattr(builtins, "_ansa_mcp_plugin_bridge", None))

    def test_timer_construction_failure_closes_journal_and_mutex_preserving_error(self):
        original = RuntimeError("timer construction failed")
        cleanup = RuntimeError("journal cleanup failed")
        self._construct_with_failure("timer", original, cleanup)

    def test_transport_construction_failure_keeps_placeholder_until_timer_retry(self):
        original = RuntimeError("transport construction failed")
        cleanup = RuntimeError("timer cleanup failed")
        self._construct_with_failure("transport", original, cleanup)

    def test_successful_close_persists_cancellations_revocation_and_stop_proof(self):
        task = {"task_id": "queued-task", "params": {}}
        journal = FakeJournal([{"task_id": "queued-task", "state": "queued", "task": task}])
        auth = {"backend_id": "backend-a", "generation": 4, "active": True}
        bridge, state, _ = self._install_bridge(journal=journal, authorization=auth)

        bridge.close()

        self.assertTrue(state.stopped.is_set())
        self.assertEqual(["expired"], [item["state"] for item in journal.cancelled])
        self.assertFalse(state._read_authorization()["active"])
        self.assertTrue((state.session_dir / "closed.json").is_file())
        self.assertEqual(1, bridge.transport.close_calls)
        self.assertEqual(1, bridge.timer.close_calls)
        self.assertEqual(1, journal.close_calls)
        self.assertEqual(1, bridge.mutex.close_calls)
        self.assertTrue(bridge.closed)
        self.assertIsNone(getattr(builtins, "_ansa_mcp_plugin_bridge", None))

    def test_panel_show_queue_full_resets_pending_and_allows_retry(self):
        bridge, _, _ = self._install_bridge()
        bridge.panel = None

        class FullOnce:
            calls = 0

            def __call__(self, *_args):
                self.calls += 1
                if self.calls == 1:
                    raise queue.Full

        submit = FullOnce()
        bridge.executor.submit = submit

        with self.assertRaises(queue.Full):
            bridge.show_panel()

        self.assertFalse(bridge._panel_show_pending)
        self.assertIsNone(bridge.show_panel())
        self.assertTrue(bridge._panel_show_pending)
        self.assertEqual(submit.calls, 2)

    def test_shutdown_reconciliation_failures_release_every_resource_without_stop_proof(self):
        cases = ("busy", "cancel", "authorization", "marker", "transport")
        for failure in cases:
            with self.subTest(failure=failure):
                session = self.root / failure
                auth = {"backend_id": "backend-a", "generation": 4, "active": True}
                state = FakeState(session, auth)
                task = {"task_id": f"queued-{failure}", "params": {}}
                journal = FakeJournal(
                    [{"task_id": task["task_id"], "state": "queued", "task": task}],
                    busy_error=RuntimeError("busy failed") if failure == "busy" else None,
                    cancel_error=RuntimeError("cancel failed") if failure == "cancel" else None,
                )
                if failure == "authorization":
                    state.save_error = RuntimeError("revoke failed")
                if failure == "marker":
                    state.mark_error = RuntimeError("marker write reported failure")
                bridge, state, journal = self._install_bridge(
                    state=state,
                    journal=journal,
                    transport_error=RuntimeError("transport close failed") if failure == "transport" else None,
                    timer_error=RuntimeError("timer close failed") if failure == "timer" else None,
                )

                with self.assertRaises(RuntimeError):
                    bridge.close()

                self.assertTrue(state.stopped.is_set())
                self.assertEqual(1, bridge.transport.close_calls)
                self.assertEqual(1, bridge.timer.close_calls)
                self.assertEqual(1, journal.close_calls)
                self.assertEqual(1, bridge.mutex.close_calls)
                self.assertTrue(bridge.closed)
                self.assertFalse((state.session_dir / "closed.json").exists())
                self.assertIsNone(getattr(builtins, "_ansa_mcp_plugin_bridge", None))

    def test_timer_stop_failure_retains_bridge_and_mutex_until_retry(self):
        auth = {"backend_id": "backend-a", "generation": 4, "active": True}
        timer_error = RuntimeError("timer close failed")
        bridge, state, journal = self._install_bridge(authorization=auth, timer_error=timer_error)

        with self.assertRaisesRegex(RuntimeError, "timer close failed"):
            bridge.close()

        self.assertTrue(state.stopped.is_set())
        self.assertTrue(bridge.executor.shutdown)
        self.assertTrue(bridge._shutdown_retry_required)
        self.assertFalse(bridge.closed)
        self.assertFalse((state.session_dir / "closed.json").exists())
        self.assertEqual(0, journal.close_calls)
        self.assertEqual(0, bridge.mutex.close_calls)
        self.assertIs(bridge, getattr(builtins, "_ansa_mcp_plugin_bridge", None))
        with self.assertRaisesRegex(RuntimeError, "timer is not confirmed stopped"):
            bootstrap.start(bridge.runtime_home, show=False)

        bridge.timer.error = None
        bridge.close()

        self.assertTrue(bridge.closed)
        self.assertFalse(bridge._shutdown_retry_required)
        self.assertTrue((state.session_dir / "closed.json").is_file())
        self.assertEqual(1, journal.close_calls)
        self.assertEqual(1, bridge.mutex.close_calls)
        self.assertIsNone(getattr(builtins, "_ansa_mcp_plugin_bridge", None))

    def test_executor_shutdown_makes_timer_callback_inert_and_rejects_new_controls(self):
        class State:
            def __init__(self):
                self.assert_calls = 0

            def assert_main(self):
                self.assert_calls += 1
                raise AssertionError("shutdown tick must not inspect state")

        state = State()
        executor = MainThreadExecutor(state)
        executor.shutdown = True
        executor.tick()
        self.assertEqual(0, state.assert_calls)
        self.assertEqual(0, executor.ticks)

        completed = []
        executor.submit("probe", {}, lambda value, error: completed.append((value, error)))
        self.assertEqual([(None, "ANSA executor is shut down")], completed)
        self.assertTrue(executor.pending.empty())

    def test_running_task_prevents_stop_proof_but_still_releases_resources(self):
        task = {"task_id": "running-task", "params": {}}
        journal = FakeJournal([{"task_id": "running-task", "state": "running", "task": task}])
        auth = {"backend_id": "backend-a", "generation": 4, "active": True}
        bridge, state, journal = self._install_bridge(journal=journal, authorization=auth)

        with self.assertRaisesRegex(RuntimeError, "running task"):
            bridge.close()

        self.assertEqual([], journal.cancelled)
        self.assertFalse(state._read_authorization()["active"])
        self.assertFalse((state.session_dir / "closed.json").exists())
        self.assertEqual(1, bridge.timer.close_calls)
        self.assertEqual(1, journal.close_calls)
        self.assertEqual(1, bridge.mutex.close_calls)
        self.assertTrue(bridge.closed)


if __name__ == "__main__":
    unittest.main()
