"""Log reads preserve input bounds and never submit an ANSA operation."""

from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_shared.operations import TASK_TOOLS


class LogMcpChecks(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def structured(response):
        return (response[0] if isinstance(response, tuple) else response).structuredContent

    async def test_strict_schema_attachment_and_no_ansa_task(self):
        artifact = {"path": "C:/private/logs/example.json", "mime_type": "application/json", "sha256": "a" * 64, "bytes": 15}
        reader = SimpleNamespace(read=Mock(return_value={"found": True, "available": True, "artifact": artifact}))
        service = SimpleNamespace(log_reader=reader, submit_task=Mock())
        server = create_server(service)
        result = self.structured(await server.call_tool("ansa_log_read", {"max_chars": 32}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["artifacts"], [artifact])
        self.assertIsNone(result["task_id"])
        reader.read.assert_called_once_with(32)
        before = reader.read.call_count
        for arguments in ({"max_chars": True}, {"max_chars": "32"}, {"max_chars": None},
                          {"max_chars": 0}, {"max_chars": 65537}, {"path": "C:/other.json"},
                          {"operation_id": "unwanted"}, {"expected_target": {}}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await server.call_tool("ansa_log_read", arguments)
        self.assertEqual(reader.read.call_count, before)
        service.submit_task.assert_not_called()
        self.assertNotIn("log_read", TASK_TOOLS)
        tools = await server.list_tools()
        self.assertEqual(len(tools), 65)
        self.assertFalse(next(t for t in tools if t.name == "ansa_log_read").inputSchema["additionalProperties"])

    async def test_empty_history_and_storage_failure_are_distinct(self):
        reader = SimpleNamespace(read=Mock(return_value={"found": False, "available": False, "artifact": None}))
        server = create_server(SimpleNamespace(log_reader=reader))
        empty = self.structured(await server.call_tool("ansa_log_read", {}))
        self.assertTrue(empty["ok"])
        self.assertFalse(empty["data"]["found"])
        self.assertEqual(empty["artifacts"], [])
        reader.read.side_effect = PermissionError("private log path was replaced")
        failed = self.structured(await server.call_tool("ansa_log_read", {}))
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "LOG_UNAVAILABLE")
        self.assertEqual(failed["artifacts"], [])
        self.assertIsNone(failed["data"])


class LogStartupCleanupChecks(unittest.TestCase):
    def test_failed_reader_initialization_closes_recovered_connections_and_stores(self):
        from ansa_mcp_plugin.backend.service import Service

        connection, tasks, memory, store, lock = (Mock() for _ in range(5))
        captured = {}

        def start_scheduler(service):
            captured["service"] = service
            service.tasks = tasks
            service._stop_scheduler = threading.Event()
            service._scheduler = SimpleNamespace(ident=None)
            service.connections["recovered"] = connection

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / "runtime") as directory:
            with patch.multiple("ansa_mcp_plugin.backend.service",
                                ensure_safe_parent=Mock(), verify_private_path=Mock(),
                                ensure_private_directory=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
                                BackendLock=Mock(return_value=lock), BindingStore=Mock(return_value=store),
                                MemoryStore=Mock(return_value=memory),
                                TaskLogReader=Mock(side_effect=PermissionError("log reader initialization failed"))), \
                    patch.object(Service, "start_scheduler", start_scheduler):
                with self.assertRaisesRegex(PermissionError, "log reader initialization failed"):
                    Service(directory)
        service = captured["service"]
        self.assertTrue(service.closed)
        self.assertTrue(service._stop_scheduler.is_set())
        self.assertEqual(service.connections, {})
        for resource in (connection, tasks, memory, store, lock):
            resource.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
