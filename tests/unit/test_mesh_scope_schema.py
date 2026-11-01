import types
import unittest
from unittest.mock import Mock, patch


class MeshScopeSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_tools_have_strict_bounded_schemas_and_submit_contracts(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        from ansa_mcp_plugin.mcp.mesh_scope import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        submitted = []
        target = {"backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1}

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("mesh-scope-schema")
        register(server, submit)
        read_schema = server._tool_manager.get_tool("ansa_mesh_scope_read").parameters
        freeze_schema = server._tool_manager.get_tool("ansa_mesh_freeze").parameters
        self.assertIs(read_schema["additionalProperties"], False)
        self.assertIs(freeze_schema["additionalProperties"], False)
        self.assertEqual(set(read_schema["properties"]), {
            "operation_id", "expected_target", "expected_model", "deck", "entity_type",
            "entity_ids", "ttl_seconds",
        })
        self.assertEqual(set(freeze_schema["properties"]), {
            "operation_id", "expected_target", "expected_model", "deck", "entity_type",
            "entity_ids", "action", "expected_scope_sha256", "backup_path", "ttl_seconds",
        })
        self.assertEqual(read_schema["properties"]["entity_type"]["enum"],
                         ["FACE", "CONS", "SHELL", "SOLID", "VOLUME"])
        self.assertEqual(freeze_schema["properties"]["entity_type"]["enum"],
                         ["FACE", "SHELL", "SOLID"])
        self.assertEqual(freeze_schema["properties"]["entity_ids"]["minItems"], 1)
        self.assertEqual(read_schema["properties"]["entity_ids"]["maxItems"], 200)

        read_args = {
            "operation_id": "scope-read",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "entity_type": "FACE",
            "entity_ids": [12, 13],
        }
        freeze_args = {
            "operation_id": "scope-freeze",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "entity_type": "SHELL",
            "entity_ids": [20],
            "action": "freeze",
            "expected_scope_sha256": "a" * 64,
            "backup_path": "C:/models/mesh-scope-backup.ansa",
        }
        for tool_name, args, invalid in (
            ("ansa_mesh_scope_read", read_args, (
                {"deck": True}, {"deck": 0}, {"entity_type": "MACRO"},
                {"entity_ids": []}, {"entity_ids": [True]}, {"entity_ids": [0]},
                {"entity_ids": [12, 12]}, {"entity_ids": list(range(1, 202))},
                {"ttl_seconds": True}, {"ttl_seconds": 86401}, {"write": True},
            )),
            ("ansa_mesh_freeze", freeze_args, (
                {"deck": True}, {"deck": 0}, {"entity_type": "CONS"},
                {"entity_ids": [True]}, {"entity_ids": [20, 20]},
                {"action": "Freeze"}, {"expected_scope_sha256": "A" * 64},
                {"expected_scope_sha256": "a" * 63}, {"backup_path": ""},
                {"ttl_seconds": True}, {"fix": True},
            )),
        ):
            for changed in invalid:
                with self.subTest(tool=tool_name, changed=changed):
                    with self.assertRaises(ToolError):
                        await server.call_tool(tool_name, {**args, **changed})
        self.assertEqual(submitted, [])

        await server.call_tool("ansa_mesh_scope_read", read_args)
        await server.call_tool("ansa_mesh_freeze", freeze_args)
        self.assertEqual(submitted, [
            ("mesh_scope_read", {"deck": 1, "entity_type": "FACE", "entity_ids": [12, 13]},
             "scope-read", 300),
            ("mesh_freeze", {
                "deck": 1, "entity_type": "SHELL", "entity_ids": [20], "action": "freeze",
                "expected_scope_sha256": "a" * 64,
                "backup_path": "C:/models/mesh-scope-backup.ansa",
            }, "scope-freeze", 300),
        ])

    async def test_server_registration_classification_and_bridge_dispatch(self):
        from ansa_mcp_bridge.operations import execute
        from ansa_mcp_plugin.__main__ import create_server
        from ansa_mcp_shared.operations import READ_ONLY_TOOLS, TASK_TOOLS, writes_model

        tools = await create_server(object()).list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(len(tools), 93)
        self.assertTrue({"ansa_mesh_scope_read", "ansa_mesh_freeze"} <= names)
        self.assertIn("mesh_scope_read", READ_ONLY_TOOLS)
        self.assertIn("mesh_scope_read", TASK_TOOLS)
        self.assertFalse(writes_model("mesh_scope_read"))
        self.assertNotIn("mesh_freeze", READ_ONLY_TOOLS)
        self.assertIn("mesh_freeze", TASK_TOOLS)
        self.assertTrue(writes_model("mesh_freeze"))

        state = types.SimpleNamespace(assert_main=Mock())
        for tool in ("mesh_scope_read", "mesh_freeze"):
            params = {"deck": 1, "entity_ids": [7]}
            with patch("ansa_mcp_bridge.operations.mesh_scope.execute",
                       side_effect=lambda state, tool, params: (state, tool, params)) as dispatched:
                self.assertEqual(execute(state, tool, params), (state, tool, params))
                dispatched.assert_called_with(state, tool, params)


if __name__ == "__main__":
    unittest.main()
