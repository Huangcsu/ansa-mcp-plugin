import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ansa_mcp_bridge.ui.panel import _overview_data, _render


class _Event:
    def __init__(self, value=False):
        self.value = value

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def clear(self):
        self.value = False


class _Journal:
    blocked = False

    @staticmethod
    def recent(_limit):
        return []


def _fixture():
    state = SimpleNamespace(
        pid=4321,
        process_start="start-current",
        instance_id="instance-current",
        session_id="session-current",
        backend_status={},
        connected=_Event(True),
        connected_backend_id="backend-current",
        cached_probe_at=time.time(),
        cached_probe_monotonic=time.monotonic(),
        cached_probe_backend_id="backend-current",
        stopped=_Event(),
        startup_probe={
            "state": "ready",
            "backend_id": "backend-current",
            "started_at": time.monotonic() - 1,
            "error": None,
        },
        journal=_Journal(),
        main_thread_id=threading.main_thread().ident,
        last_error=None,
    )
    state.identity = lambda: {
        key: getattr(state, key)
        for key in ("pid", "process_start", "instance_id", "session_id")
    }
    state.assert_main = lambda: None
    state.check_conflict = lambda: None
    bridge = SimpleNamespace(
        state=state,
        token="secret-token",
        runtime_home="C:/private/runtime",
        version="test",
    )
    snapshot = {
        **state.identity(),
        "deck": 91,
        "deck_name": "NASTRAN",
        "version": "2026.1.0",
        "authorization": {
            "active": True,
            "backend_id": "backend-current",
            "generation": 7,
        },
        "model": {
            "database": "C:/models/boom.ansa",
            "epoch": 12,
            "detection": "retained_model_lifetime",
            "write_scope": "single-window",
            "write_enabled": True,
        },
    }
    state.backend_status = {
        "backend_id": "backend-current",
        "active_target": {
            **state.identity(),
            "backend_id": "backend-current",
            "generation": 7,
            "state": "active",
            "deck": 91,
        },
        "active_model": {"epoch": 12},
        "tasks": [],
    }
    return bridge, snapshot


