"""Bounded task-history pagination and MCP query behavior."""

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from ansa_mcp_plugin.backend.scheduler import TaskNotFoundError, TaskScheduler
from ansa_mcp_plugin.backend.tasks import TaskStore


STATES = ("queued", "running", "succeeded", "failed", "cancelled", "expired", "unknown")


class TaskHistoryStoreChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = TaskStore(self.path)
        self.addCleanup(lambda: self.store.close())

    @staticmethod
    def request(params=None):
        return {
            "tool": "script_execute",
            "params": {"script": "print('task')"} if params is None else params,
            "target": {"backend_id": "backend-a", "instance_id": "ansa-a",
                       "session_id": "session-a", "generation": 1},
            "model": {"epoch": 1},
            "ttl_seconds": None,
        }

    def create(self, index, params=None):
        return self.store.create(
            f"operation-{index}", self.request(params),
            {"pid": 1234, "process_start": "start", "instance_id": "ansa-a",
             "session_id": "session-a"}, None,
        )

    def test_history_page_filters_all_states_and_returns_full_records(self):
        created = [self.create(index) for index in range(len(STATES))]
        for state, record in zip(STATES, created):
            self.store._db.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, record["task_id"]))

        for state, record in zip(STATES, created):
            with self.subTest(state=state):
                page = self.store.history_page(state=state)
                self.assertFalse(page["has_more"])
                self.assertIsNone(page["next_before_sequence"])
                self.assertEqual([item["task_id"] for item in page["tasks"]], [record["task_id"]])
                self.assertEqual(set(page["tasks"][0]), {
                    "task_id", "operation_id", "state", "dispatch", "cancel_pending",
                    "task", "result", "error", "acked",
                })
                self.assertIn("params", page["tasks"][0]["task"])

    def test_cursor_stays_stable_after_insert_and_store_restart(self):
        initial = [self.create(index) for index in range(5)]
        first_page = self.store.history_page(limit=2)
        first_ids = [record["task_id"] for record in first_page["tasks"]]
        self.assertEqual(first_ids, [initial[4]["task_id"], initial[3]["task_id"]])
        self.assertTrue(first_page["has_more"])
        expected_cursor = self.store._db.execute(
            "SELECT seq FROM tasks WHERE task_id=?", (first_ids[-1],)
        ).fetchone()[0]
        self.assertEqual(first_page["next_before_sequence"], expected_cursor)

        self.create(5)
        self.store.close()
        self.store = TaskStore(self.path)
        second_page = self.store.history_page(limit=2, before_sequence=first_page["next_before_sequence"])
        second_ids = [record["task_id"] for record in second_page["tasks"]]
        self.assertEqual(second_ids, [initial[2]["task_id"], initial[1]["task_id"]])
        self.assertTrue(first_page["has_more"])
        self.assertTrue(set(first_ids).isdisjoint(second_ids))
        self.assertNotIn("operation-5", [record["operation_id"] for record in second_page["tasks"]])

    def test_empty_end_pages_and_strict_history_parameters(self):
        empty = self.store.history_page()
        self.assertEqual(empty, {"tasks": [], "has_more": False, "next_before_sequence": None})
        record = self.create(1)
        only = self.store.history_page(limit=1)
        self.assertEqual([item["task_id"] for item in only["tasks"]], [record["task_id"]])
        self.assertFalse(only["has_more"])
        self.assertIsNone(only["next_before_sequence"])
        self.assertEqual(self.store.history_page(before_sequence=1)["tasks"], [])

        for limit in (True, False, 0, 101, 1.0, "1", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.history_page(limit=limit)
        for state in (True, 1, "pending", "queued' OR 1=1 --"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.store.history_page(state=state)
        for cursor in (True, False, 0, -1, 1.0, "1", 2**63):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.store.history_page(before_sequence=cursor)


class TaskHistoryMcpChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    @staticmethod
    def request(params=None):
        return {
            "tool": "script_execute",
            "params": {"script": "print('task')"} if params is None else params,
            "target": {"backend_id": "backend-a", "instance_id": "ansa-a",
                       "session_id": "session-a", "generation": 1},
            "model": {"epoch": 1},
            "ttl_seconds": None,
        }

    def create(self, operation_id, params=None):
        return self.store.create(
            operation_id, self.request(params),
            {"pid": 1234, "process_start": "start", "instance_id": "ansa-a",
             "session_id": "session-a"}, None,
        )

    def make_server(self):
        scheduler = object.__new__(TaskScheduler)
        scheduler._lock = threading.RLock()
        scheduler.tasks = self.store
        service = SimpleNamespace(tasks=self.store, query_task=scheduler.query_task)
        from ansa_mcp_plugin.__main__ import create_server
        return create_server(service), scheduler

    @staticmethod
    def structured(result):
        if isinstance(result, tuple):
            result = result[0]
        return result.structuredContent

    async def test_query_rejection_dual_ids_not_found_failure_and_unknown(self):
        from mcp.server.fastmcp.exceptions import ToolError

        server, scheduler = self.make_server()
        first = self.create("operation-1")
        second = self.create("operation-2")

        rejected = self.structured(await server.call_tool("ansa_task_query", {}))
        self.assertEqual(rejected["error"]["code"], "QUERY_REJECTED")

        found = self.structured(await server.call_tool("ansa_task_query", {
            "task_id": first["task_id"], "operation_id": first["operation_id"],
        }))
        self.assertTrue(found["ok"])
        self.assertEqual(found["task_id"], first["task_id"])

        for literal_id in ("null", "true", "123"):
            literal_record = self.create(literal_id)
            literal_result = self.structured(await server.call_tool("ansa_task_query", {
                "operation_id": literal_id,
            }))
            self.assertEqual(literal_result["task_id"], literal_record["task_id"])
        explicit_null = self.structured(await server.call_tool("ansa_task_query", {
            "task_id": None, "operation_id": first["operation_id"],
        }))
        self.assertEqual(explicit_null["task_id"], first["task_id"])

        with self.assertRaises(ToolError):
            await server.call_tool("ansa_task_query", {"task_id": 7})
        with self.assertRaises(ToolError):
            await server.call_tool("ansa_task_query", {"task_id": "x" * 129})

        missing = self.structured(await server.call_tool("ansa_task_query", {"task_id": "missing"}))
        self.assertEqual(missing["error"]["code"], "TASK_NOT_FOUND")
        mismatched = self.structured(await server.call_tool("ansa_task_query", {
            "task_id": first["task_id"], "operation_id": second["operation_id"],
        }))
        self.assertEqual(mismatched["error"]["code"], "TASK_NOT_FOUND")

        class BrokenStore:
            def get(self, **_kwargs):
                raise sqlite3.OperationalError("database read failed")

        scheduler.tasks = BrokenStore()
        unavailable = self.structured(await server.call_tool("ansa_task_query", {"task_id": first["task_id"]}))
        self.assertEqual(unavailable["error"]["code"], "QUERY_UNAVAILABLE")

        scheduler.tasks = self.store
        self.store._db.execute("UPDATE tasks SET state='unknown' WHERE task_id=?", (first["task_id"],))
        unknown = self.structured(await server.call_tool("ansa_task_query", {"task_id": first["task_id"]}))
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["error"]["code"], "TASK_UNKNOWN")
        with self.assertRaises(TaskNotFoundError):
            scheduler.query_task("missing", None)

    async def test_task_list_filters_shape_privacy_strict_schema_and_tool_count(self):
        from mcp.server.fastmcp.exceptions import ToolError

        server, _scheduler = self.make_server()
        record = self.create("operation-secret", {
            "script": "SECRET_SCRIPT_BODY", "params": "SECRET_REQUEST_BODY",
        })
        self.store._db.execute(
            "UPDATE tasks SET result_json=?,error_json=? WHERE task_id=?",
            (json.dumps("SECRET_RESULT_BODY"), json.dumps("SECRET_ERROR_BODY"), record["task_id"]),
        )

        tools = await server.list_tools()
        self.assertEqual(len(tools), 69)
        for name in ("ansa_task_list", "ansa_task_query"):
            tool = next(item for item in tools if item.name == name)
            self.assertIs(tool.inputSchema.get("additionalProperties"), False)
        list_schema = next(item for item in tools if item.name == "ansa_task_list").inputSchema
        self.assertTrue({"limit", "state", "before_sequence"}.issubset(list_schema["properties"]))

        response = self.structured(await server.call_tool("ansa_task_list", {
            "limit": 10, "state": "queued", "before_sequence": 2**63 - 1,
        }))
        data = response["data"]
        self.assertEqual(set(data), {
            "tasks", "limit", "state", "before_sequence", "has_more", "next_before_sequence",
        })
        self.assertEqual((data["limit"], data["state"], data["before_sequence"]), (10, "queued", 2**63 - 1))
        self.assertEqual(len(data["tasks"]), 1)
        self.assertEqual(set(data["tasks"][0]), {
            "task_id", "operation_id", "state", "tool", "dispatch", "cancel_pending", "target", "model",
        })
        serialized = json.dumps(data, ensure_ascii=False)
        for secret in ("SECRET_SCRIPT_BODY", "SECRET_REQUEST_BODY", "SECRET_RESULT_BODY", "SECRET_ERROR_BODY"):
            self.assertNotIn(secret, serialized)

        nullable = self.structured(await server.call_tool("ansa_task_list", {
            "limit": 10, "state": None, "before_sequence": None,
        }))
        self.assertEqual(nullable["data"]["tasks"], data["tasks"])
        self.assertIsNone(nullable["data"]["state"])
        self.assertIsNone(nullable["data"]["before_sequence"])

        for name, args in (
            ("ansa_task_list", {"unexpected": 1}),
            ("ansa_task_list", {"limit": True}),
            ("ansa_task_list", {"state": 1}),
            ("ansa_task_list", {"before_sequence": True}),
            ("ansa_task_list", {"before_sequence": "null"}),
            ("ansa_task_list", {"state": "null"}),
            ("ansa_task_query", {"unexpected": 1}),
        ):
            with self.subTest(name=name, args=args), self.assertRaises(ToolError):
                await server.call_tool(name, args)
