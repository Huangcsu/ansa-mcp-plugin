"""Focused checks for the scoped, read-only mesh intersection operation."""

import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import execute as dispatch_operation
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError
from ansa_mcp_shared.operations import READ_ONLY_TOOLS, writes_model
from test_mesh_checks import FakeBase, FakeState


class FakeIntersectionBase(FakeBase):
    def __init__(self):
        super().__init__()
        self.native_result = {"intersected": [], "penetrated": [], "penetrations": 0}
        self.native_calls = []

    def CheckPenetration2Sets(self, entities1, entities2, mode, factor, fast_run):
        self.native_calls.append((
            list(entities1),
            list(entities2),
            {"mode": mode, "factor": factor, "fast_run": fast_run},
        ))
        return self.native_result


class MeshIntersections(unittest.TestCase):
    def setUp(self):
        self.base = FakeIntersectionBase()
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def params(self, first_ids=(11,), second_ids=(12,), **overrides):
        params = {
            "deck": 1,
            "first_ids": list(first_ids),
            "second_ids": list(second_ids),
        }
        params.update(overrides)
        return params

    def run_operation(self, params=None):
        return dispatch_operation(
            self.state,
            "mesh_intersections",
            self.params() if params is None else params,
        )

    def test_returns_sorted_unique_shells_and_preserves_native_counts(self):
        shell_11 = self.base.add(11)
        shell_12 = self.base.add(12)
        shell_13 = self.base.add(13)
        shell_14 = self.base.add(14)
        self.base.native_result = {
            "intersected": [shell_14, shell_11, shell_12, shell_13, shell_12],
            "penetrated": [],
            "penetrations": 23,
        }

        result = self.run_operation(self.params((13, 11), (14, 12)))

        self.assertEqual(result["check"], "intersections")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["auto_fix"])
        self.assertEqual(result["scope"], {"first_ids": [13, 11], "second_ids": [14, 12]})
        self.assertEqual(result["native_options"], {"mode": 1, "factor": 0.0, "fast_run": 0})
        self.assertEqual(result["native_count"], 23)
        self.assertEqual(result["native_intersected_list_count"], 5)
        self.assertEqual(result["intersected"]["entity_ids"], [11, 12, 13, 14])
        self.assertEqual(result["intersected"]["count"], 4)
        self.assertEqual(result["intersected"]["first_ids"], [11, 13])
        self.assertEqual(result["intersected"]["second_ids"], [12, 14])
        self.assertEqual(
            result["intersected"]["references"],
            [{"deck": 1, "type": "SHELL", "id": entity_id}
             for entity_id in (11, 12, 13, 14)],
        )
        self.assertEqual(self.base.native_calls, [(
            [shell_13, shell_11],
            [shell_14, shell_12],
            {"mode": 1, "factor": 0.0, "fast_run": 0},
        )])
        self.assertIn("mesh_intersections", READ_ONLY_TOOLS)
        self.assertFalse(writes_model("mesh_intersections"))

    def test_empty_native_result_is_reported_as_zero(self):
        self.base.add(11)
        self.base.add(12)

        result = self.run_operation()

        self.assertEqual(result["native_count"], 0)
        self.assertEqual(result["native_intersected_list_count"], 0)
        self.assertEqual(result["intersected"], {
            "entity_ids": [], "count": 0, "first_ids": [], "second_ids": [], "references": [],
        })

    def test_rejects_strict_invalid_batches_before_entity_reads(self):
        invalid = (
            {"deck": True},
            {"deck": 0},
            {"first_ids": []},
            {"first_ids": [True]},
            {"second_ids": [0]},
            {"first_ids": [11, 11]},
            {"first_ids": list(range(1, 202))},
            {"first_ids": [11], "second_ids": [11]},
            {"check": "intersections"},
            {"fix": True},
            {"unknown": True},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises((TypeError, ValueError)):
                    self.run_operation(self.params(**changed))
        self.assertEqual(self.base.get_entity_calls, [])
        self.assertEqual(self.base.native_calls, [])

    def test_missing_or_wrong_shell_is_rejected_before_native_call(self):
        self.base.add(11)
        self.base.add(12)
        with self.assertRaises(LookupError):
            self.run_operation(self.params((11, 999), (12,)))
        self.assertEqual(self.base.native_calls, [])

        self.base.entities[("SHELL", 11)]._type = "FACE"
        with self.assertRaises(RuntimeError):
            self.run_operation()
        self.assertEqual(self.base.native_calls, [])

    def test_malformed_out_of_scope_and_inconsistent_native_results_fail_closed(self):
        shell_11 = self.base.add(11)
        shell_12 = self.base.add(12)
        shell_13 = self.base.add(13)
        invalid_results = (
            None,
            {"intersected": [shell_11, shell_12], "penetrated": []},
            {"intersected": [shell_11, shell_12], "penetrated": [], "penetrations": True},
            {"intersected": [shell_11, shell_12], "penetrated": [], "penetrations": -1},
            {"intersected": [shell_13], "penetrated": [], "penetrations": 1},
            {"intersected": [shell_11], "penetrated": [], "penetrations": 1},
            {"intersected": [object()], "penetrated": [], "penetrations": 1},
            {"intersected": [shell_11] * 401, "penetrated": [], "penetrations": 1},
            {"intersected": [shell_11, shell_12], "penetrated": [shell_11], "penetrations": 1},
            {"intersected": [], "penetrated": [], "penetrations": 1},
        )
        for native_result in invalid_results:
            with self.subTest(native_result=native_result):
                self.base.native_result = native_result
                with self.assertRaises(RuntimeError):
                    self.run_operation()

    def test_missing_native_api_is_rejected_before_entity_reads(self):
        self.base.CheckPenetration2Sets = None

        with self.assertRaises(CapabilityUnavailableError):
            self.run_operation()

        self.assertEqual(self.base.get_entity_calls, [])
        self.assertEqual(self.base.native_calls, [])


class MeshIntersectionsMcpSchema(unittest.IsolatedAsyncioTestCase):
    async def test_schema_is_strict_and_submits_only_the_explicit_read_scope(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.mcp.intersections import register
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

        server = FastMCP("mesh-intersections-schema")
        register(server, submit)
        args = {
            "operation_id": "mesh-intersections",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "first_ids": [11],
            "second_ids": [12],
        }
        invalid = (
            {"operation_id": ""},
            {"expected_target": {**target, "generation": True}},
            {"expected_model": {"epoch": True}},
            {"deck": True},
            {"first_ids": []},
            {"first_ids": [True]},
            {"second_ids": [0]},
            {"second_ids": list(range(1, 202))},
            {"ttl_seconds": True},
            {"ttl_seconds": 86401},
            {"check": "intersections"},
            {"fix": True},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    await server.call_tool("ansa_mesh_intersections", {**args, **changed})
        self.assertEqual(submitted, [])

        await server.call_tool("ansa_mesh_intersections", args)
        self.assertEqual(submitted, [(
            "mesh_intersections",
            {"deck": 1, "first_ids": [11], "second_ids": [12]},
            "mesh-intersections",
            300,
        )])

    async def test_main_server_registers_the_new_tool(self):
        from ansa_mcp_plugin.__main__ import create_server

        server = create_server(object())
        tools = await server.list_tools()
        tool = next(item for item in tools if item.name == "ansa_mesh_intersections")
        properties = tool.inputSchema["properties"]

        self.assertTrue({"first_ids", "second_ids", "deck"}.issubset(properties))
        self.assertIs(tool.inputSchema.get("additionalProperties"), False)
        self.assertNotIn("check", properties)
        self.assertNotIn("fix", properties)


if __name__ == "__main__":
    unittest.main()