class PanelOverviewTests(unittest.TestCase):
    def test_published_remote_context_keeps_solver_and_version_without_local_authorization(self):
        from ansa_mcp_plugin.backend.service import Service

        bridge, snapshot = _fixture()
        snapshot["authorization"] = None
        target = {"pid": 9876, "process_start": "remote-start", "instance_id": "remote-instance",
                  "session_id": "remote-session", "backend_id": "backend-current",
                  "generation": 8, "state": "active"}
        model = {"database": "D:/remote/assembly.ansa", "epoch": 99}
        authorization = {"active": True, "backend_id": "backend-current", "generation": 8}
        connection = SimpleNamespace(call=Mock(return_value=True))
        service = object.__new__(Service)
        service.backend_id = "backend-current"
        service.store = SimpleNamespace(current=lambda: target)
        service.tasks = SimpleNamespace(recent=lambda _limit: [])
        service.connections = {"local": connection}
        service.dispatch_blocked = False
        service.last_error = None
        service._panel_model = (target["instance_id"], model)
        service._panel_snapshot = {**target, "model": model, "deck": 2, "deck_name": "ABAQUS",
                                   "version": "2026.1.0", "authorization": authorization}
        service._notify_panels()
        bridge.state.backend_status = connection.call.call_args.kwargs["state"]
        data = _overview_data(bridge, snapshot)
        self.assertEqual(data["target_label"], "其他窗口")
        self.assertEqual(data["other_target"]["model_name"], "assembly.ansa")
        self.assertEqual(data["other_target"]["solver_name"], "ABAQUS")
        self.assertEqual(data["other_target"]["window_label"], "其他窗口")
        self.assertIn("后端目标 ANSA 版本：2026.1.0", _render(bridge, None, snapshot)["diagnostics"])

    def test_same_window_is_merged_and_diagnostics_keep_traceable_identity(self):
        bridge, snapshot = _fixture()

        rendered = _render(bridge, None, snapshot)
        overview = rendered["overview"]
        diagnostics = rendered["diagnostics"]

        self.assertEqual(set(rendered), {"overview", "commands", "diagnostics", "settings"})
        self.assertEqual(overview.count("boom.ansa"), 1)
        self.assertIn("AI 操作目标：此窗口", overview)
        self.assertNotIn("start-current", overview)
        self.assertNotIn("instance-current", overview)
        self.assertNotIn("session-current", overview)
        self.assertNotIn("retained_model_lifetime", overview)
        self.assertNotIn("代次", overview)
        self.assertIn("process_start", diagnostics)
        self.assertIn("instance-current", diagnostics)
        self.assertIn("epoch", diagnostics)
        self.assertIn("retained_model_lifetime", diagnostics)
        self.assertIn("91", diagnostics)

    def test_other_window_with_same_model_name_is_distinguished_by_pid(self):
        bridge, snapshot = _fixture()
        snapshot["authorization"] = None
        other = {
            "pid": 5678,
            "process_start": "start-other",
            "instance_id": "instance-other",
            "session_id": "session-other",
            "backend_id": "backend-current",
            "generation": 7,
            "state": "active",
            "deck": 94,
            "deck_name": "ABAQUS",
        }
        bridge.state.backend_status["active_target"] = other
        bridge.state.backend_status["active_model"] = {
            "database": "D:/other/boom.ansa",
            "epoch": 28,
        }

        data = _overview_data(bridge, snapshot)
        overview = _render(bridge, None, snapshot)["overview"]

        self.assertEqual(data["target_label"], "其他窗口")
        self.assertEqual(data["other_target"]["model_name"], "boom.ansa")
        self.assertEqual(data["other_target"]["solver_name"], "ABAQUS")
        self.assertEqual(data["other_target"]["window_label"], "其他窗口（PID 5678）")
        self.assertIsNone(snapshot["authorization"])
        self.assertEqual(overview.count("boom.ansa"), 2)
        self.assertIn("PID 5678", overview)
        self.assertIn("求解器：NASTRAN", overview)

    def test_offline_unprobed_paused_stale_authorization_and_unbound_states(self):
        bridge, snapshot = _fixture()
        bridge.state.connected.clear()
        offline = _overview_data(bridge, snapshot)
        self.assertEqual((offline["target_label"], offline["task_label"]), ("尚未连接", "待连接"))
        self.assertIsNone(offline["other_target"])

        bridge, snapshot = _fixture()
        bridge.state.cached_probe_at = None
        bridge.state.cached_probe_monotonic = None
        unprobed = _overview_data(bridge, snapshot)
        self.assertEqual(unprobed["target_label"], "目标待确认")
        self.assertIn("最近读取：时间未知", unprobed["updated_label"])
        self.assertIn("尚未读取当前连接", unprobed["updated_label"])

        bridge, snapshot = _fixture()
        bridge.state.stopped.set()
        paused = _overview_data(bridge, snapshot)
        self.assertEqual(paused["task_label"], "已暂停")
        self.assertIn("恢复任务", paused["next_step"])

        bridge, snapshot = _fixture()
        snapshot["authorization"]["generation"] = 6
        stale = _overview_data(bridge, snapshot)
        self.assertEqual(stale["target_label"], "目标待确认")
        self.assertEqual(stale["task_label"], "等待目标确认")

        bridge, snapshot = _fixture()
        bridge.state.cached_probe_at = time.time() - 60
        old_snapshot = _overview_data(bridge, snapshot)
        self.assertEqual(old_snapshot["target_label"], "此窗口")
        self.assertIn("模型缓存较旧", old_snapshot["updated_label"])

        bridge, snapshot = _fixture()
        bridge.state.cached_probe_monotonic = time.monotonic() - 20
        bridge.state.startup_probe = {
            "state": "pending",
            "backend_id": "backend-current",
            "started_at": time.monotonic() - 1,
            "error": None,
        }
        reconnecting = _overview_data(bridge, snapshot)
        self.assertEqual(reconnecting["target_label"], "目标待确认")
        self.assertIn("尚未读取当前连接", reconnecting["updated_label"])

        bridge, snapshot = _fixture()
        bridge.state.connected_backend_id = "backend-new"
        bridge.state.cached_probe_backend_id = "backend-old"
        old_connection = _overview_data(bridge, snapshot)
        self.assertEqual(old_connection["target_label"], "目标待确认")
        self.assertIn("来自旧连接", old_connection["updated_label"])

        bridge, snapshot = _fixture()
        bridge.state.backend_status["active_target"] = None
        unbound = _overview_data(bridge, snapshot)
        self.assertEqual(unbound["task_label"], "待连接")
        self.assertNotEqual(unbound["task_label"], "等待任务")
        self.assertIn("连接此窗口", unbound["next_step"])

    def test_long_path_and_secrets_stay_out_of_overview_and_are_safe_in_diagnostics(self):
        bridge, snapshot = _fixture()
        path = "C:/" + ("long-directory/" * 24) + "boom.ansa"
        snapshot["model"]["database"] = path
        snapshot["authorization"]["token"] = "secret-token"
        bridge.state.backend_status["active_target"]["authorization"] = {
            "active": True,
            "backend_id": "backend-current",
            "generation": 7,
            "token": "secret-token",
        }

        rendered = _render(bridge, None, snapshot)

        self.assertIn("boom.ansa", rendered["overview"])
        self.assertNotIn("long-directory", rendered["overview"])
        self.assertNotIn("secret-token", rendered["overview"])
        self.assertIn(path, rendered["diagnostics"])
        self.assertIn("[已隐藏]", rendered["diagnostics"])
        self.assertNotIn("secret-token", rendered["diagnostics"])


if __name__ == "__main__":
    unittest.main()
