"""One control or durably claimed model task per owning-thread timer tick."""

import queue
from ansa_mcp_shared.operations import MODEL_CHANGE_TOOLS


class MainThreadExecutor:
    def __init__(self, state, modal_guard=None):
        self.state = state
        self.modal_guard = modal_guard
        self.pending = queue.Queue(maxsize=64)
        self.ticks = 0
        self.shutdown = False
        self._in_tick = False

    def submit(self, kind, fields, done):
        if self.shutdown:
            done(None, "ANSA executor is shut down")
            return
        self.pending.put_nowait((kind, fields, done))

    def tick(self):
        if self.shutdown or self._in_tick:
            return
        if self.modal_guard is not None:
            try:
                if self.modal_guard():
                    return
            except Exception:
                # 门禁异常时 fail-open，定时器回调不能因此中断。
                pass
        self.state.assert_main()
        self._in_tick = True
        try:
            self._tick_once()
        finally:
            self._in_tick = False

    def _tick_once(self):
        if self.shutdown:
            return
        self.ticks += 1
        # 面板刷新由面板窗口自己的 BCTimer 负责：这个 tick 运行在 ctypes Win32
        # 定时器回调里，没有 GUI 窗口上下文，触碰控件句柄会被 ANSA 拒绝。
        try:
            kind, fields, done = self.pending.get_nowait()
        except queue.Empty:
            self._task_tick()
            return
        try:
            if kind == "panel_show":
                expected = fields.get("expected_target")
                if expected is not None:
                    prior = self.state.authorization
                    if (not prior or not prior.get("active")
                            or (expected.get("backend_id"), expected.get("generation")) != (prior["backend_id"], prior["generation"])
                            or (expected.get("instance_id"), expected.get("session_id")) != (self.state.instance_id, self.state.session_id)):
                        raise RuntimeError("Stale panel target authorization")
                value = self.state.panel_opener()
            elif kind == "probe":
                value = self.state.probe()
            elif kind == "model_snapshot":
                expected = fields["expected_target"]
                prior = self.state.authorization
                if not prior or not prior.get("active"):
                    raise RuntimeError("This ANSA candidate has no model execution authorization")
                if (expected.get("backend_id"), expected.get("generation")) != (prior["backend_id"], prior["generation"]):
                    raise RuntimeError("Stale backend authorization")
                if (expected.get("instance_id"), expected.get("session_id")) != (self.state.instance_id, self.state.session_id):
                    raise RuntimeError("Stale ANSA session")
                value = self.state.probe()
            elif kind == "authorization":
                value = self.state.authorization
            elif kind == "grant":
                self.state.check_conflict()
                if self.state.journal.busy():
                    raise RuntimeError("Executor has outstanding tasks; reconcile before granting")
                probe = self.state.probe()
                expected = fields["expected"]
                if any(probe[k] != expected[k] for k in ("pid", "process_start", "instance_id", "session_id")):
                    raise ValueError("Target identity changed")
                prior = self.state.authorization
                if prior and prior.get("active") and prior != fields["authorization"]:
                    raise RuntimeError("Existing authorization requires explicit revocation")
                self.state.save_authorization(fields["authorization"])
                value = self.state.authorization
            elif kind == "revoke":
                if self.state.journal.busy():
                    raise RuntimeError("Executor has queued or running tasks")
                prior = self.state.authorization
                if prior and prior.get("active"):
                    if fields.get("backend_id") != prior.get("backend_id"):
                        raise RuntimeError("Only the owning backend can revoke this grant")
                    self.state.save_authorization({**prior, "active": False})
                value = self.state.authorization
            else:
                raise ValueError("Unsupported control operation")
            done(value, None)
        except Exception as exc:
            done(None, str(exc))

    def _task_tick(self):
        if self.shutdown:
            return
        journal = self.state.journal
        if journal is None or journal.blocked or self.state.stopped.is_set() or not self.state.connected.is_set():
            return
        auth = self.state.authorization
        if not auth or auth.get("backend_id") != self.state.connected_backend_id:
            return  # A restarted backend must reconcile the old grant first.
        try:
            task = journal.next_queued()
            if task is None:
                return
            record = journal.claim(task["task_id"], self.state.validate_task)
            if record["state"] != "running":
                return
            from .operations import execute
            result = None
            error = None
            try:
                witness_id = None
                if task["tool"] == "grid_delete" and self.state._witness is not None:
                    from ansa import base
                    witness_id = base.GetEntityCardValues(task["params"]["deck"], self.state._witness, ("__id__",))["__id__"]
                result = execute(self.state, task["tool"], task["params"])
                if result.get("ok") is False:
                    raise RuntimeError(str(result.get("error") or "ANSA rejected the operation"))
                model_after = self.state.probe()["model"]
                allowed_change = task["tool"] in MODEL_CHANGE_TOOLS or (
                    task["tool"] == "script_execute" and task["params"].get("allow_model_change") is True)
                if model_after["epoch"] != task["model"]["epoch"] and not allowed_change and not (
                        task["tool"] == "grid_delete" and task["params"]["id"] == witness_id):
                    raise RuntimeError("Model changed during the operation; inspect the original model before any new write")
                result = {**result, "model_after": model_after, "executor": self.state.identity(),
                          "thread_id": self.state.main_thread_id, "main_thread": True}
            except Exception as exc:
                operation_result = result
                result = {"possible_effects": True, "executor": self.state.identity(), "main_thread": True}
                if isinstance(operation_result, dict):
                    result["operation_result"] = operation_result
                try:
                    result["model_after"] = self.state.probe()["model"]
                except Exception as probe_error:
                    result["model_after"] = None
                    result["model_probe_error"] = str(probe_error)
                error = str(exc)
            journal.finish(task["task_id"], result=result, error=error)
        except Exception as exc:
            # An API may already have run. Never claim it was cancelled or retry it.
            self.state.last_error = str(exc)
