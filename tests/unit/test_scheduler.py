import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ansa_mcp_plugin.backend.scheduler import TaskScheduler


class SchedulerNotificationChecks(unittest.TestCase):
    def test_notification_failure_blocks_admission_without_exiting_loop(self):
        scheduler = TaskScheduler()
        scheduler._lock = threading.RLock()
        scheduler._stop_scheduler = SimpleNamespace(wait=Mock(side_effect=[False, False, True]))
        scheduler.closed = False
        scheduler.dispatch_blocked = False
        scheduler.last_error = None
        scheduler.tasks = SimpleNamespace(expire=Mock(), pending=Mock(return_value=[]))
        scheduler.store = SimpleNamespace(current=Mock(return_value=None))
        scheduler._notify_panels = Mock(side_effect=sqlite3.OperationalError("state unavailable"))

        scheduler._schedule_loop()

        self.assertTrue(scheduler.dispatch_blocked)
        self.assertIn("state unavailable", scheduler.last_error)
        self.assertEqual(scheduler._notify_panels.call_count, 2)
        scheduler.tasks.expire.assert_called_once()


if __name__ == "__main__":
    unittest.main()
