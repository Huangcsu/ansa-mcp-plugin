import ctypes
import gc
import unittest
import weakref
from unittest.mock import Mock, patch

from ansa_mcp_bridge.platform.timer import MainThreadTimer


class TimerLifetimeChecks(unittest.TestCase):
    def test_native_callback_survives_gc_and_late_messages_after_close_are_inert(self):
        api = Mock()
        api.SetTimer.return_value = 17
        api.KillTimer.return_value = True
        ticks = []
        with patch.object(ctypes, "WinDLL", return_value=api), \
                patch.object(ctypes, "_ansa_mcp_plugin_timer_callbacks", [], create=True):
            timer = MainThreadTimer(lambda: ticks.append("tick"))
            ref = weakref.ref(timer)
            del timer
            gc.collect()
            self.assertIsNotNone(ref(), "Windows callback must retain its Python owner")
            callback = ctypes._ansa_mcp_plugin_timer_callbacks[0]
            callback(None, 0, 17, 0)
            self.assertEqual(ticks, ["tick"])
            callback(None, 0, 18, 0)
            self.assertEqual(ticks, ["tick"])
            ref().close()
            gc.collect()
            callback(None, 0, 17, 0)
            self.assertEqual(ticks, ["tick"], "late messages after KillTimer must do nothing")


if __name__ == "__main__":
    unittest.main()
