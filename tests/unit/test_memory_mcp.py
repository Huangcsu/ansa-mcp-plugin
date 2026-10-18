"""Memory schema preserves raw JSON and reports local transaction failures."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_plugin.backend.memory import MemoryConflictError


class MemoryMcpChecks(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def structured(response):
        return (response[0] if isinstance(response, tuple) else response).structuredContent

    async def test_strict_change_union_and_raw_json_values(self):
        from mcp.server.fastmcp.exceptions import ToolError

        memory = SimpleNamespace(read=Mock(return_value={"found": False}), write=Mock(return_value={"scope": "runtime"}))
        server = create_server(SimpleNamespace(memory=memory))
        request = {"operation_id": "null", "change": {
            "kind": "preference", "key": "example", "value": None, "expected_revision": 0}}
        for value in (None, "null", "true", "1", True, [1, "2"], {"nested": "null"}):
            change = {**request["change"], "value": value}
            response = self.structured(await server.call_tool("ansa_memory_write", {**request, "change": change}))
            self.assertTrue(response["ok"])
            self.assertIsNone(response["task_id"])
            memory.write.assert_called_with("null", {**change, "note": ""})
        for extra in ({"operation_id": 1}, {"unexpected": True},
                      {"change": {k: v for k, v in request["change"].items() if k != "value"}},
                      {"change": {**request["change"], "expected_revision": True}},
                      {"change": {**request["change"], "expected_revision": "0"}},
                      {"change": {**request["change"], "extra": 1}},
                      {"change": {"kind": "note", "text": 1}},
                      {"change": {"kind": "note", "text": "example", "key": "unexpected"}}):
            with self.subTest(extra=extra), self.assertRaises(ToolError):
                await server.call_tool("ansa_memory_write", {**request, **extra})
        for arguments in ({"key": 1}, {"limit": True}, {"limit": "1"},
                          {"before_note_sequence": True}, {"before_note_sequence": "null"}, {"extra": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await server.call_tool("ansa_memory_read", arguments)
        await server.call_tool("ansa_memory_read", {"key": "null"})
        memory.read.assert_called_with("null", 30, None, None)
        await server.call_tool("ansa_memory_read", {"key": None})
        memory.read.assert_called_with(None, 30, None, None)
        tools = await server.list_tools()
        self.assertEqual(len(tools), 65)
        for name in ("ansa_memory_read", "ansa_memory_write"):
            tool = next(tool for tool in tools if tool.name == name)
            self.assertFalse(tool.inputSchema["additionalProperties"])

    async def test_memory_errors_do_not_create_model_tasks(self):
        memory = SimpleNamespace(read=Mock(), write=Mock())
        service = SimpleNamespace(memory=memory, submit_task=Mock())
        server = create_server(service)
        arguments = {"operation_id": "example", "change": {"kind": "note", "text": "synthetic"}}
        for exception, code in ((MemoryConflictError("stale revision"), "MEMORY_CONFLICT"),
                                (ValueError("invalid key"), "MEMORY_REJECTED"),
                                (RuntimeError("store unavailable"), "MEMORY_UNAVAILABLE")):
            memory.write.side_effect = exception
            response = self.structured(await server.call_tool("ansa_memory_write", arguments))
            self.assertFalse(response["ok"])
            self.assertEqual(response["operation_id"], "example")
            self.assertIsNone(response["task_id"])
            self.assertEqual(response["error"]["code"], code)
        for exception, code in ((ValueError("invalid cursor"), "MEMORY_REJECTED"),
                                (RuntimeError("store unavailable"), "MEMORY_UNAVAILABLE")):
            memory.read.side_effect = exception
            response = self.structured(await server.call_tool("ansa_memory_read", {}))
            self.assertEqual(response["error"]["code"], code)
        service.submit_task.assert_not_called()
