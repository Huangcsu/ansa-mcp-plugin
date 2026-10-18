"""Persistent, runtime-local preferences and notes."""

import datetime
import json
import math
import sqlite3
import threading
import time
import unicodedata


_MAX_JSON_DEPTH = 32
_MAX_RECORD_JSON_DEPTH = 40
_MAX_VALUE_JSON_LENGTH = 4000
_MAX_SQLITE_INTEGER = 2**63 - 1
_PREFERENCE_FIELDS = frozenset(("kind", "key", "value", "expected_revision", "note"))
_NOTE_FIELDS = frozenset(("kind", "text", "tag"))
_PREFERENCE_RECORD_FIELDS = frozenset(("value", "revision", "updated", "updated_local", "note"))
_NOTE_RECORD_FIELDS = frozenset(("sequence", "text", "tag", "created", "created_local"))


class MemoryConflictError(ValueError):
    """An operation key or expected preference revision conflicts with stored state."""


def _exact_string(value, name, maximum, *, nonempty=False):
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    if len(value) > maximum or (nonempty and not value):
        raise ValueError(f"{name} must contain 1 to {maximum} characters" if nonempty
                         else f"{name} must contain at most {maximum} characters")
    return value


def _key(value):
    value = _exact_string(value, "key", 120, nonempty=True)
    if value.strip() != value or any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("key must not have surrounding whitespace or control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("key must be valid UTF-8 text") from exc
    return value


def _strict_json_copy(value, depth=0, active=None, max_depth=_MAX_JSON_DEPTH):
    if depth > max_depth:
        raise ValueError(f"JSON value exceeds maximum depth {max_depth}")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if type(value) not in (list, dict):
        raise ValueError("value must contain only strict JSON types")

    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise ValueError("JSON value must not contain cycles")
    active.add(identity)
    try:
        if type(value) is list:
            return [_strict_json_copy(item, depth + 1, active, max_depth)
                    for item in value]
        copied = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            copied[key] = _strict_json_copy(item, depth + 1, active, max_depth)
        return copied
    finally:
        active.remove(identity)


def _canonical_json(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False)
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("value cannot be represented as strict JSON") from exc


def _value_json(value):
    copied = _strict_json_copy(value)
    encoded = _canonical_json(copied)
    if len(encoded) > _MAX_VALUE_JSON_LENGTH:
        raise ValueError("value JSON must contain at most 4000 characters")
    return copied, encoded


def _normalize_change(change):
    if type(change) is not dict:
        raise ValueError("change must be a strict dictionary")
    kind = change.get("kind")
    if type(kind) is not str:
        raise ValueError("change.kind must be a string")

    if kind == "preference":
        required = frozenset(("kind", "key", "value", "expected_revision"))
        if not required.issubset(change) or change.keys() - _PREFERENCE_FIELDS:
            raise ValueError("preference change has missing or extra fields")
        key = _key(change["key"])
        expected = change["expected_revision"]
        if type(expected) is not int or not 0 <= expected <= _MAX_SQLITE_INTEGER:
            raise ValueError("expected_revision must be an integer from 0 to 2**63-1")
        note = _exact_string(change.get("note", ""), "note", 4000)
        value, value_json = _value_json(change["value"])
        normalized = {"kind": kind, "key": key, "value": value,
                      "expected_revision": expected, "note": note}
        return normalized, _canonical_json(normalized), value_json

    if kind == "note":
        if "text" not in change or change.keys() - _NOTE_FIELDS:
            raise ValueError("note change has missing or extra fields")
        text = _exact_string(change["text"], "text", 4000, nonempty=True)
        if not text.strip():
            raise ValueError("text must not be blank")
        tag = _exact_string(change.get("tag", ""), "tag", 120)
        normalized = {"kind": kind, "text": text, "tag": tag}
        return normalized, _canonical_json(normalized), None

    raise ValueError("change.kind must be 'preference' or 'note'")


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _load_json_text(value, label):
    if type(value) is not str:
        raise RuntimeError(f"corrupt {label}: expected JSON text")
    try:
        parsed = json.loads(value, parse_constant=_reject_constant,
                            object_pairs_hook=_unique_object)
        copied = _strict_json_copy(parsed, max_depth=_MAX_RECORD_JSON_DEPTH)
        if _canonical_json(copied) != value:
            raise ValueError("JSON is not in canonical form")
        return copied
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError) as exc:
        raise RuntimeError(f"corrupt {label}: invalid strict JSON") from exc


