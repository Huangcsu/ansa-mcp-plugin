"""Durable FIFO dispatch and evidence-based reconciliation, never write replay."""

import json
import threading
import sqlite3

from .tasks import TaskStore
from ansa_mcp_shared.operations import TASK_TOOLS, writes_model

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})
TOOLS = TASK_TOOLS


class ContextMismatchError(RuntimeError):
    """Authoritative evidence that the immutable target/model context is stale."""


class TaskNotFoundError(LookupError):
    """The requested durable task record does not exist."""


class TaskScheduler:
    def start_scheduler(self):
        self.tasks = TaskStore(self.runtime_home / "state.sqlite3")
        self.dispatch_blocked = False
        self._stop_scheduler = threading.Event()
        self._scheduler = threading.Thread(target=self._schedule_loop, name="ansa-mcp-scheduler", daemon=True)
        self._scheduler.start()

    def submit_task(self, tool, params, operation_id, expected_target, expected_model, ttl_seconds=300):
        request = {"tool": tool, "params": params, "target": expected_target,
                   "model": expected_model, "ttl_seconds": ttl_seconds}
        with self._lock:
            existing = self.tasks.lookup(operation_id, request)
            if existing is not None:
                return existing  # Idempotency precedes current context checks.
            if self.closed or self.dispatch_blocked:
                raise RuntimeError("Backend admission is stopped")
            if tool not in TOOLS:
                raise ValueError("Unsupported controlled operation")
            snapshot = self.model_snapshot(expected_target)
            if snapshot.get("stopped"):
                raise RuntimeError("The ANSA panel has stopped task admission")
            if snapshot.get("persistence_blocked"):
                raise RuntimeError("Executor persistence failed; new task admission is stopped")
            if expected_model != {"epoch": snapshot["model"]["epoch"]}:
                raise RuntimeError("Model context is missing or stale")
            if writes_model(tool) and not snapshot["model"]["write_enabled"]:
                raise RuntimeError("Model replacement guard is unavailable; writes are closed")
            identity = {k: snapshot[k] for k in ("pid", "process_start", "instance_id", "session_id")}
            try:
                return self.tasks.create(operation_id, request, identity, ttl_seconds)
            except sqlite3.Error:
                self.dispatch_blocked = True
                raise

    def query_task(self, task_id=None, operation_id=None):
        with self._lock:
            record = self.tasks.get(task_id=task_id, operation_id=operation_id)
            if record is None:
                raise TaskNotFoundError("Task or operation was not found")
            if record["dispatch"] == "sent":
                self._sync_task(record)
            return self.tasks.get(task_id=record["task_id"])

    def cancel_task(self, task_id):
        with self._lock:
            record = self.tasks.cancel(task_id)
            if record is None:
                raise LookupError("Task was not found")
            if record["cancel_pending"]:
                self._sync_task(record)
            return self.tasks.get(task_id=task_id)

    def _task_connection(self, task):
        identity = task["identity"]
        registration = next((r for r in self.discover() if all(r[k] == identity[k] for k in identity)), None)
        if registration is None:
            raise ConnectionError("Original executor session is unavailable; result remains unknown")
        return self._connect(registration)

    def _merge_record(self, record, received):
        if received is None:
            return None
        if received.get("task") != record["task"] or received.get("task_id") != record["task_id"]:
            raise RuntimeError("Executor result has a different immutable task identity")
        return self.tasks.merge(received)

    def _sync_task(self, record):
        try:
            connection = self._task_connection(record["task"])
            received = connection.call("task_query", task_id=record["task_id"])
            if received and received.get("journal_blocked") and received["state"] == "running":
                self.tasks.unknown(record["task_id"], "Executor persistence failed after claim; operation may have executed")
                return
            current = self.store.current()
            original = record["task"]["target"]
            same_grant = bool(current and current["state"] == "active" and all(
                original[k] == current[k] for k in original) and original["backend_id"] == self.backend_id)
            needs_cancel = record["cancel_pending"] or not same_grant or received is None
            if needs_cancel and (received is None or received["state"] == "queued"):
                received = connection.call("task_cancel", task=record["task"],
                                           state="cancelled" if record["cancel_pending"] else "expired")
            merged = self._merge_record(record, received)
            if merged and merged["state"] in TERMINAL and not merged["acked"]:
                # The local COMMIT above must precede the executor ACK.
                acknowledged = connection.call("result_ack", task_id=record["task_id"])
                if acknowledged and acknowledged.get("acked") is True:
                    self.tasks.ack(record["task_id"])
        except (OSError, RuntimeError, ValueError, LookupError) as exc:
            self.tasks.unknown(record["task_id"], str(exc))

    def _reconcile_target(self, instance_id, session_id):
        for record in self.tasks.busy(instance_id, session_id):
            if record["dispatch"] == "sent":
                self._sync_task(record)
            elif record["task"]["target"]["backend_id"] != self.backend_id:
                self.tasks.merge({"task_id": record["task_id"], "state": "expired",
                                  "error": "Backend identity changed before dispatch"})

    def _schedule_loop(self):
        while not self._stop_scheduler.wait(0.2):
            try:
                with self._lock:
                    if self.closed or self.dispatch_blocked:
                        continue
                    self.tasks.expire()
                    for record in self.tasks.pending():
                        if record["dispatch"] == "sent":
                            self._sync_task(record)
                        elif record["task"]["target"]["backend_id"] != self.backend_id:
                            self.tasks.merge({"task_id": record["task_id"], "state": "expired",
                                              "error": "Backend restarted before dispatch"})
                    current = self.store.current()
                    if not current or current["state"] != "active" or current["backend_id"] != self.backend_id:
                        continue
                    busy = self.tasks.busy(current["instance_id"], current["session_id"])
                    if any(r["dispatch"] == "sent" for r in busy):
                        continue
                    record = self.tasks.next_queued()
                    if record is None:
                        continue
                    task = record["task"]
                    try:
                        snapshot = self.model_snapshot(task["target"])
                        if snapshot.get("stopped"):
                            continue
                        if snapshot.get("persistence_blocked"):
                            continue
                        if task["model"] != {"epoch": snapshot["model"]["epoch"]}:
                            raise ContextMismatchError("Model replaced before dispatch")
                    except ContextMismatchError as exc:
                        self.tasks.merge({"task_id": record["task_id"], "state": "expired", "error": str(exc)})
                        continue
                    except (RuntimeError, OSError, ValueError) as exc:
                        self.last_error = "Waiting for authoritative main-thread context: " + str(exc)
                        continue
                    record = self.tasks.dispatch(record["task_id"])
                    if record["state"] != "queued":
                        continue
                    try:
                        received = self._task_connection(task).call("task_submit", task=task)
                        self._merge_record(record, received)
                    except (RuntimeError, OSError, ValueError) as exc:
                        self.tasks.unknown(record["task_id"], str(exc))
            except Exception as exc:
                # Durable state failure disables both new admission and dispatch.
                self.dispatch_blocked = True
                self.last_error = "Task persistence or scheduling failed: " + str(exc)
            finally:
                try:
                    with self._lock:
                        if not self.closed:
                            self._notify_panels()
                except Exception as exc:
                    self.dispatch_blocked = True
                    self.last_error = "Panel state persistence or notification failed: " + str(exc)
