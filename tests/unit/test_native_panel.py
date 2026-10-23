import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.ui import native_panel, panel


class NativePanelGui:
    """最小 guitk 替身：区分窗口登记代理与真实 BCWindow 句柄。"""

    def __init__(self):
        self.registry = {}
        self.windows = []
        self.timers = []
        self.proxy = object()
        self.show_handles = []
        self.show_scenarios = []
        self.show_count = 0
        self.show_returned_windows = []
        self.api_calls = []
        self.api_count_at_show_return = 0
        self.reject_handles = []
        self.start_count = 0
        self.stop_count = 0
        self._timer_id = 0
        self.constants = types.SimpleNamespace(BCOnExitHide=1, BCOnExitDestroy=2,
                                              BCHorizontal=3, BCVertical=4,
                                              BCAlignLeft=5, BCWidgetWidth=6,
                                              BCWrapAtWordBoundaryOrAnywhere=7,
                                              BCMinimizeButton=8, BCMaximizeButton=16, BCCloseButton=32)

    def module(self):
        guitk = types.SimpleNamespace(
            constants=self.constants,
            BCGetTopLevelWidget=self.get_top_level,
            BCGetTopWidget=self.get_top_widget,
            BCWindowCreate=self.create_window,
            BCWindowSetModal=Mock(),
            BCWindowShowTitleBarButtons=lambda window, buttons: window.update(title_bar_buttons=buttons),
            BCWindowSetOnCloseFunction=self.set_close,
            BCWindowSetAcceptFunction=self.set_accept,
            BCWindowSetRejectFunction=self.set_reject,
            BCLabelCreate=Mock(side_effect=lambda parent, text: {"parent": parent, "text": text}),
            BCLabelSetText=Mock(side_effect=lambda label, text: label.update(text=text)),
            BCBoxLayoutCreate=lambda parent, *_: {"window": parent.get("window", parent)},
            BCGridLayoutCreate=lambda parent: {"parent": parent},
            BCGridLayoutAddWidget=Mock(),
            BCGridLayoutSetMargin=Mock(),
            BCGridLayoutSetSpacing=Mock(),
            BCGridLayoutSetColStretch=Mock(),
            BCSpacerCreate=Mock(),
            BCSetVisible=lambda widget, visible: widget.update(visible=visible),
            BCPushButtonCreate=Mock(),
            BCTabWidgetCreate=self.create_tabs,
            BCFrameCreate=Mock(side_effect=lambda *_: {}),
            BCTextEditCreate=Mock(side_effect=lambda *_: {}),
            BCTextEditSetReadOnly=Mock(),
            BCTextEditSetWordWrap=Mock(),
            BCTextEditSetWrapPolicy=Mock(),
            BCTextEditSetText=Mock(),
            BCTabWidgetAddTab=Mock(),
            BCTabWidgetSetCurrentTab=lambda tabs, page: tabs.update(current=page),
            BCWindowSetInitSize=Mock(),
            BCWindowSetSize=Mock(),
            BCWindowSetPosition=Mock(),
            BCTimerCreate=self.create_timer,
            BCTimerSetTimeoutFunction=self.set_timer_callback,
            BCTimerStart=self.start_timer,
            BCTimerStop=self.stop_timer,
            BCShow=self.show,
            BCWindowReject=self.reject,
        )
        for name, callback in vars(guitk).items():
            if callable(callback):
                setattr(guitk, name, self._track_api(name, callback))
        return guitk

    def _track_api(self, name, callback):
        def tracked(*args, **kwargs):
            self.api_calls.append(name)
            return callback(*args, **kwargs)
        return tracked

    def get_top_level(self, title):
        return self.proxy if title in self.registry else None

    def get_top_widget(self, tabs):
        return tabs["window"]

    def create_window(self, title, exit_action):
        window = {"title": title, "exit_action": exit_action}
        self.windows.append(window)
        return window

    def create_tabs(self, window):
        window = window.get("window", window)
        tabs = {"window": window}
        window["tabs"] = tabs
        return tabs

    @staticmethod
    def _set_callback(window, key, callback, data):
        window[key] = (callback, data)

    def set_close(self, window, callback, data):
        self._set_callback(window, "on_close", callback, data)

    def set_accept(self, window, callback, data):
        self._set_callback(window, "on_accept", callback, data)

    def set_reject(self, window, callback, data):
        self._set_callback(window, "on_reject", callback, data)

    def create_timer(self, window):
        timer = {"window": window, "active": False}
        self.timers.append(timer)
        return timer

    @staticmethod
    def set_timer_callback(timer, callback, data):
        timer["callback"] = callback
        timer["data"] = data

    def start_timer(self, timer, interval_ms, single_shot):
        self.start_count += 1
        self._timer_id += 1
        timer.update(active=True, interval_ms=interval_ms, single_shot=single_shot)
        return self._timer_id

    def stop_timer(self, timer):
        self.stop_count += 1
        timer["active"] = False

    def show(self, window):
        self.show_count += 1
        self.registry[window["title"]] = window
        self.show_handles.append(window)
        scenario = self.show_scenarios.pop(0) if self.show_scenarios else None
        try:
            if scenario is not None:
                scenario(window)
        finally:
            # BCShow owns the blocking lifecycle. Returning from this fake is
            # the native script-window destruction boundary.
            context = window["on_close"][1]
            timer = context.get("timer")
            if timer is not None:
                timer["active"] = False
            self.registry.pop(window["title"], None)
            window["destroyed"] = True
            self.show_returned_windows.append(window)
            self.api_count_at_show_return = len(self.api_calls)

    def reject(self, window):
        self.reject_handles.append(window)
        callback, data = window["on_reject"]
        callback(window, data)