def _stored_value(value, label):
    parsed = _load_json_text(value, label)
    if len(value) > _MAX_VALUE_JSON_LENGTH:
        raise RuntimeError(f"corrupt {label}: JSON exceeds 4000 characters")
    return parsed


def _revision(value):
    if type(value) is not int or not 1 <= value <= _MAX_SQLITE_INTEGER:
        raise ValueError("revision must be a positive SQLite int64")
    return value


def _timestamp(value, name):
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite float")
    return value


def _local_iso(value, name):
    if type(value) is not str:
        raise ValueError(f"{name} must be an ISO local timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO local timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a local UTC offset")
    return value


def _preference_record(record):
    if type(record) is not dict:
        raise ValueError("preference record must be a dictionary")
    fields = record.keys()
    if not _PREFERENCE_RECORD_FIELDS.issubset(fields) or fields - (_PREFERENCE_RECORD_FIELDS | {"previous_value"}):
        raise ValueError("preference record has missing or extra fields")
    value, encoded = _value_json(record["value"])
    result = {
        "value": value,
        "revision": _revision(record["revision"]),
        "updated": _timestamp(record["updated"], "updated"),
        "updated_local": _local_iso(record["updated_local"], "updated_local"),
        "note": _exact_string(record["note"], "note", 4000),
    }
    if "previous_value" in record:
        previous, _ = _value_json(record["previous_value"])
        result["previous_value"] = previous
    if (result["revision"] == 1) == ("previous_value" in result):
        raise ValueError("previous_value must exist exactly after the first revision")
    return result


def _note_record(note):
    if type(note) is not dict or note.keys() != _NOTE_RECORD_FIELDS:
        raise ValueError("note record has missing or extra fields")
    text = _exact_string(note["text"], "text", 4000, nonempty=True)
    if not text.strip():
        raise ValueError("text must not be blank")
    return {
        "sequence": _revision(note["sequence"]),
        "text": text,
        "tag": _exact_string(note["tag"], "tag", 120),
        "created": _timestamp(note["created"], "created"),
        "created_local": _local_iso(note["created_local"], "created_local"),
    }


def _receipt(receipt, change=None):
    if type(receipt) is not dict or receipt.get("scope") != "runtime":
        raise ValueError("receipt has an invalid scope or shape")
    kind = receipt.get("kind")
    if kind == "preference":
        if receipt.keys() != {"scope", "kind", "key", "record"}:
            raise ValueError("preference receipt has missing or extra fields")
        key = _key(receipt["key"])
        record = _preference_record(receipt["record"])
        if change is not None:
            if change["kind"] != kind or change["key"] != key:
                raise ValueError("receipt does not match its operation")
            if (_canonical_json(record["value"]) != _canonical_json(change["value"])
                    or record["note"] != change["note"]
                    or record["revision"] != change["expected_revision"] + 1
                    or (("previous_value" in record) != (change["expected_revision"] > 0))):
                raise ValueError("receipt does not match its operation")
        return {"scope": "runtime", "kind": kind, "key": key, "record": record}

    if kind == "note":
        if receipt.keys() != {"scope", "kind", "note"}:
            raise ValueError("note receipt has missing or extra fields")
        note = _note_record(receipt["note"])
        if change is not None and (change["kind"] != kind or note["text"] != change["text"]
                                   or note["tag"] != change["tag"]):
            raise ValueError("receipt does not match its operation")
        return {"scope": "runtime", "kind": kind, "note": note}

    raise ValueError("receipt has an unknown kind")


