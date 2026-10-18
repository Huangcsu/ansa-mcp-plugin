"""Durability and state-transition checks for the backend task queue."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from ansa_mcp_plugin.backend.tasks import TaskStore


class TaskStoreChecks(unittest.TestCase):
    def setUp(self):
        runtime_test_dir = Path(__file__).resolve().parents[2] / "runtime"
        self.temp = tempfile.TemporaryDirectory(dir=runtime_test_dir)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def request(*, params=None, ttl_seconds=None):
        return {
            "tool": "ansa_node_create",
            "params": {"name": "node"} if params is None else params,
            "target": {"backend_id": "backend-a", "instance_id": "ansa-a",
                       "session_id": "session-a", "generation": 3},
            "model": {"epoch": 7, "key": "model-a"},
            "ttl_seconds": ttl_seconds,
        }

    @staticmethod
    def identity(instance_id="ansa-a", session_id="session-a"):
        return {"pid": 1234, "process_start": "98765",
                "instance_id": instance_id, "session_id": session_id,
                "backend_id": "backend-a", "generation": 3}

    def create(self, operation_id="op-1", request=None, identity=None, ttl=None):
        request = request or self.request(ttl_seconds=ttl)
        if ttl is None:
            ttl = request["ttl_seconds"]
        return self.store.create(operation_id, request,
                                 identity or self.identity(), ttl)

    def test_idempotency_survives_restart_and_rejects_changed_request(self):
        request = self.request(params={"name": "node", "coordinates": [1, 2, 3]})
        first = self.store.create("op-1", request, self.identity(), None)
        self.store.close()
        self.store = TaskStore(self.path)

        reordered = dict(reversed(list(request.items())))
        retried = self.store.lookup("op-1", reordered)
        self.assertEqual(retried["task_id"], first["task_id"])
        self.assertEqual(self.store.create("op-1", reordered, self.identity(), None), first)
        changed = self.request(params={"name": "node", "coordinates": [9, 2, 3]})
        with self.assertRaises(ValueError):
            self.store.lookup("op-1", changed)
        with self.assertRaises(ValueError):
            self.store.create("op-1", changed, self.identity(), None)

    def test_concurrent_duplicate_creation_returns_one_task(self):
        other = TaskStore(self.path)
        barrier = threading.Barrier(2)
        results = []
        errors = []
        request = self.request()

        def create(store):
            try:
                barrier.wait()
                results.append(store.create("op-race", request, self.identity(), None))
            except Exception as exc:  # surfaced in the test thread below
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(item,))
                   for item in (self.store, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        other.close()
        self.assertEqual(errors, [])
        self.assertEqual(len({record["task_id"] for record in results}), 1)

    def test_memory_and_model_tasks_share_operation_keys_atomically(self):
        from ansa_mcp_plugin.backend.memory import MemoryConflictError, MemoryStore

        memory = MemoryStore(self.path)
        change = {"kind": "preference", "key": "example", "value": None, "expected_revision": 0}
        try:
            original = memory.write("memory-first", change)
            with self.assertRaisesRegex(ValueError, "memory write"):
                self.store.lookup("memory-first", self.request())
            with self.assertRaisesRegex(ValueError, "memory write"):
                self.create("memory-first")
            self.assertIsNone(self.store.get(operation_id="memory-first"))
            task = self.create("task-first")
            with self.assertRaises(MemoryConflictError):
                memory.write("task-first", {"kind": "note", "text": "synthetic note"})
            self.assertEqual(self.store.get(operation_id="task-first"), task)
            self.assertEqual(memory.write("memory-first", change), original)

            barrier = threading.Barrier(2)
            successes, failures = [], []
            def race(write):
                try:
                    barrier.wait(timeout=5)
                    successes.append(write())
                except Exception as exc:
                    failures.append(exc)
            threads = [threading.Thread(target=race, args=(write,)) for write in (
                lambda: self.create("cross-race"),
                lambda: memory.write("cross-race", {"kind": "note", "text": "race note"}),
            )]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], ValueError)
            self.assertEqual(bool(self.store.get(operation_id="cross-race")) +
                             bool(memory.read()["notes"]), 1)
        finally:
            memory.close()

    def test_task_freezes_original_parameters_identity_and_context(self):
        request = self.request(params={"coordinates": [1, 2, 3]})
        identity = self.identity()
        record = self.store.create("op-1", request, identity, None)
        request["params"]["coordinates"][0] = 99
        request["target"]["instance_id"] = "ansa-b"
        identity["session_id"] = "session-b"

        stored = self.store.get(task_id=record["task_id"])
        self.assertEqual(stored["task"]["params"]["coordinates"], [1, 2, 3])
        self.assertEqual(stored["task"]["target"]["instance_id"], "ansa-a")
        self.assertEqual(stored["task"]["identity"]["session_id"], "session-a")
        self.assertEqual(self.store.busy("ansa-a", "session-a"), [stored])
        self.assertEqual(self.store.busy("ansa-b", "session-b"), [])

    def test_sent_cancel_is_pending_until_executor_proves_unclaimed(self):
        record = self.create()
        sent = self.store.dispatch(record["task_id"])
        self.assertEqual(sent["dispatch"], "sent")
        requested = self.store.cancel(record["task_id"])
        self.assertEqual(requested["state"], "queued")
        self.assertTrue(requested["cancel_pending"])
        self.assertEqual(self.store.next_queued(), None)

        settled = self.store.settle_unclaimed(record["task_id"], "cancelled")
        self.assertEqual(settled["state"], "cancelled")
        self.assertFalse(settled["cancel_pending"])

    def test_unknown_converges_and_terminal_result_cannot_be_overwritten(self):
        record = self.create()
        self.store.dispatch(record["task_id"])
        uncertain = self.store.unknown(record["task_id"], "connection lost")
        self.assertEqual(uncertain["state"], "unknown")
        self.assertIsNone(self.store.next_queued())

        running = self.store.merge({"task_id": record["task_id"], "state": "running"})
        self.assertEqual(running["state"], "running")
        succeeded = self.store.merge({"task_id": record["task_id"], "state": "succeeded",
                                      "result": {"created": 1}})
        self.assertEqual(succeeded["result"], {"created": 1})
        later_failure = self.store.merge({"task_id": record["task_id"], "state": "failed",
                                          "error": "late contradictory record"})
        self.assertEqual(later_failure["state"], "succeeded")
        self.assertEqual(later_failure["result"], {"created": 1})
        self.assertIsNone(later_failure["error"])
        self.assertEqual(len(self.store.pending()), 1)  # terminal result awaits ACK
        self.assertTrue(self.store.ack(record["task_id"])["acked"])
        self.assertEqual(self.store.pending(), [])

    def test_fifo_dispatch_and_only_unsent_tasks_expire(self):
        expiring = self.create("op-expire", ttl=30)
        sent = self.create("op-sent")
        self.assertEqual(self.store.next_queued()["task_id"], expiring["task_id"])
        self.assertEqual(self.store.dispatch(sent["task_id"])["dispatch"], "sent")
        self.assertEqual(self.store.next_queued()["task_id"], expiring["task_id"])

        expired = self.store.expire(time.time() + 60)
        self.assertEqual([item["task_id"] for item in expired], [expiring["task_id"]])
        self.assertEqual(self.store.get(task_id=expiring["task_id"])["state"], "expired")
        self.assertEqual(self.store.get(task_id=sent["task_id"])["state"], "queued")
        self.assertEqual(self.store.get(task_id=sent["task_id"])["dispatch"], "sent")

    def test_json_rejects_nan_before_persisting(self):
        request = self.request(params={"value": float("nan")})
        with self.assertRaises(ValueError):
            self.store.create("op-nan", request, self.identity(), None)
        self.assertIsNone(self.store.lookup("op-nan", self.request()))


if __name__ == "__main__":
    unittest.main()
