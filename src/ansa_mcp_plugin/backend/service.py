"""Backend lifecycle, discovery, explicit binding and durable local services."""

import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from ansa_mcp_shared.identity import process_start_filetime, same_process_alive

from .connection import BridgeConnection
from .lock import BackendLock
from .security import ensure_private_directory, ensure_safe_parent, verify_private_path
from .store import BindingStore
from .memory import MemoryStore
from .log_reader import TaskLogReader
from .scheduler import TaskScheduler, ContextMismatchError


_CONNECTION_TIMEOUT = 2.0
_AUTHENTICATION_TIMEOUT = 5.0
_RECONNECT_MAX_SECONDS = 15.0


class Service(TaskScheduler):
    def __init__(self, runtime_home):
        ensure_safe_parent(runtime_home)
        self.runtime_home = Path(runtime_home).resolve()
        ensure_private_directory(self.runtime_home)
        for name in ("config", "instances"):
            ensure_private_directory(self.runtime_home / name)
        for name in ("config/token.txt", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm", "backend.lock"):
            path = self.runtime_home / name
            if path.exists():
                verify_private_path(path)
        self.lock = BackendLock(self.runtime_home / "backend.lock")
        try:
            self.backend_id = str(uuid.uuid4())
            token_file = self.runtime_home / "config" / "token.txt"
            if not token_file.exists():
                with token_file.open("x", encoding="ascii") as stream:
                    stream.write(secrets.token_hex(32))
            verify_private_path(token_file)
            self.token = token_file.read_text(encoding="ascii").strip()
            if len(self.token) != 64 or any(c not in "0123456789abcdef" for c in self.token):
                raise RuntimeError("Invalid runtime token")
            self.store = BindingStore(self.runtime_home / "state.sqlite3")
            try:
                self.memory = MemoryStore(self.runtime_home / "state.sqlite3")
            except Exception:
                self.store.close()
                raise
        except Exception:
            self.lock.close()
            raise
        self.connections = {}
        self._connecting = set()
        self._connection_diagnostics = {}
        self._lock = threading.RLock()
        self.last_error = None
        self.closed = False
        try:
            self.start_scheduler()
            self.log_reader = TaskLogReader(self.runtime_home, self.tasks)
            self._discovery = threading.Thread(target=self._discover_loop, name="ansa-mcp-discovery", daemon=True)
            self._discovery.start()
        except Exception:
            with self._lock:
                self.closed = True
                stop = getattr(self, "_stop_scheduler", None)
                if stop is not None:
                    stop.set()
                for connection in self.connections.values():
                    connection.close()
                self.connections.clear()
            scheduler = getattr(self, "_scheduler", None)
            if scheduler is not None and scheduler.ident is not None:
                scheduler.join(timeout=10)
            tasks = getattr(self, "tasks", None)
            if tasks is not None:
                tasks.close()
            self.memory.close()
            self.store.close()
            self.lock.close()
            raise

    def discover(self):
        registrations = []
        for path in (self.runtime_home / "instances").glob("*/connection.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if any(key not in data for key in ("version", "host", "port", "pid", "process_start", "instance_id", "session_id")):
                    continue
                if data["version"] != 1 or not isinstance(data["session_id"], str) or not data["session_id"]:
                    continue
                if data.get("host") != "127.0.0.1" or data.get("instance_id") != path.parent.name:
                    continue
                if not (1 <= data["port"] <= 65535) or process_start_filetime(data["pid"]) != data["process_start"]:
                    continue
                registrations.append(data)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return registrations

    def _discover_loop(self):
        """认证连接在调度锁外完成；目标授权仍只响应面板请求。"""
        while not self._stop_scheduler.wait(.5):
            registrations = self.discover()
            self._prune_connection_state(registrations)
            for registration in registrations:
                try:
                    self._connect(registration)
                except Exception:
                    # _connect records actual failures; in-progress connections
                    # and backoff are not new failures and must not extend it.
                    if self.closed:
                        return

    def _connect(self, registration):
        instance_id = registration["instance_id"]
        with self._lock:
            if self.closed:
                raise RuntimeError("Backend is stopping")
            existing = self.connections.get(instance_id)
            if existing and not existing.closed and existing.registration == registration:
                return existing
            if instance_id in self._connecting:
                raise ConnectionError("ANSA bridge connection is being established")
            if existing:
                self.connections.pop(instance_id, None)
                if existing.closed and existing.failure_reason:
                    self._record_connection_failure(
                        registration, existing.failure_stage or "reader", existing.failure_reason
                    )
                existing.close()
            remaining = self._connection_retry_remaining(registration)
            if remaining > 0:
                diagnostic = self._connection_diagnostics[instance_id]
                raise ConnectionError(
                    f"ANSA bridge reconnect is delayed for {remaining:.1f}s: {diagnostic['failure_reason']}"
                )
            self._connecting.add(instance_id)
        connection = None
        try:
            connection = BridgeConnection(
                registration, self.token, self._panel_bind, backend_id=self.backend_id,
                connect_timeout=_CONNECTION_TIMEOUT, auth_timeout=_AUTHENTICATION_TIMEOUT, start_reader=False,
            )
            with self._lock:
                if self.closed:
                    raise RuntimeError("Backend is stopping")
                self.connections[instance_id] = connection
                connection.start_reader()
                self._clear_connection_failure(instance_id)
            return connection
        except Exception as exc:
            if connection is not None:
                connection.close()
                with self._lock:
                    if self.connections.get(instance_id) is connection:
                        self.connections.pop(instance_id, None)
            self._record_connection_failure(registration, getattr(exc, "connection_stage", "connect"), exc)
            raise
        finally:
            with self._lock:
                self._connecting.discard(instance_id)

    @staticmethod
    def _registration_identity(registration):
        return tuple(registration.get(key) for key in ("pid", "process_start", "session_id", "port"))

    @staticmethod
    def _retry_delay(failure_count):
        exponent = min(max(failure_count - 1, 0), 4)
        return min(float(2 ** exponent), _RECONNECT_MAX_SECONDS)

    @staticmethod
    def _failure_reason(reason):
        if isinstance(reason, BaseException):
            return str(reason).strip() or type(reason).__name__
        return str(reason).strip() or "Unknown connection failure"

    def _record_connection_failure(self, registration, stage, reason):
        instance_id = registration["instance_id"]
        identity = self._registration_identity(registration)
        with self._lock:
            previous = self._connection_diagnostics.get(instance_id)
            count = previous["failure_count"] + 1 if previous and previous["registration"] == identity else 1
            delay = self._retry_delay(count)
            self._connection_diagnostics[instance_id] = {
                "registration": identity,
                "failure_stage": stage or "connect",
                "failure_reason": self._failure_reason(reason),
                "failure_count": count,
                "retry_at": time.monotonic() + delay,
            }

    def _clear_connection_failure(self, instance_id):
        with self._lock:
            self._connection_diagnostics.pop(instance_id, None)

    def _connection_retry_remaining(self, registration):
        instance_id = registration["instance_id"]
        with self._lock:
            diagnostic = self._connection_diagnostics.get(instance_id)
            if not diagnostic or diagnostic["registration"] != self._registration_identity(registration):
                return 0.0
            return max(0.0, diagnostic["retry_at"] - time.monotonic())

    def _connection_diagnostics_snapshot(self):
        now = time.monotonic()
        with self._lock:
            return {
                instance_id: {
                    "failure_stage": diagnostic["failure_stage"],
                    "failure_reason": diagnostic["failure_reason"],
                    "failure_count": diagnostic["failure_count"],
                    "retry_in_seconds": round(max(0.0, diagnostic["retry_at"] - now), 3),
                }
                for instance_id, diagnostic in sorted(self._connection_diagnostics.items())
            }

    def _prune_connection_state(self, registrations):
        active = {item["instance_id"]: self._registration_identity(item) for item in registrations}
        with self._lock:
            for instance_id, connection in list(self.connections.items()):
                if active.get(instance_id) == self._registration_identity(connection.registration):
                    continue
                self.connections.pop(instance_id, None)
                try:
                    connection.close()
                except Exception:
                    pass
            for instance_id, diagnostic in list(self._connection_diagnostics.items()):
                if active.get(instance_id) != diagnostic["registration"]:
                    self._connection_diagnostics.pop(instance_id, None)

    def _panel_bind(self, instance_id):
        try:
            self.bind(instance_id)
            self.last_error = None
            self._notify_panels()
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
                if not self.closed:
                    self.store.audit_failure(instance_id, str(exc))
                    self._notify_panels()

    def _notify_panels(self):
        current = self.store.current()
        active = current if current and current["state"] == "active" and current["backend_id"] == self.backend_id else None
        try:
            tasks = [{"task_id": r["task_id"], "operation_id": r["operation_id"], "state": r["state"],
                      "tool": r["task"]["tool"], "cancel_pending": r["cancel_pending"], "target": r["task"]["target"]}
                     for r in self.tasks.recent(30)]
        except Exception as exc:
            self.dispatch_blocked = True
            self.last_error = "Panel task history persistence failed: " + str(exc)
            tasks = []
        cached = getattr(self, "_panel_model", None)
        model = cached[1] if active and cached and cached[0] == active["instance_id"] else None
        snapshot = getattr(self, "_panel_snapshot", None)
        if active and snapshot and all(snapshot.get(key) == active[key] for key in ("pid", "instance_id", "session_id")):
            expected_auth = {"backend_id": self.backend_id, "generation": active["generation"], "active": True}
            if snapshot.get("authorization") == expected_auth:
                active = {**active, "model": snapshot["model"], "version": snapshot.get("version"),
                          "deck": snapshot.get("deck"), "deck_name": snapshot.get("deck_name"),
                          "authorization": expected_auth, "model_snapshot_cached": True}
        value = {"active_target": active, "active_model": model, "tasks": tasks,
                 "dispatch_blocked": self.dispatch_blocked, "error": self.last_error}
        signature = json.dumps(value, sort_keys=True, allow_nan=False)
        for connection in list(self.connections.values()):
            if getattr(connection, "panel_state_signature", None) == signature:
                continue
            try:
                connection.call("backend_status", state=value)
                connection.panel_state_signature = signature
            except (OSError, RuntimeError):
                pass

    def bind(self, instance_id):
        """Only the ANSA panel invokes this through an authenticated event."""
        with self._lock:
            if self.closed:
                raise RuntimeError("Backend is stopping")
            registrations = {item["instance_id"]: item for item in self.discover()}
            if instance_id not in registrations:
                raise LookupError("Panel target is not a live registered process")
            candidate = self._connect(registrations[instance_id])
            snapshot = candidate.call("probe")
            self._check_identity(snapshot, registrations[instance_id])
            current = self.store.current()
            if current:
                self._reconcile_target(current["instance_id"], current["session_id"])
                busy = self.tasks.busy(current["instance_id"], current["session_id"])
                alive = same_process_alive(current.get("pid"), current.get("process_start"))
                if busy and alive:
                    raise RuntimeError("Old target has queued, running or unknown tasks; finish or confirm cancellation before switching")
            candidate_tasks = candidate.call("task_status")
            if candidate_tasks["busy"] or candidate_tasks["blocked"]:
                raise RuntimeError("Candidate executor requires task recovery before binding")
            for registration in registrations.values():
                actual = self._connect(registration).call("authorization")
                if actual and actual.get("active"):
                    if not current or registration["instance_id"] != current["instance_id"] or (actual.get("backend_id"), actual.get("generation")) != (current["backend_id"], current["generation"]):
                        raise RuntimeError("Unaccounted residual execution grant; explicit recovery required")
            old_connection = None
            if current and current["state"] in ("intent", "active"):
                old_registration = registrations.get(current["instance_id"])
                if old_registration:
                    if old_registration["session_id"] != current["session_id"]:
                        raise RuntimeError("Old ANSA session identity changed; recovery required")
                    old_connection = self._connect(old_registration)
                    old_auth = old_connection.call("authorization")
                    if old_auth and old_auth.get("active"):
                        if (old_auth.get("backend_id"), old_auth.get("generation")) != (current["backend_id"], current["generation"]):
                            raise RuntimeError("Residual authorization differs from the database")
                        try:
                            old_connection.call("revoke", backend_id=current["backend_id"])
                        except (RuntimeError, OSError):
                            pass  # Query the authoritative state; never infer cancellation.
                        remaining = old_connection.call("authorization")
                        if remaining and remaining.get("active"):
                            raise RuntimeError("Old target revocation was not confirmed")
                else:
                    if same_process_alive(current.get("pid"), current.get("process_start")):
                        closed_file = self.runtime_home / "instances" / current["instance_id"] / "sessions" / current["session_id"] / "closed.json"
                        if not closed_file.is_file():
                            raise RuntimeError("Old target is alive without a confirmed executor shutdown")
                        verify_private_path(closed_file)
                        closed = json.loads(closed_file.read_text(encoding="utf-8"))
                        if any(closed.get(k) != current.get(k) for k in ("pid", "process_start", "instance_id", "session_id")) or closed.get("executor_stopped") is not True:
                            raise RuntimeError("Old executor shutdown identity is not confirmed")
                self.store.record(current["backend_id"], current["instance_id"], current["session_id"],
                                  current["generation"], "revoked", "revocation or process exit confirmed",
                                  pid=current.get("pid"), process_start=current.get("process_start"))
            generation = (current["generation"] if current else 0) + 1
            self.store.record(self.backend_id, snapshot["instance_id"], snapshot["session_id"], generation, "intent",
                              pid=snapshot["pid"], process_start=snapshot["process_start"])
            authorization = {"backend_id": self.backend_id, "generation": generation, "active": True}
            try:
                candidate.call("grant", expected={k: snapshot[k] for k in ("pid", "process_start", "instance_id", "session_id")},
                               authorization=authorization)
            except (RuntimeError, OSError):
                pass  # Lost acknowledgement is resolved by the following state query.
            confirmed = candidate.call("authorization")
            if confirmed != authorization:
                raise RuntimeError("Grant acknowledgement did not match ANSA state")
            self.store.record(self.backend_id, snapshot["instance_id"], snapshot["session_id"], generation, "active",
                              pid=snapshot["pid"], process_start=snapshot["process_start"])
            self._panel_model = (snapshot["instance_id"], snapshot["model"])
            self._panel_snapshot = {**snapshot, "authorization": confirmed}
            return snapshot

    @staticmethod
    def _check_identity(snapshot, registration):
        for key in ("pid", "process_start", "instance_id", "session_id"):
            if snapshot.get(key) != registration.get(key):
                raise ContextMismatchError("Main-thread target identity differs from registration")
        if not snapshot.get("main_thread"):
            raise ContextMismatchError("ANSA probe did not execute on the main thread")

    def status(self):
        with self._lock:
            current = self.store.current()
            available = []
            active = None
            active_model = None
            registrations = self.discover()
            self._prune_connection_state(registrations)
            for registration in registrations:
                entry = {k: registration[k] for k in ("instance_id", "session_id", "pid", "process_start")}
                stage = "connect"
                try:
                    connection = self._connect(registration)
                    stage = "main_thread_probe"
                    snapshot = connection.call("probe")
                    self._check_identity(snapshot, registration)
                    entry.update(ready=True, connection_state="unbound", deck=snapshot["deck"], deck_name=snapshot.get("deck_name"),
                                 version=snapshot.get("version"), model=snapshot["model"])
                    if (current and current["state"] == "active" and current["backend_id"] == self.backend_id
                            and all(snapshot[k] == current[k] for k in ("instance_id", "session_id", "pid", "process_start"))
                            and snapshot["authorization"] == {"backend_id": self.backend_id, "generation": current["generation"], "active": True}):
                        active, active_model = current, snapshot["model"]
                        entry["connection_state"] = "bound"
                except Exception as exc:
                    entry.update(ready=False, connection_state="unavailable", failure_stage=stage, error=str(exc))
                available.append(entry)
            return {"backend_id": self.backend_id, "active_target": active,
                    "active_model": active_model,
                    "recovery_required": bool(current and current["state"] in ("intent", "active") and not active),
                    "instances": available, "last_error": self.last_error,
                    "dispatch_blocked": self.dispatch_blocked,
                    "connection_diagnostics": self._connection_diagnostics_snapshot(),
                    "tasks": [{"task_id": r["task_id"], "state": r["state"], "cancel_pending": r["cancel_pending"],
                               "target": r["task"]["target"]} for r in self.tasks.pending()]}

    def model_snapshot(self, expected_target):
        with self._lock:
            current = self.store.current()
            if not current or current["state"] != "active" or current["backend_id"] != self.backend_id:
                raise ContextMismatchError("No ANSA instance is bound by its panel")
            if any(expected_target.get(k) != current[k] for k in ("backend_id", "instance_id", "session_id", "generation")):
                raise ContextMismatchError("Target context is missing or stale")
            registration = next((r for r in self.discover() if r["instance_id"] == current["instance_id"]), None)
            if not registration:
                raise ConnectionError("Bound ANSA registration is unavailable")
            connection = self._connect(registration)
            authorization = connection.call("authorization")
            if authorization != {"backend_id": current["backend_id"], "generation": current["generation"], "active": True}:
                raise ContextMismatchError("ANSA authorization differs from the active binding")
            snapshot = connection.call("model_snapshot", expected_target=expected_target)
            self._check_identity(snapshot, registration)
            self._panel_model = (snapshot["instance_id"], snapshot["model"])
            self._panel_snapshot = snapshot
            return snapshot

    def panel_show(self, expected_target):
        with self._lock:
            self.model_snapshot(expected_target)
            connection = self.connections.get(expected_target["instance_id"])
            if connection is None or connection.closed:
                raise ConnectionError("Bound ANSA bridge is unavailable")
            return connection.call("panel_show", expected_target=expected_target)

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._stop_scheduler.set()
            for connection in self.connections.values():
                connection.close()
        self._scheduler.join(timeout=10)
        self._discovery.join(timeout=10)
        with self._lock:
            self.tasks.close()
            self.memory.close()
            self.store.close()
            self.lock.close()
