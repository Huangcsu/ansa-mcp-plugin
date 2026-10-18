"""Persistence, strict input, paging, and concurrency checks for MemoryStore."""

import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from ansa_mcp_plugin.backend.memory import MemoryConflictError, MemoryStore


@contextmanager
def _database(path):
    db = sqlite3.connect(path)
    try:
        yield db
        db.commit()
    finally:
        db.close()


class MemoryStoreChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = MemoryStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = MemoryStore(self.path)

    @staticmethod
    def preference(key, value, expected_revision, note=""):
        return {"kind": "preference", "key": key, "value": value,
                "expected_revision": expected_revision, "note": note}

    @staticmethod
    def note(text, tag=""):
        return {"kind": "note", "text": text, "tag": tag}

    def test_preference_persistence_cas_previous_null_and_legacy_tables(self):
        with _database(self.path) as db:
            db.execute("CREATE TABLE binding (singleton INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO binding VALUES (1, 'kept')")
            db.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL)")
            db.execute("INSERT INTO tasks VALUES ('legacy-task', 'legacy-op')")

        self.reopen()
        first = self.store.write("pref-1", self.preference("display.mode", None, 0, "initial"))
        self.assertEqual(first["record"]["revision"], 1)
        self.assertNotIn("previous_value", first["record"])

        second = self.store.write("pref-2", self.preference("display.mode", "compact", 1))
        self.assertEqual(second["record"]["revision"], 2)
        self.assertIsNone(second["record"]["previous_value"])
        third = self.store.write("pref-3", self.preference("display.mode", {"scale": 2}, 2))
        self.assertEqual(third["record"]["previous_value"], "compact")
        self.assertEqual(third["record"]["revision"], 3)

        with self.assertRaises(MemoryConflictError):
            self.store.write("stale", self.preference("display.mode", "wrong", 1))
        self.assertEqual(self.store.read("display.mode")["record"]["revision"], 3)
        with _database(self.path) as db:
            self.assertEqual(db.execute("SELECT value FROM binding WHERE singleton=1").fetchone()[0],
                             "kept")
            self.assertEqual(db.execute("SELECT task_id,operation_id FROM tasks").fetchone(),
                             ("legacy-task", "legacy-op"))

    def test_note_receipts_survive_restart_and_task_operation_keys_conflict(self):
        with _database(self.path) as db:
            db.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL)")
            db.execute("INSERT INTO tasks VALUES ('task-1', 'task-op')")

        with self.assertRaises(MemoryConflictError):
            self.store.write("task-op", self.note("must not append"))
        with _database(self.path) as db:
            self.assertEqual(db.execute("SELECT task_id,operation_id FROM tasks").fetchall(),
                             [("task-1", "task-op")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0], 0)

        request = self.note("remember this", "work")
        first = self.store.write(" opaque-op ", request)
        with _database(self.path) as db:
            db.execute("INSERT INTO tasks VALUES ('legacy-collision', ' opaque-op ')")
        self.assertEqual(self.store.write(" opaque-op ", request), first)

        self.reopen()
        self.assertEqual(self.store.write(" opaque-op ", request), first)
        with self.assertRaises(MemoryConflictError):
            self.store.write(" opaque-op ", self.note("different"))
        second = self.store.write("note-2", self.note("next"))
        self.assertEqual((first["note"]["sequence"], second["note"]["sequence"]), (1, 2))

    def test_snapshot_cursors_insertions_empty_tail_and_missing_key(self):
        self.store.write("p-a", self.preference("a", 1, 0))
        self.store.write("p-c", self.preference("c", 3, 0))
        self.store.write("n-1", self.note("old"))
        self.store.write("n-2", self.note("new"))

        first = self.store.read(limit=1)
        self.assertEqual([item["key"] for item in first["preferences"]], ["a"])
        self.assertTrue(first["has_more_preferences"])
        self.assertEqual(first["next_after_key"], "a")
        self.assertEqual([item["text"] for item in first["notes"]], ["new"])
        self.assertTrue(first["has_more_notes"])
        self.assertEqual(first["next_before_note_sequence"], 2)

        self.store.write("p-b", self.preference("b", 2, 0))
        self.store.write("n-3", self.note("inserted after page one"))
        second = self.store.read(limit=1, after_key=first["next_after_key"],
                                 before_note_sequence=first["next_before_note_sequence"])
        self.assertEqual([item["key"] for item in second["preferences"]], ["b"])
        self.assertEqual([item["text"] for item in second["notes"]], ["old"])
        self.assertTrue(second["has_more_preferences"])
        self.assertFalse(second["has_more_notes"])
        self.assertIsNone(second["next_before_note_sequence"])

        tail = self.store.read(limit=1, after_key="c", before_note_sequence=1)
        self.assertEqual(tail["preferences"], [])
        self.assertEqual(tail["notes"], [])
        self.assertFalse(tail["has_more_preferences"])
        self.assertFalse(tail["has_more_notes"])
        self.assertIsNone(tail["next_after_key"])
        missing = self.store.read("missing")
        self.assertEqual(missing, {"scope": "runtime", "key": "missing",
                                   "found": False, "record": None})

    def test_strict_inputs_json_boundaries_and_no_invalid_write_side_effects(self):
        class DictSubclass(dict):
            pass

        boundary = "x" * 3998  # JSON quotes bring this value to exactly 4000 characters.
        accepted = self.store.write(" boundary-op ", self.preference("boundary", boundary, 0,
                                                                     "n" * 4000))
        self.assertEqual(len(accepted["record"]["value"]) + 2, 4000)
        note_boundary = self.store.write("note-boundary", self.note("n" * 4000, "t" * 120))
        self.assertEqual(len(note_boundary["note"]["text"]), 4000)
        self.assertEqual(len(note_boundary["note"]["tag"]), 120)
        max_depth_value = None
        for _ in range(32):
            max_depth_value = [max_depth_value]
        deep_change = self.preference("max-depth", max_depth_value, 0)
        deep_receipt = self.store.write("max-depth-op", deep_change)
        self.assertEqual(self.store.read("max-depth")["record"]["value"], max_depth_value)
        self.assertEqual(self.store.write("max-depth-op", deep_change), deep_receipt)
        before = self.store.read(limit=10)

        cycle = []
        cycle.append(cycle)
        too_deep = None
        for _ in range(33):
            too_deep = [too_deep]
        invalid_changes = [
            DictSubclass(self.note("subclass")),
            self.preference("tuple", (1, 2), 0),
            self.preference("int-key", {1: "bad"}, 0),
            self.preference("boolean-revision", "x", True),
            self.preference("large-revision", "x", 2**63),
            self.preference("nonfinite", float("nan"), 0),
            self.preference("cycle", cycle, 0),
            self.preference("too-deep", too_deep, 0),
            self.preference("too-large", "x" * 3999, 0),
            self.preference(" bad-key", "x", 0),
            self.preference("bad\nkey", "x", 0),
            {"kind": "preference", "key": "extra", "value": 1,
             "expected_revision": 0, "extra": True},
            {"kind": "preference", "key": "missing-revision", "value": 1},
            {"kind": "preference", "key": "bad-note", "value": 1,
             "expected_revision": 0, "note": "n" * 4001},
            self.note(" \t\n "),
            self.note("x" * 4001),
            {"kind": "note", "text": "ok", "tag": "t" * 121},
        ]
        for index, change in enumerate(invalid_changes):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.store.write(f"invalid-{index}", change)
        with self.assertRaises(ValueError):
            self.store.write("x" * 129, self.note("bad operation id"))
        with self.assertRaises(MemoryConflictError):
            self.store.write("max-revision", self.preference("max-revision", "x", 2**63 - 1))
        with self.assertRaises(ValueError):
            self.store.read(limit=True)
        for cursor in (0, True, 2**63):
            with self.subTest(before_note_sequence=cursor), self.assertRaises(ValueError):
                self.store.read(before_note_sequence=cursor)
        self.assertEqual(len(self.store.read(before_note_sequence=2**63 - 1)["notes"]), 1)
        with self.assertRaises(ValueError):
            self.store.read("boundary", after_key="a")

        self.assertEqual(self.store.read(limit=10), before)
        with _database(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_operations").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0], 1)

            db.execute("UPDATE memory_preferences SET value_json='NaN' WHERE key='boundary'")
        with self.assertRaises(RuntimeError):
            self.store.read("boundary")

    def test_concurrent_cas_has_one_winner_and_duplicate_operation_has_one_receipt(self):
        other = MemoryStore(self.path)
        try:
            barrier = threading.Barrier(2)
            successes = []
            errors = []

            def compare_and_swap(store, operation_id, value):
                try:
                    barrier.wait(timeout=5)
                    successes.append(store.write(operation_id,
                                                 self.preference("race", value, 0)))
                except Exception as exc:  # reported by the assertions on the main test thread
                    errors.append(exc)

            threads = [
                threading.Thread(target=compare_and_swap, args=(self.store, "race-a", "a")),
                threading.Thread(target=compare_and_swap, args=(other, "race-b", "b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], MemoryConflictError)

            barrier = threading.Barrier(2)
            results = []
            errors.clear()

            def duplicate(store):
                try:
                    barrier.wait(timeout=5)
                    results.append(store.write("concurrent-note", self.note("once")))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=duplicate, args=(store,))
                       for store in (self.store, other)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            snapshot = self.store.read(limit=10)
            self.assertEqual([item["text"] for item in snapshot["notes"]], ["once"])
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