class NativePanelLifecycleChecks(unittest.TestCase):
    def setUp(self):
        native_panel.GUI_HANDLES[native_panel._PANEL_KEY] = None
        self.gui = NativePanelGui()
        self.ansa = types.SimpleNamespace(guitk=self.gui.module())
        self.ansa_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.ansa_patch.start()
        self.addCleanup(self.ansa_patch.stop)
        self.addCleanup(lambda: native_panel.GUI_HANDLES.__setitem__(native_panel._PANEL_KEY, None))

    @staticmethod
    def make_bridge(cached_probe=None):
        state = types.SimpleNamespace(
            assert_main=Mock(),
            cached_probe=cached_probe,
            authorization={"active": False},
            session_id="test-session",
            panel_info={},
            stopped=threading.Event(),
            connected=threading.Event(),
            connected_backend_id=None,
            backend_status={},
            last_error=None,
            identity=lambda: {"instance_id": "instance", "session_id": "test-session", "pid": 123, "process_start": "123"},
            probe=Mock(side_effect=AssertionError("native GUI must not probe the model")),
        )
        return types.SimpleNamespace(
            state=state,
            transport=types.SimpleNamespace(request_bind=Mock()),
            executor=types.SimpleNamespace(ticks=12),
            timer=types.SimpleNamespace(_timer_id=7),
        )

    def assert_no_gui_api_after_show_return(self):
        self.assertEqual(len(self.gui.api_calls), self.gui.api_count_at_show_return)

    def assert_attached_until_show_returns(self, bridge, context):
        self.assertFalse(context["show_returned"])
        self.assertIs(context["bridge"], bridge)
        self.assertIs(bridge._native_panel_context, context)
        self.assertIs(native_panel.GUI_HANDLES.get(native_panel._PANEL_KEY), context)

    def assert_detached_and_anchored(self, context, window, tabs, timer, widgets):
        import ctypes

        self.assertFalse(context["alive"])
        self.assertFalse(context["visible"])
        self.assertFalse(context["timer_active"])
        self.assertIsNone(context["bridge"])
        for key in ("window", "tabs", "timer", "control_status", "action_status"):
            self.assertIsNone(context[key])
        self.assertEqual(context["widgets"], {})
        self.assertIsNone(native_panel.GUI_HANDLES.get(native_panel._PANEL_KEY))

        anchors = getattr(ctypes, "_ansa_mcp_plugin_native_panel_callbacks")
        retained = next(entry for entry in reversed(anchors) if entry[0] is context)
        self.assertIs(retained[1], window)
        self.assertIs(retained[2], tabs)
        self.assertIs(retained[3], timer)
        self.assertEqual(set(retained[4]), set(widgets))
        for key, widget in widgets.items():
            self.assertIs(retained[4][key], widget)
        self.assertIs(retained[7], native_panel._on_timer)
        self.assertIs(retained[8], native_panel._on_window_close)
        self.assertIs(retained[9], native_panel._on_window_accept)
        self.assertIs(retained[10], native_panel._on_window_reject)

    def test_buttons_show_admission_offline_error_and_confirmed_binding(self):
        bridge = self.make_bridge()
        def exercise_inside_bcshow(window):
            self.assertEqual(window['title_bar_buttons'], self.gui.constants.BCMinimizeButton |
                             self.gui.constants.BCMaximizeButton | self.gui.constants.BCCloseButton)
            context = window["on_close"][1]
            native_panel._on_stop(None, context)
            self.assertTrue(bridge.state.stopped.is_set())
            self.assertIn("任务：已暂停", context["info"]["control_status"])
            self.assertIn("已暂停", context["info"]["last_action"])
            native_panel._on_start(None, context)
            self.assertFalse(bridge.state.stopped.is_set())
            self.assertIn("任务：等待连接", context["info"]["control_status"])
            self.assertIn("后端仍未连接", context["info"]["last_action"])

            bridge.transport.request_bind.side_effect = ConnectionError("MCP 后端未连接")
            native_panel._on_bind(None, context)
            native_panel._on_timer(context["timer"], context)
            self.assertIn("连接未完成：MCP 后端未连接", context["info"]["last_action"])
            self.assertIn("MCP 后端未连接", bridge.state.last_error)
            self.assertIsNone(context["bind_pending"])

            bridge.state.connected.set()
            bridge.state.connected_backend_id = "backend"
            bridge.transport.request_bind.side_effect = None
            native_panel._on_bind(None, context)
            self.assertIn("等待后端确认", context["info"]["last_action"])
            native_panel._on_bind(None, context)
            self.assertEqual(bridge.transport.request_bind.call_count, 2)
            bridge.state.authorization = {"active": True, "backend_id": "backend", "generation": 1}
            bridge.state.backend_status = {"active_target": {**bridge.state.identity(), "backend_id": "backend", "generation": 1}}
            native_panel._on_timer(context["timer"], context)
            self.assertIn("连接成功", context["info"]["last_action"])
            self.assertIn("连接：已连接到此窗口", context["info"]["control_status"])
            self.assertIsNone(context["bind_pending"])

            timer = context["timer"]
            stop_count = self.gui.stop_count
            native_panel._on_window_close(window, context)
            self.assertGreater(self.gui.stop_count, stop_count)
            self.assertFalse(timer["active"])
            self.assertFalse(context["info"]["alive"])
            self.assertFalse(context["info"]["visible"])
            native_panel._on_stop(None, context)
            native_panel._on_bind(None, context)
            self.assertFalse(bridge.state.stopped.is_set())
            self.assertEqual(bridge.transport.request_bind.call_count, 2)
            bridge.state.probe.assert_not_called()

        self.gui.show_scenarios.append(exercise_inside_bcshow)
        with patch.object(panel, "_render", return_value={}):
            self.assertIsNone(native_panel.show_panel(bridge))
        self.assert_no_gui_api_after_show_return()
        bridge.state.probe.assert_not_called()

    def test_binding_failure_and_disconnect_remain_visible_without_retry(self):
        bridge = self.make_bridge()
        bridge.state.connected.set()
        bridge.state.connected_backend_id = "backend"
        def exercise_inside_bcshow(window):
            context = window["on_close"][1]
            timer = context["timer"]
            native_panel._on_bind(None, context)
            bridge.state.backend_status = {"error": "Old target has queued tasks"}
            native_panel._on_timer(timer, context)
            self.assertIn("连接失败", context["info"]["last_action"])
            self.assertIsNone(context["bind_pending"])
            native_panel._on_bind(None, context)
            bridge.state.connected.clear()
            native_panel._on_timer(timer, context)
            self.assertIn("后端已断开", context["info"]["last_action"])
            self.assertIsNone(context["bind_pending"])
            native_panel._on_timer(timer, context)
            self.assertEqual(bridge.transport.request_bind.call_count, 2)
            bridge.state.connected.set()
            bridge.state.startup_probe = {"state": "ready", "backend_id": "backend", "started_at": 100.0}
            bridge.state.cached_probe = {"model": {"write_enabled": True}}
            bridge.state.cached_probe_backend_id = "backend"
            bridge.state.cached_probe_monotonic = 101.0
            native_panel._on_timer(timer, context)
            self.assertIn("模型：已识别", context["info"]["control_status"])
            self.assertIn("连接此窗口", context["info"]["last_action"])
            self.assertNotIn("后端已断开", context["info"]["last_action"])
            window["on_reject"][0](window, window["on_reject"][1])

        self.gui.show_scenarios.append(exercise_inside_bcshow)
        with patch.object(panel, "_render", return_value={}):
            self.assertIsNone(native_panel.show_panel(bridge))
        self.assert_no_gui_api_after_show_return()

    def test_refresh_uses_cached_or_pending_snapshot_without_model_probe(self):
        bridge = self.make_bridge()
        rendered_snapshots = []
        scenario_state = {}

        def render(_bridge, _widgets, snapshot=None):
            rendered_snapshots.append(snapshot)
            return {"overview": "state"}

        def refresh_inside_bcshow(window):
            context = window["on_close"][1]
            timer = context["timer"]
            scenario_state.update(
                context=context,
                window=window,
                tabs=context["tabs"],
                timer=timer,
                widgets=dict(context["widgets"]),
            )
            self.assertEqual(timer["callback"](timer, timer["data"]), 0)
            scenario_state["render_count_at_return"] = len(rendered_snapshots)

        self.gui.show_scenarios.append(refresh_inside_bcshow)
        with patch.object(panel, "_render", side_effect=render):
            self.assertIsNone(native_panel.show_panel(bridge))

        expected = {
            "model": {
                "write_enabled": False,
                "write_enabled_confirmed": False,
                "detection": "等待自动读取模型状态",
            },
            "authorization": bridge.state.authorization,
        }
        self.assertEqual(rendered_snapshots, [expected])
        self.assertEqual(len(rendered_snapshots), scenario_state["render_count_at_return"])
        self.assert_no_gui_api_after_show_return()
        status_text = panel._model_text(expected["model"])
        self.assertIn("受控写入 待模型识别", status_text)
        self.assertNotIn("受控写入 开启", status_text)
        bridge.state.probe.assert_not_called()
        self.assertEqual(bridge.state.panel_info["timer_id"], bridge.timer._timer_id)
        self.assertEqual(bridge.state.panel_info["session_id"], bridge.state.session_id)
        saved = scenario_state
        self.assert_detached_and_anchored(
            saved["context"], saved["window"], saved["tabs"], saved["timer"], saved["widgets"]
        )
        with patch.object(native_panel, "_guitk", side_effect=AssertionError("late timer called guitk")):
            self.assertEqual(saved["timer"]["callback"](saved["timer"], saved["timer"]["data"]), 0)

    def test_startup_feedback_and_recovered_connection_error(self):
        bridge = self.make_bridge()

        def exercise(window):
            context = window["on_close"][1]
            bridge.transport.request_bind.side_effect = ConnectionError("MCP 后端未连接")
            native_panel._on_bind(None, context)
            old_error = bridge.state.last_error
            bridge.state.connected.set()
            bridge.state.connected_backend_id = "backend"
            bridge.state.startup_probe = {
                "state": "pending", "backend_id": "backend", "started_at": 100.0,
            }
            with patch.object(native_panel.time, "monotonic", return_value=101.0):
                native_panel._on_timer(context["timer"], context)
            self.assertIn("模型：正在读取", context["info"]["control_status"])
            self.assertIn("无需先让 AI", context["info"]["last_action"])
            self.assertNotIn("连接未完成", context["info"]["last_action"])
            self.assertEqual(bridge.state.last_error, old_error)
            with patch.object(native_panel.time, "monotonic", return_value=108.0):
                native_panel._on_timer(context["timer"], context)
            self.assertIn("模型：等待响应", context["info"]["control_status"])
            self.assertIn("等待 ANSA 响应", context["info"]["last_action"])
            bridge.state.startup_probe = {
                "state": "failed", "backend_id": "backend", "started_at": 100.0,
            }
            native_panel._on_timer(context["timer"], context)
            self.assertIn("模型：读取失败", context["info"]["control_status"])
            self.assertIn("诊断日志", context["info"]["last_action"])
            bridge.state.probe.assert_not_called()

        self.gui.show_scenarios.append(exercise)
        with patch.object(panel, "_render", return_value={}):
            native_panel.show_panel(bridge)
        self.assert_no_gui_api_after_show_return()

    def test_current_snapshot_is_not_authorization_and_old_connection_is_not_ready(self):
        bridge = self.make_bridge(cached_probe={
            "model": {"database": "C:/model.ansa", "write_enabled": True}, "deck": 1,
        })
        state = bridge.state
        state.connected.set()
        state.connected_backend_id = "backend"
        state.cached_probe_backend_id = "backend"
        state.cached_probe_at = 200.0
        state.cached_probe_monotonic = 101.0
        state.startup_probe = {"state": "ready", "backend_id": "backend", "started_at": 100.0}

        def exercise(window):
            context = window["on_close"][1]
            with patch.object(native_panel.time, "time", return_value=203.0):
                snapshot = native_panel._snapshot_from_cache(state)
            self.assertEqual(snapshot["model"]["snapshot_age_seconds"], 3)
            self.assertFalse(snapshot["model"]["write_enabled_confirmed"])
            self.assertIn("执行前校验", panel._model_text(snapshot["model"]))
            native_panel._on_timer(context["timer"], context)
            self.assertIn("模型：已识别", context["info"]["control_status"])
            self.assertIn("任务：待绑定", context["info"]["control_status"])
            self.assertFalse(state.authorization["active"])
            state.startup_probe = {"state": "pending", "backend_id": "backend", "started_at": 110.0}
            with patch.object(native_panel.time, "monotonic", return_value=111.0):
                native_panel._on_timer(context["timer"], context)
            self.assertIn("模型：正在读取", context["info"]["control_status"])
            self.assertIn("旧连接快照", native_panel._snapshot_from_cache(state)["model"]["detection"])
            state.probe.assert_not_called()

        self.gui.show_scenarios.append(exercise)
        with patch.object(panel, "_render", return_value={}):
            native_panel.show_panel(bridge)
        self.assert_no_gui_api_after_show_return()

    def test_grouped_overview_updates_other_target_and_opens_diagnostics(self):
        bridge = self.make_bridge()
        overview = {
            "model_name": "<模型>&" + "很长的模型名称" * 12,
            "solver_name": "ABAQUS", "target_label": "尚未连接",
            "task_label": "待连接", "next_step": "请连接此窗口",
            "updated_label": "刚刚读取", "other_target": None,
        }

        def exercise(window):
            context = window["on_close"][1]
            native_panel._on_timer(context["timer"], context)
            self.assertNotIn("overview", context["widgets"])
            self.assertEqual(set(context["pages"]), {"overview", "commands", "diagnostics", "settings"})
            self.assertIn("&lt;模型&gt;&amp;", context["overview_widgets"]["model_name"]["text"])
            self.assertIn("<br>", context["overview_widgets"]["model_name"]["text"])
            self.assertFalse(context["other_target_frame"]["visible"])
            overview["other_target"] = {
                "model_name": "other.ansa", "solver_name": "NASTRAN", "window_label": "其他窗口",
            }
            native_panel._on_timer(context["timer"], context)
            self.assertTrue(context["other_target_frame"]["visible"])
            self.assertIn("other.ansa", context["overview_widgets"]["other_model_name"]["text"])
            native_panel._on_diagnostics(None, context)
            self.assertIs(context["tabs"]["current"], context["pages"]["diagnostics"])
            bridge.state.probe.assert_not_called()

        self.gui.show_scenarios.append(exercise)
        with patch.object(panel, "_render", return_value={}), \
             patch.object(panel, "_overview_data", return_value=overview, create=True):
            native_panel.show_panel(bridge)
        self.assert_no_gui_api_after_show_return()
        context = self.gui.show_returned_windows[-1]["on_close"][1]
        self.assertEqual(context["overview_widgets"], {})
        self.assertEqual(context["pages"], {})

    def test_destroy_reopen_rebuilds_window_and_keeps_bridge_executor(self):
        bridge = self.make_bridge()
        session_id = bridge.state.session_id
        executor = bridge.executor
        kernel_timer = bridge.timer
        kernel_timer_id = bridge.timer._timer_id
        previous_window = None
        previous_timer = None
        saved_contexts = []

        for callback_name, expected_return, reason in (
            ("on_close", 0, "x"),
            ("on_accept", 1, "accept"),
            ("on_reject", 1, "reject"),
        ):
            saved = {}

            def close_inside_bcshow(window, callback_name=callback_name,
                                    expected_return=expected_return, reason=reason):
                context = window["on_close"][1]
                timer = context["timer"]
                saved.update(
                    context=context,
                    window=window,
                    tabs=context["tabs"],
                    timer=timer,
                    widgets=dict(context["widgets"]),
                )
                self.assertEqual(context["session_id"], session_id)
                self.assertEqual(context["info"]["timer_id"], kernel_timer_id)
                self.assertIs(timer["callback"], native_panel._on_timer)
                self.assertIs(timer["data"], context)
                stopped_before = self.gui.stop_count
                self.assertEqual(window[callback_name][0](window, window[callback_name][1]),
                                 expected_return)
                self.assertGreater(self.gui.stop_count, stopped_before)
                self.assertFalse(timer["active"])
                self.assertFalse(context["info"]["alive"])
                self.assertFalse(context["info"]["visible"])
                self.assertEqual(context["info"]["close_reason"], reason)
                self.assert_attached_until_show_returns(bridge, context)

            self.gui.show_scenarios.append(close_inside_bcshow)
            with patch.object(panel, "_render", return_value={}):
                self.assertIsNone(native_panel.show_panel(bridge))
            self.assert_no_gui_api_after_show_return()

            self.assertIs(self.gui.windows[-1], saved["window"])
            self.assertIs(self.gui.windows[-1]["exit_action"], self.gui.constants.BCOnExitDestroy)
            if previous_window is not None:
                self.assertIsNot(saved["window"], previous_window)
                self.assertIsNot(saved["timer"], previous_timer)
            self.assertEqual(len(self.gui.windows), len(saved_contexts) + 1)
            self.assertEqual(len(self.gui.timers), len(saved_contexts) + 1)
            self.assert_detached_and_anchored(
                saved["context"], saved["window"], saved["tabs"], saved["timer"], saved["widgets"]
            )
            self.assertIs(bridge.executor, executor)
            self.assertIs(bridge.timer, kernel_timer)
            self.assertEqual(bridge.timer._timer_id, kernel_timer_id)
            self.assertEqual(bridge.state.session_id, session_id)
            self.assertEqual(bridge.state.panel_info["session_id"], session_id)
            self.assertEqual(bridge.state.panel_info["timer_id"], kernel_timer_id)
            saved_contexts.append(saved["context"])
            previous_window = saved["window"]
            previous_timer = saved["timer"]

        self.assertEqual(self.gui.start_count, 3)
        for context in saved_contexts:
            self.assertFalse(context["alive"])
            self.assertFalse(context["visible"])

    def test_show_reentry_and_bridge_close_wait_for_bcshow_return(self):
        bridge = self.make_bridge()
        saved = {}

        def request_close_inside_bcshow(window):
            context = window["on_close"][1]
            timer = context["timer"]
            saved.update(context=context, window=window, tabs=context["tabs"], timer=timer,
                         widgets=dict(context["widgets"]))

            self.assertIs(native_panel.show_panel(bridge), window)
            self.assertEqual(self.gui.show_count, 1)
            self.assertFalse(native_panel.close_panel(bridge))
            self.assertEqual(context["info"]["close_reason"], "bridge_close_pending")
            with self.assertRaisesRegex(RuntimeError, "正在关闭"):
                native_panel.show_panel(bridge)
            self.assertEqual(self.gui.show_count, 1)

            self.assertEqual(timer["callback"](timer, timer["data"]), 0)
            self.assertEqual(self.gui.reject_handles, [window])
            self.assertFalse(timer["active"])
            self.assertFalse(context["info"]["alive"])
            self.assertFalse(context["info"]["visible"])
            self.assertFalse(native_panel.close_panel(bridge))
            self.assert_attached_until_show_returns(bridge, context)

        self.gui.show_scenarios.append(request_close_inside_bcshow)
        with patch.object(panel, "_render", return_value={}):
            self.assertIsNone(native_panel.show_panel(bridge))
        self.assert_no_gui_api_after_show_return()
        self.assertTrue(native_panel.close_panel(bridge))
        self.assertEqual(self.gui.show_count, 1)
        self.assert_detached_and_anchored(
            saved["context"], saved["window"], saved["tabs"], saved["timer"], saved["widgets"]
        )

    def test_destroyed_context_detaches_only_after_bcshow_returns(self):
        bridge = self.make_bridge()
        saved = {}

        def destroy_inside_bcshow(window):
            context = window["on_close"][1]
            timer = context["timer"]
            saved.update(context=context, window=window, tabs=context["tabs"], timer=timer,
                         widgets=dict(context["widgets"]))
            self.gui.registry.pop(window["title"])
            self.assertEqual(timer["callback"](timer, timer["data"]), 0)
            self.assertFalse(context["alive"])
            self.assertFalse(context["visible"])
            self.assertFalse(context["timer_active"])
            self.assertEqual(context["info"]["close_reason"], "destroyed")
            self.assert_attached_until_show_returns(bridge, context)

        self.gui.show_scenarios.append(destroy_inside_bcshow)
        with patch.object(panel, "_render", return_value={}):
            self.assertIsNone(native_panel.show_panel(bridge))
        self.assert_no_gui_api_after_show_return()
        self.assert_detached_and_anchored(
            saved["context"], saved["window"], saved["tabs"], saved["timer"], saved["widgets"]
        )


if __name__ == "__main__":
    unittest.main()
