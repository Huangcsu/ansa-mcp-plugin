"""Durable, idempotent task queue for the external ANSA backend."""

import json
import math
import sqlite3
import threading
import time
import uuid


_NONTERMINAL = frozenset(("queued", "running", "unknown"))
_TERMINAL = frozenset(("succeeded", "failed", "cancelled", "expired"))
_ALL_STATES = _NONTERMINAL | _TERMINAL
_REQUEST_FIELDS = frozenset(("tool", "params", "target", "model", "ttl_seconds"))
_TASK_STATES = frozenset(("queued", "running", "succeeded", "failed", "cancelled", "expired", "unknown"))


def _json(value):
    """Return stable JSON and reject values JSON cannot safely represent."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _decode(value):
    return json.loads(value) if value is not None else None


class TaskStore:
    """SQLite-backed FIFO task queue sharing the backend state database."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False,
                                   isolation_level=None)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'queued', 'running', 'succeeded', 'failed',
                        'cancelled', 'expired', 'unknown')),
                    dispatch TEXT NOT NULL CHECK (dispatch IN ('not_sent', 'sent')),
                    cancel_pending INTEGER NOT NULL DEFAULT 0 CHECK (cancel_pending IN (0, 1)),
                    result_json TEXT,
                    error_json TEXT,
                    acked INTEGER NOT NULL DEFAULT 0 CHECK (acked IN (0, 1)),
                    expires_at REAL
                );
                CREATE INDEX IF NOT EXISTS tasks_fifo ON tasks(seq);
                CREATE TABLE IF NOT EXISTS operation_keys (
                    operation_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id)
                );
            """)
        except Exception:
            self._db.close()
            raise

    @staticmethod
    def _request_json(request):
        if not isinstance(request, dict):
            raise TypeError("request must be a dictionary")
        if frozenset(request) != _REQUEST_FIELDS:
            raise ValueError("request must contain tool, params, target, model, and ttl_seconds")
        if not isinstance(request["tool"], str) or not request["tool"]:
            raise ValueError("request.tool must be a non-empty string")
        return _json(request)

    @staticmethod
    def _ttl(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("ttl_seconds must be a finite non-negative number or None")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("ttl_seconds must be a finite non-negative number or None")
        return value

    @staticmethod
    def _identity(identity):
        if not isinstance(identity, dict):
            raise TypeError("identity must be a dictionary")
        # Round-trip to detach the persisted task from caller-owned objects.
        return json.loads(_json(identity))

    @staticmethod
    def _record(row):
        if row is None:
            return None
        (task_id, operation_id, state, dispatch, cancel_pending, task_json,
         result_json, error_json, acked) = row
        return {
            "task_id": task_id,
            "operation_id": operation_id,
            "state": state,
            "dispatch": dispatch,
            "cancel_pending": bool(cancel_pending),
            "task": _decode(task_json),
            "result": _decode(result_json),
            "error": _decode(error_json),
            "acked": bool(acked),
        }

    @staticmethod
    def _select_sql():
        return ("SELECT task_id,operation_id,state,dispatch,cancel_pending,"
                "task_json,result_json,error_json,acked FROM tasks ")

    def _get_locked(self, task_id=None, operation_id=None):
        if task_id is not None:
            if operation_id is None:
                row = self._db.execute(self._select_sql() +
                                        "WHERE task_id=?", (task_id,)).fetchone()
            else:
                row = self._db.execute(self._select_sql() +
                                        "WHERE task_id=? AND operation_id=?",
                                        (task_id, operation_id)).fetchone()
        elif operation_id is not None:
            row = self._db.execute(self._select_sql() +
                                    "WHERE operation_id=?", (operation_id,)).fetchone()
        else:
            return None
        return self._record(row)

    def _reject_memory_operation(self, operation_id):
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_operations'"
        ).fetchone()
        if exists and self._db.execute(
                "SELECT 1 FROM memory_operations WHERE operation_id=?", (operation_id,)).fetchone():
            raise ValueError("operation_id is already mapped to a memory write")

    def lookup(self, operation_id, request):
        """Find an operation before current target/model validation."""
        request_json = self._request_json(request)
        with self._lock:
            row = self._db.execute(
                "SELECT request_json,task_id FROM operation_keys WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                self._reject_memory_operation(operation_id)
                return None
            if row[0] != request_json:
                raise ValueError("operation_id is already mapped to a different request")
            record = self._get_locked(task_id=row[1])
            if record is None:
                raise RuntimeError("operation mapping refers to a missing task")
            return record

    def create(self, operation_id, request, identity, ttl_seconds):
        """Atomically create the operation mapping and its immutable task."""
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        request_json = self._request_json(request)
        request_copy = json.loads(request_json)
        ttl = self._ttl(ttl_seconds)
        if self._ttl(request_copy["ttl_seconds"]) != ttl:
            raise ValueError("ttl_seconds must match the immutable request")
        identity_copy = self._identity(identity)
        expires_at = time.time() + ttl if ttl is not None else None

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT request_json,task_id FROM operation_keys WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != request_json:
                        raise ValueError("operation_id is already mapped to a different request")
                    record = self._get_locked(task_id=existing[1])
                    if record is None:
                        raise RuntimeError("operation mapping refers to a missing task")
                    self._db.execute("COMMIT")
                    return record

                self._reject_memory_operation(operation_id)
                task_id = str(uuid.uuid4())
                task = {
                    "task_id": task_id,
                    "operation_id": operation_id,
                    "tool": request_copy["tool"],
                    "params": request_copy["params"],
                    "target": request_copy["target"],
                    "model": request_copy["model"],
                    "identity": identity_copy,
                    "expires_at": expires_at,
                }
                task_json = _json(task)
                self._db.execute(
                    "INSERT INTO tasks(task_id,operation_id,request_json,task_json,"
                    "state,dispatch,expires_at) VALUES (?,?,?,?, 'queued','not_sent',?)",
                    (task_id, operation_id, request_json, task_json, expires_at),
                )
                self._db.execute(
                    "INSERT INTO operation_keys(operation_id,request_json,task_id) VALUES (?,?,?)",
                    (operation_id, request_json, task_id),
                )
                record = self._get_locked(task_id=task_id)
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def get(self, task_id=None, operation_id=None):
        with self._lock:
            return self._get_locked(task_id, operation_id)

    def pending(self):
        """Return live tasks and sent terminal results awaiting acknowledgement."""
        with self._lock:
            rows = self._db.execute(
                self._select_sql() +
                "WHERE state IN ('queued','running','unknown') "
                "OR (dispatch='sent' AND state IN ('succeeded','failed','cancelled','expired') AND acked=0) "
                "ORDER BY seq"
            ).fetchall()
            return [self._record(row) for row in rows]

    def recent(self, limit=30):
        """Read bounded history without requiring a current active target."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self._lock:
            rows = self._db.execute(self._select_sql() + "ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            return [self._record(row) for row in rows]

    def history_page(self, limit=30, state=None, before_sequence=None):
        """Read a durable, newest-first history page using sequence-based pagination."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if state is not None and (type(state) is not str or state not in _TASK_STATES):
            raise ValueError("state must be one of the defined task states or None")
        if before_sequence is not None and (
                type(before_sequence) is not int or not 1 <= before_sequence <= 2**63 - 1):
            raise ValueError("before_sequence must be a positive signed 64-bit integer or None")

        clauses = []
        parameters = []
        if state is not None:
            clauses.append("state=?")
            parameters.append(state)
        if before_sequence is not None:
            clauses.append("seq<?")
            parameters.append(before_sequence)
        where = "WHERE " + " AND ".join(clauses) + " " if clauses else ""

        with self._lock:
            rows = self._db.execute(
                "SELECT seq,task_id,operation_id,state,dispatch,cancel_pending,"
                "task_json,result_json,error_json,acked FROM tasks " +
                where + "ORDER BY seq DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return {
            "tasks": [self._record(row[1:]) for row in page_rows],
            "has_more": has_more,
            "next_before_sequence": page_rows[-1][0] if has_more else None,
        }

    def busy(self, instance_id, session_id):
        """Return nonterminal tasks owned by the specified ANSA session."""
        with self._lock:
            rows = self._db.execute(
                self._select_sql() +
                "WHERE state IN ('queued','running','unknown') ORDER BY seq"
            ).fetchall()
            return [record for record in map(self._record, rows)
                    if record["task"]["identity"].get("instance_id") == instance_id
                    and record["task"]["identity"].get("session_id") == session_id]

    def next_queued(self):
        with self._lock:
            row = self._db.execute(
                self._select_sql() +
                "WHERE state='queued' AND dispatch='not_sent' ORDER BY seq LIMIT 1"
            ).fetchone()
            return self._record(row)

    def dispatch(self, task_id):
        """Durably mark a queued task sent before its network delivery."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT state,dispatch,expires_at FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                state, dispatch, expires_at = row
                if state == "queued" and dispatch == "not_sent":
                    if expires_at is not None and expires_at <= time.time():
                        self._db.execute(
                            "UPDATE tasks SET state='expired' WHERE task_id=?",
                            (task_id,),
                        )
                    else:
                        self._db.execute(
                            "UPDATE tasks SET dispatch='sent' WHERE task_id=?",
                            (task_id,),
                        )
                record = self._get_locked(task_id=task_id)
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def cancel(self, task_id):
        """Cancel an unsent task or durably request confirmation for a sent one."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT state,dispatch FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                state, dispatch = row
                if state == "queued" and dispatch == "not_sent":
                    self._db.execute(
                        "UPDATE tasks SET state='cancelled' WHERE task_id=?",
                        (task_id,),
                    )
                elif state in ("queued", "unknown") and dispatch == "sent":
                    self._db.execute(
                        "UPDATE tasks SET cancel_pending=1 WHERE task_id=?",
                        (task_id,),
                    )
                elif state == "running":
                    raise ValueError("a task already claimed by the executor cannot be cancelled")
                record = self._get_locked(task_id=task_id)
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def expire(self, now=None):
        """Expire only tasks that have not been sent; return changed records."""
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                ids = [row[0] for row in self._db.execute(
                    "SELECT task_id FROM tasks WHERE state='queued' "
                    "AND dispatch='not_sent' AND expires_at IS NOT NULL AND expires_at<=? "
                    "ORDER BY seq", (now,),
                ).fetchall()]
                self._db.executemany(
                    "UPDATE tasks SET state='expired' WHERE task_id=? AND state='queued' AND dispatch='not_sent'",
                    ((task_id,) for task_id in ids),
                )
                records = [self._get_locked(task_id=task_id) for task_id in ids]
                self._db.execute("COMMIT")
                return records
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def unknown(self, task_id, error):
        """Record an uncertain outcome without making it dispatchable again."""
        error_json = _json(error)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE tasks SET state='unknown',error_json=? "
                    "WHERE task_id=? AND dispatch='sent' "
                    "AND state IN ('queued','running','unknown')",
                    (error_json, task_id),
                )
                record = self._get_locked(task_id=task_id)
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def merge(self, executor_record):
        """Merge an executor journal update; terminal outcomes are immutable."""
        if not isinstance(executor_record, dict):
            raise TypeError("executor_record must be a dictionary")
        task_id = executor_record.get("task_id")
        state = executor_record.get("state")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("executor_record.task_id must be a non-empty string")
        if state not in _ALL_STATES:
            raise ValueError("executor_record.state is invalid")
        result_json = _json(executor_record.get("result"))
        error_json = _json(executor_record.get("error"))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT state FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                current = row[0]
                allowed = (
                    (current == "queued" and (state == "running" or state in _TERMINAL))
                    or (current == "running" and state in _TERMINAL)
                    or (current == "unknown" and (state == "running" or state in _TERMINAL))
                )
                if allowed:
                    self._db.execute(
                        "UPDATE tasks SET state=?,result_json=?,error_json=?,cancel_pending=0 "
                        "WHERE task_id=?",
                        (state, result_json, error_json, task_id),
                    )
                record = self._get_locked(task_id=task_id)
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def settle_unclaimed(self, task_id, state):
        """Settle a sent task only after ANSA proves it remains unclaimed."""
        if state not in ("cancelled", "expired"):
            raise ValueError("unclaimed task state must be cancelled or expired")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT state,dispatch FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                current, dispatch = row
                if current in ("queued", "unknown") and dispatch == "sent":
                    self._db.execute(
                        "UPDATE tasks SET state=?,cancel_pending=0,error_json=NULL "
                        "WHERE task_id=?", (state, task_id),
                    )
                record = self._get_locked(task_id=task_id)
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def ack(self, task_id):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    "UPDATE tasks SET acked=1 WHERE task_id=?", (task_id,)
                )
                record = self._get_locked(task_id=task_id) if cursor.rowcount else None
                self._db.execute("COMMIT")
                return record
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
