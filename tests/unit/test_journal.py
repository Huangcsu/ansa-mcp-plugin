"""Durability and race checks for the per-session executor journal."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from ansa_mcp_bridge.journal import (
    Journal,
    JournalPersistenceError,
    TaskCollisionError,
)


IDENTITY = {
    "pid": 2148,
    "process_start": "134012345678900000",
    "instance_id": "instance-a",
    "session_id": "session-a",
}
_DEFAULT_EXPIRY = object()


def make_task(task_id="task-1", *, expires_at=_DEFAULT_EXPIRY, params=None):
    if expires_at is _DEFAULT_EXPIRY:
        expires_at = time.time() + 60
    return {
        "task_id": task_id,
        "operation_id": f"operation-{task_id}",
        "tool": "ansa_create_node",
        "params": {"coordinates": [1.0, 2.0, 3.0]} if params is None else params,
        "target": {
            "backend_id": "backend-a",
            "instance_id": IDENTITY["instance_id"],
            "session_id": IDENTITY["session_id"],
            "generation": 4,
        },
        "model": {"epoch": 7},
        "identity": dict(IDENTITY),
        "expires_at": expires_at,
    }


class JournalChecks(unittest.TestCase):
    def setUp(self):
        project_root = Path(__file__).resolve().parents[2]
        runtime_root = (project_root / "runtime").resolve()
        self.temp_root = (runtime_root / "unit-test-temp").resolve()
        if self.temp_root.parent != runtime_root:
            raise RuntimeError("Journal test temp root resolved outside project runtime")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        if self.temp_root.resolve() != self.temp_root:
            raise RuntimeError("Journal test temp root must not be a reparse point")
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.journal = Journal(self.temp.name, IDENTITY)

    def tearDown(self):
        self.journal.close()
        temp_path = Path(self.temp.name).resolve()
        if temp_path.parent != self.temp_root:
            raise RuntimeError("Refusing to clean journal test temp outside project runtime/unit-test-temp")
        self.temp.cleanup()

    def test_database_uses_wal_and_full_synchronous_mode(self):
        self.assertEqual("wal", self.journal._db.execute("PRAGMA journal_mode").fetchone()[0].lower())
        self.assertEqual(2, self.journal._db.execute("PRAGMA synchronous").fetchone()[0])

    def test_duplicate_submit_is_idempotent_and_content_collision_rejected(self):
        task = make_task()
        first = self.journal.submit(task)
        duplicate = self.journal.submit(dict(reversed(list(task.items()))))
        self.assertEqual(first, duplicate)
        self.assertEqual("queued", self.journal.get("task-1")["state"])

        changed = make_task(params={"coordinates": [9.0, 2.0, 3.0]})
        with self.assertRaises(TaskCollisionError):
            self.journal.submit(changed)

    def test_cancel_before_late_submit_leaves_durable_tombstone(self):
        task = make_task()
        cancelled = self.journal.cancel(task)
        self.assertEqual("cancelled", cancelled["state"])
        self.assertEqual(cancelled, self.journal.submit(task))
        self.assertIsNone(self.journal.next_queued())
        self.assertEqual([], self.journal.busy())

    def test_journal_reopen_preserves_queue_and_does_not_reclaim_running_task(self):
        queued = make_task("queued-on-disk")
        running = make_task("running-on-disk")
        self.journal.submit(queued)
        self.journal.submit(running)
        self.journal.claim("running-on-disk", lambda _task: None)
        self.journal.close()

        self.journal = Journal(self.temp.name, IDENTITY)
        self.assertEqual("queued", self.journal.get("queued-on-disk")["state"])
        self.assertEqual("running", self.journal.get("running-on-disk")["state"])
        self.assertEqual("queued-on-disk", self.journal.next_queued()["task_id"])
        self.assertEqual("running", self.journal.claim("running-on-disk", lambda _task: self.fail("running task was reclaimed"))["state"])

    def test_new_executor_session_expires_old_queued_task(self):
        old_task = make_task("old-session-task")
        self.journal.submit(old_task)
        self.journal.close()
        new_identity = {**IDENTITY, "instance_id": "instance-b", "session_id": "session-b"}
        self.journal = Journal(self.temp.name, new_identity)

        result = self.journal.claim("old-session-task", lambda _task: self.fail("old task reached validation"))
        self.assertEqual("expired", result["state"])
        self.assertIn("different ANSA executor identity", result["error"])
        self.assertEqual([], self.journal.busy())

    def test_claim_and_cancel_are_serialized(self):
        task = make_task()
        self.journal.submit(task)
        validation_started = threading.Event()
        release_validation = threading.Event()
        claim_results = []
        cancel_results = []
        thread_errors = []

        def validate(_task):
            validation_started.set()
            if not release_validation.wait(3):
                raise TimeoutError("test did not release claim validation")

        def claim_task():
            try:
                claim_results.append(self.journal.claim("task-1", validate))
            except Exception as exc:  # surfaced in the test thread below
                thread_errors.append(exc)

        def cancel_task():
            try:
                cancel_results.append(self.journal.cancel(task))
            except Exception as exc:  # surfaced in the test thread below
                thread_errors.append(exc)

        claimant = threading.Thread(target=claim_task)
        claimant.start()
        self.assertTrue(validation_started.wait(2))
        canceller = threading.Thread(target=cancel_task)
        canceller.start()
        release_validation.set()
        claimant.join(3)
        canceller.join(3)

        self.assertFalse(claimant.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual([], thread_errors)
        self.assertEqual("running", claim_results[0]["state"])
        self.assertEqual("running", cancel_results[0]["state"])
        self.assertEqual("running", self.journal.get("task-1")["state"])

    def test_expiry_is_authoritative_at_claim_and_cancel(self):
        self.journal.submit(make_task("claim-expired", expires_at=time.time() - 1))
        result = self.journal.claim("claim-expired", lambda _task: self.fail("expired task was validated"))
        self.assertEqual("expired", result["state"])
        self.assertIn("expired", result["error"])

        cancel_task = make_task("cancel-expired", expires_at=time.time() - 1)
        result = self.journal.cancel(cancel_task)
        self.assertEqual("expired", result["state"])

    def test_none_expiry_does_not_expire_on_claim_or_cancel(self):
        task = make_task("no-expiry-claim", expires_at=None)
        submitted = self.journal.submit(task)
        self.assertIsNone(submitted["task"]["expires_at"])
        claimed = self.journal.claim("no-expiry-claim", lambda _task: None)
        self.assertEqual("running", claimed["state"])

        cancelled = self.journal.cancel(make_task("no-expiry-cancel", expires_at=None))
        self.assertEqual("cancelled", cancelled["state"])


    def test_finish_ack_and_terminal_states_are_idempotent(self):
        task = make_task()
        self.journal.submit(task)
        self.journal.claim("task-1", lambda _task: None)
        succeeded = self.journal.finish("task-1", result={"entity_id": 21})
        self.assertEqual("succeeded", succeeded["state"])
        self.assertEqual({"entity_id": 21}, succeeded["result"])

        self.assertEqual(succeeded, self.journal.finish("task-1", error="late failure"))
        self.assertEqual(succeeded, self.journal.cancel(task))
        acknowledged = self.journal.ack("task-1")
        self.assertTrue(acknowledged["acked"])
        self.assertEqual(acknowledged, self.journal.ack("task-1"))
        self.assertEqual([], self.journal.busy())

    def test_non_json_values_are_rejected_without_changing_task_state(self):
        with self.assertRaises(ValueError):
            self.journal.submit(make_task("nan-input", params={"value": float("nan")}))
        self.assertIsNone(self.journal.get("nan-input"))

        self.journal.submit(make_task())
        self.journal.claim("task-1", lambda _task: None)
        with self.assertRaises(ValueError):
            self.journal.finish("task-1", result={"value": float("nan")})
        self.assertEqual("running", self.journal.get("task-1")["state"])

    def test_persistence_failure_blocks_new_admission_and_claim(self):
        queued = make_task("queued-before-failure")
        self.journal.submit(queued)
        self.journal._db.execute(
            "CREATE TRIGGER fail_task_insert BEFORE INSERT ON tasks "
            "BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END"
        )

        with self.assertRaises(JournalPersistenceError):
            self.journal.submit(make_task("insert-fails"))
        self.assertTrue(self.journal.blocked)
        self.assertEqual("queued", self.journal.get("queued-before-failure")["state"])

        self.journal._db.execute("DROP TRIGGER fail_task_insert")
        with self.assertRaises(JournalPersistenceError):
            self.journal.submit(make_task("new-admission-blocked"))
        with self.assertRaises(JournalPersistenceError):
            self.journal.claim("queued-before-failure", lambda _task: None)
        self.assertEqual("queued", self.journal.get("queued-before-failure")["state"])

    def test_cancel_and_ack_can_persist_while_journal_is_blocked(self):
        self.journal._db.execute(
            "CREATE TRIGGER fail_task_insert BEFORE INSERT ON tasks "
            "BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END"
        )
        with self.assertRaises(JournalPersistenceError):
            self.journal.submit(make_task("insert-fails"))
        self.assertTrue(self.journal.blocked)
        self.journal._db.execute("DROP TRIGGER fail_task_insert")

        tombstone = self.journal.cancel(make_task("blocked-cancel", expires_at=None))
        self.assertEqual("cancelled", tombstone["state"])
        self.assertTrue(self.journal.ack("blocked-cancel")["acked"])

    def test_failed_finish_keeps_running_state(self):
        self.journal.submit(make_task())
        self.journal.claim("task-1", lambda _task: None)
        self.journal._db.execute(
            "CREATE TRIGGER fail_task_finish BEFORE UPDATE OF state ON tasks "
            "WHEN NEW.state IN ('succeeded', 'failed') "
            "BEGIN SELECT RAISE(ABORT, 'simulated finish persistence failure'); END"
        )

        with self.assertRaises(JournalPersistenceError):
            self.journal.finish("task-1", result={"entity_id": 21})
        self.assertTrue(self.journal.blocked)
        self.assertEqual("running", self.journal.get("task-1")["state"])

    def test_failed_task_preserves_authoritative_model_after_across_restart(self):
        self.journal.submit(make_task())
        self.journal.claim("task-1", lambda _task: None)
        evidence = {"model_after": {"epoch": 2}, "possible_effects": True}
        self.journal.finish("task-1", result=evidence, error="Open failed after model invalidation")
        self.journal.close()
        self.journal = Journal(self.temp.name, IDENTITY)
        failed = self.journal.get("task-1")
        self.assertEqual("failed", failed["state"])
        self.assertEqual(evidence, failed["result"])
        self.assertEqual(failed, self.journal.finish("task-1", result={"wrong": True}))


if __name__ == "__main__":
    unittest.main()
