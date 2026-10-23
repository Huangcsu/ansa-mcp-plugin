import builtins
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge import bootstrap
from ansa_mcp_bridge.ui import panel


class PanelLifecycleChecks(unittest.TestCase):
    """面板刷新必须走窗口自己的 BCTimer，且窗口销毁后不再触碰旧控件句柄。"""

    def setUp(self):
        self.registry = {}
        self.windows = []
        self.timers = []
        self.text_writes = []
        self.write_error = None
        self.close_event = "on_close"
        self.scenario = lambda window: None
        self.gui = types.ModuleType("guitk")
        self.gui.constants = types.SimpleNamespace(BCOnExitDestroy=1, BCVertical=2, BCHorizontal=3, BCOnExitHide=4,
                                                  BCMinimizeButton=8, BCMaximizeButton=16, BCCloseButton=32)
        self.gui.BCWindowCreate = self.create_window
        self.gui.BCGetTopLevelWidget = lambda name: self.registry.get(name)
        self.gui.BCIsVisible = self.is_visible
        self.gui.BCSetVisible = Mock()
        self.gui.BCBoxLayoutCreate = Mock(side_effect=lambda *args: self.widget("layout"))
        self.gui.BCTabWidgetCreate = Mock(side_effect=lambda parent: self.widget("tabs", parent))
        self.gui.BCFrameCreate = Mock(side_effect=lambda parent: self.widget("frame", parent))
        self.gui.BCTextEditCreate = Mock(side_effect=lambda parent, text: self.widget("text_edit", parent))
        self.gui.BCPushButtonCreate = Mock(side_effect=lambda *args: self.widget("button"))
        self.gui.BCTextEditSetReadOnly = Mock()
        self.gui.BCTabWidgetAddTab = Mock()
        self.gui.BCTabWidgetSetCurrentTab = Mock()
        self.gui.BCWindowSetModal = Mock()
        self.gui.BCWindowShowTitleBarButtons = Mock()
        self.gui.BCWindowSetInitSize = Mock()
        self.gui.BCWindowSetSize = Mock()
        self.gui.BCTimerSingleShot = Mock()
        self.gui.BCTextEditSetText = self.set_text
        self.gui.BCTimerCreate = self.create_timer
        self.gui.BCTimerSetTimeoutFunction = self.set_timer_function
        self.gui.BCTimerStart = self.start_timer
        self.gui.BCTimerStop = lambda timer: timer.update(active=False)
        self.gui.BCWindowSetOnCloseFunction = lambda window, cb, data: window.update(on_close=cb)
        self.gui.BCWindowSetAcceptFunction = lambda window, cb, data: window.update(on_accept=cb)
        self.gui.BCWindowSetRejectFunction = lambda window, cb, data: window.update(on_reject=cb)
        self.gui.BCWindowReject = lambda window: None
        self.gui.BCShow = self.show_window
        self.bridge = types.SimpleNamespace(
            state=types.SimpleNamespace(assert_main=Mock(), panel_refresh=None, last_error=None,
                                        session_id="session", panel_info={}),
            panel=None, transport=types.SimpleNamespace(request_bind=Mock()),
            timer=types.SimpleNamespace(_timer_id=7), executor=types.SimpleNamespace(ticks=5),
        )
        self.modules = patch.dict(sys.modules, {"ansa": types.SimpleNamespace(guitk=self.gui)})
        self.modules.start()
        self.render = patch.object(panel, "_render", return_value={
            "overview": "overview", "commands": "commands", "diagnostics": "diagnostics", "settings": "settings",
        })
        self.render.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.render.stop)

    # ------------------------------------------------------------ guitk 替身

    def widget(self, kind, parent=None):
        return {"kind": kind, "parent": parent, "text": None, "alive": True}

    def create_window(self, *args):
        window = self.widget("window")
        window["exit_action"] = args[1]
        window.update(shown=False, timer=None, on_close=None, on_accept=None, on_reject=None)
        self.registry[args[0]] = window
        self.windows.append(window)
        return window

    def create_timer(self, window):
        timer = self.widget("timer", window)
        window["timer"] = timer
        self.timers.append(timer)
        return timer

    def is_visible(self, window):
        if not window["alive"]:
            raise RuntimeError("destroyed native window")
        return bool(window["shown"])

    def set_text(self, edit, text):
        if self.write_error is not None:
            raise RuntimeError(self.write_error)
        edit["text"] = text
        self.text_writes.append(text)

    def set_timer_function(self, timer, function, data):
        timer["function"] = function
        timer["data"] = data

    def start_timer(self, timer, ms, single_shot):
        timer.update(ms=ms, single_shot=single_shot, active=True)
        return 11

    def fire_timer(self):
        timer = self.windows[-1]["timer"]
        self.assertIsNotNone(timer, "面板没有建立窗口内 BCTimer")
        self.assertTrue(timer.get("active"))
        return timer["function"](timer, timer.get("data"))

    def show_window(self, window):
        """模拟 BCShow 阻塞到窗口关闭：中间的“等待”由场景填满。"""
        window["shown"] = True
        self.scenario(window)
        if self.close_event and window["alive"] and self.bridge.state.panel_info.get("alive"):
            callback = window[self.close_event]
            self.assertIsNotNone(callback)
            expected = 0 if self.close_event == "on_close" else 1
            self.assertEqual(callback(window, None), expected)
            if window["exit_action"] == self.gui.constants.BCOnExitHide:
                window["shown"] = False
            else:
                window["alive"] = False

    # ------------------------------------------------------------ 场景

    def scenario_timer_refresh_and_reuse(self, window):
        self.gui.BCWindowShowTitleBarButtons.assert_called_once_with(
            window, self.gui.constants.BCMinimizeButton | self.gui.constants.BCMaximizeButton |
            self.gui.constants.BCCloseButton)
        timer = window["timer"]
        self.assertIsNotNone(timer)
        self.assertEqual(timer["ms"], panel._REFRESH_MS)
        self.assertFalse(timer["single_shot"])
        self.assertEqual(self.bridge.state.panel_info["paint_count"], 1)
        self.fire_timer()
        self.assertEqual(sorted(self.text_writes),
                         ["commands", "diagnostics", "overview", "settings"])
        self.assertEqual(self.bridge.state.panel_info["paint_count"], 2)
        self.assertTrue(self.bridge.state.panel_info["visible"])
        self.assertFalse(self.bridge.state.panel_info["error"])
        # 第二次 show_panel 复用同一窗口：不新建窗口，也不重建定时器。
        self.assertIs(panel.show_panel(self.bridge), window)
        self.assertEqual(len(self.windows), 1)
        self.assertIs(self.bridge.state.panel_refresh, self.bridge._panel_context["refresh"])
        self.assertEqual(len(self.timers), 1, "复用窗口时不应重建定时器")

    def scenario_window_destroyed(self, window):
        self.fire_timer()
        written = len(self.text_writes)
        self.registry.clear()          # ANSA 的 New/Open 会清掉脚本窗口
        window["alive"] = False        # 旧句柄调用从此失败
        self.close_event = None
        self.fire_timer()
        self.assertEqual(self.bridge.state.panel_info["close_reason"], "destroyed")
        self.assertFalse(self.bridge.state.panel_info["alive"])
        self.assertIsNone(self.bridge.state.panel_refresh)
        self.assertIsNone(self.bridge._panel_context)
        self.assertEqual(len(self.text_writes), written)
        self.assertFalse(self.fire_timer())

    def scenario_control_write_failure(self, window):
        self.write_error = "BCTextEditSetText: Parameter 1 is of wrong type, at line 50."
        self.close_event = None
        self.fire_timer()
        self.assertEqual(self.bridge.state.panel_info["close_reason"], "error")
        self.assertFalse(self.bridge.state.panel_info["alive"])
        self.assertIsNone(self.bridge._panel_context)
        self.assertIn("Parameter 1 is of wrong type", self.bridge.state.last_error)
        # 句柄已丢弃：下一次 show_panel 重建窗口，而不是继续用旧控件。
        self.write_error = None
        self.scenario = lambda window: None
        self.close_event = "on_close"
        panel.show_panel(self.bridge)
        self.assertEqual(len(self.windows), 2)

    def scenario_window_gone_with_stale_context(self, window):
        """窗口没了但上下文仍自称存活：show_panel 必须丢掉旧引用并重建。"""
        self.registry.clear()
        window["alive"] = False
        self.assertIsNotNone(self.bridge._panel_context)
        self.scenario = lambda inner: None
        self.close_event = "on_close"
        panel.show_panel(self.bridge)
        self.assertEqual(len(self.windows), 2)
        self.assertIn("ANSA MCP Plugin", self.registry)
        self.assertIs(self.bridge.panel, self.windows[1])

    # ------------------------------------------------------------ 用例

    def test_window_timer_refreshes_all_pages_and_reuses_the_window(self):
        self.scenario = self.scenario_timer_refresh_and_reuse
        self.assertIsNone(panel.show_panel(self.bridge))
        self.assertEqual(self.bridge.state.panel_info["close_reason"], "x")

    def test_native_hidden_window_stops_timer_and_keeps_its_callback_owner(self):
        panels = panel.GUI_HANDLES["native_panels"]
        with patch.dict(panel.GUI_HANDLES, {"native_panels": []}):
            self.assertIsNone(panel.show_panel(self.bridge, persistent=True))
            context = panel.GUI_HANDLES["native_panels"][0]
            self.assertTrue(self.windows[0]["alive"])
            self.assertFalse(self.windows[0]["shown"])
            self.assertFalse(self.timers[0]["active"])
            self.assertTrue(context["show_returned"])
            self.assertEqual(self.bridge.state.panel_info["close_reason"], "x")
            written = len(self.text_writes)
            self.timers[0]["function"](self.timers[0], None)
            self.assertEqual(len(self.text_writes), written)
            self.assertIsNone(panel.show_panel(self.bridge, persistent=True))
            self.assertEqual(len(panel.GUI_HANDLES["native_panels"]), 2)
            self.assertEqual(self.bridge.timer._timer_id, 7)

    def test_refresh_stops_touching_controls_after_the_window_is_destroyed(self):
        self.scenario = self.scenario_window_destroyed
        self.assertIsNone(panel.show_panel(self.bridge))

    def test_control_write_failure_detaches_the_panel_and_next_show_rebuilds(self):
        self.scenario = self.scenario_control_write_failure
        self.assertIsNone(panel.show_panel(self.bridge))

    def test_stale_context_is_detached_and_the_window_is_rebuilt(self):
        self.scenario = self.scenario_window_gone_with_stale_context
        self.assertIsNone(panel.show_panel(self.bridge))

    def test_close_paths_detach_refresh_and_reopen_without_using_destroyed_controls(self):
        for event in ("on_close", "on_accept", "on_reject"):
            with self.subTest(event=event):
                self.close_event = event
                self.assertIsNone(panel.show_panel(self.bridge))
                self.assertIsNone(self.bridge.state.panel_refresh)
                self.assertIsNone(self.bridge.panel)
                self.assertIsNone(self.bridge._panel_context)
                self.assertEqual(self.bridge.state.panel_info["close_reason"],
                                 "x" if event == "on_close" else event.removeprefix("on_"))
                self.assertIsNone(self.bridge.state.last_error)
        self.assertEqual(len(self.windows), 3)

    def test_bridge_close_retries_until_the_owned_window_is_gone(self):
        project_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=project_root) as temp_dir:
            bridge = self.bridge
            bridge.runtime_home = temp_dir
            bridge.panel = None
            bridge.closed = False
            bridge._shutdown_retry_required = False
            bridge._panel_close_retry_required = False
            bridge._panel_close_context = None
            bridge.state.stopped = threading.Event()
            bridge.state.session_dir = temp_dir
            bridge.state.authorization = None
            bridge.state.mark_closed = Mock()
            bridge.state.journal = types.SimpleNamespace(busy=Mock(return_value=[]), close=Mock())
            bridge.executor.shutdown = False
            bridge.transport.close = Mock()
            bridge.transport._thread = None
            bridge.timer.close = Mock()
            bridge.mutex = Mock()
            builtins._ansa_mcp_plugin_bridge = bridge
            self.addCleanup(lambda: setattr(builtins, "_ansa_mcp_plugin_bridge", None)
                            if getattr(builtins, "_ansa_mcp_plugin_bridge", None) is bridge else None)

            def reject_panel(window):
                if not window["alive"]:
                    raise RuntimeError("stale native window must not be rejected again")
                callback = window["on_reject"]
                self.assertEqual(callback(window, None), 1)
                self.registry.pop("ANSA MCP Plugin", None)
                window["alive"] = False
                window["timer"]["active"] = False
                return 1

            def attempt_close_while_window_is_open(window):
                with self.assertRaisesRegex(RuntimeError, "panel is still open"):
                    bootstrap.Bridge.close(bridge)
                self.assertTrue(bridge._panel_close_retry_required)
                self.assertFalse(bridge.closed)
                bridge.transport.close.assert_not_called()
                bridge.timer.close.assert_not_called()
                bridge.state.journal.close.assert_not_called()
                bridge.mutex.close.assert_not_called()
                with self.assertRaisesRegex(RuntimeError, "panel is not confirmed closed"):
                    bootstrap.start(temp_dir, show=False)

                self.gui.BCWindowReject = reject_panel
                writes_before = len(self.text_writes)
                self.assertEqual(self.fire_timer(), 0)
                self.assertEqual(len(self.text_writes), writes_before)
                # 拒绝回调已完成，BCShow 尚未返回；资源仍不能释放。
                with self.assertRaisesRegex(RuntimeError, "panel is still open"):
                    bootstrap.Bridge.close(bridge)

            self.scenario = attempt_close_while_window_is_open
            self.close_event = None
            panel.show_panel(bridge)
            # BCShow 返回后重试：不得再拒绝已经失效的原生句柄。
            bootstrap.Bridge.close(bridge)

            self.assertTrue(bridge.closed)
            self.assertFalse(bridge._panel_close_retry_required)
            self.assertIsNone(bridge._panel_close_context)
            self.assertNotIn("ANSA MCP Plugin", self.registry)
            self.assertFalse(self.timers[0]["active"])
            bridge.transport.close.assert_called_once()
            bridge.timer.close.assert_called_once()
            bridge.state.journal.close.assert_called_once()
            bridge.mutex.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