class MemoryStore:
    """Store runtime-local preferences, notes, and durable write receipts."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False,
                                   isolation_level=None)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_preferences (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated REAL NOT NULL,
                    updated_local TEXT NOT NULL,
                    note TEXT NOT NULL,
                    previous_value_json TEXT
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_notes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    created REAL NOT NULL,
                    created_local TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_operations (
                    operation_id TEXT PRIMARY KEY,
                    change_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
            """)
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            self._closed = True
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("MemoryStore is closed")

    @staticmethod
    def _operation_id(value):
        value = _exact_string(value, "operation_id", 128, nonempty=True)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("operation_id must be valid UTF-8 text") from exc
        return value

    def _task_operation_exists(self, operation_id):
        columns = self._db.execute("PRAGMA table_info(tasks)").fetchall()
        if not any(column[1] == "operation_id" for column in columns):
            return False
        return self._db.execute(
            "SELECT 1 FROM tasks WHERE operation_id=? LIMIT 1", (operation_id,)
        ).fetchone() is not None

    def _decode_preference_row(self, row):
        try:
            key = _key(row[0])
            value = _stored_value(row[1], "preference value")
            revision = _revision(row[2])
            updated = _timestamp(row[3], "updated")
            updated_local = _local_iso(row[4], "updated_local")
            note = _exact_string(row[5], "note", 4000)
            record = {"value": value, "revision": revision, "updated": updated,
                      "updated_local": updated_local, "note": note}
            if row[6] is not None:
                record["previous_value"] = _stored_value(row[6], "previous preference value")
            return key, _preference_record(record)
        except (TypeError, ValueError, IndexError, OverflowError) as exc:
            raise RuntimeError("corrupt preference record") from exc

    def _decode_note_row(self, row):
        try:
            note = {"sequence": row[0], "text": row[1], "tag": row[2],
                    "created": row[3], "created_local": row[4]}
            return _note_record(note)
        except (TypeError, ValueError, IndexError, OverflowError) as exc:
            raise RuntimeError("corrupt note record") from exc

    def read(self, key=None, limit=30, after_key=None, before_note_sequence=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if key is not None:
            key = _key(key)
            if after_key is not None or before_note_sequence is not None:
                raise ValueError("key reads do not accept snapshot cursors")
        elif after_key is not None:
            after_key = _key(after_key)
        if before_note_sequence is not None and (
                type(before_note_sequence) is not int
                or not 1 <= before_note_sequence <= _MAX_SQLITE_INTEGER):
            raise ValueError("before_note_sequence must be an integer from 1 to 2**63-1")

        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN")
            try:
                if key is not None:
                    row = self._db.execute(
                        "SELECT key,value_json,revision,updated,updated_local,note,previous_value_json "
                        "FROM memory_preferences WHERE key=?", (key,)
                    ).fetchone()
                    if row is None:
                        result = {"scope": "runtime", "key": key, "found": False, "record": None}
                    else:
                        stored_key, record = self._decode_preference_row(row)
                        if stored_key != key:
                            raise RuntimeError("corrupt preference key")
                        result = {"scope": "runtime", "key": key, "found": True,
                                  "record": record}
                else:
                    preferences_sql = (
                        "SELECT key,value_json,revision,updated,updated_local,note,previous_value_json "
                        "FROM memory_preferences"
                    )
                    preferences_args = ()
                    if after_key is not None:
                        preferences_sql += " WHERE key>?"
                        preferences_args = (after_key,)
                    preferences_sql += " ORDER BY key ASC LIMIT ?"
                    preference_rows = self._db.execute(
                        preferences_sql, (*preferences_args, limit + 1)
                    ).fetchall()
                    decoded_preferences = [self._decode_preference_row(row)
                                           for row in preference_rows]
                    has_more_preferences = len(decoded_preferences) > limit
                    preferences = [dict(key=item_key, **record)
                                   for item_key, record in decoded_preferences[:limit]]

                    notes_sql = "SELECT sequence,text,tag,created,created_local FROM memory_notes"
                    notes_args = ()
                    if before_note_sequence is not None:
                        notes_sql += " WHERE sequence<?"
                        notes_args = (before_note_sequence,)
                    notes_sql += " ORDER BY sequence DESC LIMIT ?"
                    note_rows = self._db.execute(
                        notes_sql, (*notes_args, limit + 1)
                    ).fetchall()
                    decoded_notes = [self._decode_note_row(row) for row in note_rows]
                    has_more_notes = len(decoded_notes) > limit
                    notes = decoded_notes[:limit]
                    result = {
                        "scope": "runtime",
                        "preferences": preferences,
                        "notes": notes,
                        "limit": limit,
                        "has_more_preferences": has_more_preferences,
                        "next_after_key": preferences[-1]["key"] if has_more_preferences else None,
                        "has_more_notes": has_more_notes,
                        "next_before_note_sequence": notes[-1]["sequence"] if has_more_notes else None,
                    }
                self._db.execute("COMMIT")
                return result
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def write(self, operation_id, change):
        operation_id = self._operation_id(operation_id)
        normalized, change_json, value_json = _normalize_change(change)

        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT change_json,result_json FROM memory_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    stored_change = _load_json_text(existing[0], "memory operation")
                    stored_normalized, canonical_change, _ = _normalize_change(stored_change)
                    if canonical_change != existing[0]:
                        raise RuntimeError("corrupt memory operation request")
                    receipt_value = _load_json_text(existing[1], "memory operation receipt")
                    receipt = _receipt(receipt_value, stored_normalized)
                    if _canonical_json(receipt) != existing[1]:
                        raise RuntimeError("corrupt memory operation receipt")
                    if canonical_change != change_json:
                        raise MemoryConflictError("operation_id was already used for a different request")
                    self._db.execute("COMMIT")
                    return receipt

                if self._task_operation_exists(operation_id):
                    raise MemoryConflictError("operation_id is already used by a task")

                if normalized["kind"] == "preference":
                    result, result_json = self._write_preference(normalized, value_json)
                else:
                    result, result_json = self._write_note(normalized)

                self._db.execute(
                    "INSERT INTO memory_operations(operation_id,change_json,result_json) VALUES (?,?,?)",
                    (operation_id, change_json, result_json),
                )
                self._db.execute("COMMIT")
                return result
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _write_preference(self, change, value_json):
        row = self._db.execute(
            "SELECT key,value_json,revision,updated,updated_local,note,previous_value_json "
            "FROM memory_preferences WHERE key=?", (change["key"],)
        ).fetchone()
        current = self._decode_preference_row(row) if row is not None else None
        current_revision = current[1]["revision"] if current is not None else 0
        if current_revision != change["expected_revision"]:
            raise MemoryConflictError("expected_revision does not match the current preference")

        revision = current_revision + 1
        updated = float(time.time())
        updated_local = datetime.datetime.fromtimestamp(updated).astimezone().isoformat()
        record = {"value": change["value"], "revision": revision, "updated": updated,
                  "updated_local": updated_local, "note": change["note"]}
        if current is not None:
            record["previous_value"] = current[1]["value"]
        receipt = _receipt({"scope": "runtime", "kind": "preference",
                            "key": change["key"], "record": record}, change)
        result_json = _canonical_json(receipt)

        if current is None:
            self._db.execute(
                "INSERT INTO memory_preferences(key,value_json,revision,updated,updated_local,note,previous_value_json) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (change["key"], value_json, revision, updated, updated_local, change["note"]),
            )
        else:
            previous_json = row[1]
            cursor = self._db.execute(
                "UPDATE memory_preferences SET value_json=?,revision=?,updated=?,updated_local=?,note=?, "
                "previous_value_json=? WHERE key=? AND revision=?",
                (value_json, revision, updated, updated_local, change["note"], previous_json,
                 change["key"], change["expected_revision"]),
            )
            if cursor.rowcount != 1:
                raise MemoryConflictError("preference changed during compare-and-swap")
        return receipt, result_json

    def _write_note(self, change):
        sequence_row = self._db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='memory_notes'"
        ).fetchone()
        maximum_row = self._db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM memory_notes"
        ).fetchone()
        sequence = max(sequence_row[0] if sequence_row else 0, maximum_row[0]) + 1
        created = float(time.time())
        created_local = datetime.datetime.fromtimestamp(created).astimezone().isoformat()
        note = {"sequence": sequence, "text": change["text"], "tag": change["tag"],
                "created": created, "created_local": created_local}
        receipt = _receipt({"scope": "runtime", "kind": "note", "note": note}, change)
        result_json = _canonical_json(receipt)
        self._db.execute(
            "INSERT INTO memory_notes(sequence,text,tag,created,created_local) VALUES (?,?,?,?,?)",
            (sequence, change["text"], change["tag"], created, created_local),
        )
        return receipt, result_json


__all__ = ["MemoryConflictError", "MemoryStore"]
