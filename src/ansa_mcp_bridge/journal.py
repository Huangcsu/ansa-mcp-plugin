"""Durable, per-session queue and execution journal for ANSA tasks."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import threading
import time


_IDENTITY_KEYS = ("pid", "process_start", "instance_id", "session_id")
_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "expired"}


class JournalPersistenceError(RuntimeError):
    """Raised when the journal cannot make a durable state transition."""


class TaskCollisionError(ValueError):
    """Raised when one task ID is reused for different request content."""


class Journal:
    """Persist task admission, claim, result, cancellation, and result ACK.

    A journal belongs to exactly one ANSA executor session. ``validate`` passed
    to :meth:`claim` must be a short, side-effect-free identity/model check;
    business operations are run only after the returned ``running`` record has
    committed.
    """

    def __init__(self, session_dir, identity):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.identity = self._copy_identity(identity)
        self.path = self.session_dir / "journal.sqlite3"
        self._lock = threading.RLock()
        self.blocked = False
        self._closed = False
        try:
            self._db = sqlite3.connect(
                str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            self.blocked = True
            self._closed = True
            raise JournalPersistenceError(f"Could not open task journal: {exc}") from exc
        try:
            mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise sqlite3.OperationalError("SQLite did not enable WAL mode")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    task_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired'
                    )),
                    result_json TEXT,
                    error TEXT,
                    acked INTEGER NOT NULL DEFAULT 0 CHECK (acked IN (0, 1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_state_order
                    ON tasks(state, sequence);
                """
            )
        except sqlite3.Error as exc:
            self.blocked = True
            try:
                self._db.close()
            finally:
                self._closed = True
            raise JournalPersistenceError(f"Could not initialize task journal: {exc}") from exc

    @staticmethod
    def _copy_identity(identity):
        if not isinstance(identity, dict) or set(identity) != set(_IDENTITY_KEYS):
            raise ValueError("identity must contain pid, process_start, instance_id, and session_id")
        copied = dict(identity)
        pid = copied["pid"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("identity pid must be a positive integer")
        for key in _IDENTITY_KEYS[1:]:
            if not isinstance(copied[key], str) or not copied[key]:
                raise ValueError(f"identity {key} must be a non-empty string")
        return copied

    @classmethod
    def _assert_json_value(cls, value, label):
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{label} contains a non-finite number")
            return
        if isinstance(value, list):
            for item in value:
                cls._assert_json_value(item, label)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} contains a non-string object key")
                cls._assert_json_value(item, label)
            return
        raise ValueError(f"{label} contains a non-JSON value of type {type(value).__name__}")

    @classmethod
    def _canonical(cls, value, label):
        cls._assert_json_value(value, label)
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be strict JSON: {exc}") from exc

    def _prepare_task(self, task):
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        encoded = self._canonical(task, "task")
        normalized = json.loads(encoded)
        required = {"task_id", "operation_id", "tool", "params", "target", "model", "identity", "expires_at"}
        missing = required.difference(normalized)
        if missing:
            raise ValueError(f"task is missing required fields: {', '.join(sorted(missing))}")
        for key in ("task_id", "operation_id", "tool"):
            if not isinstance(normalized[key], str) or not normalized[key]:
                raise ValueError(f"task {key} must be a non-empty string")
        if not isinstance(normalized["params"], dict):
            raise ValueError("task params must be an object")

        task_identity = normalized["identity"]
        if not isinstance(task_identity, dict) or set(task_identity) != set(_IDENTITY_KEYS):
            raise ValueError("task identity must contain the complete executor identity")
        normalized_identity = self._copy_identity(task_identity)
        if normalized_identity != self.identity:
            raise ValueError("task belongs to a different ANSA executor identity")

        target = normalized["target"]
        if not isinstance(target, dict):
            raise ValueError("task target must be an object")
        target_required = {"backend_id", "instance_id", "session_id", "generation"}
        if target_required.difference(target):
            raise ValueError("task target is missing binding identity fields")
        if not isinstance(target["backend_id"], str) or not target["backend_id"]:
            raise ValueError("task target backend_id must be a non-empty string")
        if target["instance_id"] != self.identity["instance_id"] or target["session_id"] != self.identity["session_id"]:
            raise ValueError("task target does not match this ANSA instance and session")
        generation = target["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("task target generation must be a positive integer")

        model = normalized["model"]
        if not isinstance(model, dict):
            raise ValueError("task model must be an object")
        epoch = model.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("task model epoch must be a positive integer")

        expires_at = normalized["expires_at"]
        if expires_at is not None:
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                raise ValueError("task expires_at must be None or a finite Unix timestamp")
            try:
                finite_expires_at = math.isfinite(float(expires_at))
            except OverflowError:
                finite_expires_at = False
            if not finite_expires_at:
                raise ValueError("task expires_at must be None or a finite Unix timestamp")
        return normalized, encoded

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("task journal is closed")

    @contextmanager
    def _transaction(self):
        self._ensure_open()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except sqlite3.Error as exc:
            self.blocked = True
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise JournalPersistenceError(f"Task journal persistence failed: {exc}") from exc
        except BaseException:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error as exc:
                self.blocked = True
            raise

    def _row_to_record(self, row):
        if row is None:
            return None
        try:
            task = json.loads(row[1])
            result = None if row[4] is None else json.loads(row[4])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.blocked = True
            raise JournalPersistenceError(f"Task journal contains invalid JSON: {exc}") from exc
        return {
            "task_id": row[0],
            "state": row[2],
            "result": result,
            "error": row[3],
            "acked": bool(row[5]),
            "task": task,
        }

    def _select(self, task_id):
        try:
            row = self._db.execute(
                "SELECT task_id, task_json, state, error, result_json, acked "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            self.blocked = True
            raise JournalPersistenceError(f"Task journal read failed: {exc}") from exc
        return self._row_to_record(row)

    def _insert(self, task_id, task_json, state, now):
        self._db.execute(
            "INSERT INTO tasks(task_id, task_json, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, task_json, state, now, now),
        )

    def _update_state(self, task_id, state, now, *, result_json=None, error=None):
        self._db.execute(
            "UPDATE tasks SET state = ?, result_json = ?, error = ?, updated_at = ? "
            "WHERE task_id = ?",
            (state, result_json, error, now, task_id),
        )

    def submit(self, task):
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        encoded = self._canonical(task, "task")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task task_id must be a non-empty string")
        with self._lock:
            with self._transaction():
                prior = self._select(task_id)
                if prior is not None:
                    if self._canonical(prior["task"], "stored task") != encoded:
                        raise TaskCollisionError(f"task_id {task_id!r} was already used for different content")
                    return prior
                if self.blocked:
                    raise JournalPersistenceError("Task journal is blocked after a persistence failure")
                normalized, encoded = self._prepare_task(task)
                now = time.time()
                self._insert(task_id, encoded, "queued", now)
            return self._select(task_id)

    def get(self, task_id):
        with self._lock:
            self._ensure_open()
            return self._select(task_id)

    def next_queued(self):
        with self._lock:
            self._ensure_open()
            try:
                row = self._db.execute(
                    "SELECT task_id, task_json, state, error, result_json, acked "
                    "FROM tasks WHERE state = 'queued' ORDER BY sequence LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                self.blocked = True
                raise JournalPersistenceError(f"Task journal queue read failed: {exc}") from exc
            record = self._row_to_record(row)
            return None if record is None else record["task"]

    def claim(self, task_id, validate):
        if not callable(validate):
            raise TypeError("validate must be callable")
        with self._lock:
            with self._transaction():
                record = self._select(task_id)
                if record is None or record["state"] != "queued":
                    return record
                if self.blocked:
                    raise JournalPersistenceError("Task journal is blocked after a persistence failure")

                task = record["task"]
                now = time.time()
                if task["expires_at"] is not None and task["expires_at"] <= now:
                    self._update_state(task_id, "expired", now, error="Task expired before execution")
                else:
                    try:
                        self._prepare_task(task)
                        verdict = validate(task)
                        if verdict is False:
                            raise RuntimeError("Claim validation rejected the task")
                    except Exception as exc:
                        self._update_state(
                            task_id,
                            "expired",
                            now,
                            error=f"Claim validation failed: {exc}",
                        )
                    else:
                        self._update_state(task_id, "running", now)
            return self._select(task_id)

    def finish(self, task_id, result=None, error=None):
        with self._lock:
            with self._transaction():
                record = self._select(task_id)
                if record is None:
                    raise KeyError(task_id)
                if record["state"] in _TERMINAL_STATES:
                    return record
                if record["state"] != "running":
                    raise RuntimeError(f"Cannot finish task in state {record['state']!r}")
                if error is not None and not isinstance(error, str):
                    raise ValueError("task error must be a string or None")
                # Failures can carry authoritative model_after/partial-effect
                # evidence. The error determines state, not absence of data.
                result_json = None if result is None and error is not None else self._canonical(result, "result")
                state = "failed" if error is not None else "succeeded"
                self._update_state(task_id, state, time.time(), result_json=result_json, error=error)
            return self._select(task_id)

    def cancel(self, task, state="cancelled"):
        if state not in {"cancelled", "expired"}:
            raise ValueError("cancel state must be 'cancelled' or 'expired'")
        normalized, encoded = self._prepare_task(task)
        task_id = normalized["task_id"]
        with self._lock:
            with self._transaction():
                prior = self._select(task_id)
                if prior is not None:
                    if self._canonical(prior["task"], "stored task") != encoded:
                        raise TaskCollisionError(f"task_id {task_id!r} was already used for different content")
                    if prior["state"] != "queued":
                        return prior
                now = time.time()
                expires_at = normalized["expires_at"]
                terminal = "expired" if state == "expired" or (expires_at is not None and expires_at <= now) else "cancelled"
                if prior is None:
                    self._insert(task_id, encoded, terminal, now)
                else:
                    self._update_state(task_id, terminal, now)
            return self._select(task_id)

    def ack(self, task_id):
        with self._lock:
            with self._transaction():
                record = self._select(task_id)
                if record is None:
                    return None
                if record["state"] not in _TERMINAL_STATES:
                    raise RuntimeError(f"Cannot acknowledge task in state {record['state']!r}")
                if record["acked"]:
                    return record
                try:
                    self._db.execute(
                        "UPDATE tasks SET acked = 1, updated_at = ? WHERE task_id = ?",
                        (time.time(), task_id),
                    )
                except sqlite3.Error as exc:
                    self.blocked = True
                    raise JournalPersistenceError(f"Task result ACK failed: {exc}") from exc
            return self._select(task_id)

    def busy(self):
        with self._lock:
            self._ensure_open()
            try:
                rows = self._db.execute(
                    "SELECT task_id, task_json, state, error, result_json, acked "
                    "FROM tasks WHERE state IN ('queued', 'running') ORDER BY sequence"
                ).fetchall()
            except sqlite3.Error as exc:
                self.blocked = True
                raise JournalPersistenceError(f"Task journal busy-state read failed: {exc}") from exc
            return [self._row_to_record(row) for row in rows]

    def recent(self, limit=20):
        """Return a bounded, newest-first history snapshot without modifying rows."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
            raise ValueError("limit must be an integer between 0 and 100")
        with self._lock:
            self._ensure_open()
            try:
                rows = self._db.execute(
                    "SELECT task_id, task_json, state, error, result_json, acked, "
                    "created_at, updated_at FROM tasks ORDER BY sequence DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.Error as exc:
                self.blocked = True
                raise JournalPersistenceError(f"Task journal history read failed: {exc}") from exc
            records = []
            for row in rows:
                record = self._row_to_record(row[:6])
                record["created_at"] = row[6]
                record["updated_at"] = row[7]
                records.append(record)
            return records

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                self._db.close()
            except sqlite3.Error as exc:
                self.blocked = True
                raise JournalPersistenceError(f"Could not close task journal: {exc}") from exc
            finally:
                self._closed = True
