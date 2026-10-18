"""Private task-log projection and immutable attachment checks."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansa_mcp_plugin.backend.log_reader import TaskLogReader
from ansa_mcp_plugin.backend.tasks import TaskStore


class TaskLogReaderChecks(unittest.TestCase):
    def setUp(self):
        runtime_test_dir = Path(__file__).resolve().parents[2] / "runtime"
        self.temp = tempfile.TemporaryDirectory(dir=runtime_test_dir)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.database = self.home / "state.sqlite3"
        self.store = TaskStore(self.database)
        self.addCleanup(lambda: self.store.close())
        self.security = patch.multiple(
            "ansa_mcp_plugin.backend.log_reader",
            ensure_safe_parent=self._ensure_safe_parent,
            verify_private_path=self._verify_private_path,
            ensure_private_directory=self._ensure_private_directory,
        )
        self.security.start()
        self.addCleanup(self.security.stop)

    def _ensure_inside_home(self, path):
        path = Path(path).absolute()
        try:
            path.relative_to(self.home.absolute())
        except ValueError as exc:
            raise PermissionError("test runtime path escaped") from exc
        for entry in (path, *path.parents):
            if entry.is_symlink():
                raise PermissionError("test runtime path contains a link")

    def _ensure_safe_parent(self, path):
        self._ensure_inside_home(path)

    def _verify_private_path(self, path):
        self._ensure_inside_home(path)
        if not Path(path).exists():
            raise PermissionError("test private path does not exist")

    def _ensure_private_directory(self, path):
        self._ensure_inside_home(path)
        Path(path).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def request(index, *, tool="script_execute", params=None):
        return {
            "tool": tool,
            "params": {"script": "print('private request')"} if params is None else params,
            "target": {
                "backend_id": "backend-a",
                "instance_id": "ansa-a",
                "session_id": "session-a",
                "generation": 3,
                "unapproved_path": "C:/must-not-be-projected",
            },
            "model": {"epoch": 7, "private_extra": "must-not-be-projected"},
            "ttl_seconds": None,
        }

    def create(self, index, *, tool="script_execute", params=None):
        return self.store.create(
            f"operation-{index}", self.request(index, tool=tool, params=params),
            {
                "pid": 1234,
                "process_start": "98765",
                "instance_id": "ansa-a",
                "session_id": "session-a",
                "backend_id": "backend-a",
                "generation": 3,
            },
            None,
        )

    def finish(self, record, state, *, result=None, error=None):
        return self.store.merge({
            "task_id": record["task_id"],
            "state": state,
            "result": result,
            "error": error,
        })

    def reader(self, runtime_home=None):
        return TaskLogReader(self.home if runtime_home is None else runtime_home, self.store)

    def document(self, response):
        return json.loads(Path(response["artifact"]["path"]).read_text(encoding="utf-8"))

    def test_empty_history_has_no_attachment_and_max_chars_is_strict(self):
        reader = self.reader()
        response = reader.read()
        self.assertFalse(response["found"])
        self.assertFalse(response["available"])
        self.assertEqual(response["audit_scope"], "task_history")
        self.assertIsNone(response["artifact"])
        self.assertFalse((self.home / "logs").exists())

        for value in (True, False, "10", 0, 65537, 1.0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reader.read(max_chars=value)

    def test_latest_persisted_body_projection_and_task_history_audit(self):
        older = self.create(1, params={"script": "OLDER_REQUEST_SECRET"})
        self.finish(older, "succeeded", result={"result": {"value": "older"}})
        attacker_path = self.home.parent / ("attacker-selected-" + self.home.name + ".json")
        latest = self.create(2, params={
            "script": "LATEST_REQUEST_SECRET",
            "path": str(attacker_path),
        })
        persisted_result = {
            "result": {"model_path": "C:/models/current.ansa", "value": 42},
            "stdout": "persisted stdout",
            "stdout_truncated": True,
        }
        self.finish(latest, "failed", result=persisted_result, error="persisted failure")

        before = self.store._db.total_changes
        response = self.reader().read()
        document = self.document(response)
        serialized = json.dumps(document, ensure_ascii=False)
        projected = document["history"]["tasks"][0]

        self.assertTrue(response["available"])
        self.assertEqual(response["audit_scope"], "task_history")
        self.assertFalse(response["has_more"])
        self.assertEqual(response["latest"]["task_id"], latest["task_id"])
        self.assertEqual(document["result"], persisted_result)
        self.assertEqual(document["error"], "persisted failure")
        self.assertEqual(document["history"]["state_counts"], {"failed": 1, "succeeded": 1})
        self.assertEqual(document["history"]["tool_counts"], {"script_execute": 2})
        self.assertEqual(set(projected), {
            "task_id", "operation_id", "state", "tool", "target", "model",
        })
        self.assertEqual(projected["target"], {
            "backend_id": "backend-a", "instance_id": "ansa-a",
            "session_id": "session-a", "generation": 3,
        })
        self.assertEqual(projected["model"], {"epoch": 7})
        for secret in (
            "OLDER_REQUEST_SECRET", "LATEST_REQUEST_SECRET", attacker_path.name,
            "unapproved_path", "private_extra", '"params"', '"request"',
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.store._db.total_changes, before)
        self.assertFalse(attacker_path.exists())

        artifact = response["artifact"]
        path = Path(artifact["path"])
        content = path.read_bytes()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, self.home / "logs")
        self.assertRegex(path.name, r"^[0-9a-f]{64}\.json$")
        self.assertEqual(artifact["mime_type"], "application/json")
        self.assertEqual(artifact["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(artifact["bytes"], len(content))
        self.assertIn("persisted stdout", content.decode("utf-8"))
        self.assertIn("stdout_truncated", content.decode("utf-8"))

        path.write_bytes(b"preexisting different content")
        with self.assertRaisesRegex(RuntimeError, "already contains different data"):
            self.reader().read()
        self.assertEqual(path.read_bytes(), b"preexisting different content")

    def test_history_caps_at_200_reports_more_and_preserves_read_only_state(self):
        records = [self.create(index, tool="ansa_node_create") for index in range(201)]
        before = self.store._db.total_changes

        response = self.reader().read()
        document = self.document(response)
        history = document["history"]

        self.assertEqual(history["task_count"], 200)
        self.assertEqual(len(history["tasks"]), 200)
        self.assertEqual(history["tasks"][0]["task_id"], records[-1]["task_id"])
        self.assertEqual(history["tool_counts"], {"ansa_node_create": 200})
        self.assertEqual(history["state_counts"], {"queued": 200})
        self.assertTrue(response["has_more"])
        self.assertEqual(self.store._db.total_changes, before)

    def test_latest_pending_task_does_not_fall_back_to_older_result(self):
        older = self.create(1)
        self.finish(older, "succeeded", result={"result": "OLDER_RESULT_SECRET"})
        latest = self.create(2)

        response = self.reader().read()
        document = self.document(response)
        serialized = json.dumps(document, ensure_ascii=False)

        self.assertTrue(response["found"])
        self.assertFalse(response["available"])
        self.assertEqual(response["latest"]["task_id"], latest["task_id"])
        self.assertEqual(response["latest"]["state"], "queued")
        self.assertIsNone(response["result"])
        self.assertIsNone(response["error"])
        self.assertNotIn("OLDER_RESULT_SECRET", serialized)
        self.assertEqual(document["history"]["state_counts"], {"queued": 1, "succeeded": 1})

    def test_excerpt_idempotency_restart_and_unvalidated_runtime_rejection(self):
        record = self.create(1)
        self.finish(record, "succeeded", result={
            "result": {"value": "恢复读取"},
            "stdout": "captured output",
            "stdout_truncated": False,
        })
        reader = self.reader()
        first = reader.read(max_chars=12)
        second = reader.read(max_chars=12)

        self.assertTrue(first["truncated"])
        self.assertEqual(len(first["content_excerpt"]), 12)
        document = self.document(first)
        body = json.dumps({"result": document["result"], "error": document["error"]},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(first["total_chars"], len(body))
        self.assertEqual(first["preview_scope"], "latest_result_and_error")
        self.assertIsNone(first["result"])
        self.assertIsNone(first["error"])
        self.assertFalse(first["body_inline"])
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertEqual(len(list((self.home / "logs").glob("*.json"))), 1)

        self.store.close()
        self.store = TaskStore(self.database)
        resumed = self.reader().read()
        self.assertEqual(resumed["artifact"]["path"], first["artifact"]["path"])
        self.assertEqual(resumed["artifact"]["sha256"], first["artifact"]["sha256"])
        self.assertTrue(resumed["body_inline"])
        self.assertEqual(resumed["result"], document["result"])

        with tempfile.TemporaryDirectory(dir=self.home.parent) as outside:
            with self.assertRaises(PermissionError):
                self.reader(outside)


if __name__ == "__main__":
    unittest.main()
