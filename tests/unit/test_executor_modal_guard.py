"""主线程执行器的前台模态门禁。"""

import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge import bootstrap
from ansa_mcp_bridge.executor import MainThreadExecutor
from ansa_mcp_bridge.platform import modal


class ForegroundModalChecks(unittest.TestCase):
    def test_current_process_dialog_is_busy(self):
        with patch.object(
            modal,
            "_foreground_window_info",
            return_value=(os.getpid(), "Open", "#32770"),
        ):
            self.assertTrue(modal.foreground_modal_busy())

    def test_other_process_and_ansa_windows_are_not_busy(self):
        cases = (
            (os.getpid() + 1, "Open", "#32770"),
            (os.getpid(), "ANSA MCP Plugin", "Qt5152QWindowIcon"),
            (os.getpid(), "model.ansa", "Qt5152QWindowIcon"),
        )
        for info in cases:
            with self.subTest(info=info), patch.object(
                modal, "_foreground_window_info", return_value=info
            ):
                self.assertFalse(modal.foreground_modal_busy())

    def test_probe_failure_fails_open(self):
        with patch.object(modal, "_foreground_window_info", side_effect=OSError("no user32")):
            self.assertFalse(modal.foreground_modal_busy())


class ExecutorModalGuardChecks(unittest.TestCase):
    def test_busy_keeps_pending_queue_and_skips_state_work(self):
        state = types.SimpleNamespace(assert_main=Mock(), probe=Mock())
        done = Mock()
        executor = MainThreadExecutor(state, modal_guard=Mock(return_value=True))
        executor._task_tick = Mock()
        executor.submit("probe", {}, done)

        executor.tick()

        self.assertEqual(executor.pending.qsize(), 1)
        self.assertEqual(executor.ticks, 0)
        self.assertFalse(executor._in_tick)
        state.assert_main.assert_not_called()
        state.probe.assert_not_called()
        executor._task_tick.assert_not_called()
        done.assert_not_called()

    def test_non_busy_consumes_queue_normally(self):
        state = types.SimpleNamespace(
            assert_main=Mock(),
            probe=Mock(return_value={"deck": "NASTRAN"}),
        )
        done = Mock()
        executor = MainThreadExecutor(state, modal_guard=Mock(return_value=False))
        executor.submit("probe", {}, done)

        executor.tick()

        self.assertTrue(executor.pending.empty())
        state.assert_main.assert_called_once_with()
        state.probe.assert_called_once_with()
        done.assert_called_once_with({"deck": "NASTRAN"}, None)

    def test_guard_exception_fails_open_without_crashing_timer(self):
        state = types.SimpleNamespace(assert_main=Mock())
        executor = MainThreadExecutor(
            state, modal_guard=Mock(side_effect=RuntimeError("probe failed"))
        )
        executor._task_tick = Mock()

        executor.tick()

        state.assert_main.assert_called_once_with()
        executor._task_tick.assert_called_once_with()
        self.assertEqual(executor.ticks, 1)

    def test_bridge_injects_foreground_guard(self):
        class State:
            def __init__(self, root):
                self.session_dir = root
                self.authorization = None
                self.panel_info = {}
                self.stopped = threading.Event()
                self.journal = None

            def identity(self):
                return {
                    "pid": 1,
                    "process_start": "1",
                    "instance_id": "instance",
                    "session_id": "session",
                }

        class Resource:
            _timer_id = 1

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            (runtime / "config").mkdir()
            (runtime / "config" / "token.txt").write_text("t" * 64, encoding="ascii")
            state = State(runtime / "session")
            with (
                patch.object(bootstrap, "verify_runtime"),
                patch.object(bootstrap, "ProcessMutex", return_value=Resource()),
                patch.object(bootstrap, "BridgeState", return_value=state),
                patch.object(bootstrap, "Journal", return_value=Resource()),
                patch.object(bootstrap, "MainThreadTimer", return_value=Resource()),
                patch.object(bootstrap, "BridgeTransport", return_value=Resource()),
                patch.object(bootstrap, "MainThreadExecutor") as executor_type,
            ):
                bootstrap.Bridge(runtime)

        self.assertIs(
            executor_type.call_args.kwargs["modal_guard"],
            modal.foreground_modal_busy,
        )


if __name__ == "__main__":
    unittest.main()
