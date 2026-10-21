"""Connection schemas preserve raw JSON types and the shared task classification."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_plugin.mcp.connections import register
from ansa_mcp_plugin.mcp.schemas import ToolResult
from ansa_mcp_shared.operations import TASK_TOOLS, writes_model


class ConnectionMcpChecks(unittest.IsolatedAsyncioTestCase):
    async def test_strict_schemas_and_bounded_task_submission(self):
        submit = Mock(return_value=ToolResult(ok=True, status="queued").as_mcp())
        server = FastMCP("connection schemas")
        register(server, submit)
        context = {"operation_id": "point-create", "expected_target": {
            "backend_id": "backend", "instance_id": "instance", "session_id": "session", "generation": 1},
            "expected_model": {"epoch": 1}}
        fields = {"position": [0, 1.5, -2], "diameter": 3,
                  "parts": [{"deck": 1, "type": "ANSAPART", "id": n} for n in (1, 2)]}
        await server.call_tool("ansa_connection_create", {**context, **fields})
        self.assertEqual(submit.call_args.args[0], "connection_create")
        self.assertEqual(submit.call_args.args[1], {**fields, "connection_type": "Bolt_Type", "entity_id": 0, "name": None})
        self.assertEqual(submit.call_args.args[-1], 300)
        invalid = ({"position": [True, 0, 0]}, {"position": ["0", 0, 0]}, {"position": "[0,0,0]"},
                   {"position": [0, 0]}, {"diameter": "3"}, {"diameter": True}, {"diameter": 0},
                   {"connection_type": "unsupported"}, {"entity_id": True}, {"entity_id": "1"},
                   {"entity_id": 2147483648}, {"name": 1}, {"name": "x" * 129}, {"deck": "1"},
                   {"ttl_seconds": True}, {"operation_id": 1}, {"card_values": {"D": 2}},
                   {"parts": [{"deck": 1, "type": "GRID", "id": 1}, fields["parts"][1]]},
                   {"parts": [{"deck": 1, "type": "ANSAPART", "id": "1"}, fields["parts"][1]]},
                   {"parts": [{**fields["parts"][0], "extra": 1}, fields["parts"][1]]})
        before = submit.call_count
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ToolError):
                await server.call_tool("ansa_connection_create", {**context, **fields, **change})
        self.assertEqual(submit.call_count, before)
        await server.call_tool("ansa_connection_list", {**context, "kind": "point", "limit": 2, "offset": 1})
        self.assertEqual(submit.call_args.args[0:2], ("connection_list", {"kind": "point", "limit": 2, "offset": 1}))
        for change in ({"limit": True}, {"limit": "2"}, {"limit": 201}, {"offset": -1},
                       {"offset": "0"}, {"kind": "line"}, {"fix": True}):
            with self.subTest(change=change), self.assertRaises(ToolError):
                await server.call_tool("ansa_connection_list", {**context, **change})
        for tool in await server.list_tools():
            self.assertFalse(tool.inputSchema["additionalProperties"])

    async def test_main_registration_and_read_write_classification(self):
        tools = await create_server(SimpleNamespace()).list_tools()
        self.assertEqual(len(tools), 69)
        self.assertTrue({"ansa_connection_list", "ansa_connection_create"}.issubset({tool.name for tool in tools}))
        self.assertTrue({"connection_list", "connection_create"}.issubset(TASK_TOOLS))
        self.assertFalse(writes_model("connection_list"))
        self.assertTrue(writes_model("connection_create"))


if __name__ == "__main__":
    unittest.main()
