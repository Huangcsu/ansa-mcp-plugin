"""Call start() in ANSA's Script Editor; repeated calls reuse the live executor."""

import builtins
import os
import queue
from pathlib import Path
import sys
import threading

from .executor import MainThreadExecutor
from .platform.modal import foreground_modal_busy
from .platform.mutex import ProcessMutex
from .platform.timer import MainThreadTimer
from .state import BridgeState
from .journal import Journal
from .transport import BridgeTransport
from ansa_mcp_shared.security import verify_runtime


class Bridge:
    def __init__(self, runtime_home):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Start the bridge on ANSA's main thread")
        old_plugin = sys.modules.get("ansa_mcp_sum.plugin")
        if old_plugin is not None:
            old_state = getattr(old_plugin, "GUI_HANDLES", {}).get("bridge_runtime_state", {})
            if old_state and not old_state.get("stop_requested") and (
                old_state.get("poller") is not None or old_state.get("timer") is not None
            ):
                raise RuntimeError("The legacy ANSA MCP-Sum bridge is active in this process")
        verify_runtime(runtime_home)
        self.runtime_home = Path(runtime_home).resolve()
        token_path = self.runtime_home / "config" / "token.txt"
        self.token = token_path.read_text(encoding="ascii").strip()
        if len(self.token) < 32:
            raise RuntimeError("Backend token is missing or too short")
        self.mutex = ProcessMutex()
        self.timer = self.transport = None
        self.executor = None
        self.closed = False
        self._shutdown_retry_required = False
        self._panel_close_retry_required = False
        self._panel_close_context = None
        try:
            self.state = BridgeState(self.runtime_home)
            self.state.journal = Journal(self.state.session_dir, self.state.identity())
            self.executor = MainThreadExecutor(self.state, modal_guard=foreground_modal_busy)
            self.timer = MainThreadTimer(self.executor.tick)
            self.state.panel_info["timer_id"] = getattr(self.timer, "_timer_id", None)
            self.transport = BridgeTransport(self.state, self.executor, self.token)
        except BaseException as original:
            journal = getattr(getattr(self, "state", None), "journal", None)
            cleanup_errors = []

            executor = getattr(self, "executor", None)
            if executor is not None:
                try:
                    executor.shutdown = True
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            state = getattr(self, "state", None)
            if state is not None:
                try:
                    state.stopped.set()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            for resource in (self.transport,):
                if resource is not None:
                    try:
                        resource.close()
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)

            timer_stopped = self.timer is None
            if self.timer is not None:
                try:
                    self.timer.close()
                    timer_stopped = True
                except BaseException as cleanup_error:
                    timer_stopped = False
                    cleanup_errors.append(cleanup_error)

            if timer_stopped:
                for resource in (journal, self.mutex):
                    if resource is None:
                        continue
                    try:
                        resource.close()
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            else:
                # The callback may still be registered. Keep its owner, journal,
                # and process mutex alive until a later close confirms KillTimer.
                self._shutdown_retry_required = True
                try:
                    builtins._ansa_mcp_plugin_bridge = self
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            for cleanup_error in cleanup_errors:
                try:
                    original.add_note(f"Bootstrap cleanup also failed: {cleanup_error!r}")
                except (AttributeError, TypeError):
                    pass
            raise
        self.panel = None
        self.state.panel_opener = self._queue_native_panel

    def show_panel(self):
        self.state.assert_main()
        if self.closed or self.executor.shutdown:
            raise RuntimeError("Bridge is closed")
        if getattr(self, "_panel_show_pending", False):
            return self.panel
        self._panel_show_pending = True

        def completed(value, error):
            if error is not None:
                self._panel_show_pending = False
                self.state.last_error = "Panel open failed: " + error

        # 执行器只排队；后续原生回调进入 BCShow，避免占住执行器 tick。
        try:
            self.executor.submit("panel_show", {}, completed)
        except queue.Full:
            self._panel_show_pending = False
            raise
        return self.panel

    def _queue_native_panel(self):
        self.state.assert_main()
        from ansa import guitk

        def open_native(_data):
            self._panel_show_pending = False
            if self.closed or self.executor.shutdown:
                return 0
            from .ui.panel import show_panel
            show_panel(self)
            return 0

        guitk.BCTimerSingleShot(25, open_native, None)
        return {"queued": True}

    def close(self):
        if self.closed:
            return
        errors = []
        safe_to_mark_closed = True

        def attempt(action):
            try:
                return True, action()
            except BaseException as exc:
                errors.append(exc)
                return False, None

        executor = getattr(self, "executor", None)
        if executor is not None:
            shutdown_set, _ = attempt(lambda: setattr(executor, "shutdown", True))
            safe_to_mark_closed &= shutdown_set
        stopped_ok, _ = attempt(self.state.stopped.set)
        safe_to_mark_closed &= stopped_ok
        main_thread_ok, _ = attempt(self.state.assert_main)
        safe_to_mark_closed &= main_thread_ok
        if not main_thread_ok:
            self._shutdown_retry_required = True
            try:
                builtins._ansa_mcp_plugin_bridge = self
            except BaseException as exc:
                errors.append(exc)
            first = errors[0]
            for later in errors[1:]:
                try:
                    first.add_note(f"Additional bridge shutdown failure: {later!r}")
                except (AttributeError, TypeError):
                    break
            raise first

        def close_owned_panel():
            from .ui.panel import close_panel
            return close_panel(self)

        panel_close_ok, panel_closed = attempt(close_owned_panel)
        if not panel_close_ok or not panel_closed:
            self._panel_close_retry_required = True
            self._shutdown_retry_required = True
            if panel_close_ok:
                errors.append(RuntimeError("ANSA MCP panel is still open; retry bridge.close() before starting another bridge"))
            try:
                builtins._ansa_mcp_plugin_bridge = self
            except BaseException as exc:
                errors.append(exc)
            first = errors[0]
            for later in errors[1:]:
                try:
                    first.add_note(f"Additional bridge shutdown failure: {later!r}")
                except (AttributeError, TypeError):
                    break
            raise first
        self._panel_close_retry_required = False

        # Remove any stale stop proof first; a new proof is written only after
        # transport, timer, queued tasks, and authorization are all reconciled.
        closed_path = Path(self.state.session_dir) / "closed.json"
        marker_removed, _ = attempt(lambda: closed_path.unlink(missing_ok=True))
        safe_to_mark_closed &= marker_removed

        timer_stopped = self.timer is None
        for resource in (self.transport, self.timer):
            if resource is not None:
                resource_stopped, _ = attempt(resource.close)
                safe_to_mark_closed &= resource_stopped
                if resource is self.timer:
                    timer_stopped = resource_stopped
                if resource is self.transport:
                    transport_thread = getattr(resource, "_thread", None)
                    if transport_thread is not None:
                        joined, _ = attempt(lambda: transport_thread.join(timeout=5.0))
                        safe_to_mark_closed &= joined
                        if joined:
                            alive_checked, still_alive = attempt(transport_thread.is_alive)
                            safe_to_mark_closed &= alive_checked and not still_alive
                            if alive_checked and still_alive:
                                errors.append(RuntimeError("Transport thread did not stop during bridge shutdown"))

        journal = getattr(self.state, "journal", None)
        if journal is None:
            safe_to_mark_closed = False
            errors.append(RuntimeError("Cannot confirm journal state during bridge shutdown"))
        else:
            busy_ok, records = attempt(lambda: list(journal.busy()))
            safe_to_mark_closed &= busy_ok
            if busy_ok:
                for record in records:
                    try:
                        record_state = record.get("state")
                        if record_state == "running":
                            safe_to_mark_closed = False
                            errors.append(RuntimeError("A running task prevents safe bridge shutdown"))
                            continue
                        if record_state != "queued":
                            safe_to_mark_closed = False
                            errors.append(RuntimeError("Journal returned an invalid busy task state"))
                            continue
                        cancelled_ok, cancelled = attempt(
                            lambda task=record["task"]: journal.cancel(task, state="expired")
                        )
                        safe_to_mark_closed &= cancelled_ok
                        if cancelled_ok and (
                            not isinstance(cancelled, dict)
                            or cancelled.get("state") not in {"cancelled", "expired"}
                        ):
                            safe_to_mark_closed = False
                            errors.append(RuntimeError("Queued task cancellation was not durably confirmed"))
                    except BaseException as exc:
                        safe_to_mark_closed = False
                        errors.append(exc)

        try:
            authorization = self.state.authorization
        except BaseException as exc:
            authorization = None
            safe_to_mark_closed = False
            errors.append(exc)
        if authorization:
            revoked = {**authorization, "active": False}
            revoked_ok, _ = attempt(lambda: self.state.save_authorization(revoked))
            safe_to_mark_closed &= revoked_ok
            if revoked_ok:
                persisted_ok, persisted = attempt(self.state._read_authorization)
                safe_to_mark_closed &= persisted_ok and persisted == revoked
                if persisted_ok and persisted != revoked:
                    errors.append(RuntimeError("Authorization revocation was not durably confirmed"))

        if safe_to_mark_closed:
            marked_ok, _ = attempt(self.state.mark_closed)
            if not marked_ok:
                attempt(lambda: closed_path.unlink(missing_ok=True))
        else:
            attempt(lambda: closed_path.unlink(missing_ok=True))

        if not timer_stopped:
            # KillTimer was not confirmed. Preserve a reachable Bridge and its
            # mutex/journal so a later close can retry without admitting a new
            # executor while the old callback may still be registered.
            self._shutdown_retry_required = True
            try:
                builtins._ansa_mcp_plugin_bridge = self
            except BaseException as exc:
                errors.append(exc)
            if not errors:
                errors.append(RuntimeError("Cannot confirm executor timer stopped; retry bridge.close() before starting another bridge"))
            first = errors[0]
            for later in errors[1:]:
                try:
                    first.add_note(f"Additional bridge shutdown failure: {later!r}")
                except (AttributeError, TypeError):
                    break
            raise first

        # Always release these resources, even when reconciliation or an
        # earlier close step failed. The first failure is reported afterward.
        if journal is not None:
            attempt(journal.close)
        if self.mutex is not None:
            attempt(self.mutex.close)

        self.closed = True
        self._shutdown_retry_required = False
        if getattr(builtins, "_ansa_mcp_plugin_bridge", None) is self:
            try:
                del builtins._ansa_mcp_plugin_bridge
            except BaseException as exc:
                errors.append(exc)

        if errors:
            first = errors[0]
            for later in errors[1:]:
                try:
                    first.add_note(f"Additional bridge shutdown failure: {later!r}")
                except (AttributeError, TypeError):
                    break
            raise first


def start(runtime_home=None, show=True):
    existing = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    if existing is not None and getattr(existing, "_panel_close_retry_required", False):
        raise RuntimeError("A prior ANSA panel is not confirmed closed; call its close() to retry before starting a bridge")
    if existing is not None and getattr(existing, "_shutdown_retry_required", False):
        raise RuntimeError("A prior ANSA timer is not confirmed stopped; call its close() to retry before starting a bridge")
    home = runtime_home or os.environ.get("ANSA_MCP_PLUGIN_HOME")
    if not home:
        raise ValueError("Set ANSA_MCP_PLUGIN_HOME or pass runtime_home")
    if existing is not None and not existing.closed:
        if existing.runtime_home != Path(home).resolve():
            raise RuntimeError("This ANSA process already has a bridge for another runtime")
        if show:
            existing.show_panel()
        return existing
    bridge = Bridge(home)
    builtins._ansa_mcp_plugin_bridge = bridge
    if show:
        bridge.show_panel()
    return bridge
