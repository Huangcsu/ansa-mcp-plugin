"""Native ANSA APIs may pump timer events while a task is being claimed."""
import types
import unittest
from unittest.mock import Mock

from ansa_mcp_bridge.executor import MainThreadExecutor


class ExecutorReentrancyChecks(unittest.TestCase):
    def test_task_event_pump_cannot_reenter_claim_or_consume_queued_control(self):
        state = types.SimpleNamespace(assert_main=Mock())
        executor = MainThreadExecutor(state)
        done = Mock()

        def task_tick():
            executor.submit('authorization', {}, done)
            executor.tick()
            self.assertEqual(executor.pending.qsize(), 1)
            done.assert_not_called()

        executor._task_tick = Mock(side_effect=task_tick)
        executor.tick()
        executor._task_tick.assert_called_once_with()
        self.assertEqual(executor.ticks, 1)
        self.assertFalse(executor._in_tick)
        state.authorization = {'active': True}
        executor.tick()
        done.assert_called_once_with(state.authorization, None)
        self.assertEqual(executor.ticks, 2)

    def test_guard_releases_on_exception_and_control_callback_reentrancy(self):
        state = types.SimpleNamespace(assert_main=Mock(), probe=lambda: executor.tick())
        executor = MainThreadExecutor(state)
        executor._task_tick = Mock(side_effect=ValueError('claim failed'))
        with self.assertRaisesRegex(ValueError, 'claim failed'):
            executor.tick()
        self.assertFalse(executor._in_tick)
        callback = Mock()
        executor.submit('probe', {}, callback)
        executor.tick()
        callback.assert_called_once_with(None, None)
        self.assertEqual(executor._task_tick.call_count, 1)
        self.assertFalse(executor._in_tick)


if __name__ == '__main__':
    unittest.main()
